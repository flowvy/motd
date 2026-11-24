from copy import deepcopy
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

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
    def __init__(self, keys):
        self.keys = iter(keys)
        self.titles = []

    def size(self):
        return 24, 80

    def paint(self, title, subtitle, rows, index, help_text):
        self.titles.append(title)

    def key(self):
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
            self.assertIsNone(tui.edit_config({}, [], lambda config: "preview"))
        self.assertNotIn("Apply configuration", screen.titles)
