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
    return servers, tunnels


def save(servers, tunnels):
    forward = "".join(
        "|".join(map(str, (tid, t["name"], t["server"], t["listen"], t["target"], t["bind"]))) + "\n"
        for tid, t in tunnels.items() if t["kind"] == "Forward")
    atomic_write(DATA / "tunnels.db", forward)
    atomic_write(DATA / "reverse.json", json.dumps(
        {tid: t for tid, t in tunnels.items() if t["kind"] == "Reverse"}))
    atomic_write(DATA / "servers.db", "".join(
        f"{sid}|{s['host']}|{s['user']}\n" for sid, s in servers.items()))


@contextmanager
def locked():
    with (DATA / "manager.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def unit_name(tid, tunnel):
    if tunnel["kind"] == "Forward":
        return f"ssh-tunnel-{tid}.service"
    return f"{tunnel['account']}.service"


def on_client(server, tunnel, args, **kwargs):
    if tunnel["kind"] == "Reverse":
        return remote(server, args, **kwargs)
    return run(args, **kwargs)


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
    identity = ["-i", str(key)] if key.exists() else []
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
    host_key = Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text().split()
    remote(server, ["tee", known_hosts],
           data=f"{tunnel['ssh_host']} {host_key[0]} {host_key[1]}\n".encode())
    pubkey = output(remote(server, ["sh", "-c",
        'test -f "$1" || ssh-keygen -q -t ed25519 -N "" -f "$1"; cat "$1.pub"',
        "sh", key]))
    fields = pubkey.split()
    if len(fields) < 2 or fields[0] != "ssh-ed25519" or not re.fullmatch(r"[A-Za-z0-9+/=]+", fields[1]):
        raise RuntimeError("Invalid reverse public key")
    run(["useradd", "--system", "--no-create-home", "--home-dir", "/nonexistent", account])
    authorized = Path(f"/etc/ssh/{account}.keys")
    authorized.write_text(
        f'restrict,port-forwarding,permitlisten="{tunnel["bind"]}:{tunnel["listen"]}" '
        f'{fields[0]} {fields[1]}\n')
    authorized.chmod(0o644)
    config = Path(f"/etc/ssh/sshd_config.d/{account}.conf")
    config.write_text(
        f"Match User {account}\n"
        f"    AuthorizedKeysFile {authorized}\n"
        "    PasswordAuthentication no\n"
        "    KbdInteractiveAuthentication no\n"
        "    AuthenticationMethods publickey\n"
        "    MaxSessions 0\n"
        "    GatewayPorts clientspecified\n"
        "    AllowTcpForwarding remote\n"
        f"    PermitListen {tunnel['bind']}:{tunnel['listen']}\n"
        "Match all\n")
    run(["sshd", "-t"])
    effective = output(run(["sshd", "-T", "-C", f"user={account},host=localhost,addr=127.0.0.1"]))
    for setting in ("gatewayports clientspecified", "maxsessions 0",
                    "allowtcpforwarding remote", "disableforwarding no",
                    f"permitlisten {tunnel['bind']}:{tunnel['listen']}"):
        if setting not in effective.splitlines():
            raise RuntimeError(f"Missing effective sshd setting: {setting}")
    run(["systemctl", "reload", "ssh"])


def remove_tunnel(servers, tunnels, tid):
    tunnel = tunnels[tid]
    server = servers[tunnel["server"]]
    unit = unit_name(tid, tunnel)
    path = f"/etc/systemd/system/{unit}"
    on_client(server, tunnel, ["sh", "-ec",
        'if test -f "$1"; then systemctl daemon-reload; systemctl disable --now "$2"; '
        'rm -f "$1"; fi; systemctl daemon-reload', "sh", path, unit])
    if tunnel["kind"] == "Reverse":
        account = tunnel["account"]
        remote(server, ["rm", "-f", f"{BASE}/data/{account}.key",
                        f"{BASE}/data/{account}.key.pub", f"{BASE}/data/{account}.hosts"])
        Path(f"/etc/ssh/sshd_config.d/{account}.conf").unlink(missing_ok=True)
        Path(f"/etc/ssh/{account}.keys").unlink(missing_ok=True)
        run(["sshd", "-t"])
        run(["systemctl", "reload", "ssh"])
        if run(["id", account], check=False).returncode == 0:
            run(["userdel", account])
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
    sid = choose_id(servers, "Server")
    if sid is None:
        return
    kind = input("Tunnel type [1 Forward, 2 Reverse, 0 to cancel]: ").strip()
    if kind == "0":
        return
    if kind not in ("1", "2"):
        raise ValueError("Choose 1 for Forward or 2 for Reverse.")
    kind = "Forward" if kind == "1" else "Reverse"
    server = servers[sid]
    name = input("Tunnel name [e.g. GR02-XRAY]: ").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
        raise ValueError("Invalid tunnel name")
    listen = port("Listen port [e.g. 18443]: ")
    target = port("Target port on peer localhost [e.g. 443]: ")
    bind = str(ipaddress.IPv4Address(input("Listen IPv4 [default 127.0.0.1, e.g. 91.216.217.244]: ").strip() or "127.0.0.1"))
    if any(t["listen"] == listen and (t["bind"] == bind or "0.0.0.0" in (bind, t["bind"]))
           for t in tunnels.values()):
        raise ValueError("Listen address and port already managed")
    with socket.socket() as test:
        test.bind((bind, listen))
    tid = next_id(tunnels)
    tunnel = dict(name=name, server=sid, kind=kind, listen=listen, target=target, bind=bind)
    if kind == "Reverse":
        ssh_host = input("SSH address of this host from the peer [e.g. i.30na.top]: ").strip()
        if not HOST.fullmatch(ssh_host):
            raise ValueError("Invalid SSH address")
        tunnel.update(account="stm-r-" + secrets.token_hex(6), ssh_host=ssh_host)
    remote(server, ["true"])
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
        raise RuntimeError(f"Tunnel {tid} has no listener. See Status.")
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"Tunnel {tid} setup failed: {error}")
        try:
            remove_tunnel(servers, tunnels, tid)
        except (RuntimeError, OSError, subprocess.SubprocessError) as cleanup:
            print(f"Cleanup pending for tunnel {tid}: {cleanup}")


def listener(tunnel):
    lines = output(run(["ss", "-Hlnpt", f"sport = :{tunnel['listen']}"])).splitlines()
    address = f"{tunnel['bind']}:{tunnel['listen']}"
    process = '"sshd",pid=' if tunnel["kind"] == "Reverse" else '"ssh",pid='
    return any(len(line.split()) >= 5 and line.split()[3] == address and process in line
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
        ssh_ok = remote(server, ["true"], check=False).returncode == 0
    else:
        # A live reverse listener is created by the authenticated SSH session.
        ssh_ok = svc == "UP" and listener(tunnel)
    listening = listener(tunnel) and svc == "UP"
    path_ok = False
    if listening:
        address = tunnel["bind"] if tunnel["bind"] != "0.0.0.0" else "127.0.0.1"
        try:
            baseline = target_sockets(server, tunnel)
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
            result = record_test(servers.get(tunnel["server"]), tunnel, tid)
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
        server = servers.get(tunnel["server"])
        check = checks.get(tid)
        result = status_word(check_passed(check)) if check else "—"
        listen = f"{tunnel['bind']}:{tunnel['listen']}"
        target_host = server["host"] if server else "UNKNOWN"
        target = f"{target_host}:{tunnel['target']}"
        status = "UNKNOWN"
        if server and (tunnel["kind"] == "Forward" or server_status[tunnel["server"]] == "OK"):
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


def server_menu(choice, servers, tunnels):
    if choice == "1":
        add_server(servers, tunnels)
    elif choice in ("2", "3"):
        sid = choose_id(servers, "Server")
        if sid is None:
            return
        server = servers[sid]
        if choice == "3":
            print(status_word(remote(server, ["true"], check=False).returncode == 0))
            return
        related = [tid for tid, t in tunnels.items() if t["server"] == sid]
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
    if choice == "4":
        test_all(servers, tunnels)
        return
    tid = choose_id(tunnels, "Tunnel")
    if tid is None:
        return
    tunnel = tunnels[tid]
    server = servers.get(tunnel["server"])
    if choice == "3":
        result = record_test(server, tunnel, tid)
        for name in ("ssh", "listener", "tcp"):
            print(f"{name.upper()}: {status_word(result[name])}")
        if result.get("error"):
            print(f"Error: {result['error']}")
        print(f"Checked: {result['checked']}")
    elif server is None:
        raise ValueError("Associated server is missing.")
    elif choice == "2":
        remove_tunnel(servers, tunnels, tid)
        print(f"Tunnel {tid} removed.")
    else:
        status = unit_action(server, tunnel, tid, ["status", "--no-pager", "--lines=8"], check=False)
        details = output(status) or status.stderr.decode(errors="replace").strip()
        print(details or "Service status unavailable.")


MENUS = {
    "home": ("Main menu", "1. Servers\n2. Tunnels\n0. Exit", {"0", "1", "2"}),
    "servers": ("Servers", "1. Add server\n2. Remove server\n3. Test SSH\n0. Back",
                {"0", "1", "2", "3"}),
    "tunnels": ("Tunnels", "1. Add tunnel\n2. Remove tunnel\n3. Test one tunnel\n"
                "4. Test all tunnels\n5. Status\n0. Back", {"0", "1", "2", "3", "4", "5"}),
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
