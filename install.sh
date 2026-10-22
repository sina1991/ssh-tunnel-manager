#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

REPO="https://github.com/sina1991/ssh-tunnel-manager.git"
BRANCH="main"
BASE="/opt/tunnel-manager"

if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: Run as root: sudo bash install.sh" >&2
    exit 1
fi

if [[ ! -d /run/systemd/system ]] || ! command -v systemctl >/dev/null 2>&1; then
    echo "ERROR: This installer requires a running systemd system." >&2
    exit 1
fi

TMP="$(mktemp -d)"
cleanup() {
    rm -rf "$TMP"
}
trap cleanup EXIT

echo "==> Installing prerequisites"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git openssh-client util-linux coreutils

echo "==> Downloading project"
git clone --depth 1 --branch "$BRANCH" "$REPO" "$TMP/repo"

echo "==> Installing application files"
install -d -o root -g root -m 0750 "$BASE"
install -d -o root -g root -m 0750 "$BASE/lib"
install -d -o root -g root -m 0700 "$BASE/data"

chown root:root "$BASE" "$BASE/lib" "$BASE/data"
chmod 0750 "$BASE" "$BASE/lib"
chmod 0700 "$BASE/data"

for file in tunnel.sh health-check.sh install.sh; do
    install -o root -g root -m 0750 "$TMP/repo/$file" "$BASE/$file"
done

for file in common.sh servers.sh systemd.sh tunnels.sh; do
    install -o root -g root -m 0640 "$TMP/repo/lib/$file" "$BASE/lib/$file"
done

install -o root -g root -m 0644 "$TMP/repo/README.md" "$BASE/README.md"
install -o root -g root -m 0644 "$TMP/repo/.gitignore" "$BASE/.gitignore"

echo "==> Initializing runtime databases without deleting existing data"
for db in servers.db tunnels.db health.db; do
    touch "$BASE/data/$db"
    chown root:root "$BASE/data/$db"
    chmod 0600 "$BASE/data/$db"
done

touch "$BASE/data/health.lock"
chown root:root "$BASE/data/health.lock"
chmod 0600 "$BASE/data/health.lock"

echo "==> Installing health-check systemd units"

cat > /etc/systemd/system/tunnel-health-check.service <<'UNIT'
[Unit]
Description=SSH Tunnel Manager Health Check
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=root
ExecStart=/bin/bash /opt/tunnel-manager/health-check.sh
UNIT

cat > /etc/systemd/system/tunnel-health-check.timer <<'UNIT'
[Unit]
Description=Run SSH Tunnel Health Check Every 30 Minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=30min
AccuracySec=30s
Unit=tunnel-health-check.service

[Install]
WantedBy=timers.target
UNIT

echo "==> Validating shell scripts"
bash -n "$BASE/install.sh"
bash -n "$BASE/tunnel.sh"
bash -n "$BASE/health-check.sh"
for file in "$BASE"/lib/*.sh; do
    bash -n "$file"
done

echo "==> Reloading systemd and enabling health checks"
systemctl daemon-reload
systemctl enable --now tunnel-health-check.timer

echo "==> Running initial health check"
if ! systemctl start tunnel-health-check.service; then
    echo "WARNING: Initial health check failed. Inspect its journal:"
    echo "  journalctl -u tunnel-health-check.service -n 50 --no-pager"
fi

echo
echo "========== INSTALLATION SUMMARY =========="
echo "Application: $BASE"
echo "Dashboard:   $BASE/tunnel.sh"
echo "Health timer:"
systemctl is-active tunnel-health-check.timer || true
echo
echo "Installed files:"
ls -l "$BASE/tunnel.sh" "$BASE/health-check.sh" "$BASE/lib"
echo
echo "Runtime databases:"
ls -l "$BASE/data"
echo
echo "To open the dashboard:"
echo "  sudo bash $BASE/tunnel.sh"

if [[ -t 1 && -r /dev/tty ]]; then
    echo
    read -r -p "Open dashboard now? [Y/n] " answer </dev/tty
    if [[ ! "$answer" =~ ^[nN]$ ]]; then
        exec bash "$BASE/tunnel.sh" </dev/tty
    fi
fi
