#!/usr/bin/env bash
# Pull the latest code from GitHub and restart. Run as root:  bash deploy/cloudpanel/update.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
OWNER=$(stat -c %U .)
sudo -u "$OWNER" git pull --ff-only
sudo -u "$OWNER" ./venv/bin/pip install -q -r requirements.txt
systemctl restart rail-sahayak
sleep 2 && systemctl --no-pager --lines=5 status rail-sahayak
