"""Format a bounded snapshot as an English, width-aware terminal dashboard."""

import unicodedata

from .settings import clean_text
from .snapshot import NETBIRD_CONNECTION

LOGO = '''███████╗██╗      ██████╗ ██╗    ██╗██╗   ██╗██╗   ██╗
██╔════╝██║     ██╔═══██╗██║    ██║██║   ██║╚██╗ ██╔╝
█████╗  ██║     ██║   ██║██║ █╗ ██║██║   ██║ ╚████╔╝
██╔══╝  ██║     ██║   ██║██║███╗██║╚██╗ ██╔╝  ╚██╔╝
██║     ███████╗╚██████╔╝╚███╔███╔╝ ╚████╔╝    ██║
╚═╝     ╚══════╝ ╚═════╝  ╚══╝╚══╝   ╚═══╝     ╚═╝'''

COLORS = {
    'accent': '\033[1;38;5;78m', 'ok': '\033[38;5;78m', 'warn': '\033[38;5;221m',
    'bad': '\033[38;5;203m', 'muted': '\033[38;5;246m',
}
RESET = '\033[0m'
STATUS = {
    'running': ('ok', 'Running'), 'healthy': ('ok', 'Healthy'),
    'waiting': ('muted', 'Waiting'), 'completed': ('muted', 'Completed'),
    'failed': ('bad', 'Failed'), 'stopped': ('warn', 'Stopped'),
    'starting': ('warn', 'Starting'), 'stopping': ('warn', 'Stopping'),
    'paused': ('warn', 'Paused'), 'unavailable': ('warn', 'Not checked'),
    'missing': ('warn', 'Not found'), 'unknown': ('muted', 'Not checked'),
}
PROBLEM_STATES = {'failed', 'missing', 'stopped', 'paused'}
UNKNOWN_STATES = {'unavailable', 'unknown'}
SYMBOLS = {'ok': '●', 'warn': '▲', 'bad': '×', 'muted': '○'}


def describe(component):
    return STATUS.get(component.state, STATUS['unknown'])


def app_status(app, components, probe):
    members = [components[member] for member in app['members'] if member in components]
    issues = [component for component in members if component.state in PROBLEM_STATES]
    unchecked = [component for component in members if component.state in UNKNOWN_STATES]
    details = [
        f'{component.name} · {component.detail or describe(component)[1]}'
        for component in issues + unchecked
    ]
    if probe:
        result, detail = probe
        if result == 'failed':
            return 'bad', 'Unavailable', [detail, *details]
        if result == 'ok':
            return ('warn', 'Available with warnings', details) if details else ('ok', 'Available', [])
        details.insert(0, detail)
    primary = components.get(app['primary'])
    if primary and primary.state in PROBLEM_STATES:
        tone = 'bad' if primary.state in {'failed', 'stopped', 'missing'} else 'warn'
        return tone, describe(primary)[1], details
    if probe and probe[0] == 'unknown':
        return 'warn', 'Availability not checked', details
    if issues:
        count = len(issues)
        label = 'issue' if count == 1 else 'issues'
        return 'warn', f'{count} {label} in {len(app["members"])} components', details
    if unchecked or len(members) < len(app['members']):
        return 'warn', 'Some components not checked', details
    if primary:
        _, state = describe(primary)
        return ('ok' if primary.state in {'running', 'healthy'} else 'muted'), state, details
    running = sum(component.state in {'running', 'healthy'} for component in members)
    if running == len(members) and running:
        label = 'component' if running == 1 else 'components'
        return 'ok', f'{running} of {running} selected {label} running', details
    return 'muted', 'No reported failures', details


def char_width(char):
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {'W', 'F'} else 1


def fit(value, limit, ascii_only=False):
    value = clean_text(value)
    if ascii_only:
        value = value.encode('ascii', 'replace').decode()
    if sum(map(char_width, value)) <= limit:
        return value
    ellipsis = '...' if ascii_only else '…'
    limit -= len(ellipsis)
    output = []
    for char in value:
        limit -= char_width(char)
        if limit < 0:
            break
        output.append(char)
    return ''.join(output) + ellipsis


def format_bytes(size):
    if size >= 1024 ** 3:
        return f'{size / (1024 ** 3):.1f} GiB'
    return f'{size / (1024 ** 2):.0f} MiB'


def render_dashboard(config, metrics, network, components, probes, *, width=80, plain=False, color=True):
    width = max(20, min(160, width))
    compact = config.get('compact', False) or width < 64 or plain
    separator = ' | ' if plain else ' │ '
    key_width = 18 if not compact else min(11, width // 3)
    lines = []

    def tint(text, tone):
        return COLORS[tone] + text + RESET if color and not plain and text else text

    def row(label, value, tone=None):
        label = fit(label, key_width, plain)
        value = fit(value, width - key_width - len(separator), plain)
        label += ' ' * (key_width - sum(map(char_width, label)))
        lines.append(tint(label, 'accent') + separator + (tint(value, tone) if tone else value))

    def heading(title):
        lines.append('')
        if not compact:
            lines.append(tint(title, 'accent') + ' ' + tint('─' * min(50, width - len(title) - 1), 'muted'))
        else:
            lines.append(tint(title, 'accent'))

    def detail(value):
        indent = min(key_width + 3, 21)
        marker = '> ' if plain else '↳ '
        body = fit(value, width - indent - len(marker), plain)
        lines.append(' ' * indent + tint(marker + body, 'muted'))

    def status(label, tone, value):
        row(label, value if plain else f'{SYMBOLS[tone]} {value}', tone)

    def resource(label, key):
        value = metrics.get(key)
        if not value:
            return
        used, total = value
        figures = f'{format_bytes(used)} / {format_bytes(total)}'
        if key == 'swap' and not total:
            figures = 'Disabled'
        if compact:
            row(label, figures)
            return
        scale_width = 12
        scale = ' ' * scale_width
        if total:
            length = min(scale_width, max(0, round(scale_width * used / total)))
            stroke = '−'
            track = stroke if color else '·'
            scale = tint(stroke * length, 'accent') + tint(track * (scale_width - length), 'muted')
        lines.append(f"{tint(label.ljust(key_width), 'accent')}{separator}{scale}  {figures}")

    if not compact:
        lines.extend(tint(line, 'accent') for line in LOGO.splitlines())
        lines.append('')
    host = metrics.get('host', 'Server')
    meta = ' | ' if plain else ' · '
    title = meta.join(filter(None, ('FLOWVY' if compact else '', host, config.get('role'))))
    lines.append(tint(fit(title, width, plain), 'accent'))
    host_info = meta.join(filter(None, (config.get('location'), config.get('provider'))))
    if host_info and not compact:
        lines.append(fit(host_info, width, plain))

    if not compact:
        heading('SYSTEM')
    if metrics.get('os'):
        row('OS', meta.join(filter(None, (metrics['os'], metrics.get('kernel') if not compact else None))))
    if metrics.get('uptime'):
        row('Uptime', metrics['uptime'])
    if not compact:
        for label, key in (('CPU', 'cpu'), ('Load', 'load')):
            if metrics.get(key):
                row(label, metrics[key])
    resource('RAM', 'memory')
    if not compact:
        resource('Swap', 'swap')
    resource('Disk', 'disk')

    if network:
        if not compact:
            heading('NETWORK')
        for label in ('Interface', 'IPv4', 'IPv6', 'TCP'):
            if network.get(label) and (not compact or label == 'IPv4'):
                row(label, network[label])

    apps = config.get('applications', [])
    grouped = {member for app in apps for member in app['members']}
    if apps:
        heading('APPLICATIONS')
        for app in apps:
            tone, value, details = app_status(app, components, probes.get(app['name']))
            status(app['name'], tone, value)
            for message in details[:1 if compact else 2]:
                detail(message)
    mapping = (
        ('SERVICES', [f'unit:{unit}' for unit in config.get('services', [])]),
        ('CONTAINERS', config.get('containers', [])),
    )
    labels = config.get('service_labels', {})
    for title, ids in mapping:
        ungrouped = [components[item] for item in ids if item not in grouped and item in components]
        if not ungrouped:
            continue
        heading(title)
        for component in ungrouped:
            if component.state in {'waiting', 'completed'}:
                continue
            tone, value = describe(component)
            detail_text = component.detail
            if component.id == 'unit:netbird.service' and NETBIRD_CONNECTION in probes:
                state, reason = probes[NETBIRD_CONNECTION]
                if state == 'connected':
                    tone, value = 'ok', 'Connected' + (meta + reason if reason else '')
                elif state == 'connecting':
                    tone, value = 'warn', 'Connecting'
                elif state == 'disconnected':
                    tone, value = 'warn', 'Disconnected'
                elif component.state in {'running', 'healthy'}:
                    tone, value = 'warn', 'Connection not checked'
                    detail_text = reason or detail_text
            label = component.name
            if component.id.startswith('unit:'):
                label = labels.get(component.id.removeprefix('unit:'), label)
            status(label, tone, value)
            if not compact and detail_text and tone in {'warn', 'bad'}:
                detail(detail_text)

    return '\n'.join(lines) + '\n'
