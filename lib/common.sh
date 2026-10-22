
valid_host() {
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]
}

valid_port() {
    [[ "$1" =~ ^[0-9]{1,5}$ ]] &&
    (( 10#$1 >= 1 && 10#$1 <= 65535 ))
}

valid_id() {
    [[ "$1" =~ ^[0-9]+$ ]]
}

next_id() {
    local db="$1" id max=0
    while IFS='|' read -r id _; do
        [[ "$id" =~ ^[0-9]+$ ]] || continue
        (( id > max )) && max="$id"
    done < "$db"
    echo $((max + 1))
}

server_by_id() {
    local wanted="$1" id host user
    while IFS='|' read -r id host user; do
        if [[ "$id" == "$wanted" ]]; then
            printf '%s|%s|%s\n' "$id" "$host" "$user"
            return 0
        fi
    done < "$SERVERS_DB"
    return 1
}

tunnel_by_id() {
    local wanted="$1" id name sid lp rp bind
    while IFS='|' read -r id name sid lp rp bind; do
        if [[ "$id" == "$wanted" ]]; then
            printf '%s|%s|%s|%s|%s|%s\n' \
                "$id" "$name" "$sid" "$lp" "$rp" "$bind"
            return 0
        fi
    done < "$TUNNELS_DB"
    return 1
}

print_servers() {
    local id host user
    printf '%-5s %-32s %s\n' ID HOST USER
    while IFS='|' read -r id host user; do
        [[ -n "${id:-}" ]] &&
            printf '%-5s %-32s %s\n' "$id" "$host" "$user"
    done < "$SERVERS_DB"
}

print_tunnels() {
    local id name sid lp rp bind rec host user

    printf '%-4s %-16s %-25s %-8s %-8s %s\n' \
        ID NAME DESTINATION LOCAL REMOTE BIND

    while IFS='|' read -r id name sid lp rp bind; do
        [[ -n "${id:-}" ]] || continue

        rec="$(server_by_id "$sid" 2>/dev/null)" || rec=""
        if [[ -n "$rec" ]]; then
            IFS='|' read -r _ host user <<< "$rec"
        else
            host="MISSING"
        fi

        printf '%-4s %-16s %-25s %-8s %-8s %s\n' \
            "$id" "$name" "$host" "$lp" "$rp" "$bind"
    done < "$TUNNELS_DB"
}

choose_tunnel_id() {
    local id

    print_tunnels >&2

    [[ -s "$TUNNELS_DB" ]] || {
        echo "No tunnels configured." >&2
        return 1
    }

    read -r -p "Tunnel ID: " id
    valid_id "$id" || return 1
    tunnel_by_id "$id" >/dev/null || return 1

    printf '%s\n' "$id"
}
