from copy import deepcopy
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flowvy_motd import tui


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

    def test_search_crosses_tabs_and_matches_metadata(self):
        picker = tui._Selection(self.items, [])
        picker.search("NETBIRD")
        self.assertCountEqual([entry.id for entry in picker.results()],
                              [self.peer.id, self.dashboard.id])
        picker.switch()
        self.assertEqual([entry.id for entry in picker.results()], [self.dashboard.id])

    def test_selections_survive_filter_and_missing_items(self):
        picker = tui._Selection(self.items, ["container:gone"])
        picker.toggle(self.ssh.id)
        picker.search("dashboard")
        picker.toggle(self.dashboard.id)
        picker.search("")
        self.assertEqual(picker.selected, ["container:gone", self.ssh.id, self.dashboard.id])

    def test_removing_primary_removes_only_that_group_member(self):
        config = {
            "provider": "Example", "services": ["ssh.service"],
            "containers": [self.dashboard.id], "applications": [{
                "name": "Office", "members": [self.ssh.id, self.dashboard.id],
                "primary": self.ssh.id, "check_url": "",
            }],
        }
        original = deepcopy(config)
        result = tui._replace_selection(config, [self.dashboard.id])
        self.assertEqual(result["services"], [])
        self.assertEqual(result["applications"], [{
            "name": "Office", "members": [self.dashboard.id], "primary": "", "check_url": "",
        }])
        self.assertEqual(config, original)

    def test_selected_compose_and_package_are_separate_group_suggestions(self):
        partner = item("compose:netbird/server", "server", "docker", "netbird")
        groups = tui._suggest_groups([self.peer, partner, self.proxy], [])
        self.assertCountEqual([group["name"] for group in groups],
                              ["netbird (package)", "netbird (Compose)"])

    def test_member_declared_only_in_a_toml_group_stays_selected(self):
        config = {
            "services": ["ssh.service"], "containers": [], "applications": [{
                "name": "NetBird", "members": [self.dashboard.id],
                "primary": self.dashboard.id, "check_url": "",
            }],
        }
        selected = tui._selected_ids(config)
        self.assertEqual(selected, [self.ssh.id, self.dashboard.id])
        self.assertEqual(tui._replace_selection(config, selected), config)


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

    def paint(self, title, subtitle, rows, index, help_text):
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

    def test_preview_escape_returns_to_draft_without_saving(self):
        screen = ScriptedTerminal([tui.curses.KEY_END, "k", "\n", "\x1b", "\x1b"])
        with patch.object(tui, "_run", side_effect=lambda action: action(screen)):
            self.assertIsNone(tui.edit_config({"location": "Existing"}, [], lambda config: "preview"))
        self.assertNotIn("Apply configuration", screen.titles)


class ConfigurationTests(unittest.TestCase):
    APPLY = [tui.curses.KEY_END, "k", "\n", "\n", "j", "\n"]

    def edit(self, config, screen, lookup):
        with patch.object(tui, "_run", side_effect=lambda action: action(screen)):
            return tui.edit_config(config, [], lambda draft: "preview", lookup_factory=lookup)

    def test_provider_edits_inline_and_moves_caret(self):
        screen = ScriptedTerminal(["\n", tui.curses.KEY_HOME, tui.curses.KEY_RIGHT, "e", "\n", *self.APPLY])
        with patch("builtins.input", side_effect=AssertionError("not terminal input")):
            config = {"provider": "Htzner", "location": "Helsinki"}
            result = self.edit(config, screen, lambda: self.fail("existing location wins"))
        self.assertEqual(result["provider"], "Hetzner")
        self.assertEqual(config["provider"], "Htzner")
        frame = next(frame for frame in screen.frames if any("H▌tzner" in line for line, _ in frame[2]))
        self.assertEqual(frame[0], "Configure Flowvy MOTD")
        self.assertTrue(any("Services and containers" in line for line, _ in frame[2]))
        self.assertIn("Esc Cancel", frame[4])

    def test_resolved_location_appears_without_waiting_for_a_key(self):
        screen = ScriptedTerminal([None, *self.APPLY])
        lookup = SimpleNamespace(poll=Mock(
            side_effect=[("pending", ""), ("detected", "Helsinki, Finland")]))
        factory = Mock(return_value=lookup)
        result = self.edit({"location": ""}, screen, factory)
        self.assertEqual(result["location"], "Helsinki, Finland")
        self.assertEqual(screen.timeouts[0], 100)
        self.assertIn("Detecting location", str(screen.frames))
        self.assertIn("Auto-detected from this server", str(screen.frames))
        factory.assert_called_once_with()

    def test_explicit_clear_wins_over_pending_detection(self):
        screen = ScriptedTerminal(["j", "j", "\n", "\x15", "\n", *self.APPLY])
        lookup = SimpleNamespace(poll=Mock(
            side_effect=[("pending", "")] * 4 + [("detected", "Paris, France")]))
        result = self.edit({"location": ""}, screen, lambda: lookup)
        self.assertEqual(result["location"], "")
        self.assertNotIn("Paris, France", str(screen.frames))

    def test_escape_cancels_only_the_field_and_resumes_lookup(self):
        screen = ScriptedTerminal(["j", "j", "\n", "X", "\x1b", *self.APPLY])
        lookup = SimpleNamespace(poll=Mock(
            side_effect=[("pending", "")] * 4 + [("detected", "Paris, France")]))
        result = self.edit({"location": ""}, screen, lambda: lookup)
        self.assertEqual(result["location"], "Paris, France")
        self.assertIn("X▌", str(screen.frames))

    def test_pristine_location_editor_shows_detection_while_open(self):
        screen = ScriptedTerminal(["j", "j", "\n", None, "\n", *self.APPLY])
        lookup = SimpleNamespace(poll=Mock(
            side_effect=[("pending", "")] * 4 + [("detected", "Helsinki, Finland")]))
        result = self.edit({"location": ""}, screen, lambda: lookup)
        self.assertEqual(result["location"], "Helsinki, Finland")
        self.assertIn("[Helsinki, Finland▌]", str(screen.frames))

    @patch("builtins.input", side_effect=["7", "2"])
    def test_plain_terminal_refreshes_detection_before_preview(self, read):
        lookup = SimpleNamespace(poll=Mock(side_effect=[("pending", ""), ("detected", "Helsinki, Finland")]))
        with patch.object(tui.sys, "stdout", io.StringIO()), patch.object(tui.sys, "stdin", io.StringIO()):
            result = self.edit({"location": ""}, None, lambda: lookup)
        self.assertEqual(result["location"], "Helsinki, Finland")

    def test_narrow_field_keeps_the_caret_visible(self):
        screen = ScriptedTerminal(["\n", "\x1b", "\x1b"], width=26)
        config = {"provider": "A very long provider name", "location": "Existing"}
        self.assertIsNone(self.edit(config, screen, lambda: self.fail("existing location wins")))
        row = next(line for _, _, rows, _, _ in screen.frames for line, _ in rows if "▌" in line)
        self.assertLessEqual(len(row), screen.width - 3)
        self.assertTrue(row.startswith("Provider"))

    @patch("builtins.input", side_effect=["3", "Manual city", "7", "2"])
    def test_plain_terminal_offers_manual_location_after_failed_detection(self, read):
        lookup = SimpleNamespace(poll=lambda: ("unavailable", ""))
        with patch.object(tui.sys, "stdout", io.StringIO()) as stream, patch.object(tui.sys, "stdin", io.StringIO()):
            result = self.edit({"location": ""}, None, lambda: lookup)
        self.assertEqual(result["location"], "Manual city")
        self.assertIn("Could not detect location. Enter it manually.", stream.getvalue())
