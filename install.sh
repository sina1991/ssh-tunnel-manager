
#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run as root."
    exit 1
fi

apt-get update
apt-get install -y openssh-client

BASE="/opt/tunnel-manager"

mkdir -p "$BASE/lib" "$BASE/data"
touch "$BASE/data/servers.db" "$BASE/data/tunnels.db"

chmod 0750 "$BASE/tunnel.sh"
chmod 0640 "$BASE/lib/"*.sh
chmod 0600 "$BASE/data/"*.db
chown -R root:root "$BASE"

echo "Tunnel Manager files prepared."
echo "Run: $BASE/tunnel.sh"
