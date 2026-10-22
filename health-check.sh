#!/usr/bin/env bash
set -uo pipefail

BASE="/opt/tunnel-manager"
SERVERS_DB="$BASE/data/servers.db"
TUNNELS_DB="$BASE/data/tunnels.db"
HEALTH_DB="$BASE/data/health.db"
LIB="$BASE/lib"

source "$LIB/common.sh"

[[ $EUID -eq 0 ]] || exit 1

# Use the same lock as manual tests to prevent concurrent database writes.
exec 9>"$BASE/data/health.lock"
flock 9

tmp="$(mktemp "$BASE/data/health.XXXXXX")" || exit 1
trap 'rm -f "$tmp"' EXIT

checked_at="$(date '+%F %T %Z')"

while IFS='|' read -r id name sid lp rp bind; do
    [[ "$id" =~ ^[0-9]+$ ]] || continue

    rec="$(server_by_id "$sid" 2>/dev/null)" || rec=""
    if [[ -z "$rec" ]]; then
        printf '%s|FAIL|DOWN|FAIL|%s\n' \
            "$id" "$checked_at" >> "$tmp"
        continue
    fi

    IFS='|' read -r _ host user <<< "$rec"
    unit="ssh-tunnel-${id}.service"

    if ssh \
        -o BatchMode=yes \
        -o ConnectTimeout=8 \
        -o StrictHostKeyChecking=accept-new \
        "$user@$host" true >/dev/null 2>&1; then
        ssh_result="OK"
    else
        ssh_result="FAIL"
    fi

    if systemctl is-active --quiet "$unit"; then
        service_result="ACTIVE"
    else
        service_result="DOWN"
    fi

    if timeout 3 bash -c ":</dev/tcp/127.0.0.1/$lp" \
        >/dev/null 2>&1; then
        port_result="OK"
    else
        port_result="FAIL"
    fi

    printf '%s|%s|%s|%s|%s\n' \
        "$id" "$ssh_result" "$service_result" \
        "$port_result" "$checked_at" >> "$tmp"

done < "$TUNNELS_DB"

chmod 600 "$tmp"
mv -f "$tmp" "$HEALTH_DB"
trap - EXIT
