"""Rolling on-disk history of samples, capped at 30 days, used for baselines,
right-sizing, maintenance-window mining, and storage-runway regression."""
import json
import os
import statistics

RETENTION_SECONDS = 30 * 24 * 3600

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
COUNTERS_PATH = os.path.join(DATA_DIR, "mysql_counters.json")


def _ensure_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def append_sample(sample):
    """sample: {"ts": epoch_seconds, "metrics": {metric_id: value_or_None}}"""
    _ensure_dir()
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(sample) + "\n")


def load_history(now_ts):
    """Return all samples within the retention window, oldest first. Corrupt
    lines are skipped rather than aborting the whole read."""
    if not os.path.isfile(HISTORY_PATH):
        return []
    cutoff = now_ts - RETENTION_SECONDS
    samples = []
    with open(HISTORY_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(s, dict) and s.get("ts", 0) >= cutoff:
                samples.append(s)
    # Analysis code assumes chronological order for span/regression math;
    # don't trust raw file/append order (e.g. clock changes, manual edits).
    samples.sort(key=lambda s: s.get("ts", 0))
    return samples


def prune_history(now_ts):
    """Rewrite the history file keeping only samples inside the retention
    window, so it doesn't grow unbounded over long-running deployments."""
    samples = load_history(now_ts)
    _ensure_dir()
    tmp_path = HISTORY_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        for s in samples:
            f.write(json.dumps(s) + "\n")
    os.replace(tmp_path, HISTORY_PATH)
    return samples


def baseline_for(samples, metric_id, exclude_last=True):
    """Mean/stddev of a metric's historical values, ignoring None (failed
    readings) and the current sample itself if it's already appended."""
    values = []
    use = samples[:-1] if (exclude_last and samples) else samples
    for s in use:
        v = s.get("metrics", {}).get(metric_id)
        if isinstance(v, (int, float)):
            values.append(v)
    if len(values) < 2:
        return {"mean": None, "stddev": None, "n": len(values)}
    mean = statistics.mean(values)
    stddev = statistics.pstdev(values)
    return {"mean": mean, "stddev": stddev, "n": len(values)}


def load_mysql_counters():
    if not os.path.isfile(COUNTERS_PATH):
        return None
    try:
        with open(COUNTERS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError, OSError):
        return None


def save_mysql_counters(counters):
    _ensure_dir()
    with open(COUNTERS_PATH, "w", encoding="utf-8") as f:
        json.dump(counters, f)
