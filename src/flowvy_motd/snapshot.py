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

# Leave time for startup and rendering within the SSH hook's two-second cap.
LOGIN_BUDGET = 1.7
CPU_SAMPLE_INTERVAL = 0.15
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
    cpu_label = 'CPU' if cores == 1 else 'CPUs'
    fields['cpu'] = ' · '.join(filter(None, (cpu, f'{cores} logical {cpu_label}')))
    return fields


def cpu_times(cpus):
    totals = [0] * 8
    seen = set()
    for row in read_file('/proc/stat').splitlines():
        fields = row.split()
        if not fields or fields[0] not in cpus:
            continue
        if len(fields) < 9:
            return None
        # guest and guest_nice are already counted in user and nice.
        totals = [total + int(field) for total, field in zip(totals, fields[1:9])]
        seen.add(fields[0])
    return totals if seen == cpus else None


def cpu_usage(deadline):
    if deadline - time.monotonic() < CPU_SAMPLE_INTERVAL:
        return None
    cpus = {f'cpu{cpu}' for cpu in os.sched_getaffinity(0)}
    before = cpu_times(cpus)
    if before is None:
        return None
    time.sleep(CPU_SAMPLE_INTERVAL)
    after = cpu_times(cpus)
    if after is None:
        return None
    deltas = [end - start for start, end in zip(before, after)]
    elapsed = sum(deltas)
    if elapsed <= 0 or min(deltas) < 0:
        return None
    busy = elapsed - deltas[3] - deltas[4] - deltas[7]
    return round(100 * busy / elapsed)


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
    # Allow command timeouts to be converted into results before the snapshot ends.
    command_deadline = deadline - min(0.1, timeout / 5)
    applications = config['applications']
    ids = [f'unit:{unit}' for unit in config['services']] + config['containers']
    ids.extend(member for group in applications for member in group['members'])
    ids = list(dict.fromkeys(ids))

    workers = ThreadPoolExecutor(max_workers=6)
    component_tasks = inventory.submit_collection(workers, ids, command_deadline)
    network_task = workers.submit(network_metrics, command_deadline)
    cpu_task = workers.submit(cpu_usage, command_deadline)
    checks = {}
    if 'unit:netbird.service' in ids:
        checks[NETBIRD_CONNECTION] = workers.submit(
            inventory.read_netbird, max(0, command_deadline - time.monotonic()))
    checks.update({group['name']: workers.submit(http_check, group['check_url'], command_deadline)
                   for group in applications if group['check_url']})
    metrics = native_metrics()
    wait([*component_tasks, network_task, cpu_task, *checks.values()],
         timeout=max(0, deadline - time.monotonic()))
    workers.shutdown(wait=False, cancel_futures=True)

    def result(future, fallback):
        if future.done() and not future.cancelled():
            try:
                return future.result()
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        return fallback

    cpu = result(cpu_task, None)
    if cpu is not None:
        metrics['cpu'] += f' · {cpu}%'
    components = {}
    for task in component_tasks:
        components.update(result(task, {}))
    for item in ids:
        if item not in components:
            components[item] = inventory.Component(item, item.partition(':')[2], state='unavailable',
                                                   detail='Check did not finish in time')
    probes = {name: result(task, ('unknown', 'Check did not finish in time')) for name, task in checks.items()}
    return metrics, result(network_task, {}), components, probes
