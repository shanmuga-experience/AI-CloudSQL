#!/usr/bin/env bash
# Run ONCE in Cloud Shell (or anywhere with gcloud + project owner/editor rights),
# BEFORE uploading the project. Creates: APIs, a read-only service account, a
# static IP, the VM, and a firewall rule for 80/443. Safe to re-run.
#
#   bash gcp_setup.sh
set -euo pipefail

# ---- edit these ----
PROJECT="your-gcp-project-id"
REGION="asia-south1"                 # same region as the Cloud SQL instance
ZONE="asia-south1-a"
NETWORK="default"                    # the VPC the Cloud SQL private IP lives in
VM="dbdash-vm"
MACHINE_TYPE="e2-small"
ALLOWED_SOURCE="0.0.0.0/0"           # public; narrow to your office/VPN CIDR if you can
# --------------------

SA_NAME=dbdash-vm
SA="$SA_NAME@$PROJECT.iam.gserviceaccount.com"
gcloud config set project "$PROJECT" >/dev/null

echo "==> APIs"
gcloud services enable compute.googleapis.com sqladmin.googleapis.com monitoring.googleapis.com

echo "==> Service account $SA"
gcloud iam service-accounts describe "$SA" &>/dev/null \
  || gcloud iam service-accounts create "$SA_NAME" --display-name="DB health dashboard VM"
for ROLE in roles/monitoring.viewer roles/cloudsql.viewer roles/cloudsql.client; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" \
    --role="$ROLE" --condition=None --format=none
  echo "    granted $ROLE"
done

echo "==> Static IP"
gcloud compute addresses describe dbdash-ip --region="$REGION" &>/dev/null \
  || gcloud compute addresses create dbdash-ip --region="$REGION"
VM_IP="$(gcloud compute addresses describe dbdash-ip --region="$REGION" --format='value(address)')"

echo "==> VM $VM"
gcloud compute instances describe "$VM" --zone="$ZONE" &>/dev/null \
  || gcloud compute instances create "$VM" --zone="$ZONE" --machine-type="$MACHINE_TYPE" \
       --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud --boot-disk-size=20GB \
       --network="$NETWORK" --address="$VM_IP" \
       --service-account="$SA" --scopes=cloud-platform --tags=dbdash-web

echo "==> Firewall 80/443 from $ALLOWED_SOURCE"
gcloud compute firewall-rules describe dbdash-allow-web &>/dev/null \
  || gcloud compute firewall-rules create dbdash-allow-web --network="$NETWORK" \
       --direction=INGRESS --action=ALLOW --rules=tcp:80,tcp:443 \
       --target-tags=dbdash-web --source-ranges="$ALLOWED_SOURCE"

echo
echo "==> Cloud SQL connection names in $PROJECT (put yours in deploy/deploy.conf):"
gcloud sql instances list --format="table(name, connectionName, ipAddresses[].type.list():label=IP_TYPES)"
echo
echo "VM external IP: $VM_IP"
echo "Next: upload the project folder:  gcloud compute scp --recurse --zone=$ZONE cloudsql-ai-advisor $VM:~/"
