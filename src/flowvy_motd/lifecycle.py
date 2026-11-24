"""Installation and SSH integration for the standalone Flowvy MOTD release."""

from contextlib import contextmanager
import errno
import fcntl
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile


BIN = Path("/usr/local/bin/flowvy-motd")
HOOK = Path("/etc/profile.d/flowvy-motd.sh")
CONFIG = Path("/etc/flowvy-motd/config.toml")
SYSTEMD_DIR = Path("/etc/systemd/system")
SYSTEMD_RUNNING = Path("/run/systemd/system")
DOCKER_CACHE = Path("/run/flowvy-motd/docker-status.json")
CONFIG_HEADER = b"# Managed by Flowvy MOTD\n"
HOOK_CONTENT = b"""# Managed by Flowvy MOTD
if [ -n "${BASH_VERSION:-}" ] && [ -n "${SSH_CONNECTION:-}" ] &&
   [ -n "${SSH_TTY:-}" ] && [ -t 0 ] && [ -t 1 ] &&
   [ -x /usr/local/bin/flowvy-motd ] &&
   [ "${FLOWVY_MOTD_SHOWN:-}" != "$SSH_CONNECTION" ]; then
    case "$0:$-" in
        -bash:*i*)
            if shopt -q login_shell; then
                FLOWVY_MOTD_SHOWN=$SSH_CONNECTION
                export FLOWVY_MOTD_SHOWN
                /usr/bin/timeout --kill-after=1s 2s /usr/local/bin/flowvy-motd --login 2>/dev/null || :
            fi
            ;;
    esac
fi
"""
_ARTIFACT = "flowvy-motd.pyz"
_REPOSITORY = "https://github.com/flowvy/motd"
_RELEASE_API = "https://api.github.com/repos/flowvy/motd/releases/latest"
_VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
_SHEBANG = b"#!/usr/bin/python3\n"
_MAX_BUNDLE_BYTES = 4 * 1024 * 1024
_DOCKER_CACHE_PREFIX = b'{"format":"flowvy-motd-docker-v1","statuses":'
_DOCKER_CACHE_TTL = 60
_DOCKER_SERVICE = "flowvy-motd-docker.service"
_DOCKER_TIMER = "flowvy-motd-docker.timer"
_DOCKER_UNITS = {
    _DOCKER_SERVICE: b"""# Managed by Flowvy MOTD
[Unit]
Description=Collect Docker statuses for Flowvy MOTD
ConditionPathExists=/etc/flowvy-motd/config.toml

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I /usr/local/bin/flowvy-motd --admin refresh-docker
Environment=PATH=/usr/sbin:/usr/bin:/sbin:/bin
Environment=DOCKER_HOST=unix:///var/run/docker.sock
RuntimeDirectory=flowvy-motd
RuntimeDirectoryMode=0755
RuntimeDirectoryPreserve=yes
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_UNIX
TimeoutStartSec=8s
""",
    _DOCKER_TIMER: b"""# Managed by Flowvy MOTD
[Unit]
Description=Refresh Flowvy MOTD Docker statuses

[Timer]
OnActiveSec=1s
OnCalendar=*-*-* *:*:00/20
AccuracySec=1s

[Install]
WantedBy=timers.target
""",
}


def _path_stat(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _trusted_directory(path):
    """Refuse symlinked or writable system directories before changing files."""
    for directory in (path, *path.parents):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()):
            raise ValueError(f"Unsafe directory: {directory}")
        sticky_ancestor = directory != path and bool(info.st_mode & stat.S_ISVTX)
        if info.st_mode & 0o022 and not sticky_ancestor:
            raise ValueError(f"Unsafe directory: {directory}")


def _safe_file(info):
    return (
        info is not None
        and stat.S_ISREG(info.st_mode)
        and info.st_uid in (0, os.geteuid())
        and not info.st_mode & 0o022
    )


@contextmanager
def _serialized():
    # A directory lock serializes package-owned mutations without a lockfile.
    _trusted_directory(BIN.parent)
    fd = os.open(BIN.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _atomic_write(path, data, mode):
    _trusted_directory(path.parent)
    with tempfile.NamedTemporaryFile(prefix=".flowvy-motd-", dir=path.parent, delete=False) as staged:
        staging_path = Path(staged.name)
        try:
            staged.write(data)
            staged.flush()
            os.fchmod(staged.fileno(), mode)
            os.fsync(staged.fileno())
            os.replace(staging_path, path)
        finally:
            staging_path.unlink(missing_ok=True)


def _read_bundle(path):
    with path.open("rb") as file:
        contents = file.read(_MAX_BUNDLE_BYTES + 1)
    if len(contents) > _MAX_BUNDLE_BYTES:
        raise ValueError(f"Flowvy MOTD bundle is too large: {path}")
    return contents


def _bundle_metadata(contents):
    if not contents.startswith(_SHEBANG):
        raise ValueError("This is not a Flowvy MOTD executable")
    try:
        with zipfile.ZipFile(BytesIO(contents)) as archive:
            archive.getinfo("__main__.py")
            archive.getinfo("flowvy_motd/lifecycle.py")
            info = archive.getinfo("flowvy_motd/__init__.py")
            if info.file_size > 8_192:
                raise ValueError("Invalid Flowvy MOTD version metadata")
            metadata = archive.read(info).decode("utf-8")
    except (KeyError, UnicodeError, zipfile.BadZipFile) as error:
        raise ValueError("This is not a Flowvy MOTD executable") from error
    match = re.search(rf'^__version__ = ["\']({_VERSION})["\']$', metadata, re.MULTILINE)
    if not match:
        raise ValueError("Missing Flowvy MOTD version metadata")
    return match[1]


def _bundle_version(path):
    return _bundle_metadata(_read_bundle(path))


def _version_key(version):
    return tuple(int(part) for part in version.split("."))


def _owned_binary():
    info = _path_stat(BIN)
    if not _safe_file(info):
        return False
    try:
        _bundle_version(BIN)
    except (OSError, ValueError, RuntimeError):
        return False
    return True


@contextmanager
def settings_write_lock():
    """Ensure configuration saves complete before uninstall removes the files."""
    with _serialized():
        if not _owned_binary():
            raise ValueError("Install Flowvy MOTD before saving settings")
        yield


def _install_bundle(contents, *, expected=None, require_installed=False):
    incoming_version = _bundle_metadata(contents)
    if expected is not None and f"v{incoming_version}" != expected:
        raise ValueError(f"Expected release {expected}, got v{incoming_version}")
    with _serialized():
        installed = _path_stat(BIN) is not None
        if require_installed and not installed:
            raise ValueError("Install Flowvy MOTD before updating")
        if installed:
            if not _owned_binary():
                raise ValueError(f"Refusing to replace a file not managed by Flowvy MOTD: {BIN}")
            previous = _read_bundle(BIN)
            if contents == previous:
                return
            installed_version = _bundle_metadata(previous)
            if _version_key(incoming_version) < _version_key(installed_version):
                raise ValueError(
                    f"Refusing to downgrade Flowvy MOTD from {installed_version} to {incoming_version}"
                )
        _atomic_write(BIN, contents, 0o755)


def install_from(source, *, expected=None):
    """Install a release bundle without changing configuration or enabling SSH."""
    _install_bundle(_read_bundle(source), expected=expected)


def _hook_status():
    try:
        _trusted_directory(HOOK.parent)
    except FileNotFoundError:
        return "disabled"
    except ValueError:
        return "modified"
    info = _path_stat(HOOK)
    if info is None:
        return "disabled"
    if _safe_file(info) and info.st_size == len(HOOK_CONTENT):
        try:
            if HOOK.read_bytes() == HOOK_CONTENT:
                return "enabled"
        except OSError:
            pass
    return "modified"


def hook_status():
    """Return whether this release's own integration is present."""
    return _hook_status()


def _systemctl(*arguments):
    try:
        result = subprocess.run(["/usr/bin/systemctl", *arguments], capture_output=True,
                                text=True, errors="replace", timeout=15)
    except subprocess.TimeoutExpired as error:
        raise ValueError("Docker status collector setup timed out") from error
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Could not configure the Docker status collector")


def _collector_files():
    existing = {name: SYSTEMD_DIR / name for name in _DOCKER_UNITS
                if _path_stat(SYSTEMD_DIR / name) is not None}
    if existing:
        _trusted_directory(SYSTEMD_DIR)
    for name, path in existing.items():
        info = _path_stat(path)
        contents = _DOCKER_UNITS[name]
        if not _safe_file(info) or info.st_size != len(contents) or path.read_bytes() != contents:
            raise ValueError(f"Refusing to change an unmanaged Docker status unit: {path}")
    return existing


def _enable_docker_collector():
    if not SYSTEMD_RUNNING.is_dir():
        return
    existing = _collector_files()
    for name, contents in _DOCKER_UNITS.items():
        if name not in existing:
            _atomic_write(SYSTEMD_DIR / name, contents, 0o644)
    _systemctl("enable", "--now", _DOCKER_TIMER)


def _disable_docker_collector():
    existing = _collector_files()
    if existing and SYSTEMD_RUNNING.is_dir():
        if _DOCKER_TIMER in existing:
            _systemctl("disable", "--now", _DOCKER_TIMER)
        if _DOCKER_SERVICE in existing:
            _systemctl("stop", _DOCKER_SERVICE)
    elif _DOCKER_TIMER in existing:
        # Also remove the timer's enablement symlink on an offline system.
        _systemctl("--no-reload", "disable", _DOCKER_TIMER)
    for path in existing.values():
        path.unlink()
    if existing and SYSTEMD_RUNNING.is_dir():
        _systemctl("daemon-reload")
    remove_docker_cache()


def managed_paths():
    return (BIN, HOOK, CONFIG, *(SYSTEMD_DIR / name for name in _DOCKER_UNITS), DOCKER_CACHE)


def _cache_contents():
    _trusted_directory(DOCKER_CACHE.parent)
    fd = os.open(DOCKER_CACHE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if info.st_uid != 0 or not _safe_file(info):
            raise ValueError(f"Unsafe Docker status cache: {DOCKER_CACHE}")
        contents = file.read(_MAX_BUNDLE_BYTES + 1)
    if len(contents) > _MAX_BUNDLE_BYTES or not contents.startswith(_DOCKER_CACHE_PREFIX):
        raise ValueError(f"Unmanaged Docker status cache: {DOCKER_CACHE}")
    return contents, info.st_mtime


def read_docker_cache():
    """Return statuses only while the root-owned snapshot is fresh."""
    try:
        contents, timestamp = _cache_contents()
        if 0 <= time.time() - timestamp <= _DOCKER_CACHE_TTL:
            statuses = json.loads(contents)["statuses"]
            if isinstance(statuses, dict):
                return {selector: entry for selector, entry in statuses.items()
                        if isinstance(entry, list) and len(entry) == 2
                        and all(isinstance(value, str) for value in entry)}
    except (OSError, ValueError, KeyError):
        pass
    return {}


def write_docker_cache(statuses):
    try:
        _cache_contents()
    except FileNotFoundError:
        pass
    payload = json.dumps(statuses, separators=(",", ":")).encode()
    _atomic_write(DOCKER_CACHE, _DOCKER_CACHE_PREFIX + payload + b"}\n", 0o644)


def remove_docker_cache():
    try:
        _cache_contents()
    except FileNotFoundError:
        return
    DOCKER_CACHE.unlink()
    try:
        DOCKER_CACHE.parent.rmdir()
    except OSError as error:
        if error.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.EBUSY):
            raise


def collector_settings():
    from .settings import load

    info = _path_stat(CONFIG)
    if info is not None:
        _trusted_directory(CONFIG.parent)
        if not _safe_file(info):
            raise ValueError(f"Unsafe collector settings: {CONFIG}")
    return load(CONFIG)


def set_hook(enabled):
    with _serialized():
        status = _hook_status()
        if status == "modified":
            raise ValueError(f"Refusing to change an existing unmanaged hook: {HOOK}")
        if enabled:
            if not _owned_binary():
                raise ValueError("Install Flowvy MOTD before enabling the SSH greeting")
            _enable_docker_collector()
            if status != "enabled":
                _atomic_write(HOOK, HOOK_CONTENT, 0o644)
        else:
            _disable_docker_collector()
            if status == "enabled":
                HOOK.unlink()


def _download(url, *, limit=_MAX_BUNDLE_BYTES):
    request = urllib.request.Request(url, headers={"User-Agent": "flowvy-motd"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            if not response.geturl().startswith("https://"):
                raise ValueError("An update download was redirected away from HTTPS")
            data = response.read(limit + 1)
    except (urllib.error.URLError, TimeoutError) as error:
        raise ValueError(f"Download failed: {error}") from error
    if len(data) > limit:
        raise ValueError("Download is larger than expected")
    return data


def _release_tag(tag):
    if not isinstance(tag, str) or not re.fullmatch(rf"v{_VERSION}", tag):
        raise ValueError("The release must use a stable version tag such as v0.1.0")
    return tag


def fetch_update():
    """Return the installed version and latest stable release tag, with v prefixes."""
    if not _owned_binary():
        raise ValueError("Install Flowvy MOTD before checking for updates")
    installed = "v" + _bundle_version(BIN)
    try:
        release = json.loads(_download(_RELEASE_API, limit=1024 * 1024))
        latest = _release_tag(release["tag_name"])
    except (KeyError, TypeError, json.JSONDecodeError, UnicodeError) as error:
        raise ValueError("GitHub did not return usable Flowvy MOTD release information") from error
    return installed, latest


def update(expected=None):
    if expected is None:
        installed, expected = fetch_update()
        if _version_key(expected[1:]) <= _version_key(installed[1:]):
            return
    tag = _release_tag(expected)
    base = f"{_REPOSITORY}/releases/download/{tag}"
    contents = _download(f"{base}/{_ARTIFACT}")
    checksum = _download(f"{base}/{_ARTIFACT}.sha256", limit=4096).strip()
    match = re.fullmatch(rb"([0-9a-fA-F]{64})(?:[ \t]+\*?flowvy-motd\.pyz)?", checksum)
    actual = hashlib.sha256(contents).hexdigest().encode("ascii")
    if not match or match[1].lower() != actual:
        raise ValueError("Release checksum did not match the downloaded executable")
    _install_bundle(contents, expected=tag, require_installed=True)


def _remove_config():
    try:
        _trusted_directory(CONFIG.parent)
    except FileNotFoundError:
        return True
    except ValueError:
        return False
    info = _path_stat(CONFIG)
    if info is None:
        return True
    if not _safe_file(info):
        return False
    with CONFIG.open("rb") as file:
        if file.read(len(CONFIG_HEADER)) != CONFIG_HEADER:
            return False
    CONFIG.unlink()
    try:
        CONFIG.parent.rmdir()
    except OSError as error:
        if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
            raise
    return True


def uninstall(keep_config):
    """Remove managed files only. Return paths preserved as ownership conflicts."""
    preserved = []
    with _serialized():
        _disable_docker_collector()
        status = _hook_status()
        if status == "enabled":
            HOOK.unlink()
        elif status == "modified":
            preserved.append(str(HOOK))
        if not keep_config and not _remove_config():
            preserved.append(str(CONFIG))
        if _path_stat(BIN) is not None:
            if _owned_binary():
                BIN.unlink()
            else:
                preserved.append(str(BIN))
    return preserved
