# Cloud SQL AI Advisor: upload-and-run on a GCP VM

## What's in this folder

```
cloudsql-ai-advisor/
├── .env.template            ┐
├── .gitignore               │ your project, unchanged
├── collector/  (9 files)    │ (identical to branch shanmugapriya-veeranakalai/cloudsql-ai-advisor)
├── src/index.html           ┘
├── DEPLOY_STEPS.md          this file
└── deploy/
    ├── gcp_setup.sh          run once in Cloud Shell: APIs, service account, static IP, VM, firewall
    ├── deploy.conf           5 settings for the installer (connection name, login, optional domain)
    ├── env.vm.template       the .env to use on the VM (no key-file line, no inline comments)
    ├── install.sh            run on the VM: installs and starts everything
    ├── cloud-sql-proxy.service   systemd unit: Cloud SQL Auth Proxy on 127.0.0.1:3306
    ├── dbdash.service            systemd unit: collector.py --serve on 127.0.0.1:8000
    ├── nginx-dbdash.conf         public :80 with a password, proxying to 127.0.0.1:8000
    ├── check.sh              says OK/FAIL per data source (don't trust the health score alone)
    └── create_mysql_user.sh  optional: creates the read-only MySQL user
```

The result: `Browser → nginx :80/:443 (password) → dashboard 127.0.0.1:8000 → proxy 127.0.0.1:3306 → Cloud SQL`.

---

## Step 1: Create the VM (Cloud Shell, once)

Open **Cloud Shell** in the GCP console and upload `deploy/gcp_setup.sh`, using the ⋮ menu and **Upload**. Edit the 7 values at the top, then run it:

```bash
nano gcp_setup.sh
bash gcp_setup.sh
```

**Expected output**
```
==> APIs
Operation "operations/acat.p2-..." finished successfully.
==> Service account dbdash-vm@your-gcp-project-id.iam.gserviceaccount.com
Created service account [dbdash-vm].
    granted roles/monitoring.viewer
    granted roles/cloudsql.viewer
    granted roles/cloudsql.client
==> Static IP
Created [https://www.googleapis.com/compute/v1/projects/.../regions/asia-south1/addresses/dbdash-ip].
==> VM dbdash-vm
NAME       ZONE           MACHINE_TYPE  INTERNAL_IP  EXTERNAL_IP  STATUS
dbdash-vm  asia-south1-a  e2-small      10.160.0.5   34.93.12.34  RUNNING
==> Firewall 80/443 from 0.0.0.0/0
NAME              NETWORK  DIRECTION  PRIORITY  ALLOW          DENY  DISABLED
dbdash-allow-web  default  INGRESS    1000      tcp:80,tcp:443       False

==> Cloud SQL connection names in your-gcp-project-id (put yours in deploy/deploy.conf):
NAME           CONNECTION_NAME                               IP_TYPES
nonprod-mysql  your-gcp-project-id:asia-south1:nonprod-mysql  PRIMARY,PRIVATE

VM external IP: 34.93.12.34
```
Copy the **CONNECTION_NAME**. If `IP_TYPES` has no `PRIVATE`, you'll set `USE_PRIVATE_IP="false"` in Step 3.

## Step 2: Upload the folder to the VM

From your laptop, in the folder that **contains** `cloudsql-ai-advisor/`:
```bash
gcloud compute scp --recurse --zone=asia-south1-a cloudsql-ai-advisor dbdash-vm:~/
```
Or upload the zip instead: open **SSH** on the VM page in the console, use the ⚙ menu and **Upload file**, then run
`sudo apt-get install -y unzip && unzip cloudsql-ai-advisor.zip`.

Then SSH in and look:
```bash
gcloud compute ssh dbdash-vm --zone=asia-south1-a
cd ~/cloudsql-ai-advisor && ls -A
```
**Expected output**
```
.env.template  .gitignore  DEPLOY_STEPS.md  collector  deploy  src
```

## Step 3: Fill in the two config files (on the VM)

```bash
cp deploy/env.vm.template .env
nano .env                 # GCP_PROJECT_ID, CLOUDSQL_INSTANCE_ID, MYSQL_USER, MYSQL_PASSWORD (+ optional ANTHROPIC_API_KEY)
nano deploy/deploy.conf   # CLOUDSQL_CONNECTION_NAME, USE_PRIVATE_IP (+ optional DOMAIN, LETSENCRYPT_EMAIL)
```
Rules for `.env`, because `config.py` reads it literally:
- **No comments at the end of a value line.** `MYSQL_HOST=127.0.0.1  # proxy` would become the host `127.0.0.1  # proxy`. The installer refuses to run if it finds one.
- **No `GOOGLE_APPLICATION_CREDENTIALS` line.** The VM's service account is used. If you copy your local `.env` and it still has the placeholder path, the installer comments it out.
- `CLOUDSQL_INSTANCE_ID` is the instance **name** (for example `nonprod-mysql`), not the `project:region:name` connection name. The connection name goes in `deploy.conf`.

## Step 4: Run the installer

```bash
sudo bash deploy/install.sh
```
It takes about 2 minutes and asks you **once** for the dashboard password.

**Expected output**
```
==> 1/8  Installing OS packages
    OK    Python 3.12.3, nginx/1.24.0

==> 2/8  Service user 'dbdash' and app directory /opt/dbdash/app
    OK    code in /opt/dbdash/app, .env is 600 dbdash:dbdash

==> 3/8  Python virtualenv + collector/requirements.txt
    OK    google-auth 2.x  google-cloud-monitoring 2.x  PyMySQL 1.x

==> 4/8  Cloud SQL Auth Proxy v2.14.3 -> 127.0.0.1:3306
    OK    cloud-sql-proxy version 2.14.3+linux.amd64  listening on 127.0.0.1:3306

==> 5/8  One live collection cycle (before starting the service)
    [collector] Running in live mode: will attempt GCP Cloud Monitoring + direct MySQL.
    [collector] 2026-09-28T12:30:04.118+00:00 health=100.0 (Healthy) alerts=0
    mode: live   health: 100.0 Healthy   instance: nonprod-mysql   updated 0s ago
    OK    gcp_monitoring
    OK    mysql
    SKIP  ai_analysis: ANTHROPIC_API_KEY not set — AI root-cause analysis is an optional feature and is disabled.
    OK    spec source: cloudsql_admin_api (2.0 vCPU, 8.0 GB RAM, 100.0 GB disk)
    OK    GCP Monitoring and MySQL are both live

==> 6/8  dbdash.service (collector + dashboard on 127.0.0.1:8000)
    OK    active, HTTP 200

==> 7/8  nginx on :80 with a login for 'admin'
    Choose the dashboard password for 'admin':
New password:
Re-type new password:
Adding password for user admin
    OK    /healthz -> ok,  / without login -> 401

==> 8/8  HTTPS
    WARN  skipped (DOMAIN / LETSENCRYPT_EMAIL empty in deploy.conf). The password is sent unencrypted over plain http.

==> Done.
    Dashboard : http://34.93.12.34/        (login: admin)
    Liveness  : http://34.93.12.34/healthz
    Logs      : sudo journalctl -u dbdash -f
    Check     : bash /opt/dbdash/app/deploy/check.sh
```
Your exact numbers will differ. The line that matters is **`OK GCP Monitoring and MySQL are both live`** in step 5/8.

### If step 5/8 shows FAIL instead

The installer keeps going, so the site still comes up. Fix the cause, then run `sudo systemctl restart dbdash && sleep 5 && bash /opt/dbdash/app/deploy/check.sh`.

| FAIL line says | Fix |
|---|---|
| `mysql: ... (1045, "Access denied for user ...")` | The user or password is wrong, or the user doesn't exist. Run `sudo bash deploy/create_mysql_user.sh` (Step 5), or fix `/opt/dbdash/app/.env`. |
| `mysql: ... (2003, ... Connection refused)` | The proxy is down. Check `sudo journalctl -u cloud-sql-proxy -n 20`. |
| `gcp_monitoring: ... 403 Permission denied` | The VM service account lacks `roles/monitoring.viewer`. Re-run `gcp_setup.sh`. |
| `gcp_monitoring: ... File /path/to/... was not found` | Remove `GOOGLE_APPLICATION_CREDENTIALS` from `/opt/dbdash/app/.env`. |
| `spec source: manual_fallback ... Cloud SQL Admin API unreachable (... 403 ...)` | It needs `roles/cloudsql.viewer`, and the VM must have been created with `--scopes=cloud-platform` (`gcp_setup.sh` does both). |
| `spec source: manual_fallback ... (HTTP Error 404 ...)` | `CLOUDSQL_INSTANCE_ID` in `.env` is wrong. It's the instance name only. |
| Step 4/8 fails: `proxy did not start` | Check the connection name in `deploy.conf`. With `USE_PRIVATE_IP="true"`, the VM must be in the instance's VPC; otherwise set it to `"false"`. |

## Step 5 (only if you don't already have a monitoring user): create the MySQL user

```bash
sudo bash deploy/create_mysql_user.sh
```
**Expected output**
```
Existing MySQL admin user [root]: root
Enter password:
+---------------------------------------------+
| Grants for monitoring_user@%                |
+---------------------------------------------+
| GRANT USAGE ON *.* TO `monitoring_user`@`%` |
+---------------------------------------------+
Done. Now: sudo systemctl restart dbdash && sleep 5 && bash /opt/dbdash/app/deploy/check.sh
```
It creates the user and password from `.env`, with no grants. That's all `SHOW GLOBAL STATUS` needs, and it can't read table data.

## Step 6: Open it from anywhere

From your laptop:
```bash
curl -s  http://34.93.12.34/healthz
curl -sI http://34.93.12.34/ | head -1
curl -s  -u admin:YOUR_PASSWORD http://34.93.12.34/status.json | head -c 100
```
**Expected output**
```
ok
HTTP/1.1 401 Unauthorized
{
  "generated_at": "2026-09-28T12:35:11.402913+00:00",
  "instance_name": "nonprod-mysql",
```
In a browser, **`http://<VM external IP>/`** asks for `admin` and your password, then shows the dashboard with live numbers. The page refreshes itself, and the collector adds a sample every 60 s.

## Step 7 (recommended): HTTPS

1. Point a DNS **A record** (for example `dbhealth.yourdomain.com`) at the VM IP. For a quick test with no domain, use `34-93-12-34.sslip.io`, which is your IP with dashes.
2. Set `DOMAIN="dbhealth.yourdomain.com"` and `LETSENCRYPT_EMAIL="you@yourdomain.com"` in `~/cloudsql-ai-advisor/deploy/deploy.conf`.
3. Run `sudo bash deploy/install.sh` again. It's safe to re-run, and your history is kept.

**Expected at step 8/8:** `OK    certificate installed for dbhealth.yourdomain.com (auto-renews)`, and the summary shows `Dashboard : https://dbhealth.yourdomain.com/`.

---

## After install

| Task | Command (on the VM) |
|---|---|
| Is it working? | `bash /opt/dbdash/app/deploy/check.sh` |
| Live logs | `sudo journalctl -u dbdash -f` (one `health=` line per minute) |
| Change a setting | `sudo nano /opt/dbdash/app/.env && sudo systemctl restart dbdash` |
| Deploy new code | Upload the folder again, then `cd ~/cloudsql-ai-advisor && sudo bash deploy/install.sh` |
| Service status | `systemctl status dbdash cloud-sql-proxy nginx --no-pager` |
| Survives reboot | `sudo reboot`, then Step 6 again. All three services are enabled. |
| Add a login | `sudo htpasswd /etc/nginx/.htpasswd-dbdash anotheruser` |
| Reset baselines | `sudo systemctl stop dbdash && sudo rm -rf /opt/dbdash/app/collector/data && sudo systemctl start dbdash` |

Note that `install.sh` copies the uploaded folder to **`/opt/dbdash/app`**, and that's what runs. Edit `.env` there after install, or re-run the installer after changing the uploaded copy.
