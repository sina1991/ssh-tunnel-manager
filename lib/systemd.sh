
create_tunnel_service() {
    local id="$1"
    local rec name sid lp rp bind
    local serverrec host user unit tmp

    rec="$(tunnel_by_id "$id")" || return 1
    IFS='|' read -r _ name sid lp rp bind <<< "$rec"

    serverrec="$(server_by_id "$sid")" || return 1
    IFS='|' read -r _ host user <<< "$serverrec"

    unit="ssh-tunnel-${id}.service"
    tmp="$(mktemp)"

    cat > "$tmp" <<EOF
[Unit]
Description=SSH Tunnel ${name}
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=root
ExecStart=/usr/bin/ssh -N -T -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new -L ${bind}:${lp}:127.0.0.1:${rp} ${user}@${host}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

    install -o root -g root -m 0644 \
        "$tmp" "/etc/systemd/system/$unit"
    rm -f "$tmp"

    systemctl daemon-reload
    echo "Created $unit"
}
