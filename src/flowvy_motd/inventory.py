"""Read systemd units and local Docker containers without acting on them."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import ipaddress
import json
import os
from pathlib import PurePosixPath
import re
import subprocess
import time


@dataclass(frozen=True)
class Component:
    id: str
    name: str
    description: str = ""
    group: str = ""
    source: str = ""
    state: str = "unknown"
    detail: str = ""
    schedule: str = ""
    group_kind: str = ""
    search_text: str = ""
    exit_code: int | None = None


@dataclass(frozen=True)
class _Container:
    id: str
    name: str
    image: str
    project: str
    service: str
    one_off: bool = False

    @property
    def selection(self):
        if self.project and self.service and not self.one_off:
            return f"compose:{self.project}/{self.service}"
        return f"container:{self.name}"


_UNIT_FIELDS = (
    "Id", "Names", "Description", "Documentation", "LoadState", "ActiveState",
    "SubState", "Type", "Result", "TriggeredBy", "ExecMainExitTimestampMonotonic",
    "ExecMainStatus", "ConditionResult", "ConditionTimestampMonotonic", "FragmentPath",
    "SourcePath", "ExecStart",
)
_DOCKER_LIST_FORMAT = (
    '[{{json .ID}},{{json .Names}},{{json .Image}},'
    '{{json (.Label "com.docker.compose.project")}},'
    '{{json (.Label "com.docker.compose.service")}},'
    '{{json (.Label "com.docker.compose.oneoff")}}]'
)
_DOCKER_INSPECT_FORMAT = (
    '{{- $health := index .State "Health" -}}'
    '[{{json .Id}},{"Status":{{json .State.Status}},'
    '"ExitCode":{{json .State.ExitCode}},"OOMKilled":{{json .State.OOMKilled}},'
    '"Health":{{if $health}}{"Status":{{json $health.Status}}}{{else}}null{{end}}}]'
)
_EXECUTABLE = re.compile(r"(?:^|[\s{])path=([^\s;]+)\s*;")
_WRAPPER = re.compile(
    r"(?:ba|da|k|z)?sh|env|sudo|su|chroot|runuser|timeout|setsid|flock|"
    r"python(?:[0-9.]+)?|perl|ruby|node|java|busybox|docker|podman|systemd-run"
)


def _command(args, deadline, env=None):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return subprocess.CompletedProcess(args, 124, "", "")
    try:
        return subprocess.run(
            args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=remaining,
            env={**(os.environ if env is None else env), "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "")
    except OSError:
        return subprocess.CompletedProcess(args, 127, "", "")


def _error(result, source):
    if result.returncode == 124:
        return f"{source} check timed out"
    if result.returncode == 127:
        return f"{source} command unavailable"
    stderr = result.stderr.lower()
    if "permission denied" in stderr or "access denied" in stderr:
        return f"{source} access denied"
    return f"{source} unavailable"


def _property_blocks(output):
    blocks = {}
    for entry in re.split(r"\n\s*\n", output.strip()):
        properties = dict(line.split("=", 1) for line in entry.splitlines() if "=" in line)
        if properties.get("Id"):
            for name in [properties["Id"], *properties.get("Names", "").split()]:
                blocks[name] = properties
    return blocks


def _systemd_query(names, deadline):
    blocks = {}
    error = ""
    for start in range(0, len(names), 128):
        result = _command(
            ["systemctl", "show", "--all", "--no-pager", "-p", ",".join(_UNIT_FIELDS),
             "--", *names[start:start + 128]],
            deadline,
        )
        blocks.update(_property_blocks(result.stdout))
        if result.returncode:
            error = _error(result, "systemd")
            if result.returncode in (124, 127):
                break
    return blocks, error


def _triggers(unit):
    return tuple(
        name for name in unit.get("TriggeredBy", "").split()
        if name.endswith((".timer", ".socket", ".path"))
    )


def _has_execution(unit):
    return _has_timestamp(unit, "ExecMainExitTimestampMonotonic")


def _has_timestamp(unit, key):
    stamp = unit.get(key, "")
    return stamp.isdecimal() and int(stamp) > 0


def _failed_unit_detail(unit):
    result = unit.get("Result", "")
    code = unit.get("ExecMainStatus", "")
    if result == "exit-code" and code.isdecimal() and int(code):
        return f"Exit code {code}"
    return {
        "exit-code": "Process exited with an error",
        "timeout": "Operation timed out",
        "signal": "Terminated by a signal",
        "oom-kill": "Out of memory",
        "watchdog": "Watchdog timed out",
        "start-limit-hit": "Start limit reached",
        "dependency": "Dependency failed",
    }.get(result, "")


def _unit_state(unit, schedule, triggers, trigger_properties):
    load = unit.get("LoadState")
    active = unit.get("ActiveState")
    substate = unit.get("SubState")
    result = unit.get("Result")
    known_success = result == "success" and _has_execution(unit)
    if load == "not-found":
        return "missing", "Unit not found"
    if load in ("bad-setting", "error"):
        return "failed", "Could not load unit"
    condition_not_met = unit.get("ConditionResult") == "no" and _has_timestamp(unit, "ConditionTimestampMonotonic")
    if active == "inactive" and (result == "exec-condition" or condition_not_met):
        return "waiting", "Start condition not met"
    if active == "failed" or (active == "inactive" and result and result != "success"):
        return "failed", _failed_unit_detail(unit)
    if active in ("activating", "reloading", "refreshing"):
        return "starting", "Restarting" if substate == "auto-restart" else ""
    if active == "deactivating":
        return "stopping", ""
    if active == "active":
        if substate == "exited" and unit.get("Type") == "oneshot":
            return "completed", ""
        return "running", "" if substate == "running" else "Active"
    if active != "inactive":
        return "unknown", ""
    if load == "masked":
        return "stopped", "Unit masked"
    if triggers:
        states = [trigger_properties.get(name, {}) for name in triggers]
        if any(state.get("ActiveState") == "active" and state.get("SubState") != "elapsed" for state in states):
            return "waiting", "Last run succeeded" if known_success else "Waiting for trigger"
        if any(state.get("ActiveState") == "failed" for state in states):
            return "failed", "Trigger failed"
        if any(state.get("SubState") == "elapsed" for state in states):
            return ("completed", "") if known_success else ("unknown", "Trigger elapsed; no recorded run")
        if all(state.get("ActiveState") == "inactive" for state in states):
            return "stopped", "Trigger stopped"
        return "unknown", "Trigger state unavailable"
    if schedule == "one-shot":
        return ("completed", "") if known_success else ("waiting", "No recorded run")
    return "stopped", ""


def _unit_component(name, unit, trigger_properties):
    triggers = _triggers(unit)
    schedule = ""
    if any(trigger.endswith(".timer") for trigger in triggers):
        schedule = "scheduled"
    elif triggers:
        schedule = "on-demand"
    elif unit.get("Type") == "oneshot":
        schedule = "one-shot"
    state, detail = _unit_state(unit, schedule, triggers, trigger_properties)
    return Component(
        f"unit:{name}", name, unit.get("Description", ""), source="systemd",
        state=state, detail=detail, schedule=schedule,
        search_text=" ".join((unit.get("Names", ""), unit.get("Documentation", ""))),
    )


def _unread(selector, state, detail=""):
    kind, _, identifier = selector.partition(":")
    name = identifier.rsplit("/", 1)[-1]
    group = identifier.rsplit("/", 1)[0] if kind == "compose" else ""
    return Component(
        selector, name, group=group, source="systemd" if kind == "unit" else "docker",
        state=state, detail=detail, group_kind="compose" if group else "",
    )


def _trigger_properties(units, deadline):
    triggers = sorted({trigger for unit in units.values() for trigger in _triggers(unit)})
    return _systemd_query(triggers, deadline)[0] if triggers else {}


def _collect_systemd(ids, deadline):
    names = [selector.removeprefix("unit:") for selector in ids]
    units, error = _systemd_query(names, deadline)
    triggers = _trigger_properties(units, deadline)
    return {
        selector: _unit_component(name, units[name], triggers)
        if name in units else _unread(selector, "unavailable" if error else "unknown", error)
        for selector, name in zip(ids, names)
    }


def _executable(unit):
    paths = {path for path in _EXECUTABLE.findall(unit.get("ExecStart", "")) if path.startswith("/")}
    if len(paths) != 1:
        return ""
    path = next(iter(paths))
    return "" if _WRAPPER.fullmatch(PurePosixPath(path).name) else path


def _package_owners(paths, deadline):
    def variants(path):
        if path.startswith(("/bin/", "/sbin/", "/lib/")):
            return path, f"/usr{path}"
        if path.startswith(("/usr/bin/", "/usr/sbin/", "/usr/lib/")):
            return path, path.removeprefix("/usr")
        return (path,)

    lookup = {}
    for path in paths:
        if path:
            for variant in variants(path):
                lookup.setdefault(variant, set()).add(path)
    candidates = {}
    paths = sorted(lookup)
    for start in range(0, len(paths), 128):
        result = _command(["dpkg-query", "--search", "--", *paths[start:start + 128]], deadline)
        for line in result.stdout.splitlines():
            package, separator, path = line.partition(": ")
            if separator and path in lookup and re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?", package):
                for original in lookup[path]:
                    candidates.setdefault(original, set()).add(package)
        if result.returncode in (124, 127):
            break
    return {path: next(iter(packages)) for path, packages in candidates.items() if len(packages) == 1}


def _discover_systemd(deadline):
    names = set()
    for listing in (
        ["list-unit-files"], ["list-units", "--all", "--plain"],
    ):
        result = _command(
            ["systemctl", *listing, "--type=service", "--no-legend", "--no-pager"], deadline
        )
        for line in result.stdout.splitlines():
            fields = line.split()
            if fields and fields[0].endswith(".service") and not fields[0].endswith("@.service"):
                if len(fields) < 2 or fields[1] != "alias":
                    names.add(fields[0])
    metadata, _ = _systemd_query(sorted(names), deadline)
    units = {metadata.get(name, {}).get("Id", name): metadata.get(name, {}) for name in names}
    triggers = _trigger_properties(units, deadline)
    paths = {name: (unit.get("FragmentPath", ""), unit.get("SourcePath", ""), _executable(unit))
             for name, unit in units.items()}
    owners = _package_owners({path for choices in paths.values() for path in choices}, deadline)
    found = []
    for name, unit in units.items():
        if unit.get("LoadState") == "not-found":
            continue
        component = _unit_component(name, unit, triggers)
        for path, kind in zip(paths[name], ("package", "package", "executable")):
            if path in owners:
                component = replace(component, group=owners[path], group_kind=kind)
                break
        found.append(component)
    return found


def _docker(args, deadline):
    # Use a local Unix socket even when the interactive Docker context is remote.
    env = {key: value for key, value in os.environ.items() if key not in ("DOCKER_HOST", "DOCKER_CONTEXT")}
    host = os.environ.get("DOCKER_HOST", "")
    if not host.startswith("unix:///"):
        runtime = os.environ.get("XDG_RUNTIME_DIR", "")
        user_socket = os.path.join(runtime, "docker.sock") if runtime.startswith("/") else ""
        host = f"unix://{user_socket}" if user_socket and os.path.exists(user_socket) else "unix:///var/run/docker.sock"
    return _command(["docker", "--host", host, *args], deadline, env)


def _json_lines(output):
    for line in output.splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _docker_list(deadline):
    result = _docker(["container", "ls", "--all", "--no-trunc", "--format", _DOCKER_LIST_FORMAT], deadline)
    if result.returncode:
        return [], _error(result, "Docker")
    rows = []
    for item in _json_lines(result.stdout):
        if not isinstance(item, list) or len(item) != 6 or not all(isinstance(value, str) for value in item):
            continue
        identifier, name, image, project, service, one_off = item
        if identifier and name:
            rows.append(_Container(identifier, name, image, project, service, one_off.lower() == "true"))
    return rows, ""


def _container_state(state, one_shot):
    status = state.get("Status")
    health = (state.get("Health") or {}).get("Status")
    if status == "paused":
        return "paused", ""
    if status == "created":
        return ("waiting", "Waiting to run") if one_shot else ("stopped", "Not started")
    if status == "restarting" or (status == "running" and health == "starting"):
        detail = "Restarting" if status == "restarting" else ""
        return "starting", detail
    if status == "running":
        if health == "unhealthy":
            return "failed", "Health check failed"
        return ("healthy", "") if health == "healthy" else ("running", "")
    if status == "exited":
        if state.get("OOMKilled"):
            return "failed", "Out of memory"
        code = state.get("ExitCode")
        if type(code) is not int:
            return "stopped", ""
        if code != 0:
            return "failed", f"Exited with code {code}"
        return ("completed" if one_shot else "stopped"), "Exited with code 0"
    if status == "dead":
        return "failed", "Container is dead"
    if status == "removing":
        return "stopping", ""
    return "unknown", ""


def _container_component(selector, rows, inspected):
    first = rows[0]
    grouped = selector.startswith("compose:")
    images = ", ".join(sorted({row.image for row in rows if row.image}))
    component = Component(
        selector, first.service if grouped else first.name, images,
        group=first.project, source="docker", schedule="one-shot" if first.one_off else "",
        group_kind="compose" if first.project else "",
        search_text=" ".join([images, *(row.name for row in rows)]),
    )
    state_rank = (
        "failed", "unavailable", "unknown", "stopped", "paused", "stopping",
        "starting", "waiting", "completed", "running", "healthy",
    )
    statuses = [
        _container_state(inspected[row.id], row.one_off)
        if row.id in inspected else ("unavailable", "Container state unavailable")
        for row in rows
    ]
    state, detail = min(statuses, key=lambda value: state_rank.index(value[0]))
    exit_code = None
    if len(rows) == 1 and inspected.get(first.id, {}).get("Status") == "exited":
        code = inspected[first.id].get("ExitCode")
        exit_code = code if type(code) is int else None
    if len(rows) > 1:
        running = sum(current in ("running", "healthy") for current, _ in statuses)
        replicas = f"{running}/{len(rows)} running"
        detail = f"{replicas}; {detail}" if detail else replicas
    return replace(component, state=state, detail=detail, exit_code=exit_code)


def _container_selections(rows):
    groups = {}
    for row in rows:
        groups.setdefault(row.selection, []).append(row)
    return groups


def _inspect_docker(container_ids, deadline):
    inspected = {}
    for start in range(0, len(container_ids), 128):
        result = _docker(
            ["container", "inspect", "--format", _DOCKER_INSPECT_FORMAT, "--", *container_ids[start:start + 128]],
            deadline,
        )
        for item in _json_lines(result.stdout):
            if isinstance(item, list) and len(item) == 2 and isinstance(item[0], str) and isinstance(item[1], dict):
                inspected[item[0]] = item[1]
        if result.returncode in (124, 127):
            break
    return inspected


def _discover_docker(deadline):
    rows, _ = _docker_list(deadline)
    inspected = _inspect_docker([row.id for row in rows], deadline)
    return [_container_component(selector, replicas, inspected)
            for selector, replicas in _container_selections(rows).items()]


def _collect_docker(ids, deadline):
    rows, error = _docker_list(deadline)
    if error:
        return {selector: _unread(selector, "unavailable", error) for selector in ids}
    groups = _container_selections(rows)
    by_name = {f"container:{row.name}": [row] for row in rows}
    selected = {selector: groups.get(selector, by_name.get(selector, [])) for selector in ids}
    container_ids = sorted({row.id for replicas in selected.values() for row in replicas})
    inspected = _inspect_docker(container_ids, deadline)
    return {
        selector: _container_component(selector, replicas, inspected)
        if replicas else _unread(selector, "missing", "Container not found")
        for selector, replicas in selected.items()
    }


def discover():
    """Find local units and containers for the settings picker."""
    deadline = time.monotonic() + 4.0
    with ThreadPoolExecutor(max_workers=2) as threads:
        systemd = threads.submit(_discover_systemd, deadline)
        docker = threads.submit(_discover_docker, deadline)
        components = systemd.result() + docker.result()
    return sorted(components, key=lambda item: (item.source, item.group, item.name))


def submit_collection(executor, ids, deadline):
    """Submit independent providers so callers can keep results as they finish."""
    selectors = list(dict.fromkeys(ids))
    unit_ids = [item for item in selectors if item.startswith("unit:")]
    docker_ids = [item for item in selectors if item.startswith(("compose:", "container:"))]
    work = []
    if unit_ids:
        work.append(executor.submit(_collect_systemd, unit_ids, deadline))
    if docker_ids:
        work.append(executor.submit(_collect_docker, docker_ids, deadline))
    return work


def collect(ids, *, timeout=0.65):
    """Read selected component states in a single shared time budget."""
    deadline = time.monotonic() + timeout
    found = {}
    with ThreadPoolExecutor(max_workers=2) as threads:
        for future in submit_collection(threads, ids, deadline):
            found.update(future.result())
    return found


def read_netbird(timeout=0.4):
    """Read the local NetBird daemon's connection state, not its readiness."""
    result = _command(
        ["netbird", "status", "--daemon-addr", "unix:///var/run/netbird.sock", "--json"],
        time.monotonic() + timeout,
    )
    if result.returncode:
        return "unavailable", _error(result, "NetBird")
    try:
        data = json.loads(result.stdout)
        status = str(data.get("daemonStatus", "")).lower()
        management = (data.get("management") or {}).get("connected")
        signal = (data.get("signal") or {}).get("connected")
    except (ValueError, AttributeError):
        return "unavailable", "Unexpected daemon response"
    if status == "connecting":
        return "connecting", ""
    if status in ("needslogin", "loginfailed", "sessionexpired"):
        return "disconnected", "Authentication required"
    if status == "idle":
        return "disconnected", "Daemon is idle"
    if management is True and signal is True:
        address = data.get("netbirdIp") or data.get("netbirdIpv6") or ""
        try:
            address = str(ipaddress.ip_interface(address).ip)
        except ValueError:
            address = ""
        return "connected", address
    if management is False or signal is False:
        return "disconnected", "Management or signaling is disconnected"
    return "unavailable", "Connection state unavailable"
