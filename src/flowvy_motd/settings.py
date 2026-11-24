"""The administrator-editable configuration and legacy import."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import tomllib
from urllib.parse import urlsplit

MARKER = '# Managed by Flowvy MOTD'
MAX_BYTES = 256 * 1024
SERVICE_LABEL_LIMIT = 64
DEFAULTS = {
    'provider': '', 'role': '', 'location': '', 'compact': False,
    'services': [], 'containers': [], 'applications': [], 'service_labels': {},
}
UNIT = re.compile(r'[-\w:.\\][-\w:.@\\]*\.service\Z', re.ASCII)
DOCKER_NAME = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*\Z')


class SettingsError(ValueError):
    pass


def clean_text(value):
    return ''.join(c for c in str(value) if c.isprintable()).strip()


def config_text(value, field, max_size=160):
    if not isinstance(value, str) or len(value) > max_size or any(not c.isprintable() for c in value):
        raise SettingsError(f'{field} must be a single line of up to {max_size} characters.')
    return value.strip()


def probe_target(value):
    try:
        url = urlsplit(value)
        if (url.scheme != 'http' or url.hostname not in {'localhost', '127.0.0.1', '::1'}
                or url.username is not None or url.password is not None or url.fragment
                or not value.isascii() or any(c.isspace() for c in value)):
            raise ValueError
        port = url.port if url.port is not None else 80
        if port == 0:
            raise ValueError
    except ValueError as error:
        raise SettingsError('Health checks require a local HTTP URL, such as http://127.0.0.1:8080/health.') from error
    resource = url.path or '/'
    if url.query:
        resource += '?' + url.query
    return ('127.0.0.1' if url.hostname == 'localhost' else url.hostname), port, resource


def strings(values, field):
    if not isinstance(values, list) or len(values) > 256:
        raise SettingsError(f'{field} must be a list of up to 256 entries.')
    return list(dict.fromkeys(config_text(value, field, 255) for value in values))


def container_id(value):
    if value.startswith('container:') and DOCKER_NAME.fullmatch(value.removeprefix('container:')):
        return value
    if value.startswith('compose:'):
        parts = value.removeprefix('compose:').split('/')
        if len(parts) == 2 and all(DOCKER_NAME.fullmatch(part) for part in parts):
            return value
    raise SettingsError(f'Invalid container selection: {value}')


def component_id(value):
    if value.startswith('unit:') and UNIT.fullmatch(value.removeprefix('unit:')):
        return value
    return container_id(value)


def normalize(data):
    if not isinstance(data, dict):
        raise SettingsError('Expected a settings table.')
    unknown = data.keys() - DEFAULTS.keys()
    if unknown:
        raise SettingsError(f'Unknown configuration fields: {", ".join(sorted(unknown))}.')
    result = {field: config_text(data.get(field, ''), field) for field in ('provider', 'role', 'location')}
    compact = data.get('compact', False)
    if not isinstance(compact, bool):
        raise SettingsError('compact must be true or false.')
    result['compact'] = compact
    result['services'] = strings(data.get('services', []), 'services')
    for name in result['services']:
        if not UNIT.fullmatch(name):
            raise SettingsError(f'Invalid systemd service: {name}')
    raw_labels = data.get('service_labels', {})
    if not isinstance(raw_labels, dict):
        raise SettingsError('Service display names must be a table.')
    labels = {}
    for name, value in raw_labels.items():
        if not isinstance(name, str) or not UNIT.fullmatch(name):
            raise SettingsError(f'Invalid systemd service: {name}')
        label = config_text(value, 'Display name', SERVICE_LABEL_LIMIT)
        if name in result['services'] and label and label != name:
            labels[name] = label
    if labels:
        result['service_labels'] = labels
    result['containers'] = [container_id(value) for value in strings(data.get('containers', []), 'containers')]

    apps = data.get('applications', [])
    if not isinstance(apps, list) or len(apps) > 32:
        raise SettingsError('applications must be an array of up to 32 tables.')
    result['applications'] = []
    names = set()
    for app in apps:
        if not isinstance(app, dict) or app.keys() - {'name', 'members', 'primary', 'check_url'}:
            raise SettingsError('Invalid application table.')
        name = config_text(app.get('name', ''), 'Application name', 64)
        if not name or name.casefold() in names:
            raise SettingsError('Applications need non-empty, unique names.')
        names.add(name.casefold())
        members = [component_id(value) for value in strings(app.get('members', []), 'members')]
        if not members:
            raise SettingsError(f'{name} needs at least one selected component.')
        primary = config_text(app.get('primary', ''), 'Primary component', 255)
        if primary and primary not in members:
            raise SettingsError(f'The primary component for {name} must be in the group.')
        check_url = config_text(app.get('check_url', ''), 'Health check URL', 300)
        if check_url:
            probe_target(check_url)
        result['applications'].append({'name': name, 'members': members,
                                       'primary': primary, 'check_url': check_url})
    return result


def read_bytes(path):
    if path.parent.is_symlink() or path.is_symlink():
        raise SettingsError(f'Refusing to follow a symlink: {path}')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise SettingsError(f'Configuration path is not a regular file: {path}')
            data = source.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SettingsError(f'Cannot read {path}: {error.strerror}') from error
    if len(data) > MAX_BYTES:
        raise SettingsError(f'Configuration exceeds {MAX_BYTES} bytes.')
    return data


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else '-'


def token(path):
    return digest(read_bytes(path))


def read_snapshot(path):
    data = read_bytes(path)
    try:
        parsed = tomllib.loads(data.decode()) if data is not None else {}
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise SettingsError(f'{path}: {error}') from error
    return normalize(parsed), digest(data)


def load(path):
    return read_snapshot(path)[0]


def toml_string(value):
    return json.dumps(value, ensure_ascii=False)


def serialize(data):
    lines = [MARKER, '# Host details and dashboard selections.']
    lines.extend(f'{name} = {toml_string(data[name])}' for name in ('provider', 'role', 'location'))
    lines.append(f'compact = {str(data["compact"]).lower()}')
    lines.extend(f'{name} = {toml_string(data[name])}' for name in ('services', 'containers'))
    if labels := data.get('service_labels'):
        lines.append('\n[service_labels]')
        lines.extend(f'{toml_string(name)} = {toml_string(label)}' for name, label in sorted(labels.items()))
    for app in data['applications']:
        lines.append('\n[[applications]]')
        lines.extend(f'{name} = {toml_string(app[name])}' for name in ('name', 'members', 'primary', 'check_url'))
    return '\n'.join(lines) + '\n'


def save(path: Path, data, expected=None):
    contents = serialize(normalize(data))
    if path.parent.is_symlink():
        raise SettingsError(f'Refusing to follow a symlink: {path.parent}')
    path.parent.mkdir(exist_ok=True, mode=0o755)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise SettingsError(f'Unsafe configuration directory: {path.parent}')
        fcntl.flock(fd, fcntl.LOCK_EX)
        if path.exists() and not path.is_file():
            raise SettingsError(f'Configuration path is not a regular file: {path}')
        current = read_bytes(path)
        if current is not None and current.splitlines()[:1] != [MARKER.encode()]:
            raise SettingsError(f'Existing configuration is not owned by Flowvy: {path}')
        if expected is not None and digest(current) != expected:
            raise SettingsError('Settings changed while editing. Reopen the configuration and try again.')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', prefix='.config-', dir=path.parent, delete=False) as target:
                temporary = Path(target.name)
                target.write(contents)
                os.fchmod(target.fileno(), 0o644)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    finally:
        os.close(fd)


def read_legacy(path):
    raw = read_bytes(path)
    try:
        data = json.loads(raw) if raw is not None else None
    except (ValueError, UnicodeError) as error:
        raise SettingsError(f'Cannot read legacy settings: {error}') from error
    if not isinstance(data, dict):
        raise SettingsError(f'Expected legacy settings in {path}.')
    legacy = {field: data[field] for field in ('provider', 'role', 'location', 'services') if field in data}
    legacy['containers'] = [f'container:{name}' for name in strings(data.get('containers', []), 'containers')]
    return normalize(legacy)
