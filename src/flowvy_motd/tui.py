"""Interactive terminal screens for Flowvy MOTD."""

from collections.abc import Callable, Iterable
from copy import deepcopy
import curses
import os
import re
import sys
import textwrap
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .inventory import Component


Row = tuple[str, str]
Option = tuple[str, str]
_ENTER = ("\n", "\r", curses.KEY_ENTER)
_BACKSPACE = ("\b", "\x7f", curses.KEY_BACKSPACE)
_SOURCES = ("systemd", "docker")
_GROUP_KIND = {"package": "package", "compose": "Compose"}
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_STATE_LABELS = {
    "running": "Running",
    "healthy": "Healthy",
    "failed": "Failed",
    "stopped": "Stopped",
    "waiting": "Waiting",
    "completed": "Completed",
    "starting": "Starting",
    "stopping": "Stopping",
    "paused": "Paused",
    "unavailable": "Unavailable",
    "missing": "Not found",
    "unknown": "Unknown",
}


def _text(value):
    return "".join(c for c in str(value) if c.isprintable())


class _Terminal:
    def __init__(self, window):
        self.window = window
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.styles = {
            "title": curses.A_BOLD,
            "section": curses.A_BOLD,
            "focus": curses.A_REVERSE,
            "plain": curses.A_NORMAL,
            "muted": curses.A_DIM,
            "danger": curses.A_BOLD,
        }
        if curses.has_colors() and not os.environ.get("NO_COLOR"):
            try:
                curses.use_default_colors()
                curses.init_pair(1, curses.COLOR_GREEN, -1)
                curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_GREEN)
                curses.init_pair(3, curses.COLOR_RED, -1)
                self.styles["title"] = curses.color_pair(1) | curses.A_BOLD
                self.styles["section"] = curses.color_pair(1) | curses.A_BOLD
                self.styles["focus"] = curses.color_pair(2) | curses.A_BOLD
                self.styles["danger"] = curses.color_pair(3) | curses.A_BOLD
            except curses.error:
                pass

    def size(self):
        return self.window.getmaxyx()

    def paint(self, title, subtitle, rows, index, help_text):
        height, width = self.size()
        self.window.erase()
        start_y = 2 if height >= 7 else 1
        subtitle = textwrap.shorten(subtitle, width=max(2, width - 2), placeholder="…")
        if subtitle and height >= 8:
            self._line(start_y, subtitle, "muted")
            start_y += 2
        visible = max(0, height - start_y - 2)
        start = min(max(0, index - visible + 1), max(0, len(rows) - visible))
        for offset, (content, style) in enumerate(rows[start:start + visible]):
            focused = start + offset == index
            label = ("› " if focused else "  ") + content
            self._line(start_y + offset, label, "focus" if focused else style)
        self._line(0, title, "title")
        if height >= 3:
            self._line(height - 2, help_text, "muted")
        self.window.refresh()

    def _line(self, y, value, style):
        height, width = self.size()
        if 0 <= y < height and width > 1:
            try:
                self.window.addnstr(y, 0, _text(value), width - 1, self.styles[style])
            except curses.error:
                pass

    def key(self):
        return self.window.get_wch()


def _run(action):
    if sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM") not in ("", "dumb", None):
        try:
            return curses.wrapper(lambda window: action(_Terminal(window)))
        except curses.error:
            pass
        except KeyboardInterrupt:
            return None
    try:
        return action(None)
    except (EOFError, KeyboardInterrupt):
        return None


def _plain_menu(options, title, subtitle=""):
    print(f"\n{title}")
    if subtitle:
        print(subtitle)
    for i, (_, label) in enumerate(options, 1):
        print(f"  {i}. {label}")
    try:
        answer = input("Choose a number (Enter to go back): ").strip()
        if answer and answer.isascii() and answer.isdecimal():
            index = int(answer)
            if 1 <= index <= len(options):
                return options[index - 1][0]
    except (EOFError, KeyboardInterrupt, ValueError):
        pass
    return None


def _navigate(key, index, count):
    if key in (curses.KEY_UP, "k"):
        return (index - 1) % count
    if key in (curses.KEY_DOWN, "j"):
        return (index + 1) % count
    if key == curses.KEY_HOME:
        return 0
    if key == curses.KEY_END:
        return count - 1
    return index


def _menu(screen, options, title, subtitle=""):
    if not options:
        return None
    if screen is None:
        return _plain_menu(options, title, subtitle)
    index = 0
    rows = [(label, "danger" if key == "uninstall" else "plain") for key, label in options]
    while True:
        screen.paint(title, subtitle, rows, index, "↑ ↓ Move   Enter Select   Esc Back")
        key = screen.key()
        if key == "\x1b":
            return None
        if key in _ENTER:
            return options[index][0]
        index = _navigate(key, index, len(options))


def menu(options: list[Option], *, title: str, subtitle: str = "") -> str | None:
    return _run(lambda screen: _menu(screen, options, title, subtitle))


def _input(screen, title, value="", *, subtitle=""):
    if screen is None:
        print(f"\n{title}")
        if subtitle:
            print(subtitle)
        answer = input(f"[{value}] (use - to clear): ")
        return "" if answer == "-" else answer or value
    content = value
    while True:
        _, width = screen.size()
        tail = content[-max(1, width - 7):]
        screen.paint(title, subtitle, [(tail + "▌", "plain")], -1,
                     "Enter Accept   Esc Cancel   Ctrl-U Clear")
        key = screen.key()
        if key == "\x1b":
            return None
        if key in _ENTER:
            return content
        if key in _BACKSPACE:
            content = content[:-1]
        elif key == "\x15":
            content = ""
        elif isinstance(key, str) and key.isprintable() and len(content) < 512:
            content += key


def _show(screen, title, body):
    plain = _ANSI.sub("", body).expandtabs()
    if screen is None:
        print(f"\n{title}\n\n{plain.rstrip()}")
        if sys.stdin.isatty():
            input("\nPress Enter to go back. ")
        return True
    first = 0
    while True:
        height, width = screen.size()
        width = max(1, width - 3)
        rows = [(part, "plain") for line in plain.split("\n")
                for part in (textwrap.wrap(line, width, replace_whitespace=False,
                                           drop_whitespace=False) or [""])]
        visible = max(1, height - 4)
        first = min(first, max(0, len(rows) - visible))
        screen.paint(title, "", rows[first:first + visible], -1, "↑ ↓ Scroll   Enter / Esc Back")
        key = screen.key()
        if key in _ENTER:
            return True
        if key == "\x1b":
            return False
        if key in (curses.KEY_DOWN, "j"):
            first += 1
        elif key in (curses.KEY_UP, "k"):
            first = max(0, first - 1)
        elif key == curses.KEY_NPAGE:
            first += visible
        elif key == curses.KEY_PPAGE:
            first = max(0, first - visible)


def show_text(title: str, body: str) -> None:
    _run(lambda screen: _show(screen, title, body))


def confirm_uninstall(paths: Iterable[str] = ()) -> bool | None:
    def decide(screen):
        files = "\n".join(f"  {path}" for path in paths)
        if files and not _show(screen, "Flowvy MOTD files", files):
            return None
        choice = _menu(screen, [
            ("cancel", "Cancel"),
            ("remove", "Uninstall Flowvy MOTD and remove settings"),
            ("keep", "Uninstall Flowvy MOTD and keep settings"),
        ], "Confirm uninstall", "System packages and OS login settings are kept.")
        if choice in ("remove", "keep"):
            return choice == "keep"
        return None

    return _run(decide)


class _Selection:
    def __init__(self, items, selected):
        self.items = sorted(items, key=lambda item: (item.source, not bool(item.group),
                                                       item.group.casefold(), item.name.casefold()))
        self.selected = list(dict.fromkeys(selected))
        self.tab = 0
        self.query = ""
        self.search_both = False

    def search(self, query):
        self.query = query.strip().casefold()
        self.search_both = bool(self.query)

    def switch(self):
        self.tab = 1 - self.tab
        self.search_both = False

    def toggle(self, identity):
        if identity in self.selected:
            self.selected.remove(identity)
        else:
            self.selected.append(identity)

    def results(self):
        result = []
        for item in self.items:
            if not self.search_both and item.source != _SOURCES[self.tab]:
                continue
            haystack = " ".join((item.id, item.name, item.description, item.group,
                                 item.search_text)).casefold()
            if all(word in haystack for word in self.query.split()):
                result.append(item)
        return result


def _picker(screen, items, selected, title):
    controller = _Selection(items, selected)
    if screen is None:
        return _plain_picker(controller, title)

    index = 0
    while True:
        results = controller.results()
        index = min(index, max(0, len(results) - 1))
        rows, indexes = _picker_rows(results, controller.selected)
        tabs = ("Services + Containers" if controller.search_both else
                "[Services]  Containers" if controller.tab == 0 else "Services  [Containers]")
        status = f"{tabs}   ·   {len(controller.selected)} selected"
        if controller.query:
            status += f"   / {controller.query}"
        if results:
            description = results[index].description
            if description and description != results[index].name:
                status += f"   ·   {description}"
        screen.paint(title, status, rows, indexes[index] if indexes else -1,
                     "↑ ↓ Move  Space Select  Tab Tab  / Search  Enter Continue  Esc Back")
        key = screen.key()
        if key == "\x1b":
            return None
        if key in _ENTER:
            return controller.selected
        if key == "\t":
            controller.switch()
            index = 0
        elif key == "/":
            query = _input(screen, "Search services and containers", controller.query,
                           subtitle="Search by name, description, Debian package, project, or image.")
            if query is not None:
                controller.search(query)
                index = 0
        elif results and key == " ":
            controller.toggle(results[index].id)
        elif results:
            index = _navigate(key, index, len(results))


def _picker_rows(results, selected) -> tuple[list[Row], list[int]]:
    rows = []
    indexes = []
    previous_group = None
    for item in results:
        group = (item.source, item.group_kind, item.group)
        if group != previous_group:
            if item.group:
                kind = _GROUP_KIND.get(item.group_kind, "group")
                heading = f"{item.group} · {kind}"
            else:
                heading = "Other services" if item.source == "systemd" else "Standalone containers"
            rows.append((heading, "section"))
            previous_group = group
        indexes.append(len(rows))
        checked = "x" if item.id in selected else " "
        status = _STATE_LABELS.get(item.state, item.state.replace("-", " ").capitalize())
        rows.append((f"[{checked}] {item.name}  ·  {status}", "plain"))
    if not rows:
        rows.append(("No matching components", "muted"))
    return rows, indexes


def _plain_picker(controller, title):
    while True:
        options = []
        for index, component in enumerate(controller.results()):
            selected = "x" if component.id in controller.selected else " "
            metadata = f"  ·  {component.group}" if component.group else ""
            options.append((str(index), f"[{selected}] {component.name}{metadata}"))
        options.extend([
            ("tab", "Switch between services and containers"),
            ("search", "Search both lists"),
            ("continue", "Continue with selection"),
        ])
        choice = _plain_menu(options, title, f"{len(controller.selected)} selected")
        if choice is None:
            return None
        if choice == "continue":
            return controller.selected
        if choice == "tab":
            controller.switch()
        elif choice == "search":
            controller.search(_input(None, "Search", controller.query))
        else:
            controller.toggle(controller.results()[int(choice)].id)


def _top_level_ids(config):
    services = ("unit:" + unit for unit in config.get("services", []))
    containers = (name if name.startswith(("compose:", "container:")) else "container:" + name
                  for name in config.get("containers", []))
    return [*services, *containers]


def _selected_ids(config):
    members = (identity for group in config.get("applications", []) for identity in group["members"])
    return list(dict.fromkeys((*_top_level_ids(config), *members)))


def _replace_selection(config, selected):
    draft = deepcopy(config)
    grouped_only = set(_selected_ids(config)) - set(_top_level_ids(config))
    top_level = [identity for identity in selected if identity not in grouped_only]
    draft["services"] = [item.removeprefix("unit:") for item in top_level if item.startswith("unit:")]
    draft["containers"] = [item for item in top_level if item.startswith(("compose:", "container:"))]
    available = set(selected)
    groups = []
    for group in draft.get("applications", []):
        members = [identity for identity in group["members"] if identity in available]
        if members:
            group["members"] = members
            if group.get("primary") not in members:
                group["primary"] = ""
            groups.append(group)
    draft["applications"] = groups
    return draft


def _with_missing(items, selected):
    from .inventory import Component

    by_id = {item.id: item for item in items}
    for identity in selected:
        if identity not in by_id:
            kind, _, name = identity.partition(":")
            source = "systemd" if kind == "unit" else "docker"
            by_id[identity] = Component(identity, name, source=source, state="missing")
    return list(by_id.values())


def _suggest_groups(items, applications):
    attached = {identity for group in applications for identity in group.get("members", [])}
    grouped = {}
    for item in items:
        if item.group and item.id not in attached:
            key = (item.group_kind, item.group)
            grouped.setdefault(key, []).append(item.id)
    return [{"name": f"{name} ({_GROUP_KIND.get(kind, 'group')})", "members": members,
             "primary": "", "check_url": ""}
            for (kind, name), members in sorted(grouped.items())]


def _group_editor(screen, group, eligible, existing_names, existing):
    draft = deepcopy(group)
    by_id = {item.id: item.name for item in eligible}
    while True:
        label = by_id.get(draft["primary"], "None")
        options = [
            ("name", f"Name  ·  {draft['name'] or 'Not set'}"),
            ("members", f"Components  ·  {len(draft['members'])} selected"),
            ("primary", f"Primary component  ·  {label}"),
            ("check", f"Local HTTP check  ·  {draft['check_url'] or 'None'}"),
            ("done", "Keep group in configuration draft"),
        ]
        if existing:
            options.append(("delete", "Remove grouping"))
        options.append(("back", "Back"))
        choice = _menu(screen, options, "Application group")
        if choice in (None, "back"):
            return None
        if choice == "delete":
            return {}
        if choice == "name":
            value = _input(screen, "Application name", draft["name"])
            if value is not None:
                draft["name"] = value.strip()
        elif choice == "members":
            value = _picker(screen, eligible, draft["members"], "Select group components")
            if value is not None:
                draft["members"] = value
                if draft["primary"] not in value:
                    draft["primary"] = ""
        elif choice == "primary":
            options = [("", "No primary component")]
            options.extend((member, by_id[member]) for member in draft["members"] if member in by_id)
            value = _menu(screen, options, "Primary component",
                          "Choose the component that reflects the application's main function.")
            if value is not None:
                draft["primary"] = value
        elif choice == "check":
            value = _input(screen, "Local HTTP check", draft["check_url"],
                           subtitle="Optional. Example: http://127.0.0.1:8080/health")
            if value is not None:
                from .settings import SettingsError, probe_target

                value = value.strip()
                try:
                    if value:
                        probe_target(value)
                except SettingsError as error:
                    _show(screen, "Invalid HTTP check", str(error))
                else:
                    draft["check_url"] = value
        elif choice == "done":
            if not draft["name"] or not draft["members"]:
                _show(screen, "Incomplete application group", "Choose a name and at least one component.")
            elif draft["name"].casefold() in existing_names:
                _show(screen, "Duplicate application name", "Choose a different name.")
            else:
                return draft


def _edit_groups(screen, draft, items):
    selected = set(_selected_ids(draft))
    items = [item for item in _with_missing(items, selected) if item.id in selected]
    groups = draft.setdefault("applications", [])
    while True:
        suggestions = _suggest_groups(items, groups)
        options = [(f"existing:{i}", f"Edit {group['name']}") for i, group in enumerate(groups)]
        options.extend((f"suggested:{i}", f"Group {group['name']}") for i, group in enumerate(suggestions))
        attached = {identity for group in groups for identity in group["members"]}
        if any(item.id not in attached for item in items):
            options.append(("custom", "Create a custom application group"))
        options.append(("back", "Back"))
        choice = _menu(screen, options, "Application groups",
                       "Groups combine selected components into one MOTD row.")
        if choice in (None, "back"):
            return
        action, _, index = choice.partition(":")
        existing = action == "existing"
        if existing:
            i = int(index)
            group = groups[i]
        elif action == "suggested":
            group = suggestions[int(index)]
        else:
            group = {"name": "", "members": [], "primary": "", "check_url": ""}
        existing_names = {other["name"].casefold() for other in groups if other is not group}
        eligible = [item for item in items if item.id not in attached or item.id in group["members"]]
        result = _group_editor(screen, group, eligible, existing_names, existing)
        if result is None:
            continue
        if not result and existing:
            groups.pop(i)
        elif existing:
            groups[i] = result
        else:
            groups.append(result)


def _edit(screen, config, items, preview):
    draft = deepcopy(config)
    while True:
        style = "Compact" if draft.get("compact") else "Automatic"
        options = [
            ("provider", f"Provider  ·  {draft.get('provider') or 'Not set'}"),
            ("role", f"Server role  ·  {draft.get('role') or 'Not set'}"),
            ("location", f"Location  ·  {draft.get('location') or 'Not set'}"),
            ("selection", f"Services and containers  ·  {len(_selected_ids(draft))} selected"),
            ("groups", f"Application groups  ·  {len(draft.get('applications', []))}"),
            ("layout", f"Display  ·  {style}"),
            ("preview", "Preview and apply"),
            ("back", "Cancel"),
        ]
        choice = _menu(screen, options, "Configure Flowvy MOTD")
        if choice in (None, "back"):
            return None
        if choice in ("provider", "role", "location"):
            title = "Server role" if choice == "role" else choice.capitalize()
            value = _input(screen, title, draft.get(choice, ""))
            if value is not None:
                draft[choice] = value.strip()
        elif choice == "selection":
            selected = _selected_ids(draft)
            value = _picker(screen, _with_missing(items, selected), selected, "Show in MOTD")
            if value is not None:
                draft = _replace_selection(draft, value)
        elif choice == "groups":
            _edit_groups(screen, draft, items)
        elif choice == "layout":
            draft["compact"] = not draft.get("compact", False)
        elif choice == "preview":
            try:
                output = preview(draft)
            except ValueError as error:
                _show(screen, "Invalid settings", str(error))
                continue
            if not _show(screen, "MOTD preview", output):
                continue
            action = _menu(screen, [("back", "Return to configuration"), ("apply", "Save settings")],
                           "Apply configuration")
            if action == "apply":
                return draft


def edit_config(config: dict, items: list["Component"], preview: Callable[[dict], str]) -> dict | None:
    return _run(lambda screen: _edit(screen, config, items, preview))
