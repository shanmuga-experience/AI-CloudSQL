"""Requirement 6: AI root-cause analysis, gated behind an optional API key.

Model choice: defaults to Haiku (fast/cheap) because this is a bounded,
low-reasoning task — classifying a handful of structured (metric, value,
baseline, spec) tuples into a root cause, not open-ended reasoning. The
model is overridable via ANTHROPIC_MODEL for anyone who wants a stronger
model. No `thinking` budget is requested: Haiku doesn't spend budget on
extended reasoning by default, and this task doesn't need it.

Uses raw HTTP (urllib) instead of the anthropic SDK to avoid an extra
dependency for an already-optional feature. Requests a tool-forced,
JSON-schema-constrained response so parsing can never partially fail.
"""
import json
import time
import urllib.request
import urllib.error

from config import safe_str

_CACHE = {}  # cache_key -> (cached_at_epoch, result_dict)

_TOOL_SCHEMA = {
    "name": "report_root_cause",
    "description": "Report root-cause diagnosis for each active database alert.",
    "input_schema": {
        "type": "object",
        "properties": {
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string"},
                        "likely_root_cause": {"type": "string"},
                        "immediate_fix": {"type": "string"},
                        "preventive_action": {"type": "string"},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                    "required": ["metric", "likely_root_cause", "immediate_fix", "preventive_action", "confidence"],
                },
            }
        },
        "required": ["issues"],
    },
}


def _cache_key(active_alerts):
    return tuple(sorted((a["metric"], a["severity"]) for a in active_alerts))


def get_ai_root_cause(cfg, active_alerts, provisioned_spec):
    if not cfg.anthropic_api_key:
        return {"available": False, "reason": "ANTHROPIC_API_KEY not set — AI root-cause analysis is an optional feature and is disabled.", "issues": []}
    if not active_alerts:
        return {"available": True, "issues": [], "from_cache": False}

    key = _cache_key(active_alerts)
    now = time.time()
    cached = _CACHE.get(key)
    if cached and (now - cached[0]) < cfg.ai_cache_ttl_seconds:
        result = dict(cached[1])
        result["from_cache"] = True
        return result

    try:
        alert_context = [
            {
                "metric": a["label"],
                "current_value": a["value"],
                "z_score": a.get("z"),
                "severity": a["severity"],
            }
            for a in active_alerts
        ]
        prompt = (
            "You are diagnosing a Cloud SQL MySQL instance. Here are the "
            "currently active alerts (each already compares the live value "
            "to this instance's own statistical baseline) and the "
            "instance's provisioned hardware spec. For EACH alert, give a "
            "SPECIFIC likely root cause grounded in these actual numbers "
            "and this spec — not generic advice you'd give for any "
            "database.\n\n"
            f"Active alerts: {json.dumps(alert_context)}\n\n"
            f"Provisioned spec: {json.dumps(provisioned_spec)}\n\n"
            "Call report_root_cause with one entry per alert."
        )

        body = json.dumps({
            "model": cfg.anthropic_model,
            "max_tokens": 1024,
            "tools": [_TOOL_SCHEMA],
            "tool_choice": {"type": "tool", "name": "report_root_cause"},
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "x-api-key": cfg.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        issues = []
        for block in payload.get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "report_root_cause":
                issues = block.get("input", {}).get("issues", [])
                break

        result = {"available": True, "issues": issues, "model": cfg.anthropic_model, "from_cache": False}
        _CACHE[key] = (now, result)
        return result
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:200]
        except Exception:  # noqa: BLE001
            pass
        return {"available": False, "reason": f"Anthropic API returned an error: {safe_str(e)} {detail}".strip(), "issues": []}
    except Exception as e:  # noqa: BLE001 - must never crash the collector
        return {"available": False, "reason": f"AI root-cause analysis failed: {safe_str(e)}", "issues": []}
