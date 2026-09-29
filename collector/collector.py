#!/usr/bin/env python3
"""Database Health Dashboard collector.

Polls GCP Cloud Monitoring + direct MySQL (or generates synthetic data in
--mock mode), runs the anomaly detection / scoring / maintenance / cost
analysis, and writes src/status.json for the static dashboard to poll.
Can optionally serve the dashboard directory itself over HTTP.

Usage:
  python collector.py --mock --serve              # zero-setup demo
  python collector.py --mock --once               # single sample, no server
  python collector.py --serve                      # real GCP + MySQL
  python collector.py --mock --mock-anomaly cpu    # force an alerting demo
"""
import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

sys.path.insert(0, os.path.dirname(__file__))

from config import Config, METRICS, METRIC_ORDER, safe_str  # noqa: E402
from history import append_sample, load_history, prune_history, baseline_for  # noqa: E402
from analysis import (  # noqa: E402
    evaluate_metric, evaluate_connections, composite_health_score, build_alerts,
    recommend_maintenance_window, forecast_storage_runway, long_term_rightsizing,
    estimate_monthly_cost, build_recommendations,
)
from sources import collect_gcp_metrics, collect_mysql_metrics, resolve_provisioned_spec  # noqa: E402
from mock import generate_mock_sample  # noqa: E402
from ai_analysis import get_ai_root_cause  # noqa: E402

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")
STATUS_PATH = os.path.join(SRC_DIR, "status.json")
COST_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "cost_config.json")
# The dashboard's calendar/time-range picker needs the full retained window
# (up to 30 days) available client-side to filter, not just a recent tail —
# but sending every raw sample at a short poll interval over 30 days could
# be huge, so it's decimated to an even stride, always keeping the latest point.
MAX_HISTORY_POINTS_FOR_UI = 6000


def _decimate(samples, max_points):
    n = len(samples)
    if n <= max_points:
        return samples
    step = n / max_points
    indices = sorted({int(i * step) for i in range(max_points)})
    if indices[-1] != n - 1:
        indices[-1] = n - 1
    return [samples[i] for i in indices]


def load_cost_config():
    try:
        with open(COST_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[collector] WARNING: could not load cost_config.json ({safe_str(e)}); using built-in defaults", file=sys.stderr)
        return {
            "vcpu_hour_usd": 0.0413, "memory_gb_hour_usd": 0.0070,
            "storage_gb_month_usd": 0.17, "hours_per_month": 730,
            "ha_multiplier": 2.0, "nonprod_offhours_uptime_fraction": 0.36,
        }


def collect_one_sample(cfg, mock, mock_anomaly):
    """Gathers raw metric values plus per-source availability. Each source
    is independent: a GCP failure never blocks MySQL values and vice versa."""
    now = time.time()

    if mock:
        values = generate_mock_sample(now, anomaly_metrics=mock_anomaly)
        data_sources = {
            "gcp_monitoring": {"available": True, "reason": None, "mode": "mock"},
            "mysql": {"available": True, "reason": None, "mode": "mock"},
        }
        max_connections = 200
    else:
        gcp_values, gcp_status = collect_gcp_metrics(cfg)
        mysql_values, mysql_status = collect_mysql_metrics(cfg)
        values = {}
        values.update(gcp_values)
        values.update(mysql_values)
        data_sources = {"gcp_monitoring": gcp_status, "mysql": mysql_status}
        max_connections = mysql_status.get("max_connections")

    sample_metrics = {m: values.get(m) for m in METRIC_ORDER}
    return now, sample_metrics, data_sources, max_connections


def build_status(cfg, cost_cfg, mock, mock_anomaly):
    now, sample_metrics, data_sources, max_connections = collect_one_sample(cfg, mock, mock_anomaly)

    append_sample({"ts": now, "metrics": sample_metrics})
    samples = prune_history(now)

    evaluations = {}
    for metric_id in METRIC_ORDER:
        value = sample_metrics.get(metric_id)
        baseline = baseline_for(samples, metric_id, exclude_last=True)
        if metric_id == "connections":
            evaluations[metric_id] = evaluate_connections(value, baseline, max_connections)
        else:
            evaluations[metric_id] = evaluate_metric(metric_id, value, baseline)

    health_score, health_label = composite_health_score(evaluations)
    alerts = build_alerts(evaluations)

    maintenance = recommend_maintenance_window(samples)
    storage_runway = forecast_storage_runway(samples, now)
    rightsizing = long_term_rightsizing(samples)

    provisioned_spec = resolve_provisioned_spec(cfg)
    cost = estimate_monthly_cost(provisioned_spec, cost_cfg)
    recommendations = build_recommendations(cfg.instance_name, provisioned_spec, rightsizing, cost, cost_cfg)

    ai_analysis = get_ai_root_cause(cfg, alerts, provisioned_spec)
    data_sources["ai_analysis"] = {
        "available": ai_analysis["available"],
        "reason": ai_analysis.get("reason"),
    }

    history_samples = _decimate(samples, MAX_HISTORY_POINTS_FOR_UI)
    metrics_out = {}
    for metric_id in METRIC_ORDER:
        meta = METRICS[metric_id]
        ev = evaluations[metric_id]
        metrics_out[metric_id] = {
            "label": meta["label"],
            "unit": meta["unit"],
            "value": ev["value"],
            "mean": ev.get("mean"),
            "stddev": ev.get("stddev"),
            "z": ev.get("z"),
            "severity": ev["severity"],
            "warn_limit": ev.get("warn_limit"),
            "crit_limit": ev.get("crit_limit"),
            "history": [
                {"ts": s["ts"], "value": s.get("metrics", {}).get(metric_id)}
                for s in history_samples
            ],
        }

    status = {
        "generated_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "instance_name": cfg.instance_name,
        "mode": "mock" if mock else "live",
        "health_score": health_score,
        "health_label": health_label,
        "metrics": metrics_out,
        "alerts": alerts,
        "maintenance": maintenance,
        "storage_runway": storage_runway,
        "rightsizing": rightsizing,
        "provisioned_spec": provisioned_spec,
        "cost_estimate": cost,
        "recommendations": recommendations,
        "ai_analysis": ai_analysis,
        "data_sources": data_sources,
    }
    return status


def write_status(status):
    os.makedirs(SRC_DIR, exist_ok=True)
    tmp_path = STATUS_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)
    os.replace(tmp_path, STATUS_PATH)


def serve_dashboard(port):
    handler = partial(SimpleHTTPRequestHandler, directory=SRC_DIR)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    print(f"[collector] Serving dashboard at http://127.0.0.1:{port}/")
    return httpd


def main():
    parser = argparse.ArgumentParser(description="Database Health Dashboard collector")
    parser.add_argument("--mock", action="store_true", help="Generate synthetic data instead of hitting GCP/MySQL")
    parser.add_argument("--mock-anomaly", default=None, help="Comma-separated metric ids to spike in mock mode, e.g. cpu,connections (for demoing the alerting state)")
    parser.add_argument("--once", action="store_true", help="Collect a single sample and exit")
    parser.add_argument("--serve", action="store_true", help="Also serve the dashboard directory over HTTP")
    parser.add_argument("--port", type=int, default=None, help="HTTP port (default: HTTP_PORT env var or 8000)")
    parser.add_argument("--interval", type=int, default=None, help="Poll interval in seconds (default: POLL_INTERVAL_SECONDS env var or 60)")
    args = parser.parse_args()

    cfg = Config()
    cost_cfg = load_cost_config()
    interval = args.interval or cfg.poll_interval_seconds
    port = args.port or cfg.http_port
    mock_anomalies = [m.strip() for m in args.mock_anomaly.split(",")] if args.mock_anomaly else None
    invalid = [m for m in (mock_anomalies or []) if m not in METRIC_ORDER]
    if invalid:
        parser.error(f"unknown metric(s) for --mock-anomaly: {', '.join(invalid)} (choices: {', '.join(METRIC_ORDER)})")

    if args.mock:
        print("[collector] Running in --mock mode: synthetic data, no cloud/DB access needed.")
    else:
        print("[collector] Running in live mode: will attempt GCP Cloud Monitoring + direct MySQL.")

    httpd = serve_dashboard(port) if args.serve else None

    try:
        while True:
            status = build_status(cfg, cost_cfg, args.mock, mock_anomalies)
            write_status(status)
            print(f"[collector] {status['generated_at']} health={status['health_score']} ({status['health_label']}) alerts={len(status['alerts'])}")
            if args.once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[collector] Stopped.")
    finally:
        if httpd is not None:
            httpd.shutdown()


if __name__ == "__main__":
    main()
