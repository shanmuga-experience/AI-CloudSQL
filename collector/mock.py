"""Synthetic-but-plausible data so the dashboard is fully demoable with zero
cloud/database access: a daily usage curve plus noise, and an injectable
anomaly for the alerting-state demo."""
import math
import random


def _daily_factor(ts):
    """0..1 business-hours curve peaking around 14:00 UTC, trough overnight."""
    hour = (ts / 3600) % 24
    return 0.5 + 0.5 * math.sin((hour - 8) / 24 * 2 * math.pi)


def generate_mock_sample(ts, anomaly_metrics=None):
    """anomaly_metrics: metric id, or a list of ids to spike simultaneously
    (useful for demoing several concurrent alerts)."""
    f = _daily_factor(ts)
    values = {
        "cpu": 20 + 35 * f + random.gauss(0, 3),
        "memory": 40 + 20 * f + random.gauss(0, 2),
        "connections": 8 + 40 * f + random.gauss(0, 3),
        "storage": 55 + 0.01 * (ts % 100000) / 1000 + random.gauss(0, 0.3),
        "qps": 15 + 200 * f + random.gauss(0, 10),
        "disk_io": 30 + 150 * f + random.gauss(0, 15),
    }
    for k in values:
        values[k] = max(0.0, values[k])

    if isinstance(anomaly_metrics, str):
        anomaly_metrics = [anomaly_metrics]
    spikes = {
        "cpu": 74, "memory": 88, "connections": 190, "storage": 91,
        "qps": 900, "disk_io": 650,
    }
    for metric_id in (anomaly_metrics or []):
        if metric_id in values:
            values[metric_id] = spikes.get(metric_id, values[metric_id] * 4)

    return {k: round(v, 2) for k, v in values.items()}
