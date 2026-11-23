#!/usr/bin/env python3
import datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

BASE = Path('/etc/flowvy-motd')
HOOK = Path('/etc/profile.d/flowvy-motd.sh')
COMMANDS = {
    Path('/usr/local/bin/motd'): '#!/bin/sh\n# Managed by Flowvy MOTD\nexec /usr/bin/python3 /etc/flowvy-motd/installer.py --show\n',
    Path('/usr/local/bin/motd-config'): '#!/bin/sh\n# Managed by Flowvy MOTD\nexec /usr/bin/python3 /etc/flowvy-motd/installer.py --config\n',
}
HUSH = Path('/root/.hushlogin')
HUSH_MARKER = BASE/'hushlogin-created'
GREEN = '\033[38;2;73;221;147m'
RESET = '\033[0m'
GRAY = '\033[90m'
RED = '\033[31m'
YELLOW = '\033[33m'
LOGO = '''███████╗██╗      ██████╗ ██╗    ██╗██╗   ██╗██╗   ██╗
██╔════╝██║     ██╔═══██╗██║    ██║██║   ██║╚██╗ ██╔╝
█████╗  ██║     ██║   ██║██║ █╗ ██║██║   ██║ ╚████╔╝
██╔══╝  ██║     ██║   ██║██║███╗██║╚██╗ ██╔╝  ╚██╔╝
██║     ███████╗╚██████╔╝╚███╔███╔╝ ╚████╔╝    ██║
╚═╝     ╚══════╝ ╚═════╝  ╚══╝╚══╝   ╚═══╝     ╚═╝'''
KNOWN = {'netbird.service':'NetBird', 'beszel-agent.service':'Beszel Agent', 'docker.service':'Docker', 'containerd.service':'Containerd', 'nginx.service':'Nginx', 'ssh.service':'SSH', 'nftables.service':'Firewall', 'fail2ban.service':'Fail2Ban', 'warp-native-killswitch.service':'WARP Killswitch', 'xray.service':'Xray', 'sing-box.service':'sing-box', 'caddy.service':'Caddy', 'haproxy.service':'HAProxy'}

def run(*args, timeout=3):
    try:
        p = subprocess.run(args, text=True, errors='replace', capture_output=True, timeout=timeout, env={**os.environ,'LC_ALL':'C'})
        return p.stdout.strip() if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None

def clean(value):
    return ''.join(c for c in str(value) if c.isprintable()).strip()

def label(unit):
    return KNOWN.get(unit, unit.removesuffix('.service'))

def network():
    routes = run('ip','-j','-4','route','show','default')
    try:
        route = min(json.loads(routes or '[]'), key=lambda r:r.get('metric',0))
        iface = route['dev']
        addresses = json.loads(run('ip','-j','addr','show','dev',iface) or '[]')
        return iface, [a for i in addresses for a in i.get('addr_info',[]) if a.get('scope')=='global']
    except (ValueError, KeyError, TypeError):
        return '', []

def locate(addresses):
    candidates = [a['local'] for a in addresses if a.get('family')=='inet' and ipaddress.ip_address(a['local']).is_global]
    candidates += [a['local'] for a in addresses if a.get('family')=='inet6' and ipaddress.ip_address(a['local']).is_global]
    if not candidates:
        print('No public IP found on the default interface. Enter location manually.')
        return ''
    ip = candidates[0]
    print(f'Geolocation sends {ip} to ipwho.is over HTTPS.')
    if input('Look up location? [Y/n]: ').strip().lower() not in ('','y','yes'):
        return ''
    try:
        with urllib.request.urlopen('https://ipwho.is/'+ip,timeout=6) as response:
            data=json.load(response)
        if data.get('success'):
            return ', '.join(clean(data.get(k,'')) for k in ('city','country') if data.get(k))
    except (OSError, ValueError):
        pass
    print('Geolocation unavailable; enter location manually.')
    return ''

def ask(prompt, default=''):
    return clean(input(f'{prompt} [{default}]: ' if default else f'{prompt}: ')) or default

def select(title, options, defaults):
    options = list(dict.fromkeys(options))
    print('\n'+title)
    if not options:
        print('No items found.')
        return []
    for i,item in enumerate(options,1):
        print(f'{i:3}. [{"x" if item in defaults else " "}] {clean(item)}')
    while True:
        s=input('Numbers/ranges (1,3,5-8), all, none, Enter = checked: ').strip().lower()
        if not s: return [x for x in options if x in defaults]
        if s=='all': return options
        if s=='none': return []
        try:
            nums=set()
            for part in re.split(r'[,\s]+',s):
                if '-' in part:
                    a,b=map(int,part.split('-'))
                    if not 1<=a<=b<=len(options): raise ValueError
                    nums.update(range(a,b+1))
                else:
                    n=int(part)
                    if not 1<=n<=len(options): raise ValueError
                    nums.add(n)
            return [x for i,x in enumerate(options,1) if i in nums]
        except ValueError:
            print('Invalid selection. Try again.')

def services():
    files=run('systemctl','list-unit-files','--type=service','--no-legend','--no-pager',timeout=10)
    loaded=run('systemctl','list-units','--all','--type=service','--no-legend','--no-pager','--plain',timeout=10)
    result=set()
    for line in ((files or '')+'\n'+(loaded or '')).splitlines():
        fields=line.split()
        if fields and fields[0].endswith('.service') and not fields[0].endswith('@.service') and 'masked' not in fields and 'not-found' not in fields:
            result.add(fields[0])
    return sorted(result,key=lambda x:(x not in KNOWN,x))

def containers():
    result=run('docker','ps','-a','--format','{{json .}}',timeout=4)
    if result is None: return None
    try: return {d['Names']:d for d in map(json.loads,result.splitlines())}
    except (ValueError, KeyError): return None

def status(state):
    color=GREEN if state in ('running','active','ready','healthy') else YELLOW if state in ('activating','reloading','restarting','paused','created','not ready','starting','deactivating') else RED if state in ('exited','failed','dead','inactive','missing','unhealthy') else GRAY
    return color+'● '+clean(state).capitalize()+RESET

def unit_status(unit):
    out=run('systemctl','show',unit,'-p','LoadState','-p','ActiveState','-p','SubState')
    if out is None: return 'unavailable'
    d=dict(line.split('=',1) for line in out.splitlines() if '=' in line)
    if d.get('LoadState')=='not-found': return 'missing'
    if d.get('ActiveState')=='active' and d.get('SubState')=='running': return 'running'
    return d.get('ActiveState','unknown')

def width(settings):
    return max([12]+[len(label(x)) for x in settings['services']]+[len(x) for x in settings['containers']])

def fast_config(settings):
    w=width(settings)
    modules=[]
    for typ,key in [('os','System'),('kernel','Kernel'),('command','Uptime'),('cpu','CPU'),('cpuusage','Usage'),('loadavg','Load'),('memory','RAM'),('swap','Swap'),('disk','Disk')]:
        if typ=='cpu': modules.append('break')
        module={'type':typ,'key':key.ljust(w)}
        if typ=='command': module['text'] = "awk '{m=int($1/60); printf \"%dd %dh %dm\\n\", int(m/1440), int(m%1440/60), m%60}' /proc/uptime"
        modules.append(module)
    return {'logo':{'type':'none'},'display':{'separator':' │ ','color':{'keys':'38;2;73;221;147'},'percent':{'type':3},'bar':{'width':12}},'modules':modules}

def render(settings,config_path):
    w=width(settings)
    def row(k,v): print(f'{GREEN}\033[1m{clean(k):<{w}}{RESET} │ {v}',flush=True)
    def section(s): print(f'\n{GREEN}{s}{RESET}\n{GRAY}'+('─'*max(44,w+20))+RESET,flush=True)
    print(GREEN+LOGO+RESET+'\n',flush=True)
    for k in ('location','provider','role'): row(k.capitalize(),clean(settings[k]) or 'Unknown')
    print(flush=True)
    # Use Fastfetch for system and resource information.
    try:
        subprocess.run(['fastfetch','--config',str(config_path)],timeout=8,check=False)
    except (OSError,subprocess.TimeoutExpired): print('Fastfetch unavailable')
    section('NETWORK')
    iface,addresses=network()
    if iface:
        row('Interface',clean(iface))
        for family,name in [('inet','IPv4'),('inet6','IPv6')]:
            values=[f"{a['local']}/{a['prefixlen']}" for a in addresses if a.get('family')==family]
            if values: row(name,', '.join(values))
        q=run('tc','qdisc','show','dev',iface)
        row('Qdisc',' / '.join(dict.fromkeys(l.split()[1] for l in (q or '').splitlines() if l.startswith('qdisc '))) or 'Unknown')
    row('Congestion',clean(run('sysctl','-n','net.ipv4.tcp_congestion_control') or 'Unknown'))
    if 'netbird.service' in settings['services']:
        ready=run('netbird','status','--check','ready') is not None
        row('NetBird',status('ready' if ready else 'not ready'))
        if ready:
            mesh=run('netbird','status','--ipv4')
            if mesh: row('Mesh IP',clean(mesh))
    section('SERVICES')
    for unit in settings['services']: row(label(unit),status(unit_status(unit)))
    failed=run('systemctl','--failed','--type=service','--no-legend','--plain','--no-pager')
    count=len(failed.splitlines()) if failed is not None else None
    row('Failed units', (GREEN if count==0 else RED)+str(count)+RESET if count is not None else 'Unknown')
    if settings['containers']:
        section('CONTAINERS')
        found=containers()
        for name in settings['containers']:
            if found is None: row(name,status('unavailable')); continue
            d=found.get(name)
            if d is None: row(name,status('missing')); continue
            state=d.get('State','unknown'); details=clean(d.get('Status',''))
            if '(unhealthy)' in details: state='unhealthy'
            elif '(health: starting)' in details: state='starting'
            row(name,status(state)+(GRAY+' · '+details+RESET if details else ''))
    print()

def write(path,data,mode):
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.flowvy-')
    try:
        with os.fdopen(fd,'w') as f: f.write(data)
        os.chmod(tmp,mode); os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def require_admin():
    if os.geteuid()!=0 or not sys.stdin.isatty():
        raise SystemExit('Run as root in an interactive terminal.')

def check_commands():
    for p, content in COMMANDS.items():
        if p.is_symlink() or (p.exists() and (not p.is_file() or p.read_text(errors='replace') != content)):
            raise SystemExit(f'Refusing to overwrite existing command: {p}')

def install():
    require_admin()
    check_commands()
    if not shutil.which('fastfetch'):
        if input('Install Fastfetch from APT? [y/N]: ').strip().lower()!='y': return
        subprocess.run(['apt-get','update'],check=True)
        subprocess.run(['apt-get','install','-y','fastfetch'],check=True)
    old={}
    if (BASE/'settings.json').exists(): old=json.loads((BASE/'settings.json').read_text())
    print('\nFlowvy MOTD installer\n')
    provider=ask('Provider',old.get('provider',''))
    role=ask('Role',old.get('role',''))
    location=old.get('location','')
    if not location or input(f'Look up location again ({location})? [y/N]: ').strip().lower()=='y':
        location=locate(network()[1]) or location
    location=ask('Location (city, country)',location)
    units=services(); previous=old.get('services',[x for x in KNOWN if x in units])
    units=list(dict.fromkeys(units+previous))
    selected=select('SYSTEMD SERVICES',units,previous)
    found=containers(); names=sorted(found or {})
    previous=old.get('containers',names)
    if found is None and shutil.which('docker'): print('Docker unavailable; previously selected names are preserved as options.')
    names=list(dict.fromkeys(names+previous))
    chosen=select('DOCKER CONTAINERS',names,previous)
    settings={'provider':provider,'role':role,'location':location,'services':selected,'containers':chosen}
    cfg=json.dumps(fast_config(settings),ensure_ascii=False,indent=2)+'\n'
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'config.jsonc'; path.write_text(cfg)
        render(settings,path)
    if input('Install this MOTD? [y/N]: ').strip().lower() not in ('y','yes'): print('Cancelled. Existing MOTD unchanged.'); return
    check_commands()
    BASE.mkdir(mode=0o755,exist_ok=True)
    stamp=datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup=BASE/'backups'/stamp; backup.mkdir(parents=True,mode=0o700)
    for p in (BASE/'settings.json',BASE/'config.jsonc',BASE/'motd.sh',BASE/'installer.py',HOOK):
        if p.is_file(): shutil.copy2(p,backup/('profile-hook.sh' if p==HOOK else p.name))
    write(BASE/'installer.py',Path(__file__).read_text(),0o755)
    write(BASE/'settings.json',json.dumps(settings,ensure_ascii=False,indent=2)+'\n',0o644)
    write(BASE/'config.jsonc',cfg,0o644)
    write(BASE/'motd.sh','#!/bin/sh\nexec /usr/bin/python3 /etc/flowvy-motd/installer.py --show\n',0o755)
    write(HOOK,'''if [ -n "${SSH_CONNECTION:-}" ] && [ -t 0 ] && [ -t 1 ]; then
    case "$-" in
        *i*)
            if [ "${FLOWVY_MOTD_SHOWN:-}" != 1 ]; then
                export FLOWVY_MOTD_SHOWN=1
                /etc/flowvy-motd/motd.sh || :
            fi
            ;;
    esac
fi
''',0o644)
    for p, content in COMMANDS.items():
        write(p,content,0o755)
    if not HUSH.exists() and not HUSH.is_symlink() and input('Hide default Debian greeting and last-login message for root? [y/N]: ').strip().lower() in ('y','yes'):
        HUSH.touch(mode=0o600,exist_ok=False)
        write(HUSH_MARKER,str(HUSH.stat().st_ino),0o600)
    print(f'Installed. Backup: {backup}\nConfigure: motd-config\nPreview: motd')

def show():
    render(json.loads((BASE/'settings.json').read_text()),BASE/'config.jsonc')

def uninstall():
    require_admin()
    print('This removes Flowvy MOTD, its settings, backups, SSH greeting and commands. System packages and services are retained.')
    if input('Type REMOVE to confirm: ').strip() != 'REMOVE':
        print('Cancelled.')
        return False
    for p,content in COMMANDS.items():
        if not p.is_symlink() and p.is_file() and p.read_text(errors='replace') == content:
            p.unlink()
    if not HOOK.is_symlink() and HOOK.is_file() and '/etc/flowvy-motd/motd.sh' in HOOK.read_text():
        HOOK.unlink()
    if HUSH_MARKER.is_file() and not HUSH.is_symlink() and HUSH.is_file() and HUSH.stat().st_size == 0 and str(HUSH.stat().st_ino) == HUSH_MARKER.read_text().strip():
        HUSH.unlink()
    elif HUSH.exists():
        print('Pre-existing .hushlogin retained. Remove it manually if you want the default greeting restored.')
    shutil.rmtree(BASE)
    print('Flowvy MOTD removed.')
    return True

def configure():
    require_admin()
    while True:
        print('\nFlowvy MOTD configuration\n1. Edit settings\n2. Preview\n3. Uninstall\n0. Exit')
        choice=input('Select: ').strip()
        if choice=='1': install()
        elif choice=='2': show()
        elif choice=='3':
            if uninstall(): return
        elif choice=='0': return
        else: print('Invalid selection.')

if __name__=='__main__':
    try:
        if sys.argv[1:]==['--show']:
            show()
        elif sys.argv[1:]==['--config']: configure()
        elif not sys.argv[1:]: install()
        else: raise SystemExit('Usage: installer.py [--show|--config]')
    except (KeyboardInterrupt,EOFError): raise SystemExit('\nCancelled.')
