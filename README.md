# Flowvy MOTD

A terminal dashboard for interactive Bash SSH sessions on Debian 13. It shows host metrics, network details, and the status of the services, containers, and application groups you select.

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/flowvy/motd/v0.1.12/install.sh | sh
```

The installer requires Python 3.11 or newer. It downloads the latest stable GitHub release and checks its SHA-256 checksum before installing the executable. It uses `sudo` when run by a regular account, or installs directly when run as root. The SSH greeting is initially off. CPU usage is sampled from Linux kernel counters. Docker and NetBird are optional.

Run `flowvy-motd` to open the English menu. Select **Configure appearance and services**, preview your choices, and then select **Enable SSH greeting**. The greeting applies to interactive Bash SSH logins. The menu also opens the dashboard on demand, checks for releases, displays diagnostics, and handles removal.

On Debian 13, enabling the greeting silences the stock SSH MOTD for all users. Flowvy adds reversible options to the stock `pam_motd` rules in `/etc/pam.d/sshd`. New SSH sessions pick up the change immediately.

Open **Host details** to edit the provider, server role, and location. Type in the focused field, use **Tab** or **Shift+Tab** to move, and press **Enter** to keep the edits in the draft or **Esc** to discard them.

If **Location** is blank when you open configuration, Flowvy detects the location of the server’s public outbound IP through [ipwho.is](https://ipwhois.io/documentation). The lookup runs in the background, and **Host details** shows the result under the location field. You can keep, edit, or clear it before applying settings. A saved location takes precedence. The SSH greeting reads the saved value locally.

## Select what appears

Open **Service groups** to create a group, name it, and choose its services in one flow. **Containers** has its own picker. In either list, press **/** to filter in place, **Space** to toggle an item, and **Enter** to keep the selection in the draft. Search matches service names, descriptions, and known Debian packages, or container names, images, and Compose projects. Compose selections use the project and service labels, so they survive container replacement.

Groups combine chosen services into a row. You can nominate a primary service and optionally check a local HTTP endpoint, such as `http://127.0.0.1:8080/health`. A passing application check and a failed helper produce a warning. Without a functional check, the dashboard reports observed component states. Idle scheduled services and completed one-shot jobs are treated separately from failures.

When the SSH greeting is enabled on a systemd host, a timer collects the selected system Docker statuses every 20 seconds. SSH users with no access to the system Docker socket can read the latest snapshot for up to 60 seconds. The snapshot contains the selected identifiers, states, and brief status details. Flowvy retains no status history.

## Update, disable, or uninstall

Use **Check for updates** to see the installed and available versions. Confirming the update replaces the executable after verification and retains your settings. Use **Disable SSH greeting** to turn off automatic display while keeping the dashboard available.

After upgrading from an older release with the SSH greeting already enabled, run `sudo flowvy-motd --admin enable` once to apply the current integration, including the Docker status timer and Debian MOTD suppression.

Use **Uninstall Flowvy MOTD** to review the file paths and decide whether to keep settings. Flowvy manages these paths:

- `/usr/local/bin/flowvy-motd` for the executable.
- `/etc/flowvy-motd/config.toml` for settings, after the first save.
- `/etc/profile.d/flowvy-motd.sh` for the optional SSH greeting.
- `/etc/systemd/system/flowvy-motd-docker.service` and `flowvy-motd-docker.timer` for the optional Docker status collector.
- `/run/flowvy-motd/docker-status.json` for the temporary snapshot.

Disabling the greeting or uninstalling restores the original options on any SSH PAM rule still managed by Flowvy. Existing comments, custom PAM rules, and `.hushlogin` settings stay as configured by the administrator. Uninstall checks file ownership and reports foreign or edited hooks by path. You can review the local configuration and integration via **Diagnostics**.

## Remove the previous script installation

Run the previous version's menu from an interactive terminal:

```sh
sudo /usr/local/bin/motd-config
```

Select **3. Uninstall**, then type `REMOVE`. The old uninstaller deletes `/etc/flowvy-motd`, including the settings and backups. Copy any settings or backups you want to retain to another location first. If the old wrapper is missing, use `sudo /usr/bin/python3 /etc/flowvy-motd/installer.py --config`. Do this before installing the current release.

To import a saved copy once the new version is installed, run `flowvy-motd --import-legacy /path/to/settings.json`, preview it, and confirm.

## Build and test

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
python3 scripts/build-release.py
```

The second command writes the executable archive and checksum to `dist/`.
