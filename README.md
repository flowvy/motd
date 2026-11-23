# Flowvy MOTD

A dynamic SSH login dashboard for Debian 13, powered by Fastfetch.

## Installation

Run as root in an interactive terminal. Python 3 and curl are required. The installer offers to install Fastfetch via APT if missing.

```bash
curl -fsSL https://raw.githubusercontent.com/flowvy/motd/main/install.py -o /root/flowvy-motd-install.py && python3 /root/flowvy-motd-install.py
```

Review the installer before running it on production systems.

## Commands

| Command | Description |
| --- | --- |
| `motd` | Display a fresh server status snapshot. |
| `motd-config` | Edit settings, preview, or uninstall (root required). |

Configure provider, role, location, monitored systemd services and Docker containers interactively. IP geolocation is optional and performed only during configuration. No background daemon or external HTTP request is required at SSH login.

## Uninstall

Run `motd-config`, select **Uninstall**, and confirm with `REMOVE`. This removes the installed dashboard, configuration, backups, managed commands and SSH profile hook. System packages and services remain installed. A pre-existing `.hushlogin` is preserved.

## Compatibility

Target: Debian 13, Bash SSH login sessions, Fastfetch 2.40.4. Docker and NetBird are optional. Container information requires permission to query Docker. Integration testing on Debian 13 is pending.
