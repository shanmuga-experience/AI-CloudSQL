#!/usr/bin/env bash
# Optional: creates (or resets the password of) the read-only MySQL user named
# in .env, through the Cloud SQL Auth Proxy that install.sh started.
# Skip this if you already have a working monitoring user from your local setup.
#
#   sudo bash deploy/create_mysql_user.sh            # asks for an admin MySQL user + password
#
# The user gets NO grants: SHOW GLOBAL STATUS / SHOW GLOBAL VARIABLES need none,
# so it can't read any table data.
set -euo pipefail
ENV_FILE=/opt/dbdash/app/.env
[[ $EUID -eq 0 ]] || { echo "Run with sudo"; exit 1; }
[[ -f $ENV_FILE ]] || { echo "$ENV_FILE not found - run deploy/install.sh first"; exit 1; }

get() { grep -E "^$1=" "$ENV_FILE" | tail -n1 | cut -d= -f2-; }
USER_NAME="$(get MYSQL_USER)"; USER_PASS="$(get MYSQL_PASSWORD)"; PORT="$(get MYSQL_PORT)"; PORT="${PORT:-3306}"
[[ "$USER_NAME$USER_PASS" != *"'"* ]] || { echo "MYSQL_USER / MYSQL_PASSWORD must not contain a single quote"; exit 1; }

read -r -p "Existing MySQL admin user [root]: " ADMIN; ADMIN="${ADMIN:-root}"
# Password prompt comes from the mysql client; the SQL goes over stdin, not the command line.
mysql -h 127.0.0.1 -P "$PORT" -u "$ADMIN" -p --table <<SQL
CREATE USER IF NOT EXISTS '$USER_NAME'@'%' IDENTIFIED BY '$USER_PASS';
ALTER USER '$USER_NAME'@'%' IDENTIFIED BY '$USER_PASS';
SHOW GRANTS FOR '$USER_NAME'@'%';
SQL
echo "Done. Now: sudo systemctl restart dbdash && sleep 5 && bash /opt/dbdash/app/deploy/check.sh"
