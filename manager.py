#!/usr/bin/env python3
"""SSH Tunnel Manager terminal and health-check entry point."""
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager


BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
USER = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
OPERATION_ERRORS = (ValueError, KeyError, OSError, RuntimeError, subprocess.SubprocessError)
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
       "-o", "StrictHostKeyChecking=yes"]


def run(args, *, check=True, data=None, timeout=20):
    stream = {"stdin": subprocess.DEVNULL} if data is None else {"input": data}
    result = subprocess.run(args, **stream, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip()
                           or f"{args[0]} exited {result.returncode}")
    return result


def output(result):
    return result.stdout.decode().strip()


def remote(server, args, *, check=True, data=None, timeout=20):
    if server.get("dashboard"):
        return run(args, check=check, data=data, timeout=timeout)
    identity = Path("/root/.ssh/30na_windows")
    options = ["-i", str(identity)] if identity.is_file() else []
    return run([*SSH, *options, f"{server['user']}@{server['host']}", shlex.join(args)],
               check=check, data=data, timeout=timeout)


def read_rows(name):
    return [line.split("|") for line in (DATA / name).read_text().splitlines() if line]


def atomic_write(path, contents):
    temp = path.with_suffix(".tmp")
    temp.write_text(contents)
    temp.replace(path)


def load():
    servers = {row[0]: dict(host=row[1], user=row[2])
               for row in read_rows("servers.db")
               if len(row) == 3 and row[0].isdigit()
               and HOST.fullmatch(row[1]) and USER.fullmatch(row[2])}
    tunnels = {}
    for row in read_rows("tunnels.db"):
        if len(row) == 6 and row[0].isdigit():
            tid, name, server, listen, target, bind = row
            tunnels[tid] = dict(name=name, server=server, listen=int(listen),
                                target=int(target), bind=bind, kind="Forward")
    reverse = DATA / "reverse.json"
    if reverse.exists():
        tunnels.update(json.loads(reverse.read_text()))
    pairs = DATA / "pairs.json"
    if pairs.exists():
        tunnels.update(json.loads(pairs.read_text()))
    return servers, tunnels


def save(servers, tunnels):
    forward = "".join(
        "|".join(map(str, (tid, t["name"], t["server"], t["listen"], t["target"], t["bind"]))) + "\n"
        for tid, t in tunnels.items() if t["kind"] == "Forward" and "local_id" not in t)
    atomic_write(DATA / "tunnels.db", forward)
    atomic_write(DATA / "reverse.json", json.dumps(
        {tid: t for tid, t in tunnels.items() if t["kind"] == "Reverse" and "local_id" not in t}))
    atomic_write(DATA / "pairs.json", json.dumps(
        {tid: t for tid, t in tunnels.items() if "local_id" in t}))
    atomic_write(DATA / "servers.db", "".join(
        f"{sid}|{s['host']}|{s['user']}\n" for sid, s in servers.items()))


@contextmanager
def locked():
    with (DATA / "manager.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def unit_name(tid, tunnel):
    if tunnel["kind"] == "Forward" and "account" not in tunnel:
        return f"ssh-tunnel-{tid}.service"
    return f"{tunnel['account']}.service"


def on_client(server, tunnel, args, **kwargs):
    if tunnel["kind"] == "Reverse":
        return remote(server, args, **kwargs)
    return on_local(tunnel, args, **kwargs)


def on_local(tunnel, args, **kwargs):
    endpoint = tunnel.get("local_endpoint")
    return remote(endpoint, args, **kwargs) if endpoint else run(args, **kwargs)


def unit_action(server, tunnel, tid, action, *, check=True):
    return on_client(server, tunnel, ["systemctl", *action, unit_name(tid, tunnel)], check=check)


def state(server, tunnel, tid):
    status = unit_action(server, tunnel, tid, ["is-active"], check=False)
    return {"active": "UP", "inactive": "DOWN", "failed": "DOWN",
            "activating": "STARTING", "deactivating": "STOPPING"}.get(output(status), "UNKNOWN")


def service_definition(args, name):
    return f"""[Unit]
Description=SSH Tunnel {name}
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
ExecStart={shlex.join(args)}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def write_unit(server, tunnel, tid, args):
    path = f"/etc/systemd/system/{unit_name(tid, tunnel)}"
    source = service_definition(args, tunnel["name"]).encode()
    on_client(server, tunnel, ["sh", "-c",
        'umask 022; cat > "$1.tmp" && mv "$1.tmp" "$1" && systemctl daemon-reload',
        "sh", path], data=source)


def forward_args(server, tunnel):
    key = Path("/root/.ssh/30na_windows")
    identity = ["-i", str(key)] if key.exists() and not tunnel.get("local_endpoint") else []
    return ["/usr/bin/ssh", "-NT", *SSH[1:], *identity, "-L",
            f"{tunnel['bind']}:{tunnel['listen']}:127.0.0.1:{tunnel['target']}",
            "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3", f"{server['user']}@{server['host']}"]


def reverse_args(tunnel):
    account = tunnel["account"]
    return ["/usr/bin/ssh", "-NT", *SSH[1:],
            "-o", f"UserKnownHostsFile={BASE}/data/{account}.hosts",
            "-i", f"{BASE}/data/{account}.key", "-o", "IdentitiesOnly=yes",
            "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3", "-R",
            f"{tunnel['bind']}:{tunnel['listen']}:127.0.0.1:{tunnel['target']}",
            f"{account}@{tunnel['ssh_host']}"]


def prepare_reverse(server, tunnel):
    account = tunnel["account"]
    key = f"{BASE}/data/{account}.key"
    known_hosts = f"{BASE}/data/{account}.hosts"
    remote(server, ["install", "-d", "-m", "0700", f"{BASE}/data"])
    host_key = output(on_local(tunnel, ["cat", "/etc/ssh/ssh_host_ed25519_key.pub"])).split()
    remote(server, ["tee", known_hosts],
           data=f"{tunnel['ssh_host']} {host_key[0]} {host_key[1]}\n".encode())
    pubkey = output(remote(server, ["sh", "-c",
        'test -f "$1" || ssh-keygen -q -t ed25519 -N "" -f "$1"; cat "$1.pub"',
        "sh", key]))
    fields = pubkey.split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519" or not re.fullmatch(r"[A-Za-z0-9+/=]+", fields[1]):
        raise RuntimeError("Invalid reverse public key")
    on_local(tunnel, ["useradd", "--system", "--no-create-home", "--home-dir", "/nonexistent", account])
    authorized = f"/etc/ssh/{account}.keys"
    on_local(tunnel, ["tee", authorized], data=(
        f'restrict,port-forwarding,permitlisten="{tunnel["bind"]}:{tunnel["listen"]}" '
        f'{fields[0]} {fields[1]}\n').encode())
    on_local(tunnel, ["chmod", "0644", authorized])
    config = f"/etc/ssh/sshd_config.d/{account}.conf"
    on_local(tunnel, ["tee", config], data=(
        f"Match User {account}\n"
        f"    AuthorizedKeysFile {authorized}\n"
        "    PasswordAuthentication no\n"
        "    KbdInteractiveAuthentication no\n"
        "    AuthenticationMethods publickey\n"
        "    MaxSessions 0\n"
        "    GatewayPorts clientspecified\n"
        "    AllowTcpForwarding remote\n"
        f"    PermitListen {tunnel['bind']}:{tunnel['listen']}\n"
        "Match all\n").encode())
    on_local(tunnel, ["sshd", "-t"])
    effective = output(on_local(tunnel, ["sshd", "-T", "-C", f"user={account},host=localhost,addr=127.0.0.1"]))
    for setting in ("gatewayports clientspecified", "maxsessions 0",
                    "allowtcpforwarding remote", "disableforwarding no",
                    f"permitlisten {tunnel['bind']}:{tunnel['listen']}"):
        if setting not in effective.splitlines():
            raise RuntimeError(f"Missing effective sshd setting: {setting}")
    on_local(tunnel, ["systemctl", "reload", "ssh"])


def target_vps(servers, tunnel):
    return tunnel.get("dashboard_target") or servers.get(tunnel["server"])


def remove_tunnel(servers, tunnels, tid):
    tunnel = tunnels[tid]
    server = target_vps(servers, tunnel)
    unit = unit_name(tid, tunnel)
    path = f"/etc/systemd/system/{unit}"
    on_client(server, tunnel, ["sh", "-ec",
        'if test -f "$1"; then systemctl daemon-reload; systemctl disable --now "$2"; '
        'rm -f "$1"; fi; systemctl daemon-reload', "sh", path, unit])
    if tunnel["kind"] == "Reverse":
        account = tunnel["account"]
        remote(server, ["rm", "-f", f"{BASE}/data/{account}.key",
                        f"{BASE}/data/{account}.key.pub", f"{BASE}/data/{account}.hosts"])
        on_local(tunnel, ["rm", "-f", f"/etc/ssh/sshd_config.d/{account}.conf", f"/etc/ssh/{account}.keys"])
        on_local(tunnel, ["sshd", "-t"])
        on_local(tunnel, ["systemctl", "reload", "ssh"])
        if on_local(tunnel, ["id", account], check=False).returncode == 0:
            on_local(tunnel, ["userdel", account])
    del tunnels[tid]
    save(servers, tunnels)
    results = tests()
    if results.pop(tid, None) is not None:
        atomic_write(DATA / "checks.json", json.dumps(results))


def next_id(records):
    return str(max(map(int, records), default=0) + 1)


def port(label):
    value = int(input(label))
    if not 1 <= value <= 65535:
        raise ValueError("Port must be 1–65535")
    return value


def choose_id(records, label):
    if not records:
        raise ValueError(f"No {label.lower()}s configured.")
    choices = ", ".join(sorted(records, key=int))
    value = input(f"{label} ID [{choices}; 0 to cancel]: ").strip()
    if value == "0":
        return None
    if value not in records:
        raise ValueError(f"Choose a {label.lower()} ID from the list: {choices}.")
    return value


def create_tunnel(servers, tunnels):
    addresses = output(run(["hostname", "-I"], check=False)).split()
    if not addresses:
        raise RuntimeError("Cannot determine the dashboard VPS address.")
    local_candidates = {"1": dict(host=addresses[0], user="root"), **servers}
    for key, endpoint in local_candidates.items():
        print(f"{key}. {endpoint['host']}" + (" (this VPS)" if key == "1" and key not in servers else ""))
    # Existing installations may already have a peer with ID 1.
    dashboard_id = next_id(servers) if "1" in servers else "1"
    if dashboard_id != "1":
        local_candidates[dashboard_id] = dict(host=addresses[0], user="root")
        print(f"{dashboard_id}. {addresses[0]} (this VPS)")
    local_id = choose_id(local_candidates, "Local VPS")
    if local_id is None:
        return
    sid = choose_id({key: value for key, value in local_candidates.items()
                     if key != local_id}, "Remote VPS")
    if sid is None:
        return
    listen = port("Local Port [e.g. 28000]: ")
    target = port("Remote Port [e.g. 443]: ")
    mode = input("Mode [1 Forward, 2 Reverse, 0 to cancel]: ").strip()
    if mode == "0":
        return
    if mode not in ("1", "2"):
        raise ValueError("Choose 1 for Forward or 2 for Reverse.")
    kind = "Forward" if mode == "1" else "Reverse"
    server = local_candidates[sid]
    if sid == dashboard_id:
        server = dict(server, dashboard=True)
    endpoint = None if local_id == dashboard_id else servers[local_id]
    bind = "0.0.0.0"
    if any(t["listen"] == listen and t.get("local_endpoint") == endpoint for t in tunnels.values()):
        raise ValueError("Local Port is already managed on this VPS.")
    tid = next_id(tunnels)
    tunnel = dict(name=f"tunnel-{tid}", server=None if sid == dashboard_id else sid, kind=kind, listen=listen, target=target,
                  bind=bind, local_id=local_id if endpoint else None, local_endpoint=endpoint,
                  dashboard_target=server if sid == dashboard_id else None,
                  account="stm-r-" + secrets.token_hex(6), ssh_host=local_candidates[local_id]["host"])
    on_local(tunnel, ["python3", "-c",
        "import socket,sys; s=socket.socket(); s.bind(('0.0.0.0',int(sys.argv[1])))",
        str(listen)])
    remote(server, ["true"])
    if kind == "Forward" and endpoint:
        on_local(tunnel, [*SSH, f"{server['user']}@{server['host']}", "true"])
    tunnels[tid] = tunnel
    save(servers, tunnels)
    try:
        if kind == "Reverse":
            prepare_reverse(server, tunnel)
            args = reverse_args(tunnel)
        else:
            args = forward_args(server, tunnel)
        write_unit(server, tunnel, tid, args)
        unit_action(server, tunnel, tid, ["enable", "--now"])
        for _ in range(12):
            if listener(tunnel):
                print(f"Tunnel {tid} started.")
                return
            time.sleep(0.5)
        details = on_client(server, tunnel, ["journalctl", "-u", unit_name(tid, tunnel),
                                              "-n", "5", "--no-pager"], check=False)
        raise RuntimeError(f"Tunnel {tid} has no listener.\n{output(details)}")
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"Tunnel {tid} setup failed: {error}")
        try:
            remove_tunnel(servers, tunnels, tid)
        except (RuntimeError, OSError, subprocess.SubprocessError) as cleanup:
            print(f"Cleanup pending for tunnel {tid}: {cleanup}")


def listener(tunnel):
    lines = output(on_local(tunnel, ["ss", "-Hlnpt", f"sport = :{tunnel['listen']}"])).splitlines()
    address = f"{tunnel['bind']}:{tunnel['listen']}"
    process = '"sshd",pid=' if tunnel["kind"] == "Reverse" else '"ssh",pid='
    return any(len(line.split()) >= 5 and line.split()[3] in (address, f"*:{tunnel['listen']}" if tunnel["bind"] == "0.0.0.0" else address) and process in line
               for line in lines)


def target_sockets(server, tunnel):
    args = ["ss", "-Hnt", "state", "established", f"sport = :{tunnel['target']}"]
    result = remote(server, args)
    sockets = (line.split()[-2:] for line in output(result).splitlines())
    return {tuple(pair) for pair in sockets if len(pair) == 2
            and all(address.startswith(("127.0.0.1:", "[::ffff:127.0.0.1]:"))
                    for address in pair)}


def test_tunnel(server, tunnel, tid):
    svc = state(server, tunnel, tid)
    if tunnel["kind"] == "Forward":
        ssh_ok = (svc == "UP" and listener(tunnel)) or (
            not tunnel.get("local_endpoint") and remote(server, ["true"], check=False).returncode == 0)
    else:
        # A live reverse listener is created by the authenticated SSH session.
        ssh_ok = svc == "UP" and listener(tunnel)
    listening = listener(tunnel) and svc == "UP"
    path_ok = False
    if listening:
        address = tunnel["bind"] if tunnel["bind"] != "0.0.0.0" else "127.0.0.1"
        try:
            baseline = target_sockets(server, tunnel)
            if tunnel.get("local_endpoint"):
                hold = ("import socket,sys,time; "
                        "s=socket.create_connection((sys.argv[1],int(sys.argv[2])),3); time.sleep(3)")
                with ThreadPoolExecutor(max_workers=1) as pool:
                    connection = pool.submit(on_local, tunnel,
                                             ["python3", "-c", hold, address, str(tunnel["listen"])],
                                             check=False)
                    while not connection.done():
                        if target_sockets(server, tunnel) - baseline:
                            path_ok = connection.result().returncode == 0
                            break
                        time.sleep(0.25)
            else:
                with socket.create_connection((address, tunnel["listen"]), timeout=3):
                    for _ in range(3):
                        if target_sockets(server, tunnel) - baseline:
                            path_ok = True
                            break
                        time.sleep(0.2)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pass
    return dict(ssh=ssh_ok, listener=listening, tcp=path_ok,
                checked=time.strftime("%Y-%m-%d %H:%M:%S %Z"))


def tests():
    path = DATA / "checks.json"
    return json.loads(path.read_text()) if path.exists() else {}


def check_passed(result):
    return not result.get("error") and all(result[name] for name in ("ssh", "listener", "tcp"))


def record_test(server, tunnel, tid):
    try:
        if server is None:
            raise RuntimeError("Associated server is missing")
        result = test_tunnel(server, tunnel, tid)
    except OPERATION_ERRORS as error:
        result = dict(ssh=False, listener=False, tcp=False, error=str(error),
                      checked=time.strftime("%Y-%m-%d %H:%M:%S %Z"))
    results = tests()
    results[tid] = result
    atomic_write(DATA / "checks.json", json.dumps(results))
    return result


def test_all(servers, tunnels):
    if not tunnels:
        print("No tunnels configured.")
        return True
    passed = 0
    print("ID   TYPE     SSH    LISTENER  TCP    RESULT")
    for tid, tunnel in sorted(tunnels.items(), key=lambda entry: int(entry[0])):
        try:
            result = record_test(target_vps(servers, tunnel), tunnel, tid)
        except OPERATION_ERRORS as error:
            print(f"{tid:<4} {tunnel['kind']:<8} ERROR: {error}", flush=True)
            continue
        ok = check_passed(result)
        passed += int(ok)
        print(f"{tid:<4} {tunnel['kind']:<8} {status_word(result['ssh']):<6} "
              f"{status_word(result['listener']):<9} {status_word(result['tcp']):<6} "
              f"{status_word(ok)}", flush=True)
        if result.get("error"):
            print(f"     {result['error']}", flush=True)
    print(f"\n{passed}/{len(tunnels)} passed.")
    return passed == len(tunnels)


def status_word(ok):
    return "OK" if ok else "FAIL"


def show(servers, tunnels):
    checks = tests()
    server_status = {}
    print("\nSERVERS\nID   HOST                           SSH")
    for sid, server in servers.items():
        try:
            ok = remote(server, ["true"], check=False).returncode == 0
            server_status[sid] = status_word(ok)
        except OPERATION_ERRORS:
            server_status[sid] = "UNKNOWN"
        print(f"{sid:<4} {server['host']:<30} {server_status[sid]}")
    if not servers:
        print("No servers configured.")

    print("\nTUNNELS\nID   TYPE     LISTEN                 TARGET                         STATE    TEST")
    for tid, tunnel in tunnels.items():
        server = target_vps(servers, tunnel)
        check = checks.get(tid)
        result = status_word(check_passed(check)) if check else "—"
        local_host = (tunnel.get("local_endpoint") or {}).get("host", "LOCAL")
        listen = f"{local_host}:{tunnel['listen']}"
        target_host = server["host"] if server else "UNKNOWN"
        target = f"{target_host}:{tunnel['target']}"
        status = "UNKNOWN"
        if server and (tunnel["kind"] == "Forward" or (server.get("dashboard") or server_status.get(tunnel["server"]) == "OK")):
            try:
                status = state(server, tunnel, tid)
            except OPERATION_ERRORS:
                pass
        print(f"{tid:<4} {tunnel['kind']:<8} {listen:<22} {target:<30} {status:<8} {result}")
    if not tunnels:
        print("No tunnels configured.")


def add_server(servers, tunnels):
    host = input("Server address [e.g. gr02.30na.top]: ").strip()
    if not HOST.fullmatch(host):
        raise ValueError("Invalid server address")
    existing = next((sid for sid, server in servers.items() if server["host"] == host), None)
    server = servers[existing] if existing else dict(host=host, user="root")
    local_machine = Path("/etc/machine-id").read_text().strip()
    identity = Path("/root/.ssh/30na_windows")
    if remote(server, ["true"], check=False).returncode:
        # ssh-copy-id performs first-use host verification and initial login.
        subprocess.run(["ssh-copy-id", "-o", "ConnectTimeout=8", "-i",
                        f"{identity}.pub", f"{server['user']}@{host}"], check=True, timeout=120)
    if output(remote(server, ["cat", "/etc/machine-id"])) == local_machine:
        raise ValueError("The local server cannot be added as a peer")
    revision = (BASE / "revision").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError("Run the installer to register the dashboard revision")
    result = remote(server, ["bash", "-s", "--", "--peer", revision],
                    data=(BASE / "install.sh").read_bytes(), timeout=600)
    print(output(result))
    installed = remote(server, ["cat", str(BASE / "manager.py")]).stdout
    if installed != (BASE / "manager.py").read_bytes():
        raise RuntimeError("The installed peer version differs from this dashboard. Update this host first.")
    remote(server, ["systemctl", "is-active", "tunnel-health-check.timer"])
    servers[existing or next_id(servers)] = server
    save(servers, tunnels)


def optional_port(label, default):
    value = input(label).strip()
    if not value:
        return default
    if not value.isascii() or not value.isdecimal() or not 1 <= int(value) <= 65535:
        raise ValueError("Port must be 1–65535.")
    return int(value)


def test_vps(servers):
    if not servers:
        print("No VPS configured.")
        return
    choice = input("VPS Test [1 All SSH connections, 2 Two VPSs, 0 Back]: ").strip()
    if choice == "0":
        return
    if choice == "1":
        for sid, server in servers.items():
            try:
                result = remote(server, ["true"], check=False)
                detail = result.stderr.decode(errors="replace").strip()
                print(f"{sid} {server['host']}: {status_word(result.returncode == 0)}"
                      + (f" — {detail}" if result.returncode and detail else ""))
            except OPERATION_ERRORS as error:
                print(f"{sid} {server['host']}: FAIL — {error}")
        return
    if choice != "2":
        raise ValueError("Choose 1, 2, or 0.")
    local_id = choose_id(servers, "Local VPS")
    if local_id is None:
        return
    remote_id = choose_id({sid: server for sid, server in servers.items() if sid != local_id}, "Remote VPS")
    if remote_id is None:
        return
    local, peer = servers[local_id], servers[remote_id]
    listen = optional_port("Local test port [Enter = free port, e.g. 28443]: ", 0)
    target = optional_port("Remote service port [Enter = 22, e.g. 443]: ", 22)
    mode = input("Mode [1 Forward, 2 Reverse, 0 Back]: ").strip()
    if mode == "0":
        return
    if mode not in ("1", "2"):
        raise ValueError("Choose 1 for Forward or 2 for Reverse.")

    free_port = ("import socket; s=socket.socket(); "
                 "s.bind(('127.0.0.1',int(__import__('sys').argv[1]))); print(s.getsockname()[1])")
    listen = int(output(remote(local, ["python3", "-c", free_port, str(listen)])))
    target_check = ("import socket,sys; "
                    "socket.create_connection(('127.0.0.1',int(sys.argv[1])),3).close()")
    remote(peer, ["python3", "-c", target_check, str(target)])
    baseline = target_sockets(peer, {"target": target})
    source, destination = (local, peer) if mode == "1" else (peer, local)
    direction = "-L" if mode == "1" else "-R"
    command = ["timeout", "12", *SSH, "-NT", "-o", "ExitOnForwardFailure=yes",
               direction, f"127.0.0.1:{listen}:127.0.0.1:{target}",
               f"{destination['user']}@{destination['host']}"]
    print(f"SSH: {source['host']} → {destination['host']}")
    print(f"TCP: {local['host']}:{listen} → {peer['host']}:{target}")
    hold = ("import socket,sys,time; "
            "s=socket.create_connection(('127.0.0.1',int(sys.argv[1])),2); "
            "print('CONNECTED',flush=True); time.sleep(3)")
    with ThreadPoolExecutor(max_workers=2) as pool:
        session = pool.submit(remote, source, command, check=False)
        for _ in range(3):
            if session.done():
                break
            probe = pool.submit(remote, local, ["python3", "-c", hold, str(listen)], check=False)
            while not probe.done() and not session.done():
                if target_sockets(peer, {"target": target}) - baseline:
                    if probe.result().returncode == 0:
                        print("Tunnel path: OK")
                        return
                    break
                time.sleep(0.25)
            time.sleep(0.25)
        result = session.result()
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(detail or "Tunnel path verification failed.")


def server_menu(choice, servers, tunnels):
    if choice == "1":
        add_server(servers, tunnels)
    elif choice == "3":
        test_vps(servers)
    elif choice == "2":
        sid = choose_id(servers, "Server")
        if sid is None:
            return
        server = servers[sid]
        related = [tid for tid, t in tunnels.items() if t["server"] == sid or t.get("local_id") == sid]
        for tid in related:
            try:
                remove_tunnel(servers, tunnels, tid)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                raise RuntimeError(f"Tunnel {tid}: {error}") from error
        del servers[sid]
        save(servers, tunnels)
        print(f"Server removed with {len(related)} tunnel(s).")


def tunnel_menu(choice, servers, tunnels):
    if choice == "1":
        create_tunnel(servers, tunnels)
        return
    if choice == "3":
        active = {}
        for tid, tunnel in tunnels.items():
            try:
                server = target_vps(servers, tunnel)
                current = state(server, tunnel, tid)
                if current == "UNKNOWN":
                    print(f"Tunnel {tid}: service state unavailable.")
                if current == "UP":
                    active[tid] = tunnel
            except OPERATION_ERRORS as error:
                print(f"Tunnel {tid}: service state unavailable — {error}")
        if active:
            test_all(servers, active)
        else:
            print("No active tunnels to test.")
        return
    tid = choose_id(tunnels, "Tunnel")
    if tid is not None:
        remove_tunnel(servers, tunnels, tid)
        print(f"Tunnel {tid} removed.")


MENUS = {
    "home": ("Main menu", "1. Servers\n2. Tunnels\n0. Exit", {"0", "1", "2"}),
    "servers": ("Servers", "1. Add server\n2. Remove server\n3. Test SSH\n0. Back",
                {"0", "1", "2", "3"}),
    "tunnels": ("Tunnel", "1. Add Tunnel\n2. Remove Tunnel\n3. Tunnel Test\n0. Back",
                {"0", "1", "2", "3"}),
}


def clear_screen():
    if sys.stdout.isatty() and os.environ.get("TERM", "dumb") != "dumb":
        print("\033[2J\033[H", end="", flush=True)


def display(section, servers, tunnels):
    clear_screen()
    addresses = output(run(["hostname", "-I"], check=False)).split()
    address = next(iter(addresses), "—")
    print(f"SSH TUNNEL MANAGER v2  ·  {socket.gethostname()}  ·  {address}")
    show(servers, tunnels)
    print(f"\n{MENUS[section][0].upper()}")


def cli():
    section = "home"
    try:
        while True:
            servers, tunnels = load()
            display(section, servers, tunnels)
            _, menu, choices = MENUS[section]
            print(menu)
            choice = input("Select > ").strip()
            if section == "home" and choice == "0":
                clear_screen()
                return
            if choice == "0":
                section = "home"
                continue
            if section == "home" and choice in {"1", "2"}:
                section = "servers" if choice == "1" else "tunnels"
                continue
            display(section, servers, tunnels)
            try:
                if choice not in choices:
                    raise ValueError("Choose one of the numbered options.")
                action = server_menu if section == "servers" else tunnel_menu
                with locked():
                    action(choice, *load())
            except OPERATION_ERRORS as error:
                print(f"Error: {error}", file=sys.stderr)
            input("\nEnter to return > ")
    except (EOFError, KeyboardInterrupt):
        return


def main():
    if os.geteuid() != 0:
        sys.exit("Run as root.")
    os.umask(0o077)
    if sys.argv[1:] == ["--health"]:
        with locked():
            passed = test_all(*load())
        if not passed:
            sys.exit(1)
    else:
        cli()


if __name__ == "__main__":
    main()
