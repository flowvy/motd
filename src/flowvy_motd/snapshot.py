"""Read local metrics and selected checks within the login time budget."""

from concurrent.futures import ThreadPoolExecutor, wait
import http.client
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import time

from . import inventory
from .settings import probe_target

LOGIN_BUDGET = 0.62
NETBIRD_CONNECTION = object()


def local_command(args, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, errors='replace', timeout=remaining,
                                env={**os.environ, 'LC_ALL': 'C'})
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def read_file(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ''


def uptime(seconds):
    days, hours = divmod(int(seconds) // 3600, 24)
    minutes = (int(seconds) // 60) % 60
    return ' '.join(value for value, amount in (
        (f'{days}d', days), (f'{hours}h', hours), (f'{minutes}m', minutes)
    ) if amount) or '<1m'


def native_metrics():
    fields = {'host': socket.gethostname(), 'kernel': f'Linux {os.uname().release}'}
    try:
        fields['os'] = platform.freedesktop_os_release()['PRETTY_NAME']
    except (OSError, KeyError):
        fields['os'] = 'Linux'
    seconds = read_file('/proc/uptime').split()
    if seconds:
        fields['uptime'] = uptime(float(seconds[0]))
    fields['load'] = ' / '.join(f'{value:.2f}' for value in os.getloadavg())

    mem = {}
    for line in read_file('/proc/meminfo').splitlines():
        key, _, value = line.partition(':')
        digits = value.split()
        if digits and digits[0].isdigit():
            mem[key] = int(digits[0]) * 1024
    if 'MemTotal' in mem:
        fields['memory'] = (mem['MemTotal'] - mem.get('MemAvailable', mem.get('MemFree', 0)), mem['MemTotal'])
    if 'SwapTotal' in mem:
        fields['swap'] = (mem['SwapTotal'] - mem.get('SwapFree', 0), mem['SwapTotal'])
    try:
        disk = shutil.disk_usage('/')
        fields['disk'] = disk.used, disk.total
    except OSError:
        pass
    cores = len(os.sched_getaffinity(0))
    cpu = next((line.partition(':')[2].strip() for line in read_file('/proc/cpuinfo').splitlines()
                if line.startswith(('model name', 'Hardware')) and ':' in line), '')
    fields['cpu'] = ' · '.join(filter(None, (cpu, f'{cores} logical CPUs')))
    return fields


def fast_metrics(deadline):
    data = local_command(
        ['fastfetch', '--config', 'none', '--format', 'json', '--structure', 'CPU:CPUUsage'],
        min(deadline, time.monotonic() + 0.31),
    )
    try:
        results = {item['type']: item.get('result') for item in json.loads(data or '[]')}
        cpu = results.get('CPU') or {}
        usage = results.get('CPUUsage') or []
        name = cpu.get('cpu')
        logical = cpu.get('cores', {}).get('logical')
        percent = f'{sum(usage) / len(usage):.0f}%' if usage else ''
        if name and logical:
            return ' · '.join(filter(None, (name, f'{logical} logical CPUs', percent)))
    except (TypeError, ValueError, KeyError, AttributeError):
        pass
    return ''


def network_metrics(deadline):
    result = {}
    routes = local_command(['ip', '-j', 'route', 'show', 'default'], deadline)
    try:
        routes = json.loads(routes or '[]')
        if not routes:
            routes = json.loads(local_command(['ip', '-j', '-6', 'route', 'show', 'default'], deadline) or '[]')
        device = min(routes, key=lambda route: route.get('metric', 0))['dev']
        output = local_command(['ip', '-j', 'addr', 'show', 'dev', device], deadline)
        addresses = [addr for link in json.loads(output or '[]') for addr in link.get('addr_info', [])
                     if addr.get('scope') == 'global']
        result['Interface'] = device
        for family, label in (('inet', 'IPv4'), ('inet6', 'IPv6')):
            items = [f'{addr["local"]}/{addr["prefixlen"]}' for addr in addresses if addr.get('family') == family]
            if items:
                result[label] = ', '.join(items)
        qdiscs = local_command(['tc', 'qdisc', 'show', 'dev', device], deadline) or ''
        kinds = list(dict.fromkeys(line.split()[1] for line in qdiscs.splitlines()
                                   if line.startswith('qdisc ') and len(line.split()) >= 2))
        tcp = read_file('/proc/sys/net/ipv4/tcp_congestion_control')
        values = [value for value in (tcp, f'qdisc {"/".join(kinds)}' if kinds else '') if value]
        if values:
            result['TCP'] = ' · '.join(values)
    except (TypeError, ValueError, KeyError):
        pass
    return result


def http_check(url, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 'unknown', 'Local HTTP check: time budget exhausted'
    host, port, resource = probe_target(url)
    connection = http.client.HTTPConnection(host, port, timeout=min(remaining, 0.27))
    try:
        connection.request('GET', resource)
        response = connection.getresponse()
        status = response.status
        response.close()
        return ('ok' if 200 <= status < 300 else 'failed'), f'Local HTTP check: HTTP {status}'
    except TimeoutError:
        return 'failed', 'Local HTTP check: timed out'
    except ConnectionRefusedError:
        return 'failed', 'Local HTTP check: connection refused'
    except (OSError, http.client.HTTPException):
        return 'unknown', 'Local HTTP check: could not read response'
    finally:
        connection.close()


def snapshot(config, timeout=LOGIN_BUDGET):
    deadline = time.monotonic() + timeout
    applications = config['applications']
    ids = [f'unit:{unit}' for unit in config['services']] + config['containers']
    ids.extend(member for group in applications for member in group['members'])
    ids = list(dict.fromkeys(ids))
    with_budget = max(0.01, deadline - time.monotonic())

    workers = ThreadPoolExecutor(max_workers=6)
    component_task = workers.submit(inventory.collect, ids, timeout=with_budget)
    network_task = workers.submit(network_metrics, deadline)
    fast_task = workers.submit(fast_metrics, deadline)
    checks = {group['name']: workers.submit(http_check, group['check_url'], deadline)
              for group in applications if group['check_url']}
    if 'unit:netbird.service' in ids:
        checks[NETBIRD_CONNECTION] = workers.submit(inventory.read_netbird, min(with_budget, 0.25))
    metrics = native_metrics()
    wait([component_task, network_task, fast_task, *checks.values()],
         timeout=max(0, deadline - time.monotonic()))
    workers.shutdown(wait=False, cancel_futures=True)

    def result(future, fallback):
        if future.done() and not future.cancelled():
            try:
                return future.result()
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        return fallback

    cpu = result(fast_task, '')
    if cpu:
        metrics['cpu'] = cpu
    components = result(component_task, {})
    for item in ids:
        if item not in components:
            components[item] = inventory.Component(item, item.partition(':')[2], state='unavailable',
                                                   detail='Check did not finish in time')
    probes = {name: result(task, ('unknown', 'Check did not finish in time')) for name, task in checks.items()}
    return metrics, result(network_task, {}), components, probes
