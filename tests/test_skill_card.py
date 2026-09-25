import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from skill_card import (
    CardAssets,
    COMPACT_AVATAR_SIZE,
    COMPACT_HEIGHT,
    COMPACT_ICON_SIZE,
    COMPACT_TEMPLATE_VERSION,
    COMPACT_WIDTH,
    FontSet,
    COMPACT_WATERMARK,
    _draw_compact_watermark,
    _font,
    _font_collection_index,
    _compact_skill_text,
    _compact_body_layout,
    _compact_skill_records,
    _compact_identity_copy,
    _compact_weapon_effect_rows,
    _card_manifest_key,
    _level_label,
    _minify_compact_text,
    _font_candidates,
    _resolve_character_jsons,
    _sha256,
    _update_card_manifest,
    render_skill_card,
    verify_latest_skill_cards,
)


def _skill(skill_id, kind, name):
    return {
        "id": skill_id,
        "kind": kind,
        "names": {"zh-CN": name},
        "max_cooldown": 4 if kind == "active" else None,
        "levels": [
            {
                "order": 1,
                "character_level": 1,
                "equipment_rarity_flags": 0,
                "descriptions": {"zh-CN": "造成攻击力百分比的伤害。"},
                "source_memo_ja": {"label": "Lv1", "text": "source"},
            },
            {
                "order": 2,
                "character_level": 240,
                "equipment_rarity_flags": 128,
                "descriptions": {"zh-CN": "强化技能效果。"},
                "source_memo_ja": {"label": "Ex1", "text": "source ex"},
            },
        ],
    }


class SkillCardTests(unittest.TestCase):
    def test_compact_watermark_is_right_aligned_with_expected_author(self):
        fonts = FontSet(
            Path("regular.ttf"),
            Path("medium.ttf"),
            Path("serif.ttf"),
        )
        draw = Mock()
        draw.textlength.return_value = 240.4
        face = object()

        with patch("skill_card._font", return_value=face):
            position = _draw_compact_watermark(
                draw,
                fonts,
                right=1000,
                top=82,
            )

        self.assertEqual(COMPACT_WATERMARK, "Made By 光时")
        self.assertEqual(position, (760, 82))
        draw.text.assert_called_once_with(
            (760, 82),
            "Made By 光时",
            font=face,
            fill=(184, 177, 164, 255),
        )

    def test_font_collection_resolves_simplified_chinese_face_by_name(self):
        family_names = [
            "Noto Sans CJK JP",
            "Noto Sans CJK KR",
            "Noto Sans CJK SC",
        ]

        class FakeFace:
            def __init__(self, family):
                self.family = family

            def getname(self):
                return self.family, "Regular"

        with patch(
            "skill_card.ImageFont.truetype",
            side_effect=lambda _path, _size, index=0: FakeFace(family_names[index]),
        ):
            self.assertEqual(_font_collection_index(Path("noto.ttc")), 2)

    def test_font_uses_explicit_collection_face_index(self):
        fonts = FontSet(
            Path("regular.ttc"),
            Path("bold.ttc"),
            Path("serif.ttc"),
            sans_index=2,
            medium_index=3,
            serif_index=4,
        )

        with patch("skill_card.ImageFont.truetype") as truetype:
            _font(fonts, 42)
            truetype.assert_called_once_with("regular.ttc", 42, index=2)

        with patch("skill_card.ImageFont.truetype") as truetype:
            _font(fonts, 43, medium=True)
            truetype.assert_called_once_with("bold.ttc", 43, index=3)

        with patch("skill_card.ImageFont.truetype") as truetype:
            _font(fonts, 44, serif=True)
            truetype.assert_called_once_with("serif.ttc", 44, index=4)

    def test_equipment_rarity_uses_ex_label(self):
        self.assertEqual(
            _level_label({"order": 4, "equipment_rarity_flags": 128}), "Ex1"
        )
        self.assertEqual(
            _level_label({"order": 3, "equipment_rarity_flags": 0}), "Lv3"
        )

    def test_latest_resolves_every_character_at_same_release_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            skills = Path(temporary)
            characters = skills / "characters"
            characters.mkdir()
            for character_id in (7, 3):
                (characters / f"{character_id:06d}.json").write_text(
                    "{}", encoding="utf-8"
                )
            (skills / "latest.json").write_text(
                json.dumps({"latest_characters": [{"id": 7}, {"id": 3}]}),
                encoding="utf-8",
            )

            paths = _resolve_character_jsons(skills, None, True, False)

            self.assertEqual(
                [path.name for path in paths], ["000007.json", "000003.json"]
            )

    def test_compact_identity_wraps_localized_subtitle_and_includes_element(self):
        character = {
            "id": 151,
            "element_type": 5,
            "names": {"zh-CN": "福尔蒂娜", "ja-JP": "フォルティナ"},
            "subtitles": {"zh-CN": "黄昏之约", "ja-JP": "黄昏の約束"},
        }

        self.assertEqual(
            _compact_identity_copy(character, "zh-CN"),
            ("福尔蒂娜", "【黄昏之约】", "天"),
        )

    def test_compact_text_merges_all_numeric_upgrades_and_removes_dialogue(self):
        skill = {
            "levels": [
                {
                    "descriptions": {
                        "zh-CN": "“一起赢下这场战斗！”佛罗伦斯使生命值百分比最低的友军恢复佛罗伦斯攻击力×5%的生命值。“这就是我的觉悟！”再随机对敌人进行2次攻击，每次造成攻击力×520%的物理伤害。第7回合起，发动此技能不会恢复生命值，造成的物理伤害提升为攻击力×1040%。"
                    }
                },
                {
                    "descriptions": {
                        "zh-CN": "生命值恢复量提升为佛罗伦斯攻击力×15%。"
                    }
                },
                {
                    "descriptions": {
                        "zh-CN": "造成的物理伤害提升为攻击力×890%。第7回合起，造成的物理伤害提升为攻击力×1780%。"
                    }
                },
                {
                    "descriptions": {
                        "zh-CN": "强化闪光瞬辉斩，攻击次数增加为3次。"
                    }
                },
                {
                    "descriptions": {
                        "zh-CN": "强化闪光瞬辉斩，造成的物理伤害提升为攻击力×980%。第7回合起，造成的物理伤害提升为攻击力×1960%。"
                    }
                },
            ]
        }

        self.assertEqual(
            _compact_skill_text(skill, "zh-CN"),
            "佛罗伦斯使生命值百分比最低的友军恢复佛罗伦斯攻击力×15%的生命值。"
            "再随机对敌人进行3次攻击，每次造成攻击力×980%的物理伤害。"
            "第7回合起，发动此技能不会恢复生命值，造成的物理伤害提升为攻击力×1960%。",
        )

    def test_compact_text_minifies_common_master_phrases(self):
        source = (
            "战斗开始时，佛罗伦斯强化自己的普通攻击，并额外减少40%自身承受伤害，"
            "使自己增加100%最大生命值（无法被解除），效果持续10回合。"
            "当生命值恢复目标原本的生命值为50%以上，且身上附带控制效果时，"
            "额外解除目标身上的所有控制效果。"
        )

        self.assertEqual(
            _minify_compact_text(source),
            "战斗开始时，强化普通攻击，承受伤害-40%、最大生命值+100%（无法解除），持续10回合。"
            "若恢复前目标生命值≥50%且附带控制效果，解除其所有控制效果。",
        )

    def test_compact_text_combines_matching_defense_buffs(self):
        source = (
            "福尔蒂娜使自身及速度高于自身的友军增加防御力，"
            "增幅为福尔蒂娜防御力×50%，效果持续3回合（无法被解除）。"
            "再随机对5名敌人造成攻击力×480%的物理伤害。"
            "发动攻击前，福尔蒂娜使自身及速度高于自身的友军额外增加物理防御力与魔法防御力，"
            "增幅分别为福尔蒂娜物理防御力×50%及福尔蒂娜魔法防御力×50%，"
            "效果持续3回合（无法被解除）。"
        )

        self.assertEqual(
            _minify_compact_text(source),
            "攻击前，使自身及速度高于自身的友军三防增加自身对应三防×50%，"
            "持续3回合（无法解除）。"
            "随后随机攻击5名敌人，造成攻击力×480%的物理伤害。",
        )

    def test_compact_weapon_effect_rows_include_standalone_ur_effect(self):
        weapon = {
            "skill_effects": [
                {
                    "equipment_rarity_flags": 128,
                    "descriptions": {"zh-CN": "强化技能一。"},
                },
                {
                    "equipment_rarity_flags": 256,
                    "descriptions": {
                        "zh-CN": "战斗开始时，福尔蒂娜获得2层「多重屏障」（无法被解除）。"
                        "当她受到最大生命值×10%以上的伤害时，消耗1层屏障来抵消该伤害。"
                    },
                },
                {
                    "equipment_rarity_flags": 512,
                    "descriptions": {
                        "zh-CN": "强化神意的天平，造成的物理伤害提升为攻击力×610%。"
                    },
                },
            ]
        }

        self.assertEqual(
            _compact_weapon_effect_rows(weapon, "zh-CN"),
            [
                (
                    "UR专效果",
                    "战斗开始时，获得2层「多重屏障」（无法解除）。"
                    "受到的伤害达到最大生命值的10%以上时，消耗1层并抵消该伤害。",
                ),
                ("LR专效果", "造成的物理伤害提升为攻击力×610%。"),
            ],
        )

    def test_compact_renderer_waits_for_complete_official_localization(self):
        payload = {
            "character": {"id": 42},
            "localization_complete": False,
        }
        assets = CardAssets(
            art=Path("missing.png"),
            icons={},
            asset_version="asset",
        )

        with self.assertRaisesRegex(RuntimeError, "官方 zh-CN 技能文本尚未完整"):
            render_skill_card(
                payload,
                assets,
                Path("unused.png"),
                mode="compact",
            )

    def test_compact_skills_exclude_unnamed_exclusive_effect_row(self):
        payload = {
            "character": {"id": 151},
            "active_skills": [
                _skill(151001, "active", "主动一"),
                _skill(151002, "active", "主动二"),
            ],
            "passive_skills": [
                _skill(151003, "passive", "被动一"),
                _skill(151004, "passive", "被动二"),
                {
                    **_skill(151005, "passive", "unused"),
                    "name_key": "*",
                    "names": {"zh-CN": None},
                    "master_record": {"NameKey": "*"},
                },
            ],
            "localization_complete": False,
        }

        skills = _compact_skill_records(payload, "zh-CN")

        self.assertEqual(
            [skill["id"] for skill in skills],
            [151001, 151002, 151003, 151004],
        )

    def test_compact_text_keeps_conditional_damage_and_final_shield_values(self):
        conditional = _skill(97002, "active", "条件伤害")
        conditional["levels"] = [
            {"descriptions": {"zh-CN": "随机对2名敌人造成攻击力×280%的物理伤害。"}},
            {"descriptions": {"zh-CN": "造成的物理伤害提升为攻击力×580%。"}},
            {"descriptions": {"zh-CN": "攻击目标增加为3名随机敌人。"}},
            {
                "descriptions": {
                    "zh-CN": "发动攻击前，如果库希与友军在战斗中因技能效果解除的弱化效果总数达5种以上，造成的物理伤害提升为攻击力×870%。"
                }
            },
        ]
        shield = _skill(97004, "passive", "护盾")
        shield["levels"] = [
            {
                "descriptions": {
                    "zh-CN": "第1回合开始时，库希使全体友军获得库希攻击力×40%的「护盾」，效果持续6回合（无法被解除）。"
                }
            },
            {
                "descriptions": {
                    "zh-CN": "当「护盾」的附加目标为忧蓝属性时，「护盾」值提升为库希攻击力×200%。"
                }
            },
            {
                "descriptions": {
                    "zh-CN": "战斗开始时，库希额外减少40%自身承受伤害（无法被解除）。"
                }
            },
            {
                "descriptions": {
                    "zh-CN": "强化离巢的时刻，「护盾」值提升为库希攻击力×100%。当附加目标为忧蓝属性时，「护盾」值提升为库希攻击力×500%。"
                }
            },
        ]

        conditional_text = _compact_skill_text(conditional, "zh-CN")
        shield_text = _compact_skill_text(shield, "zh-CN")

        self.assertIn("随机对3名敌人造成攻击力×580%的物理伤害", conditional_text)
        self.assertIn("若库希与友军通过技能累计解除弱化≥5种，伤害提升至攻击力×870%", conditional_text)
        self.assertNotIn("随机对2名敌人", conditional_text)
        self.assertIn("库希攻击力×100%的「护盾」", shield_text)
        self.assertIn("忧蓝属性目标的「护盾」提升至库希攻击力×500%", shield_text)
        self.assertNotIn("攻击力×40%", shield_text)
        self.assertNotIn("攻击力×200%", shield_text)

    def test_compact_text_excludes_lr_exclusive_upgrade(self):
        skill = _skill(97001, "active", "雏鸟爪击")
        skill["levels"] = [
            {
                "equipment_rarity_flags": 0,
                "descriptions": {
                    "zh-CN": "库希随机对3名敌人造成攻击力×230%的物理伤害。"
                },
            },
            {
                "equipment_rarity_flags": 0,
                "descriptions": {
                    "zh-CN": "造成的物理伤害提升为攻击力×380%。"
                },
            },
            {
                "equipment_rarity_flags": 128,
                "descriptions": {"zh-CN": "强化雏鸟爪击，攻击目标增加为5名随机敌人。"},
            },
            {
                "equipment_rarity_flags": 512,
                "descriptions": {
                    "zh-CN": "强化雏鸟爪击，造成的物理伤害提升为攻击力×420%。"
                },
            },
        ]

        text = _compact_skill_text(skill, "zh-CN")

        self.assertEqual(text, "库希随机对5名敌人造成攻击力×380%的物理伤害。")
        self.assertNotIn("420%", text)

    def test_latest_kushi_skill_copy_fits_compact_panels(self):
        # Text measured from the latest official Master payload for character 97.
        texts = (
            "库希随机对5名敌人造成攻击力×380%的物理伤害。攻击结束后，使其他友军与5名随机敌人吸血-50%，效果持续4回合。",
            "随机对3名敌人造成攻击力×580%的物理伤害。若库希与友军通过技能累计解除弱化≥5种，伤害提升至攻击力×870%。",
            "敌人攻击后若库希存活，30%概率解除弱化最多的友军1种弱化。判定前若没有友军带弱化，使攻击力最高的2名友军攻击力+30%，持续1回合（无法解除）。",
            "第1回合开始，为全体友军附加库希攻击力×100%的「护盾」，效果持续6回合（无法解除）。忧蓝属性目标的「护盾」提升至库希攻击力×500%。战斗开始时，自身承受伤害-40%（无法解除）。",
        )
        draw = ImageDraw.Draw(Image.new("RGB", (COMPACT_WIDTH, COMPACT_HEIGHT)))
        fonts = _font_candidates()
        panel_width = COMPACT_WIDTH - 2 * 60 - 160

        for text in texts:
            with self.subTest(text=text[:12]):
                body, _, _, _ = _compact_body_layout(draw, text, fonts, panel_width, 326)
                self.assertGreaterEqual(body.size, 44)

    def test_renderer_accepts_master_json_without_character_specific_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            art = root / "art.png"
            icon1 = root / "icon1.png"
            icon2 = root / "icon2.png"
            Image.new("RGB", (1024, 1024), (220, 150, 80)).save(art)
            Image.new("RGBA", (100, 100), (150, 90, 40, 255)).save(icon1)
            Image.new("RGBA", (100, 100), (50, 90, 130, 255)).save(icon2)
            payload = {
                "character": {
                    "id": 42,
                    "memo": "fixture",
                    "names": {"zh-CN": "测试角色"},
                    "subtitles": {"zh-CN": "测试称号"},
                    "element_type": 3,
                    "job_flags": 1,
                },
                "active_skills": [_skill(42001, "active", "主动测试")],
                "passive_skills": [_skill(42002, "passive", "被动测试")],
                "localization_complete": True,
                "source": {"master_version": "1"},
            }
            assets = CardAssets(
                art=art,
                icons={42001: icon1, 42002: icon2},
                asset_version="asset",
            )
            output = root / "card.png"
            render_skill_card(payload, assets, output)

            with Image.open(output) as rendered:
                self.assertEqual(rendered.size, (3840, 2160))

    def test_compact_renderer_uses_portrait_canvas_and_larger_avatar_tier(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            art = root / "art.png"
            icon = root / "icon.png"
            Image.new("RGB", (1024, 1024), (220, 150, 80)).save(art)
            Image.new("RGBA", (100, 100), (150, 90, 40, 255)).save(icon)
            payload = {
                "character": {
                    "id": 42,
                    "names": {"zh-CN": "测试角色"},
                    "subtitles": {"zh-CN": "测试称号"},
                },
                "active_skills": [
                    _skill(42001, "active", "主动一"),
                    _skill(42002, "active", "主动二"),
                ],
                "passive_skills": [
                    _skill(42003, "passive", "被动一"),
                    _skill(42004, "passive", "被动二"),
                ],
                "exclusive_weapon": {
                    "names": {"zh-CN": "测试专武"},
                    "passive_effects": [
                        {"parameter_code": "Muscle", "value_text": "+8%"},
                        {"parameter_code": "AttackPower", "value_text": "+18%"},
                        {"parameter_code": "Critical", "value_text": "+10%"},
                    ],
                    "skill_effects": [
                        {"descriptions": {"zh-CN": "强化主动一。"}},
                        {"descriptions": {"zh-CN": "强化被动一。"}},
                        {
                            "descriptions": {
                                "zh-CN": "强化主动一，造成的物理伤害提升为攻击力×980%。"
                                "第7回合起，造成的物理伤害提升为攻击力×1960%。"
                            }
                        },
                    ],
                },
                "arcanas": [
                    {
                        "id": 9,
                        "names": {"zh-CN": "测试秘仪"},
                        "levels": [
                            {
                                "character_rarity_flags": 512,
                                "effects": [
                                    {
                                        "parameter_code": "AttackPower",
                                        "value_text": "+2%",
                                    }
                                ],
                            }
                        ],
                        "required_characters": [
                            {"id": 42, "names": {"zh-CN": "测试角色"}},
                            {"id": 0, "names": {"zh-CN": None}},
                        ],
                    }
                ],
            }
            assets = CardAssets(
                art=art,
                avatar=icon,
                weapon=icon,
                element=icon,
                icons={skill_id: icon for skill_id in (42001, 42002, 42003, 42004)},
                arcana_characters={42: icon},
                asset_version="asset",
            )
            skills_dir = root / "skills"
            characters_dir = skills_dir / "characters"
            characters_dir.mkdir(parents=True)
            source = characters_dir / "000042.json"
            source.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            (skills_dir / "latest.json").write_text(
                json.dumps({"latest_characters": [{"id": 42}]}),
                encoding="utf-8",
            )
            output_dir = root / "cards" / "characters"
            output = output_dir / "character-000042-compact-zh-CN.png"
            render_skill_card(payload, assets, output, mode="compact")

            with Image.open(output) as rendered:
                self.assertEqual(rendered.size, (COMPACT_WIDTH, COMPACT_HEIGHT))
            self.assertGreater(COMPACT_AVATAR_SIZE, COMPACT_ICON_SIZE)
            _update_card_manifest(
                root / "cards" / "manifest.json",
                [
                    {
                        "key": _card_manifest_key(42, "zh-CN", "compact"),
                        "character_id": 42,
                        "language": "zh-CN",
                        "mode": "compact",
                        "path": "characters/character-000042-compact-zh-CN.png",
                        "source_sha256": _sha256(source),
                        "template_version": COMPACT_TEMPLATE_VERSION,
                        "asset_version": "asset",
                        "width": COMPACT_WIDTH,
                        "height": COMPACT_HEIGHT,
                        "sha256": _sha256(output),
                    }
                ],
            )
            self.assertEqual(
                verify_latest_skill_cards(skills_dir, output_dir),
                [output],
            )
            payload["character"]["names"]["zh-CN"] = "已更新角色"
            source.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "已过期"):
                verify_latest_skill_cards(skills_dir, output_dir)


if __name__ == "__main__":
    unittest.main()
