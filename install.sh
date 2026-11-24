#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

REPO="https://github.com/sina1991/ssh-tunnel-manager.git"
BRANCH="v2/reverse-tunnels"
KEY_SETUP="https://raw.githubusercontent.com/sina1991/How-to/5cd85860aa6a7bc5de3bb7e9701bb18d302df987/SSH-Infrastructure-30nA/install.sh"
peer="${1:-}"
revision="${2:-}"
if [[ -n "$peer" ]]; then
    [[ "$peer" == --peer && "$revision" =~ ^[0-9a-f]{40}$ && $# -eq 2 ]] || exit 2
fi
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

if [[ -z "$peer" && ! -f /root/30na-SSH-Backup.zip ]]; then
    echo "Place the encrypted backup at /root/30na-SSH-Backup.zip and run the installer again." >&2
    exit 1
fi

echo "==> Installing prerequisites"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y curl git openssh-client openssh-server p7zip-full python3 iproute2 util-linux coreutils

register_public_key() (
    local ssh_dir="$1"
    local public_key="$ssh_dir/30na_windows.pub"
    local authorized="$ssh_dir/authorized_keys"
    local fingerprint

    fingerprint="$(ssh-keygen -lf "$public_key" | awk 'NR == 1 {print $2}')"
    [[ -n "$fingerprint" ]]
    exec 9>"$ssh_dir/.30na-authorized-keys.lock"
    flock 9
    if [[ -L "$authorized" || ( -e "$authorized" && ! -f "$authorized" ) ]]; then
        echo "Refusing unexpected authorized_keys file: $authorized" >&2
        exit 1
    fi
    if [[ -s "$authorized" ]] &&
        ssh-keygen -lf "$authorized" 2>/dev/null |
        awk -v expected="$fingerprint" '$2 == expected {found=1} END {exit !found}'; then
        echo "SSH public key already authorized for root."
    else
        touch "$authorized"
        chmod 600 "$authorized"
        if [[ -s "$authorized" && "$(tail -c 1 "$authorized" | wc -l)" -eq 0 ]]; then
            printf '\n' >> "$authorized"
        fi
        cat "$public_key" >> "$authorized"
        echo "SSH public key authorized for root."
    fi
)

if [[ -z "$peer" ]]; then
    curl -fsSL "$KEY_SETUP" -o "$TMP/restore.sh"
    existing_pair=no
    if [[ -f /root/.ssh/30na_windows && -f /root/.ssh/30na_windows.pub ]]; then
        existing_pair=yes
    fi
    HOME=/root bash "$TMP/restore.sh" /root/30na-SSH-Backup.zip </dev/tty
    if [[ "$existing_pair" == yes ]]; then
        echo "Existing SSH key pair matches the backup."
    else
        echo "SSH key pair installed from the backup."
    fi
    register_public_key /root/.ssh
    eval "$(ssh-agent -s)"
    trap 'ssh-agent -k >/dev/null; cleanup' EXIT
    ssh-add /root/.ssh/30na_windows </dev/tty
fi

echo "==> Downloading project"
git clone --depth 1 --branch "$BRANCH" "$REPO" "$TMP/repo"
if [[ -n "$revision" && "$(git -C "$TMP/repo" rev-parse HEAD)" != "$revision" ]]; then
    git -C "$TMP/repo" fetch --depth 1 origin "$revision"
    git -C "$TMP/repo" checkout --detach "$revision"
fi
git -C "$TMP/repo" rev-parse HEAD > "$TMP/revision"

for file in tunnel.sh health-check.sh install.sh; do
    bash -n "$TMP/repo/$file"
done
python3 -m py_compile "$TMP/repo/manager.py"

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

install -o root -g root -m 0640 "$TMP/repo/manager.py" "$BASE/manager.py"
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

systemctl daemon-reload
systemctl enable --now tunnel-health-check.timer
install -o root -g root -m 0640 "$TMP/revision" "$BASE/revision"
echo "Installed SSH Tunnel Manager v2 at $BASE"

if [[ -z "$peer" ]]; then
    bash "$BASE/tunnel.sh" </dev/tty
fi
