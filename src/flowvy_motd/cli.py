"""The Flowvy MOTD command, interactive menu and narrow admin entry point."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from . import __version__, inventory, lifecycle, tui
from .dashboard import COLORS, RESET, render_dashboard
from .settings import MAX_BYTES, SettingsError, load, normalize, read_legacy, read_snapshot, save
from .snapshot import snapshot


def terminal_width():
    return shutil.get_terminal_size((80, 24)).columns


def show(config, *, plain=False, width=None):
    metrics, network, components, probes = snapshot(config)
    plain = plain or not sys.stdout.isatty() or os.environ.get('TERM') == 'dumb'
    color = not plain and not os.environ.get('NO_COLOR')
    return render_dashboard(config, metrics, network, components, probes,
                            width=width or terminal_width(), plain=plain, color=color)


def run_admin(arguments):
    if os.geteuid() != 0:
        raise PermissionError('This action requires root.')
    match arguments:
        case ['install', *version] if len(version) <= 1:
            lifecycle.install_from(Path(sys.argv[0]), expected=version[0] if version else None)
            print(f'Installed Flowvy MOTD {__version__}. Run flowvy-motd to open the menu.')
        case ['save', revision]:
            payload = sys.stdin.buffer.read(MAX_BYTES + 1)
            if len(payload) > MAX_BYTES:
                raise SettingsError('Settings request is too large.')
            with lifecycle.settings_write_lock():
                save(lifecycle.CONFIG, json.loads(payload), expected=revision)
            print('Settings saved.')
        case ['enable']:
            lifecycle.set_hook(True)
            print('SSH greeting enabled for your next Bash SSH login.')
        case ['disable']:
            lifecycle.set_hook(False)
            print('SSH greeting disabled.')
        case ['update', tag]:
            lifecycle.update(expected=tag)
            print(f'Updated Flowvy MOTD to {tag}. Reopen flowvy-motd to use it.')
        case ['uninstall', choice] if choice in {'keep', 'remove'}:
            paths = lifecycle.uninstall(keep_config=choice == 'keep')
            print('Flowvy MOTD uninstall finished.')
            if choice == 'keep' and lifecycle.CONFIG.is_file():
                print(f'Settings kept: {lifecycle.CONFIG}')
            for path in paths:
                print(f'Preserved existing file: {path}')
        case _:
            raise ValueError('Invalid Flowvy MOTD administration action.')


def admin_action(arguments, payload=None):
    command = [str(lifecycle.BIN), '--admin', *arguments]
    if os.geteuid() != 0:
        sudo = shutil.which('sudo')
        if sudo is None:
            raise PermissionError('Install sudo or reopen Flowvy MOTD as root for this action.')
        command = [sudo, '--', *command]
    result = subprocess.run(command, input=payload, capture_output=True, text=True, errors='replace')
    message = '\n'.join(value for value in (result.stdout.strip(), result.stderr.strip()) if value)
    if result.returncode:
        raise ValueError(message or 'The administration action did not complete.')
    return message


def _diagnostic_sections():
    installation = [f'Flowvy MOTD {__version__}', f'Executable: {lifecycle.BIN}',
                    f'Configuration: {lifecycle.CONFIG}',
                    f'SSH greeting: {lifecycle.hook_status().capitalize()}']
    selection = []
    issues = []
    try:
        config = load(lifecycle.CONFIG)
        installation.append('Settings: valid' if lifecycle.CONFIG.exists() else 'Settings: using defaults')
        selection.extend([
            f'Selected services: {len(config["services"])}',
            f'Selected containers: {len(config["containers"])}',
            f'Application groups: {len(config["applications"])}',
        ])
        ids = [f'unit:{unit}' for unit in config['services']] + config['containers']
        ids.extend(member for group in config['applications'] for member in group['members'])
        for component in inventory.collect(list(dict.fromkeys(ids)), timeout=2.0).values():
            if component.state in {'unavailable', 'missing', 'failed', 'unknown'}:
                issues.append(f'{component.name}: {component.detail or component.state}')
    except SettingsError as error:
        installation.append(f'Settings: {error}')
    tools = [f'{name}: {shutil.which(name) or "not found"}'
             for name in ('systemctl', 'fastfetch', 'ip', 'docker', 'netbird')]
    return [('Installation', installation), ('Selection', selection), ('Issues', issues), ('Tools', tools)]


def diagnostic():
    return '\n'.join(line for _, lines in _diagnostic_sections() for line in lines)


def _diagnostic_view():
    blocks = []
    for title, lines in _diagnostic_sections():
        if lines:
            header = COLORS['accent'] + title + RESET
            blocks.append('\n'.join([header, *(f'  {line}' for line in lines)]))
    return '\n\n'.join(blocks)


def preview_prompt(draft):
    size = terminal_width()
    metrics, network, components, probes = snapshot(normalize(draft))
    plain = not sys.stdout.isatty() or os.environ.get('TERM') == 'dumb'
    return render_dashboard(draft, metrics, network, components, probes,
                            width=tui._content_width(size), plain=plain, color=not plain)


def configure(source=None):
    config, revision = read_snapshot(lifecycle.CONFIG)
    if source is not None:
        config = read_legacy(source)
    draft = tui.edit_config(config, inventory.discover(), preview_prompt)
    if draft is not None:
        data = json.dumps(normalize(draft), ensure_ascii=False)
        tui.show_text('Configuration', admin_action(['save', revision], data))


def version_key(version):
    return tuple(int(part) for part in version.removeprefix('v').split('.'))


def update_menu():
    installed, latest = lifecycle.fetch_update()
    if version_key(latest) <= version_key(installed):
        tui.show_text('Updates', f'Flowvy MOTD {installed} is up to date.')
        return False
    choice = tui.menu([('cancel', 'Cancel'), ('update', f'Install {latest}')], title='Update Flowvy MOTD',
                      subtitle=f'Installed {installed}  ·  Available {latest}')
    if choice == 'update':
        tui.show_text('Updates', admin_action(['update', latest]))
        return True
    return False


def menu():
    while True:
        hook = lifecycle.hook_status()
        integration = {'enabled': 'Disable SSH greeting', 'disabled': 'Enable SSH greeting',
                       'modified': 'Inspect existing SSH hook'}[hook]
        action = tui.menu([
            ('show', 'Show MOTD'),
            ('configure', 'Configure appearance and services'),
            ('hook', integration),
            ('update', 'Check for updates'),
            ('doctor', 'Diagnostics'),
            ('uninstall', 'Uninstall Flowvy MOTD'),
            ('exit', 'Exit'),
        ], title='Main menu', subtitle=f'SSH greeting {hook}  ·  Version {__version__}')
        try:
            if action in (None, 'exit'):
                return
            if action == 'show':
                tui.show_text('MOTD', preview_prompt(load(lifecycle.CONFIG)))
            elif action == 'configure':
                configure()
            elif action == 'hook':
                if hook == 'modified':
                    tui.show_text('Existing SSH hook', f'Existing file: {lifecycle.HOOK}\nFlowvy will preserve this file.')
                else:
                    choice = 'disable' if hook == 'enabled' else 'enable'
                    decision = tui.menu([('cancel', 'Cancel'), (choice, integration)],
                                        title='SSH greeting', subtitle='Applies to interactive Bash SSH logins.')
                    if decision == choice:
                        tui.show_text('SSH greeting', admin_action([choice]))
            elif action == 'update' and update_menu():
                return
            elif action == 'doctor':
                tui.show_text('Diagnostics', _diagnostic_view())
            elif action == 'uninstall':
                keep_config = tui.confirm_uninstall([str(path) for path in (lifecycle.BIN, lifecycle.HOOK, lifecycle.CONFIG)])
                if keep_config is not None:
                    tui.show_text('Uninstall', admin_action(['uninstall', 'keep' if keep_config else 'remove']))
                    return
        except (OSError, ValueError) as error:
            tui.show_text('Action failed', str(error))


def main():
    parser = argparse.ArgumentParser(description='SSH login dashboard and terminal menu.')
    parser.add_argument('--version', action='version', version=f'Flowvy MOTD {__version__}')
    parser.add_argument('--show', action='store_true', help='print the dashboard')
    parser.add_argument('--plain', action='store_true', help='print a compact ASCII dashboard')
    parser.add_argument('--width', type=int, help='preview with a fixed dashboard width')
    parser.add_argument('--doctor', action='store_true', help='print local diagnostic information')
    parser.add_argument('--import-legacy', type=Path, metavar='FILE', help='preview and import saved legacy settings.json')
    parser.add_argument('--login', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--admin', nargs='+', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.admin:
            run_admin(args.admin)
        elif args.doctor:
            print(diagnostic())
        elif args.import_legacy:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise ValueError('Import opens an interactive preview. Run it in a terminal.')
            configure(args.import_legacy)
        elif args.login or args.show or args.plain or args.width is not None or not sys.stdin.isatty() or not sys.stdout.isatty():
            print(show(load(lifecycle.CONFIG), plain=args.plain, width=args.width), end='')
        else:
            menu()
    except (KeyboardInterrupt, EOFError):
        return 130
    except (OSError, ValueError) as error:
        if not args.login:
            print(f'Flowvy MOTD: {error}', file=sys.stderr)
        return 1
    return 0


def entrypoint():
    raise SystemExit(main())
