"""No-network tests for the maintenance watcher; use a deterministic clock."""
import json
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cloud_update_watch as cloud

NOW = datetime(2026, 10, 15, 6, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self):
        self.now = NOW
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


def window(end=60, start=-60):
    # Effective end includes the intentional extra 30 minutes.
    return SimpleNamespace(start_at_utc=NOW + timedelta(seconds=start), end_at_utc=NOW + timedelta(seconds=end - 1800))


def info(master="old", checksum="hash-1"):
    return SimpleNamespace(app_version="4.24.0", asset_version="asset", master_version=master, catalog_hash=checksum)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "derived"
        self.state = self.root / "state.json"
        self.raw = self.root / "raw"
        self.clock = FakeClock()
        self.exports = []

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, current=None, ids=(1, 2)):
        current = current or info()
        self.snapshot(current, ids)
        cloud.write_json(self.state, {"accepted_versions": cloud.versions(current), "character_ids": list(ids)})

    def snapshot(self, current, ids):
        source = {"master_version": current.master_version}
        languages = ["zh-CN", "ja-JP"]
        cloud.write_json(self.output / "skills/index.json", {"characters": [{"id": cid} for cid in ids]})
        entries = []
        for cid in ids:
            path = f"skills/characters/{cid:06d}.json"
            cloud.write_json(self.output / path, {"character": {"id": cid}})
            data = (self.output / path).read_bytes()
            entries.append({"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        path = "skills/index.json"
        data = (self.output / path).read_bytes()
        entries.append({"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        cloud.write_json(self.output / "skills/manifest.json", {"source": source, "languages": languages, "files": entries, "file_count": len(entries)})
        cloud.write_json(self.output / "skills/latest.json", {"source": source, "languages": languages, "latest_characters": [{"id": cid, "path": f"skills/characters/{cid:06d}.json"} for cid in ids]})
        (self.output / "skills/.generated-by-mmtm-apk-tracker").write_text("generated", encoding="utf-8")

    def export(self, current, output, raw, as_of, previous):
        self.exports.append((current.master_version, list(previous), as_of))
        self.assertLessEqual(as_of, self.clock())  # Never silently export future characters.
        ids = [1, 2, 3] if current.master_version == "new" else [1, 2]
        added = sorted(set(ids) - set(previous)) if previous else []
        self.snapshot(current, ids)
        return {"character_ids": ids, "new_character_ids": added, "brief": "技能快报"}

    def run_watch(self, **kwargs):
        defaults = dict(clock=self.clock, sleep=self.clock.sleep, windows=lambda: [window()], probe=lambda: info(), exporter=self.export, max_duration=80, refresh_interval=20)
        defaults.update(kwargs)
        return cloud.watch(self.output, self.state, self.raw, **defaults)

    def test_plan_is_30_minutes_early_and_late(self):
        w = SimpleNamespace(start_at_utc=NOW + timedelta(minutes=20), end_at_utc=NOW + timedelta(hours=1))
        plan = cloud.make_plan([w], NOW)
        self.assertTrue(plan["should_watch"])
        self.assertEqual(datetime.fromisoformat(plan["start_at_utc"]), NOW - timedelta(minutes=10))
        self.assertEqual(datetime.fromisoformat(plan["end_at_utc"]), NOW + timedelta(minutes=90))

    def test_plan_exposes_next_window_before_watch_starts(self):
        w = SimpleNamespace(start_at_utc=NOW + timedelta(hours=2), end_at_utc=NOW + timedelta(hours=3))
        self.assertFalse(cloud.make_plan([w], NOW)["should_watch"])
        self.assertTrue(cloud.make_plan([w], NOW)["start_at_utc"])

    def test_cold_cache_does_not_call_old_roster_new(self):
        result = self.run_watch()
        self.assertFalse(result["exported"])
        self.assertFalse(result["has_new_characters"])
        self.assertEqual(len(self.exports), 1)

    def test_cold_baseline_keeps_watching_and_detects_update_at_20_seconds(self):
        result = self.run_watch(probe=lambda: info("new") if self.clock() >= NOW + timedelta(seconds=20) else info())
        self.assertTrue(result["exported"])
        self.assertEqual(result["new_character_ids"], "3")
        self.assertEqual(self.clock(), NOW + timedelta(seconds=20))
        self.assertEqual(len(self.exports), 2)

    def test_published_baseline_detects_only_new_id(self):
        baseline = self.root / "published-skills"
        cloud.write_json(baseline / "index.json", {"characters": [{"id": 1}, {"id": 2}]})
        result = self.run_watch(probe=lambda: info("new"), baseline_skills_dir=baseline)
        self.assertEqual(result["new_character_ids"], "3")

    def test_failed_export_retries_without_advancing_accepted(self):
        self.seed()
        attempts = []
        def export(*args):
            attempts.append(True)
            self.assertEqual(cloud.read_json(self.state, {})["accepted_versions"]["master_version"], "old")
            if len(attempts) == 1:
                raise RuntimeError("checksum mismatch")
            return self.export(*args)
        result = self.run_watch(probe=lambda: info("new"), exporter=export)
        self.assertEqual(len(attempts), 2)
        self.assertTrue(result["exported"])
        self.assertEqual(self.clock.sleeps, [20])

    def test_repeated_failures_leave_old_state_and_fail(self):
        self.seed()
        def fail(*args):
            raise RuntimeError("network unavailable")
        with self.assertRaisesRegex(RuntimeError, "network unavailable"):
            self.run_watch(probe=fail)
        self.assertEqual(cloud.read_json(self.state, {})["accepted_versions"]["master_version"], "old")
        self.assertEqual(self.exports, [])

    def test_probe_failure_then_success(self):
        calls = []
        def probe():
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError("maintenance")
            return info()
        self.assertFalse(self.run_watch(probe=probe)["exported"])
        self.assertGreaterEqual(len(calls), 2)

    def test_maintenance_extension_is_refreshed(self):
        self.seed()
        calls = []
        def windows():
            calls.append(True)
            return [window(end=25 if len(calls) == 1 else 100)]
        def probe():
            return info("new") if self.clock() >= NOW + timedelta(seconds=40) else info()
        result = self.run_watch(windows=windows, probe=probe)
        self.assertTrue(result["exported"])
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(self.clock(), NOW + timedelta(seconds=40))

    def test_extension_is_checked_at_boundary_before_regular_refresh(self):
        self.seed()
        calls = []
        def windows():
            calls.append(True)
            return [window(end=25 if len(calls) == 1 else 100)]
        result = self.run_watch(windows=windows, refresh_interval=300, probe=lambda: info("new") if self.clock() >= NOW + timedelta(seconds=40) else info())
        self.assertTrue(result["exported"])
        self.assertGreaterEqual(len(calls), 2)

    def test_unchanged_version_has_no_export(self):
        self.seed()
        result = self.run_watch()
        self.assertFalse(result["exported"])
        self.assertEqual(self.exports, [])

    def test_same_master_catalog_change_triggers_export(self):
        self.seed()
        result = self.run_watch(probe=lambda: info(checksum="hash-2"))
        self.assertTrue(result["exported"])
        self.assertFalse(result["has_new_characters"])

    def test_state_without_derived_snapshot_is_rebuilt(self):
        self.seed()
        (self.output / "skills/latest.json").unlink()
        self.assertFalse(self.run_watch()["exported"])
        self.assertEqual(len(self.exports), 1)

    def test_corrupt_derived_cache_is_rebuilt(self):
        self.seed()
        (self.output / "skills/latest.json").write_text("truncated", encoding="utf-8")
        self.assertFalse(self.run_watch()["exported"])
        self.assertEqual(len(self.exports), 1)

    def test_changed_character_json_hash_forces_rebuild(self):
        self.seed()
        (self.output / "skills/characters/000001.json").write_text("{}", encoding="utf-8")
        self.assertFalse(cloud.cache_matches(self.output, info(), NOW))
        self.run_watch()
        self.assertEqual(len(self.exports), 1)

    def test_due_character_release_refreshes_same_master(self):
        self.seed()
        latest = cloud.read_json(self.output / "skills/latest.json", {})
        latest["next_release_time_jst"] = "2026-10-15 15:00:00"
        cloud.write_json(self.output / "skills/latest.json", latest)
        self.assertFalse(self.run_watch()["exported"])
        self.assertEqual(len(self.exports), 1)

    def test_app_asset_change_keeps_polling_until_master_changes(self):
        self.seed()
        def probe():
            current = info("new" if self.clock() >= NOW + timedelta(seconds=40) else "old")
            if self.clock() >= NOW + timedelta(seconds=20):
                current.asset_version = "new-art"
            return current
        result = self.run_watch(probe=probe)
        self.assertTrue(result["exported"])
        self.assertEqual(self.clock(), NOW + timedelta(seconds=40))
        self.assertEqual(len(self.exports), 1)

    def test_future_character_gets_isolated_preview_with_real_release_time(self):
        import msgpack
        from test_master import _write_master_fixture
        raw = self.raw / "old"
        raw.mkdir(parents=True)
        _write_master_fixture(raw)
        catalog = msgpack.unpackb((raw / "master-catalog").read_bytes(), raw=False)
        rows = msgpack.unpackb((raw / "CharacterMB").read_bytes(), raw=False)
        rows[1]["StartTimeFixJST"] = "2026-10-15 15:01:00"
        for name, data in (("CharacterMB", msgpack.packb(rows, use_bin_type=True)), ("TextResourceJaJpMB", (raw / "TextResourceZhCnMB").read_bytes())):
            (raw / name).write_bytes(data)
            catalog["MasterBookInfoMap"][name] = {"Name": name, "Size": len(data), "Hash": hashlib.md5(data, usedforsecurity=False).hexdigest()}
        (raw / "master-catalog").write_bytes(msgpack.packb(catalog, use_bin_type=True))
        current = info(checksum=cloud.catalog_signature(catalog))
        with patch("master.download_master_books", return_value=raw):
            result = cloud.export_official(current, self.output, self.raw, NOW, [50])
        self.assertEqual(result["new_character_ids"], [])
        self.assertEqual(result["preview_character_ids"], [5])
        self.assertEqual(cloud.character_ids(self.output), [50])
        self.assertEqual(cloud.character_ids(self.output / "preview"), [5, 50])
        preview_latest = cloud.read_json(self.output / "preview/skills/latest.json", {})
        self.assertEqual(preview_latest["latest_release_time_jst"], "2026-10-15 15:01:00")
        self.assertIn("尚未实装的候选技能预览", result["brief"])
        self.assertIn("active lv1", result["brief"])
        self.assertFalse((self.output / "master-catalog").exists())

    def test_catalog_change_during_export_is_not_accepted(self):
        import msgpack
        raw = self.raw / "old"
        raw.mkdir(parents=True)
        (raw / "master-catalog").write_bytes(msgpack.packb({"MasterBookInfoMap": {"CharacterMB": {"Hash": "unexpected", "Size": 1}}}))
        with patch("master.download_master_books", return_value=raw):
            with self.assertRaisesRegex(RuntimeError, "changed during download"):
                cloud.export_official(info(), self.output, self.raw, NOW, [1, 2])

    def test_output_written_before_return(self):
        outputs = self.root / "github-output"
        summary = self.root / "summary"
        self.run_watch(github_output=outputs, summary_file=summary)
        self.assertIn("exported=false", outputs.read_text(encoding="utf-8"))
        self.assertIn("技能快报", summary.read_text(encoding="utf-8"))

    def test_raw_cannot_be_nested_in_public_output(self):
        with self.assertRaisesRegex(ValueError, "separate trees"):
            cloud.watch(self.output, self.state, self.output / "raw")

    def test_single_instance_lock_rejects_second_owner(self):
        path = self.root / "watch.lock"
        with cloud.single_instance(path):
            with self.assertRaises(FileExistsError):
                with cloud.single_instance(path):
                    self.fail("must not enter second lock")
        self.assertFalse(path.exists())

    def test_catalog_hash_tracks_content_and_table_membership(self):
        initial = {"MasterBookInfoMap": {"CharacterMB": {"Hash": "a", "Size": 1}}}
        changed = {"MasterBookInfoMap": {"CharacterMB": {"Hash": "b", "Size": 1}}}
        extra = {"MasterBookInfoMap": {"CharacterMB": {"Hash": "a", "Size": 1}, "OtherMB": {"Hash": "b", "Size": 1}}}
        self.assertNotEqual(cloud.catalog_signature(initial), cloud.catalog_signature(changed))
        self.assertNotEqual(cloud.catalog_signature(initial), cloud.catalog_signature(extra))


if __name__ == "__main__":
    unittest.main()
