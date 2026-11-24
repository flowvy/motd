#!/bin/sh
set -eu

install_flowvy_motd() {
    if ! /usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        echo 'Flowvy MOTD requires Python 3.11 or newer.' >&2
        exit 1
    fi

    /usr/bin/python3 -c '
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request

repo = "https://github.com/flowvy/motd"
artifact = "flowvy-motd.pyz"
version = os.environ.get("FLOWVY_MOTD_VERSION")


def download(url, limit):
    request = urllib.request.Request(url, headers={"User-Agent": "flowvy-motd-installer"})
    with urllib.request.urlopen(request, timeout=12) as response:
        if not response.geturl().startswith("https://"):
            raise ValueError("Download redirected away from HTTPS")
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Download is larger than expected")
    return data


try:
    if version is None:
        data = download("https://api.github.com/repos/flowvy/motd/releases/latest", 1024 * 1024)
        version = json.loads(data)["tag_name"]
    if not isinstance(version, str) or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Use a stable release tag such as v0.1.0")
    root = f"{repo}/releases/download/{version}"
    executable = download(f"{root}/{artifact}", 4 * 1024 * 1024)
    checksum = download(f"{root}/{artifact}.sha256", 4096).strip()
    match = re.fullmatch(rb"([0-9a-fA-F]{64})(?:[ \t]+\*?flowvy-motd\.pyz)?", checksum)
    if not match or match[1].lower() != hashlib.sha256(executable).hexdigest().encode("ascii"):
        raise ValueError("Release checksum did not match the downloaded executable")
    with tempfile.TemporaryDirectory(prefix="flowvy-motd-install-") as temp:
        bundle = Path(temp) / artifact
        bundle.write_bytes(executable)
        command = ["/usr/bin/python3", str(bundle), "--admin", "install", version]
        if os.geteuid() != 0:
            command.insert(0, "sudo")
        result = subprocess.run(command)
        sys.exit(result.returncode)
except (OSError, ValueError, KeyError, TypeError) as error:
    sys.exit(f"Flowvy MOTD installer: {error}")
'
}

install_flowvy_motd
