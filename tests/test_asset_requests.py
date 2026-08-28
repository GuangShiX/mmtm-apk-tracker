import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from asset_requests import (
    load_asset_request_registry,
    requested_asset_for_name,
    requested_catalog_target,
    verify_requested_repository,
)


class AssetRequestTests(unittest.TestCase):
    def test_registry_tracks_curated_groups_and_special_master_mapping(self):
        registry = load_asset_request_registry()

        self.assertEqual(len(registry.assets), 100)
        self.assertEqual(len(registry.group_names["avatar-compositor-core"]), 21)
        self.assertEqual(len(registry.group_names["character-menu-workbench"]), 58)
        self.assertEqual(len(registry.group_names["helper-common-gameplay-ui"]), 16)
        self.assertEqual(
            len(registry.group_names["account-special-player-icons-2026-08-28"]),
            14,
        )
        request = requested_asset_for_name("CHR_000045_00_em_002.png")
        self.assertIsNotNone(request)
        self.assertEqual(request.metadata["special_icon_item_id"], 1003)
        self.assertEqual(
            requested_catalog_target(
                "CharacterIcon/CHR_000135/CHR_000135_00_em_001"
            ),
            ("characters", "CHR_000135_00_em_001.png"),
        )

    def test_verifies_complete_generated_repository(self):
        registry = load_asset_request_registry()
        with tempfile.TemporaryDirectory() as temp:
            repository = Path(temp)
            entries = []
            for request in registry.assets:
                relative = Path("assets") / request.category / request.name
                target = repository / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGBA", (64, 64), (255, 255, 255, 255)).save(target)
                content = target.read_bytes()
                entries.append(
                    {
                        "category": request.category,
                        "name": request.name,
                        "path": relative.as_posix(),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                        "source_resource_key": f"Assets/{request.name}",
                    }
                )
            manifest = {
                "version": "1.2.3",
                "asset_version": "asset-version",
                "request_registry_sha256": registry.fingerprint,
                "request_registry_status": "complete",
                "assets": entries,
            }
            (repository / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            result = verify_requested_repository(repository)

            self.assertEqual(result["status"], "accepted")
            self.assertEqual(result["requested_asset_count"], 100)
            self.assertEqual(result["verified_manifest_asset_count"], 100)
            self.assertEqual(len(result["special_player_icon_dimensions"]), 14)

    def test_rejects_stale_request_registry_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            repository = Path(temp)
            (repository / "manifest.json").write_text(
                json.dumps(
                    {
                        "request_registry_sha256": "stale",
                        "request_registry_status": "complete",
                        "assets": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "指纹"):
                verify_requested_repository(repository)


if __name__ == "__main__":
    unittest.main()
