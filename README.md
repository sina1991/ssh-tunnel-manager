# SSH Tunnel Manager

A Bash-based SSH tunnel management dashboard for Ubuntu servers. It manages
SSH destinations, local TCP forwarding tunnels, systemd services, and health
checks.

## Features

- Interactive terminal dashboard
- Multiple SSH server configurations
- Multiple local-to-remote SSH forwarding tunnels
- systemd service creation and management for configured tunnels
- Enable, disable, start, and stop tunnel services
- Manual SSH and local-port checks
- Scheduled health checks every 30 minutes
- Persistent runtime databases

## Requirements

- Ubuntu Linux with systemd
- Root privileges for installation and system configuration
- Network access to GitHub and the package repositories during installation

## Installation

Review `install.sh` before running a remote installer as root.

Install directly on a fresh VPS:

```bash
curl -fsSL https://raw.githubusercontent.com/sina1991/ssh-tunnel-manager/main/install.sh | sudo bash
```

The installer downloads the project, installs the required packages, prepares
runtime files, configures the health-check systemd service and timer, validates
the shell scripts, and runs an initial health check. If an interactive terminal
is available, it offers to open the dashboard.

The installation directory is `/opt/tunnel-manager`.

## Usage

Open the dashboard:

```bash
sudo bash /opt/tunnel-manager/tunnel.sh
```

Use the menu to add an SSH server, then configure a tunnel for that server.
The server-add workflow installs the local root user's SSH public key on the
destination using `ssh-copy-id`; you must have the necessary authentication
access to that destination.

Enable or start a configured tunnel from the dashboard. Tunnel services are
created when a tunnel is added.

## Health Checks

Run a manual check:

```bash
sudo bash /opt/tunnel-manager/health-check.sh
```

Inspect the scheduled timer and recent logs:

```bash
systemctl status tunnel-health-check.timer --no-pager
systemctl status tunnel-health-check.service --no-pager
journalctl -u tunnel-health-check.service -n 50 --no-pager
```

## Runtime Data and Updates

Runtime databases are stored under `/opt/tunnel-manager/data/` and are not
included in the repository. The installer creates missing databases without
overwriting existing database contents.

The installer downloads the current `main` branch and installs application
files. Back up runtime databases before making major changes or reinstalling.

## Security Notes

- The dashboard and health checks run with root privileges.
- SSH private keys must never be committed to the repository.
- Treat `data/` as sensitive operational data.
- A tunnel bound to `0.0.0.0` listens on all IPv4 interfaces. Use firewall
  rules and select the bind address carefully.
- Review the installer source before executing it as root.

## Compatibility

The application uses fixed paths under `/opt/tunnel-manager` and systemd unit
files under `/etc/systemd/system/`.

## License

No license has been specified yet.
