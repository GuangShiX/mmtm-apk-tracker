import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fallback import (
    GENERATED_MARKER,
    UI_ASSET_NAMES,
    build_fallback_repository,
)


def create_version(root: Path, version="1.2.3") -> Path:
    version_dir = root / "extracted" / version
    version_dir.mkdir(parents=True)
    (version_dir / "package_manifest.json").write_text("{}", encoding="utf-8")
    (version_dir / "extraction_report.json").write_text(
        json.dumps(
            {
                "version": version,
                "status": "complete",
                "bundles": 1,
                "unity_sources": 1,
                "objects_total": 30,
                "objects_raw_exported": 30,
                "raw_object_coverage": 1.0,
                "errors": [],
            }
        ),
        encoding="utf-8",
    )
    connection = sqlite3.connect(version_dir / "manifest.sqlite3")
    connection.execute(
        """
        CREATE TABLE resources (
          resource_key TEXT PRIMARY KEY,
          type TEXT NOT NULL,
          file TEXT NOT NULL,
          container TEXT NOT NULL
        )
        """
    )

    def add(name, category, content=b"image", canonical=True, container=True):
        relative = f"assets/{category}/{name}"
        if not canonical:
            relative = relative.replace(".png", "__123.png")
        target = version_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        logical = f"Assets/{category}/{name}" if container else ""
        key = logical if canonical and logical else f"bundle/{category}/{name}/{canonical}"
        connection.execute(
            "INSERT INTO resources VALUES (?, ?, ?, ?)",
            (key, "Sprite", relative, logical),
        )

    add("CHR_000001_00_s.png", "characters", b"canonical")
    add("CHR_000001_00_s.png", "characters-duplicate", b"duplicate", canonical=False)
    add("ENE_000001.png", "enemies")
    add("EQP_000001.png", "equipment")
    add("SPH_0101.png", "spheres")
    add("Item_0001.png", "items")
    for name in sorted(UI_ASSET_NAMES):
        if name == "frame_common_slice.png":
            add(name, "ui", container=False, canonical=False)
        else:
            add(name, "ui")
    connection.commit()
    connection.close()
    return version_dir


class FallbackRepositoryTests(unittest.TestCase):
    def test_builds_small_canonical_repository_with_stable_urls(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            version_dir = create_version(root)
            output = root / "fallback"
            manifest = build_fallback_repository(
                version="1.2.3",
                output_dir=output,
                repository="owner/icons",
                version_dir=version_dir,
                generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )

            self.assertEqual(manifest["asset_count"], 26)
            self.assertEqual(manifest["ref"], "main")
            self.assertEqual(manifest["archive_ref"], "v1.2.3")
            self.assertEqual(
                manifest["base_url"],
                "https://raw.githubusercontent.com/owner/icons/main",
            )
            self.assertEqual(
                (output / "assets/characters/CHR_000001_00_s.png").read_bytes(),
                b"canonical",
            )
            self.assertTrue((output / "assets" / GENERATED_MARKER).is_file())
            latest = json.loads((output / "latest.json").read_text(encoding="utf-8"))
            self.assertTrue(latest["manifest_url"].endswith("/main/manifest.json"))

    def test_rebuild_removes_stale_generated_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            version_dir = create_version(root)
            output = root / "fallback"
            build_fallback_repository("1.2.3", output, "owner/icons", version_dir)
            stale = output / "assets/stale.png"
            stale.write_bytes(b"stale")
            build_fallback_repository("1.2.3", output, "owner/icons", version_dir)
            self.assertFalse(stale.exists())

    def test_refuses_to_delete_unowned_assets_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            version_dir = create_version(root)
            output = root / "fallback"
            (output / "assets").mkdir(parents=True)
            with self.assertRaisesRegex(RuntimeError, "拒绝清理"):
                build_fallback_repository("1.2.3", output, "owner/icons", version_dir)


if __name__ == "__main__":
    unittest.main()
