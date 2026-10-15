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
    OfficialAssetInfo,
    download_catalog,
    download_file,
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
SPLIT_WIDTH = WIDTH
SPLIT_HEIGHT = 2500
COMPACT_ICON_SIZE = 160
COMPACT_AVATAR_SIZE = 270
CARD_MANIFEST_SCHEMA_VERSION = 1
FULL_TEMPLATE_VERSION = "landscape-full-v1"
COMPACT_TEMPLATE_VERSION = "portrait-compact-v8"
SPLIT_TEMPLATE_VERSION = "landscape-split-v8.2"
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
    job_icon: Path | None = None
    banner: Path | None = None


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
    if mode == "split":
        return SPLIT_WIDTH, SPLIT_HEIGHT
    raise ValueError("mode 必须是 full、compact 或 split")


def _template_version(mode: str) -> str:
    if mode == "full":
        return FULL_TEMPLATE_VERSION
    if mode == "compact":
        return COMPACT_TEMPLATE_VERSION
    if mode == "split":
        return SPLIT_TEMPLATE_VERSION
    raise ValueError("mode 必须是 full、compact 或 split")


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
    mode: str = "split",
) -> list[Path]:
    if mode not in ("split", "full"):
        raise ValueError("竖版模板已停用，请使用 split 横版模板")
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
            suffix = "" if card_mode == "full" else f"-{card_mode}"
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


def _fetch_official_character_banner(
    info: Any,
    cache_dir: Path,
    character_id: int,
    *,
    language: str = "CN",
    allow_missing: bool = False,
) -> Path | None:
    """Fetch the game's complete banner, including its built-in copy."""
    raw_data_uri_format = getattr(info, "raw_data_uri_format", "")
    if not raw_data_uri_format:
        if allow_missing:
            return None
        raise RuntimeError("官方资源接口缺少 RawDataUriFormat，无法获取登场横幅")
    banner_dir = cache_dir / "banners"
    banner_dir.mkdir(parents=True, exist_ok=True)
    language_code = "CN" if language.casefold() in {"zh-cn", "cn", "zh"} else language.upper()
    for variant in ("01", "02"):
        filename = f"MYPAGE_BANNER_{language_code}_{variant}{character_id:04d}.png"
        target = banner_dir / filename
        if target.is_file():
            return target
        relative_path = f"MyPageBanner/{language_code}/{filename}"
        try:
            download_file(info.raw_data_url(relative_path), target)
            with Image.open(target) as banner:
                if banner.width < 2 or banner.height < 2:
                    raise RuntimeError(f"横幅尺寸无效: {target}")
            return target
        except (OSError, RuntimeError) as exc:
            target.unlink(missing_ok=True)
            target.with_name(target.name + ".tmp").unlink(missing_ok=True)
            if variant == "02" and not allow_missing:
                raise RuntimeError(f"无法获取角色 {character_id} 的官方登场横幅") from exc
    return None


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
    include_banner: bool = False,
    banner_language: str = "CN",
    allow_missing: bool = False,
    official_info: OfficialAssetInfo | None = None,
) -> CardAssets:
    if art_size not in {"l", "m", "w"}:
        raise ValueError("art_size 必须是 l、m 或 w")
    info = official_info or get_official_asset_info()
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
    banner = (
        _fetch_official_character_banner(
            info,
            cache_dir,
            character_id,
            language=banner_language,
            allow_missing=allow_missing,
        )
        if include_banner
        else None
    )
    return CardAssets(
        art=art,
        icons=icons,
        asset_version=info.asset_version,
        weapon=weapon,
        avatar=avatar,
        element=element_icon,
        arcana_characters=arcana_characters,
        banner=banner,
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
    "速度",
    "增益",
    "弱化",
    "状态",
    "效果",
    "解除",
    "发动",
    "触发",
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

        # Repeated-cast thresholds coexist with the initial effect. They are
        # battle conditions, rather than unconditional level replacements.
        if re.match(r"战斗中自第\d+次(?:发动|触发)此技能起，", core):
            text = _append_sentence(text, core)
            continue

        duration_upgrade = re.fullmatch(r"(「[^」]+」)效果增加为(\d+)回合", core)
        if duration_upgrade:
            buff, duration = duration_upgrade.groups()
            text, applied = _replace_sentence(
                text,
                lambda item: buff in item and "获得" in item,
                r"效果持续\d+回合",
                f"效果持续{duration}回合",
            )

        target_upgrade = re.fullmatch(r"(「[^」]+」)的附加目标增加为(.+)", core)
        if not applied and target_upgrade:
            buff, targets = target_upgrade.groups()
            grant = next(
                (item for item in _sentences(text) if buff in item and "获得" in item),
                "",
            )
            actor = re.search(r"([^，。]{1,20})使(?:自己|自身)", grant)
            if actor and targets.startswith(actor.group(1) + "与"):
                targets = "自己" + targets[len(actor.group(1)):]
            text, applied = _replace_sentence(
                text,
                lambda item: buff in item and "获得" in item,
                r"使[^，。]*?获得(?=[^。]*" + re.escape(buff) + r")",
                f"使{targets}获得",
            )

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
            match = re.search(
                r"生命值恢复量提升为承受伤害×([\d.]+)%（最多([\d.]+)%最大生命值）",
                core,
            )
            if match:
                value, maximum = match.groups()
                text, applied = _replace_sentence(
                    text,
                    lambda item: "恢复承受伤害×" in item,
                    r"承受伤害×[\d.]+%的生命值（最多[\d.]+%最大生命值）",
                    f"承受伤害×{value}%的生命值（最多{maximum}%最大生命值）",
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
            match = re.search(r"(?:攻击|技能)目标增加为(\d+)名随机敌人", core)
            if match:
                value = match.group(1)
                text, applied = _replace_sentence(
                    text,
                    lambda item: "随机对" in item and "名敌人" in item and "伤害" in item,
                    r"随机对\d+名敌人",
                    f"随机对{value}名敌人",
                )

        if not applied:
            match = re.search(
                r"「护盾」值提升为[^。]*?攻击力×([\d.]+)%", core
            )
            if match:
                value = match.group(1)
                target_specific = "附加目标" in core
                text, applied = _replace_sentence(
                    text,
                    lambda item: (
                        "「护盾」" in item
                        and ("附加目标" in item) == target_specific
                    ),
                    r"(攻击力×)[\d.]+%",
                    rf"\g<1>{value}%",
                )

        if not applied:
            match = re.search(
                r"^(?:发动攻击前，)?(如果.+?，|当.+?时，)"
                r"((?:造成的)?(?:物理|魔法)?伤害提升为攻击力×[\d.]+%)$",
                core,
            )
            if match:
                condition, effect = match.groups()
                condition = condition.rstrip("，")
                if condition.startswith("如果"):
                    condition = condition.removeprefix("如果")
                elif condition.startswith("当"):
                    condition = condition.removeprefix("当").removesuffix("时")
                compact_condition = f"若{condition}，"
                text, applied = _replace_sentence(
                    text,
                    lambda item: compact_condition in item and "攻击力×" in item,
                    r"攻击力×[\d.]+%",
                    re.search(r"攻击力×[\d.]+%", effect).group(0),
                )
                if not applied:
                    text = _append_sentence(text, compact_condition + effect)
                    applied = True

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

        # A later target-count upgrade supersedes the earlier target-count
        # sentence. Keep only the final target rule in the compact summary.
        if not applied and "附加目标增加为" in core:
            parts = [
                sentence
                for sentence in _sentences(text)
                if "附加目标增加为" not in sentence
            ]
            text = "".join(parts) + core + "。"
            applied = True

        if "最大生命值增幅提升为" in core:
            match = re.search(r"最大生命值增幅提升为([\d.]+)%", core)
            if match:
                value = match.group(1)
                text, hp_applied = _replace_sentence(
                    text,
                    # The upgrade refers to the battle-start self bonus that
                    # was introduced by the preceding level. Prefer that
                    # sentence over an earlier conditional team bonus, whose
                    # 30% value remains unchanged.
                    lambda item: (
                        "战斗开始时" in item
                        and "额外增加" in item
                        and "最大生命值" in item
                    ),
                    r"(增加)[\d.]+%(最大生命值)",
                    rf"\g<1>{value}%\2",
                )
                if not hp_applied:
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
        if level.get("equipment_rarity_flags") != 512
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
    text = _minify_compact_text(text)

    # Keep the two parts of the Holy Sword Glow passive together: the final
    # target upgrade belongs to the buff grant, while the heal upgrade and
    # defeat clause describe one mechanic and read as one compact entry.
    parts = _sentences(text)
    target_index = next(
        (
            index
            for index, sentence in enumerate(parts)
            if "圣剑光辉" in sentence and "附加目标增加为全体友军" in sentence
        ),
        None,
    )
    if target_index is not None:
        grant_index = next(
            (
                index
                for index, sentence in enumerate(parts)
                if "圣剑光辉" in sentence and "获得" in sentence
            ),
            None,
        )
        if grant_index is not None:
            parts[grant_index] = re.sub(
                r"使自己与攻击力最高的其他友军获得",
                "使全体友军获得",
                parts[grant_index],
                count=1,
            )
            del parts[target_index]

    heal_index = next(
        (
            index
            for index, sentence in enumerate(parts)
            if "圣剑光辉" in sentence and "恢复承受伤害×" in sentence
        ),
        None,
    )
    defeat_index = next(
        (
            index
            for index, sentence in enumerate(parts)
            if "失去战斗能力" in sentence
        ),
        None,
    )
    if heal_index is not None and defeat_index is not None and heal_index != defeat_index:
        parts[heal_index] = (
            parts[heal_index].rstrip("。")
            + "；"
            + parts[defeat_index].lstrip()
        )
        del parts[defeat_index]

    return "".join(parts)


def _minify_compact_text(text: str) -> str:
    text = text.replace("无法被解除", "无法解除")
    text = text.replace("其他业红属性", "其他红属性")
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
            increase = f"三防增加自身对应三防×{defense}%"
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
    text = re.sub(
        r"如果([^，。]+)在敌人发动攻击后仍然存活，([\d.]+)%概率解除"
        r"附带弱化效果最多的友军身上的1种弱化效果",
        r"敌人攻击后若\1存活，\2%概率解除弱化最多的友军1种弱化",
        text,
    )
    text = re.sub(
        r"在解除弱化效果发动判定前，如果身上附带弱化效果的友军人数为0，"
        r"[^，。]+使攻击力最高的(\d+)名友军额外增加([\d.]+)%攻击力，"
        r"效果持续(\d+)回合",
        r"判定前若没有友军带弱化，使攻击力最高的\1名友军攻击力+\2%，持续\3回合",
        text,
    )
    text = re.sub(
        r"若([^，。]+)在战斗中因技能效果解除的弱化效果总数达(\d+)种以上，"
        r"造成的(?:物理|魔法)?伤害提升为",
        r"若\1通过技能累计解除弱化≥\2种，伤害提升至",
        text,
    )
    text = re.sub(
        r"第1回合开始时，[^，。]+使全体友军获得([^，。]+攻击力×[\d.]+%)的「护盾」",
        r"第1回合开始，为全体友军附加\1的「护盾」",
        text,
    )
    text = re.sub(
        r"当「护盾」的附加目标为([^，。]+)时，「护盾」值提升为([^。]+)",
        r"\1目标的「护盾」提升至\2",
        text,
    )
    text = re.sub(
        r"战斗开始时，[^，。]+额外减少([\d.]+)%自身承受伤害",
        r"战斗开始时，自身承受伤害-\1%",
        text,
    )
    text = re.sub(r"额外减少([\d.]+)%吸血", r"吸血-\1%", text)
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


def _preview_lr_skill_effect_rows(
    skills: list[dict[str, Any]], language: str
) -> list[tuple[str, str]]:
    """Keep published LR skill records visible when the weapon is not published."""
    rows = []
    for skill in skills:
        name = _localized(skill.get("names"), language) or f"技能 {skill.get('id')}"
        for level in skill.get("levels") or []:
            if level.get("equipment_rarity_flags") != 512:
                continue
            description = _localized(level.get("descriptions"), language)
            if description:
                description = re.sub(r"^强化[^，。]+[，。]", "", description)
                rows.append(("LR技能强化（主表预览）", f"{name}：{description}"))
    return rows


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
    *,
    maximum: int = 72,
) -> tuple[ImageFont.FreeTypeFont, int, int, list[list[str]]]:
    paragraphs = _sentences(text)
    # Long new-character passive descriptions can exceed the original 44px
    # floor even after compacting.  Keep the portrait layout fixed, but allow
    # the body copy to step down to 24px so the complete text remains present
    # and readable instead of rejecting the vertical card.
    for size in range(maximum, 23, -1):
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
    title_x = element_x
    if assets.element is not None:
        element_image = Image.open(assets.element).convert("RGBA").resize(
            (element_size, element_size),
            Image.Resampling.LANCZOS,
        )
        canvas.paste(element_image, (element_x, element_y), element_image)
        title_x += element_size + 28
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
    speed = character.get("speed")
    speed_text = f"初始速度 {speed}" if isinstance(speed, int) and speed > 0 else "初始速度 暂未公开"
    speed_face = _font(fonts, 50, medium=True)
    speed_width = draw.textlength(speed_text, font=speed_face)
    speed_right = weapon_x + footer_width - 40
    draw.text(
        (speed_right, footer_top + 72), speed_text, font=speed_face,
        anchor="rm", fill=(255, 224, 165, 255),
    )
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


def _render_split_skill_card(
    payload: dict[str, Any],
    assets: CardAssets,
    output: Path,
    *,
    language: str,
) -> Path:
    """Trial landscape template with art at left and a two-by-two skill grid."""
    character = payload["character"]
    fonts = _font_candidates()
    raw_skills = _compact_skill_records(payload, language)
    skills = [_skill_view(skill, language) for skill in raw_skills]
    for view, raw in zip(skills, raw_skills):
        view["compact_text"] = _compact_skill_text(raw, language)
    active_skills = [skill for skill in skills if skill["active"]]
    passive_skills = [skill for skill in skills if not skill["active"]]
    if len(active_skills) != 2 or len(passive_skills) != 2:
        raise RuntimeError(
            f"角色 {character['id']} 的试验模板需要 2 个主动和 2 个被动技能，"
            f"当前为 {len(active_skills)} 个主动、{len(passive_skills)} 个被动"
        )
    skills = active_skills + passive_skills

    weapon = payload.get("exclusive_weapon")
    weapon_available = isinstance(weapon, dict)
    weapon = weapon if weapon_available else {}
    weapon_effect_rows = (
        _compact_weapon_effect_rows(weapon, language)
        if weapon_available
        else _preview_lr_skill_effect_rows(raw_skills, language)
    )
    arcanas = [item for item in payload.get("arcanas") or [] if isinstance(item, dict)]
    arcana = arcanas[-1] if arcanas else None

    canvas = _background(SPLIT_HEIGHT, character["id"], width=SPLIT_WIDTH)
    draw = ImageDraw.Draw(canvas)
    margin = 48
    gap = 28
    left_width = 1080
    right_left = margin + left_width + gap
    right_width = SPLIT_WIDTH - right_left - margin
    panel_fill = (13, 23, 36, 247)

    def panel(x: int, y: int, width: int, height: int, accent: tuple[int, int, int]) -> None:
        draw.rounded_rectangle(
            (x, y, x + width, y + height),
            radius=34,
            fill=panel_fill,
            outline=(*accent, 235),
            width=3,
        )

    def paste_rounded(source: Path | None, x: int, y: int, size: int, radius: int) -> None:
        if source is None:
            draw.rounded_rectangle(
                (x, y, x + size, y + size),
                radius=radius,
                fill=(39, 56, 72, 255),
                outline=(106, 145, 170, 255),
                width=3,
            )
            return
        image = _fit_crop(Image.open(source).convert("RGBA"), (size, size), focus=(0.5, 0.5))
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
        canvas.paste(image, (x, y), Image.composite(image.getchannel("A"), mask, mask))

    def fitted_face(
        text: str, preferred: int, minimum: int, width: int,
        *, serif: bool = False, medium: bool = False,
    ):
        face = _font(fonts, preferred, serif=serif, medium=medium)
        while draw.textlength(text, font=face) > width and face.size > minimum:
            face = _font(fonts, face.size - 1, serif=serif, medium=medium)
        return face

    # Keep the native banner intact, followed by two identity rows and full artwork.
    visual_top, visual_bottom = margin, 1880
    panel(margin, visual_top, left_width, visual_bottom - visual_top, (197, 126, 60))
    banner_x, banner_y, banner_width = margin + 40, visual_top + 30, 1000
    banner_bottom = banner_y + 500
    if assets.banner is not None:
        banner = Image.open(assets.banner).convert("RGBA")
        banner_size = (banner_width, round(banner_width * banner.height / banner.width))
        banner = banner.resize(banner_size, Image.Resampling.LANCZOS)
        banner_x = margin + (left_width - banner_size[0]) // 2
        canvas.paste(banner, (banner_x, banner_y), banner)
        banner_bottom = banner_y + banner_size[1]
    else:
        draw.rounded_rectangle(
            (banner_x, banner_y, banner_x + banner_width, banner_bottom),
            radius=20, fill=(28, 43, 57, 255), outline=(85, 112, 133, 255), width=2,
        )
        draw.text(
            (banner_x + banner_width / 2, banner_y + 250), "官方横幅暂未公开",
            font=_font(fonts, 48, medium=True), anchor="mm", fill=(174, 194, 207, 255),
        )

    title, subtitle, element = _compact_identity_copy(character, language)
    identity_y = banner_bottom + 32
    identity_face = _font(fonts, 52, medium=True)
    icon_size = 64
    identity_items = (
        (assets.element, f"{element}属性"),
        (assets.job_icon, JOB_NAMES.get(character.get("job_flags"), "未知")),
        (None, f"ID {character['id']}"),
    )
    identity_width = sum(
        draw.textlength(label, font=identity_face) + (icon_size + 14 if source is not None else 0)
        for source, label in identity_items
    ) + 80
    identity_x = margin + (left_width - identity_width) / 2
    for source, label in identity_items:
        if source is not None:
            paste_rounded(source, round(identity_x), identity_y, icon_size, 12)
            identity_x += icon_size + 14
        text_bounds = draw.textbbox((0, 0), label, font=identity_face, anchor="lt")
        text_height = text_bounds[3] - text_bounds[1]
        draw.text(
            (identity_x, identity_y + (icon_size - text_height) / 2 - text_bounds[1]), label,
            font=identity_face, anchor="lt", fill=(223, 227, 224, 255),
        )
        identity_x += draw.textlength(label, font=identity_face) + 40

    preview_notice = payload.get("preview_notice")
    preview_notice = preview_notice if isinstance(preview_notice, str) else ""
    notice_face = _font(fonts, 36, medium=True)
    notice_width = draw.textlength(preview_notice, font=notice_face) + 28 if preview_notice else 0
    notice_gap = 28 if preview_notice else 0
    name_face = fitted_face(
        f"{subtitle} {title}", 60, 48,
        left_width - 100 - notice_width - notice_gap, medium=True,
    )
    name_gap = 18 if subtitle else 0
    name_width = draw.textlength(subtitle + title, font=name_face) + name_gap
    name_x = margin + (left_width - name_width - notice_width - notice_gap) / 2
    name_y = identity_y + 96
    draw.text(
        (name_x, name_y), subtitle,
        font=name_face, anchor="lt", fill=(244, 181, 78, 255),
    )
    draw.text(
        (name_x + draw.textlength(subtitle, font=name_face) + name_gap, name_y), title,
        font=name_face, anchor="lt", fill=(255, 247, 232, 255),
    )
    if preview_notice:
        notice_x = name_x + name_width + notice_gap
        draw.rounded_rectangle(
            (notice_x, name_y + 2, notice_x + notice_width, name_y + 58),
            radius=10, fill=(84, 61, 35, 255), outline=(197, 145, 77, 255), width=2,
        )
        draw.text(
            (notice_x + notice_width / 2, name_y + 30), preview_notice,
            font=notice_face, anchor="mm", fill=(255, 220, 160, 255),
        )

    art_limit = 1000
    art_y = identity_y + 190
    if assets.art is not None:
        source_art = Image.open(assets.art).convert("RGBA")
        art = source_art.copy()
        art.thumbnail((art_limit, art_limit), Image.Resampling.LANCZOS)
        art_x = margin + (left_width - art.width) // 2
        canvas.paste(art, (art_x, art_y), art)
    else:
        art_x = margin + (left_width - art_limit) // 2
        draw.rounded_rectangle(
            (art_x, art_y, art_x + art_limit, art_y + art_limit),
            radius=20, fill=(28, 43, 57, 255), outline=(85, 112, 133, 255), width=2,
        )
        draw.text(
            (art_x + art_limit / 2, art_y + art_limit / 2), "官方立绘暂未公开",
            font=_font(fonts, 48, medium=True), anchor="mm", fill=(174, 194, 207, 255),
        )

    # The wide footer gives weapon and arcana copy the same readable scale as skills.
    footer_top, footer_bottom = 1908, SPLIT_HEIGHT - margin
    footer_gap = 28
    footer_width = (SPLIT_WIDTH - margin * 2 - footer_gap) // 2
    weapon_x = margin
    arcana_x = margin + footer_width + footer_gap
    panel(weapon_x, footer_top, footer_width, footer_bottom - footer_top, (197, 126, 60))
    panel(arcana_x, footer_top, footer_width, footer_bottom - footer_top, (68, 116, 153))
    weapon_icon = 145
    paste_rounded(assets.weapon, weapon_x + 30, footer_top + 28, weapon_icon, 26)
    weapon_name = _localized(weapon.get("names"), language) or "专属武器"
    speed = character.get("speed")
    speed_text = f"初始速度 {speed}" if isinstance(speed, int) and speed > 0 else "初始速度 暂未公开"
    speed_face = _font(fonts, 50, medium=True)
    speed_width = draw.textlength(speed_text, font=speed_face)
    speed_right = weapon_x + footer_width - 40
    draw.text(
        (speed_right, footer_top + 72), speed_text, font=speed_face,
        anchor="rm", fill=(255, 224, 165, 255),
    )
    draw.text(
        (weapon_x + 210, footer_top + 32),
        weapon_name,
        font=fitted_face(weapon_name, 64, 48, footer_width - 290 - speed_width, medium=True),
        fill=(255, 239, 205, 255),
        stroke_width=1, stroke_fill=(255, 239, 205, 255),
    )
    stat_parts: list[str] = []
    for effect in weapon.get("passive_effects") or []:
        name = PARAMETER_NAMES_ZH_CN.get(str(effect.get("parameter_code")), str(effect.get("parameter_code")))
        stat_parts.append(f"{name} {effect.get('value_text') or ''}")
    if not weapon_available:
        stat_parts.append("官方数据暂未公开")
    stat_face = _font(fonts, 43)
    stat_lines = _wrap(draw, "  ".join(stat_parts), stat_face, footer_width - 250)
    for line_index, line in enumerate(stat_lines[:2]):
        draw.text(
            (weapon_x + 210, footer_top + 111 + line_index * 54),
            line,
            font=stat_face,
            fill=(255, 224, 165, 255),
        )
    draw.line((weapon_x + 30, footer_top + 225, weapon_x + footer_width - 30, footer_top + 225), fill=(197, 126, 60, 120), width=2)
    effect_y = footer_top + 255
    effect_body = _font(fonts, 54)
    effect_width = footer_width - 310
    for label, effect_text in weapon_effect_rows:
        if not weapon_available:
            draw.text(
                (weapon_x + 30, effect_y), label,
                font=_font(fonts, 38, medium=True), fill=(229, 194, 132, 255),
            )
            effect_y += 62
            for line in _wrap(draw, effect_text, effect_body, footer_width - 60):
                draw.text((weapon_x + 30, effect_y), line, font=effect_body, fill=(244, 241, 233, 255))
                effect_y += 70
            effect_y += 16
            continue
        lines = [line for sentence in _sentences(effect_text) for line in _wrap(draw, sentence, effect_body, effect_width)]
        label_width = 195
        draw.rounded_rectangle((weapon_x + 30, effect_y, weapon_x + 30 + label_width, effect_y + 66), radius=11, fill=(143, 91, 157, 255) if label.startswith("UR") else (192, 117, 48, 255))
        label_face = _font(fonts, 38, medium=True)
        draw.text((weapon_x + 30 + (label_width - draw.textlength(label, font=label_face)) / 2, effect_y + 6), label, font=label_face, fill=(255, 249, 237, 255))
        for line in lines:
            draw.text((weapon_x + 255, effect_y), line, font=effect_body, fill=(244, 241, 233, 255))
            effect_y += 70
        effect_y += 16
    if effect_y > footer_bottom - 10:
        raise RuntimeError("专武效果文本超出横版模板页脚")

    arcana_name = "暂未配置"
    arcana_summary = "官方 Master 尚未提供该角色的秘仪关系。"
    if arcana is not None:
        arcana_name = _localized(arcana.get("names"), language) or f"秘仪 {arcana.get('id')}"
        levels = [item for item in arcana.get("levels") or [] if isinstance(item, dict)]
        final_level = next((item for item in levels if item.get("character_rarity_flags") == 512), {})
        summary_parts = []
        for effect in final_level.get("effects") or []:
            name = PARAMETER_NAMES_ZH_CN.get(str(effect.get("parameter_code")), str(effect.get("parameter_code")))
            summary_parts.append(f"{name} {effect.get('value_text') or ''}")
        arcana_summary = " · ".join(summary_parts) or "LR效果未配置"
    def draw_centered_left(
        text: str, x: int, center_y: float, font: ImageFont.FreeTypeFont, **kwargs: Any
    ) -> None:
        bounds = draw.textbbox((0, 0), text, font=font, anchor="lt")
        visible_height = bounds[3] - bounds[1]
        draw.text(
            (x, center_y - visible_height / 2 - bounds[1]),
            text,
            font=font,
            anchor="lt",
            **kwargs,
        )

    arcana_row_center = footer_top + 65
    draw_centered_left(
        "秘仪", arcana_x + 30, arcana_row_center,
        _font(fonts, 42, medium=True), fill=(174, 209, 230, 255),
    )
    draw_centered_left(
        arcana_name, arcana_x + 160, arcana_row_center,
        fitted_face(arcana_name, 64, 48, footer_width - 200, medium=True),
        fill=(226, 239, 246, 255), stroke_width=1,
        stroke_fill=(226, 239, 246, 255),
    )
    arcana_effect_center = footer_top + 145
    draw_centered_left(
        "LR效果", arcana_x + 30, arcana_effect_center,
        _font(fonts, 38, medium=True), fill=(174, 209, 230, 255),
    )
    arcana_face = _font(fonts, 54)
    arcana_lines = _wrap(draw, arcana_summary, arcana_face, footer_width - 270)
    for line_index, line in enumerate(arcana_lines):
        draw_centered_left(
            line, arcana_x + 205, arcana_effect_center + line_index * 66,
            arcana_face, fill=(239, 241, 239, 255),
        )
    draw.line((arcana_x + 30, footer_top + 255, arcana_x + footer_width - 30, footer_top + 255), fill=(93, 139, 168, 150), width=2)
    draw.text((arcana_x + 30, footer_top + 276), "解锁角色", font=_font(fonts, 42, medium=True), fill=(174, 209, 230, 255))
    required_characters = arcana.get("required_characters") or [] if isinstance(arcana, dict) else []
    for index, required in enumerate(required_characters[:4]):
        column, row = index % 2, index // 2
        tile_x = arcana_x + 30 + column * 890
        tile_y = footer_top + 342 + row * 105
        character_id = required.get("id") if isinstance(required, dict) else None
        paste_rounded(assets.arcana_characters.get(character_id), tile_x, tile_y, 88, 18)
        required_name = (_localized(required.get("names"), language) if isinstance(required, dict) else None) or "待公布"
        draw.text((tile_x + 105, tile_y + 16), required_name, font=fitted_face(required_name, 50, 38, 680), fill=(232, 237, 238, 255))

    # Right text column: active skills on the first row, passive skills below.
    skill_gap = 20
    skill_width = (right_width - skill_gap) // 2
    active_height = 760
    passive_height = visual_bottom - margin - skill_gap - active_height
    accents = ((197, 126, 60), (68, 116, 153))
    for index, skill in enumerate(skills):
        column, row = index % 2, index // 2
        x = right_left + column * (skill_width + skill_gap)
        y = margin if row == 0 else margin + active_height + skill_gap
        skill_height = active_height if row == 0 else passive_height
        accent = accents[0 if skill["active"] else 1]
        panel(x, y, skill_width, skill_height, accent)
        icon_path = assets.icons.get(skill["id"])
        if icon_path is not None:
            paste_rounded(icon_path, x + 25, y + 15, 140, 24)
        heading_x = x + 189
        draw.text(
            (heading_x, y + 21), skill["name"],
            font=fitted_face(skill["name"], 70, 52, skill_width - 185, medium=True),
            fill=(255, 247, 232, 255), stroke_width=1,
            stroke_fill=(255, 247, 232, 255),
        )
        draw.text((heading_x, y + 110), skill["tag"], font=_font(fonts, 38, medium=True), fill=(229, 194, 132, 255) if skill["active"] else (175, 207, 228, 255))
        draw.line((x + 30, y + 160, x + skill_width - 30, y + 160), fill=(*accent, 125), width=2)
        body, line_height, paragraph_gap, paragraphs = _compact_body_layout(
            draw, skill["compact_text"], fonts, skill_width - 112, skill_height - 220,
            maximum=54,
        )
        cursor = y + 194
        for paragraph in paragraphs:
            bullet_y = cursor + max(18, line_height // 2 - 6)
            draw.ellipse((x + 29, bullet_y, x + 43, bullet_y + 14), fill=(*accent, 255))
            for line in paragraph:
                draw.text((x + 67, cursor), line, font=body, fill=(242, 241, 235, 255))
                cursor += line_height
            cursor += paragraph_gap

    watermark_face = _font(fonts, 24, medium=True)
    watermark_x = SPLIT_WIDTH - 24 - draw.textlength(COMPACT_WATERMARK, font=watermark_face)
    draw.text((watermark_x, SPLIT_HEIGHT - 36), COMPACT_WATERMARK, font=watermark_face, fill=(123, 115, 105, 255))

    return _save_card(canvas, output, "split")


def render_skill_card(
    payload: dict[str, Any],
    assets: CardAssets,
    output: Path,
    *,
    language: str = "zh-CN",
    mode: str = "split",
) -> Path:
    if mode == "compact":
        raise ValueError("竖版模板已停用，请使用 split 横版模板")
    if mode == "split":
        return _render_split_skill_card(payload, assets, output, language=language)
    if mode != "full":
        raise ValueError("mode 必须是 full 或 split；旧竖版模板已停用")
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
        choices=("split",),
        default="split",
        help="统一使用 split 横版角色模板（3840×2500）；旧竖版模板已停用",
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
                if args.mode in ("compact", "split", "both")
                else _display_skill_records(payload)
            )
            weapon = payload.get("exclusive_weapon")
            weapon_icon_id = (
                weapon.get("icon_id")
                if args.mode in ("compact", "split", "both") and isinstance(weapon, dict)
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
            job_icon_name = {1: "warrior", 2: "sniper", 4: "sorcerer"}.get(
                payload["character"].get("job_flags")
            )
            job_icon = (
                args.skills_dir.parent / "assets" / "ui" / f"icon_job_{job_icon_name}.png"
                if job_icon_name else None
            )
            if job_icon is not None and not job_icon.is_file():
                job_icon = None
            arcana_character_ids = (
                [
                    int(item.get("id"))
                    for arcana in payload.get("arcanas") or []
                    if isinstance(arcana, dict)
                    for item in arcana.get("required_characters") or []
                    if isinstance(item, dict) and isinstance(item.get("id"), int)
                ]
                if args.mode in ("compact", "split", "both")
                else []
            )
            assets = fetch_official_card_assets(
                character_id,
                [skill["id"] for skill in skills],
                args.cache_dir,
                art_size=args.art_size,
                weapon_icon_id=weapon_icon_id,
                include_avatar=args.mode in ("compact", "split", "both"),
                element_icon=element_icon,
                arcana_character_ids=arcana_character_ids,
                include_banner=args.mode == "split",
                banner_language="CN",
                allow_missing=args.mode in ("compact", "split"),
            )
            assets = CardAssets(
                art=assets.art,
                icons=assets.icons,
                asset_version=assets.asset_version,
                weapon=assets.weapon,
                avatar=assets.avatar,
                element=assets.element,
                arcana_characters=assets.arcana_characters,
                job_icon=job_icon,
                banner=assets.banner,
            )
            modes = ("full", "compact") if args.mode == "both" else (args.mode,)
            for mode in modes:
                suffix = "" if mode == "full" else f"-{mode}"
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
                            "initial_speed": payload["character"].get("speed"),
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
