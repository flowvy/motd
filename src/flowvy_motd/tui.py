"""Interactive terminal screens for Flowvy MOTD."""

from collections import Counter
from collections.abc import Callable, Iterable
from copy import deepcopy
import curses
from dataclasses import dataclass
import os
import re
import shutil
import socket
import sys
import textwrap
from typing import TYPE_CHECKING

from .dashboard import COLORS, RESET, STATUS, char_width, fit
from .settings import SERVICE_LABEL_LIMIT, SettingsError, config_text, probe_target

if TYPE_CHECKING:
    from .inventory import Component


@dataclass(frozen=True)
class _Entry:
    label: str
    value: str = ""
    tone: str = "muted"
    group: str = ""
    value_column: int = 0


Row = tuple[str | _Entry | list[tuple[str, str]], str]
Shortcuts = tuple[tuple[str, str], ...]
Option = tuple[str, str]
_ENTER = ("\n", "\r", curses.KEY_ENTER)
_BACKSPACE = ("\b", "\x7f", curses.KEY_BACKSPACE)
_GROUP_KIND = {"package": "package", "compose": "Compose"}
_CONFIG_FIELDS = {"provider": "Provider", "role": "Server role", "location": "Location"}
_GROUP_FIELDS = {"name": "Group name", "check_url": "Local HTTP check"}
_FIELD_LIMITS = {"name": 64, "check_url": 300}
_LOCATION_NOTES = {
    "pending": "Detecting location…",
    "detected": "Auto-detected from this server’s public IP.",
    "unavailable": "Could not detect location. Enter it manually.",
}
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_BODY_TOKENS = re.compile(f"{_ANSI.pattern}|[^\x1b]+|\x1b")
_BODY_TONES = {escape: tone for tone, escape in COLORS.items()} | {RESET: "plain"}
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


def _identifier(text, width):
    if width < 3 or sum(map(char_width, text)) <= width:
        return fit(text, width)
    tail = []
    used = 0
    for char in reversed(text):
        size = char_width(char)
        if used + size > width // 2:
            break
        tail.append(char)
        used += size
    return fit(text, width - used) + "".join(reversed(tail))


_MENU_KEYS = (("↑ ↓", "Move"), ("Enter", "Select"), ("Esc", "Back"))
_EDIT_KEYS = (("← →", "Cursor"), ("Ctrl-U", "Clear"), ("Enter", "Apply"), ("Esc", "Revert"))
_HOST_KEYS = (("Tab", "Next field"), ("Shift-Tab", "Previous"), ("Enter", "Keep changes"), ("Esc", "Cancel"))
_PICKER_KEYS = (("↑ ↓", "Move"), ("Space", "Toggle"), ("/", "Filter"), ("Enter", "Keep"), ("Esc", "Back"))


def _shortcut_width(shortcut):
    key, label = shortcut
    return len(key) + 2 + (len(label) + 1 if label else 0)


def _shortcut_lines(shortcuts, width, limit):
    if not limit:
        return []

    def pack(pairs, gap=3):
        lines = []
        used = width
        for pair in pairs:
            size = _shortcut_width(pair)
            if size > width:
                key, _ = pair
                pair = (key, "")
                size = _shortcut_width(pair)
                if size > width:
                    continue
            if used + gap + size > width:
                lines.append([])
                used = 0
            if used:
                used += gap
            lines[-1].append(pair)
            used += size
        return lines

    lines = pack(shortcuts)
    if len(lines) <= limit:
        return lines
    essential = {key for key, _ in shortcuts if key.split()[0] in {"Enter", "Esc", "Space", "/"}}
    chosen = set(essential)
    lines = pack(pair for pair in shortcuts if pair[0] in chosen)
    if len(lines) > limit:
        compact = pack(((key, "") for key, _ in shortcuts if key in chosen), gap=1)
        if len(compact) <= limit:
            return compact
        chosen = {key for key in essential if key.split()[0] in {"Enter", "Esc"}}
        lines = pack(pair for pair in shortcuts if pair[0] in chosen)
        if len(lines) > limit:
            return pack(((key, "") for key, _ in shortcuts if key in chosen), gap=1)[:limit]

    for key, _ in shortcuts:
        if key in chosen:
            continue
        candidate = pack(pair for pair in shortcuts if pair[0] in chosen or pair[0] == key)
        if len(candidate) <= limit:
            chosen.add(key)
            lines = candidate
    return lines


@dataclass(frozen=True)
class _Layout:
    left: int
    width: int
    padding: int
    top: int
    available: int
    framed: bool
    footer: list[list[tuple[str, str]]]

    @property
    def content_left(self):
        return self.left + self.padding

    @property
    def content_width(self):
        return _content_width(self.width, self.padding)


def _layout(height, width, has_subtitle, shortcuts=()):
    framed = height >= 19 and width >= 40
    panel_width = min(width, 100) if framed else width
    padding = 4 if framed and width >= 58 else 3 if framed else 2
    footer = _shortcut_lines(shortcuts, _content_width(panel_width, padding),
                             2 if height >= 14 else int(height >= 3))
    if framed:
        top = 7 if has_subtitle else 6
        available = height - top - 3 - len(footer)
    else:
        top = 2 if height >= 7 else 1
        if has_subtitle and height >= 8:
            top += 2
        available = height - top - len(footer)
    return _Layout((width - panel_width) // 2, panel_width, padding, top, max(0, available), framed, footer)


def _content_width(width, margin=4):
    return max(1, min(width, 100) - 2 * margin - 1)


class _Terminal:
    def __init__(self, window):
        self.window = window
        self.hostname = socket.gethostname()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.styles = {
            "title": curses.A_BOLD,
            "section": curses.A_BOLD,
            "focus": curses.A_REVERSE | curses.A_BOLD,
            "focus-danger": curses.A_REVERSE | curses.A_BOLD,
            "plain": curses.A_NORMAL,
            "input": curses.A_NORMAL,
            "input-focus": curses.A_BOLD,
            "border": curses.A_DIM,
            "border-focus": curses.A_BOLD,
            "rule": curses.A_DIM,
            "key": curses.A_REVERSE | curses.A_BOLD,
            "footer-label": curses.A_NORMAL,
            "link": curses.A_BOLD,
            "muted": curses.A_DIM,
            "danger": curses.A_BOLD,
            "accent": curses.A_BOLD,
            "ok": curses.A_NORMAL,
            "warn": curses.A_BOLD,
            "bad": curses.A_BOLD,
        }
        if curses.has_colors() and not os.environ.get("NO_COLOR"):
            try:
                curses.use_default_colors()
                if curses.COLORS >= 256 and curses.COLOR_PAIRS > len(self.styles):
                    palette = {
                        "title": (78, 233), "section": (79, 233), "plain": (252, 233),
                        "focus": (253, 236), "focus-danger": (203, 236),
                        "input": (253, 234), "input-focus": (253, 236),
                        "border": (66, 234), "border-focus": (79, 236),
                        "rule": (66, 233), "muted": (246, 233),
                        "key": (253, 238), "footer-label": (249, 234), "link": (79, 233),
                        "danger": (203, 233), "accent": (78, 233), "ok": (78, 233),
                        "warn": (221, 233), "bad": (203, 233),
                    }
                    for pair, (style, colors) in enumerate(palette.items(), start=1):
                        curses.init_pair(pair, *colors)
                        emphasis = self.styles[style] & ~(curses.A_DIM | curses.A_REVERSE)
                        self.styles[style] = curses.color_pair(pair) | emphasis
                else:
                    for pair, (color, styles) in enumerate((
                        (curses.COLOR_GREEN, ("title", "section", "accent", "ok", "border-focus")),
                        (curses.COLOR_YELLOW, ("warn",)),
                        (curses.COLOR_RED, ("danger", "bad", "focus-danger")),
                    ), start=1):
                        curses.init_pair(pair, color, -1)
                        for style in styles:
                            self.styles[style] |= curses.color_pair(pair)
            except curses.error:
                pass

    def size(self):
        return self.window.getmaxyx()

    def paint(self, title, subtitle, rows: list[Row], index, shortcuts: Shortcuts, *, pinned=0):
        height, width = self.size()
        layout = _layout(height, width, bool(subtitle), shortcuts)
        top, available = layout.top, layout.available
        margin, content_width = layout.content_left, layout.content_width
        self.window.erase()
        note_column = margin
        note_lines = []
        if rows and rows[-1][1] == "note":
            label, _, _ = rows[-2][0].partition("[ ")
            if content_width - len(label) >= 31:
                note_column += len(label)
            note_lines = textwrap.wrap(rows[-1][0], content_width - note_column + margin)
        field_count = sum(style == "field" for _, style in rows)
        boxed = width >= 24 and available >= max(8, field_count * 3 + len(note_lines))
        field_height = 3 if boxed else 1
        sizes = []
        for _, style in rows:
            size = 1
            if style == "field":
                size = field_height
            elif style == "note":
                size = len(note_lines)
            elif style in ("gap", "divider", "detail", "columns") and available < 6:
                size = 0
            elif style == "section" and available >= 10:
                size = 2
            sizes.append(size)
        if layout.framed:
            if boxed and field_count > 1:
                gaps = [i for i in range(len(rows) - 1) if rows[i][1] == rows[i + 1][1] == "field"]
                if sum(sizes) + len(gaps) <= available:
                    for i in gaps:
                        sizes[i] += 1
            end = top + min(available, max(1, sum(sizes))) + len(layout.footer) + 2
            self._frame(title, subtitle, end, layout)
        else:
            title_y = 0
            if height >= 7:
                self._line(0, fit(f"Flowvy MOTD  ·  {self.hostname}", width - 1), "muted")
                title_y = 1
            self._line(title_y, fit(title, width - 1), "title")
            if subtitle and height >= 8:
                self._line(title_y + 1, fit(subtitle, width - 1), "muted")
            self._footer(height - len(layout.footer), layout)

        pinned = min(pinned, available, len(rows))
        budget = available - sum(sizes[:pinned])
        start = pinned
        through_focus = sum(sizes[pinned:index + 1]) if index >= pinned else 0
        if 0 <= index < len(rows) - 1 and rows[index + 1][1] in ("note", "detail"):
            if sizes[index] + sizes[index + 1] <= budget:
                through_focus += sizes[index + 1]
        y = top
        shown_group = ""
        while start < index and through_focus > budget:
            through_focus -= sizes[start]
            start += 1
        for row_index in (*range(pinned), *range(start, len(rows))):
            size = sizes[row_index]
            if not size:
                continue
            if y + size > top + available:
                break
            content, style = rows[row_index]
            focused = row_index == index
            if style == "field":
                self._field(y, content, focused, size, margin)
            elif style == "note":
                for offset, line in enumerate(note_lines):
                    self._line(y + offset, line, "muted", note_column)
            elif style == "body":
                column = margin
                for segment, tone in content:
                    self._line(y, segment, tone, column)
                    column += sum(map(char_width, segment))
            elif style == "section":
                self._line(y + size - 1, fit(content, content_width), style, margin)
                shown_group = content
            elif style == "detail":
                self._line(y, fit(content, max(1, content_width - 8)), "muted", margin + 8)
            elif style == "divider":
                self._line(y, "─" * content_width, "rule", margin)
            elif style != "gap":
                self._entry(y, content, style, focused, shown_group, layout)
            y += size
        self.window.refresh()

    def _entry(self, y, content, style, focused, shown_group, layout):
        entry = content if isinstance(content, _Entry) else _Entry(content)
        left, width = layout.content_left, layout.content_width
        is_component = style in ("component", "columns")
        indent = 4 if is_component else 2
        tone = "muted" if style == "columns" else style if style in ("danger", "link", "muted") else "plain"
        marker = "›" if focused else " "
        if focused:
            tone = "focus-danger" if style == "danger" else "focus"
            self._line(y, " " * width, tone, left)
            self._line(y, marker, tone, left + indent - 2)
        label = entry.label
        if entry.group and entry.group != shown_group:
            check, closing, name = label.partition("] ")
            label = f"{check}{closing}{entry.group.partition(' · ')[0]}/{name}"
        label_width = max(1, width - indent)
        if entry.value and width >= 42:
            column = entry.value_column or min(44, width * 3 // 5)
            label_width = column - indent - 2
            self._line(y, fit(entry.value, width - column), tone if focused else entry.tone, left + column)
        elif entry.value and style == "component":
            name_width = label_width - len(entry.value) - 3
            if name_width >= 16 or sum(map(char_width, label)) <= name_width:
                label = _identifier(label, name_width) + " · " + entry.value
        elif entry.value:
            label = f"{label} · {entry.value}"
        shorten = _identifier if style == "component" else fit
        self._line(y, shorten(label, label_width), tone, left + indent)

    def _frame(self, title, subtitle, end, layout):
        left, width, margin = layout.left, layout.width, layout.content_left
        rule = "─" * (width - 3)
        footer_y = end - len(layout.footer)
        self._line(0, "╭" + rule + "╮", "rule", left)
        for y in range(1, end):
            background = "input" if y == 1 or y >= footer_y else "plain"
            self._line(y, " " * (width - 1), background, left)
            self._line(y, "│", "rule", left)
            self._line(y, "│", "rule", left + width - 2)
        self._line(1, "● Flowvy MOTD", "input", margin)
        name = fit(self.hostname, width - 23)
        self._line(1, name, "input", margin + layout.content_width - sum(map(char_width, name)))
        self._line(2, "├" + rule + "┤", "rule", left)
        self._line(4, fit(title, layout.content_width), "title", margin)
        if subtitle:
            self._line(5, fit(subtitle, layout.content_width), "muted", margin)
        self._line(footer_y - 1, "─" * layout.content_width, "rule", margin)
        self._footer(footer_y, layout)
        self._line(end, "╰" + rule + "╯", "rule", left)

    def _footer(self, start, layout):
        for offset, shortcuts in enumerate(layout.footer):
            column = layout.content_left
            gap = 3 if any(label for _, label in shortcuts) else 1
            for key, label in shortcuts:
                self._line(start + offset, f" {key} ", "key", column)
                if label:
                    self._line(start + offset, label, "footer-label", column + len(key) + 3)
                column += _shortcut_width((key, label)) + gap

    def _field(self, y, content, focused, size, margin):
        label, _, value = content.partition("[ ")
        value = " " + value.removesuffix(" ]") + " "
        column = margin + len(label)
        middle = y + (size > 1)
        width = sum(map(char_width, value))
        border = "border-focus" if focused else "border"
        self._line(middle, label, "section", margin)
        self._line(middle, "│", border, column)
        self._line(middle, value, "input-focus" if focused else "input", column + 1)
        self._line(middle, "│", border, column + width + 1)
        if size > 1:
            self._line(y, "╭" + "─" * width + "╮", border, column)
            self._line(y + 2, "╰" + "─" * width + "╯", border, column)

    def _line(self, y, value, style, column=0):
        height, width = self.size()
        if 0 <= y < height and width > column + 1:
            try:
                self.window.addnstr(y, column, _text(value), width - column - 1, self.styles[style])
            except curses.error:
                pass

    def key(self, timeout=None):
        if timeout is None:
            return self.window.get_wch()
        self.window.timeout(timeout)
        try:
            return self.window.get_wch()
        except curses.error:
            return None
        finally:
            self.window.timeout(-1)


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


def _navigation_rows(options, rows, sections, columns):
    output, indexes = [], []
    labels = [len(label.partition("  ·  ")[0]) for key, label in options if key in columns]
    value_column = max(20, max(labels, default=0) + 4)
    for (key, _), (label, style) in zip(options, rows):
        if key in sections:
            output.append(sections[key])
        indexes.append(len(output))
        if key in columns and style == "plain":
            label, _, value = label.partition("  ·  ")
            label = _Entry(label, value, value_column=value_column)
        if key in {"uninstall", "remove", "keep", "delete"}:
            style = "danger"
        elif key in {"create", "done", "preview", "apply"}:
            style = "link"
        output.append((label, style))
    return output, indexes


def _menu(screen, options, title, subtitle="", *, sections=None, columns=()):
    if not options:
        return None
    if screen is None:
        return _plain_menu(options, title, subtitle)
    index = 0
    boundaries = sections if sections is not None else {
        "update": ("Maintenance", "section"), "exit": ("", "divider"),
    }
    rows, indexes = _navigation_rows(options, [(label, "plain") for _, label in options], boundaries, columns)
    while True:
        screen.paint(title, subtitle, rows, indexes[index], _MENU_KEYS)
        key = screen.key()
        if key == "\x1b":
            return None
        if key in _ENTER:
            return options[index][0]
        index = _navigate(key, index, len(options))


def menu(options: list[Option], *, title: str, subtitle: str = "") -> str | None:
    return _run(lambda screen: _menu(screen, options, title, subtitle))


def _plain_input(title, label, value):
    print(f"\n{title} › {label}")
    width = shutil.get_terminal_size((80, 24)).columns
    print("  " + _field_row(label, value, width))
    answer = input("Value (- clear, :cancel back): ")
    if answer in ("", "\x1b", ":cancel"):
        return None
    return "" if answer == "-" else answer


def _body_rows(body, width):
    rows = []
    for line in body.expandtabs().splitlines():
        spans = []
        remaining = width
        tone = "plain"
        for token in _BODY_TOKENS.findall(line):
            if token.startswith("\x1b"):
                tone = _BODY_TONES.get(token, tone)
                continue
            for char in _text(token):
                size = char_width(char)
                if size > width:
                    char, size = "…", 1
                if size > remaining:
                    rows.append((spans, "body"))
                    spans = []
                    remaining = width
                if spans and spans[-1][1] == tone:
                    spans[-1] = (spans[-1][0] + char, tone)
                else:
                    spans.append((char, tone))
                remaining -= size
        rows.append((spans, "body"))
    return rows


def _show(screen, title, body, *, advance=False):
    if screen is None:
        plain = _ANSI.sub("", body).expandtabs()
        print(f"\n{title}\n\n{plain.rstrip()}")
        if sys.stdin.isatty():
            if advance:
                return input("\nPress Enter to continue (:cancel to go back). ") not in ("\x1b", ":cancel")
            input("\nPress Enter to go back. ")
        return True
    first = 0
    previous_width = 0
    rows = []
    keys = (("Enter", "Continue"), ("Esc", "Back")) if advance else (("Enter / Esc", "Back"),)
    while True:
        height, width = screen.size()
        layout = _layout(height, width, False, keys)
        content_width = layout.content_width
        if content_width != previous_width:
            rows = _body_rows(body, content_width)
            previous_width = content_width
        shortcuts = keys
        if len(rows) > layout.available:
            shortcuts = (("↑ ↓", "Scroll"), ("PgUp / PgDn", "Page"), *keys)
            layout = _layout(height, width, False, shortcuts)
        visible = max(1, layout.available)
        first = min(first, max(0, len(rows) - visible))
        screen.paint(title, "", rows[first:first + visible], -1, shortcuts)
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
        if files and not _show(screen, "Flowvy MOTD files", files, advance=True):
            return None
        choice = _menu(screen, [
            ("cancel", "Cancel"),
            ("remove", "Remove settings and uninstall"),
            ("keep", "Keep settings and uninstall"),
        ], "Confirm uninstall", "System packages and OS login settings are kept.")
        if choice in ("remove", "keep"):
            return choice == "keep"
        return None

    return _run(decide)


class _Selection:
    def __init__(self, items, selected, labels=None):
        self.items = sorted(items, key=lambda item: (item.source, not bool(item.group),
                                                       item.group.casefold(), item.name.casefold()))
        self.selected = list(dict.fromkeys(selected))
        self.labels = labels
        self.query = ""

    def toggle(self, identity):
        if identity in self.selected:
            self.selected.remove(identity)
        else:
            self.selected.append(identity)

    def results(self, query):
        words = query.casefold().split()
        return [item for item in self.items if all(word in self._searchable(item) for word in words)]

    def _searchable(self, item):
        label = (self.labels or {}).get(item.name, "")
        return " ".join((item.id, item.name, item.description, item.group, item.search_text, label)).casefold()

    def empty(self, noun, query):
        if self.items and query.strip():
            return f"No {noun} match the filter."
        return f"No {noun} found."


def _rename_service(screen, item, labels):
    title = "Rename service"
    name = item.name
    current = labels.get(name, "")
    if screen is None:
        value = _plain_input(f"{title} › {name}", "Display name", current)
        if value is None:
            return
    else:
        editor = _InlineField(current, SERVICE_LABEL_LIMIT)
        while True:
            height, width = screen.size()
            layout = _layout(height, width, True, _EDIT_KEYS)
            field = _field_row("Display name", editor.value, layout.content_width + 3,
                               editor=editor, full_width=True)
            screen.paint(title, name, [(field, "field")], 0, _EDIT_KEYS)
            key = screen.key()
            if key == "\x1b":
                return
            if key in _ENTER:
                value = editor.value
                break
            editor.key(key)
    try:
        label = config_text(value, "Display name", SERVICE_LABEL_LIMIT)
    except SettingsError as error:
        _show(screen, "Invalid display name", str(error))
        return
    if label and label != name:
        labels[name] = label
    else:
        labels.pop(name, None)


def _picker(screen, items, selected, title, noun, *, labels=None):
    controller = _Selection(items, selected, labels)
    if screen is None:
        return _plain_picker(controller, title, noun)

    index = 0
    editor = None
    while True:
        query = editor.value if editor else controller.query
        results = controller.results(query)
        height, width = screen.size()
        can_rename = editor is None and labels is not None and results and results[index].id in controller.selected
        shortcuts = _EDIT_KEYS if editor is not None else _PICKER_KEYS
        if can_rename:
            shortcuts = (*_PICKER_KEYS[:-2], ("R", "Rename"), *_PICKER_KEYS[-2:])
        layout = _layout(height, width, True, shortcuts)
        rows = [(_field_row("Filter", query, layout.content_width + 3, editor=editor,
                            placeholder="Search by name", label_width=11, full_width=True), "field")]
        if layout.available >= 11 and results:
            if layout.content_width >= 42:
                rows.append((_Entry(noun.removesuffix("s").capitalize(), "Status"), "columns"))
        pinned = len(rows)
        current = 0 if editor is not None and editor.modified else index
        entries, indexes = _picker_rows(results, controller.selected, detail_index=current, labels=labels)
        rows.extend(entries or [(controller.empty(noun, query), "muted")])
        status = f"{len(controller.selected)} selected  ·  {len(results)} shown"
        focus = 0 if editor is not None else indexes[index] + pinned if indexes else len(rows) - 1
        screen.paint(title, status, rows, focus, shortcuts, pinned=pinned if layout.available >= 2 else 0)
        key = screen.key()
        if editor is not None:
            if key in _ENTER:
                query = editor.value.strip()
                if query != controller.query:
                    index = 0
                controller.query = query
                editor = None
            elif key == "\x1b":
                editor = None
            else:
                editor.key(key)
        elif key == "\x1b":
            return None
        elif key in _ENTER:
            return controller.selected
        elif key == "/":
            editor = _InlineField(controller.query)
        elif can_rename and key in ("r", "R"):
            item = results[index]
            _rename_service(screen, item, labels)
            results = controller.results(controller.query)
            if item not in results:
                controller.query = ""
                results = controller.results("")
            index = results.index(item)
        elif results and key == " ":
            controller.toggle(results[index].id)
        elif results:
            index = _navigate(key, index, len(results))


def _picker_rows(results, selected, *, detail_index=-1, labels=None) -> tuple[list[Row], list[int]]:
    rows = []
    indexes = []
    previous_group = None
    names = Counter(item.name for item in results)
    for index, item in enumerate(results):
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
        name = item.name
        context = heading if item.group else ""
        if item.group and names[name] > 1:
            name = f"{item.group}/{name}" if item.group_kind == "compose" else f"{name} ({item.group})"
            context = ""
        tone = STATUS.get(item.state, ("muted", ""))[0]
        rows.append((_Entry(f"[{checked}] {name}", status, tone, context), "component"))
        if index == detail_index:
            label = (labels or {}).get(item.name)
            detail = f"Display name: {label}" if label else item.description
            if detail and detail != item.name:
                rows.append((detail, "detail"))
    return rows, indexes


def _plain_picker(controller, title, noun):
    while True:
        results = controller.results(controller.query)
        width = shutil.get_terminal_size((80, 24)).columns
        options = []
        for index, component in enumerate(results):
            selected = "x" if component.id in controller.selected else " "
            metadata = f"  ·  {component.group}" if component.group else ""
            label = (controller.labels or {}).get(component.name)
            suffix = f"  ·  Display name: {label}" if label else ""
            options.append((str(index), f"[{selected}] {component.name}{metadata}{suffix}"))
        options.append(("filter", "Edit filter"))
        if controller.labels is not None and controller.selected:
            options.append(("rename", "Rename a selected service"))
        options.append(("continue", "Continue with selection"))
        count = f"{len(controller.selected)} selected  ·  {len(results)} shown"
        field = _field_row("Filter", controller.query, width, placeholder="Search by name")
        empty = f"\n{controller.empty(noun, controller.query)}" if not results else ""
        choice = _plain_menu(options, title, f"{count}\n  {field}{empty}")
        if choice is None:
            return None
        if choice == "continue":
            return controller.selected
        if choice == "filter":
            value = _plain_input(title, "Filter", controller.query)
            if value is not None:
                controller.query = _text(value).strip()
        elif choice == "rename":
            by_id = {item.id: item for item in controller.items if item.id in controller.selected}
            identity = _plain_menu([(item.id, item.name) for item in by_id.values()], "Rename service")
            if identity is not None:
                _rename_service(None, by_id[identity], controller.labels)
        else:
            controller.toggle(results[int(choice)].id)


def _with_missing(items, selected):
    from .inventory import Component

    by_id = {item.id: item for item in items}
    for identity in selected:
        if identity not in by_id:
            kind, _, name = identity.partition(":")
            source = "systemd" if kind == "unit" else "docker"
            by_id[identity] = Component(identity, name, source=source, state="missing")
    return list(by_id.values())


def _top_level_ids(draft, field):
    values = draft.get(field, [])
    if field == "services":
        return ["unit:" + name for name in values]
    return [name if name.startswith(("compose:", "container:")) else "container:" + name
            for name in values]


def _grouped_ids(draft):
    return {member for group in draft.get("applications", []) for member in group["members"]}


def _standalone_count(draft, field):
    return len(set(_top_level_ids(draft, field)) - _grouped_ids(draft))


def _select_standalone(screen, draft, items, field):
    containers = field == "containers"
    source = "docker" if containers else "systemd"
    current = _top_level_ids(draft, field)
    grouped = _grouped_ids(draft)
    selected = [identity for identity in current if identity not in grouped]
    available = _with_missing([item for item in items if item.source == source and item.id not in grouped], selected)
    title = "Select containers" if containers else "Select standalone services"
    labels = None if containers else draft.get("service_labels", {}).copy()
    result = _picker(screen, available, selected, title, field, labels=labels)
    if result is None:
        return
    if result != selected:
        keep = grouped | set(result)
        preserved = [value for identity, value in zip(current, draft.get(field, [])) if identity in keep]
        added = [identity for identity in result if identity not in current]
        draft[field] = preserved + (added if containers else [identity.removeprefix("unit:") for identity in added])
    if labels is not None:
        kept_labels = {name: labels[name] for name in draft.get("services", []) if name in labels}
        if kept_labels:
            draft["service_labels"] = kept_labels
        else:
            draft.pop("service_labels", None)


def _keep_ungrouped(draft, members):
    seen = _grouped_ids(draft) | set(_top_level_ids(draft, "services") + _top_level_ids(draft, "containers"))
    for identity in members:
        if identity in seen:
            continue
        if identity.startswith("unit:"):
            draft.setdefault("services", []).append(identity.removeprefix("unit:"))
        else:
            draft.setdefault("containers", []).append(identity)
        seen.add(identity)


def _group_editor(screen, group, eligible, existing_names, existing):
    draft = deepcopy(group)
    by_id = {item.id: item.name for item in eligible}
    has_legacy_containers = any(not member.startswith("unit:") for member in group["members"])
    member_label = "Members" if has_legacy_containers else "Services"
    primary_label = "Primary member" if has_legacy_containers else "Primary service"
    title = "Edit service group" if existing else "Create service group"
    focus = 0
    sections = {
        "members": ("Membership", "section"),
        "check_url": ("", "gap"),
        "done": ("", "divider"),
    }

    def options():
        return [
            ("name", "Group name"),
            ("members", f"{member_label}  ·  {len(draft['members'])} selected"),
            ("primary", f"{primary_label}  ·  {by_id.get(draft['primary'], 'None')}"),
            ("check_url", "Local HTTP check"),
            ("done", "Keep group in draft"),
            *([("delete", "Remove grouping (keep members)")] if existing else []),
            ("back", "Cancel"),
        ]

    while True:
        choice, focus = _form_choice(screen, title, draft, options, _GROUP_FIELDS, focus,
                                     sections=sections, columns={"members", "primary"})
        if choice in (None, "back"):
            return None
        if choice == "delete":
            return {}
        if choice == "members":
            noun = "members" if has_legacy_containers else "services"
            value = _picker(screen, eligible, draft["members"], f"Select {noun}", noun)
            if value is not None:
                draft["members"] = value
                if draft["primary"] not in value:
                    draft["primary"] = ""
        elif choice == "primary":
            choices = [("", f"No {primary_label.lower()}")]
            choices.extend((member, by_id[member]) for member in draft["members"] if member in by_id)
            value = _menu(screen, choices, primary_label,
                          "Choose what reflects the application's main function.")
            if value is not None:
                draft["primary"] = value
        elif choice == "done":
            if not draft["name"] or not draft["members"]:
                _show(screen, "Incomplete service group", "Enter a name and choose at least one member.")
            elif draft["name"].casefold() in existing_names:
                _show(screen, "Duplicate group name", "Choose a different name.")
            else:
                return draft


def _edit_groups(screen, draft, items):
    groups = draft.get("applications", [])
    while True:
        options = [("create", "Create service group")]
        options.extend((f"group:{i}", f"Edit {group['name']}") for i, group in enumerate(groups))
        options.extend([
            ("standalone", f"Standalone services  ·  {_standalone_count(draft, 'services')} selected"),
            ("back", "Back"),
        ])
        sections = {"group:0": ("Groups", "section"), "standalone": ("", "divider")}
        choice = _menu(screen, options, "Service groups", "Each group appears as one row in MOTD.",
                       sections=sections, columns={"standalone"})
        if choice in (None, "back"):
            return
        if choice == "standalone":
            _select_standalone(screen, draft, items, "services")
            continue
        existing = choice.startswith("group:")
        if existing:
            index = int(choice.partition(":")[2])
            group = groups[index]
        else:
            group = {"name": "", "members": [], "primary": "", "check_url": ""}
        other_groups = [other for other in groups if other is not group]
        occupied = {member for other in other_groups for member in other["members"]}
        eligible = [item for item in items if item.id in group["members"]
                    or (item.source == "systemd" and item.id not in occupied)]
        configured = [unit for unit in _top_level_ids(draft, "services") if unit not in occupied]
        result = _group_editor(screen, group, _with_missing(eligible, group["members"] + configured),
                               {other["name"].casefold() for other in other_groups}, existing)
        if result is None:
            continue
        if not result:
            groups.pop(index)
            _keep_ungrouped(draft, group["members"])
        elif existing:
            groups[index] = result
        else:
            groups.append(result)
        draft["applications"] = groups


class _LocationDetection:
    def __init__(self, has_location, factory):
        self.lookup = None
        self.state = ""
        self.value = ""
        if not has_location:
            if factory is None:
                from .location import LocationLookup

                factory = LocationLookup
            self.lookup = factory()
            if self.lookup is not None:
                self.state = "pending"

    @property
    def note(self):
        return _LOCATION_NOTES.get(self.state, "")

    def refresh(self, draft):
        if self.lookup is not None:
            self.state, self.value = self.lookup.poll()
            if self.state != "pending":
                self.lookup = None
        if self.state == "detected" and not draft.get("location"):
            draft["location"] = self.value

    def edited(self):
        self.lookup = None
        self.state = ""


class _InlineField:
    def __init__(self, value, limit=160):
        self.value = value
        self.cursor = len(value)
        self.limit = limit
        self.modified = False

    def key(self, key):
        index = self.cursor
        value = self.value
        if key == curses.KEY_LEFT:
            self.cursor = max(0, index - 1)
        elif key == curses.KEY_RIGHT:
            self.cursor = min(len(value), index + 1)
        elif key == curses.KEY_HOME:
            self.cursor = 0
        elif key == curses.KEY_END:
            self.cursor = len(value)
        elif key == curses.KEY_DC:
            self.value = value[:index] + value[index + 1:]
        elif key in _BACKSPACE and index:
            self.cursor -= 1
            self.value = value[:self.cursor] + value[index:]
        elif key == "\x15":
            self.value = ""
            self.cursor = 0
        elif isinstance(key, str) and key.isprintable() and len(value) < self.limit:
            self.value = value[:index] + key + value[index:]
            self.cursor += 1
        self.modified = self.modified or self.value != value or key == "\x15"

    def display(self, width):
        remaining = width - 1
        left = []
        for char in reversed(self.value[:self.cursor]):
            if char_width(char) > remaining:
                break
            remaining -= char_width(char)
            left.append(char)
        right = []
        for char in self.value[self.cursor:]:
            if char_width(char) > remaining:
                break
            remaining -= char_width(char)
            right.append(char)
        return "".join(reversed(left)) + "▌" + "".join(right)


def _field_row(label, value, width, *, editor=None, placeholder="Not set", label_width=0,
               full_width=False):
    available = max(1, width - 3)
    maximum = max(0, available - 7)
    label = fit(label, maximum) if maximum else ""
    column = min(max(len(label), label_width), maximum)
    prefix = label.ljust(column) + "  "
    interior = max(1, available - len(prefix) - 4)
    if not full_width:
        interior = min(46, interior)
    if editor is not None:
        content = editor.display(interior)
    else:
        content = fit(value or placeholder, interior)
    padding = " " * max(0, interior - sum(map(char_width, content)))
    return prefix + "[ " + content + padding + " ]"


def _form_rows(options, fields, draft, width, index=-1, editor=None):
    column = max(map(len, fields.values()), default=0)
    return [
        (_field_row(fields[key], draft.get(key, ""), width, label_width=column,
                    editor=editor if row == index else None,
                    placeholder="Optional" if key == "check_url" else "Not set"), "field")
        if key in fields else (label, "plain")
        for row, (key, label) in enumerate(options)
    ]


def _save_field(screen, draft, field, label, value):
    try:
        value = config_text(value, label, _FIELD_LIMITS.get(field, 160))
        if field == "check_url" and value:
            probe_target(value)
    except SettingsError as error:
        _show(screen, f"Invalid {label.lower()}", str(error))
        return False
    draft[field] = value
    return True


def _form_choice(screen, title, draft, get_options, fields, index, *, subtitle="", sections=None, columns=()):
    if screen is None:
        while True:
            options = get_options()
            width = shutil.get_terminal_size((80, 24)).columns
            number_width = len(f"  {len(options)}. ")
            rows = _form_rows(options, fields, draft, width - number_width)
            choices = [(key, row) for (key, _), (row, _) in zip(options, rows)]
            choice = _plain_menu(choices, title, subtitle)
            if choice not in fields:
                return choice, index
            value = _plain_input(title, fields[choice], draft.get(choice, ""))
            if value is not None:
                _save_field(screen, draft, choice, fields[choice], value)

    editor = None
    while True:
        options = get_options()
        choice = options[index][0]
        height, width = screen.size()
        shortcuts = _MENU_KEYS if editor is None else _EDIT_KEYS
        layout = _layout(height, width, bool(subtitle), shortcuts)
        rows = _form_rows(options, fields, draft, layout.content_width + 3, index, editor)
        rows, indexes = _navigation_rows(options, rows, sections or {}, columns)
        screen.paint(title, subtitle, rows, indexes[index], shortcuts)
        key = screen.key()
        if editor is not None:
            if key in _ENTER:
                if _save_field(screen, draft, choice, fields[choice], editor.value):
                    editor = None
            elif key == "\x1b":
                editor = None
            else:
                editor.key(key)
        elif key == "\x1b":
            return None, index
        elif key in _ENTER:
            if choice not in fields:
                return choice, index
            editor = _InlineField(draft.get(choice, ""), _FIELD_LIMITS.get(choice, 160))
        else:
            index = _navigate(key, index, len(options))


def _host_details(screen, draft, detection):
    title = "Host details"
    subtitle = "Shown on the SSH dashboard."
    fields = tuple(_CONFIG_FIELDS)
    editors = {field: _InlineField(draft.get(field, "")) for field in fields}
    focus = 0
    while True:
        detection.refresh(draft)
        location = editors["location"]
        if not location.modified and location.value != draft.get("location", ""):
            editors["location"] = location = _InlineField(draft["location"])
        note = "" if location.modified else detection.note
        if screen is None:
            width = shutil.get_terminal_size((80, 24)).columns - 5
            options = [(field, _field_row(label, editors[field].value, width, label_width=11))
                       for field, label in _CONFIG_FIELDS.items()]
            if note:
                field, row = options[-1]
                details = textwrap.wrap(note, max(1, width))
                options[-1] = (field, row + "\n" + textwrap.indent("\n".join(details), " " * 5))
            options.extend([("keep", "Keep changes"), ("cancel", "Cancel")])
            choice = _plain_menu(options, title, subtitle)
            if choice in (None, "cancel"):
                return
            if choice != "keep":
                answer = _plain_input(title, _CONFIG_FIELDS[choice], editors[choice].value)
                if answer is not None:
                    editors[choice] = editor = _InlineField(answer)
                    editor.modified = True
                continue
        else:
            height, width = screen.size()
            shortcuts = _HOST_KEYS
            if height < 14 and width < 56:
                shortcuts = (("Tab", "Next"), ("Enter", "Keep"), ("Esc", ""))
            layout = _layout(height, width, True, shortcuts)
            label_width = max(11, min(24, layout.content_width // 4 - 3))
            rows = [(_field_row(_CONFIG_FIELDS[field], editors[field].value, layout.content_width + 3,
                                editor=editors[field] if i == focus else None,
                                label_width=label_width, full_width=True), "field")
                    for i, field in enumerate(fields)]
            if note:
                rows.append((note, "note"))
            screen.paint(title, subtitle, rows, focus, shortcuts)
            key = screen.key(timeout=100) if detection.lookup is not None else screen.key()
            if key == "\x1b":
                return
            if key in ("\t", curses.KEY_DOWN):
                focus = (focus + 1) % len(fields)
            elif key in (curses.KEY_BTAB, curses.KEY_UP):
                focus = (focus - 1) % len(fields)
            elif key not in _ENTER:
                editors[fields[focus]].key(key)
            if key not in _ENTER:
                continue

        changes = {}
        for i, field in enumerate(fields):
            editor = editors[field]
            if editor.modified and not _save_field(screen, changes, field, _CONFIG_FIELDS[field], editor.value):
                focus = i
                break
        else:
            draft.update(changes)
            if "location" in changes:
                detection.edited()
            return


def _config_options(draft):
    style = "Compact" if draft.get("compact") else "Automatic"
    return [
        ("host", "Host details"),
        ("groups", f"Service groups  ·  {len(draft.get('applications', []))}"),
        ("containers", f"Containers  ·  {_standalone_count(draft, 'containers')} selected"),
        ("layout", f"Display  ·  {style}"),
        ("preview", "Preview and apply"),
        ("back", "Cancel"),
    ]


def _edit(screen, config, items, preview, detection):
    draft = deepcopy(config)
    focus = 0
    while True:
        detection.refresh(draft)
        choice, focus = _form_choice(screen, "Configure Flowvy MOTD", draft,
                                     lambda: _config_options(draft), {}, focus,
                                     sections={"preview": ("", "divider")},
                                     columns={"groups", "containers", "layout"})
        detection.refresh(draft)
        if choice in (None, "back"):
            return None
        if choice == "host":
            _host_details(screen, draft, detection)
        elif choice == "groups":
            _edit_groups(screen, draft, items)
        elif choice == "containers":
            _select_standalone(screen, draft, items, "containers")
        elif choice == "layout":
            draft["compact"] = not draft.get("compact", False)
        elif choice == "preview":
            try:
                output = preview(draft)
            except ValueError as error:
                _show(screen, "Invalid settings", str(error))
                continue
            if not _show(screen, "MOTD preview", output, advance=True):
                continue
            action = _menu(screen, [("back", "Return to configuration"), ("apply", "Save settings")],
                           "Apply configuration")
            if action == "apply":
                return draft


def edit_config(config: dict, items: list["Component"], preview: Callable[[dict], str], *, lookup_factory=None) -> dict | None:
    detection = _LocationDetection(bool(config.get("location")), lookup_factory)
    return _run(lambda screen: _edit(screen, config, items, preview, detection))
