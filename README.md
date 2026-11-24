# SSH Tunnel Manager v2

Ubuntu terminal dashboard for managing OpenSSH TCP forwards with systemd. The
dashboard host holds the server registry and the listening ports. Forward
connections originate on the dashboard host. Reverse SSH connections originate
on the selected peer, with the SSH listener on the dashboard host and the TCP
target on the peer.

## Install on the dashboard host

Run as root on Ubuntu with systemd and nano. Place the encrypted
`30na-SSH-Backup.zip` at `/root/30na-SSH-Backup.zip`. The installer checks the ZIP first and opens nano for the ZIP password. Save
with Ctrl+O, Enter, then exit with Ctrl+X. Package installation starts after
that. The pinned SSH Infrastructure 30nA installer validates the key pair and
restores it when absent. It keeps matching existing files and stops on a conflicting pair.
It registers the public key for root only if its fingerprint is absent from
`/root/.ssh/authorized_keys`, then loads the key and opens the installed dashboard.

```bash
clear
bash <(curl -fsSL https://raw.githubusercontent.com/sina1991/ssh-tunnel-manager/v2/reverse-tunnels/install.sh)
```

Open again with `sudo bash /opt/tunnel-manager/tunnel.sh`. The installer
preserves the existing server and forward databases and systemd units under
`/opt/tunnel-manager` and `/etc/systemd/system`. Existing forwards keep their
original units. A manually created reverse service must be stopped before
assigning its listener to the manager.

## Menu

- **Servers** — Add, Remove, Test SSH. Add verifies SSH access, installs the
  project and prerequisites on the peer, and saves the server after verification.
  First-time enrollment can require host-key verification and initial SSH
  authentication. Only the public key is registered on the peer. Regular
  dashboard-to-peer operations require key-based login. For unattended operations,
  the management key must work without an interactive SSH agent.
- **Tunnels** — Add, Remove, Test one, Test all, and Status. Choose Forward or
  Reverse when adding a tunnel. New tunnels start immediately and at boot. Use the dashboard host's public IPv4
  address as the listen address if it is assigned to an interface and the listener
  must accept external connections. Permit the chosen TCP port in your firewalls.
  The TCP target is `127.0.0.1` on the selected peer.
- **Remove server** — Removes every managed tunnel for the peer before removing
  the server record. If cleanup fails, the server remains registered so the
  operation can be retried.

Reverse setup generates a key on the peer and installs a restricted SSH account
on the dashboard host for that tunnel. The account permits one remote listener
and has no shell sessions. The peer pins the dashboard host's Ed25519 host key.
Custom sshd policies must allow public-key login for that account and load
`/etc/ssh/sshd_config.d/*.conf`. The SSH daemon is validated before reload.

## Checks

The dashboard separates service state from the last connection test. The timer
runs every 30 minutes. Test all checks both tunnel types and continues after
a failure. Manual tests report SSH, listener, and TCP path. TCP path
testing opens the listener and checks for a new established loopback socket at
the target while holding the connection. This probe expects services that leave
the TCP session open long enough to observe and can be affected by simultaneous
new connections. Test application authentication, external firewall reachability
and VPN egress from the actual client.

```bash
systemctl status tunnel-health-check.timer --no-pager
journalctl -u tunnel-health-check.service -n 50 --no-pager
```
