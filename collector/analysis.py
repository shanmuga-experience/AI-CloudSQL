"""Requirements 1-5: self-baselining anomaly detection, composite health
score, plain-English alerts, maintenance-window/runway forecasting, and
long-term right-sizing + cost estimation. Pure functions over history/config
so they're easy to unit-check without a live DB or GCP connection."""
import statistics
from datetime import datetime, timezone, timedelta

from config import METRICS

SEVERITY_RANK = {"ok": 0, "warning": 1, "critical": 2}
MIN_SAMPLES_FOR_STATS = 5


def _worse(a, b):
    return a if SEVERITY_RANK[a] >= SEVERITY_RANK[b] else b


def compute_limits(meta, mean, stddev, n):
    """The actual value that would trigger warning/critical right now for
    this widget's "limit" display — the lower (more sensitive) of the
    fixed hard ceiling and this instance's own statistical baseline
    (mean + 2sigma / mean + 3sigma). None when neither applies yet (not
    enough history AND no hard ceiling defined for this metric type)."""
    stat_warn = stat_crit = None
    if mean is not None and n >= MIN_SAMPLES_FOR_STATS and stddev is not None:
        stat_warn = mean + 2 * stddev
        stat_crit = mean + 3 * stddev

    def combine(hard, stat):
        candidates = [x for x in (hard, stat) if x is not None]
        return min(candidates) if candidates else None

    return combine(meta["hard_warn"], stat_warn), combine(meta["hard_crit"], stat_crit)


def evaluate_metric(metric_id, value, baseline):
    """Requirement 1: statistical z-score baseline PLUS a hard-ceiling floor
    for cold start, combined by taking whichever is more severe."""
    meta = METRICS[metric_id]
    mean, stddev, n = baseline.get("mean"), baseline.get("stddev"), baseline.get("n", 0)
    warn_limit, crit_limit = compute_limits(meta, mean, stddev, n)
    result = {
        "value": value,
        "mean": mean,
        "stddev": stddev,
        "n": n,
        "z": None,
        "severity": "ok",
        "trigger": None,
        "warn_limit": warn_limit,
        "crit_limit": crit_limit,
    }
    if value is None:
        result["severity"] = "unavailable"
        return result

    stat_severity = "ok"
    if mean is not None and n >= MIN_SAMPLES_FOR_STATS:
        if stddev and stddev > 1e-9:
            z = (value - mean) / stddev
            result["z"] = z
            if abs(z) >= 3:
                stat_severity = "critical"
            elif abs(z) >= 2:
                stat_severity = "warning"
        else:
            # No variance at all in a long-enough history is itself notable
            # only if the current value actually differs from that constant.
            result["z"] = 0.0

    hard_severity = "ok"
    if meta["hard_crit"] is not None and value >= meta["hard_crit"]:
        hard_severity = "critical"
    elif meta["hard_warn"] is not None and value >= meta["hard_warn"]:
        hard_severity = "warning"

    final = _worse(stat_severity, hard_severity)
    result["severity"] = final
    if final != "ok":
        result["trigger"] = "statistical" if SEVERITY_RANK[stat_severity] >= SEVERITY_RANK[hard_severity] else "hard_ceiling"
    return result


def evaluate_connections(value, baseline, max_connections):
    """Connections gets a dynamic hard ceiling relative to max_connections
    (80%/95%) instead of a fixed absolute count."""
    result = evaluate_metric("connections", value, baseline)
    if not max_connections:
        return result

    hard_warn_abs = max_connections * 0.80
    hard_crit_abs = max_connections * 0.95
    result["warn_limit"] = min(x for x in (result["warn_limit"], hard_warn_abs) if x is not None)
    result["crit_limit"] = min(x for x in (result["crit_limit"], hard_crit_abs) if x is not None)

    if value is None:
        return result
    pct = (value / max_connections) * 100
    hard_severity = "critical" if pct >= 95 else "warning" if pct >= 80 else "ok"
    stat_severity = result["severity"] if result["severity"] in SEVERITY_RANK else "ok"
    final = _worse(stat_severity, hard_severity)
    if final != result["severity"]:
        result["trigger"] = "hard_ceiling"
    result["severity"] = final
    result["pct_of_max"] = pct
    return result


def composite_health_score(evaluations):
    """Requirement 2: one 0-100 number, weighted by severity and per-metric
    weight, so nobody has to mentally fuse seven graphs."""
    score = 100.0
    for metric_id, ev in evaluations.items():
        weight = METRICS[metric_id]["weight"]
        if ev["severity"] == "critical":
            score -= 20 * weight
        elif ev["severity"] == "warning":
            score -= 8 * weight
        elif ev["severity"] == "unavailable":
            # Missing data is not good health. Without this penalty a collector whose
            # GCP and MySQL sources had both failed still reported 100 "Healthy".
            # 10*weight: one lost source is at least Degraded, both lost is Critical,
            # while QPS's normal first-poll gap (it needs two polls) stays Healthy.
            score -= 10 * weight
    score = max(0.0, min(100.0, score))
    if score >= 85:
        label = "Healthy"
    elif score >= 60:
        label = "Degraded"
    else:
        label = "Critical"
    return round(score, 1), label


def _fmt(value, unit):
    if unit == "%":
        return f"{value:.1f}%"
    if unit == "GB":
        return f"{value:.1f} GB"
    return f"{value:.1f} {unit}"


def build_alerts(evaluations):
    """Requirement 3: every alert is a sentence with the actual number and
    baseline, plus one concrete suggested action — never just a metric name."""
    alerts = []
    for metric_id, ev in evaluations.items():
        if ev["severity"] not in ("warning", "critical"):
            continue
        meta = METRICS[metric_id]
        unit = meta["unit"]
        value_s = _fmt(ev["value"], unit)
        if ev.get("z") is not None and ev["n"] >= MIN_SAMPLES_FOR_STATS:
            mean_s = _fmt(ev["mean"], unit)
            std_s = _fmt(ev["stddev"], unit) if ev["stddev"] else f"0 {unit}"
            sentence = (
                f"{meta['label']} is at {value_s}, which is {abs(ev['z']):.1f}σ "
                f"{'above' if ev['z'] >= 0 else 'below'} this instance's own baseline "
                f"of {mean_s} (σ={std_s}) — {ev['severity']}."
            )
        else:
            threshold = meta["hard_crit"] if ev["severity"] == "critical" else meta["hard_warn"]
            threshold_s = f"{threshold}{unit}" if threshold is not None else "the safe range"
            sentence = (
                f"{meta['label']} is at {value_s}, above the {threshold_s} "
                f"{ev['severity']} threshold (not enough history yet for a personal baseline)."
            )
        alerts.append({
            "metric": metric_id,
            "label": meta["label"],
            "severity": ev["severity"],
            "value": ev["value"],
            "z": ev.get("z"),
            "message": sentence,
            "suggested_action": meta["action"],
        })
    alerts.sort(key=lambda a: SEVERITY_RANK[a["severity"]], reverse=True)
    return alerts


# --- Requirement 4: maintenance window + storage runway --------------------

def recommend_maintenance_window(samples):
    if len(samples) < 48:
        return {"available": False, "reason": "Fewer than 48 samples collected — need more history to find a reliable low-traffic window."}

    hourly = {h: [] for h in range(24)}
    for s in samples:
        m = s.get("metrics", {})
        conn, qps = m.get("connections"), m.get("qps")
        if conn is None and qps is None:
            continue
        hour = datetime.fromtimestamp(s["ts"], tz=timezone.utc).hour
        hourly[hour].append((conn or 0, qps or 0))

    hours_with_data = [h for h, v in hourly.items() if v]
    if len(hours_with_data) < 12:
        return {"available": False, "reason": f"Only {len(hours_with_data)} distinct hours of day observed so far — need broader coverage across a full day."}

    max_conn = max((c for h in hourly.values() for c, _ in h), default=0) or 1
    max_qps = max((q for h in hourly.values() for _, q in h), default=0) or 1

    hourly_load = {}
    for h in hours_with_data:
        conns, qpss = zip(*hourly[h])
        hourly_load[h] = statistics.mean(conns) / max_conn + statistics.mean(qpss) / max_qps

    best_start, best_load = None, None
    for start in range(24):
        window_hours = [(start + i) % 24 for i in range(2)]
        if not all(h in hourly_load for h in window_hours):
            continue
        avg = statistics.mean(hourly_load[h] for h in window_hours)
        if best_load is None or avg < best_load:
            best_load, best_start = avg, start

    if best_start is None:
        return {"available": False, "reason": "Not enough hour-of-day coverage yet to pick a 2-hour window."}

    end = (best_start + 2) % 24
    return {
        "available": True,
        "window_utc": f"{best_start:02d}:00-{end:02d}:00 UTC",
        "basis": f"Lowest combined connection+query load across {len(hours_with_data)} observed hours-of-day.",
    }


def _linear_regression(xs, ys):
    n = len(xs)
    mean_x, mean_y = statistics.mean(xs), statistics.mean(ys)
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom < 1e-9:
        return 0.0, mean_y
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    intercept = mean_y - slope * mean_x
    return slope, intercept


def forecast_storage_runway(samples, now_ts):
    points = [(s["ts"], s["metrics"]["storage"]) for s in samples
              if isinstance(s.get("metrics", {}).get("storage"), (int, float))]
    if len(points) < 5:
        return {"available": False, "reason": "Fewer than 5 storage readings collected yet."}

    span_days = (points[-1][0] - points[0][0]) / 86400
    if span_days < 0.5:
        return {"available": False, "reason": "Less than 12 hours of storage history — trend would be noise."}

    xs = [(ts - points[0][0]) / 86400 for ts, _ in points]
    ys = [pct for _, pct in points]
    slope_per_day, intercept = _linear_regression(xs, ys)
    current_pct = ys[-1]

    if slope_per_day <= 0.001:
        return {"available": True, "trend": "flat_or_decreasing", "days_until_90pct": None,
                "message": "Storage usage is flat or decreasing — no runway concern."}

    days_until_90 = (90.0 - current_pct) / slope_per_day
    if days_until_90 < 0:
        days_until_90 = 0.0

    target_date = (datetime.fromtimestamp(now_ts, tz=timezone.utc) + timedelta(days=days_until_90)).date().isoformat()
    result = {
        "available": True,
        "trend": "growing",
        "slope_pct_per_day": round(slope_per_day, 4),
        "days_until_90pct": round(days_until_90, 1),
        "projected_90pct_date": target_date,
    }
    if days_until_90 < 60:
        result["action"] = f"Storage is projected to hit ~90% by {target_date}. Schedule the storage increase before then."
    return result


# --- Requirement 5: long-term right-sizing + cost ---------------------------

def _percentile(values, pct):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def classify_dimension(mean, p95):
    if p95 is None or mean is None:
        return "unknown"
    if p95 < 40 and mean < 20:
        return "over-provisioned"
    if p95 > 85 or mean > 70:
        return "under-provisioned"
    return "well-sized"


def confidence_from_span(samples):
    if len(samples) < 2:
        return "low", 0.0
    span_days = (samples[-1]["ts"] - samples[0]["ts"]) / 86400
    if span_days < 3:
        return "low", span_days
    if span_days < 14:
        return "medium", span_days
    return "high", span_days


def long_term_rightsizing(samples):
    confidence, span_days = confidence_from_span(samples)
    dims = {}
    for metric_id in ("cpu", "memory", "storage"):
        values = [s["metrics"].get(metric_id) for s in samples if isinstance(s.get("metrics", {}).get(metric_id), (int, float))]
        mean = statistics.mean(values) if values else None
        p95 = _percentile(values, 95) if values else None
        dims[metric_id] = {
            "mean": round(mean, 1) if mean is not None else None,
            "p95": round(p95, 1) if p95 is not None else None,
            "classification": classify_dimension(mean, p95),
            "n": len(values),
        }
    return {"confidence": confidence, "days_of_history": round(span_days, 1), "dimensions": dims}


def resolve_tier_spec_fallback(cfg):
    return {
        "source": "manual_fallback",
        "vcpus": cfg.instance_vcpus,
        "memory_gb": cfg.instance_memory_gb,
        "storage_gb": cfg.instance_storage_gb,
        "ha": cfg.instance_ha,
    }


def estimate_monthly_cost(spec, cost_cfg):
    compute = (spec["vcpus"] * cost_cfg["vcpu_hour_usd"] + spec["memory_gb"] * cost_cfg["memory_gb_hour_usd"]) * cost_cfg["hours_per_month"]
    storage = spec["storage_gb"] * cost_cfg["storage_gb_month_usd"]
    multiplier = cost_cfg["ha_multiplier"] if spec.get("ha") else 1.0
    total = (compute + storage) * multiplier
    return {
        "compute_usd": round(compute * multiplier, 2),
        "storage_usd": round(storage * multiplier, 2),
        "total_usd": round(total, 2),
        "ha_applied": bool(spec.get("ha")),
    }


def _is_nonprod(instance_name):
    lowered = instance_name.lower()
    return any(tag in lowered for tag in ("qa", "dev", "test", "staging"))


def build_recommendations(instance_name, spec, rightsizing, cost, cost_cfg):
    """Requirement 5: concrete recommendations with estimated $ impact."""
    recs = []
    nonprod = _is_nonprod(instance_name)
    dims = rightsizing["dimensions"]

    if dims["cpu"]["classification"] == "over-provisioned":
        savings = round(cost["compute_usd"] * 0.30, 2)
        recs.append({
            "title": "CPU is over-provisioned",
            "detail": f"p95 CPU is {dims['cpu']['p95']}% and mean is {dims['cpu']['mean']}% over {rightsizing['days_of_history']} days — a smaller machine type would likely still have headroom.",
            "estimated_monthly_savings_usd": savings,
        })
    if dims["memory"]["classification"] == "over-provisioned":
        savings = round(cost["compute_usd"] * 0.20, 2)
        recs.append({
            "title": "Memory is over-provisioned",
            "detail": f"p95 memory is {dims['memory']['p95']}% and mean is {dims['memory']['mean']}% over {rightsizing['days_of_history']} days — consider a lower-memory tier.",
            "estimated_monthly_savings_usd": savings,
        })
    if dims["storage"]["classification"] == "over-provisioned":
        recs.append({
            "title": "Storage is over-provisioned",
            "detail": f"Storage utilization has stayed low (p95 {dims['storage']['p95']}%). Cloud SQL storage can't be shrunk, so avoid further pre-emptive increases.",
            "estimated_monthly_savings_usd": 0.0,
        })
    if spec.get("ha") and nonprod:
        ha_extra = round(cost["total_usd"] * (1 - 1 / cost_cfg["ha_multiplier"]), 2)
        recs.append({
            "title": "Regional HA enabled on a non-production instance",
            "detail": f"'{instance_name}' looks non-production (name contains qa/dev/test/staging) but has regional (HA) availability enabled.",
            "estimated_monthly_savings_usd": ha_extra,
        })
    if nonprod:
        uptime_fraction = cost_cfg["nonprod_offhours_uptime_fraction"]
        savings = round(cost["compute_usd"] * (1 - uptime_fraction), 2)
        recs.append({
            "title": "Consider off-hours stop/start for this non-production instance",
            "detail": f"'{instance_name}' looks non-production. Running it only during business hours (~{int(uptime_fraction*100)}% uptime) instead of 24/7 would cut compute cost accordingly.",
            "estimated_monthly_savings_usd": savings,
        })

    recs.sort(key=lambda r: r["estimated_monthly_savings_usd"], reverse=True)
    return recs
