import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from flowvy_motd import lifecycle


DEBIAN_PAM = (
    b"@include common-auth\n"
    b"session    optional     pam_motd.so  motd=/run/motd.dynamic\n"
    b"session    optional     pam_motd.so noupdate\n"
    b"@include common-session\n"
)


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        base = self.root / "bin"
        base.mkdir()
        profile = self.root / "profile.d"
        profile.mkdir()
        self.bin = base / "flowvy-motd"
        self.hook = profile / "flowvy-motd.sh"
        self.config = self.root / "flowvy-motd" / "config.toml"
        self.sshd_pam = self.root / "sshd"
        self.systemd = self.root / "systemd"
        self.systemd.mkdir()
        self.systemd_running = self.root / "systemd-running"
        patcher = patch.multiple(lifecycle, BIN=self.bin, HOOK=self.hook, CONFIG=self.config,
                                 SYSTEMD_DIR=self.systemd, SYSTEMD_RUNNING=self.systemd_running,
                                 DOCKER_CACHE=self.root / "run" / "docker-status.json",
                                 SSHD_PAM=self.sshd_pam, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        platform = patch("platform.freedesktop_os_release", return_value={"ID": "debian", "VERSION_ID": "13"})
        platform.start()
        self.addCleanup(platform.stop)

    def bundle(self, version, *, name="candidate.pyz"):
        artifact = self.root / name
        artifact.write_bytes(b"#!/usr/bin/python3\n")
        with zipfile.ZipFile(artifact, "a") as archive:
            archive.writestr("__main__.py", "print('test')")
            archive.writestr("flowvy_motd/__init__.py", f'__version__ = "{version}"\n')
            archive.writestr("flowvy_motd/lifecycle.py", "# test")
        return artifact

    def existing_config(self, content="# Managed by Flowvy MOTD\nrole = 'web'\n"):
        self.config.parent.mkdir(exist_ok=True)
        self.config.write_text(content)

    def test_install_only_publishes_executable_and_can_repeat(self):
        candidate = self.bundle("0.1.0")
        lifecycle.install_from(candidate)
        before = self.bin.stat().st_ino
        lifecycle.install_from(candidate)
        self.assertEqual(self.bin.stat().st_ino, before)
        self.assertEqual(self.bin.stat().st_mode & 0o777, 0o755)
        self.assertFalse(self.hook.exists())
        self.assertFalse(self.config.exists())
        self.assertEqual(list(self.bin.parent.iterdir()), [self.bin])

    def test_foreign_or_symlink_install_path_is_preserved(self):
        candidate = self.bundle("0.1.0")
        self.bin.write_text("foreign")
        with self.assertRaises(ValueError):
            lifecycle.install_from(candidate)
        self.assertEqual(self.bin.read_text(), "foreign")
        self.bin.unlink()
        self.bin.symlink_to(candidate)
        with self.assertRaises(ValueError):
            lifecycle.install_from(candidate)
        self.assertTrue(self.bin.is_symlink())

    def test_hook_rejects_foreign_content_and_requires_installed_binary(self):
        with self.assertRaises(ValueError):
            lifecycle.set_hook(True)
        lifecycle.install_from(self.bundle("0.1.0"))
        self.hook.write_text("# administrator's hook\n")
        self.assertEqual(lifecycle.hook_status(), "modified")
        with self.assertRaises(ValueError):
            lifecycle.set_hook(True)
        with self.assertRaises(ValueError):
            lifecycle.set_hook(False)
        self.assertIn("administrator", self.hook.read_text())

    def test_ssh_enable_and_disable_manage_docker_collector_units(self):
        self.systemd_running.mkdir()
        lifecycle.install_from(self.bundle("0.1.0"))
        with patch.object(lifecycle, "_systemctl", create=True) as systemctl:
            lifecycle.set_hook(True)
            self.assertTrue((self.systemd / "flowvy-motd-docker.timer").is_file())
            self.assertTrue((self.systemd / "flowvy-motd-docker.service").is_file())
            systemctl.assert_any_call("enable", "--now", "flowvy-motd-docker.timer")
            lifecycle.set_hook(False)
            systemctl.assert_any_call("disable", "--now", "flowvy-motd-docker.timer")
            systemctl.assert_any_call("stop", "flowvy-motd-docker.service")
        self.assertEqual(list(self.systemd.iterdir()), [])
        self.assertFalse(self.hook.exists())

    def test_enable_hides_both_stock_debian_motd_rules_and_disable_restores_them(self):
        lifecycle.install_from(self.bundle("0.1.0"))
        self.sshd_pam.write_bytes(DEBIAN_PAM)
        self.sshd_pam.chmod(0o640)

        lifecycle.set_hook(True)
        hidden = self.sshd_pam.read_bytes()
        self.assertEqual(hidden.count(b"motd=/dev/null"), 2)
        self.assertEqual(hidden.count(b"\nsession"), 2)
        self.assertEqual(self.sshd_pam.stat().st_mode & 0o777, 0o640)
        inode = self.sshd_pam.stat().st_ino
        lifecycle.set_hook(True)
        self.assertEqual(self.sshd_pam.stat().st_ino, inode)

        lifecycle.set_hook(False)
        self.assertEqual(self.sshd_pam.read_bytes(), DEBIAN_PAM)

    def test_uninstall_restores_only_its_own_debian_motd_rules(self):
        lifecycle.install_from(self.bundle("0.1.0"))
        previous = DEBIAN_PAM.replace(b"session    optional     pam_motd.so noupdate",
                                      b"# session    optional     pam_motd.so noupdate")
        custom = b"session optional pam_motd.so motd=/etc/ops-notice\n"
        self.sshd_pam.write_bytes(previous + custom)
        lifecycle.set_hook(True)
        self.assertNotEqual(self.sshd_pam.read_bytes(), previous + custom)
        self.assertTrue(self.sshd_pam.read_bytes().endswith(custom))
        with self.sshd_pam.open("ab") as current:
            current.write(b"# A later administrator edit\n")

        lifecycle.uninstall(False)
        self.assertEqual(self.sshd_pam.read_bytes(), previous + custom + b"# A later administrator edit\n")

    def test_enable_applies_pam_change_to_existing_flowvy_hook(self):
        lifecycle.install_from(self.bundle("0.1.0"))
        self.hook.write_bytes(lifecycle.HOOK_CONTENT)
        self.sshd_pam.write_bytes(DEBIAN_PAM)

        lifecycle.set_hook(True)

        self.assertEqual(self.sshd_pam.read_bytes().count(b"motd=/dev/null"), 2)
        self.assertEqual(lifecycle.hook_status(), "enabled")

    def test_enable_refuses_unsafe_debian_pam_file_before_enabling_greeting(self):
        lifecycle.install_from(self.bundle("0.1.0"))
        target = self.root / "elsewhere"
        target.write_bytes(DEBIAN_PAM)
        self.sshd_pam.symlink_to(target)

        with self.assertRaises(ValueError):
            lifecycle.set_hook(True)

        self.assertEqual(target.read_bytes(), DEBIAN_PAM)
        self.assertFalse(self.hook.exists())

    def test_enable_leaves_non_debian_pam_file_alone(self):
        lifecycle.install_from(self.bundle("0.1.0"))
        self.sshd_pam.write_bytes(DEBIAN_PAM)

        with patch("platform.freedesktop_os_release", return_value={"ID": "ubuntu", "VERSION_ID": "24.04"}):
            lifecycle.set_hook(True)

        self.assertEqual(self.sshd_pam.read_bytes(), DEBIAN_PAM)
        self.assertEqual(lifecycle.hook_status(), "enabled")

    def test_enable_does_not_edit_a_motd_rule_inside_a_continuation(self):
        lifecycle.install_from(self.bundle("0.1.0"))
        continued = DEBIAN_PAM.replace(b"@include common-auth\n",
                                      b"session optional pam_exec.so /usr/bin/true \\\n")
        self.sshd_pam.write_bytes(continued)

        lifecycle.set_hook(True)

        self.assertIn(b"pam_motd.so  motd=/run/motd.dynamic\n", self.sshd_pam.read_bytes())
        self.assertEqual(self.sshd_pam.read_bytes().count(b"motd=/dev/null"), 1)

    def test_failed_pam_write_rolls_back_new_greeting(self):
        lifecycle.install_from(self.bundle("0.1.0"))
        self.systemd_running.mkdir()
        self.sshd_pam.write_bytes(DEBIAN_PAM)
        write = lifecycle._atomic_write

        def fail_pam_write(path, *args, **kwargs):
            if path == self.sshd_pam:
                raise PermissionError("PAM config is read-only")
            write(path, *args, **kwargs)

        with patch.object(lifecycle, "_systemctl"), patch.object(lifecycle, "_atomic_write", fail_pam_write):
            with self.assertRaises(PermissionError):
                lifecycle.set_hook(True)

        self.assertFalse(self.hook.exists())
        self.assertEqual(list(self.systemd.iterdir()), [])
        self.assertEqual(self.sshd_pam.read_bytes(), DEBIAN_PAM)

    def test_enable_preserves_foreign_collector_unit(self):
        self.systemd_running.mkdir()
        lifecycle.install_from(self.bundle("0.1.0"))
        foreign = self.systemd / "flowvy-motd-docker.service"
        foreign.write_text("administrator's unit")
        with patch.object(lifecycle, "_systemctl", create=True) as systemctl:
            with self.assertRaises(ValueError):
                lifecycle.set_hook(True)
            systemctl.assert_not_called()
        self.assertEqual(foreign.read_text(), "administrator's unit")
        self.assertFalse(self.hook.exists())

    def test_cache_expiration_and_invalid_entries_are_ignored(self):
        contents = (b'{"format":"flowvy-motd-docker-v1","statuses":'
                    b'{"container:ok":["healthy",""],"container:invalid":"running"}}')
        with patch.object(lifecycle, "_cache_contents", return_value=(contents, 100)), \
             patch.object(lifecycle.time, "time", return_value=110) as clock:
            self.assertEqual(lifecycle.read_docker_cache(), {"container:ok": ["healthy", ""]})
            clock.return_value = 161
            self.assertEqual(lifecycle.read_docker_cache(), {})

    def test_uninstall_keeps_config_only_on_request_and_leaves_other_files(self):
        lifecycle.install_from(self.bundle("0.1.0"))
        lifecycle.set_hook(True)
        self.existing_config()
        other = self.config.parent / "administrator-notes.txt"
        other.write_text("keep")
        self.assertEqual(lifecycle.uninstall(True), [])
        self.assertFalse(self.bin.exists())
        self.assertFalse(self.hook.exists())
        self.assertTrue(self.config.exists())
        self.assertEqual(lifecycle.uninstall(False), [])
        self.assertFalse(self.config.exists())
        self.assertEqual(other.read_text(), "keep")

    def test_uninstall_preserves_changed_hook_unmarked_config_and_symlinks(self):
        lifecycle.install_from(self.bundle("0.1.0"))
        lifecycle.set_hook(True)
        self.hook.write_text(self.hook.read_text() + "\n# local change\n")
        self.existing_config('role = "administrator"\n')
        self.assertEqual(set(lifecycle.uninstall(False)), {str(self.hook), str(self.config)})
        self.assertTrue(self.hook.exists())
        self.assertTrue(self.config.exists())
        self.hook.unlink()
        self.hook.symlink_to(self.config)
        self.assertIn(str(self.hook), lifecycle.uninstall(False))
        self.assertTrue(self.hook.is_symlink())

    def test_uninstall_does_not_follow_config_directory_symlink(self):
        original = self.root / "other-config"
        original.mkdir()
        owned = original / self.config.name
        owned.write_text("# Managed by Flowvy MOTD\n")
        self.config.parent.symlink_to(original, target_is_directory=True)
        self.assertEqual(lifecycle.uninstall(False), [str(self.config)])
        self.assertTrue(owned.exists())
        self.assertTrue(self.config.parent.is_symlink())

    def test_update_validates_sha256_and_version_then_preserves_config_and_hook(self):
        lifecycle.install_from(self.bundle("0.1.0"))
        lifecycle.set_hook(True)
        self.existing_config()
        config_content = self.config.read_bytes()
        hook_content = self.hook.read_bytes()
        candidate = self.bundle("0.1.1", name="update.pyz").read_bytes()
        checksum = hashlib.sha256(candidate).hexdigest().encode() + b"  flowvy-motd.pyz\n"
        with patch.object(lifecycle, "_download", side_effect=(candidate, b"0" * 64)):
            with self.assertRaises(ValueError):
                lifecycle.update("v0.1.1")
        self.assertEqual(lifecycle._bundle_version(self.bin), "0.1.0")
        with patch.object(lifecycle, "_download", side_effect=(candidate, checksum)):
            lifecycle.update("v0.1.1")
        self.assertEqual(lifecycle._bundle_version(self.bin), "0.1.1")
        self.assertEqual(self.config.read_bytes(), config_content)
        self.assertEqual(self.hook.read_bytes(), hook_content)

    def test_regular_root_owned_files_are_recognized_by_a_nonroot_menu(self):
        info = SimpleNamespace(st_mode=0o100755, st_uid=0)
        with patch.object(lifecycle.os, "geteuid", return_value=1001):
            self.assertTrue(lifecycle._safe_file(info))
        with patch.object(lifecycle.os, "geteuid", return_value=0):
            self.assertFalse(lifecycle._safe_file(SimpleNamespace(st_mode=0o100755, st_uid=1001)))

    def test_settings_save_cannot_begin_after_uninstall(self):
        lifecycle.install_from(self.bundle("0.1.0"))
        with lifecycle.settings_write_lock():
            self.existing_config()
        lifecycle.uninstall(False)
        with self.assertRaises(ValueError):
            with lifecycle.settings_write_lock():
                self.existing_config()
        self.assertFalse(self.config.exists())

    def test_update_cannot_reinstall_after_a_concurrent_uninstall(self):
        candidate = self.bundle("0.1.1").read_bytes()
        checksum = hashlib.sha256(candidate).hexdigest().encode()
        with patch.object(lifecycle, "_download", side_effect=(candidate, checksum)):
            with self.assertRaises(ValueError):
                lifecycle.update("v0.1.1")
        self.assertFalse(self.bin.exists())

    def test_foreign_writable_parent_refused(self):
        os.chmod(self.bin.parent, 0o777)
        with self.assertRaises(ValueError):
            lifecycle.install_from(self.bundle("0.1.0"))
        self.assertFalse(self.bin.exists())

    def test_update_only_removes_staged_files_and_refuses_downgrade(self):
        lifecycle.install_from(self.bundle("0.1.1"))
        with self.assertRaises(ValueError):
            lifecycle.install_from(self.bundle("0.1.0", name="older.pyz"))
        self.assertEqual(lifecycle._bundle_version(self.bin), "0.1.1")
        self.assertEqual(list(self.bin.parent.iterdir()), [self.bin])


if __name__ == "__main__":
    unittest.main()
