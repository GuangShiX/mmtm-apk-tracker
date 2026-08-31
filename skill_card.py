"""Render reusable character skill cards from exported Master JSON and official art."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import UnityPy
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from asset_cdn import (
    download_catalog,
    download_target_bundles,
    get_official_asset_info,
    load_catalog,
    resolve_catalog_key_bundles,
)


ROOT = Path(__file__).parent
DEFAULT_SKILLS_DIR = ROOT / "fallback_dist" / "skills"
DEFAULT_OUTPUT_DIR = ROOT / "skill_cards"
CACHE_MARKER = ".generated-by-mmtm-apk-tracker"
WIDTH = 3840
HEIGHT = 2160
COMPACT_WIDTH = 2160
COMPACT_HEIGHT = 3840
COMPACT_ICON_SIZE = 160
COMPACT_AVATAR_SIZE = 270
CARD_MANIFEST_SCHEMA_VERSION = 1
FULL_TEMPLATE_VERSION = "landscape-full-v1"
COMPACT_TEMPLATE_VERSION = "portrait-compact-v7"
COMPACT_WATERMARK = "Made By 光时"
MARGIN = 48
GAP = 30
LEFT_WIDTH = 1380
ELEMENT_NAMES = {0: "无", 1: "蓝", 2: "红", 3: "翠", 4: "黄", 5: "天", 6: "冥"}
JOB_NAMES = {0: "未知", 1: "战士", 2: "狙击手", 4: "魔法师"}
RARITY_LABELS = {128: "Ex1", 256: "Ex2", 512: "Ex3"}
PARAMETER_NAMES_ZH_CN = {
    "Muscle": "力量",
    "Energy": "战技",
    "Intelligence": "魔力",
    "Health": "耐力",
    "Hp": "生命值",
    "AttackPower": "攻击力",
    "PhysicalDamageRelax": "物理防御力",
    "MagicDamageRelax": "魔法防御力",
    "Hit": "命中",
    "Avoidance": "闪避",
    "Critical": "暴击",
    "CriticalResist": "暴击抗性",
    "CriticalDamageEnhance": "暴击伤害强化",
    "PhysicalCriticalDamageRelax": "物理暴击伤害降低",
    "MagicCriticalDamageRelax": "魔法暴击伤害降低",
    "DefensePenetration": "防御穿透",
    "Defense": "防御力",
    "DamageEnhance": "物魔防御穿透",
    "DebuffHit": "弱化效果命中",
    "DebuffResist": "弱化效果抗性",
    "DamageReflect": "伤害反弹",
    "HpDrain": "吸血",
    "Speed": "速度",
}


@dataclass(frozen=True)
class FontSet:
    sans: Path
    medium: Path
    serif: Path
    sans_index: int = 0
    medium_index: int = 0
    serif_index: int = 0


@dataclass(frozen=True)
class CardAssets:
    art: Path | None
    icons: dict[int, Path]
    asset_version: str
    weapon: Path | None = None
    avatar: Path | None = None
    element: Path | None = None
    arcana_characters: dict[int, Path] = field(default_factory=dict)


def _font_candidates() -> FontSet:
    windows = Path(r"C:\Windows\Fonts")
    candidates = (
        FontSet(
            windows / "Noto Sans SC.ttf",
            windows / "NotoSansSC-Medium.otf",
            windows / "NotoSerifSC-VF.ttf",
        ),
        FontSet(
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
        ),
    )
    for fonts in candidates:
        if all(path.is_file() for path in (fonts.sans, fonts.medium, fonts.serif)):
            if all(
                path.suffix.casefold() == ".ttc"
                for path in (fonts.sans, fonts.medium, fonts.serif)
            ):
                return FontSet(
                    fonts.sans,
                    fonts.medium,
                    fonts.serif,
                    sans_index=_font_collection_index(fonts.sans),
                    medium_index=_font_collection_index(fonts.medium),
                    serif_index=_font_collection_index(fonts.serif),
                )
            return fonts
    raise RuntimeError(
        "找不到中文字体；Windows 需要 Noto Sans/Serif SC，Linux 需要 fonts-noto-cjk"
    )


def _font_collection_index(path: Path, family_marker: str = "CJK SC") -> int:
    """Resolve the Simplified Chinese face instead of TTC's default JP face."""
    for index in range(32):
        try:
            face = ImageFont.truetype(str(path), 16, index=index)
        except OSError:
            break
        family, _style = face.getname()
        if family_marker.casefold() in family.casefold():
            return index
    raise RuntimeError(f"字体集合不包含 {family_marker} 字面: {path}")


def _font(fonts: FontSet, size: int, *, serif: bool = False, medium: bool = False):
    if serif:
        path, index = fonts.serif, fonts.serif_index
    elif medium:
        path, index = fonts.medium, fonts.medium_index
    else:
        path, index = fonts.sans, fonts.sans_index
    return ImageFont.truetype(str(path), size, index=index)


def _draw_compact_watermark(
    draw: ImageDraw.ImageDraw,
    fonts: FontSet,
    *,
    right: int,
    top: int,
) -> tuple[int, int]:
    face = _font(fonts, 34, medium=True)
    x = round(right - draw.textlength(COMPACT_WATERMARK, font=face))
    draw.text(
        (x, top),
        COMPACT_WATERMARK,
        font=face,
        fill=(184, 177, 164, 255),
    )
    return x, top


def _localized(mapping: Any, language: str) -> str | None:
    if not isinstance(mapping, dict):
        return None
    value = mapping.get(language)
    if isinstance(value, str) and value.strip():
        return value.strip()
    for value in mapping.values():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _compact_identity_copy(
    character: dict[str, Any], language: str
) -> tuple[str, str, str]:
    title = _localized(character.get("names"), language) or str(character.get("id"))
    subtitle = _localized(character.get("subtitles"), language) or ""
    if subtitle:
        subtitle = f"【{subtitle}】"
    element = ELEMENT_NAMES.get(
        character.get("element_type"), str(character.get("element_type", "?"))
    )
    return title, subtitle, element


def _load_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取角色技能 JSON: {path}") from exc
    character = payload.get("character") if isinstance(payload, dict) else None
    skills = [*(payload.get("active_skills") or []), *(payload.get("passive_skills") or [])]
    if not isinstance(character, dict) or not isinstance(character.get("id"), int):
        raise RuntimeError("角色技能 JSON 缺少 character.id")
    if not skills or not all(isinstance(skill.get("id"), int) for skill in skills):
        raise RuntimeError("角色技能 JSON 没有有效技能")
    return payload


def _display_skill_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Exclude unnamed exclusive-effect rows that are not real skill slots."""
    skills = [
        *(payload.get("active_skills") or []),
        *(payload.get("passive_skills") or []),
    ]
    return [
        skill
        for skill in skills
        if skill.get("name_key") != "*"
        and (skill.get("master_record") or {}).get("NameKey") != "*"
    ]


def _compact_skill_records(
    payload: dict[str, Any], language: str
) -> list[dict[str, Any]]:
    character = payload.get("character") or {}
    skills = _display_skill_records(payload)
    localized = []
    for skill in skills:
        names = skill.get("names")
        name = names.get(language) if isinstance(names, dict) else None
        levels = skill.get("levels") or []
        descriptions_complete = bool(levels) and all(
            isinstance(level.get("descriptions"), dict)
            and isinstance(level["descriptions"].get(language), str)
            and bool(level["descriptions"][language].strip())
            for level in levels
        )
        if isinstance(name, str) and name.strip() and descriptions_complete:
            localized.append(skill)
    if len(localized) != 4:
        raise RuntimeError(
            f"角色 {character.get('id', '?')} 的官方 {language} 技能文本尚未完整，"
            "暂不生成省流图"
        )
    return localized


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _expected_card_size(mode: str) -> tuple[int, int]:
    if mode == "full":
        return WIDTH, HEIGHT
    if mode == "compact":
        return COMPACT_WIDTH, COMPACT_HEIGHT
    raise ValueError("mode 必须是 full 或 compact")


def _template_version(mode: str) -> str:
    if mode == "full":
        return FULL_TEMPLATE_VERSION
    if mode == "compact":
        return COMPACT_TEMPLATE_VERSION
    raise ValueError("mode 必须是 full 或 compact")


def _save_card(image: Image.Image, output: Path, mode: str) -> Path:
    expected_size = _expected_card_size(mode)
    if image.size != expected_size:
        raise RuntimeError(
            f"{mode} 模板尺寸错误: expected={expected_size}, actual={image.size}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    try:
        image.convert("RGB").save(
            temporary,
            format="PNG",
            compress_level=6,
        )
        with Image.open(temporary) as rendered:
            if rendered.format != "PNG" or rendered.size != expected_size:
                raise RuntimeError(f"生成的技能图校验失败: {output}")
            rendered.verify()
        temporary.replace(output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return output


def _card_manifest_key(character_id: int, language: str, mode: str) -> str:
    return f"{character_id:06d}:{language}:{mode}"


def _update_card_manifest(
    manifest_path: Path,
    entries: list[dict[str, Any]],
) -> Path:
    current: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            current = {}
    cards = current.get("cards")
    if not isinstance(cards, dict):
        cards = {}
    for entry in entries:
        cards[entry["key"]] = {key: value for key, value in entry.items() if key != "key"}
    manifest = {
        "schema_version": CARD_MANIFEST_SCHEMA_VERSION,
        "game": "MementoMori",
        "cards": dict(sorted(cards.items())),
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_name(f".{manifest_path.name}.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)
    return manifest_path


def verify_latest_skill_cards(
    skills_dir: Path,
    output_dir: Path,
    *,
    language: str = "zh-CN",
    mode: str = "compact",
) -> list[Path]:
    modes = ("full", "compact") if mode == "both" else (mode,)
    sources = _resolve_character_jsons(skills_dir, None, True, False)
    manifest_path = output_dir.parent / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        cards = manifest["cards"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"技能图清单不存在或无效: {manifest_path}") from exc
    if manifest.get("schema_version") != CARD_MANIFEST_SCHEMA_VERSION or not isinstance(
        cards, dict
    ):
        raise RuntimeError(f"技能图清单版本无效: {manifest_path}")

    verified: list[Path] = []
    for source in sources:
        payload = _load_payload(source)
        character_id = payload["character"]["id"]
        source_sha256 = _sha256(source)
        for card_mode in modes:
            suffix = "" if card_mode == "full" else "-compact"
            target = output_dir / f"character-{character_id:06d}{suffix}-{language}.png"
            key = _card_manifest_key(character_id, language, card_mode)
            entry = cards.get(key)
            if not isinstance(entry, dict):
                raise RuntimeError(f"技能图清单缺少 {key}")
            expected = {
                "character_id": character_id,
                "language": language,
                "mode": card_mode,
                "source_sha256": source_sha256,
                "template_version": _template_version(card_mode),
                "width": _expected_card_size(card_mode)[0],
                "height": _expected_card_size(card_mode)[1],
            }
            mismatched = [name for name, value in expected.items() if entry.get(name) != value]
            if mismatched:
                raise RuntimeError(f"技能图 {key} 已过期: {', '.join(mismatched)}")
            if not target.is_file() or entry.get("path") != target.relative_to(
                output_dir.parent
            ).as_posix():
                raise RuntimeError(f"技能图文件缺失: {target}")
            if entry.get("sha256") != _sha256(target):
                raise RuntimeError(f"技能图哈希不一致: {target}")
            with Image.open(target) as rendered:
                if rendered.format != "PNG" or rendered.size != _expected_card_size(card_mode):
                    raise RuntimeError(f"技能图格式或尺寸错误: {target}")
                rendered.verify()
            verified.append(target)
    return verified


def _asset_keys(
    character_id: int,
    skill_ids: list[int],
    art_size: str,
    weapon_icon_id: int | None = None,
    include_avatar: bool = False,
    arcana_character_ids: list[int] | None = None,
) -> dict[str, int | str | None]:
    keys: dict[str, int | str | None] = {
        f"CharacterIcon/CHR_{character_id:06d}/CHR_{character_id:06d}_00_{art_size}": None,
        **{f"Icon/Skill/CSK_{skill_id:09d}": skill_id for skill_id in skill_ids},
    }
    if isinstance(weapon_icon_id, int) and weapon_icon_id > 0:
        keys[f"Icon/Equipment/EQP_{weapon_icon_id:06d}"] = "weapon"
    if include_avatar:
        keys[f"CharacterIcon/CHR_{character_id:06d}/CHR_{character_id:06d}_00_s"] = (
            "avatar"
        )
    for arcana_character_id in arcana_character_ids or []:
        if arcana_character_id <= 0 or arcana_character_id == character_id:
            continue
        keys[
            f"CharacterIcon/CHR_{arcana_character_id:06d}/"
            f"CHR_{arcana_character_id:06d}_00_s"
        ] = f"arcana:{arcana_character_id}"
    return keys


def _safe_cache_dir(cache_root: Path, asset_version: str, character_id: int) -> Path:
    if not re.fullmatch(r"[0-9A-Za-z_.-]+", asset_version):
        raise RuntimeError(f"非法 assetVersion: {asset_version}")
    target = cache_root.resolve() / asset_version / f"{character_id:06d}"
    target.mkdir(parents=True, exist_ok=True)
    marker = target / CACHE_MARKER
    if not marker.exists():
        marker.write_text("Generated cache. Safe to rebuild.\n", encoding="utf-8")
    return target


def _export_key_image(bundle_paths: list[Path], expected_name: str, output: Path) -> Path:
    candidates: list[tuple[int, str, Image.Image]] = []
    for bundle in bundle_paths:
        environment = UnityPy.load(str(bundle))
        for obj in environment.objects:
            if obj.type.name not in ("Sprite", "Texture2D"):
                continue
            try:
                data = obj.read()
                name = str(getattr(data, "m_Name", ""))
                if name != expected_name:
                    continue
                score = 1 if obj.type.name == "Sprite" else 0
                candidates.append((score, obj.type.name, data.image.copy()))
            except (AttributeError, EOFError, OSError, ValueError):
                continue
    if not candidates:
        raise RuntimeError(f"Bundle 中找不到图片对象: {expected_name}")
    candidates.sort(key=lambda item: (item[0], item[2].width * item[2].height), reverse=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    candidates[0][2].save(output)
    return output


def fetch_official_card_assets(
    character_id: int,
    skill_ids: list[int],
    cache_root: Path,
    *,
    art_size: str = "l",
    catalog_path: Path | None = None,
    weapon_icon_id: int | None = None,
    include_avatar: bool = False,
    element_icon: Path | None = None,
    arcana_character_ids: list[int] | None = None,
    allow_missing: bool = False,
) -> CardAssets:
    if art_size not in {"l", "m", "w"}:
        raise ValueError("art_size 必须是 l、m 或 w")
    info = get_official_asset_info()
    cache_dir = _safe_cache_dir(cache_root, info.asset_version, character_id)
    catalog_target = catalog_path or cache_dir / "catalog.json"
    if catalog_path is None and not catalog_target.is_file():
        download_catalog(info, catalog_target)
    catalog = load_catalog(catalog_target)
    requested = _asset_keys(
        character_id,
        skill_ids,
        art_size,
        weapon_icon_id,
        include_avatar,
        arcana_character_ids,
    )
    resolved: dict[str, tuple[str, ...]] = {}
    for key in requested:
        try:
            resolved.update(resolve_catalog_key_bundles(catalog, [key]))
        except RuntimeError as exc:
            if not allow_missing:
                raise
            print(f"  可选图片暂缺，技能文字继续生成: {key} ({exc})")
    bundle_names = sorted({name for names in resolved.values() for name in names})
    bundles_dir = cache_dir / "bundles"
    missing = [name for name in bundle_names if not (bundles_dir / name).is_file()]
    if missing and not allow_missing:
        download_target_bundles(info, missing, bundles_dir)
    elif missing:
        for name in missing:
            try:
                download_target_bundles(info, [name], bundles_dir)
            except Exception as exc:
                print(f"  可选图片 Bundle 暂不可用，技能文字继续生成: {name} ({exc})")

    images_dir = cache_dir / "images"
    art_key = next(key for key, asset_id in requested.items() if asset_id is None)
    art_name = art_key.rsplit("/", 1)[-1]
    art = images_dir / f"{art_name}.png"
    if art_key not in resolved:
        art = None
    elif not art.is_file():
        try:
            _export_key_image(
                [bundles_dir / name for name in resolved[art_key]], art_name, art
            )
        except (OSError, RuntimeError) as exc:
            if not allow_missing:
                raise
            print(f"  角色立绘暂缺，继续生成文字卡: {art_name} ({exc})")
            art = None
    icons: dict[int, Path] = {}
    weapon = None
    avatar = None
    arcana_characters: dict[int, Path] = {}
    for key, asset_id in requested.items():
        if asset_id is None:
            continue
        if key not in resolved:
            continue
        expected_name = key.rsplit("/", 1)[-1]
        icon = images_dir / f"{expected_name}.png"
        if not icon.is_file():
            try:
                _export_key_image(
                    [bundles_dir / name for name in resolved[key]], expected_name, icon
                )
            except (OSError, RuntimeError) as exc:
                if not allow_missing:
                    raise
                print(f"  可选图片暂缺，技能文字继续生成: {expected_name} ({exc})")
                continue
        if asset_id == "weapon":
            weapon = icon
        elif asset_id == "avatar":
            avatar = icon
        elif isinstance(asset_id, str) and asset_id.startswith("arcana:"):
            arcana_characters[int(asset_id.split(":", 1)[1])] = icon
        else:
            icons[int(asset_id)] = icon
    if avatar is not None and character_id in (arcana_character_ids or []):
        arcana_characters[character_id] = avatar
    return CardAssets(
        art=art,
        icons=icons,
        asset_version=info.asset_version,
        weapon=weapon,
        avatar=avatar,
        element=element_icon,
        arcana_characters=arcana_characters,
    )


def _fit_crop(image: Image.Image, size: tuple[int, int], focus=(0.5, 0.35)) -> Image.Image:
    ratio = max(size[0] / image.width, size[1] / image.height)
    resized = image.resize(
        (round(image.width * ratio), round(image.height * ratio)), Image.Resampling.LANCZOS
    )
    left = max(0, min(resized.width - size[0], round((resized.width - size[0]) * focus[0])))
    top = max(0, min(resized.height - size[1], round((resized.height - size[1]) * focus[1])))
    return resized.crop((left, top, left + size[0], top + size[1]))


def _wrap(draw: ImageDraw.ImageDraw, text: str, face: ImageFont.FreeTypeFont, width: int):
    lines: list[str] = []
    line = ""
    tokens = re.findall(
        r"「[^」]+」|（[^（）]{1,16}）|×\d[\d,]*(?:\.\d+)?%?|"
        r"[+\-]?\d[\d,]*(?:\.\d+)?(?:×角色等级|%|次|名|层|回合|连击)?|"
        r"[A-Za-z]+|.",
        text,
        re.DOTALL,
    )
    closing = "，。；：！？）》」』】、%"
    for index, token in enumerate(tokens):
        proposed = line + token
        if line and draw.textlength(proposed, font=face) > width:
            if token in closing:
                line += token
            else:
                lines.append(line)
                line = token
        else:
            suffix = ""
            for following in tokens[index + 1 :]:
                if following not in closing:
                    break
                suffix += following
            if (
                line
                and token not in closing
                and suffix
                and draw.textlength(proposed + suffix, font=face) > width
            ):
                lines.append(line)
                line = token
            else:
                line = proposed
    if line:
        lines.append(line)
    return lines


def _line_height(draw: ImageDraw.ImageDraw, face: ImageFont.FreeTypeFont, gap: int = 7):
    box = draw.textbbox((0, 0), "国Ag", font=face)
    return box[3] - box[1] + gap


def _level_label(level: dict[str, Any]) -> str:
    rarity = level.get("equipment_rarity_flags")
    if rarity in RARITY_LABELS:
        return RARITY_LABELS[rarity]
    order = level.get("order")
    return f"Lv{order}" if isinstance(order, int) else "Lv"


def _skill_view(skill: dict[str, Any], language: str) -> dict[str, Any]:
    levels = []
    for level in skill.get("levels") or []:
        description = _localized(level.get("descriptions"), language)
        if description is None:
            source = level.get("source_memo_ja")
            description = source.get("text") if isinstance(source, dict) else "本地化文本缺失"
        levels.append(
            {
                "label": _level_label(level),
                "unlock": level.get("character_level"),
                "text": description,
            }
        )
    tag = "主动技能" if skill.get("kind") == "active" else "被动技能"
    cooldown = skill.get("max_cooldown")
    if tag == "主动技能" and isinstance(cooldown, int):
        tag += f" · CD {cooldown}"
    return {
        "id": skill["id"],
        "name": _localized(skill.get("names"), language) or f"技能 {skill['id']}",
        "tag": tag,
        "levels": levels,
        "active": skill.get("kind") == "active",
    }


_MECHANICAL_WORDS = (
    "攻击",
    "伤害",
    "恢复",
    "生命值",
    "回合",
    "暴击",
    "防御",
    "增益",
    "弱化",
    "状态",
    "效果",
    "解除",
    "发动",
    "附带",
    "获得",
    "普攻",
    "敌人",
    "友军",
)


def _sentences(text: str) -> list[str]:
    return [item.strip() for item in re.findall(r"[^。！？]+[。！？]?", text) if item.strip()]


def _clean_compact_text(text: str) -> str:
    text = re.sub(r"“[^”]*”", "", text).strip()
    cleaned: list[str] = []
    for sentence in _sentences(text):
        if "——" in sentence:
            sentence = sentence.rsplit("——", 1)[-1].strip()
        core = sentence.rstrip("。！？")
        if not any(word in core for word in _MECHANICAL_WORDS):
            continue
        cleaned.append(core + "。")
    return "".join(cleaned)


def _replace_sentence(
    text: str,
    predicate,
    pattern: str,
    replacement: str,
) -> tuple[str, bool]:
    parts = _sentences(text)
    for index, sentence in enumerate(parts):
        if not predicate(sentence):
            continue
        updated, count = re.subn(pattern, replacement, sentence, count=1)
        if count:
            parts[index] = updated
            return "".join(parts), True
    return text, False


def _append_sentence(text: str, sentence: str) -> str:
    sentence = sentence.strip().rstrip("。！？") + "。"
    if sentence in text:
        return text
    return text + sentence


def _merge_compact_upgrade(text: str, upgrade: str) -> str:
    upgrade = _clean_compact_text(upgrade)
    upgrade = re.sub(r"^强化[^，。]+[，。]", "", upgrade)
    for sentence in _sentences(upgrade):
        core = sentence.rstrip("。！？")
        applied = False

        match = re.search(
            r"防御力、物理防御力与魔法防御力增幅分别提升为"
            r"[^，。]+?防御力×([\d.]+)%、"
            r"[^，。]+?物理防御力×([\d.]+)%及"
            r"[^，。]+?魔法防御力×([\d.]+)%",
            core,
        )
        if match:
            defense, physical, magic = match.groups()
            text, physical_count = re.subn(
                r"物理防御力×[\d.]+%", f"物理防御力×{physical}%", text
            )
            text, magic_count = re.subn(
                r"魔法防御力×[\d.]+%", f"魔法防御力×{magic}%", text
            )
            text, defense_count = re.subn(
                r"(?<!物理)(?<!魔法)防御力×[\d.]+%",
                f"防御力×{defense}%",
                text,
            )
            applied = bool(physical_count or magic_count or defense_count)

        if not applied:
            match = re.search(
                r"攻击力增幅提升为[^，。]+?攻击力×([\d.]+)%", core
            )
            if match:
                value = match.group(1)
                text, applied = _replace_sentence(
                    text,
                    lambda item: "增加攻击力" in item and "攻击力×" in item,
                    r"(攻击力×)[\d.]+%",
                    rf"\g<1>{value}%",
                )

        if not applied:
            match = re.search(r"伤害阻绝量提升为([\d.]+)%", core)
            if match:
                value = match.group(1)
                text, applied = _replace_sentence(
                    text,
                    lambda item: "阻绝" in item and "伤害" in item,
                    r"阻绝[\d.]+%伤害",
                    f"阻绝{value}%伤害",
                )

        match = re.search(r"生命值恢复量提升为[^。]*?攻击力×([\d.]+)%", core)
        if not applied and match:
            value = match.group(1)
            text, applied = _replace_sentence(
                text,
                lambda item: "恢复" in item and "生命值" in item and "攻击力×" in item,
                r"(攻击力×)[\d.]+%",
                rf"\g<1>{value}%",
            )

        if not applied:
            match = re.search(r"攻击次数(?:增加|提升)为(\d+)次", core)
            if match:
                value = match.group(1)
                text, applied = _replace_sentence(
                    text,
                    lambda item: "攻击" in item and ("进行" in item or "攻击次数" in item),
                    r"进行\d+次攻击",
                    f"进行{value}次攻击",
                )

        if not applied:
            match = re.search(
                r"(?:造成的)?(?:物理|魔法)?伤害提升为攻击力×([\d.]+)%", core
            )
            if match:
                value = match.group(1)
                late_turn = bool(re.search(r"第\d+回合起", core))
                text, applied = _replace_sentence(
                    text,
                    lambda item: (
                        "伤害" in item
                        and "攻击力×" in item
                        and bool(re.search(r"第\d+回合起", item)) == late_turn
                    ),
                    r"(攻击力×)[\d.]+%",
                    rf"\g<1>{value}%",
                )

        if not applied:
            match = re.search(r"(.+?)增幅提升为([\d.]+)%", core)
            if match:
                subjects, value = match.groups()
                compact_subjects = subjects.replace("与", "(?:与|、)")
                text, applied = _replace_sentence(
                    text,
                    lambda item: all(part in item for part in subjects.split("与")),
                    rf"增加[\d.]+%({compact_subjects})",
                    rf"增加{value}%\1",
                )

        if not applied:
            match = re.search(r"承受伤害降幅提升为([\d.]+)%", core)
            if match:
                value = match.group(1)
                text, applied = _replace_sentence(
                    text,
                    lambda item: "承受伤害" in item and ("减少" in item or "降低" in item),
                    r"((?:额外)?减少)[\d.]+%(自身承受伤害)",
                    rf"\g<1>{value}%\2",
                )

        if "最大生命值增幅提升为" in core:
            match = re.search(r"最大生命值增幅提升为([\d.]+)%", core)
            if match:
                value = match.group(1)
                text, hp_applied = _replace_sentence(
                    text,
                    lambda item: "最大生命值" in item and "增加" in item,
                    r"(增加)[\d.]+%(最大生命值)",
                    rf"\g<1>{value}%\2",
                )
                applied = applied or hp_applied

        if not applied:
            text = _append_sentence(text, core)
    return text


def _compact_skill_text(skill: dict[str, Any], language: str) -> str:
    levels = skill.get("levels") or []
    descriptions = [
        description
        for level in levels
        if (description := _localized(level.get("descriptions"), language))
    ]
    if not descriptions:
        return "技能文本缺失。"
    text = _clean_compact_text(descriptions[0])
    for upgrade in descriptions[1:]:
        text = _merge_compact_upgrade(text, upgrade)
    parts = _sentences(text)
    battle_start = [
        index for index, sentence in enumerate(parts) if sentence.startswith("战斗开始时，")
    ]
    if len(battle_start) >= 2:
        first, second = battle_start[0], battle_start[1]
        initial = parts[first].rstrip("。")
        addition = parts[second].removeprefix("战斗开始时，").rstrip("。")
        for subject in ("佛罗伦斯", "自身", "自己"):
            if addition.startswith(subject) and subject in initial:
                addition = addition.removeprefix(subject)
                break
        duration = re.search(r"，效果持续\d+回合$", initial)
        if duration:
            initial = initial[: duration.start()] + f"，并{addition}" + duration.group(0)
            parts[first] = initial + "。"
            del parts[second]
            text = "".join(parts)
    return _minify_compact_text(text)


def _minify_compact_text(text: str) -> str:
    text = text.replace("无法被解除", "无法解除")
    text = re.sub(
        r"当(.+?)发动主动技能触发恢复生命值的效果时",
        r"\1的主动技能恢复生命值时",
        text,
    )
    text = re.sub(
        r"获得1层增益效果，增加([\d.]+)%([^（。]+)",
        r"获得1层\2+\1%的增益",
        text,
    )
    text = re.sub(
        r"（最多(\d+)层）（无法解除）",
        r"（最多\1层，无法解除）",
        text,
    )
    text = re.sub(
        r"第(\d+)回合起，在每回合开始时，如果[^。]*?附带(\d+)层此技能附加的增益效果，获得",
        r"第\1回合起，回合开始时若达到\2层，获得",
        text,
    )
    text = re.sub(
        r"战斗开始时，使自己额外增加([\d.]+)%([^与，。]+)与\1%([^，。]+)，效果持续(\d+)回合",
        r"战斗开始时，\2与\3+\1%，持续\4回合",
        text,
    )
    text = re.sub(
        r"战斗开始时，[^，。]+强化自己的普通攻击，并额外减少([\d.]+)%自身承受伤害，使自己增加([\d.]+)%最大生命值（无法解除），效果持续(\d+)回合",
        r"战斗开始时，强化普通攻击，承受伤害-\1%、最大生命值+\2%（无法解除），持续\3回合",
        text,
    )
    text = re.sub(
        r"普攻时随机对(\d+)名敌人造成([^。]+?)，再使生命值百分比最低的(\d+)名友军恢复([\d.]+)%最大生命值",
        r"普攻随机攻击\1名敌人，造成\2，并使生命值百分比最低的\3名友军恢复\4%最大生命值",
        text,
    )
    text = re.sub(
        r"当生命值恢复目标原本的生命值为([\d.]+)%以上，且身上附带控制效果时，额外解除目标身上的所有控制效果",
        r"若恢复前目标生命值≥\1%且附带控制效果，解除其所有控制效果",
        text,
    )
    text = re.sub(
        r"当(.+?)身上附带(「[^」]+」)状态时，可发动此技能",
        r"\1附带\2时可发动",
        text,
    )
    text = text.replace("使自己与速度高于自己的其他友军", "使自身及速度高于自身的友军")
    text = re.sub(
        r"(^|。)[^，。]{1,12}使自身及速度高于自身的友军",
        r"\1使自身及速度高于自身的友军",
        text,
    )
    text = re.sub(
        r"使自身及速度高于自身的友军增加防御力，增幅为[^，。]+防御力×([\d.]+)%，效果持续(\d+)回合",
        r"使自身和速度高于自身的友军增加相当于自身防御力\1%的防御力，持续\2回合",
        text,
    )
    text = re.sub(
        r"发动攻击前，[^，。]+使自身及速度高于自身的友军额外增加物理防御力与魔法防御力，"
        r"增幅分别为[^，。]+物理防御力×([\d.]+)%及[^，。]+魔法防御力×([\d.]+)%，效果持续(\d+)回合",
        r"攻击前，使上述友军的物防与魔防分别增加相当于自身对应防御力\1%、\2%的数值，持续\3回合",
        text,
    )

    def merge_defense_buffs(match: re.Match[str]) -> str:
        defense, duration, restriction, middle, physical, magic = match.groups()
        if defense == physical == magic:
            increase = (
                "防御力、物防、魔防 + 自身防御力、物防、魔防"
                f"×{defense}%"
            )
        else:
            increase = (
                f"防御力 + 自身防御力×{defense}%、"
                f"物防 + 自身物防×{physical}%、"
                f"魔防 + 自身魔防×{magic}%"
            )
        return (
            f"攻击前，使自身及速度高于自身的友军{increase}，"
            f"持续{duration}回合{restriction or ''}。{middle}"
        )

    text = re.sub(
        r"使自身和速度高于自身的友军增加相当于自身防御力([\d.]+)%的防御力，"
        r"持续(\d+)回合(（无法解除）)?。"
        r"(.*?)"
        r"攻击前，使上述友军的物防与魔防分别增加相当于自身对应防御力"
        r"([\d.]+)%、([\d.]+)%的数值，持续\2回合(?:\3)?。",
        merge_defense_buffs,
        text,
    )
    text = re.sub(r"再随机对(\d+)名敌人造成", r"随后随机攻击\1名敌人，造成", text)
    text = text.replace("如果队伍中包含的角色属性为2种以下", "若队伍属性≤2种")
    text = re.sub(r"[^，。]{1,12}使全体友军增加([\d.]+)%速度", r"全体友军速度+\1%", text)
    text = re.sub(
        r"第2回合开始时，[^，。]+使自己与攻击力最高的(\d+)名其他友军额外增加攻击力，"
        r"增幅为[^，。]+攻击力×([\d.]+)%，效果持续(\d+)回合",
        r"第2回合开始，自身及攻击力最高的\1名友军攻击力+自身攻击力×\2%，持续\3回合",
        text,
    )
    text = re.sub(
        r"第1回合行动开始时，[^，。]+获得(「[^」]+」)状态，代其他友军承受([\d.]+)%攻击伤害，效果持续(\d+)回合",
        r"第1回合行动时，获得\1\3回合，代友军承受\2%攻击伤害",
        text,
    )
    text = re.sub(
        r"如果[^，。]+在身上附带此技能附加的(「[^」]+」)状态时受到攻击，阻绝([\d.]+)%伤害",
        r"附带\1时受到攻击，阻绝\2%伤害",
        text,
    )
    return text


def _compact_weapon_effect_rows(
    weapon: dict[str, Any], language: str
) -> list[tuple[str, str]]:
    effects = [
        effect
        for effect in weapon.get("skill_effects") or []
        if isinstance(effect, dict)
    ]

    def effect_for(rarity: int, fallback_index: int) -> dict[str, Any] | None:
        matched = next(
            (
                effect
                for effect in effects
                if effect.get("equipment_rarity_flags") == rarity
            ),
            None,
        )
        if matched is not None:
            return matched
        return effects[fallback_index] if len(effects) > fallback_index else None

    ur_effect = effect_for(256, 1)
    lr_effect = effect_for(512, 2)
    if ur_effect is None or lr_effect is None:
        return []

    ur_text = _localized(ur_effect.get("descriptions"), language) or "文本缺失"
    ur_text = re.sub(
        r"战斗开始时，[^，。]+获得",
        "战斗开始时，获得",
        ur_text,
    )
    ur_text = re.sub(
        r"当(?:她|[^，。]{1,12})受到最大生命值×([\d.]+)%以上的伤害时，"
        r"消耗1层屏障来抵消该伤害",
        r"受到的伤害达到最大生命值的\1%以上时，消耗1层并抵消该伤害",
        ur_text,
    ).replace("无法被解除", "无法解除")

    lr_text = _localized(lr_effect.get("descriptions"), language) or "文本缺失"
    lr_text = re.sub(r"^强化[^，。]+[，。]", "", lr_text)
    return [("UR专效果", ur_text), ("LR专效果", lr_text)]


def _skill_body_layout(
    draw: ImageDraw.ImageDraw,
    skill: dict[str, Any],
    fonts: FontSet,
    text_width: int,
    available_height: int,
) -> tuple[ImageFont.FreeTypeFont, int, list[list[str]]]:
    for size in range(34, 23, -1):
        body = _font(fonts, size)
        line_height = _line_height(draw, body, gap=max(7, size // 4))
        lines = [_wrap(draw, level["text"], body, text_width) for level in skill["levels"]]
        required = sum(max(52, len(item) * line_height) + 17 for item in lines)
        if required <= available_height:
            return body, line_height, lines
    raise RuntimeError(
        f"技能 {skill['id']} 的官方全文无法在 4K 单图中以可读字号完整排入"
    )


def _background(height: int, character_id: int, *, width: int = WIDTH) -> Image.Image:
    strip = Image.new("RGB", (1, height))
    strip_pixels = strip.load()
    top, middle, bottom = (247, 235, 211), (126, 75, 59), (17, 28, 43)
    rng = random.Random(character_id)
    for y in range(height):
        t = y / max(1, height - 1)
        if t < 0.45:
            u, a, b = t / 0.45, top, middle
        else:
            u, a, b = (t - 0.45) / 0.55, middle, bottom
        row_noise = rng.uniform(-2.5, 2.5)
        strip_pixels[0, y] = tuple(
            max(0, min(255, round(a[i] * (1 - u) + b[i] * u + row_noise)))
            for i in range(3)
        )
    image = strip.resize((width, height))
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((-560, -760, 1120, 1300), fill=(255, 229, 178, 38))
    glow = glow.filter(ImageFilter.GaussianBlur(170))
    wash = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(wash)
    for _ in range(max(20, height // 110)):
        x, y = rng.randint(-250, width + 150), rng.randint(-100, height)
        rx, ry = rng.randint(120, 430), rng.randint(60, 230)
        color = rng.choice(
            [(240, 153, 48, 16), (250, 224, 166, 14), (22, 47, 72, 20), (90, 39, 35, 18)]
        )
        draw.ellipse((x - rx, y - ry, x + rx, y + ry), fill=color)
    return Image.alpha_composite(
        Image.alpha_composite(image.convert("RGBA"), glow),
        wash.filter(ImageFilter.GaussianBlur(65)),
    )


def _compact_body_layout(
    draw: ImageDraw.ImageDraw,
    text: str,
    fonts: FontSet,
    width: int,
    height: int,
) -> tuple[ImageFont.FreeTypeFont, int, int, list[list[str]]]:
    paragraphs = _sentences(text)
    for size in range(72, 43, -1):
        body = _font(fonts, size)
        line_height = _line_height(draw, body, gap=max(10, size // 4))
        paragraph_gap = max(14, size // 3)
        lines = [_wrap(draw, paragraph, body, width) for paragraph in paragraphs]
        required = sum(len(item) * line_height for item in lines)
        required += paragraph_gap * max(0, len(lines) - 1)
        if required <= height:
            return body, line_height, paragraph_gap, lines
    raise RuntimeError("合并后的技能文本无法在 4K 省流模板中清晰排入")


def _render_compact_skill_card(
    payload: dict[str, Any],
    assets: CardAssets,
    output: Path,
    *,
    language: str,
) -> Path:
    character = payload["character"]
    fonts = _font_candidates()
    raw_skills = _compact_skill_records(payload, language)
    skills = [_skill_view(skill, language) for skill in raw_skills]
    for view, raw in zip(skills, raw_skills):
        view["compact_text"] = _compact_skill_text(raw, language)
    if len(skills) > 4:
        raise RuntimeError(
            f"角色 {character['id']} 有 {len(skills)} 个技能，超过省流模板的四技能上限"
        )

    weapon = payload.get("exclusive_weapon")
    if not isinstance(weapon, dict):
        raise RuntimeError(f"角色 {character['id']} 缺少专武 Master 数据")
    passive_effects = weapon.get("passive_effects") or []
    if not passive_effects:
        raise RuntimeError(f"角色 {character['id']} 的专武没有被动词条")
    skill_effects = weapon.get("skill_effects") or []
    if len(skill_effects) < 3:
        raise RuntimeError(f"角色 {character['id']} 的专武缺少 LR 专效果")
    weapon_effect_rows = _compact_weapon_effect_rows(weapon, language)
    if len(weapon_effect_rows) != 2:
        raise RuntimeError(f"角色 {character['id']} 的专武缺少 UR 或 LR 专效果")
    arcanas = [item for item in payload.get("arcanas") or [] if isinstance(item, dict)]
    arcana = arcanas[-1] if arcanas else None

    # Compact cards are portrait-first social images. The reading order is
    # identity -> four skills -> weapon -> arcana, with the actual game icons
    # used as the visual anchors instead of a large character illustration.
    canvas = _background(
        COMPACT_HEIGHT,
        character["id"],
        width=COMPACT_WIDTH,
    )
    draw = ImageDraw.Draw(canvas)
    margin = 60
    panel_left = margin
    panel_right = COMPACT_WIDTH - margin
    panel_width = panel_right - panel_left
    icon_size = COMPACT_ICON_SIZE
    avatar_size = COMPACT_AVATAR_SIZE

    def panel(top: int, bottom: int, accent: tuple[int, int, int]) -> None:
        draw.rounded_rectangle(
            (panel_left, top, panel_right, bottom),
            radius=34,
            fill=(13, 23, 36, 247),
            outline=(*accent, 220),
            width=3,
        )

    def paste_rounded(source: Path, x: int, y: int, size: int, radius: int) -> None:
        image = _fit_crop(
            Image.open(source).convert("RGBA"),
            (size, size),
            focus=(0.5, 0.5),
        )
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, size - 1, size - 1), radius=radius, fill=255
        )
        canvas.paste(image, (x, y), Image.composite(image.getchannel("A"), mask, mask))

    def fitted_face(
        text: str,
        preferred: int,
        minimum: int,
        width: int,
        *,
        serif: bool = False,
    ) -> ImageFont.FreeTypeFont:
        face = _font(fonts, preferred, serif=serif)
        while draw.textlength(text, font=face) > width and face.size > minimum:
            face = _font(fonts, face.size - 1, serif=serif)
        return face

    # Identity: the avatar is one full size tier larger than every ability or
    # weapon icon, and no ID or badge is drawn over it.
    identity_top, identity_bottom = 60, 390
    panel(identity_top, identity_bottom, (197, 126, 60))
    avatar_source = assets.avatar
    avatar_x = panel_left + 30
    avatar_y = identity_top + 30
    if avatar_source is not None:
        paste_rounded(avatar_source, avatar_x, avatar_y, avatar_size, 34)
    draw.rounded_rectangle(
        (avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size),
        radius=34,
        outline=(231, 178, 91, 235),
        width=4,
    )
    title, subtitle, element = _compact_identity_copy(character, language)
    identity_x = avatar_x + avatar_size + 54
    draw.text(
        (identity_x, identity_top + 68),
        subtitle,
        font=fitted_face(subtitle, 56, 34, panel_right - identity_x - 40, serif=True),
        fill=(244, 181, 78, 255),
    )
    element_size = 92
    element_x = identity_x
    element_y = identity_top + 151
    if assets.element is None:
        raise RuntimeError(
            f"角色 {character['id']} 缺少官方{element}属性图标"
        )
    element_image = Image.open(assets.element).convert("RGBA").resize(
        (element_size, element_size),
        Image.Resampling.LANCZOS,
    )
    canvas.paste(element_image, (element_x, element_y), element_image)
    title_x = element_x + element_size + 28
    draw.text(
        (title_x, identity_top + 142),
        title,
        font=fitted_face(title, 94, 64, panel_right - title_x - 40, serif=True),
        fill=(255, 247, 232, 255),
    )
    draw.line(
        (identity_x, identity_top + 260, panel_right - 42, identity_top + 260),
        fill=(224, 171, 83, 130),
        width=3,
    )
    _draw_compact_watermark(
        draw,
        fonts,
        right=panel_right - 34,
        top=identity_top + 22,
    )

    # Skills own the largest share of the portrait canvas and appear before all
    # equipment/collection information.
    skills_top = 410
    skill_height = 550
    skill_gap = 20
    skill_accents = ((197, 126, 60), (68, 116, 153))
    for index, skill in enumerate(skills):
        x = panel_left
        y = skills_top + index * (skill_height + skill_gap)
        bottom = y + skill_height
        accent = skill_accents[0 if skill["active"] else 1]
        panel(y, bottom, accent)

        icon_x, icon_y = x + 34, y + 30
        draw.rounded_rectangle(
            (icon_x, icon_y, icon_x + icon_size, icon_y + icon_size),
            radius=28,
            fill=(35, 50, 66, 255),
        )
        icon_path = assets.icons.get(skill["id"])
        if icon_path is not None:
            paste_rounded(icon_path, icon_x, icon_y, icon_size, 28)

        heading_x = icon_x + icon_size + 42
        heading_width = panel_right - heading_x - 38
        draw.text(
            (heading_x, y + 30),
            skill["name"],
            font=fitted_face(skill["name"], 66, 48, heading_width, serif=True),
            fill=(255, 247, 232, 255),
        )
        draw.text(
            (heading_x, y + 116),
            f"{skill['tag']} · 满强化最终效果",
            font=_font(fonts, 34, medium=True),
            fill=(229, 194, 132, 255) if skill["active"] else (175, 207, 228, 255),
        )
        draw.line(
            (heading_x, y + 178, panel_right - 38, y + 178),
            fill=(*accent, 125),
            width=2,
        )

        body, line_height, paragraph_gap, paragraphs = _compact_body_layout(
            draw,
            skill["compact_text"],
            fonts,
            panel_width - 160,
            skill_height - 224,
        )
        cursor = y + 208
        for paragraph in paragraphs:
            bullet_y = cursor + max(18, line_height // 2 - 6)
            draw.ellipse(
                (x + 48, bullet_y, x + 62, bullet_y + 14),
                fill=(*accent, 255),
            )
            for line in paragraph:
                draw.text(
                    (x + 92, cursor),
                    line,
                    font=body,
                    fill=(242, 241, 235, 255),
                )
                cursor += line_height
            cursor += paragraph_gap

    # The weapon uses the same 160px icon tier as skills. UR and LR effects are
    # separate rows so a standalone UR mechanic cannot disappear into skill copy.
    weapon_top, weapon_bottom = 2690, 3110
    panel(weapon_top, weapon_bottom, (197, 126, 60))
    weapon_icon_x, weapon_icon_y = panel_left + 34, weapon_top + 38
    draw.rounded_rectangle(
        (
            weapon_icon_x,
            weapon_icon_y,
            weapon_icon_x + icon_size,
            weapon_icon_y + icon_size,
        ),
        radius=28,
        fill=(52, 43, 38, 255),
    )
    if assets.weapon is not None:
        paste_rounded(assets.weapon, weapon_icon_x, weapon_icon_y, icon_size, 28)
    weapon_copy_x = weapon_icon_x + icon_size + 42
    weapon_name = _localized(weapon.get("names"), language) or "专属武器"
    draw.text(
        (weapon_copy_x, weapon_top + 38),
        weapon_name,
        font=fitted_face(
            weapon_name,
            66,
            48,
            panel_right - weapon_copy_x - 38,
            serif=True,
        ),
        fill=(255, 239, 205, 255),
    )
    stat_parts: list[str] = []
    for effect in passive_effects:
        name = PARAMETER_NAMES_ZH_CN.get(
            str(effect.get("parameter_code")), str(effect.get("parameter_code"))
        )
        stat_parts.append(f"{name} {effect.get('value_text') or ''}")
    stat_text = "　".join(stat_parts)
    draw.text(
        (weapon_copy_x, weapon_top + 128),
        stat_text,
        font=fitted_face(stat_text, 44, 34, panel_right - weapon_copy_x - 38),
        fill=(255, 224, 165, 255),
    )
    label_left, label_width, label_height = weapon_copy_x, 188, 58
    effect_x = label_left + label_width + 30
    effect_width = panel_right - effect_x - 36
    effect_body = _font(fonts, 38)
    effect_line_height = _line_height(draw, effect_body, gap=7)
    effect_cursor = weapon_top + 218
    effect_colors = ((143, 91, 157), (192, 117, 48))
    for (label, effect_text), label_color in zip(
        weapon_effect_rows, effect_colors
    ):
        lines = [
            line
            for sentence in _sentences(effect_text)
            for line in _wrap(draw, sentence, effect_body, effect_width)
        ]
        row_height = max(label_height, len(lines) * effect_line_height)
        label_top = effect_cursor + (row_height - label_height) // 2
        draw.rounded_rectangle(
            (
                label_left,
                label_top,
                label_left + label_width,
                label_top + label_height,
            ),
            radius=13,
            fill=(*label_color, 255),
        )
        label_face = _font(fonts, 30, medium=True)
        draw.text(
            (
                label_left
                + (label_width - draw.textlength(label, font=label_face)) / 2,
                label_top + 9,
            ),
            label,
            font=label_face,
            fill=(255, 249, 237, 255),
        )
        text_cursor = effect_cursor
        for line in lines:
            draw.text(
                (effect_x, text_cursor),
                line,
                font=effect_body,
                fill=(244, 241, 233, 255),
            )
            text_cursor += effect_line_height
        effect_cursor += row_height + 8
    if effect_cursor > weapon_bottom - 18:
        raise RuntimeError("专武 UR/LR 效果无法在省流模板中清晰排入")

    # Arcana closes the card: LR-rank effects first, then large unlock avatars
    # in up to two rows so the names remain readable.
    arcana_top, arcana_bottom = 3130, 3780
    panel(arcana_top, arcana_bottom, (68, 116, 153))
    if arcana is None:
        arcana_name = "暂未配置"
        arcana_summary = "官方 Master 尚未提供该角色的秘仪关系。"
    else:
        arcana_name = _localized(arcana.get("names"), language) or f"秘仪 {arcana.get('id')}"
        levels = [item for item in arcana.get("levels") or [] if isinstance(item, dict)]
        final_level = next(
            (item for item in levels if item.get("character_rarity_flags") == 512),
            {},
        )
        summary_parts: list[str] = []
        for effect in final_level.get("effects") or []:
            name = PARAMETER_NAMES_ZH_CN.get(
                str(effect.get("parameter_code")), str(effect.get("parameter_code"))
            )
            summary_parts.append(f"{name} {effect.get('value_text') or ''}")
        arcana_summary = " · ".join(summary_parts) or "LR效果未配置"

    arcana_label_left, arcana_label_top = panel_left + 34, arcana_top + 34
    draw.rounded_rectangle(
        (
            arcana_label_left,
            arcana_label_top,
            arcana_label_left + 140,
            arcana_label_top + 66,
        ),
        radius=13,
        fill=(60, 105, 137, 255),
    )
    arcana_label_face = _font(fonts, 34, medium=True)
    draw.text(
        (
            arcana_label_left
            + (140 - draw.textlength("秘仪", font=arcana_label_face)) / 2,
            arcana_label_top + 10,
        ),
        "秘仪",
        font=arcana_label_face,
        fill=(251, 249, 240, 255),
    )
    arcana_name_x = arcana_label_left + 172
    draw.text(
        (arcana_name_x, arcana_top + 31),
        arcana_name,
        font=fitted_face(
            arcana_name,
            62,
            46,
            panel_right - arcana_name_x - 38,
            serif=True,
        ),
        fill=(226, 239, 246, 255),
    )
    draw.text(
        (panel_left + 36, arcana_top + 134),
        "LR效果",
        font=_font(fonts, 38, medium=True),
        fill=(174, 209, 230, 255),
    )
    draw.text(
        (panel_left + 214, arcana_top + 130),
        arcana_summary,
        font=fitted_face(arcana_summary, 46, 34, panel_width - 250),
        fill=(239, 241, 239, 255),
    )
    draw.line(
        (panel_left + 34, arcana_top + 216, panel_right - 34, arcana_top + 216),
        fill=(93, 139, 168, 150),
        width=2,
    )
    draw.text(
        (panel_left + 36, arcana_top + 244),
        "解锁角色",
        font=_font(fonts, 40, medium=True),
        fill=(174, 209, 230, 255),
    )
    required_characters = (
        arcana.get("required_characters") or [] if isinstance(arcana, dict) else []
    )
    tile_gap = 24
    tile_width = (panel_width - 68 - tile_gap) // 2
    required_avatar_size = 150
    for index, required in enumerate(required_characters[:4]):
        column, row = index % 2, index // 2
        tile_x = panel_left + 34 + column * (tile_width + tile_gap)
        tile_y = arcana_top + 326 + row * 174
        character_id = required.get("id") if isinstance(required, dict) else None
        avatar_path = assets.arcana_characters.get(character_id)
        if avatar_path is not None:
            paste_rounded(
                avatar_path,
                tile_x,
                tile_y,
                required_avatar_size,
                28,
            )
        else:
            draw.rounded_rectangle(
                (
                    tile_x,
                    tile_y,
                    tile_x + required_avatar_size,
                    tile_y + required_avatar_size,
                ),
                radius=28,
                fill=(39, 56, 72, 255),
                outline=(106, 145, 170, 255),
                width=3,
            )
        required_name = (
            _localized(required.get("names"), language)
            if isinstance(required, dict)
            else None
        ) or "待公布"
        name_x = tile_x + required_avatar_size + 30
        draw.text(
            (name_x, tile_y + 42),
            required_name,
            font=fitted_face(
                required_name,
                48,
                36,
                tile_x + tile_width - name_x - 18,
            ),
            fill=(232, 237, 238, 255),
        )

    return _save_card(canvas, output, "compact")


def render_skill_card(
    payload: dict[str, Any],
    assets: CardAssets,
    output: Path,
    *,
    language: str = "zh-CN",
    mode: str = "full",
) -> Path:
    if mode == "compact":
        return _render_compact_skill_card(
            payload, assets, output, language=language
        )
    if mode != "full":
        raise ValueError("mode 必须是 full 或 compact")
    fonts = _font_candidates()
    character = payload["character"]
    skills = [
        _skill_view(skill, language)
        for skill in _display_skill_records(payload)
    ]
    if len(skills) > 4:
        raise RuntimeError(
            f"角色 {character['id']} 有 {len(skills)} 个技能，超过 4K 横版模板的四技能上限"
        )
    canvas = _background(HEIGHT, character["id"])
    draw = ImageDraw.Draw(canvas)

    left_x, left_y = MARGIN, MARGIN
    left_height = HEIGHT - MARGIN * 2
    art_size = LEFT_WIDTH
    art = _fit_crop(Image.open(assets.art).convert("RGBA"), (art_size, art_size), focus=(0.5, 0.5))
    mask = Image.new("L", art.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, art.width - 1, art.height - 1), radius=38, fill=255
    )
    canvas.paste(art, (left_x, left_y), mask)
    draw.rounded_rectangle(
        (left_x, left_y, left_x + LEFT_WIDTH, left_y + left_height),
        radius=38,
        outline=(224, 171, 83, 235),
        width=3,
    )

    title = _localized(character.get("names"), language) or character.get("memo") or str(character["id"])
    subtitle = _localized(character.get("subtitles"), language) or ""
    info_top = left_y + art_size - 22
    info_bottom = left_y + left_height
    draw.rounded_rectangle(
        (left_x, info_top, left_x + LEFT_WIDTH, info_bottom),
        radius=38,
        fill=(14, 24, 37, 244),
        outline=(224, 171, 83, 235),
        width=3,
    )
    draw.rectangle(
        (left_x + 2, info_top, left_x + LEFT_WIDTH - 2, info_top + 42),
        fill=(14, 24, 37, 244),
    )
    if subtitle:
        draw.text(
            (left_x + 60, info_top + 45),
            subtitle,
            font=_font(fonts, 52, serif=True),
            fill=(244, 193, 95, 255),
        )
    draw.text(
        (left_x + 54, info_top + (112 if subtitle else 58)),
        title,
        font=_font(fonts, 88, serif=True),
        fill=(255, 247, 232, 255),
    )
    element = ELEMENT_NAMES.get(character.get("element_type"), str(character.get("element_type")))
    job = JOB_NAMES.get(character.get("job_flags"), str(character.get("job_flags")))
    meta_y = info_top + (220 if subtitle else 166)
    draw.text(
        (left_x + 60, meta_y),
        f"ID {character['id']}  ·  {element}属性  ·  {job}",
        font=_font(fonts, 34, medium=True),
        fill=(224, 216, 199, 255),
    )
    draw.line(
        (left_x + 60, meta_y + 62, left_x + LEFT_WIDTH - 60, meta_y + 62),
        fill=(224, 171, 83, 160),
        width=2,
    )
    overview_y = meta_y + 92
    overview_icon = 100
    overview_gap = 22
    overview_width = (LEFT_WIDTH - 120 - overview_gap) // 2
    for index, skill in enumerate(skills):
        column, row = index % 2, index // 2
        x = left_x + 60 + column * (overview_width + overview_gap)
        y = overview_y + row * 126
        icon_path = assets.icons.get(skill["id"])
        if icon_path is None:
            raise RuntimeError(f"缺少技能图标: {skill['id']}")
        icon = Image.open(icon_path).convert("RGBA").resize(
            (overview_icon, overview_icon), Image.Resampling.LANCZOS
        )
        canvas.alpha_composite(icon, (x, y))
        draw.text(
            (x + 120, y + 12),
            skill["name"],
            font=_font(fonts, 31, serif=True),
            fill=(248, 240, 222, 255),
        )
        draw.text(
            (x + 121, y + 58),
            skill["tag"],
            font=_font(fonts, 22, medium=True),
            fill=(194, 201, 210, 255),
        )

    draw.text(
        (left_x + 60, info_bottom - 55),
        "数据：官方 Master ｜ 美术：官方游戏资源",
        font=_font(fonts, 22),
        fill=(183, 192, 203, 255),
    )

    right_x = left_x + LEFT_WIDTH + GAP
    right_width = WIDTH - right_x - MARGIN
    card_width = (right_width - GAP) // 2
    rows = max(1, (len(skills) + 1) // 2)
    card_height = (left_height - GAP * (rows - 1)) // rows
    accents = ((195, 122, 49), (64, 99, 129))
    for index, skill in enumerate(skills):
        column, row = index % 2, index // 2
        x = right_x + column * (card_width + GAP)
        y = left_y + row * (card_height + GAP)
        accent = accents[0 if skill["active"] else 1]
        fill = (248, 241, 225, 242) if skill["active"] else (229, 236, 239, 244)
        draw.rounded_rectangle(
            (x, y, x + card_width, y + card_height),
            radius=32,
            fill=fill,
            outline=(*accent, 255),
            width=4,
        )
        draw.rounded_rectangle(
            (x + 4, y + 4, x + card_width - 4, y + 164),
            radius=28,
            fill=(*accent, 238),
        )
        draw.rectangle(
            (x + 4, y + 126, x + card_width - 4, y + 164),
            fill=(*accent, 238),
        )
        icon_path = assets.icons.get(skill["id"])
        if icon_path is None:
            raise RuntimeError(f"缺少技能图标: {skill['id']}")
        icon = Image.open(icon_path).convert("RGBA").resize(
            (132, 132), Image.Resampling.LANCZOS
        )
        icon_frame = Image.new("RGBA", (148, 148), (0, 0, 0, 0))
        frame_draw = ImageDraw.Draw(icon_frame)
        frame_draw.rounded_rectangle(
            (0, 0, 147, 147),
            radius=20,
            fill=(24, 28, 34, 255),
            outline=(246, 207, 126, 255),
            width=4,
        )
        icon_frame.paste(icon, (8, 8), icon)
        canvas.alpha_composite(icon_frame, (x + 18, y + 9))
        draw.text(
            (x + 190, y + 30),
            skill["name"],
            font=_font(fonts, 48, serif=True),
            fill=(255, 249, 236, 255),
        )
        draw.text(
            (x + 192, y + 100),
            skill["tag"],
            font=_font(fonts, 27, medium=True),
            fill=(244, 224, 185, 255),
        )

        cursor = y + 196
        body_color = (38, 40, 44, 255)
        text_x = x + 190
        text_width = card_width - 220
        body, line_height, wrapped_levels = _skill_body_layout(
            draw,
            skill,
            fonts,
            text_width,
            y + card_height - 26 - cursor,
        )
        for level, lines in zip(skill["levels"], wrapped_levels):
            label = level["label"]
            draw.rounded_rectangle(
                (x + 24, cursor + 2, x + 118, cursor + 50),
                radius=11,
                fill=(*accent, 238),
            )
            label_face = _font(fonts, 25, medium=True)
            label_width = draw.textlength(label, font=label_face)
            draw.text(
                (x + 71 - label_width / 2, cursor + 7),
                label,
                font=label_face,
                fill=(255, 250, 239, 255),
            )
            unlock = level.get("unlock")
            unlock_text = f"{unlock}级" if isinstance(unlock, int) else ""
            if unlock_text:
                draw.text(
                    (x + 126, cursor + 10),
                    unlock_text,
                    font=_font(fonts, 22, medium=True),
                    fill=(*accent, 255),
                )
            for line_index, line in enumerate(lines):
                draw.text(
                    (text_x, cursor + line_index * line_height),
                    line,
                    font=body,
                    fill=body_color,
                )
            cursor += max(52, len(lines) * line_height) + 17

    if not payload.get("localization_complete", True):
        draw.text(
            (left_x + 60, info_bottom - 92),
            "部分本地化缺失，已回退到技能 Master 的日文 Memo。",
            font=_font(fonts, 21),
            fill=(241, 183, 107, 255),
        )

    return _save_card(canvas, output, "full")


def _resolve_character_jsons(
    skills_dir: Path, character_id: int | None, latest: bool, render_all: bool
) -> list[Path]:
    if render_all:
        paths = sorted((skills_dir / "characters").glob("[0-9][0-9][0-9][0-9][0-9][0-9].json"))
        if not paths:
            raise RuntimeError(f"没有角色技能 JSON: {skills_dir / 'characters'}")
        return paths
    if latest:
        latest_path = skills_dir / "latest.json"
        try:
            latest_data = json.loads(latest_path.read_text(encoding="utf-8"))
            characters = latest_data["latest_characters"]
            ids = [int(character["id"]) for character in characters]
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法从 {latest_path} 解析最新角色") from exc
        if not ids:
            raise RuntimeError(f"最新角色列表为空: {latest_path}")
        paths = [skills_dir / "characters" / f"{item:06d}.json" for item in ids]
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            raise RuntimeError(f"最新角色技能 JSON 不存在: {', '.join(missing)}")
        return paths
    if character_id is None:
        raise ValueError("必须指定 --character-id、--latest 或 --all")
    path = skills_dir / "characters" / f"{character_id:06d}.json"
    if not path.is_file():
        raise RuntimeError(f"角色技能 JSON 不存在: {path}")
    return [path]


def parse_args():
    parser = argparse.ArgumentParser(description="用统一模板生成官方角色技能介绍图。")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--character-id", type=int)
    selection.add_argument("--latest", action="store_true")
    selection.add_argument("--all", action="store_true", dest="render_all")
    selection.add_argument(
        "--verify-latest",
        action="store_true",
        help="不访问官方接口，仅校验最新角色卡片、来源哈希与模板版本",
    )
    parser.add_argument("--skills-dir", type=Path, default=DEFAULT_SKILLS_DIR)
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--output", type=Path, help="单角色模式的精确输出文件")
    output.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "reports" / "skill-card-cache")
    parser.add_argument("--language", default="zh-CN")
    parser.add_argument("--art-size", choices=("l", "m", "w"), default="l")
    parser.add_argument(
        "--mode",
        choices=("full", "compact", "both"),
        default="full",
        help="full=官方全文版，compact=满强化省流版，both=两版都生成",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.verify_latest:
            if args.output:
                raise ValueError("--verify-latest 只能与 --output-dir 配合使用")
            verified = verify_latest_skill_cards(
                args.skills_dir,
                args.output_dir,
                language=args.language,
                mode=args.mode,
            )
            for target in verified:
                print(target.resolve())
            print(f"最新角色技能图校验完成: {len(verified)} 张")
            return 0
        sources = _resolve_character_jsons(
            args.skills_dir, args.character_id, args.latest, args.render_all
        )
        if args.output and (len(sources) != 1 or args.mode == "both"):
            raise ValueError("--output 只能用于单角色单模式；批量或 both 请使用 --output-dir")
        outputs: list[Path] = []
        manifest_entries: list[dict[str, Any]] = []
        for source in sources:
            payload = _load_payload(source)
            character_id = payload["character"]["id"]
            skills = (
                _compact_skill_records(payload, args.language)
                if args.mode in ("compact", "both")
                else _display_skill_records(payload)
            )
            weapon = payload.get("exclusive_weapon")
            weapon_icon_id = (
                weapon.get("icon_id")
                if args.mode in ("compact", "both") and isinstance(weapon, dict)
                else None
            )
            element_type = payload["character"].get("element_type")
            element_icon = (
                args.skills_dir.parent
                / "assets"
                / "ui"
                / f"icon_element_{element_type}.png"
            )
            if not element_icon.is_file():
                element_icon = None
            arcana_character_ids = (
                [
                    int(item.get("id"))
                    for arcana in payload.get("arcanas") or []
                    if isinstance(arcana, dict)
                    for item in arcana.get("required_characters") or []
                    if isinstance(item, dict) and isinstance(item.get("id"), int)
                ]
                if args.mode in ("compact", "both")
                else []
            )
            assets = fetch_official_card_assets(
                character_id,
                [skill["id"] for skill in skills],
                args.cache_dir,
                art_size=args.art_size,
                weapon_icon_id=weapon_icon_id,
                include_avatar=args.mode in ("compact", "both"),
                element_icon=element_icon,
                arcana_character_ids=arcana_character_ids,
                allow_missing=args.mode == "compact",
            )
            modes = ("full", "compact") if args.mode == "both" else (args.mode,)
            for mode in modes:
                suffix = "" if mode == "full" else "-compact"
                target = args.output or (
                    args.output_dir
                    / f"character-{character_id:06d}{suffix}-{args.language}.png"
                )
                rendered = render_skill_card(
                    payload,
                    assets,
                    target,
                    language=args.language,
                    mode=mode,
                )
                outputs.append(rendered)
                if not args.output:
                    width, height = _expected_card_size(mode)
                    manifest_entries.append(
                        {
                            "key": _card_manifest_key(
                                character_id,
                                args.language,
                                mode,
                            ),
                            "character_id": character_id,
                            "language": args.language,
                            "mode": mode,
                            "path": rendered.relative_to(args.output_dir.parent).as_posix(),
                            "source_sha256": _sha256(source),
                            "template_version": _template_version(mode),
                            "asset_version": assets.asset_version,
                            "width": width,
                            "height": height,
                            "sha256": _sha256(rendered),
                        }
                    )
                print(target.resolve())
        if manifest_entries:
            manifest_path = _update_card_manifest(
                args.output_dir.parent / "manifest.json",
                manifest_entries,
            )
            print(manifest_path.resolve())
        print(f"技能介绍图生成完成: {len(outputs)} 张")
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"生成技能介绍图失败: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
