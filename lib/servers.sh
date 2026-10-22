
add_server() {
    local host user key target output id

    read -r -p "Destination IP or domain: " host
    valid_host "$host" || {
        echo "Invalid hostname."
        return 1
    }

    read -r -p "SSH username [root]: " user
    user="${user:-root}"

    [[ "$user" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]] || {
        echo "Invalid username."
        return 1
    }

    if [[ ! -f /root/.ssh/id_ed25519.pub &&
          ! -f /root/.ssh/id_rsa.pub ]]; then
        mkdir -p /root/.ssh
        chmod 700 /root/.ssh
        ssh-keygen -t ed25519 -N "" \
            -f /root/.ssh/id_ed25519 || return 1
    fi

    if [[ -f /root/.ssh/id_ed25519.pub ]]; then
        key=/root/.ssh/id_ed25519.pub
    else
        key=/root/.ssh/id_rsa.pub
    fi

    target="$user@$host"

    echo "Installing SSH public key..."
    if ! ssh-copy-id -i "$key" "$target"; then
        echo "SSH key installation FAILED"
        return 1
    fi

    echo "Testing SSH key login..."

    if output=$(ssh \
        -o BatchMode=yes \
        -o ConnectTimeout=10 \
        -o StrictHostKeyChecking=accept-new \
        "$target" 'printf LOGIN_OK' 2>&1) &&
        [[ "$output" == *LOGIN_OK* ]]; then

        echo "login to vps TRUE"
        echo "ssh key to vps is TRUE"
    else
        echo "login to vps FALSE"
        echo "$output"
        return 1
    fi

    id="$(next_id "$SERVERS_DB")"
    printf '%s|%s|%s\n' "$id" "$host" "$user" >> "$SERVERS_DB"
    echo "Server saved with ID $id"
}

delete_server() {
    local id rec host user answer tid name sid lp rp bind

    print_servers
    read -r -p "Server ID to delete: " id
    valid_id "$id" || return 1

    rec="$(server_by_id "$id")" || {
        echo "Server not found."
        return 1
    }

    IFS='|' read -r _ host user <<< "$rec"

    echo "Selected: $user@$host"
    echo "Associated tunnels and their services will be deleted."
    read -r -p "Confirm [y/N]: " answer
    [[ "$answer" == [yY] ]] || return 0

    while IFS='|' read -r tid name sid lp rp bind; do
        [[ "$sid" == "$id" ]] || continue
        systemctl disable --now "ssh-tunnel-${tid}.service" \
            >/dev/null 2>&1 || true
        rm -f "/etc/systemd/system/ssh-tunnel-${tid}.service"
    done < "$TUNNELS_DB"

    awk -F'|' -v id="$id" '$1 != id' "$SERVERS_DB" \
        > "$SERVERS_DB.tmp" &&
        mv "$SERVERS_DB.tmp" "$SERVERS_DB"

    awk -F'|' -v id="$id" '$3 != id' "$TUNNELS_DB" \
        > "$TUNNELS_DB.tmp" &&
        mv "$TUNNELS_DB.tmp" "$TUNNELS_DB"

    systemctl daemon-reload
    echo "Server and associated tunnels deleted."
}
