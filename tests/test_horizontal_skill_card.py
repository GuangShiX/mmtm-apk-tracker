import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw
import msgpack

from asset_cdn import get_official_asset_info

from skill_card import (
    CardAssets,
    SPLIT_HEIGHT,
    SPLIT_TEMPLATE_VERSION,
    SPLIT_WIDTH,
    main,
    parse_args,
    render_skill_card,
    verify_latest_skill_cards,
)


TEXTS = [
    "阿尔托莉亚使生命值百分比最低的友军恢复阿尔托莉亚攻击力×30%的生命值。随后随机攻击3名敌人，造成攻击力×660%的物理伤害。发动攻击前，使全体友军额外增加30%弱化效果抵抗率，效果持续2回合。",
    "阿尔托莉亚使生命值百分比最低的友军恢复阿尔托莉亚攻击力×30%的生命值。再随机对敌人进行4次攻击，每次造成攻击力×2160%的物理伤害。",
    "阿尔托莉亚或友军的主动技能恢复生命值时，阿尔托莉亚使自己与攻击力最高的其他友军获得3层「圣剑光辉」（最多3层，无法解除）。如果「圣剑光辉」的附带者在受到魔法攻击后仍然存活，阿尔托莉亚消耗1层附带者身上的「圣剑光辉」，使附带者恢复承受伤害×20%的生命值（最多10%最大生命值）。当阿尔托莉亚失去战斗能力时，无法触发此技能的生命值恢复效果，并解除自己与友军身上附带的「圣剑光辉」。「圣剑光辉」的附加目标增加为阿尔托莉亚与攻击力最高的2名其他友军。生命值恢复量提升为承受伤害×80%（最多20%最大生命值）。「圣剑光辉」的附加目标增加为全体友军。",
    "第1回合行动开始时，阿尔托莉亚使自己与攻击力最高的其他友军增加30%魔法防御力（无法解除）。技能触发时，如果存活在场上且与阿尔托莉亚属性相同的其他友军或天光属性的友军数为2名以上，再使自己与攻击力最高的其他友军增加100%最大生命值（无法解除）。战斗开始时，阿尔托莉亚使自己额外增加20%最大生命值，减少60%自身承受伤害（无法解除）。",
]


class HorizontalSkillCardTests(unittest.TestCase):
    def test_official_banner_url_is_preserved_by_version_discovery(self):
        response = type("Response", (), {
            "headers": {"ortegaassetversion": "asset", "ortegamasterversion": "master"},
            "content": msgpack.packb({
                "AssetCatalogFixedUriFormat": "https://assets.example/{0}",
                "RawDataUriFormat": "https://raw.example/{0}",
            }),
        })()
        with patch("asset_cdn._request_with_retries", return_value=response):
            info = get_official_asset_info("4.23.0")
        self.assertEqual(
            info.raw_data_url("MyPageBanner/CN/MYPAGE_BANNER_CN_010154.png"),
            "https://raw.example/MyPageBanner/CN/MYPAGE_BANNER_CN_010154.png",
        )

    def test_cli_defaults_to_landscape_and_rejects_portrait_modes(self):
        with patch("sys.argv", ["skill_card.py", "--latest"]):
            self.assertEqual(parse_args().mode, "split")
        for mode in ("compact", "both"):
            with self.subTest(mode=mode), contextlib.redirect_stderr(io.StringIO()):
                with patch("sys.argv", ["skill_card.py", "--latest", "--mode", mode]):
                    with self.assertRaises(SystemExit):
                        parse_args()

    def test_public_render_and_verify_reject_portrait_mode(self):
        with self.assertRaisesRegex(ValueError, "竖版模板已停用"):
            render_skill_card({}, CardAssets(art=None, icons={}, asset_version="fixture"),
                              Path("unused.png"), mode="compact")
        with self.assertRaisesRegex(ValueError, "竖版模板已停用"):
            verify_latest_skill_cards(Path("unused"), Path("unused"), mode="compact")

    def test_latest_long_passives_generate_and_verify_with_default_landscape_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            art = root / "art.png"
            icon = root / "icon.png"
            Image.new("RGBA", (1024, 1024), (220, 150, 80, 255)).save(art)
            Image.new("RGBA", (92, 92), (68, 116, 153, 255)).save(icon)
            skills = root / "skills"
            characters = skills / "characters"
            characters.mkdir(parents=True)
            records = [
                {
                    "id": 154001 + index,
                    "kind": "active" if index < 2 else "passive",
                    "names": {"zh-CN": name},
                    "max_cooldown": 4 if index < 2 else None,
                    "levels": [{"order": 1, "descriptions": {"zh-CN": text}}],
                }
                for index, (name, text) in enumerate(
                    zip(["破魔圣剑", "审判圣剑", "黄金圣剑士", "同伴情谊"], TEXTS)
                )
            ]
            payload = {
                "character": {"id": 154, "speed": 3161, "names": {"zh-CN": "阿尔托莉亚"}},
                "active_skills": records[:2],
                "passive_skills": records[2:],
                "exclusive_weapon": {
                    "names": {"zh-CN": "测试专武"},
                    "passive_effects": [{"parameter_code": "AttackPower", "value_text": "+18%"}],
                    "skill_effects": [
                        {"descriptions": {"zh-CN": "强化破魔圣剑。"}},
                        {"descriptions": {"zh-CN": "强化黄金圣剑士。"}},
                        {"descriptions": {"zh-CN": "强化审判圣剑，造成的伤害提升。"}},
                    ],
                },
                "localization_complete": True,
                "source": {"master_version": "1791264614625"},
            }
            (characters / "000154.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            (skills / "latest.json").write_text(
                json.dumps({"latest_characters": [{"id": 154}]}), encoding="utf-8"
            )
            output = root / "cards" / "characters"
            args = [
                "skill_card.py", "--latest", "--skills-dir", str(skills),
                "--output-dir", str(output),
            ]
            with patch("sys.argv", args), patch(
                "skill_card.fetch_official_card_assets",
                return_value=CardAssets(
                    art=art, avatar=icon, weapon=icon, icons={},
                    element=icon, asset_version="fixture"
                ),
            ), patch("skill_card.ImageDraw.ImageDraw.text", autospec=True,
                     side_effect=ImageDraw.ImageDraw.text) as drawn_text:
                self.assertEqual(main(), 0)
            self.assertIn("初始速度 3161", [call.args[2] for call in drawn_text.call_args_list])
            generated = verify_latest_skill_cards(skills, output)
            self.assertEqual([path.name for path in generated],
                             ["character-000154-split-zh-CN.png"])
            with Image.open(generated[0]) as image:
                self.assertEqual(image.size, (SPLIT_WIDTH, SPLIT_HEIGHT))
                self.assertGreater(image.width, image.height)
            args[1] = "--verify-latest"
            with patch("sys.argv", args), patch(
                "skill_card.fetch_official_card_assets",
                side_effect=AssertionError("Verification must stay offline"),
            ):
                self.assertEqual(main(), 0)
            manifest_path = output.parent / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(set(manifest["cards"]), {"000154:zh-CN:split"})
            entry = manifest["cards"]["000154:zh-CN:split"]
            self.assertEqual(entry["initial_speed"], 3161)
            self.assertEqual(entry["template_version"], SPLIT_TEMPLATE_VERSION)
            entry["template_version"] = "portrait-compact-v8"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "template_version"):
                verify_latest_skill_cards(skills, output)


if __name__ == "__main__":
    unittest.main()
