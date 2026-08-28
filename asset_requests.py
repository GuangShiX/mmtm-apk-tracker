"""Curated, consumer-driven requests for official MementoMori art assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import msgpack
from PIL import Image

ROOT = Path(__file__).parent
DEFAULT_REGISTRY_PATH = ROOT / "config" / "official_asset_requests.json"
DEFAULT_PLAYER_AVATAR_INVENTORY_PATH = ROOT / "config" / "player_avatar_inventory.json"
PLAYER_AVATAR_MASTER_BOOKS = ("CharacterMB", "SpecialIconItemMB")
SUPPORTED_CATEGORIES = {
    "characters",
    "enemies",
    "equipment",
    "items",
    "spheres",
    "ui",
}
SUPPORTED_DELIVERY = {"package", "addressables", "package-or-addressables"}


@dataclass(frozen=True)
class AssetRequest:
    category: str
    name: str
    kind: str
    delivery: str
    addressable_key: str | None
    groups: tuple[str, ...]
    consumers: tuple[str, ...]
    reason: str
    metadata: dict[str, Any]

    @property
    def key(self) -> tuple[str, str]:
        return self.category, self.name


@dataclass(frozen=True)
class AssetRequestRegistry:
    path: Path
    fingerprint: str
    assets: tuple[AssetRequest, ...]
    group_names: dict[str, frozenset[str]]
    inventory_sources: dict[str, dict[str, Any]]

    @property
    def required_keys(self) -> frozenset[tuple[str, str]]:
        return frozenset(asset.key for asset in self.assets)

    @property
    def names_by_case(self) -> dict[str, AssetRequest]:
        return {asset.name.casefold(): asset for asset in self.assets}

    @property
    def catalog_targets(self) -> dict[str, tuple[str, str]]:
        return {
            asset.addressable_key.casefold(): asset.key
            for asset in self.assets
            if asset.addressable_key
        }


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"官方资产需求清单缺少 {label}")
    return value.strip()


def _validate_asset_name(name: str) -> None:
    if Path(name).name != name or not name.lower().endswith(".png"):
        raise RuntimeError(f"官方资产名称非法: {name}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.png", name):
        raise RuntimeError(f"官方资产名称包含不支持的字符: {name}")


def _validate_special_player_icon(
    name: str, addressable_key: str | None, metadata: dict[str, Any]
) -> None:
    item_id = metadata.get("special_icon_item_id")
    character_id = metadata.get("character_id")
    icon_id = metadata.get("icon_id")
    if not all(isinstance(value, int) and value > 0 for value in (item_id, character_id, icon_id)):
        raise RuntimeError(f"差分头像缺少有效 Master 映射: {name}")
    expected_name = f"CHR_{character_id:06d}_00_em_{icon_id:03d}.png"
    expected_key = (
        f"CharacterIcon/CHR_{character_id:06d}/"
        f"CHR_{character_id:06d}_00_em_{icon_id:03d}"
    )
    if name != expected_name:
        raise RuntimeError(f"差分头像名称与 Master 映射不一致: {name} != {expected_name}")
    if addressable_key != expected_key:
        raise RuntimeError(
            f"差分头像 Addressables key 不一致: {addressable_key} != {expected_key}"
        )


def _validate_standard_player_icon(
    name: str, addressable_key: str | None, metadata: dict[str, Any]
) -> None:
    character_id = metadata.get("character_id")
    if not isinstance(character_id, int) or character_id <= 0:
        raise RuntimeError(f"普通头像缺少有效 CharacterMB 映射: {name}")
    expected_name = f"CHR_{character_id:06d}_00_s.png"
    expected_key = f"CharacterIcon/CHR_{character_id:06d}/{expected_name[:-4]}"
    if name != expected_name:
        raise RuntimeError(f"普通头像名称与 Master 映射不一致: {name} != {expected_name}")
    if addressable_key != expected_key:
        raise RuntimeError(
            f"普通头像 Addressables key 不一致: {addressable_key} != {expected_key}"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _master_book_path(master_dir: Path, name: str) -> Path:
    for candidate in (master_dir / name, master_dir / f"{name}.msgpack"):
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"Master 文件缺失: {name}")


def _unpack_master_rows(path: Path, name: str) -> list[dict[str, Any]]:
    try:
        value = msgpack.unpackb(path.read_bytes(), raw=False, timestamp=3)
    except (OSError, ValueError, msgpack.ExtraData) as exc:
        raise RuntimeError(f"无法解包 Master 文件: {path}") from exc
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise RuntimeError(f"MasterBook 不是对象列表: {name}")
    return value


def _player_avatar_inventory_from_master(
    master_dir: Path, master_version: str
) -> dict[str, Any]:
    master_version = _require_text(master_version, "master_version")
    character_path = _master_book_path(master_dir, "CharacterMB")
    special_path = _master_book_path(master_dir, "SpecialIconItemMB")
    character_rows = _unpack_master_rows(character_path, "CharacterMB")
    special_rows = _unpack_master_rows(special_path, "SpecialIconItemMB")

    character_ids: list[int] = []
    for row in character_rows:
        if row.get("IsIgnore"):
            continue
        character_id = row.get("Id")
        if not isinstance(character_id, int) or character_id <= 0:
            raise RuntimeError("CharacterMB 包含无效头像记录")
        character_ids.append(character_id)
    if len(character_ids) != len(set(character_ids)):
        raise RuntimeError("CharacterMB 包含重复角色 ID")

    special_icons: list[list[int]] = []
    special_item_ids: set[int] = set()
    special_names: set[str] = set()
    for row in special_rows:
        if row.get("IsIgnore"):
            continue
        values = (row.get("Id"), row.get("CharacterId"), row.get("IconId"))
        if not all(isinstance(value, int) and value > 0 for value in values):
            raise RuntimeError("SpecialIconItemMB 包含无效头像记录")
        item_id, character_id, icon_id = values
        name = f"CHR_{character_id:06d}_00_em_{icon_id:03d}.png"
        if item_id in special_item_ids or name.casefold() in special_names:
            raise RuntimeError("SpecialIconItemMB 包含重复道具 ID 或头像文件")
        special_item_ids.add(item_id)
        special_names.add(name.casefold())
        special_icons.append([item_id, character_id, icon_id])

    character_ids.sort()
    special_icons.sort(key=lambda values: values[0])
    generated_at = ""
    if master_version.isdigit():
        generated_at = datetime.fromtimestamp(
            int(master_version) / 1000, timezone.utc
        ).isoformat()
    return {
        "schema_version": 1,
        "game": "MementoMori",
        "master_version": master_version,
        "generated_at_utc": generated_at,
        "sources": {
            "CharacterMB": {
                "sha256": _sha256(character_path),
                "record_count": len(character_ids),
            },
            "SpecialIconItemMB": {
                "sha256": _sha256(special_path),
                "record_count": len(special_icons),
            },
        },
        "standard_player_icons": character_ids,
        "special_player_icons_record_format": [
            "special_icon_item_id",
            "character_id",
            "icon_id",
        ],
        "special_player_icons": special_icons,
    }


def _load_player_avatar_inventory(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取玩家头像库存: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise RuntimeError("玩家头像库存 schema_version 必须为 1")
    master_version = _require_text(raw.get("master_version"), "inventory.master_version")
    sources = raw.get("sources")
    if not isinstance(sources, dict):
        raise RuntimeError("玩家头像库存缺少 sources")
    for book in PLAYER_AVATAR_MASTER_BOOKS:
        source = sources.get(book)
        if not isinstance(source, dict):
            raise RuntimeError(f"玩家头像库存缺少来源: {book}")
        digest = source.get("sha256")
        count = source.get("record_count")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise RuntimeError(f"玩家头像库存来源 SHA-256 无效: {book}")
        if not isinstance(count, int) or count <= 0:
            raise RuntimeError(f"玩家头像库存来源记录数无效: {book}")

    standard = raw.get("standard_player_icons")
    special = raw.get("special_player_icons")
    if not isinstance(standard, list) or not standard:
        raise RuntimeError("玩家普通头像库存不能为空")
    if not all(isinstance(value, int) and value > 0 for value in standard):
        raise RuntimeError("玩家普通头像库存包含无效角色 ID")
    if standard != sorted(set(standard)):
        raise RuntimeError("玩家普通头像库存必须按 ID 排序且无重复")
    if sources["CharacterMB"]["record_count"] != len(standard):
        raise RuntimeError("CharacterMB 来源记录数与普通头像库存不一致")

    if not isinstance(special, list) or not special:
        raise RuntimeError("玩家差分头像库存不能为空")
    special_rows: list[tuple[int, int, int]] = []
    for row in special:
        if (
            not isinstance(row, list)
            or len(row) != 3
            or not all(isinstance(value, int) and value > 0 for value in row)
        ):
            raise RuntimeError("玩家差分头像库存包含无效记录")
        special_rows.append((row[0], row[1], row[2]))
    if special_rows != sorted(special_rows, key=lambda values: values[0]):
        raise RuntimeError("玩家差分头像库存必须按道具 ID 排序")
    if len({row[0] for row in special_rows}) != len(special_rows):
        raise RuntimeError("玩家差分头像库存包含重复道具 ID")
    special_names = {
        f"CHR_{character_id:06d}_00_em_{icon_id:03d}.png".casefold()
        for _, character_id, icon_id in special_rows
    }
    if len(special_names) != len(special_rows):
        raise RuntimeError("玩家差分头像库存包含重复头像文件")
    if sources["SpecialIconItemMB"]["record_count"] != len(special_rows):
        raise RuntimeError("SpecialIconItemMB 来源记录数与差分头像库存不一致")
    return raw


def _inventory_items(
    inventory: dict[str, Any], section: str
) -> list[dict[str, Any]]:
    master_version = inventory["master_version"]
    sources = inventory["sources"]
    if section == "standard_player_icons":
        evidence = {
            "master_book": "CharacterMB",
            "master_version": master_version,
            "master_book_sha256": sources["CharacterMB"]["sha256"],
        }
        return [
            {
                "name": f"CHR_{character_id:06d}_00_s.png",
                "addressable_key": (
                    f"CharacterIcon/CHR_{character_id:06d}/"
                    f"CHR_{character_id:06d}_00_s"
                ),
                "character_id": character_id,
                "inventory_evidence": evidence,
            }
            for character_id in inventory[section]
        ]
    if section == "special_player_icons":
        evidence = {
            "master_book": "SpecialIconItemMB",
            "master_version": master_version,
            "master_book_sha256": sources["SpecialIconItemMB"]["sha256"],
        }
        return [
            {
                "name": f"CHR_{character_id:06d}_00_em_{icon_id:03d}.png",
                "addressable_key": (
                    f"CharacterIcon/CHR_{character_id:06d}/"
                    f"CHR_{character_id:06d}_00_em_{icon_id:03d}"
                ),
                "special_icon_item_id": item_id,
                "character_id": character_id,
                "icon_id": icon_id,
                "inventory_evidence": evidence,
            }
            for item_id, character_id, icon_id in inventory[section]
        ]
    raise RuntimeError(f"不支持的玩家头像库存分区: {section}")


def _download_latest_player_avatar_master(output_dir: Path) -> str:
    from asset_cdn import download_file, get_official_asset_info
    from master import _catalog_map, _validate_book

    info = get_official_asset_info()
    if not info.master_version:
        raise RuntimeError("官方资源接口缺少 masterVersion")
    output_dir.mkdir(parents=True, exist_ok=True)
    download_file(info.master_url("master-catalog"), output_dir / "master-catalog")
    catalog = _catalog_map(output_dir)
    for name in PLAYER_AVATAR_MASTER_BOOKS:
        metadata = catalog.get(name)
        if not isinstance(metadata, dict):
            raise RuntimeError(f"master-catalog 中缺少 {name}")
        target = output_dir / name
        download_file(info.master_url(name), target)
        _validate_book(target, name, metadata)
    return info.master_version


def sync_player_avatar_inventory(
    master_dir: Path,
    master_version: str,
    output: Path = DEFAULT_PLAYER_AVATAR_INVENTORY_PATH,
) -> dict[str, Any]:
    output = output.resolve()
    inventory = _player_avatar_inventory_from_master(master_dir.resolve(), master_version)
    status = "updated"
    if output.is_file():
        current = _load_player_avatar_inventory(output)
        comparable_keys = (
            "sources",
            "standard_player_icons",
            "special_player_icons_record_format",
            "special_player_icons",
        )
        if all(current.get(key) == inventory.get(key) for key in comparable_keys):
            inventory = current
            status = "current"
    if status == "updated":
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(inventory, ensure_ascii=False, indent=2) + "\n"
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=output.parent,
            delete=False,
        ) as stream:
            stream.write(payload)
            temporary = Path(stream.name)
        temporary.replace(output)
        load_asset_request_registry.cache_clear()
    return {
        "status": status,
        "path": str(output),
        "master_version": inventory["master_version"],
        "checked_master_version": master_version,
        "standard_player_icon_count": len(inventory["standard_player_icons"]),
        "special_player_icon_count": len(inventory["special_player_icons"]),
        "sources": inventory["sources"],
    }


def verify_player_avatar_inventory(
    master_dir: Path,
    master_version: str,
    inventory_path: Path = DEFAULT_PLAYER_AVATAR_INVENTORY_PATH,
) -> dict[str, Any]:
    inventory_path = inventory_path.resolve()
    actual = _load_player_avatar_inventory(inventory_path)
    expected = _player_avatar_inventory_from_master(master_dir.resolve(), master_version)
    comparable_keys = (
        "sources",
        "standard_player_icons",
        "special_player_icons_record_format",
        "special_player_icons",
    )
    if any(actual.get(key) != expected.get(key) for key in comparable_keys):
        raise RuntimeError(
            "玩家头像库存与当前 CharacterMB/SpecialIconItemMB 不一致；"
            "请运行 sync-player-avatar-inventory"
        )
    return {
        "status": "accepted",
        "path": str(inventory_path),
        "master_version": actual["master_version"],
        "checked_master_version": master_version,
        "standard_player_icon_count": len(actual["standard_player_icons"]),
        "special_player_icon_count": len(actual["special_player_icons"]),
        "sources": actual["sources"],
    }


@lru_cache(maxsize=4)
def load_asset_request_registry(
    path: Path = DEFAULT_REGISTRY_PATH,
) -> AssetRequestRegistry:
    path = path.resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取官方资产需求清单: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise RuntimeError("官方资产需求清单 schema_version 必须为 1")
    groups = raw.get("groups")
    if not isinstance(groups, list) or not groups:
        raise RuntimeError("官方资产需求清单 groups 不能为空")

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    names_by_case: dict[str, tuple[str, str]] = {}
    group_names: dict[str, frozenset[str]] = {}
    seen_group_ids: set[str] = set()
    loaded_inventories: dict[Path, dict[str, Any]] = {}

    for group in groups:
        if not isinstance(group, dict):
            raise RuntimeError("官方资产需求组必须是对象")
        group_id = _require_text(group.get("id"), "group.id")
        if group_id in seen_group_ids:
            raise RuntimeError(f"官方资产需求组重复: {group_id}")
        seen_group_ids.add(group_id)
        category = _require_text(group.get("category"), f"{group_id}.category")
        kind = _require_text(group.get("kind"), f"{group_id}.kind")
        delivery = _require_text(group.get("delivery"), f"{group_id}.delivery")
        reason = _require_text(group.get("reason"), f"{group_id}.reason")
        if category not in SUPPORTED_CATEGORIES:
            raise RuntimeError(f"官方资产需求分类不支持: {group_id}/{category}")
        if delivery not in SUPPORTED_DELIVERY:
            raise RuntimeError(f"官方资产交付方式不支持: {group_id}/{delivery}")
        consumers_raw = group.get("consumers")
        if not isinstance(consumers_raw, list) or not consumers_raw:
            raise RuntimeError(f"官方资产需求组缺少消费者: {group_id}")
        consumers = tuple(
            sorted({_require_text(item, f"{group_id}.consumers") for item in consumers_raw})
        )
        group_evidence = group.get("evidence", {})
        if not isinstance(group_evidence, dict):
            raise RuntimeError(f"官方资产需求组 evidence 必须是对象: {group_id}")

        items: list[dict[str, Any]] = []
        names = group.get("names", [])
        assets = group.get("assets", [])
        if not isinstance(names, list) or not isinstance(assets, list):
            raise RuntimeError(f"官方资产需求组 names/assets 必须是数组: {group_id}")
        items.extend({"name": name} for name in names)
        for item in assets:
            if not isinstance(item, dict):
                raise RuntimeError(f"官方资产需求项必须是对象: {group_id}")
            items.append(dict(item))
        inventory_ref = group.get("inventory")
        if inventory_ref is not None:
            if not isinstance(inventory_ref, dict):
                raise RuntimeError(f"官方资产需求组 inventory 必须是对象: {group_id}")
            inventory_file = _require_text(
                inventory_ref.get("file"), f"{group_id}.inventory.file"
            )
            inventory_section = _require_text(
                inventory_ref.get("section"), f"{group_id}.inventory.section"
            )
            if Path(inventory_file).name != inventory_file:
                raise RuntimeError(f"玩家头像库存文件名非法: {inventory_file}")
            inventory_path = (path.parent / inventory_file).resolve()
            inventory = loaded_inventories.get(inventory_path)
            if inventory is None:
                inventory = _load_player_avatar_inventory(inventory_path)
                loaded_inventories[inventory_path] = inventory
            items.extend(_inventory_items(inventory, inventory_section))
        if not items:
            raise RuntimeError(f"官方资产需求组不能为空: {group_id}")

        member_names: set[str] = set()
        for item in items:
            name = _require_text(item.get("name"), f"{group_id}.asset.name")
            _validate_asset_name(name)
            case_key = name.casefold()
            existing_case = names_by_case.get(case_key)
            if existing_case and existing_case != (category, name):
                raise RuntimeError(f"官方资产名称大小写或分类冲突: {name}")
            names_by_case[case_key] = (category, name)
            addressable_key = item.get("addressable_key")
            if addressable_key is not None:
                addressable_key = _require_text(
                    addressable_key, f"{group_id}.{name}.addressable_key"
                )
            metadata = {
                key: value
                for key, value in item.items()
                if key not in {"name", "addressable_key"}
            }
            if kind == "special-player-icon":
                _validate_special_player_icon(name, addressable_key, metadata)
            elif kind == "standard-player-icon":
                _validate_standard_player_icon(name, addressable_key, metadata)

            key = (category, name)
            current = merged.get(key)
            signature = (kind, delivery, addressable_key, metadata)
            if current is None:
                merged[key] = {
                    "category": category,
                    "name": name,
                    "kind": kind,
                    "delivery": delivery,
                    "addressable_key": addressable_key,
                    "groups": {group_id},
                    "consumers": set(consumers),
                    "reasons": [reason],
                    "metadata": metadata,
                    "signature": signature,
                    "evidence": dict(group_evidence),
                }
            else:
                if current["signature"] != signature:
                    raise RuntimeError(f"官方资产重复登记内容不一致: {category}/{name}")
                current["groups"].add(group_id)
                current["consumers"].update(consumers)
                if reason not in current["reasons"]:
                    current["reasons"].append(reason)
                for evidence_key, evidence_value in group_evidence.items():
                    previous = current["evidence"].get(evidence_key, evidence_value)
                    if previous != evidence_value:
                        raise RuntimeError(
                            f"官方资产重复登记证据冲突: {category}/{name}/{evidence_key}"
                        )
                    current["evidence"][evidence_key] = evidence_value
            member_names.add(name)
        group_names[group_id] = frozenset(member_names)

    requests: list[AssetRequest] = []
    for current in merged.values():
        metadata = dict(current["metadata"])
        if current["evidence"]:
            metadata["evidence"] = dict(current["evidence"])
        requests.append(
            AssetRequest(
                category=current["category"],
                name=current["name"],
                kind=current["kind"],
                delivery=current["delivery"],
                addressable_key=current["addressable_key"],
                groups=tuple(sorted(current["groups"])),
                consumers=tuple(sorted(current["consumers"])),
                reason=" ".join(current["reasons"]),
                metadata=metadata,
            )
        )
    requests.sort(key=lambda item: item.key)
    canonical_payload = {
        "registry": raw,
        "inventories": {
            str(inventory_path.relative_to(path.parent)).replace("\\", "/"): inventory
            for inventory_path, inventory in sorted(
                loaded_inventories.items(), key=lambda item: str(item[0])
            )
        },
    }
    canonical = json.dumps(
        canonical_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return AssetRequestRegistry(
        path=path,
        fingerprint=fingerprint,
        assets=tuple(requests),
        group_names=group_names,
        inventory_sources={
            str(inventory_path): {
                "master_version": inventory["master_version"],
                "sources": inventory["sources"],
            }
            for inventory_path, inventory in sorted(
                loaded_inventories.items(), key=lambda item: str(item[0])
            )
        },
    )


def requested_asset_for_name(name: str) -> AssetRequest | None:
    return load_asset_request_registry().names_by_case.get(name.casefold())


def requested_catalog_target(key: str) -> tuple[str, str] | None:
    return load_asset_request_registry().catalog_targets.get(key.casefold())


def requested_names_for_group(group_id: str) -> frozenset[str]:
    registry = load_asset_request_registry()
    try:
        return registry.group_names[group_id]
    except KeyError as exc:
        raise RuntimeError(f"未知官方资产需求组: {group_id}") from exc


def verify_requested_repository(repository: Path) -> dict[str, Any]:
    repository = repository.resolve()
    manifest_path = repository / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取资产仓库清单: {manifest_path}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("assets"), list):
        raise RuntimeError(f"资产仓库清单格式无效: {manifest_path}")

    registry = load_asset_request_registry()
    if manifest.get("request_registry_sha256") != registry.fingerprint:
        raise RuntimeError("资产仓库需求清单指纹不是当前版本")
    if manifest.get("request_registry_status") != "complete":
        raise RuntimeError("资产仓库需求清单状态不是 complete")

    entries: dict[tuple[str, str], dict[str, Any]] = {}
    total_verified_bytes = 0
    for entry in manifest["assets"]:
        if not isinstance(entry, dict):
            raise RuntimeError("资产仓库清单包含非对象条目")
        category = _require_text(entry.get("category"), "manifest.category")
        name = _require_text(entry.get("name"), "manifest.name")
        key = (category, name)
        if key in entries:
            raise RuntimeError(f"资产仓库清单重复: {category}/{name}")
        expected_path = (Path("assets") / category / name).as_posix()
        if entry.get("path") != expected_path:
            raise RuntimeError(f"资产仓库路径不规范: {entry.get('path')} != {expected_path}")
        path = (repository / expected_path).resolve()
        try:
            path.relative_to(repository)
        except ValueError as exc:
            raise RuntimeError(f"资产仓库路径越界: {expected_path}") from exc
        if not path.is_file():
            raise RuntimeError(f"资产仓库文件缺失: {expected_path}")
        content = path.read_bytes()
        size = len(content)
        digest = hashlib.sha256(content).hexdigest()
        if entry.get("size") != size or entry.get("sha256") != digest:
            raise RuntimeError(f"资产仓库文件哈希或大小不匹配: {expected_path}")
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                width, height = image.size
                image_format = image.format
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"资产仓库文件不是有效图片: {expected_path}") from exc
        if image_format != "PNG" or width <= 0 or height <= 0:
            raise RuntimeError(f"资产仓库 PNG 尺寸无效: {expected_path}")
        entries[key] = {**entry, "width": width, "height": height}
        total_verified_bytes += size

    missing = sorted(registry.required_keys - set(entries))
    if missing:
        labels = ", ".join(f"{category}/{name}" for category, name in missing)
        raise RuntimeError(f"资产仓库缺少已登记官方资产: {labels}")

    inventory_path = registry.path.parent / "player_avatar_inventory.json"
    player_avatar_inventory = _load_player_avatar_inventory(inventory_path)
    declared_special_names = {
        item["name"].casefold(): item
        for item in _inventory_items(player_avatar_inventory, "special_player_icons")
    }
    player_avatar_dimensions: dict[str, list[int]] = {}
    special_dimensions: dict[str, list[int]] = {}
    for request in registry.assets:
        entry = entries[request.key]
        if not entry.get("source_resource_key") and not entry.get("source_catalog_keys"):
            raise RuntimeError(
                f"已登记官方资产缺少来源证据: {request.category}/{request.name}"
            )
        if request.kind in {"standard-player-icon", "special-player-icon"}:
            width, height = entry["width"], entry["height"]
            aspect_ratio = width / height
            if min(width, height) < 64 or not 0.9 <= aspect_ratio <= 1.1:
                raise RuntimeError(f"玩家头像尺寸异常: {request.name}: {width}x{height}")
            player_avatar_dimensions[request.name] = [width, height]
            if request.kind == "special-player-icon":
                special_dimensions[request.name] = [width, height]

    for (category, name), entry in entries.items():
        if category != "characters" or not re.fullmatch(
            r"CHR_\d{6}_\d{2}_em_\d{3}\.png", name, re.IGNORECASE
        ):
            continue
        if name.casefold() not in declared_special_names:
            raise RuntimeError(f"差分头像没有 SpecialIconItemMB 映射: {name}")
        if not entry.get("source_resource_key") and not entry.get(
            "source_catalog_keys"
        ):
            raise RuntimeError(f"差分头像缺少来源证据: {category}/{name}")
        width, height = entry["width"], entry["height"]
        aspect_ratio = width / height
        if min(width, height) < 64 or not 0.9 <= aspect_ratio <= 1.1:
            raise RuntimeError(f"差分头像尺寸异常: {name}: {width}x{height}")
        player_avatar_dimensions[name] = [width, height]
        special_dimensions[name] = [width, height]

    return {
        "status": "accepted",
        "repository": str(repository),
        "manifest_version": manifest.get("version"),
        "asset_version": manifest.get("asset_version"),
        "request_registry_sha256": registry.fingerprint,
        "requested_asset_count": len(registry.assets),
        "verified_manifest_asset_count": len(entries),
        "verified_manifest_bytes": total_verified_bytes,
        "declared_special_player_icon_count": len(declared_special_names),
        "published_special_player_icon_count": len(special_dimensions),
        "unpublished_special_master_record_count": (
            len(declared_special_names) - len(special_dimensions)
        ),
        "player_avatar_dimensions": dict(sorted(player_avatar_dimensions.items())),
        "special_player_icon_dimensions": dict(sorted(special_dimensions.items())),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="登记并验收按需使用的官方游戏美术资产。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="输出当前官方资产需求清单摘要")
    verify = subparsers.add_parser("verify-repository", help="验收生成后的资产仓库")
    verify.add_argument("--repository", type=Path, required=True)
    sync = subparsers.add_parser(
        "sync-player-avatar-inventory",
        help="从官方 CharacterMB/SpecialIconItemMB 同步完整玩家头像库存",
    )
    source = sync.add_mutually_exclusive_group(required=True)
    source.add_argument("--master-dir", type=Path, help="已下载的 MasterBook 目录")
    source.add_argument(
        "--auto-update", action="store_true", help="下载并校验当前官方 MasterBook"
    )
    sync.add_argument("--master-version", default="", help="本地 Master 版本")
    sync.add_argument(
        "--output", type=Path, default=DEFAULT_PLAYER_AVATAR_INVENTORY_PATH
    )
    inventory_verify = subparsers.add_parser(
        "verify-player-avatar-inventory",
        help="验证已提交玩家头像库存与指定 MasterBook 完全一致",
    )
    inventory_verify.add_argument("--master-dir", type=Path, required=True)
    inventory_verify.add_argument("--master-version", required=True)
    inventory_verify.add_argument(
        "--inventory", type=Path, default=DEFAULT_PLAYER_AVATAR_INVENTORY_PATH
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "list":
            registry = load_asset_request_registry()
            result = {
                "schema_version": 1,
                "path": str(registry.path),
                "request_registry_sha256": registry.fingerprint,
                "requested_asset_count": len(registry.assets),
                "groups": {
                    group_id: len(names)
                    for group_id, names in sorted(registry.group_names.items())
                },
                "categories": {
                    category: sum(1 for asset in registry.assets if asset.category == category)
                    for category in sorted({asset.category for asset in registry.assets})
                },
                "inventory_sources": registry.inventory_sources,
            }
        elif args.command == "verify-repository":
            result = verify_requested_repository(args.repository)
        elif args.command == "verify-player-avatar-inventory":
            result = verify_player_avatar_inventory(
                args.master_dir, args.master_version, args.inventory
            )
        elif args.master_dir:
            if not args.master_version:
                raise ValueError("--master-dir 模式必须提供 --master-version")
            result = sync_player_avatar_inventory(
                args.master_dir, args.master_version, args.output
            )
        else:
            with tempfile.TemporaryDirectory(prefix="mmtm-player-avatar-master-") as temp:
                master_dir = Path(temp)
                master_version = _download_latest_player_avatar_master(master_dir)
                result = sync_player_avatar_inventory(
                    master_dir, master_version, args.output
                )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"官方资产工作流失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
