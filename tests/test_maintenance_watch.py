import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from maintenance_watch import VersionProbe, dispatch_workflow, probe_once


class MaintenanceWatchTests(unittest.TestCase):
    def test_first_probe_records_baseline_without_dispatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "watch.json"
            current = VersionProbe("4.21.0", "asset-1", "master-1")
            run = Mock()

            actual, dispatched = probe_once(state, dispatch=True, probe=lambda: current)

            self.assertEqual(actual, current)
            self.assertFalse(dispatched)
            run.assert_not_called()
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["last_seen"], {
                "app_version": "4.21.0",
                "asset_version": "asset-1",
                "master_version": "master-1",
            })

    def test_changed_probe_dispatches_once_and_same_probe_does_not_repeat(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "watch.json"
            first = VersionProbe("4.21.0", "asset-1", "master-1")
            second = VersionProbe("4.22.0", "asset-2", "master-2")
            probe_once(state, probe=lambda: first)
            with patch("maintenance_watch.dispatch_workflow") as dispatch:
                actual, dispatched = probe_once(
                    state,
                    dispatch=True,
                    repository="owner/repo",
                    workflow="update.yml",
                    ref="main",
                    probe=lambda: second,
                )
            self.assertEqual(actual, second)
            self.assertTrue(dispatched)
            dispatch.assert_called_once_with("owner/repo", "update.yml", "main")

            # The subprocess boundary is patched at the dispatcher, not in
            # probe_once; this verifies the persisted de-duplication marker.
            data = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(data["last_dispatched"], second.fingerprint())

            with patch("maintenance_watch.dispatch_workflow") as dispatch_again:
                _, dispatched_again = probe_once(
                    state,
                    dispatch=True,
                    repository="owner/repo",
                    workflow="update.yml",
                    ref="main",
                    probe=lambda: second,
                )
            self.assertFalse(dispatched_again)
            dispatch_again.assert_not_called()

    def test_dispatch_uses_gh_workflow_run(self):
        runner = Mock(
            return_value=SimpleNamespace(returncode=0, stdout="", stderr="")
        )

        dispatch_workflow("owner/repo", "update.yml", "main", runner=runner)

        runner.assert_called_once_with(
            ["gh", "workflow", "run", "update.yml", "--repo", "owner/repo", "--ref", "main"],
            check=False,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
