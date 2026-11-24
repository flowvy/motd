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
import tempfile
import urllib.error
import urllib.request
import zipfile


BIN = Path("/usr/local/bin/flowvy-motd")
HOOK = Path("/etc/profile.d/flowvy-motd.sh")
CONFIG = Path("/etc/flowvy-motd/config.toml")
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


def set_hook(enabled):
    with _serialized():
        status = _hook_status()
        if status == "modified":
            raise ValueError(f"Refusing to change an existing unmanaged hook: {HOOK}")
        if enabled and status != "enabled":
            if not _owned_binary():
                raise ValueError("Install Flowvy MOTD before enabling the SSH greeting")
            _atomic_write(HOOK, HOOK_CONTENT, 0o644)
        if not enabled and status == "enabled":
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
