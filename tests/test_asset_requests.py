import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import msgpack
from PIL import Image

from asset_requests import (
    load_asset_request_registry,
    requested_asset_for_name,
    requested_catalog_target,
    sync_player_avatar_inventory,
    verify_player_avatar_inventory,
    verify_requested_repository,
)


class AssetRequestTests(unittest.TestCase):
    def test_registry_tracks_curated_groups_and_complete_avatar_inventory(self):
        registry = load_asset_request_registry()

        self.assertEqual(len(registry.assets), 219)
        self.assertEqual(len(registry.group_names["avatar-compositor-core"]), 21)
        self.assertEqual(len(registry.group_names["character-menu-workbench"]), 58)
        self.assertEqual(len(registry.group_names["helper-common-gameplay-ui"]), 16)
        self.assertEqual(len(registry.group_names["all-standard-player-icons"]), 133)
        standard = requested_asset_for_name("CHR_000063_00_s.png")
        self.assertIsNotNone(standard)
        self.assertEqual(standard.metadata["character_id"], 63)
        self.assertIsNone(requested_asset_for_name("CHR_000045_00_em_002.png"))
        self.assertIsNone(
            requested_catalog_target("CharacterIcon/CHR_000135/CHR_000135_00_em_001")
        )
        inventory_source = next(iter(registry.inventory_sources.values()))
        self.assertEqual(
            inventory_source["sources"]["SpecialIconItemMB"]["record_count"], 303
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
            special_relative = (
                Path("assets") / "characters" / "CHR_000063_00_em_001.png"
            )
            special_target = repository / special_relative
            Image.new("RGBA", (128, 128), (200, 180, 160, 255)).save(
                special_target
            )
            special_content = special_target.read_bytes()
            entries.append(
                {
                    "category": "characters",
                    "name": special_target.name,
                    "path": special_relative.as_posix(),
                    "sha256": hashlib.sha256(special_content).hexdigest(),
                    "size": len(special_content),
                    "source_resource_key": f"Assets/{special_target.name}",
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
            self.assertEqual(result["requested_asset_count"], 219)
            self.assertEqual(result["verified_manifest_asset_count"], 220)
            self.assertEqual(len(result["player_avatar_dimensions"]), 134)
            self.assertEqual(len(result["special_player_icon_dimensions"]), 1)
            self.assertEqual(result["declared_special_player_icon_count"], 303)

    def test_syncs_and_verifies_player_avatar_inventory_from_master(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            master = root / "master"
            master.mkdir()
            (master / "CharacterMB").write_bytes(
                msgpack.packb(
                    [
                        {"Id": 2, "IsIgnore": None},
                        {"Id": 1, "IsIgnore": False},
                        {"Id": 3, "IsIgnore": True},
                    ],
                    use_bin_type=True,
                )
            )
            (master / "SpecialIconItemMB").write_bytes(
                msgpack.packb(
                    [
                        {"Id": 9, "CharacterId": 2, "IconId": 1},
                        {"Id": 7, "CharacterId": 1, "IconId": 2},
                    ],
                    use_bin_type=True,
                )
            )
            output = root / "player_avatar_inventory.json"

            synced = sync_player_avatar_inventory(master, "1700000000000", output)
            unchanged = sync_player_avatar_inventory(
                master, "1700000001000", output
            )
            verified = verify_player_avatar_inventory(
                master, "1700000001000", output
            )

            inventory = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(synced["standard_player_icon_count"], 2)
            self.assertEqual(unchanged["status"], "current")
            self.assertEqual(unchanged["master_version"], "1700000000000")
            self.assertEqual(verified["checked_master_version"], "1700000001000")
            self.assertEqual(verified["special_player_icon_count"], 2)
            self.assertEqual(inventory["standard_player_icons"], [1, 2])
            self.assertEqual(
                inventory["special_player_icons"], [[7, 1, 2], [9, 2, 1]]
            )

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
