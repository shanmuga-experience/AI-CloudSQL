"""Env-file loading and metric/metadata definitions shared by collector.py."""
import os

ENV_PATH = os.path.join(os.path.dirname(__file__), "..", ".env")


def load_env(path=ENV_PATH):
    """Minimal .env parser — avoids a python-dotenv dependency. Existing
    process env vars always win over the file."""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def safe_str(exc):
    """str(exc) can be '' for some exception types — never let a fallback
    path itself throw an IndexError on an empty string."""
    s = str(exc)
    return s if s else repr(exc)


class Config:
    def __init__(self):
        load_env()
        g = os.environ.get

        self.gcp_project_id = g("GCP_PROJECT_ID", "").strip()
        self.cloudsql_instance_id = g("CLOUDSQL_INSTANCE_ID", "").strip()

        self.mysql_host = g("MYSQL_HOST", "127.0.0.1")
        self.mysql_port = int(g("MYSQL_PORT", "3306") or 3306)
        self.mysql_user = g("MYSQL_USER", "")
        self.mysql_password = g("MYSQL_PASSWORD", "")
        self.mysql_database = g("MYSQL_DATABASE", "information_schema")

        self.instance_vcpus = float(g("INSTANCE_VCPUS", "2") or 2)
        self.instance_memory_gb = float(g("INSTANCE_MEMORY_GB", "8") or 8)
        self.instance_storage_gb = float(g("INSTANCE_STORAGE_GB", "100") or 100)
        self.instance_ha = g("INSTANCE_HA", "false").strip().lower() in ("1", "true", "yes")

        self.anthropic_api_key = g("ANTHROPIC_API_KEY", "").strip()
        self.anthropic_model = g("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001").strip()
        self.ai_cache_ttl_seconds = int(g("AI_CACHE_TTL_SECONDS", "300") or 300)

        self.poll_interval_seconds = int(g("POLL_INTERVAL_SECONDS", "60") or 60)
        self.http_port = int(g("HTTP_PORT", "8000") or 8000)

        self.instance_name = self.cloudsql_instance_id or "unnamed-instance"


# --- Metric metadata --------------------------------------------------------
# weight: contribution to the composite health score
# hard_warn/hard_crit: fixed-ceiling floor for cold start, in the metric's
# own unit; None means "rely on statistical baseline only" for that metric.
METRICS = {
    "cpu": {
        "label": "CPU Usage", "unit": "%", "category": "percent",
        "weight": 1.2, "hard_warn": 75, "hard_crit": 90,
        "action": "Check SHOW PROCESSLIST / performance_schema for expensive "
                  "queries; optimize or add indexes, or move to a larger machine type.",
    },
    "memory": {
        "label": "Memory Usage", "unit": "%", "category": "percent",
        "weight": 1.2, "hard_warn": 80, "hard_crit": 92,
        "action": "Look for memory-heavy queries or connection counts; tune "
                  "innodb_buffer_pool_size, or upgrade to a machine type with more RAM.",
    },
    "connections": {
        "label": "Total Connections", "unit": "conn", "category": "count",
        "weight": 1.0, "hard_warn": None, "hard_crit": None,
        "action": "Check application connection pools for leaks; add a "
                  "pooler (e.g. ProxySQL) or raise max_connections.",
    },
    "storage": {
        "label": "Storage Usage", "unit": "%", "category": "percent",
        "weight": 1.3, "hard_warn": 80, "hard_crit": 92,
        "action": "Purge old data/logs/binlogs, or schedule a storage "
                  "increase (see Maintenance panel for timing).",
    },
    "qps": {
        "label": "MySQL Queries (QPS)", "unit": "q/s", "category": "rate",
        "weight": 0.8, "hard_warn": None, "hard_crit": None,
        "action": "Review the slow query log for missing indexes; consider "
                  "a read replica to offload read traffic.",
    },
    "disk_io": {
        "label": "Disk Read/Write Ops", "unit": "ops/s", "category": "rate",
        "weight": 0.7, "hard_warn": None, "hard_crit": None,
        "action": "Look for full table scans or missing indexes driving IO; "
                  "consider a faster disk type or higher provisioned IOPS.",
    },
}

# Storage capacity is already covered by "storage" (% of provisioned disk
# used) — a separate absolute-GB "disk usage" widget would just be the same
# signal in different units, so it isn't tracked as its own metric.
METRIC_ORDER = ["cpu", "memory", "connections", "storage", "qps", "disk_io"]
