# SSH Tunnel Manager

A lightweight Bash-based SSH tunnel management tool for Ubuntu servers.
It provides an interactive terminal dashboard for managing SSH servers,
local forwarding tunnels, systemd services, and tunnel health checks.

## Features

- Interactive terminal dashboard
- Multiple SSH server configurations
- Multiple local-to-remote SSH forwarding tunnels
- systemd service status and startup checks
- Manual tunnel testing
- Health-check status and timestamps
- Server and tunnel management through a menu

## Requirements

- Ubuntu Linux
- Bash
- OpenSSH client
- systemd
- Root privileges for service and system configuration changes

## Project Structure

```text
ssh-tunnel-manager/
├── tunnel.sh
├── install.sh
├── health-check.sh
├── lib/
│   ├── common.sh
│   ├── servers.sh
│   ├── systemd.sh
│   └── tunnels.sh
├── data/              # Local runtime data; not tracked by Git
├── .gitignore
└── README.md
```

## Installation

The current scripts use `/opt/tunnel-manager` as the installation path.

Review the scripts before running them on a server.

```bash
sudo apt-get update
sudo apt-get install -y git openssh-client
sudo git clone https://github.com/sina1991/ssh-tunnel-manager.git /opt/tunnel-manager
cd /opt/tunnel-manager
sudo bash install.sh
sudo bash tunnel.sh
```

The current `install.sh` prepares file permissions and basic runtime
database files. It does not, by itself, perform a complete fresh deployment
of every systemd service and health-check timer. Review the service-management
code and configure the required systemd units before relying on scheduled
health checks on a new server.

## Usage

Run the interactive menu as root:

```bash
sudo bash /opt/tunnel-manager/tunnel.sh
```

Use the menu to add SSH servers, configure tunnels, test connectivity,
and manage tunnel services.

## Health Checks

Run the health-check script manually with Bash:

```bash
sudo bash /opt/tunnel-manager/health-check.sh
```

If scheduled health checks are configured, inspect their status with:

```bash
systemctl status tunnel-health-check.timer
systemctl status tunnel-health-check.service
```

## Security

- Do not commit real server databases or runtime configuration.
- Never commit SSH private keys, passwords, access tokens, or credentials.
- Keep `data/` and `_archive/` private.
- Review all changes before pushing to a public repository.
- Use dedicated SSH credentials with the minimum permissions required.

## Configuration and Compatibility

The scripts currently use fixed paths under `/opt/tunnel-manager`.
Review the source code before installing the project in a different location.

## License

No license has been specified yet. All rights remain with the copyright holder
until a license is added.
