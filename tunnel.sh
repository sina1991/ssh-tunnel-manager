
#!/usr/bin/env bash
set -uo pipefail

BASE="/opt/tunnel-manager"
LIB="$BASE/lib"
DATA="$BASE/data"

SERVERS_DB="$DATA/servers.db"
TUNNELS_DB="$DATA/tunnels.db"

export BASE LIB DATA SERVERS_DB TUNNELS_DB

mkdir -p "$DATA"
touch "$DATA/servers.db" "$DATA/tunnels.db"

source "$LIB/common.sh"
source "$LIB/servers.sh"
source "$LIB/tunnels.sh"
source "$LIB/systemd.sh"

if [[ $EUID -ne 0 ]]; then
    echo "Run as root: sudo $0"
    exit 1
fi

pause() {
    read -r -p "Press Enter to continue..." _
}


show_dashboard() {
    local local_ip last_check check_epoch now_epoch delta
    local check_clock relative
    local id host user tid name sid lp rp bind
    local rec destination unit active enabled
    local total up checkrow ssh_result port_result checked
    local found

    local_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    local_ip="${local_ip:-UNKNOWN}"

    last_check="$(awk -F'|' 'NF >= 5 {print $5}' \
        "$DATA/health.db" 2>/dev/null | sort | tail -n 1 || true)"

    if [[ -n "$last_check" ]]; then
        check_epoch="$(date -d "$last_check" +%s 2>/dev/null || true)"
        if [[ "$check_epoch" =~ ^[0-9]+$ ]]; then
            now_epoch="$(date +%s)"
            delta=$((now_epoch - check_epoch))
            check_clock="$(date -d "$last_check" +%H:%M:%S 2>/dev/null || echo UNKNOWN)"

            if (( delta >= 0 )); then
                relative="$((delta / 60)) min ago"
            else
                relative="in $(( (-delta + 59) / 60 )) min"
            fi
            last_check="$check_clock ($relative)"
        else
            last_check="UNKNOWN"
        fi
    else
        last_check="NEVER"
    fi

    echo "======================================================================"
    echo "                      SSH TUNNEL MANAGER"
    echo "======================================================================"
    printf ' HOST: %-24s PUBLIC IP: %s\n' "$(hostname)" "$local_ip"
    printf ' HEALTH CHECK: %s\n' "$last_check"
    echo "----------------------------------------------------------------------"

    echo "SSH SERVERS"
    printf '%-4s %-25s %-10s %-14s\n' \
        "ID" "HOST" "USER" "TUNNELS UP/TOTAL"

    found=0
    while IFS='|' read -r id host user; do
        [[ "$id" =~ ^[0-9]+$ ]] || continue

        total=0
        up=0

        while IFS='|' read -r tid name sid lp rp bind; do
            [[ "$tid" =~ ^[0-9]+$ && "$sid" == "$id" ]] || continue
            ((total += 1))
            if systemctl is-active --quiet "ssh-tunnel-${tid}.service"; then
                ((up += 1))
            fi
        done < "$TUNNELS_DB"

        printf '%-4s %-25s %-10s %s/%s UP\n' \
            "$id" "$host" "$user" "$up" "$total"
        found=1
    done < "$SERVERS_DB"

    [[ "$found" -eq 1 ]] || echo "No servers configured."

    echo "----------------------------------------------------------------------"
    echo "TUNNELS"
    printf '%-4s %-11s %-7s %-15s %-7s %-22s %-8s %-8s %-10s\n' \
        "ID" "NAME" "LOCAL" "FROM" "REMOTE" "DESTINATION" \
        "SERVICE" "BOOT" "CHECK"

    found=0
    while IFS='|' read -r tid name sid lp rp bind; do
        [[ "$tid" =~ ^[0-9]+$ ]] || continue

        rec="$(server_by_id "$sid" 2>/dev/null)" || rec=""
        destination="UNKNOWN"
        if [[ -n "$rec" ]]; then
            IFS='|' read -r _ destination _ <<< "$rec"
        fi

        unit="ssh-tunnel-${tid}.service"

        if systemctl is-active --quiet "$unit"; then
            active="UP"
        else
            active="DOWN"
        fi

        if systemctl is-enabled --quiet "$unit"; then
            enabled="YES"
        else
            enabled="NO"
        fi

        checkrow="$(awk -F'|' -v id="$tid" \
            '$1 == id {print $2 "|" $4 "|" $5; exit}' \
            "$DATA/health.db" 2>/dev/null || true)"

        ssh_result="?"
        port_result="?"
        checked=""

        if [[ -n "$checkrow" ]]; then
            IFS='|' read -r ssh_result port_result checked <<< "$checkrow"
        fi

        printf '%-4s %-11s %-7s %-15s %-7s %-22s %-8s %-8s %s/%s\n' \
            "$tid" "$name" "$lp" "$local_ip" "$rp" "$destination" \
            "$active" "$enabled" "$ssh_result" "$port_result"

        found=1
    done < "$TUNNELS_DB"

    [[ "$found" -eq 1 ]] || echo "No tunnels configured."

    echo "----------------------------------------------------------------------"
    echo "CHECK: SSH/PORT     SERVICE: UP/DOWN     BOOT: YES/NO"
    echo "======================================================================"
}

while true; do
    clear
    show_dashboard

    cat <<'MENU'
1. Add server
2. Delete server
3. Test tunnels
4. Enable and start tunnel
5. Disable and stop tunnel
6. Show service status
7. Add tunnel
8. Delete tunnel
0. Exit
==============================================
MENU

    read -r -p "Select: " choice

    case "$choice" in
        1) add_server; pause ;;
        2) delete_server; pause ;;
        3) test_tunnels_menu; pause ;;
        4) select_tunnel_action enable; pause ;;
        5) select_tunnel_action disable; pause ;;
        6) show_status; pause ;;
        7) add_tunnel; pause ;;
        8) delete_tunnel; pause ;;
        0) exit 0 ;;
        *) echo "Invalid choice"; pause ;;
    esac
done
