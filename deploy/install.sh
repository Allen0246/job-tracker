#!/usr/bin/env bash
# One-shot setup on a Raspberry Pi (or any systemd Linux box).
# Safe to re-run: every step is idempotent.
#
#   cd ~/profession-job-tracker && ./deploy/install.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
cd "$PROJECT_DIR"

echo "==> Checking prerequisites"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || { echo "Python 3.10+ required (found $(python3 --version))"; exit 1; }
command -v docker >/dev/null \
    || { echo "Docker not installed: curl -fsSL https://get.docker.com | sh"; exit 1; }
[[ -f .env ]] \
    || { echo "Missing .env: cp .env.example .env and fill it in first"; exit 1; }

echo "==> Python virtualenv"
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "==> Postgres"
docker compose up -d --wait

echo "==> systemd units (needs sudo)"
for unit in job-tracker.service job-tracker.timer; do
    sed -e "s|@USER@|$RUN_USER|g" -e "s|@PROJECT_DIR@|$PROJECT_DIR|g" "deploy/$unit" \
        | sudo tee "/etc/systemd/system/$unit" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now job-tracker.timer

echo
echo "Done. Next run:"
systemctl list-timers job-tracker.timer --no-pager
echo
echo "Run now:      sudo systemctl start job-tracker.service"
echo "Follow logs:  journalctl -u job-tracker.service -f"
