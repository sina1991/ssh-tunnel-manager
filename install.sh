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

ZIP=/root/30na-SSH-Backup.zip
if [[ -z "$peer" && ! -f "$ZIP" ]]; then
    echo "Place the encrypted backup at $ZIP and run the installer again." >&2
    exit 1
fi

if [[ ! -d /run/systemd/system ]] || ! command -v systemctl >/dev/null 2>&1; then
    echo "ERROR: This installer requires a running systemd system." >&2
    exit 1
fi

TMP="$(mktemp -d)"
password_dir=""
installer_agent=""
cleanup() {
    if [[ -n "$password_dir" ]]; then
        rm -rf -- "$password_dir"
    fi
    if [[ -n "$installer_agent" ]]; then
        ssh-agent -k >/dev/null || true
    fi
    rm -rf -- "$TMP"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

if [[ -z "$peer" ]]; then
    if ! command -v nano >/dev/null 2>&1; then
        echo "Install nano first, then rerun the installer." >&2
        exit 1
    fi
    password_dir="$(mktemp -d /dev/shm/tunnel-manager-password.XXXXXXXX)"
    password_file="$password_dir/zip-password.txt"
    : > "$password_file"
    echo "Enter only the ZIP password in nano. Save with Ctrl+O, Enter, then exit with Ctrl+X."
    nano -I "$password_file" </dev/tty >/dev/tty
    if ! awk 'NR > 1 {exit 1} NR == 1 {valid = length($0) > 0} END {if (!valid) exit 1}' "$password_file"; then
        echo "Enter the ZIP password on one line. Installation cancelled." >&2
        exit 1
    fi
fi

echo "==> Installing prerequisites"
export DEBIAN_FRONTEND=noninteractive
if ! {
    apt-get update &&
        apt-get install -y curl git openssh-client openssh-server p7zip-full python3 iproute2 util-linux coreutils
} >"$TMP/packages.log" 2>&1; then
    cat "$TMP/packages.log" >&2
    exit 1
fi

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
    # p7zip 16.02 reads directly from a controlling terminal. Give it a
    # private terminal so it cannot ask the person running the installer.
    cat > "$TMP/unlock-zip.py" <<'PYTHON'
import errno
import os
from pathlib import Path
import pty
import select
import signal
import sys
import termios
import time

password = Path(os.environ["STM_PASSWORD_FILE"]).read_bytes().rstrip(b"\n")
pid, terminal = pty.fork()
if pid == 0:
    try:
        os.execvp(sys.argv[1], sys.argv[1:])
    finally:
        os._exit(127)

deadline = time.monotonic() + 60
pending = b""
sent = False
reaped = False
try:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([terminal], [], [], remaining)[0]:
            raise RuntimeError("ZIP extraction timed out")
        try:
            chunk = os.read(terminal, 4096)
        except OSError as error:
            if error.errno != errno.EIO:
                raise
            break
        if not chunk:
            break
        pending = (pending + chunk)[-8192:]
        if b"Enter password" in pending:
            if sent:
                raise RuntimeError("ZIP tool rejected the supplied password")
            settings = termios.tcgetattr(terminal)
            settings[3] &= ~termios.ECHO
            termios.tcsetattr(terminal, termios.TCSANOW, settings)
            os.write(terminal, password + b"\n")
            pending = b""
            sent = True
    _, status = os.waitpid(pid, 0)
    reaped = True
    sys.exit(0 if sent and os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0 else 1)
except (OSError, RuntimeError) as error:
    print(f"ZIP extraction failed: {error}", file=sys.stderr)
    sys.exit(1)
finally:
    if not reaped:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)
    os.close(terminal)
PYTHON

    sed -i '/^if ! "\$seven" e /s|^if ! |if ! python3 "$STM_ZIP_HELPER" |; /^if ! python3 "\$STM_ZIP_HELPER" /s| </dev/tty||' "$TMP/restore.sh"
    if ! grep -Eq '^if ! python3 "\$STM_ZIP_HELPER" "\$seven" e .*; then$' "$TMP/restore.sh"; then
        echo "The ZIP restore script does not support the expected input handoff." >&2
        exit 1
    fi
    if ! LC_ALL=C.UTF-8 HOME=/root STM_ZIP_HELPER="$TMP/unlock-zip.py" STM_PASSWORD_FILE="$password_file" \
        bash "$TMP/restore.sh" "$ZIP" </dev/tty >"$TMP/restore.log"; then
        cat "$TMP/restore.log" >&2
        exit 1
    fi
    rm -f -- "$password_file"
    rmdir -- "$password_dir"
    password_dir=""
    if [[ "$existing_pair" == yes ]]; then
        echo "Existing SSH key pair matches the backup."
    else
        echo "SSH key pair installed from the backup."
    fi
    register_public_key /root/.ssh
    eval "$(ssh-agent -s)" >/dev/null
    installer_agent=yes
    ssh-add /root/.ssh/30na_windows </dev/tty
fi

echo "==> Downloading project"
git clone --quiet --depth 1 --branch "$BRANCH" "$REPO" "$TMP/repo"
if [[ -n "$revision" && "$(git -C "$TMP/repo" rev-parse HEAD)" != "$revision" ]]; then
    git -C "$TMP/repo" fetch --quiet --depth 1 origin "$revision"
    git -C "$TMP/repo" checkout --quiet --detach "$revision"
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
