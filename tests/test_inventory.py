import json
import subprocess
import time
import unittest
from unittest.mock import patch

from flowvy_motd import inventory


class SystemdInventoryTest(unittest.TestCase):
    def test_timer_waits_and_remembers_a_known_success(self):
        unit = inventory._unit_component(
            "daily.service",
            {
                "ActiveState": "inactive",
                "LoadState": "loaded",
                "Type": "oneshot",
                "Result": "success",
                "TriggeredBy": "daily.timer",
                "ExecMainExitTimestampMonotonic": "12345",
            },
            {"daily.timer": {"ActiveState": "active", "SubState": "waiting"}},
        )
        self.assertEqual((unit.state, unit.schedule), ("waiting", "scheduled"))
        self.assertEqual(unit.detail, "Last run succeeded")

    def test_empty_execution_state_never_implies_a_success(self):
        unit = inventory._unit_component(
            "daily.service", {"ActiveState": "inactive", "Type": "oneshot", "Result": "success"}, {}
        )
        self.assertEqual(unit.state, "waiting")
        self.assertEqual(unit.detail, "No recorded run")

    def test_real_failure_wins_over_idle_job_classification(self):
        unit = inventory._unit_component(
            "daily.service",
            {"ActiveState": "failed", "Type": "oneshot", "Result": "exit-code", "ExecMainStatus": "2"},
            {},
        )
        self.assertEqual((unit.state, unit.schedule), ("failed", "one-shot"))
        self.assertEqual(unit.detail, "Exit code 2")

    def test_timer_stopped_is_distinct_from_waiting(self):
        unit = inventory._unit_component(
            "daily.service",
            {"ActiveState": "inactive", "Type": "oneshot", "TriggeredBy": "daily.timer"},
            {"daily.timer": {"ActiveState": "inactive", "SubState": "dead"}},
        )
        self.assertEqual(unit.state, "stopped")
        self.assertEqual(unit.detail, "Trigger stopped")

    def test_unmet_start_condition_does_not_mark_the_unit_failed(self):
        unit = inventory._unit_component(
            "daily.service",
            {"ActiveState": "inactive", "Type": "oneshot", "Result": "exec-condition"},
            {},
        )
        self.assertEqual((unit.state, unit.detail), ("waiting", "Start condition not met"))

    def test_elapsed_single_trigger_with_known_success_is_completed(self):
        unit = inventory._unit_component(
            "daily.service",
            {
                "ActiveState": "inactive", "Type": "oneshot", "Result": "success",
                "ExecMainExitTimestampMonotonic": "12345", "TriggeredBy": "daily.timer",
            },
            {"daily.timer": {"ActiveState": "active", "SubState": "elapsed"}},
        )
        self.assertEqual(unit.state, "completed")

    def test_inventory_identifies_package_by_binary_and_collapses_alias(self):
        metadata = {
            "mesh.service": {
                "Id": "mesh.service", "Names": "mesh.service alias.service", "Description": "NetBird client",
                "Type": "simple", "ActiveState": "active", "SubState": "running",
                "FragmentPath": "/etc/systemd/system/mesh.service",
                "ExecStart": "{ path=/usr/bin/netbird ; argv[]=/usr/bin/netbird service run ; }",
            },
            "script.service": {
                "Id": "script.service", "Description": "Local script", "ActiveState": "inactive",
                "ExecStart": "{ path=/usr/bin/bash ; argv[]=/usr/bin/bash /opt/mesh.sh ; }",
            },
        }
        responses = {
            ("systemctl", "list-unit-files"): subprocess.CompletedProcess([], 0, "mesh.service enabled\nalias.service alias\nscript.service static\n", ""),
            ("systemctl", "list-units"): subprocess.CompletedProcess([], 0, "mesh.service loaded active running NetBird client\n", ""),
        }
        with patch.object(inventory, "_command", side_effect=lambda args, *_: responses[tuple(args[:2])]), \
             patch.object(inventory, "_systemd_query", return_value=(metadata, "")), \
             patch.object(inventory, "_package_owners", return_value={"/usr/bin/netbird": "netbird", "/usr/bin/bash": "bash"}), \
             patch.object(inventory, "_docker_list", return_value=([], "")):
            found = inventory.discover()
        self.assertEqual({item.id for item in found}, {"unit:mesh.service", "unit:script.service"})
        mesh = next(item for item in found if item.id == "unit:mesh.service")
        script = next(item for item in found if item.id == "unit:script.service")
        self.assertEqual((mesh.group, mesh.group_kind, mesh.description), ("netbird", "executable", "NetBird client"))
        self.assertEqual(script.group, "")


class DockerInventoryTest(unittest.TestCase):
    def test_compose_replicas_are_one_selection_and_health_is_structured(self):
        rows = [
            inventory._Container("one", "vpn-mostly-1", "netbirdio/netbird", "vpn", "client"),
            inventory._Container("two", "vpn-mostly-2", "netbirdio/netbird", "vpn", "client"),
        ]
        inspected = "\n".join(json.dumps([uid, state]) for uid, state in (
            ("one", {"Status": "running", "Health": {"Status": "healthy"}}),
            ("two", {"Status": "running", "Health": {"Status": "unhealthy"}}),
        ))
        with patch.object(inventory, "_docker_list", return_value=(rows, "")), \
             patch.object(inventory, "_docker", return_value=subprocess.CompletedProcess([], 0, inspected, "")):
            found = inventory.collect(["compose:vpn/client"])
        component = found["compose:vpn/client"]
        self.assertEqual((component.group, component.state), ("vpn", "failed"))
        self.assertIn("1/2 running", component.detail)
        self.assertIn("netbirdio/netbird", component.search_text)

    def test_picker_discovery_includes_current_docker_state(self):
        rows = [inventory._Container("one", "web", "nginx", "", "")]
        inspected = json.dumps(["one", {"Status": "running"}])
        response = subprocess.CompletedProcess([], 0, inspected, "")
        with patch.object(inventory, "_docker_list", return_value=(rows, "")) as listing, \
             patch.object(inventory, "_docker", return_value=response):
            found = inventory._discover_docker(time.monotonic() + 1)
        listing.assert_called_once()
        self.assertEqual(found[0].state, "running")

    def test_zero_exit_of_plain_container_is_stopped(self):
        self.assertEqual(
            inventory._container_state({"Status": "exited", "ExitCode": 0}, one_shot=False),
            ("stopped", "Exited with code 0"),
        )
        self.assertEqual(
            inventory._container_state({"Status": "exited", "ExitCode": 0}, one_shot=True),
            ("completed", "Exited with code 0"),
        )

    def test_created_container_has_not_started(self):
        self.assertEqual(
            inventory._container_state({"Status": "created"}, one_shot=False),
            ("stopped", "Not started"),
        )
        self.assertEqual(
            inventory._container_state({"Status": "created"}, one_shot=True),
            ("waiting", "Waiting to run"),
        )

    def test_daemon_permission_denial_is_unavailable(self):
        with patch.object(inventory, "_docker_list", return_value=([], "Docker access denied")):
            found = inventory.collect(["container:web"])
        self.assertEqual(found["container:web"].state, "unavailable")
        self.assertEqual(found["container:web"].detail, "Docker access denied")

    def test_denied_system_docker_uses_only_cached_selected_components(self):
        with patch.object(inventory, "_docker_list", return_value=([], "Docker access denied")), \
             patch.object(inventory, "_docker_socket", return_value="unix:///var/run/docker.sock", create=True), \
             patch.object(inventory.os, "geteuid", return_value=1000), \
             patch.object(inventory, "read_docker_cache", create=True, return_value={
                 "container:web": ["healthy", ""], "container:other": ["running", ""],
             }):
            found = inventory.collect(["container:web", "container:missing"])
        self.assertEqual(set(found), {"container:web", "container:missing"})
        self.assertEqual(found["container:web"].state, "healthy")
        self.assertEqual(found["container:missing"].detail, "Docker access denied")

    def test_rootless_docker_denial_does_not_use_system_snapshot(self):
        with patch.object(inventory, "_docker_list", return_value=([], "Docker access denied")), \
             patch.object(inventory, "_docker_socket", return_value="unix:///run/user/1000/docker.sock"), \
             patch.object(inventory.os, "geteuid", return_value=1000), \
             patch.object(inventory, "read_docker_cache") as cache:
            found = inventory.collect(["container:web"])
        cache.assert_not_called()
        self.assertEqual(found["container:web"].state, "unavailable")

    def test_only_requested_sources_are_queried(self):
        with patch.object(inventory, "_systemd_query") as systemd, \
             patch.object(inventory, "_docker_list", return_value=([], "")):
            inventory.collect(["container:web"])
        systemd.assert_not_called()


class CollectionBudgetTest(unittest.TestCase):
    def test_providers_receive_one_shared_deadline_and_run_concurrently(self):
        observed = []

        def slow(ids, deadline):
            observed.append(deadline)
            time.sleep(0.1)
            return {}

        start = time.monotonic()
        with patch.object(inventory, "_collect_systemd", side_effect=slow), \
             patch.object(inventory, "_collect_docker", side_effect=slow):
            inventory.collect(["unit:web.service", "container:web"], timeout=0.25)
        self.assertLess(time.monotonic() - start, 0.19)
        self.assertEqual(len(observed), 2)
        self.assertEqual(*observed)


class NetBirdStatusTest(unittest.TestCase):
    def state(self, data):
        result = subprocess.CompletedProcess([], 0, json.dumps(data), "")
        with patch.object(inventory, "_command", return_value=result) as command:
            status = inventory.read_netbird()
        self.assertIn("unix:///var/run/netbird.sock", command.call_args.args[0])
        return status

    def test_idle_and_connecting_do_not_mean_connected(self):
        self.assertEqual(self.state({"daemonStatus": "Idle"})[0], "disconnected")
        self.assertEqual(self.state({"daemonStatus": "Connecting"})[0], "connecting")

    def test_connected_displays_only_a_valid_address(self):
        self.assertEqual(
            self.state({
                "daemonStatus": "Connected", "management": {"connected": True},
                "signal": {"connected": True}, "netbirdIp": "100.64.0.12/16",
            }),
            ("connected", "100.64.0.12"),
        )
