"""Independently-failing data sources: GCP Cloud Monitoring, direct MySQL,
and the Cloud SQL Admin API for provisioned spec. Every function here must
be safe to call with missing deps, missing creds, or a dead network — a
failure degrades only its own metrics and always returns a plain-English
reason, never raises out of this module."""
import time

from config import safe_str
from analysis import resolve_tier_spec_fallback
from history import load_mysql_counters, save_mysql_counters


def collect_gcp_metrics(cfg):
    """Returns (values: dict[str, float], status: {available, reason})."""
    if not cfg.gcp_project_id or not cfg.cloudsql_instance_id:
        return {}, {"available": False, "reason": "GCP_PROJECT_ID / CLOUDSQL_INSTANCE_ID not configured — CPU, memory, storage and disk-IO metrics from GCP are unavailable."}

    try:
        from google.cloud import monitoring_v3
    except ImportError:
        return {}, {"available": False, "reason": "google-cloud-monitoring is not installed (pip install -r collector/requirements.txt) — GCP metrics unavailable."}

    try:
        client = monitoring_v3.MetricServiceClient()
        project_name = f"projects/{cfg.gcp_project_id}"
        now = int(time.time())
        interval = monitoring_v3.TimeInterval(
            end_time={"seconds": now},
            start_time={"seconds": now - 600},
        )
        database_id = f"{cfg.gcp_project_id}:{cfg.cloudsql_instance_id}"
        metric_types = {
            "cpu": "cloudsql.googleapis.com/database/cpu/utilization",
            "memory": "cloudsql.googleapis.com/database/memory/utilization",
            "storage": "cloudsql.googleapis.com/database/disk/utilization",
            "disk_read_ops": "cloudsql.googleapis.com/database/disk/read_ops_count",
            "disk_write_ops": "cloudsql.googleapis.com/database/disk/write_ops_count",
        }

        values = {}
        for key, metric_type in metric_types.items():
            flt = (
                f'metric.type = "{metric_type}" AND '
                f'resource.labels.database_id = "{database_id}"'
            )
            results = client.list_time_series(
                request={
                    "name": project_name,
                    "filter": flt,
                    "interval": interval,
                    "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                }
            )
            latest_point = None
            for series in results:
                if series.points:
                    latest_point = series.points[0]
                    break
            if latest_point is None:
                continue
            v = latest_point.value
            raw = v.double_value if v.double_value else float(v.int64_value)
            if key in ("disk_read_ops", "disk_write_ops"):
                # read_ops_count / write_ops_count are DELTA metrics: each point is the
                # number of ops during its sample window (60s on Cloud SQL), not a rate.
                # Divide by the window length so the dashboard's "ops/s" matches the
                # per-second Read/write operations chart in the GCP console.
                try:
                    window = (latest_point.interval.end_time - latest_point.interval.start_time).total_seconds()
                except Exception:  # noqa: BLE001 - malformed interval: fall back to Cloud SQL's 60s sampling
                    window = 0
                raw = raw / (window if window > 0 else 60)
            values[key] = raw

        out = {}
        if "cpu" in values:
            out["cpu"] = values["cpu"] * 100
        if "memory" in values:
            out["memory"] = values["memory"] * 100
        if "storage" in values:
            out["storage"] = values["storage"] * 100
        if "disk_read_ops" in values or "disk_write_ops" in values:
            out["disk_io"] = values.get("disk_read_ops", 0) + values.get("disk_write_ops", 0)

        if not out:
            return {}, {"available": False, "reason": "GCP Cloud Monitoring returned no data points for this instance yet (metric_type/database_id filter may not match, or the instance is too new)."}
        return out, {"available": True, "reason": None}
    except Exception as e:  # noqa: BLE001 - must never crash the collector
        return {}, {"available": False, "reason": f"GCP Cloud Monitoring auth/query failed: {safe_str(e)}"}


def collect_mysql_metrics(cfg):
    """Returns (values: dict, status: dict). QPS is computed as a delta
    between polls, so the first successful poll after a restart has no QPS
    yet (that's correctly represented as unavailable, not zero)."""
    try:
        import pymysql
    except ImportError:
        return {}, {"available": False, "reason": "pymysql is not installed (pip install -r collector/requirements.txt) — direct MySQL metrics unavailable."}

    conn = None
    try:
        conn = pymysql.connect(
            host=cfg.mysql_host, port=cfg.mysql_port,
            user=cfg.mysql_user, password=cfg.mysql_password,
            database=cfg.mysql_database, connect_timeout=5,
        )
        with conn.cursor() as cur:
            cur.execute("SHOW GLOBAL STATUS LIKE 'Threads_connected'")
            row = cur.fetchone()
            connections = float(row[1]) if row else None

            cur.execute("SHOW GLOBAL STATUS LIKE 'Queries'")
            row = cur.fetchone()
            queries_total = float(row[1]) if row else None

            cur.execute("SHOW GLOBAL VARIABLES LIKE 'max_connections'")
            row = cur.fetchone()
            max_connections = float(row[1]) if row else None

        values = {}
        if connections is not None:
            values["connections"] = connections

        now = time.time()
        prev = load_mysql_counters()
        if queries_total is not None:
            if prev and prev.get("ts") and now > prev["ts"]:
                dt = now - prev["ts"]
                dq = queries_total - prev.get("queries_total", queries_total)
                if dt > 0 and dq >= 0:
                    values["qps"] = dq / dt
            save_mysql_counters({"ts": now, "queries_total": queries_total})

        status = {"available": True, "reason": None}
        if max_connections:
            status["max_connections"] = max_connections
        return values, status
    except Exception as e:  # noqa: BLE001 - must never crash the collector
        return {}, {"available": False, "reason": f"Could not connect to MySQL at {cfg.mysql_host}:{cfg.mysql_port}: {safe_str(e)}"}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


_TIER_TABLE = {
    "db-f1-micro": (1, 0.6),
    "db-g1-small": (1, 1.7),
    "db-n1-standard-1": (1, 3.75),
    "db-n1-standard-2": (2, 7.5),
    "db-n1-standard-4": (4, 15),
    "db-n1-standard-8": (8, 30),
    "db-n1-highmem-2": (2, 13),
    "db-n1-highmem-4": (4, 26),
}


def _parse_tier(tier):
    if tier.startswith("db-custom-"):
        parts = tier.split("-")
        try:
            vcpus, mem_mb = int(parts[2]), int(parts[3])
            return vcpus, mem_mb / 1024
        except (IndexError, ValueError):
            return None
    return _TIER_TABLE.get(tier)


def resolve_provisioned_spec(cfg):
    """Requirement 5: try the real Cloud SQL Admin API first, fall back to
    manually-configured env values (never raises)."""
    if not cfg.gcp_project_id or not cfg.cloudsql_instance_id:
        spec = resolve_tier_spec_fallback(cfg)
        spec["reason"] = "GCP_PROJECT_ID / CLOUDSQL_INSTANCE_ID not configured."
        return spec

    try:
        import google.auth
        import google.auth.transport.requests
        import urllib.request
        import json as _json

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/sqlservice.admin"]
        )
        credentials.refresh(google.auth.transport.requests.Request())

        url = (
            f"https://sqladmin.googleapis.com/sql/v1beta4/projects/"
            f"{cfg.gcp_project_id}/instances/{cfg.cloudsql_instance_id}"
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {credentials.token}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode("utf-8"))

        settings = data.get("settings", {})
        tier = settings.get("tier", "")
        parsed = _parse_tier(tier)
        if parsed is None:
            fallback = resolve_tier_spec_fallback(cfg)
            fallback["reason"] = f"Cloud SQL Admin API returned unrecognized tier '{tier}' — using manual vCPU/memory fallback."
            fallback["storage_gb"] = float(settings.get("dataDiskSizeGb", fallback["storage_gb"]))
            fallback["ha"] = settings.get("availabilityType") == "REGIONAL"
            return fallback

        vcpus, memory_gb = parsed
        return {
            "source": "cloudsql_admin_api",
            "vcpus": vcpus,
            "memory_gb": memory_gb,
            "storage_gb": float(settings.get("dataDiskSizeGb", cfg.instance_storage_gb)),
            "ha": settings.get("availabilityType") == "REGIONAL",
            "tier": tier,
        }
    except ImportError:
        spec = resolve_tier_spec_fallback(cfg)
        spec["reason"] = "google-auth is not installed — using manually-configured spec."
        return spec
    except Exception as e:  # noqa: BLE001 - must never crash the collector
        spec = resolve_tier_spec_fallback(cfg)
        spec["reason"] = f"Cloud SQL Admin API unreachable ({safe_str(e)}) — using manually-configured spec."
        return spec
