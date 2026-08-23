"""Download, validate, and export character skills from official Master data."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import msgpack

from asset_cdn import OfficialAssetInfo, download_file, get_official_asset_info


JST = timezone(timedelta(hours=9))
SCHEMA_VERSION = 3
GENERATED_MARKER = ".generated-by-mmtm-apk-tracker"
CORE_BOOKS = (
    "CharacterMB",
    "ActiveSkillMB",
    "PassiveSkillMB",
    "EquipmentMB",
    "EquipmentExclusiveEffectMB",
    "EquipmentExclusiveSkillDescriptionMB",
    "CharacterCollectionMB",
    "CharacterCollectionLevelMB",
)
LANGUAGE_BOOKS = {
    "de-DE": "TextResourceDeDeMB",
    "en-US": "TextResourceEnUsMB",
    "es-MX": "TextResourceEsMxMB",
    "fr-FR": "TextResourceFrFrMB",
    "id-ID": "TextResourceIdIdMB",
    "ja-JP": "TextResourceJaJpMB",
    "ko-KR": "TextResourceKoKrMB",
    "pt-BR": "TextResourcePtBrMB",
    "ru-RU": "TextResourceRuRuMB",
    "th-TH": "TextResourceThThMB",
    "vi-VN": "TextResourceViVnMB",
    "zh-CN": "TextResourceZhCnMB",
    "zh-TW": "TextResourceZhTwMB",
}

BASE_PARAMETER_CODES = {
    1: "Muscle",
    2: "Energy",
    3: "Intelligence",
    4: "Health",
}
BATTLE_PARAMETER_CODES = {
    1: "Hp",
    2: "AttackPower",
    3: "PhysicalDamageRelax",
    4: "MagicDamageRelax",
    5: "Hit",
    6: "Avoidance",
    7: "Critical",
    8: "CriticalResist",
    9: "CriticalDamageEnhance",
    10: "PhysicalCriticalDamageRelax",
    11: "MagicCriticalDamageRelax",
    12: "DefensePenetration",
    13: "Defense",
    14: "DamageEnhance",
    15: "DebuffHit",
    16: "DebuffResist",
    17: "DamageReflect",
    18: "HpDrain",
    19: "Speed",
}


@dataclass(frozen=True)
class MasterSource:
    app_version: str
    asset_version: str
    master_version: str

    @property
    def generated_at(self) -> str:
        if self.master_version.isdigit():
            moment = datetime.fromtimestamp(int(self.master_version) / 1000, timezone.utc)
            return moment.isoformat()
        return ""


def _json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _md5(path: Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unpack(path: Path) -> Any:
    try:
        return msgpack.unpackb(path.read_bytes(), raw=False, timestamp=3)
    except (OSError, ValueError, msgpack.ExtraData) as exc:
        raise RuntimeError(f"无法解包 Master 文件: {path}") from exc


def _book_path(master_dir: Path, name: str) -> Path:
    for candidate in (master_dir / name, master_dir / f"{name}.msgpack"):
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"Master 文件缺失: {name}")


def _catalog_map(master_dir: Path) -> dict[str, dict[str, Any]]:
    catalog = _unpack(_book_path(master_dir, "master-catalog"))
    books = catalog.get("MasterBookInfoMap") if isinstance(catalog, dict) else None
    if not isinstance(books, dict):
        raise RuntimeError("master-catalog 缺少 MasterBookInfoMap")
    return books


def _validate_book(path: Path, name: str, metadata: dict[str, Any]) -> None:
    expected_size = metadata.get("Size")
    expected_hash = str(metadata.get("Hash", "")).lower()
    if metadata.get("Name") not in (None, "", name):
        raise RuntimeError(f"Master catalog 名称不匹配: {name}")
    if not isinstance(expected_size, int) or expected_size <= 0:
        raise RuntimeError(f"Master catalog 缺少有效大小: {name}")
    if not re.fullmatch(r"[0-9a-f]{32}", expected_hash):
        raise RuntimeError(f"Master catalog 缺少有效 MD5: {name}")
    actual_size = path.stat().st_size
    if actual_size != expected_size:
        raise RuntimeError(
            f"Master 文件大小不一致: {name}: {actual_size}/{expected_size}"
        )
    actual_hash = _md5(path)
    if actual_hash != expected_hash:
        raise RuntimeError(
            f"Master 文件 MD5 不一致: {name}: {actual_hash}/{expected_hash}"
        )


def download_master_books(
    info: OfficialAssetInfo,
    output_dir: Path,
    languages: Iterable[str],
) -> Path:
    if not info.master_version:
        raise RuntimeError("官方资源接口缺少 masterVersion")
    output_dir.mkdir(parents=True, exist_ok=True)
    download_file(info.master_url("master-catalog"), output_dir / "master-catalog")
    catalog = _catalog_map(output_dir)
    names = [*CORE_BOOKS, *(LANGUAGE_BOOKS[language] for language in languages)]
    for name in names:
        metadata = catalog.get(name)
        if not isinstance(metadata, dict):
            raise RuntimeError(f"master-catalog 中缺少 {name}")
        target = output_dir / name
        download_file(info.master_url(name), target)
        _validate_book(target, name, metadata)
    return output_dir


def load_master_books(
    master_dir: Path, languages: Iterable[str]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    catalog = _catalog_map(master_dir)
    names = [*CORE_BOOKS, *(LANGUAGE_BOOKS[language] for language in languages)]
    tables: dict[str, Any] = {}
    selected_metadata: dict[str, dict[str, Any]] = {}
    for name in names:
        metadata = catalog.get(name)
        if not isinstance(metadata, dict):
            raise RuntimeError(f"master-catalog 中缺少 {name}")
        path = _book_path(master_dir, name)
        _validate_book(path, name, metadata)
        table = _unpack(path)
        if not isinstance(table, list):
            raise RuntimeError(f"MasterBook 不是列表: {name}")
        tables[name] = table
        selected_metadata[name] = {
            "md5": str(metadata["Hash"]).lower(),
            "size": int(metadata["Size"]),
        }
    return tables, selected_metadata


def load_current_master_skill_repository(
    output_dir: Path,
    master_version: str,
    languages: Iterable[str],
    as_of: datetime,
) -> dict[str, Any] | None:
    skills_dir = output_dir.resolve() / "skills"
    if not (skills_dir / GENERATED_MARKER).is_file():
        return None
    try:
        latest = json.loads((skills_dir / "latest.json").read_text(encoding="utf-8"))
        manifest = json.loads(
            (skills_dir / "manifest.json").read_text(encoding="utf-8")
        )
        requested_languages = tuple(dict.fromkeys(languages))
        if latest.get("source", {}).get("master_version") != master_version:
            return None
        if manifest.get("source", {}).get("master_version") != master_version:
            return None
        if tuple(latest.get("languages") or ()) != requested_languages:
            return None
        if tuple(manifest.get("languages") or ()) != requested_languages:
            return None
        next_release = latest.get("next_release_time_jst")
        if isinstance(next_release, str) and _parse_jst(next_release) <= as_of:
            return None
        files = manifest.get("files")
        if not isinstance(files, list) or manifest.get("file_count") != len(files):
            return None
        root = output_dir.resolve()
        for entry in files:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                return None
            target = (root / entry["path"]).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                return None
            if target.stat().st_size != entry.get("size"):
                return None
            if _sha256(target) != entry.get("sha256"):
                return None
        latest_characters = latest.get("latest_characters")
        if not isinstance(latest_characters, list) or not latest_characters:
            return None
        for character in latest_characters:
            if not isinstance(character, dict) or not isinstance(character.get("path"), str):
                return None
            target = (root / character["path"]).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                return None
        return latest
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _parse_jst(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"角色实装时间无效: {value!r}") from exc
    return moment.replace(tzinfo=JST) if moment.tzinfo is None else moment.astimezone(JST)


def _parse_as_of(value: str | None) -> datetime:
    if value is None:
        return datetime.now(JST)
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"--as-of 不是有效 ISO 时间: {value}") from exc
    return moment.replace(tzinfo=JST) if moment.tzinfo is None else moment.astimezone(JST)


def _text_map(rows: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get("StringKey")
        text = row.get("Text")
        if isinstance(key, str) and isinstance(text, str):
            result[key] = text
    return result


def _memo_sections(memo: Any) -> list[dict[str, str]]:
    if not isinstance(memo, str):
        return []
    pattern = re.compile(
        r"(?:^.*?:)?(?P<label>Lv\d+|Ex\d+)\s+(?P<text>.*?)"
        r"(?=(?:/\s*(?:Lv|Ex)\d+\s)|$)",
        re.DOTALL,
    )
    return [
        {"label": match.group("label"), "text": match.group("text").strip()}
        for match in pattern.finditer(memo)
    ]


def _localized_values(
    key: Any, text_maps: dict[str, dict[str, str]]
) -> dict[str, str | None]:
    return {
        language: texts.get(key) if isinstance(key, str) else None
        for language, texts in text_maps.items()
    }


def _skill_payload(
    row: dict[str, Any],
    kind: str,
    text_maps: dict[str, dict[str, str]],
) -> tuple[dict[str, Any], list[str]]:
    info_key = "ActiveSkillInfos" if kind == "active" else "PassiveSkillInfos"
    infos = row.get(info_key)
    if not isinstance(infos, list):
        infos = []
    sections = _memo_sections(row.get("Memo"))
    missing: set[str] = set()
    name_key = row.get("NameKey")
    names = _localized_values(name_key, text_maps)
    if isinstance(name_key, str) and any(value is None for value in names.values()):
        missing.add(name_key)
    levels: list[dict[str, Any]] = []
    for index, info in enumerate(infos):
        if not isinstance(info, dict):
            continue
        description_key = info.get("DescriptionKey")
        descriptions = _localized_values(description_key, text_maps)
        if isinstance(description_key, str) and any(
            value is None for value in descriptions.values()
        ):
            missing.add(description_key)
        source = sections[index] if index < len(sections) else None
        levels.append(
            {
                "order": info.get("OrderNumber"),
                "character_level": info.get("CharacterLevel"),
                "equipment_rarity_flags": info.get("EquipmentRarityFlags"),
                "blessing_item_id": info.get("BlessingItemId"),
                "description_key": description_key,
                "descriptions": descriptions,
                "source_memo_ja": source,
            }
        )
    payload = {
        "id": row.get("Id"),
        "kind": kind,
        "name_key": name_key,
        "names": names,
        "source_memo_ja": row.get("Memo"),
        "source_memo_sections_ja": sections,
        "levels": levels,
        "master_record": row,
    }
    if kind == "active":
        payload.update(
            {
                "conditions": row.get("ActiveSkillConditions"),
                "initial_cooldown": row.get("SkillInitCoolTime"),
                "max_cooldown": row.get("SkillMaxCoolTime"),
            }
        )
    return payload, sorted(missing)


def _number_text(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return str(value)
    if float(value).is_integer():
        return f"{int(value):,}"
    return f"{value:g}"


def _exclusive_parameter_payload(
    info: dict[str, Any], kind: str
) -> dict[str, Any]:
    type_key = "BaseParameterType" if kind == "base" else "BattleParameterType"
    parameter_type = info.get(type_key)
    codes = BASE_PARAMETER_CODES if kind == "base" else BATTLE_PARAMETER_CODES
    change_type = info.get("ChangeParameterType")
    raw_value = info.get("Value")
    if not isinstance(parameter_type, int) or parameter_type not in codes:
        raise RuntimeError(f"专武包含未知参数类型: {kind}/{parameter_type}")
    if change_type == 2 and isinstance(raw_value, (int, float)):
        value_text = f"+{_number_text(raw_value * 0.01)}%"
    elif change_type == 1:
        value_text = f"+{_number_text(raw_value)}"
    elif change_type == 3:
        value_text = f"+{_number_text(raw_value)}×角色等级"
    else:
        value_text = _number_text(raw_value)
    return {
        "kind": kind,
        "parameter_type": parameter_type,
        "parameter_code": codes[parameter_type],
        "change_parameter_type": change_type,
        "raw_value": raw_value,
        "value_text": value_text,
    }


def _exclusive_weapon_index(
    tables: dict[str, Any],
    text_maps: dict[str, dict[str, str]],
) -> dict[int, tuple[dict[str, Any], list[str]]]:
    effects_by_id = _safe_id_map(
        tables["EquipmentExclusiveEffectMB"], "EquipmentExclusiveEffectMB"
    )
    descriptions_by_id = _safe_id_map(
        tables["EquipmentExclusiveSkillDescriptionMB"],
        "EquipmentExclusiveSkillDescriptionMB",
    )
    equipment_by_character: dict[int, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for equipment in tables["EquipmentMB"]:
        if not isinstance(equipment, dict):
            raise RuntimeError("EquipmentMB 包含无效记录")
        if equipment.get("IsIgnore") is True:
            continue
        effect = effects_by_id.get(equipment.get("ExclusiveEffectId"))
        if effect is None or effect.get("IsIgnore") is True:
            continue
        character_id = effect.get("CharacterId")
        if not isinstance(character_id, int) or character_id <= 0:
            continue
        equipment_by_character.setdefault(character_id, []).append((equipment, effect))

    result: dict[int, tuple[dict[str, Any], list[str]]] = {}
    for character_id, candidates in equipment_by_character.items():
        # LR (512) contains all dedicated passive stats. Within the same rarity,
        # the lowest equipment level is the canonical first LR record.
        equipment, effect = max(
            candidates,
            key=lambda item: (
                int(item[0].get("RarityFlags") or 0),
                -int(item[0].get("EquipmentLv") or 0),
                -int(item[0].get("Id") or 0),
            ),
        )
        description = descriptions_by_id.get(
            equipment.get("EquipmentExclusiveSkillDescriptionId")
        )
        if description is None or description.get("IsIgnore") is True:
            raise RuntimeError(f"角色 {character_id} 的专武缺少技能描述记录")

        missing: set[str] = set()
        name_key = equipment.get("NameKey")
        names = _localized_values(name_key, text_maps)
        if isinstance(name_key, str) and any(value is None for value in names.values()):
            missing.add(name_key)
        skill_effects: list[dict[str, Any]] = []
        for order in range(1, 4):
            key = description.get(f"Description{order}Key")
            localized = _localized_values(key, text_maps)
            if isinstance(key, str) and any(value is None for value in localized.values()):
                missing.add(key)
            skill_effects.append(
                {
                    "order": order,
                    "equipment_rarity_flags": 64 << order,
                    "description_key": key,
                    "descriptions": localized,
                }
            )

        passive_effects = [
            _exclusive_parameter_payload(item, "base")
            for item in effect.get("BaseParameterChangeInfoList") or []
            if isinstance(item, dict)
        ]
        passive_effects.extend(
            _exclusive_parameter_payload(item, "battle")
            for item in effect.get("BattleParameterChangeInfoList") or []
            if isinstance(item, dict)
        )
        result[character_id] = (
            {
                "name_key": name_key,
                "names": names,
                "icon_id": equipment.get("IconId"),
                "rarity_flags": equipment.get("RarityFlags"),
                "equipment_id": equipment.get("Id"),
                "exclusive_effect_id": effect.get("Id"),
                "passive_effects": passive_effects,
                "skill_effects": skill_effects,
                "master_records": {
                    "equipment": equipment,
                    "exclusive_effect": effect,
                    "skill_description": description,
                },
            },
            sorted(missing),
        )
    return result


def _arcana_index(
    tables: dict[str, Any],
    text_maps: dict[str, dict[str, str]],
) -> dict[int, list[tuple[dict[str, Any], list[str]]]]:
    character_rows = {
        row["Id"]: row
        for row in tables["CharacterMB"]
        if isinstance(row, dict) and isinstance(row.get("Id"), int)
    }
    levels_by_collection: dict[int, list[dict[str, Any]]] = {}
    for level in tables["CharacterCollectionLevelMB"]:
        if not isinstance(level, dict):
            raise RuntimeError("CharacterCollectionLevelMB 包含无效记录")
        if level.get("IsIgnore") is True:
            continue
        collection_id = level.get("CollectionId")
        if isinstance(collection_id, int):
            levels_by_collection.setdefault(collection_id, []).append(level)

    result: dict[int, list[tuple[dict[str, Any], list[str]]]] = {}
    for collection in tables["CharacterCollectionMB"]:
        if not isinstance(collection, dict):
            raise RuntimeError("CharacterCollectionMB 包含无效记录")
        if collection.get("IsIgnore") is True:
            continue
        collection_id = collection.get("Id")
        if not isinstance(collection_id, int):
            raise RuntimeError("CharacterCollectionMB 包含无效 Id")
        name_key = collection.get("NameKey")
        names = _localized_values(name_key, text_maps)
        missing = (
            [name_key]
            if isinstance(name_key, str) and any(value is None for value in names.values())
            else []
        )
        level_payloads: list[dict[str, Any]] = []
        for level in sorted(
            levels_by_collection.get(collection_id, []),
            key=lambda item: int(item.get("CollectionLevel") or 0),
        ):
            effects = [
                _exclusive_parameter_payload(item, "base")
                for item in level.get("BaseParameterChangeInfos") or []
                if isinstance(item, dict)
            ]
            effects.extend(
                _exclusive_parameter_payload(item, "battle")
                for item in level.get("BattleParameterChangeInfos") or []
                if isinstance(item, dict)
            )
            level_payloads.append(
                {
                    "level": level.get("CollectionLevel"),
                    "character_rarity_flags": level.get("CharacterRarityFlags"),
                    "character_rarity_bonus": level.get("CharacterRarityBonus"),
                    "max_level_increase": level.get("MaxLevelIncreaseValue"),
                    "effects": effects,
                    "master_record": level,
                }
            )
        arcana = {
            "id": collection_id,
            "name_key": name_key,
            "names": names,
            "required_character_ids": collection.get("RequiredCharacterIds") or [],
            "required_characters": [
                {
                    "id": character_id,
                    "names": _localized_values(
                        character_rows.get(character_id, {}).get("NameKey"), text_maps
                    ),
                }
                for character_id in collection.get("RequiredCharacterIds") or []
                if isinstance(character_id, int)
            ],
            "required_party_level": collection.get("RequiredPartyLv"),
            "start_time_jst": collection.get("StartTimeFixJST"),
            "end_time_jst": collection.get("EndTimeFixJST"),
            "levels": level_payloads,
            "master_record": collection,
        }
        for character_id in collection.get("RequiredCharacterIds") or []:
            if isinstance(character_id, int) and character_id > 0:
                result.setdefault(character_id, []).append((arcana, missing))
    return result


def _character_payload(
    character: dict[str, Any],
    active_by_id: dict[int, dict[str, Any]],
    passive_by_id: dict[int, dict[str, Any]],
    text_maps: dict[str, dict[str, str]],
    exclusive_weapons: dict[int, tuple[dict[str, Any], list[str]]],
    arcanas: dict[int, list[tuple[dict[str, Any], list[str]]]],
    source: MasterSource,
    book_metadata: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    name_key = character.get("NameKey")
    subtitle_key = character.get("Name2Key")
    names = _localized_values(name_key, text_maps)
    subtitles = _localized_values(subtitle_key, text_maps)
    missing: set[str] = set()
    for key, values in ((name_key, names), (subtitle_key, subtitles)):
        if isinstance(key, str) and any(value is None for value in values.values()):
            missing.add(key)

    exclusive_weapon = None
    weapon_entry = exclusive_weapons.get(character.get("Id"))
    if weapon_entry is not None:
        exclusive_weapon, weapon_missing = weapon_entry
        missing.update(weapon_missing)
    character_arcanas: list[dict[str, Any]] = []
    for arcana, arcana_missing in arcanas.get(character.get("Id"), []):
        character_arcanas.append(arcana)
        missing.update(arcana_missing)

    active_skills: list[dict[str, Any]] = []
    for skill_id in character.get("ActiveSkillIds") or []:
        row = active_by_id.get(skill_id)
        if row is None:
            raise RuntimeError(f"角色 {character.get('Id')} 缺少主动技能 {skill_id}")
        payload, skill_missing = _skill_payload(row, "active", text_maps)
        active_skills.append(payload)
        missing.update(skill_missing)

    passive_skills: list[dict[str, Any]] = []
    for skill_id in character.get("PassiveSkillIds") or []:
        row = passive_by_id.get(skill_id)
        if row is None:
            raise RuntimeError(f"角色 {character.get('Id')} 缺少被动技能 {skill_id}")
        payload, skill_missing = _skill_payload(row, "passive", text_maps)
        passive_skills.append(payload)
        missing.update(skill_missing)

    return {
        "schema_version": SCHEMA_VERSION,
        "game": "MementoMori",
        "source": {
            "app_version": source.app_version,
            "asset_version": source.asset_version,
            "master_version": source.master_version,
            "generated_at": source.generated_at,
            "books": book_metadata,
        },
        "character": {
            "id": character.get("Id"),
            "memo": character.get("Memo"),
            "start_time_jst": character.get("StartTimeFixJST"),
            "end_time_jst": character.get("EndTimeFixJST"),
            "name_key": name_key,
            "names": names,
            "subtitle_key": subtitle_key,
            "subtitles": subtitles,
            "character_type": character.get("CharacterType"),
            "element_type": character.get("ElementType"),
            "job_flags": character.get("JobFlags"),
            "speed": (character.get("InitialBattleParameter") or {}).get("Speed"),
            "master_record": character,
        },
        "localization_complete": not missing,
        "missing_localization_keys": sorted(missing),
        "exclusive_weapon": exclusive_weapon,
        "arcanas": character_arcanas,
        "active_skills": active_skills,
        "passive_skills": passive_skills,
    }


def _safe_id_map(rows: list[dict[str, Any]], name: str) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("Id"), int):
            raise RuntimeError(f"{name} 包含无效记录")
        row_id = row["Id"]
        if row_id in result:
            raise RuntimeError(f"{name} 包含重复 Id: {row_id}")
        result[row_id] = row
    return result


def _install_staged_skills(staging: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    current = output_dir / "skills"
    marker = current / GENERATED_MARKER
    if current.exists() and not marker.is_file():
        raise RuntimeError(f"拒绝替换非本工具生成的目录，缺少 {marker}")
    backup = output_dir.parent / f".{output_dir.name}-skills-backup"
    if backup.exists():
        backup_marker = backup / GENERATED_MARKER
        if not backup_marker.is_file():
            raise RuntimeError(f"拒绝清理非本工具生成的备份目录，缺少 {backup_marker}")
        shutil.rmtree(backup)
    if current.exists():
        try:
            current.replace(backup)
        except PermissionError:
            # Windows indexers and previewers may hold a directory handle that
            # prevents renaming the directory while still allowing its files to
            # be updated. The ownership marker keeps this fallback scoped to our
            # generated tree, and stale generated files are removed explicitly.
            staged_skills = staging / "skills"
            staged_relative = {
                path.relative_to(staged_skills)
                for path in staged_skills.rglob("*")
                if path.is_file()
            }
            for path in current.rglob("*"):
                if path.is_file() and path.relative_to(current) not in staged_relative:
                    path.unlink()
            shutil.copytree(staged_skills, current, dirs_exist_ok=True)
            for path in sorted(
                (item for item in current.rglob("*") if item.is_dir()),
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                try:
                    path.rmdir()
                except OSError:
                    pass
            return
    try:
        (staging / "skills").replace(current)
    except Exception:
        if current.exists():
            shutil.rmtree(current)
        if backup.exists():
            backup.replace(current)
        raise
    finally:
        if backup.exists():
            shutil.rmtree(backup)


def build_master_skill_repository(
    master_dir: Path,
    output_dir: Path,
    repository: str,
    source: MasterSource,
    languages: Iterable[str] = ("zh-CN",),
    as_of: datetime | None = None,
) -> dict[str, Any]:
    languages = tuple(dict.fromkeys(languages))
    if not languages:
        raise ValueError("至少需要一种语言")
    unsupported = [language for language in languages if language not in LANGUAGE_BOOKS]
    if unsupported:
        raise ValueError(f"不支持的语言: {', '.join(unsupported)}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository 必须使用 owner/name 格式")
    as_of = as_of or datetime.now(JST)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=JST)
    else:
        as_of = as_of.astimezone(JST)

    tables, book_metadata = load_master_books(master_dir, languages)
    text_maps = {
        language: _text_map(tables[LANGUAGE_BOOKS[language]])
        for language in languages
    }
    active_by_id = _safe_id_map(tables["ActiveSkillMB"], "ActiveSkillMB")
    passive_by_id = _safe_id_map(tables["PassiveSkillMB"], "PassiveSkillMB")
    exclusive_weapons = _exclusive_weapon_index(tables, text_maps)
    arcanas = _arcana_index(tables, text_maps)
    released: list[tuple[datetime, dict[str, Any]]] = []
    future_release_times: list[datetime] = []
    for character in tables["CharacterMB"]:
        if not isinstance(character, dict) or not isinstance(character.get("Id"), int):
            raise RuntimeError("CharacterMB 包含无效记录")
        if character.get("IsIgnore") is True:
            continue
        start = _parse_jst(character.get("StartTimeFixJST"))
        if start <= as_of:
            released.append((start, character))
        else:
            future_release_times.append(start)
    if not released:
        raise RuntimeError("按实装时间过滤后没有可发布角色")
    released.sort(key=lambda item: (item[0], item[1]["Id"]))
    latest_start = released[-1][0]

    raw_base = f"https://raw.githubusercontent.com/{repository}/main"
    output_dir = output_dir.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".mmtm-master-skills-", dir=output_dir.parent
    ) as temporary:
        staging = Path(temporary)
        skills_dir = staging / "skills"
        character_dir = skills_dir / "characters"
        character_dir.mkdir(parents=True)
        (skills_dir / GENERATED_MARKER).write_text(
            "This directory is generated. Do not edit files manually.\n",
            encoding="utf-8",
        )
        summaries: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        latest_characters: list[dict[str, Any]] = []
        for start, character in released:
            payload = _character_payload(
                character,
                active_by_id,
                passive_by_id,
                text_maps,
                exclusive_weapons,
                arcanas,
                source,
                book_metadata,
            )
            character_id = character["Id"]
            relative = Path("characters") / f"{character_id:06d}.json"
            target = skills_dir / relative
            _json_dump(target, payload)
            names = payload["character"]["names"]
            display_name = next((value for value in names.values() if value), None)
            summary = {
                "id": character_id,
                "memo": character.get("Memo"),
                "display_name": display_name or character.get("Memo"),
                "names": names,
                "subtitles": payload["character"]["subtitles"],
                "start_time_jst": character.get("StartTimeFixJST"),
                "localization_complete": payload["localization_complete"],
                "path": f"skills/{relative.as_posix()}",
                "url": f"{raw_base}/skills/{relative.as_posix()}",
                "sha256": _sha256(target),
                "size": target.stat().st_size,
            }
            summaries.append(summary)
            if start == latest_start:
                latest_characters.append(summary)
            files.append(
                {
                    "path": f"skills/{relative.as_posix()}",
                    "sha256": summary["sha256"],
                    "size": summary["size"],
                }
            )

        source_payload = {
            "app_version": source.app_version,
            "asset_version": source.asset_version,
            "master_version": source.master_version,
            "generated_at": source.generated_at,
            "books": book_metadata,
        }
        index = {
            "schema_version": SCHEMA_VERSION,
            "game": "MementoMori",
            "source": source_payload,
            "languages": list(languages),
            "selection": "StartTimeFixJST <= current JST; sorted by release time then Id",
            "character_count": len(summaries),
            "characters": summaries,
        }
        _json_dump(skills_dir / "index.json", index)
        files.append(
            {
                "path": "skills/index.json",
                "sha256": _sha256(skills_dir / "index.json"),
                "size": (skills_dir / "index.json").stat().st_size,
            }
        )
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "game": "MementoMori",
            "source": source_payload,
            "languages": list(languages),
            "character_count": len(summaries),
            "file_count": len(files),
            "files": sorted(files, key=lambda item: item["path"]),
        }
        latest = {
            "schema_version": SCHEMA_VERSION,
            "game": "MementoMori",
            "source": source_payload,
            "languages": list(languages),
            "latest_release_time_jst": latest_start.strftime("%Y-%m-%d %H:%M:%S"),
            "next_release_time_jst": (
                min(future_release_times).strftime("%Y-%m-%d %H:%M:%S")
                if future_release_times
                else None
            ),
            "latest_characters": latest_characters,
            "index_url": f"{raw_base}/skills/index.json",
            "manifest_url": f"{raw_base}/skills/manifest.json",
        }
        _json_dump(skills_dir / "manifest.json", manifest)
        _json_dump(skills_dir / "latest.json", latest)
        _install_staged_skills(staging, output_dir)
    return latest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从官方 Master 数据校验并导出角色技能文本。"
    )
    parser.add_argument("--auto-update", action="store_true", help="下载官方最新 Master")
    parser.add_argument("--master-dir", type=Path, help="已下载的 MasterBook 目录")
    parser.add_argument("--output", type=Path, default=Path("skill_dist"))
    parser.add_argument(
        "--repository",
        default="GuangShiX/mmtm-assets-fallback",
        help="GitHub owner/name，用于生成稳定 Raw URL",
    )
    parser.add_argument(
        "--language",
        action="append",
        dest="languages",
        choices=sorted(LANGUAGE_BOOKS),
        help="要导出的本地化；可重复，默认 zh-CN",
    )
    parser.add_argument("--app-version", default="", help="本地 Master 的应用版本")
    parser.add_argument("--asset-version", default="", help="本地 Master 的资源版本")
    parser.add_argument("--master-version", default="", help="本地 Master 的版本")
    parser.add_argument("--as-of", help="按指定 JST/ISO 时间过滤未来角色，默认当前时间")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    languages = tuple(args.languages or ("zh-CN",))
    try:
        if args.auto_update == bool(args.master_dir):
            raise ValueError("必须且只能指定 --auto-update 或 --master-dir")
        as_of = _parse_as_of(args.as_of)
        if args.auto_update:
            info = get_official_asset_info(args.app_version or None)
            source = MasterSource(
                info.app_version, info.asset_version, info.master_version
            )
            latest = load_current_master_skill_repository(
                args.output,
                source.master_version,
                languages,
                as_of,
            )
            if latest is not None:
                latest_ids = [item["id"] for item in latest["latest_characters"]]
                print(
                    f"官方 Master 版本未变化: masterVersion={source.master_version}, "
                    f"latest={latest_ids}"
                )
                return 0
            with tempfile.TemporaryDirectory(prefix="mmtm-master-download-") as temporary:
                master_dir = download_master_books(info, Path(temporary), languages)
                latest = build_master_skill_repository(
                    master_dir,
                    args.output,
                    args.repository,
                    source,
                    languages,
                    as_of,
                )
        else:
            if not args.master_version:
                raise ValueError("--master-dir 模式必须提供 --master-version")
            source = MasterSource(
                args.app_version, args.asset_version, args.master_version
            )
            latest = build_master_skill_repository(
                args.master_dir,
                args.output,
                args.repository,
                source,
                languages,
                as_of,
            )
        latest_ids = [item["id"] for item in latest["latest_characters"]]
        print(
            f"Master 技能已更新: masterVersion={source.master_version}, "
            f"latest={latest_ids}, output={(args.output / 'skills').resolve()}"
        )
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"生成 Master 技能失败: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
