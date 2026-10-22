
list_tunnels() {
    print_tunnels
}

add_tunnel() {
    local sid rec host user name lp rp bind
    local id oldid oldname oldsid oldlp oldrp oldbind answer

    print_servers
    read -r -p "Destination server ID: " sid
    valid_id "$sid" || return 1

    rec="$(server_by_id "$sid")" || {
        echo "Server not found."
        return 1
    }

    IFS='|' read -r _ host user <<< "$rec"

    read -r -p "Tunnel name (e.g. tunnelA): " name
    [[ "$name" =~ ^[A-Za-z0-9_-]{1,32}$ ]] || {
        echo "Invalid tunnel name."
        return 1
    }

    read -r -p "Local listening port: " lp
    valid_port "$lp" || return 1

    read -r -p "Remote service port: " rp
    valid_port "$rp" || return 1

    read -r -p "Bind address [0.0.0.0]: " bind
    bind="${bind:-0.0.0.0}"

    [[ "$bind" == "0.0.0.0" || "$bind" == "127.0.0.1" ]] || {
        echo "Use 0.0.0.0 or 127.0.0.1."
        return 1
    }

    while IFS='|' read -r oldid oldname oldsid oldlp oldrp oldbind; do
        [[ -n "${oldid:-}" ]] || continue

        if [[ "$lp" == "$oldlp" && "$bind" == "$oldbind" ]]; then
            echo "Local port already used by tunnel ID $oldid"
            return 1
        fi
    done < "$TUNNELS_DB"

    id="$(next_id "$TUNNELS_DB")"

    printf '%s|%s|%s|%s|%s|%s\n' \
        "$id" "$name" "$sid" "$lp" "$rp" "$bind" \
        >> "$TUNNELS_DB"

    if ! create_tunnel_service "$id"; then
        awk -F'|' -v id="$id" '$1 != id' "$TUNNELS_DB" \
            > "$TUNNELS_DB.tmp" &&
            mv "$TUNNELS_DB.tmp" "$TUNNELS_DB"
        return 1
    fi

    echo "Tunnel created: ID $id"
    read -r -p "Enable and start now? [Y/n]: " answer

    if [[ ! "$answer" =~ ^[nN]$ ]]; then
        systemctl enable --now "ssh-tunnel-${id}.service"
        systemctl --no-pager --full status "ssh-tunnel-${id}.service" || true
    fi
}

delete_tunnel() {
    local id answer unit

    id="$(choose_tunnel_id)" || return 1
    unit="ssh-tunnel-${id}.service"

    read -r -p "Delete tunnel $id and its service? [y/N]: " answer
    [[ "$answer" == [yY] ]] || return 0

    systemctl disable --now "$unit" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$unit"

    awk -F'|' -v id="$id" '$1 != id' "$TUNNELS_DB" \
        > "$TUNNELS_DB.tmp" &&
        mv "$TUNNELS_DB.tmp" "$TUNNELS_DB"

    systemctl daemon-reload
    echo "Tunnel deleted."
}

test_one_tunnel() {
    local id="$1" rec name sid lp rp bind
    local serverrec host user unit
    local ssh_result service_result port_result checked_at tmp

    rec="$(tunnel_by_id "$id")" || return 1
    IFS='|' read -r _ name sid lp rp bind <<< "$rec"

    serverrec="$(server_by_id "$sid")" || return 1
    IFS='|' read -r _ host user <<< "$serverrec"

    unit="ssh-tunnel-${id}.service"

    echo "Tunnel: $name"
    echo "Forward: $bind:$lp -> $host:127.0.0.1:$rp"

    if ssh -o BatchMode=yes -o ConnectTimeout=8 \
        -o StrictHostKeyChecking=accept-new \
        "$user@$host" true >/dev/null 2>&1; then
        ssh_result="OK"
        echo "SSH LOGIN: OK"
    else
        ssh_result="FAIL"
        echo "SSH LOGIN: FAIL"
    fi

    if systemctl is-active --quiet "$unit"; then
        service_result="ACTIVE"
        echo "SYSTEMD: ACTIVE"
    else
        service_result="DOWN"
        echo "SYSTEMD: INACTIVE"
    fi

    if systemctl is-enabled --quiet "$unit"; then
        echo "AUTOSTART: ENABLED"
    else
        echo "AUTOSTART: DISABLED"
    fi

    if timeout 3 bash -c ":</dev/tcp/127.0.0.1/$lp" \
        >/dev/null 2>&1; then
        port_result="OK"
        echo "LOCAL PORT: OPEN"
    else
        port_result="FAIL"
        echo "LOCAL PORT: NOT REACHABLE"
    fi

    checked_at="$(date '+%F %T %Z')"

    # Serialize manual updates with the scheduled health checker.
    exec 9>"$DATA/health.lock"
    flock 9

    touch "$DATA/health.db"
    tmp="$(mktemp "$DATA/health.XXXXXX")" || {
        echo "WARNING: Could not create health database temp file."
        return 1
    }

    if ! awk -F'|' -v id="$id" '$1 != id' "$DATA/health.db" > "$tmp"; then
        rm -f "$tmp"
        echo "WARNING: Could not prepare health database update."
        return 1
    fi

    printf '%s|%s|%s|%s|%s\n' \
        "$id" "$ssh_result" "$service_result" "$port_result" "$checked_at" \
        >> "$tmp"

    chmod 600 "$tmp"
    if ! mv -f "$tmp" "$DATA/health.db"; then
        rm -f "$tmp"
        echo "WARNING: Could not save health database."
        return 1
    fi

    echo "HEALTH RESULT: SAVED"
}
test_tunnels_menu() {
    local choice id

    echo "1. Test all tunnels"
    echo "2. Test a single tunnel"
    read -r -p "Select: " choice

    case "$choice" in
        1)
            while IFS='|' read -r id _; do
                [[ -n "${id:-}" ]] && test_one_tunnel "$id"
            done < "$TUNNELS_DB"
            ;;
        2)
            id="$(choose_tunnel_id)" || return 1
            test_one_tunnel "$id"
            ;;
        *) echo "Invalid choice" ;;
    esac
}

select_tunnel_action() {
    local action="$1" id unit

    id="$(choose_tunnel_id)" || return 1
    unit="ssh-tunnel-${id}.service"

    if [[ "$action" == "enable" ]]; then
        systemctl enable --now "$unit"
    else
        systemctl disable --now "$unit"
    fi

    systemctl --no-pager --full status "$unit" || true
}

show_status() {
    local id name sid lp rp bind unit

    while IFS='|' read -r id name sid lp rp bind; do
        [[ -n "${id:-}" ]] || continue
        unit="ssh-tunnel-${id}.service"

        echo
        echo "===== $name (ID $id) ====="
        systemctl is-active "$unit" 2>/dev/null || true
        systemctl is-enabled "$unit" 2>/dev/null || true
        systemctl --no-pager --lines=8 status "$unit" 2>/dev/null || true
    done < "$TUNNELS_DB"
}
