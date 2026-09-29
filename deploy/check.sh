#!/usr/bin/env bash
# Reads the dashboard's status.json and says, per data source, whether it is
# really working. Use this instead of the health score: the score shows
# "100 Healthy" even when GCP and MySQL are both failing.
#
#   bash deploy/check.sh                  # checks /opt/dbdash/app/src/status.json
#   bash deploy/check.sh path/to/status.json
# Exit code: 0 if GCP Monitoring and MySQL are both OK, 1 otherwise.
set -uo pipefail
STATUS="${1:-/opt/dbdash/app/src/status.json}"

if [[ ! -f "$STATUS" ]]; then
  echo "    FAIL  $STATUS does not exist yet (collector has not completed a cycle)"
  exit 1
fi

python3 - "$STATUS" <<'PY'
import json, sys, time
from datetime import datetime

s = json.load(open(sys.argv[1]))
age = time.time() - datetime.fromisoformat(s["generated_at"]).timestamp()
print(f"    mode: {s['mode']}   health: {s['health_score']} {s['health_label']}   "
      f"instance: {s['instance_name']}   updated {age:.0f}s ago")

bad = False
for name, src in s["data_sources"].items():
    if src.get("available"):
        print(f"    OK    {name}")
    else:
        optional = name == "ai_analysis"
        bad |= not optional
        print(f"    {'SKIP' if optional else 'FAIL'}  {name}: {src.get('reason')}")

spec = s.get("provisioned_spec", {})
print(f"    {'OK   ' if spec.get('source') == 'cloudsql_admin_api' else 'WARN '} spec source: "
      f"{spec.get('source')} ({spec.get('vcpus')} vCPU, {spec.get('memory_gb')} GB RAM, "
      f"{spec.get('storage_gb')} GB disk){' - ' + spec['reason'] if spec.get('reason') else ''}")
if s["mode"] != "live":
    print("    FAIL  collector is in mock mode")
    bad = True
sys.exit(1 if bad else 0)
PY
