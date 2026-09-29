#!/usr/bin/env bash
# One-shot installer for the Cloud SQL AI Advisor on an Ubuntu 22.04/24.04 GCP VM.
#
#   1. upload this whole folder to the VM (e.g. ~/cloudsql-ai-advisor)
#   2. cp deploy/env.vm.template .env && nano .env       (DB user/password, project, instance)
#   3. nano deploy/deploy.conf                            (connection name, login, optional domain)
#   4. sudo bash deploy/install.sh
#
# Safe to re-run: it updates code and config in place and keeps collected history.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(dirname "$HERE")"
APP_USER=dbdash
APP_HOME=/opt/dbdash
APP_DIR=$APP_HOME/app
VENV=$APP_HOME/venv

step() { printf '\n==> %s\n' "$*"; }
ok()   { printf '    OK    %s\n' "$*"; }
warn() { printf '    WARN  %s\n' "$*"; }
die()  { printf '\n    ERROR %s\n\n' "$*" >&2; exit 1; }
envval() {  # value of KEY in a .env file (last one wins), default $3
  local v; v="$(grep -E "^$1=" "$2" 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
  v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  echo "${v:-$3}"
}
wait_port() {  # wait_port PORT SECONDS
  local i; for ((i = 0; i < $2; i++)); do
    ss -ltn "sport = :$1" | grep -q LISTEN && return 0; sleep 1
  done; return 1
}

# ---------------------------------------------------------------- preflight
[[ $EUID -eq 0 ]] || die "Run with sudo:  sudo bash deploy/install.sh"
[[ -f "$HERE/deploy.conf" ]] || die "deploy/deploy.conf is missing"
# shellcheck source=/dev/null
source "$HERE/deploy.conf"

for f in collector/collector.py collector/requirements.txt collector/cost_config.json src/index.html; do
  [[ -f "$SRC/$f" ]] || die "$SRC/$f not found - upload these 3 folders into the same folder: collector/ src/ deploy/"
done
[[ -f "$SRC/.env" ]] || die "No .env yet. Create it:  cp deploy/env.vm.template .env && nano .env"
[[ "$CLOUDSQL_CONNECTION_NAME" =~ ^[^:]+:[^:]+:[^:]+$ && "$CLOUDSQL_CONNECTION_NAME" != your-* ]] \
  || die "Set CLOUDSQL_CONNECTION_NAME in deploy/deploy.conf (PROJECT:REGION:INSTANCE)"
if grep -nE '^[A-Za-z_]+=[^#]*[[:space:]]#' "$SRC/.env"; then
  die ".env has a comment at the end of a value line (shown above). config.py would read the comment as part of the value - move it to its own line."
fi
for k in GCP_PROJECT_ID CLOUDSQL_INSTANCE_ID MYSQL_USER MYSQL_PASSWORD; do
  v="$(envval "$k" "$SRC/.env" "")"
  [[ -n "$v" && "$v" != your-* && "$v" != changeme ]] || die ".env: set a real value for $k"
done
MYSQL_PORT="$(envval MYSQL_PORT "$SRC/.env" 3306)"
APP_PORT="$(envval HTTP_PORT "$SRC/.env" 8000)"

# ---------------------------------------------------------------- 1. packages
step "1/8  Installing OS packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv nginx apache2-utils curl default-mysql-client >/dev/null
ok "$(python3 --version), $(nginx -v 2>&1 | cut -d' ' -f3)"

# ---------------------------------------------------------------- 2. user + files
step "2/8  Service user '$APP_USER' and app directory $APP_DIR"
id "$APP_USER" &>/dev/null || useradd --system --create-home --home-dir "$APP_HOME" --shell /usr/sbin/nologin "$APP_USER"
chmod 755 "$APP_HOME"
mkdir -p "$APP_DIR"
if [[ "$(realpath "$SRC")" != "$(realpath "$APP_DIR")" ]]; then
  cp -R "$SRC/collector" "$SRC/src" "$SRC/deploy" "$APP_DIR/"   # merges; keeps collector/data history
  cp "$SRC/.env" "$APP_DIR/"
  for f in .env.template .gitignore; do [[ -f "$SRC/$f" ]] && cp "$SRC/$f" "$APP_DIR/"; done  # optional extras
fi
# A key-file path that does not exist breaks every GCP call; the VM's service account is used instead.
gac="$(envval GOOGLE_APPLICATION_CREDENTIALS "$APP_DIR/.env" "")"
if [[ -n "$gac" && ! -f "$gac" ]]; then
  sed -i 's|^GOOGLE_APPLICATION_CREDENTIALS=|# disabled by install.sh (file not found, using VM service account): GOOGLE_APPLICATION_CREDENTIALS=|' "$APP_DIR/.env"
  warn "GOOGLE_APPLICATION_CREDENTIALS pointed at a missing file ($gac) - commented it out"
fi
chown -R "$APP_USER:$APP_USER" "$APP_HOME"
chmod 600 "$APP_DIR/.env"
ok "code in $APP_DIR, .env is 600 $APP_USER:$APP_USER"

# ---------------------------------------------------------------- 3. python
step "3/8  Python virtualenv + collector/requirements.txt"
[[ -x "$VENV/bin/python" ]] || sudo -u "$APP_USER" python3 -m venv "$VENV"
sudo -u "$APP_USER" "$VENV/bin/pip" install -q --disable-pip-version-check -r "$APP_DIR/collector/requirements.txt"
ok "$(sudo -u "$APP_USER" "$VENV/bin/pip" list 2>/dev/null | grep -Ei '^(pymysql|google-cloud-monitoring|google-auth) ' | awk '{printf "%s %s  ", $1, $2}')"

# ---------------------------------------------------------------- 4. proxy
step "4/8  Cloud SQL Auth Proxy $PROXY_VERSION -> 127.0.0.1:$MYSQL_PORT"
if ! /usr/local/bin/cloud-sql-proxy --version 2>/dev/null | grep -q "${PROXY_VERSION#v}"; then
  curl -fsSL -o /usr/local/bin/cloud-sql-proxy \
    "https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/${PROXY_VERSION}/cloud-sql-proxy.linux.amd64" \
    || die "download failed - check PROXY_VERSION in deploy.conf"
  chmod 755 /usr/local/bin/cloud-sql-proxy
fi
PROXY_ARGS="--address 127.0.0.1 --port $MYSQL_PORT $CLOUDSQL_CONNECTION_NAME"
[[ "$USE_PRIVATE_IP" == "true" ]] && PROXY_ARGS="--private-ip $PROXY_ARGS"
sed "s|__PROXY_ARGS__|$PROXY_ARGS|" "$HERE/cloud-sql-proxy.service" > /etc/systemd/system/cloud-sql-proxy.service
systemctl daemon-reload
systemctl enable -q cloud-sql-proxy
systemctl restart cloud-sql-proxy
if wait_port "$MYSQL_PORT" 30 && journalctl -u cloud-sql-proxy --since "-1min" -q | grep -q "ready for new connections"; then
  ok "$(/usr/local/bin/cloud-sql-proxy --version)  listening on 127.0.0.1:$MYSQL_PORT"
else
  journalctl -u cloud-sql-proxy -n 15 --no-pager
  die "proxy did not start (log above). Usual causes: wrong connection name, USE_PRIVATE_IP=true but VM not in the instance's VPC, or the VM service account lacks roles/cloudsql.client."
fi

# ---------------------------------------------------------------- 5. test cycle
step "5/8  One live collection cycle (before starting the service)"
systemctl stop dbdash 2>/dev/null || true
(cd "$APP_DIR" && sudo -u "$APP_USER" "$VENV/bin/python" collector/collector.py --once 2>&1 | sed 's/^/    /') \
  || die "collector.py crashed during the test cycle (output above)"
if bash "$HERE/check.sh" "$APP_DIR/src/status.json"; then
  ok "GCP Monitoring and MySQL are both live"
else
  warn "not all data sources are working yet (see FAIL lines). The install continues; fix and re-check with: bash deploy/check.sh"
fi

# ---------------------------------------------------------------- 6. app service
step "6/8  dbdash.service (collector + dashboard on 127.0.0.1:$APP_PORT)"
cp "$HERE/dbdash.service" /etc/systemd/system/dbdash.service
systemctl daemon-reload
systemctl enable -q dbdash
systemctl restart dbdash
wait_port "$APP_PORT" 20 || { journalctl -u dbdash -n 20 --no-pager; die "dbdash did not start listening on $APP_PORT"; }
ok "$(systemctl is-active dbdash), $(curl -s -o /dev/null -w 'HTTP %{http_code}' "http://127.0.0.1:$APP_PORT/")"

# ---------------------------------------------------------------- 7. nginx
step "7/8  nginx on :80 with a login for '$DASHBOARD_USER'"
if [[ ! -s /etc/nginx/.htpasswd-dbdash ]] || ! grep -q "^$DASHBOARD_USER:" /etc/nginx/.htpasswd-dbdash; then
  echo "    Choose the dashboard password for '$DASHBOARD_USER':"
  if [[ -s /etc/nginx/.htpasswd-dbdash ]]; then htpasswd /etc/nginx/.htpasswd-dbdash "$DASHBOARD_USER"
  else htpasswd -c /etc/nginx/.htpasswd-dbdash "$DASHBOARD_USER"; fi
fi
chown root:www-data /etc/nginx/.htpasswd-dbdash && chmod 640 /etc/nginx/.htpasswd-dbdash
sed -e "s|__SERVER_NAME__|${DOMAIN:-_}|" -e "s|__APP_PORT__|$APP_PORT|" "$HERE/nginx-dbdash.conf" > /etc/nginx/sites-available/dbdash
ln -sf /etc/nginx/sites-available/dbdash /etc/nginx/sites-enabled/dbdash
rm -f /etc/nginx/sites-enabled/default
nginx -t -q || die "nginx config test failed"
systemctl enable -q nginx
systemctl reload nginx || systemctl restart nginx
ok "/healthz -> $(curl -s http://127.0.0.1/healthz | tr -d '\n'),  / without login -> $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/)"

# ---------------------------------------------------------------- 8. https
step "8/8  HTTPS"
SCHEME=http
if [[ -n "$DOMAIN" && -n "$LETSENCRYPT_EMAIL" ]]; then
  apt-get install -y -qq certbot python3-certbot-nginx >/dev/null
  certbot --nginx -d "$DOMAIN" --redirect -m "$LETSENCRYPT_EMAIL" --agree-tos -n \
    && SCHEME=https && ok "certificate installed for $DOMAIN (auto-renews)" \
    || warn "certbot failed - is $DOMAIN's A record pointing at this VM, and is port 80 open?"
else
  warn "skipped (DOMAIN / LETSENCRYPT_EMAIL empty in deploy.conf). The password is sent unencrypted over plain http."
fi

EXT_IP="$(curl -s -m 3 -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip || true)"
HOST="${DOMAIN:-${EXT_IP:-<VM external IP>}}"
cat <<EOF

==> Done.
    Dashboard : $SCHEME://$HOST/        (login: $DASHBOARD_USER)
    Liveness  : $SCHEME://$HOST/healthz
    Logs      : sudo journalctl -u dbdash -f
    Check     : bash $APP_DIR/deploy/check.sh
EOF
