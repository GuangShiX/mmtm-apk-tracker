import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import shutil
from datetime import datetime

import msgpack
from PIL import Image

import character_release as release
from asset_cdn import OfficialAssetInfo
from master import CORE_BOOKS, JST, LANGUAGE_BOOKS, MasterSource, build_master_skill_repository
from skill_card import CardAssets, SPLIT_HEIGHT, SPLIT_WIDTH


def fixture_books(raw):
    raw.mkdir(parents=True, exist_ok=True)
    characters, active, passive, texts = [], [], [], []
    for cid, start in ((7, "2026-01-01 15:00:00"), (8, "2026-02-01 15:00:00"),
                       (9, "2100-01-01 15:00:00")):
        active_ids, passive_ids = [cid * 1000 + 1, cid * 1000 + 2], [cid * 1000 + 3, cid * 1000 + 4]
        characters.append({
            "Id": cid, "ActiveSkillIds": active_ids, "PassiveSkillIds": passive_ids,
            "NameKey": f"name-{cid}", "Name2Key": f"subtitle-{cid}", "Memo": f"fixture-{cid}",
            "ElementType": 3, "JobFlags": 1, "CharacterType": 2,
            "StartTimeFixJST": start, "EndTimeFixJST": "2100-12-31 23:59:59",
            "InitialBattleParameter": {"Speed": 3000 + cid},
        })
        texts.extend([{"StringKey": f"name-{cid}", "Text": f"角色{cid}"},
                      {"StringKey": f"subtitle-{cid}", "Text": "测试称号"}])
        for skill_id in active_ids + passive_ids:
            kind = "Active" if skill_id in active_ids else "Passive"
            row = {"Id": skill_id, "NameKey": f"skill-{skill_id}", "SkillMaxCoolTime": 4,
                   f"{kind}SkillInfos": [{"OrderNumber": 1, "CharacterLevel": 1,
                                          "EquipmentRarityFlags": 0,
                                          "DescriptionKey": f"description-{skill_id}"}]}
            (active if kind == "Active" else passive).append(row)
            texts.extend([{"StringKey": f"skill-{skill_id}", "Text": f"技能{skill_id}"},
                          {"StringKey": f"description-{skill_id}", "Text": "使自己增加30%攻击力。"}])
    tables = {name: [] for name in CORE_BOOKS}
    tables.update(CharacterMB=characters, ActiveSkillMB=active, PassiveSkillMB=passive)
    tables.update({LANGUAGE_BOOKS[lang]: texts for lang in ("zh-CN", "ja-JP")})
    packed, catalog = {}, {}
    for name, rows in tables.items():
        content = msgpack.packb(rows, use_bin_type=True)
        packed[name] = content
        (raw / name).write_bytes(content)
        catalog[name] = {"Name": name, "Size": len(content), "Hash": hashlib.md5(content).hexdigest()}
    content = msgpack.packb({"MasterBookInfoMap": catalog}, use_bin_type=True)
    (raw / "master-catalog").write_bytes(content)
    packed["master-catalog"] = content
    return packed


class CharacterReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache, self.output = self.root / "cache", self.root / "output"
        self.info = OfficialAssetInfo("5.0.0", "fixture-snapshot", "1000",
                                      "https://assets.example/{0}",
                                      "https://master.example/{0}/{1}",
                                      "https://raw.example/{0}")
        self.raw = self.cache / "master" / self.info.master_version
        self.books = fixture_books(self.raw)
        self.context = self.root / "source.json"
        release.write_json(self.context, {**release.asdict(self.info), "raw_master_dir": str(self.raw)})
        self.now = datetime(2026, 10, 15, 15, tzinfo=JST)
        self.calls = {"discover": 0, "catalog": 0, "downloads": [], "fetch": [], "render": []}

    def services(self, fail_character=None):
        def discover():
            self.calls["discover"] += 1
            return self.info
        def download(url, target):
            name = url.rsplit("/", 1)[-1]
            self.calls["downloads"].append(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.books[name])
        def catalog(info, target):
            self.assertIs(info, self.info)
            self.calls["catalog"] += 1
            release.write_json(target, {"m_InternalIds": []})
        def fetch(cid, skills, cache_dir, **kwargs):
            self.assertIs(kwargs["official_info"], self.info)
            self.assertEqual(kwargs["catalog_path"], self.cache / self.info.asset_version / "catalog.json")
            self.calls["fetch"].append(cid)
            return self.cache_images(cid)
        def render(payload, assets, target, **kwargs):
            cid = payload["character"]["id"]
            self.calls["render"].append(cid)
            if cid == fail_character:
                raise RuntimeError("interrupted fixture render")
            self.assertEqual(kwargs["mode"], "split")
            target.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (SPLIT_WIDTH, SPLIT_HEIGHT), (20, 30, cid)).save(target)
            return target
        return release.ReleaseServices(
            discover=discover, download=download, catalog_download=catalog,
            fetch_assets=fetch, render=render,
            fonts=lambda: [{"family": "Noto Sans SC", "style": "fixture", "sha256": "font-fixture"}],
        )

    def cache_images(self, cid):
        images = self.cache / self.info.asset_version / f"{cid:06d}" / "images"
        images.mkdir(parents=True, exist_ok=True)
        names = [f"CHR_{cid:06d}_00_l.png", f"CHR_{cid:06d}_00_s.png"]
        names.extend(f"CSK_{cid * 1000 + index:09d}.png" for index in range(1, 5))
        for name in names:
            Image.new("RGBA", (16, 16), (30, cid, 90, 255)).save(images / name)
        return CardAssets(
            art=images / names[0], avatar=images / names[1], asset_version=self.info.asset_version,
            icons={cid * 1000 + index: images / f"CSK_{cid * 1000 + index:09d}.png" for index in range(1, 5)},
        )

    def seed_catalog(self):
        release.write_json(self.cache / self.info.asset_version / "catalog.json", {"m_InternalIds": []})

    def run_pipeline(self, **kwargs):
        defaults = dict(output=self.output, character_ids=[7], cache_dir=self.cache,
                        now=self.now, services=self.services())
        defaults.update(kwargs)
        return release.run_release(**defaults)

    def test_single_discovery_and_shared_catalog_for_released_and_placeholder(self):
        result = self.run_pipeline(preview_ids=[9])
        self.assertEqual(self.calls["discover"], 1)
        self.assertEqual(self.calls["catalog"], 1)
        self.assertEqual(self.calls["fetch"], [7, 9])
        self.assertEqual(self.calls["downloads"], [])
        self.assertEqual(result["preview_character_ids"], [9])
        latest = release.read_json(self.output / "skills/latest.json")
        self.assertEqual([row["id"] for row in latest["latest_characters"]], [8])
        self.assertFalse((self.output / "skills/characters/000009.json").exists())
        self.assertFalse((self.output / "preview/skills/latest.json").exists())

    def test_master_cache_repairs_only_missing_and_wrong_hash_books(self):
        (self.raw / "ActiveSkillMB").unlink()
        path = self.raw / "PassiveSkillMB"
        content = bytearray(path.read_bytes())
        content[-1] ^= 1
        path.write_bytes(content)
        result = release.cache_master_books(self.info, self.raw, ("zh-CN", "ja-JP"),
                                             downloader=self.services().download)
        self.assertEqual(set(result["downloaded_books"]), {"ActiveSkillMB", "PassiveSkillMB"})
        self.assertEqual(set(self.calls["downloads"]), {"ActiveSkillMB", "PassiveSkillMB"})
        self.assertEqual((self.raw / "PassiveSkillMB").read_bytes(), self.books["PassiveSkillMB"])

    def test_offline_corrupt_book_fails_without_downloading(self):
        (self.raw / "ActiveSkillMB").write_bytes(b"corrupt")
        with self.assertRaisesRegex(RuntimeError, "Offline MasterBook"):
            release.cache_master_books(self.info, self.raw, ("zh-CN", "ja-JP"), offline=True,
                                         downloader=self.services().download)
        self.assertEqual(self.calls["downloads"], [])

    def test_shared_catalog_reuses_another_character_cache_without_network(self):
        legacy = self.root / "old-cache"
        old = legacy / self.info.asset_version / "000009" / "catalog.json"
        release.write_json(old, {"m_InternalIds": ["fixture"]})
        catalog = release.shared_asset_catalog(self.info, self.cache, offline=True,
                                               legacy_cache=legacy, downloader=self.services().catalog_download)
        self.assertEqual(catalog.read_bytes(), old.read_bytes())
        self.assertEqual(self.calls["catalog"], 0)
        release.shared_asset_catalog(self.info, self.cache, offline=True, legacy_cache=legacy)

    def test_preview_export_does_not_mutate_formal_latest_and_retains_placeholder_date(self):
        build_master_skill_repository(self.raw, self.output, release.REPOSITORY,
                                      MasterSource("5.0.0", self.info.asset_version, "1000"),
                                      ("zh-CN", "ja-JP"), self.now)
        latest = (self.output / "skills/latest.json").read_bytes()
        paths = release.export_previews(self.raw, self.output, self.info, [9], ("zh-CN", "ja-JP"), self.now)
        payload = release.read_json(paths[0])
        self.assertEqual(payload["release_status"], "unreleased-placeholder")
        self.assertIn("占位", payload["release_notice"])
        self.assertEqual(payload["character"]["start_time_jst"], "2100-01-01 15:00:00")
        self.assertEqual(payload["character"]["speed"], 3009)
        self.assertEqual((self.output / "skills/latest.json").read_bytes(), latest)
        with self.assertRaisesRegex(ValueError, "already released"):
            release.export_previews(self.raw, self.output, self.info, [7], ("zh-CN", "ja-JP"), self.now)

    def test_verified_same_source_png_is_reused_completely_offline(self):
        first = self.run_pipeline(preview_ids=[9])
        # Explicit context must not call discovery or any download path.
        second = self.run_pipeline(preview_ids=[9], source_context=self.context, offline=True)
        self.assertEqual(len(second["skipped"]), 2)
        self.assertEqual(second["rendered"], [])
        self.assertEqual(self.calls["discover"], 1)
        self.assertEqual(self.calls["render"], [7, 9])
        state = release.read_json(Path(first["state_file"]))
        preview = state["cards"]["preview:9"]
        self.assertEqual(preview["initial_speed"], 3009)
        self.assertIn("banner", preview["missing_assets"])
        self.assertIn("exclusive_weapon_data", preview["missing_assets"])
        self.assertIn("full_art", preview["official_assets"])

    def test_interruption_resumes_after_last_verified_card(self):
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.run_pipeline(character_ids=[7, 8], services=self.services(fail_character=8))
        result = self.run_pipeline(character_ids=[7, 8], source_context=self.context, offline=True)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(len(result["rendered"]), 1)
        self.assertEqual(self.calls["render"], [7, 8, 8])
        state = release.read_json(Path(result["state_file"]))
        self.assertEqual(state["stage"], "complete")
        self.assertNotIn("error", state)

    def test_corrupt_output_png_cannot_be_skipped(self):
        result = self.run_pipeline()
        target = Path(result["rendered"][0])
        target.write_bytes(b"not a PNG")
        repeated = self.run_pipeline(source_context=self.context, offline=True)
        self.assertEqual(len(repeated["rendered"]), 1)
        self.assertEqual(self.calls["render"], [7, 7])

    def test_missing_released_art_or_skill_icon_fails_before_render(self):
        self.seed_catalog()
        assets = self.cache_images(7)
        assets.icons[7004].unlink()
        with self.assertRaisesRegex(RuntimeError, "missing official images"):
            self.run_pipeline(source_context=self.context, offline=True)
        self.assertEqual(self.calls["render"], [])
        self.assertEqual(self.calls["discover"], 0)
        self.assertEqual(self.calls["catalog"], 0)
        self.assertEqual(self.calls["fetch"], [])

    def test_source_context_locates_assets_from_another_worktree(self):
        self.seed_catalog()
        self.cache_images(7)
        origin_reports = self.root / "original-worktree" / "reports"
        legacy = origin_reports / "skill-card-cache" / self.info.asset_version
        legacy.mkdir(parents=True)
        shutil.move(str(self.cache / self.info.asset_version / "000007"), str(legacy / "000007"))
        context = origin_reports / "current-release-source.json"
        release.write_json(context, {**release.asdict(self.info), "raw_master_dir": str(self.raw)})
        result = self.run_pipeline(source_context=context, offline=True)
        self.assertEqual(len(result["rendered"]), 1)
        repeated = self.run_pipeline(source_context=self.context, offline=True,
                                     asset_cache_dir=legacy.parent)
        self.assertEqual(len(repeated["skipped"]), 1)
        self.assertEqual(self.calls["fetch"], [])
        self.assertEqual(self.calls["discover"], 0)


if __name__ == "__main__":
    unittest.main()
