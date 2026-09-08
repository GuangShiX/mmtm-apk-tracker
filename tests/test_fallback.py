import json
import hashlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fallback import (
    ASSET_REQUEST_REGISTRY,
    CHARACTER_MENU_UI_ASSET_NAMES,
    COMMON_GAMEPLAY_UI_ASSET_NAMES,
    CORE_UI_ASSET_NAMES,
    GENERATED_MARKER,
    UI_ASSET_NAMES,
    PACKAGE_UI_ASSET_NAMES,
    _write_repository_metadata,
    _reuse_pixel_equivalent_repository_assets,
    _carry_forward_same_version_repository_assets,
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
    for request in ASSET_REQUEST_REGISTRY.assets:
        if request.category != "ui" and request.name != "CHR_000001_00_s.png":
            add(request.name, request.category)
    connection.commit()
    connection.close()
    return version_dir


class FallbackRepositoryTests(unittest.TestCase):
    def test_ui_allowlist_preserves_character_menu_dependencies(self):
        self.assertEqual(len(CORE_UI_ASSET_NAMES), 21)
        self.assertEqual(len(CHARACTER_MENU_UI_ASSET_NAMES), 58)
        self.assertEqual(len(COMMON_GAMEPLAY_UI_ASSET_NAMES), 16)
        self.assertEqual(len(UI_ASSET_NAMES), 116)
        self.assertIn("image_levellink.png", CHARACTER_MENU_UI_ASSET_NAMES)
        self.assertLessEqual(CHARACTER_MENU_UI_ASSET_NAMES, UI_ASSET_NAMES)

    def test_base_package_defers_addressable_ui_until_hot_update(self):
        self.assertIn("button_l_01_orange.png", PACKAGE_UI_ASSET_NAMES)
        self.assertNotIn("TabIcon_000001.png", PACKAGE_UI_ASSET_NAMES)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = build_fallback_repository("1.2.3", root / "output", "owner/icons", create_version(root))
            entries = [e for e in manifest["assets"] if not (e["category"] == "ui" and e["name"] not in PACKAGE_UI_ASSET_NAMES)]
            partial = _write_repository_metadata("1.2.3", root / "output", "owner/icons", entries, datetime.now(timezone.utc), require_requested_assets=False)
            self.assertEqual(partial["request_registry_status"], "incomplete")
            with self.assertRaisesRegex(RuntimeError, "TabIcon_000001"):
                _write_repository_metadata("1.2.3", root / "output", "owner/icons", entries, datetime.now(timezone.utc))

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

            self.assertEqual(
                manifest["asset_count"], len(ASSET_REQUEST_REGISTRY.assets) + 4
            )
            self.assertEqual(manifest["request_registry_status"], "complete")
            self.assertEqual(
                manifest["request_registry_sha256"],
                ASSET_REQUEST_REGISTRY.fingerprint,
            )
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

    def test_reuses_existing_png_bytes_when_decoded_pixels_match(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            staging = root / "staging"
            relative = Path("assets/ui/example.png")
            old_path = output / relative
            new_path = staging / relative
            old_path.parent.mkdir(parents=True)
            new_path.parent.mkdir(parents=True)
            image = Image.new("RGBA", (8, 8), (40, 80, 120, 255))
            image.save(old_path, compress_level=0)
            image.save(new_path, compress_level=9)
            old_content = old_path.read_bytes()
            new_content = new_path.read_bytes()
            self.assertNotEqual(old_content, new_content)
            (output / "manifest.json").write_text(
                json.dumps(
                    {
                        "assets": [
                            {
                                "category": "ui",
                                "name": "example.png",
                                "path": relative.as_posix(),
                                "size": len(old_content),
                                "sha256": hashlib.sha256(old_content).hexdigest(),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            entries = [
                {
                    "category": "ui",
                    "name": "example.png",
                    "path": relative.as_posix(),
                    "size": len(new_content),
                    "sha256": hashlib.sha256(new_content).hexdigest(),
                }
            ]

            reused = _reuse_pixel_equivalent_repository_assets(
                output, staging, entries
            )

            self.assertEqual(reused, 1)
            self.assertEqual(new_path.read_bytes(), old_content)
            self.assertEqual(entries[0]["size"], len(old_content))

    def test_carries_catalog_only_asset_during_same_version_rebuild(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "output"
            staging = root / "staging"
            relative = Path("assets/characters/CHR_000150_00_s.png")
            source = output / relative
            source.parent.mkdir(parents=True)
            staging.mkdir()
            source.write_bytes(b"catalog-only")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            entry = {
                "category": "characters",
                "name": source.name,
                "path": relative.as_posix(),
                "size": source.stat().st_size,
                "sha256": digest,
                "source_catalog_keys": ["CharacterIcon/CHR_000150/CHR_000150_00_s"],
            }
            (output / "manifest.json").write_text(
                json.dumps({"version": "4.21.0", "assets": [entry]}),
                encoding="utf-8",
            )
            entries = []

            carried = _carry_forward_same_version_repository_assets(
                "4.21.0", output, staging, entries
            )

            self.assertEqual(carried, 1)
            self.assertEqual(entries, [entry])
            self.assertEqual((staging / relative).read_bytes(), b"catalog-only")


if __name__ == "__main__":
    unittest.main()
