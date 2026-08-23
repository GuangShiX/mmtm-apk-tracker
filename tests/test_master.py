import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import msgpack

from master import (
    JST,
    MasterSource,
    build_master_skill_repository,
    load_current_master_skill_repository,
)


def _character(character_id, start_time, active_id, passive_id):
    return {
        "Id": character_id,
        "IsIgnore": None,
        "Memo": f"character-{character_id}",
        "ActiveSkillIds": [active_id],
        "PassiveSkillIds": [passive_id],
        "NameKey": f"[CharacterName{character_id}]",
        "Name2Key": f"[CharacterSubName{character_id}]",
        "CharacterType": 2,
        "ElementType": 3,
        "JobFlags": 1,
        "InitialBattleParameter": {"Speed": 3000 + character_id},
        "StartTimeFixJST": start_time,
        "EndTimeFixJST": "2100-12-31 23:59:59",
    }


def _active(skill_id):
    return {
        "Id": skill_id,
        "Memo": f"test:Lv1 source {skill_id}/ Ex1 enhanced {skill_id}",
        "NameKey": f"[ActiveSkillName{skill_id}]",
        "ActiveSkillConditions": "True",
        "SkillInitCoolTime": 0,
        "SkillMaxCoolTime": 4,
        "ActiveSkillInfos": [
            {
                "OrderNumber": 1,
                "DescriptionKey": f"[ActiveSkillDescription{skill_id}1]",
                "CharacterLevel": 1,
                "EquipmentRarityFlags": 0,
                "BlessingItemId": 0,
            },
            {
                "OrderNumber": 2,
                "DescriptionKey": f"[ActiveSkillDescription{skill_id}2]",
                "CharacterLevel": 240,
                "EquipmentRarityFlags": 128,
                "BlessingItemId": 0,
            },
        ],
    }


def _passive(skill_id):
    return {
        "Id": skill_id,
        "Memo": f"test:Lv1 passive {skill_id}",
        "NameKey": f"[PassiveSkillName{skill_id}]",
        "PassiveSkillInfos": [
            {
                "OrderNumber": 1,
                "DescriptionKey": f"[PassiveSkillDescription{skill_id}1]",
                "CharacterLevel": 11,
                "EquipmentRarityFlags": 0,
                "BlessingItemId": 0,
            }
        ],
    }


def _write_master_fixture(root: Path) -> None:
    characters = [
        _character(50, "2026-01-01 15:00:00", 50001, 50002),
        _character(5, "2026-02-01 15:00:00", 5001, 5002),
        _character(100, "2100-01-01 15:00:00", 100001, 100002),
    ]
    active = [_active(50001), _active(5001), _active(100001)]
    passive = [_passive(50002), _passive(5002), _passive(100002)]
    text = []
    for key, value in {
        "[CharacterName50]": "larger-id-older",
        "[CharacterSubName50]": "old",
        "[ActiveSkillName50001]": "active",
        "[ActiveSkillDescription500011]": "active lv1",
        "[ActiveSkillDescription500012]": "active ex1",
        "[PassiveSkillName50002]": "passive",
        "[PassiveSkillDescription500021]": "passive lv1",
        "[EquipmentName900]": "fixture weapon",
        "[EquipmentExclusiveSkill5Description1]": "weapon effect 1",
        "[EquipmentExclusiveSkill5Description2]": "weapon effect 2",
        "[EquipmentExclusiveSkill5Description3]": "weapon effect 3",
        "[CharacterCollectionName5]": "fixture arcana",
    }.items():
        text.append({"StringKey": key, "Text": value})
    tables = {
        "CharacterMB": characters,
        "ActiveSkillMB": active,
        "PassiveSkillMB": passive,
        "EquipmentMB": [
            {
                "Id": 501110240,
                "EquipmentLv": 240,
                "RarityFlags": 512,
                "IconId": 901,
                "NameKey": "[EquipmentName900]",
                "ExclusiveEffectId": 7005,
                "EquipmentExclusiveSkillDescriptionId": 805,
            }
        ],
        "EquipmentExclusiveEffectMB": [
            {
                "Id": 7005,
                "CharacterId": 5,
                "BaseParameterChangeInfoList": [
                    {
                        "BaseParameterType": 1,
                        "ChangeParameterType": 2,
                        "Value": 800.0,
                    }
                ],
                "BattleParameterChangeInfoList": [
                    {
                        "BattleParameterType": 2,
                        "ChangeParameterType": 2,
                        "Value": 1800.0,
                    },
                    {
                        "BattleParameterType": 12,
                        "ChangeParameterType": 1,
                        "Value": 7000.0,
                    },
                ],
            }
        ],
        "EquipmentExclusiveSkillDescriptionMB": [
            {
                "Id": 805,
                "Description1Key": "[EquipmentExclusiveSkill5Description1]",
                "Description2Key": "[EquipmentExclusiveSkill5Description2]",
                "Description3Key": "[EquipmentExclusiveSkill5Description3]",
            }
        ],
        "CharacterCollectionMB": [
            {
                "Id": 5,
                "NameKey": "[CharacterCollectionName5]",
                "RequiredCharacterIds": [5, 0],
                "RequiredPartyLv": 0,
                "StartTimeFixJST": "2026-02-01 00:00:00",
                "EndTimeFixJST": "2100-01-01 00:00:00",
            }
        ],
        "CharacterCollectionLevelMB": [
            {
                "Id": 501,
                "CollectionId": 5,
                "CollectionLevel": 1,
                "CharacterRarityFlags": 8,
                "CharacterRarityBonus": 0,
                "MaxLevelIncreaseValue": 0,
                "BaseParameterChangeInfos": None,
                "BattleParameterChangeInfos": [
                    {
                        "BattleParameterType": 2,
                        "ChangeParameterType": 2,
                        "Value": 100.0,
                    }
                ],
            },
            {
                "Id": 502,
                "CollectionId": 5,
                "CollectionLevel": 2,
                "CharacterRarityFlags": 512,
                "CharacterRarityBonus": 1,
                "MaxLevelIncreaseValue": 0,
                "BaseParameterChangeInfos": [
                    {
                        "BaseParameterType": 4,
                        "ChangeParameterType": 3,
                        "Value": 35.0,
                    }
                ],
                "BattleParameterChangeInfos": [
                    {
                        "BattleParameterType": 2,
                        "ChangeParameterType": 2,
                        "Value": 250.0,
                    }
                ],
            },
        ],
        "TextResourceZhCnMB": text,
    }
    catalog = {}
    for name, table in tables.items():
        data = msgpack.packb(table, use_bin_type=True)
        (root / name).write_bytes(data)
        catalog[name] = {
            "Name": name,
            "Size": len(data),
            "Hash": hashlib.md5(data, usedforsecurity=False).hexdigest(),
        }
    (root / "master-catalog").write_bytes(
        msgpack.packb({"MasterBookInfoMap": catalog}, use_bin_type=True)
    )


class MasterSkillTests(unittest.TestCase):
    def test_verified_current_master_repository_skips_same_version_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master_dir = root / "master"
            master_dir.mkdir()
            _write_master_fixture(master_dir)
            repository = root / "repository"
            as_of = datetime(2026, 8, 13, 16, tzinfo=JST)
            build_master_skill_repository(
                master_dir,
                repository,
                "owner/repository",
                MasterSource("4.20.0", "asset", "1786600691299"),
                as_of=as_of,
            )

            current = load_current_master_skill_repository(
                repository,
                "1786600691299",
                ("zh-CN",),
                as_of,
            )

            self.assertIsNotNone(current)
            self.assertEqual(
                [item["id"] for item in current["latest_characters"]],
                [5],
            )
            self.assertEqual(current["next_release_time_jst"], "2100-01-01 15:00:00")
            self.assertIsNone(
                load_current_master_skill_repository(
                    repository,
                    "1786600691299",
                    ("zh-CN",),
                    datetime(2100, 1, 1, 15, tzinfo=JST),
                )
            )

    def test_latest_uses_release_time_not_largest_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master_dir = root / "master"
            master_dir.mkdir()
            _write_master_fixture(master_dir)

            latest = build_master_skill_repository(
                master_dir,
                root / "repository",
                "owner/repository",
                MasterSource("4.20.0", "asset", "1786600691299"),
                as_of=datetime(2026, 8, 13, 16, tzinfo=JST),
            )

            self.assertEqual(
                [item["id"] for item in latest["latest_characters"]], [5]
            )
            index = json.loads(
                (root / "repository/skills/index.json").read_text(encoding="utf-8")
            )
            self.assertEqual([item["id"] for item in index["characters"]], [50, 5])
            self.assertFalse((root / "repository/skills/characters/000100.json").exists())

    def test_missing_localization_keeps_japanese_memo_sections(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master_dir = root / "master"
            master_dir.mkdir()
            _write_master_fixture(master_dir)
            build_master_skill_repository(
                master_dir,
                root / "repository",
                "owner/repository",
                MasterSource("4.20.0", "asset", "1786600691299"),
                as_of=datetime(2026, 8, 13, 16, tzinfo=JST),
            )

            payload = json.loads(
                (root / "repository/skills/characters/000005.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(payload["localization_complete"])
            self.assertIn("[CharacterName5]", payload["missing_localization_keys"])
            self.assertEqual(
                [level["source_memo_ja"]["label"] for level in payload["active_skills"][0]["levels"]],
                ["Lv1", "Ex1"],
            )

    def test_exclusive_weapon_exports_lr_passives_and_descriptions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master_dir = root / "master"
            master_dir.mkdir()
            _write_master_fixture(master_dir)
            build_master_skill_repository(
                master_dir,
                root / "repository",
                "owner/repository",
                MasterSource("4.20.0", "asset", "1786600691299"),
                as_of=datetime(2026, 8, 13, 16, tzinfo=JST),
            )

            payload = json.loads(
                (root / "repository/skills/characters/000005.json").read_text(
                    encoding="utf-8"
                )
            )
            weapon = payload["exclusive_weapon"]
            self.assertEqual(weapon["names"]["zh-CN"], "fixture weapon")
            self.assertEqual(
                [effect["value_text"] for effect in weapon["passive_effects"]],
                ["+8%", "+18%", "+7,000"],
            )
            self.assertEqual(
                weapon["skill_effects"][2]["descriptions"]["zh-CN"],
                "weapon effect 3",
            )
            arcana = payload["arcanas"][0]
            self.assertEqual(arcana["names"]["zh-CN"], "fixture arcana")
            self.assertEqual(
                arcana["required_characters"],
                [
                    {"id": 5, "names": {"zh-CN": None}},
                    {"id": 0, "names": {"zh-CN": None}},
                ],
            )
            self.assertEqual(
                [effect["value_text"] for effect in arcana["levels"][-1]["effects"]],
                ["+35×角色等级", "+2.5%"],
            )
            self.assertEqual(arcana["levels"][-1]["character_rarity_bonus"], 1)

    def test_catalog_hash_is_required_before_unpacking(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master_dir = root / "master"
            master_dir.mkdir()
            _write_master_fixture(master_dir)
            with (master_dir / "CharacterMB").open("ab") as stream:
                stream.write(b"corrupt")
            with self.assertRaisesRegex(RuntimeError, "大小不一致"):
                build_master_skill_repository(
                    master_dir,
                    root / "repository",
                    "owner/repository",
                    MasterSource("4.20.0", "asset", "1786600691299"),
                )

    def test_refuses_to_replace_unowned_skills_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master_dir = root / "master"
            master_dir.mkdir()
            _write_master_fixture(master_dir)
            skills = root / "repository/skills"
            skills.mkdir(parents=True)
            (skills / "manual.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "拒绝替换"):
                build_master_skill_repository(
                    master_dir,
                    root / "repository",
                    "owner/repository",
                    MasterSource("4.20.0", "asset", "1786600691299"),
                    as_of=datetime(2026, 8, 13, 16, tzinfo=JST),
                )

    def test_rebuild_removes_stale_generated_character(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master_dir = root / "master"
            master_dir.mkdir()
            _write_master_fixture(master_dir)
            arguments = (
                master_dir,
                root / "repository",
                "owner/repository",
                MasterSource("4.20.0", "asset", "1786600691299"),
            )
            as_of = datetime(2026, 8, 13, 16, tzinfo=JST)
            build_master_skill_repository(*arguments, as_of=as_of)
            stale = root / "repository/skills/characters/999999.json"
            stale.write_text("{}", encoding="utf-8")

            build_master_skill_repository(*arguments, as_of=as_of)

            self.assertFalse(stale.exists())

    def test_windows_directory_lock_uses_generated_file_sync_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            master_dir = root / "master"
            master_dir.mkdir()
            _write_master_fixture(master_dir)
            arguments = (
                master_dir,
                root / "repository",
                "owner/repository",
                MasterSource("4.20.0", "asset", "1786600691299"),
            )
            as_of = datetime(2026, 8, 13, 16, tzinfo=JST)
            build_master_skill_repository(*arguments, as_of=as_of)
            stale = root / "repository/skills/characters/999999.json"
            stale.write_text("{}", encoding="utf-8")

            with patch.object(Path, "replace", side_effect=PermissionError("locked")):
                build_master_skill_repository(*arguments, as_of=as_of)

            self.assertFalse(stale.exists())
            self.assertTrue(
                (root / "repository/skills/characters/000005.json").is_file()
            )


if __name__ == "__main__":
    unittest.main()
