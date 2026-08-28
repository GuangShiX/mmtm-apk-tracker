"""Curated, consumer-driven requests for official MementoMori art assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image

ROOT = Path(__file__).parent
DEFAULT_REGISTRY_PATH = ROOT / "config" / "official_asset_requests.json"
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
    canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return AssetRequestRegistry(
        path=path,
        fingerprint=fingerprint,
        assets=tuple(requests),
        group_names=group_names,
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

    special_dimensions: dict[str, list[int]] = {}
    for request in registry.assets:
        entry = entries[request.key]
        if not entry.get("source_resource_key") and not entry.get("source_catalog_keys"):
            raise RuntimeError(
                f"已登记官方资产缺少来源证据: {request.category}/{request.name}"
            )
        if request.kind == "special-player-icon":
            width, height = entry["width"], entry["height"]
            aspect_ratio = width / height
            if min(width, height) < 64 or not 0.9 <= aspect_ratio <= 1.1:
                raise RuntimeError(f"差分头像尺寸异常: {request.name}: {width}x{height}")
            special_dimensions[request.name] = [width, height]

    return {
        "status": "accepted",
        "repository": str(repository),
        "manifest_version": manifest.get("version"),
        "asset_version": manifest.get("asset_version"),
        "request_registry_sha256": registry.fingerprint,
        "requested_asset_count": len(registry.assets),
        "verified_manifest_asset_count": len(entries),
        "verified_manifest_bytes": total_verified_bytes,
        "special_player_icon_dimensions": dict(sorted(special_dimensions.items())),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="登记并验收按需使用的官方游戏美术资产。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="输出当前官方资产需求清单摘要")
    verify = subparsers.add_parser("verify-repository", help="验收生成后的资产仓库")
    verify.add_argument("--repository", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        registry = load_asset_request_registry()
        if args.command == "list":
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
            }
        else:
            result = verify_requested_repository(args.repository)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"官方资产工作流失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
