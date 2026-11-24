import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flowvy_motd import cli, tui
from flowvy_motd.dashboard import COLORS, RESET


def item(id, name, source, group="", search_text="", description=""):
    return SimpleNamespace(
        id=id, name=name, source=source, group=group, search_text=search_text,
        description=description, state="running", detail="", schedule="",
        group_kind="compose" if source == "docker" else "package",
    )


class PickerTests(unittest.TestCase):
    def setUp(self):
        self.ssh = item("unit:ssh.service", "ssh.service", "systemd", "openssh-server")
        self.peer = item("unit:office.service", "office.service", "systemd", "netbird",
                         description="NetBird mesh network client")
        self.dashboard = item("compose:vpn/dashboard", "dashboard", "docker", "vpn",
                              search_text="netbirdio/dashboard")
        self.proxy = item("container:proxy", "proxy", "docker")
        self.items = [self.ssh, self.peer, self.dashboard, self.proxy]

    def test_search_matches_descriptions_packages_and_images_in_its_picker(self):
        picker = tui._Selection(self.items, [])
        self.assertCountEqual([entry.id for entry in picker.results("NETBIRD")],
                              [self.peer.id, self.dashboard.id])
        self.assertEqual([entry.id for entry in picker.results("openssh")], [self.ssh.id])
        self.assertEqual([entry.id for entry in picker.results("netbirdio/dashboard")], [self.dashboard.id])

    def test_selections_survive_filter_and_missing_items(self):
        available = tui._with_missing(self.items, ["container:gone"])
        picker = tui._Selection(available, ["container:gone"])
        picker.toggle(self.ssh.id)
        picker.toggle(picker.results("dashboard")[0].id)
        self.assertEqual(picker.selected, ["container:gone", self.ssh.id, self.dashboard.id])
        self.assertEqual(picker.results("gone")[0].state, "missing")

    def test_same_package_and_compose_names_have_distinct_labels(self):
        partner = item("compose:netbird/server", "server", "docker", "netbird")
        rows, _ = tui._picker_rows([self.peer, partner], [])
        self.assertIn(("netbird · package", "section"), rows)
        self.assertIn(("netbird · Compose", "section"), rows)


class FallbackMenuTests(unittest.TestCase):
    @patch("builtins.input", side_effect=["2"])
    def test_menu_can_be_used_without_cursor_controls(self, read):
        with patch.object(tui.sys, "stdout", io.StringIO()):
            result = tui._plain_menu([("show", "Show MOTD"), ("edit", "Configure")],
                                     "Flowvy MOTD")
        self.assertEqual(result, "edit")

    @patch("builtins.input", side_effect=EOFError)
    def test_eof_cancels(self, read):
        with patch.object(tui.sys, "stdout", io.StringIO()):
            result = tui._plain_menu([("remove", "Uninstall")], "Confirm removal")
        self.assertIsNone(result)


class ScriptedTerminal:
    def __init__(self, keys, width=80):
        self.keys = iter(keys)
        self.titles = []
        self.frames = []
        self.timeouts = []
        self.width = width

    def size(self):
        return 24, self.width

    def paint(self, title, subtitle, rows, index, help_text, **kwargs):
        self.titles.append(title)
        self.frames.append((title, subtitle, rows, index, help_text))

    def key(self, timeout=None):
        self.timeouts.append(timeout)
        return next(self.keys)


class ConfirmationTests(unittest.TestCase):
    def test_leaving_the_removal_file_list_does_not_enter_confirmation(self):
        screen = ScriptedTerminal(["\x1b", "j", "\n"])
        with patch.object(tui, "_run", side_effect=lambda action: action(screen)):
            self.assertIsNone(tui.confirm_uninstall(["/etc/flowvy-motd/config.toml"]))
        self.assertEqual(screen.titles, ["Flowvy MOTD files"])
        self.assertIn(("Enter", "Continue"), screen.frames[0][4])

    def test_preview_escape_returns_to_draft_without_saving(self):
        screen = ScriptedTerminal([tui.curses.KEY_END, "k", "\n", "\x1b", "\x1b"])
        with patch.object(tui, "_run", side_effect=lambda action: action(screen)):
            self.assertIsNone(tui.edit_config({"location": "Existing"}, [], lambda config: "preview"))
        self.assertNotIn("Apply configuration", screen.titles)
        preview = next(frame for frame in screen.frames if frame[0] == "MOTD preview")
        self.assertIn(("Enter", "Continue"), preview[4])


class ScreenLayoutTests(unittest.TestCase):
    def test_footer_wraps_whole_key_and_action_pairs(self):
        keys = (("Space", "Toggle"), ("/", "Filter"), ("Enter", "Keep"), ("Esc", "Back"))
        lines = tui._shortcut_lines(keys, width=30, limit=2)
        self.assertEqual(lines, [list(keys[:2]), list(keys[2:])])
        compact = tui._shortcut_lines(keys, width=24, limit=1)
        self.assertEqual(compact, [[("Enter", ""), ("Esc", "")]])
        self.assertEqual(tui._shortcut_lines(keys, width=27, limit=1), [[(key, "") for key, _ in keys]])

    def test_keycaps_have_separate_attributes_from_their_labels(self):
        window = Mock()
        window.getmaxyx.return_value = (24, 80)
        with patch.object(tui.curses, "has_colors", return_value=False):
            screen = tui._Terminal(window)
        screen.paint("Main menu", "", [("Exit", "plain")], 0, (("Enter", "Select"), ("Esc", "Back")))
        calls = [call.args for call in window.addnstr.call_args_list]
        enter = next(args for args in calls if args[2] == " Enter ")
        select = next(args for args in calls if args[2] == "Select")
        self.assertEqual(enter[0], select[0])
        self.assertGreater(select[1], enter[1] + len(enter[2]))
        self.assertNotEqual(enter[4], select[4])

    def test_menu_and_picker_use_the_same_terminal_frame_as_host_details(self):
        window = Mock()
        window.getmaxyx.return_value = (24, 80)
        with patch.object(tui.curses, "has_colors", return_value=False):
            screen = tui._Terminal(window)
        for name in ("Main menu", "Host details", "Select containers", "Diagnostics"):
            with self.subTest(name=name):
                window.reset_mock()
                screen.paint(name, "", [("First row", "plain")], 0, (("Esc", "Back"),))
                calls = window.addnstr.call_args_list
                self.assertTrue(any(c.args[2].startswith("╭") for c in calls))
                self.assertTrue(any(c.args[2].startswith("╰") for c in calls))
                self.assertTrue(any("Flowvy MOTD" in c.args[2] for c in calls))
                self.assertTrue(any(c.args[2] == " Esc " for c in calls))

    def test_deletion_and_keep_are_distinct_in_a_narrow_confirmation(self):
        window = Mock()
        window.getmaxyx.return_value = (24, 32)
        window.get_wch.return_value = "\x1b"
        with patch.object(tui.curses, "has_colors", return_value=False):
            screen = tui._Terminal(window)
        with patch.object(tui, "_run", side_effect=lambda action: action(screen)):
            self.assertIsNone(tui.confirm_uninstall())
        lines = [call.args[2] for call in window.addnstr.call_args_list]
        for value in ("Remove settings", "Keep settings", " Esc ", "Back"):
            self.assertTrue(any(value in line for line in lines))

    def test_same_compose_service_names_remain_distinguishable_without_group_headings(self):
        options = [item("compose:a/web", "web", "docker", "a"),
                   item("compose:b/web", "web", "docker", "b")]
        rows, indexes = tui._picker_rows(options, [])
        self.assertIn("a/web", rows[indexes[0]][0].label)
        self.assertIn("b/web", rows[indexes[1]][0].label)

    def test_status_column_does_not_hide_systemd_instance_suffixes(self):
        window = Mock()
        window.getmaxyx.return_value = (24, 80)
        with patch.object(tui.curses, "has_colors", return_value=False):
            screen = tui._Terminal(window)
        names = [f"distributed-worker-with-a-long-template@{region}.service" for region in ("west", "east")]
        options = [item(f"unit:{name}", name, "systemd", "worker") for name in names]
        rows, indexes = tui._picker_rows(options, [])
        screen.paint("Select services", "2 shown", rows, indexes[0], tui._PICKER_KEYS)
        lines = [call.args[2] for call in window.addnstr.call_args_list]
        self.assertTrue(any("@west.service" in line for line in lines))
        self.assertTrue(any("@east.service" in line for line in lines))

    def test_short_component_keeps_status_on_a_compact_screen(self):
        window = Mock()
        window.getmaxyx.return_value = (10, 32)
        with patch.object(tui.curses, "has_colors", return_value=False):
            screen = tui._Terminal(window)
        rows, indexes = tui._picker_rows([item("container:web", "web", "docker")], [])
        screen.paint("Select containers", "1 shown", rows, indexes[0], tui._PICKER_KEYS)
        lines = [call.args[2] for call in window.addnstr.call_args_list]
        self.assertIn("[ ] web · Running", lines)
        self.assertIn(" Space ", lines)
        self.assertIn(" / ", lines)

    def test_viewer_wraps_wide_text_and_retains_status_tones(self):
        result = tui._body_rows(" " + COLORS["warn"] + "測試x" + RESET + "\x1b[2J\nend", 4)
        self.assertEqual(result, [
            ([(" ", "plain"), ("測", "warn")], "body"),
            ([("試x", "warn")], "body"),
            ([("end", "plain")], "body"),
        ])

    def test_viewer_pages_using_the_shared_frame_height(self):
        screen = ScriptedTerminal([tui.curses.KEY_NPAGE, "\n"])
        self.assertTrue(tui._show(screen, "Diagnostics", "\n".join(f"Line {n}" for n in range(30))))
        for frame in screen.frames:
            self.assertEqual(len(frame[2]), 14)
        self.assertIn("Line 14", str(screen.frames[1]))
        self.assertNotIn("Line 13", str(screen.frames[1]))

    @patch.object(cli, "preview_prompt", return_value="dashboard")
    @patch.object(cli, "load", return_value={})
    @patch.object(cli.tui, "show_text")
    @patch.object(cli.tui, "menu", side_effect=["show", "exit"])
    def test_show_motd_uses_the_shared_viewer(self, menu, show_text, load, preview):
        cli.menu()
        show_text.assert_called_once_with("MOTD", "dashboard")

    @patch.dict(cli.os.environ, {"TERM": "dumb"})
    @patch.object(cli, "snapshot", return_value=({"host": "edge", "os": "Debian"}, {}, {}, {}))
    def test_plain_terminal_keeps_an_ascii_dashboard(self, snapshot):
        result = cli.preview_prompt({})
        self.assertTrue(result.isascii())
        self.assertIn("FLOWVY | edge", result)
        self.assertNotIn("\x1b", result)


class ConfigurationTests(unittest.TestCase):
    APPLY = [tui.curses.KEY_END, "k", "\n", "\n", "j", "\n"]

    def edit(self, config, screen, lookup):
        with patch.object(tui, "_run", side_effect=lambda action: action(screen)):
            return tui.edit_config(config, [], lambda draft: "preview", lookup_factory=lookup)

    def test_host_details_edits_without_arming_field_and_tabs_in_both_directions(self):
        screen = ScriptedTerminal([
            "\n", tui.curses.KEY_HOME, tui.curses.KEY_RIGHT, "e",
            "\t", *" proxy", tui.curses.KEY_BTAB, tui.curses.KEY_END, "r", "\n", *self.APPLY,
        ])
        config = {"provider": "Htzne", "role": "Edge", "location": "Helsinki"}
        result = self.edit(config, screen, lambda: self.fail("existing location wins"))
        self.assertEqual(result, {"provider": "Hetzner", "role": "Edge proxy",
                                  "location": "Helsinki"})
        self.assertEqual(config["provider"], "Htzne")
        frame = next(frame for frame in screen.frames if frame[0] == "Host details")
        self.assertEqual(frame[1], "Shown on the SSH dashboard.")
        self.assertIn(("Tab", "Next field"), frame[4])
        self.assertIn(("Enter", "Keep changes"), frame[4])
        self.assertIn(("Esc", "Cancel"), frame[4])
        self.assertNotIn("Service groups", str(frame))

    def test_location_detection_is_shown_with_the_field_while_it_has_focus(self):
        screen = ScriptedTerminal(["\n", "\t", "\t", None, "\n", *self.APPLY])
        lookup = SimpleNamespace(poll=Mock(side_effect=[
            ("pending", ""), ("pending", ""), ("pending", ""), ("detected", "Helsinki, Finland"),
        ]))
        factory = Mock(return_value=lookup)
        result = self.edit({"location": ""}, screen, factory)
        self.assertEqual(result["location"], "Helsinki, Finland")
        self.assertIn(100, screen.timeouts)
        for value in ("Detecting location", "Auto-detected from this server"):
            self.assertIn(value, str(screen.frames))
        frame = next(frame for frame in screen.frames if "Helsinki, Finland▌" in str(frame))
        note_index = next(i for i, (row, _) in enumerate(frame[2])
                          if "Auto-detected" in row)
        self.assertIn("Location", frame[2][note_index - 1][0])
        factory.assert_called_once_with()

    def test_explicit_clear_wins_over_pending_detection(self):
        screen = ScriptedTerminal(["\n", "\t", "\t", "\x15", "\n", *self.APPLY])
        lookup = SimpleNamespace(poll=Mock(side_effect=[
            ("pending", "")] * 6 + [("detected", "Paris, France")]))
        result = self.edit({"location": ""}, screen, lambda: lookup)
        self.assertEqual(result["location"], "")
        self.assertNotIn("Paris, France", str(screen.frames))

    def test_escape_discards_all_host_edits_and_resumes_lookup(self):
        screen = ScriptedTerminal(["\n", "X", "\t", "Y", "\t", "Z", "\x1b", *self.APPLY])
        lookup = SimpleNamespace(poll=Mock(side_effect=[
            ("pending", "")] * 7 + [("detected", "Paris, France")]))
        config = {"provider": "Hetzner", "role": "Edge proxy", "location": ""}
        result = self.edit(config, screen, lambda: lookup)
        self.assertEqual(result, {**config, "location": "Paris, France"})
        self.assertIn("Z▌", str(screen.frames))

    @patch("builtins.input", side_effect=["5", "2"])
    def test_plain_terminal_refreshes_detection_before_preview(self, read):
        lookup = SimpleNamespace(poll=Mock(side_effect=[("pending", ""), ("detected", "Helsinki, Finland")]))
        with patch.object(tui.sys, "stdout", io.StringIO()), patch.object(tui.sys, "stdin", io.StringIO()):
            result = self.edit({"location": ""}, None, lambda: lookup)
        self.assertEqual(result["location"], "Helsinki, Finland")

    def test_narrow_field_keeps_the_caret_visible(self):
        screen = ScriptedTerminal(["\n", "\x1b", "\x1b"], width=26)
        config = {"provider": "A very long provider name", "location": "Existing"}
        self.assertIsNone(self.edit(config, screen, lambda: self.fail("existing location wins")))
        row = next(line for _, _, rows, _, _ in screen.frames for line, _ in rows if "▌" in str(line))
        self.assertLessEqual(len(row), screen.width - 3)
        self.assertTrue(row.startswith("Provider"))

    @patch("builtins.input", side_effect=["1", "3", "Manual city", "4", "5", "2"])
    def test_plain_terminal_offers_manual_location_after_failed_detection(self, read):
        lookup = SimpleNamespace(poll=lambda: ("unavailable", ""))
        with patch.object(tui.sys, "stdout", io.StringIO()) as stream, patch.object(tui.sys, "stdin", io.StringIO()):
            result = self.edit({"location": ""}, None, lambda: lookup)
        self.assertEqual(result["location"], "Manual city")
        self.assertIn("Could not detect location. Enter it manually.", stream.getvalue())

    @patch("builtins.input", side_effect=["1", "3", "", "4", "5", "2"])
    def test_plain_terminal_blank_reply_does_not_cancel_pending_location(self, read):
        lookup = SimpleNamespace(poll=Mock(side_effect=[
            ("pending", ""), ("pending", ""), ("pending", ""), ("detected", "Helsinki, Finland"),
        ]))
        with patch.object(tui.sys, "stdout", io.StringIO()):
            result = self.edit({"location": ""}, None, lambda: lookup)
        self.assertEqual(result["location"], "Helsinki, Finland")

    def test_short_terminal_keeps_focused_location_and_its_note_visible(self):
        window = Mock()
        window.getmaxyx.return_value = (10, 40)
        with patch.object(tui.curses, "has_colors", return_value=False):
            screen = tui._Terminal(window)
        rows = [(tui._field_row(label, "Helsinki, Finland", 38), "field")
                for label in tui._CONFIG_FIELDS.values()]
        rows.append((tui._LOCATION_NOTES["detected"], "note"))
        screen.paint("Host details", "Shown on the SSH dashboard.", rows, 2,
                     tui._HOST_KEYS)
        drawn = [call.args[2] for call in window.addnstr.call_args_list]
        self.assertTrue(any("Location" in line for line in drawn))
        self.assertTrue(any("Auto-detected" in line for line in drawn))
        self.assertTrue(any("public IP." in line for line in drawn))


class ScopedConfigurationTests(unittest.TestCase):
    APPLY = ConfigurationTests.APPLY

    def setUp(self):
        self.ssh = item("unit:ssh.service", "ssh.service", "systemd", "openssh-server")
        self.dashboard = item("compose:vpn/dashboard", "dashboard", "docker", "vpn")
        self.cache = item("container:redis-cache", "redis-cache", "docker")
        self.items = [self.ssh, self.dashboard, self.cache]

    def edit(self, config, screen):
        with patch.object(tui, "_run", side_effect=lambda action: action(screen)):
            return tui.edit_config(config, self.items, lambda draft: "preview", lookup_factory=lambda: None)

    def test_group_creation_selects_services_without_top_level_selection(self):
        screen = ScriptedTerminal([
            "j", "\n", "\n", "\n", *"Remote access", "\n",
            "j", "\n", " ", "\n", "j", "\n", "j", "\n", "j", "\n",
            *"http://localhost/health", "\n", "j", "\n", "\x1b", *self.APPLY,
        ])
        result = self.edit({"location": "Existing", "services": []}, screen)
        self.assertEqual(result["services"], [])
        self.assertEqual(result["applications"], [{
            "name": "Remote access", "members": [self.ssh.id],
            "primary": self.ssh.id, "check_url": "http://localhost/health",
        }])
        frames = [frame for frame in screen.frames if frame[0] == "Create service group"]
        self.assertTrue(any("Group name" in str(line) and "[" in str(line) and "▌" in str(line)
                            for frame in frames for line, _ in frame[2]))
        self.assertTrue(any("Local HTTP check" in str(line) and "[" in str(line) and "▌" in str(line)
                            for frame in frames for line, _ in frame[2]))

    def test_existing_configured_service_can_join_a_group_when_missing(self):
        draft = {"services": ["old.service"]}
        screen = ScriptedTerminal([
            "\n", "\n", *"Legacy service", "\n", "j", "\n", " ", "\n",
            "j", "j", "j", "\n", "\x1b",
        ])
        tui._edit_groups(screen, draft, [])
        self.assertEqual(draft["applications"][0]["members"], ["unit:old.service"])

    def test_docker_list_filters_live_without_services_or_tabs(self):
        screen = ScriptedTerminal([
            "j", "j", "\n", "/", "d", "a", "\n", " ", "\n", *self.APPLY,
        ], width=40)
        result = self.edit({"location": "Existing"}, screen)
        self.assertEqual(result["containers"], [self.dashboard.id])
        frames = [frame for frame in screen.frames if frame[0] == "Select containers"]
        filtered = next(frame for frame in frames if any("da▌" in str(line) for line, _ in frame[2]))
        self.assertTrue(any("dashboard" in str(line) for line, _ in filtered[2]))
        self.assertFalse(any("redis-cache" in str(line) for line, _ in filtered[2]))
        self.assertTrue(all("ssh.service" not in str(frame) and "Tab" not in str(frame) for frame in frames))

    def test_deselecting_a_standalone_container_preserves_legacy_groups(self):
        config = {"location": "Existing", "services": ["ssh.service"],
                  "containers": [self.dashboard.id, "container:standalone"],
                  "applications": [
                      {"name": "Mixed", "members": [self.ssh.id, self.dashboard.id],
                       "primary": self.dashboard.id, "check_url": ""},
                      {"name": "Only containers", "members": [self.cache.id], "primary": "", "check_url": ""},
                  ]}
        screen = ScriptedTerminal(["j", "j", "\n", "\n", *self.APPLY])
        self.assertEqual(self.edit(config, screen), config)
        screen = ScriptedTerminal(["j", "j", "\n", " ", "\n", *self.APPLY])
        self.assertEqual(self.edit(config, screen), {**config, "containers": [self.dashboard.id]})
        frames = [frame for frame in screen.frames if frame[0] == "Select containers"]
        self.assertNotIn("dashboard", str(frames))
        self.assertNotIn("redis-cache", str(frames))

    def test_standalone_services_hide_grouped_ids_without_modifying_them(self):
        config = {"location": "Existing", "services": ["ssh.service", "cron.service"], "applications": [
            {"name": "Remote access", "members": [self.ssh.id], "primary": self.ssh.id, "check_url": ""},
        ]}
        screen = ScriptedTerminal([
            "j", "\n", "j", "j", "\n", " ", "\n", "\x1b", *self.APPLY,
        ])
        self.assertEqual(self.edit(config, screen), {**config, "services": ["ssh.service"]})
        frames = [frame for frame in screen.frames if frame[0] == "Select standalone services"]
        self.assertNotIn("ssh.service", str(frames))
        self.assertIn("[x] cron.service", str(frames))

    def test_rename_standalone_service_and_filter_by_display_name(self):
        draft = {"services": ["ssh.service"]}
        screen = ScriptedTerminal(["r", *"Remote shell", "\n", "/", *"Remote shell", "\n", "\n"])
        tui._select_standalone(screen, draft, self.items, "services")
        self.assertEqual(draft, {"services": ["ssh.service"], "service_labels": {"ssh.service": "Remote shell"}})
        self.assertIn("ssh.service", str(next(frame for frame in screen.frames if frame[0] == "Rename service")))
        self.assertIn("1 shown", screen.frames[-1][1])

    def test_service_display_name_can_be_reset_or_discarded(self):
        for finish, expected in (("\x1b", {"ssh.service": "Remote shell"}), ("\n", {})):
            with self.subTest(finish=finish):
                draft = {"services": ["ssh.service"], "service_labels": {"ssh.service": "Remote shell"}}
                screen = ScriptedTerminal(["r", "\x15", "\n", finish])
                tui._select_standalone(screen, draft, self.items, "services")
                self.assertEqual(draft.get("service_labels", {}), expected)

    def test_renaming_a_service_keeps_it_focused_when_the_filter_no_longer_matches(self):
        draft = {"services": ["ssh.service"], "service_labels": {"ssh.service": "Remote shell"}}
        screen = ScriptedTerminal(["/", *"Remote", "\n", "r", "\x15", *"Maintenance", "\n", "\n"])
        tui._select_standalone(screen, draft, self.items, "services")
        self.assertEqual(draft["service_labels"], {"ssh.service": "Maintenance"})
        self.assertIn("[x] ssh.service", str(screen.frames[-1]))

    @patch("builtins.input", side_effect=["3", "1", "Remote shell", "4"])
    def test_plain_standalone_services_can_be_renamed(self, read):
        draft = {"services": ["ssh.service"]}
        with patch.object(tui.sys, "stdout", io.StringIO()) as output:
            tui._select_standalone(None, draft, self.items, "services")
        self.assertEqual(draft["service_labels"], {"ssh.service": "Remote shell"})
        self.assertIn("Rename service › ssh.service › Display name", output.getvalue())

    def test_removing_only_the_group_keeps_monitored_members(self):
        groups = [
            {"name": "Legacy", "members": [self.ssh.id, self.dashboard.id, self.cache.id],
             "primary": self.ssh.id, "check_url": ""},
            {"name": "Cache", "members": [self.cache.id], "primary": "", "check_url": ""},
        ]
        config = {"location": "Existing", "services": ["cron.service"], "containers": [],
                  "applications": groups}
        screen = ScriptedTerminal([
            "j", "\n", "j", "\n", tui.curses.KEY_END, "k", "\n", "\x1b", *self.APPLY,
        ])
        self.assertEqual(self.edit(config, screen), {
            **config, "services": ["cron.service", "ssh.service"], "containers": [self.dashboard.id],
            "applications": groups[1:],
        })

    def test_empty_docker_picker_shows_filter_and_empty_state(self):
        screen = ScriptedTerminal(["\x1b"], width=40)
        self.assertIsNone(tui._picker(screen, [], [], "Select containers", "containers"))
        title, note, rows, _, _ = screen.frames[0]
        self.assertEqual(title, "Select containers")
        self.assertIn("0 selected", note)
        self.assertTrue(any("Filter" in line and "[" in line for line, _ in rows))
        self.assertTrue(any("No containers found" in line for line, _ in rows))

    def test_filter_escape_restores_the_previous_query(self):
        screen = ScriptedTerminal(["/", *"dash", "\n", "/", "\x15", *"redis", "\x1b", " ", "\n"])
        result = tui._picker(screen, [self.dashboard, self.cache], [], "Select containers", "containers")
        self.assertEqual(result, [self.dashboard.id])
        self.assertIn("redis▌", str(screen.frames))

    def test_filter_escape_preserves_focus_when_it_was_filtered_out(self):
        screen = ScriptedTerminal(["j", "/", *"dash", "\x1b", " ", "\n"])
        second = item("container:second", "second", "docker", "vpn")
        result = tui._picker(screen, [self.dashboard, second], [], "Select containers", "containers")
        self.assertEqual(result, [second.id])

    def test_existing_group_members_keep_their_scope_and_primary(self):
        group = {"name": "Legacy", "members": [self.ssh.id, self.dashboard.id],
                 "primary": self.dashboard.id, "check_url": ""}
        eligible = tui._with_missing([self.ssh], group["members"])
        keys = ["j", "\n", "\n", "j", "j", "j", "\n"]
        screen = ScriptedTerminal(keys)
        self.assertEqual(tui._group_editor(screen, group, eligible, set(), True), group)
        changed_keys = ["j", "\n", " ", "\n", "j", "j", "j", "\n"]
        changed_screen = ScriptedTerminal(changed_keys)
        self.assertEqual(tui._group_editor(changed_screen, group, eligible, set(), True),
                         {**group, "members": [self.ssh.id], "primary": ""})
        self.assertTrue(any("[x] vpn/dashboard" in str(line) for title, _, rows, _, _ in screen.frames
                            if title == "Select members" for line, _ in rows))

    def test_local_check_validation_does_not_replace_an_existing_url(self):
        group = {"name": "Remote access", "members": [self.ssh.id],
                 "primary": "", "check_url": "http://localhost/previous"}
        screen = ScriptedTerminal([
            "j", "j", "j", "\n", "\x15", *"http://example.com", "\n", "\n", "\x1b", "j", "\n",
        ])
        self.assertEqual(tui._group_editor(screen, group, [self.ssh], set(), True), group)
        self.assertIn("Invalid local http check", screen.titles)
        error_index = screen.titles.index("Invalid local http check")
        self.assertIn("http://example.com▌", str(screen.frames[error_index + 1]))

    @patch("builtins.input", side_effect=["1", "Remote access", "2", "1", "3", "4", "http://localhost/health", "5"])
    def test_plain_group_fields_have_labeled_frames_and_context(self, read):
        group = {"name": "", "members": [], "primary": "", "check_url": ""}
        with patch.object(tui.sys, "stdout", io.StringIO()) as stream:
            result = tui._group_editor(None, group, [self.ssh], set(), False)
        self.assertEqual(result, {**group, "name": "Remote access", "members": [self.ssh.id],
                                  "check_url": "http://localhost/health"})
        form = stream.getvalue()
        self.assertIn("Create service group › Group name", form)
        self.assertIn("Create service group › Local HTTP check", form)
        self.assertIn("Filter  [ Search by name", form)

    @patch("builtins.input", side_effect=["1", "1", ":cancel", "5", "6"])
    @patch("flowvy_motd.tui.shutil.get_terminal_size", return_value=SimpleNamespace(columns=40))
    def test_plain_numbered_field_rows_fit_a_40_column_terminal(self, size, read):
        with patch.object(tui.sys, "stdout", io.StringIO()) as stream:
            self.assertIsNone(self.edit({"location": "Helsinki", "provider": "Long provider"}, None))
        fields = [line for line in stream.getvalue().splitlines() if "[" in line and "]" in line]
        self.assertTrue(fields)
        self.assertTrue(all(len(line) <= 40 for line in fields))
