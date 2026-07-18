"""Build a small, GitHub-friendly fallback set of critical game icons."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import UnityPy

from asset_cdn import (
    CatalogTarget,
    OfficialAssetInfo,
    download_catalog,
    download_official_apk,
    download_target_bundles,
    get_official_app_version,
    get_official_asset_info,
    load_catalog,
    resolve_critical_catalog_targets,
)
from download import (
    download_apk,
    find_local_apk,
    get_apk_version,
    verify_zip,
    version_key,
)

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
GENERATED_MARKER = ".generated-by-mmtm-apk-tracker"
MAX_ASSET_COUNT = 3000
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024

UI_ASSET_NAMES = {
    "Background_Default.png",
    "frame_common_lr_slice.png",
    "frame_common_slice.png",
    "frame_common_watercolor.png",
    "frame_decoration_rplus.png",
    "frame_decoration_srplus.png",
    "frame_decoration_ssrplus.png",
    "frame_decoration_urplus.png",
    "frame_sphere_metal.png",
    "frame_sphere_watercolor.png",
    "icon_element_0.png",
    "icon_element_1.png",
    "icon_element_2.png",
    "icon_element_3.png",
    "icon_element_4.png",
    "icon_element_5.png",
    "icon_element_6.png",
    "icon_rarity_plus_star_1.png",
    "icon_rarity_plus_star_2.png",
    "plate_character.png",
    "tab_bg.png",
}
UI_ASSET_NAMES_BY_CASE = {name.casefold(): name for name in UI_ASSET_NAMES}


@dataclass(frozen=True)
class Candidate:
    resource_key: str
    asset_type: str
    relative_file: str
    container: str
    category: str
    name: str
    local_file: Path

    @property
    def score(self) -> tuple[bool, bool, bool, int]:
        return (
            bool(self.container) and self.resource_key == self.container,
            Path(self.relative_file).name == self.name,
            self.asset_type == "Sprite",
            -len(self.relative_file),
        )


@dataclass(frozen=True)
class PackageCandidate:
    resource_key: str
    asset_type: str
    bundle_member: str
    path_id: int
    container: str
    category: str
    name: str

    @property
    def score(self) -> tuple[bool, bool]:
        return (bool(self.container), self.asset_type == "Sprite")


def _canonical_name(relative_file: str, container: str) -> str:
    if container:
        return Path(container.split("#", 1)[0]).name
    name = Path(relative_file).name
    return re.sub(r"__-?\d+(?=\.png$)", "", name, flags=re.IGNORECASE)


def _category(name: str) -> str | None:
    if re.fullmatch(r"CHR_\d{6}_\d{2}_s\.png", name, re.IGNORECASE):
        return "characters"
    if re.fullmatch(r"ENE_\d{6}\.png", name, re.IGNORECASE):
        return "enemies"
    if re.fullmatch(r"EQP_\d{6}\.png", name, re.IGNORECASE):
        return "equipment"
    if re.fullmatch(r"SPH_\d{4}\.png", name, re.IGNORECASE):
        return "spheres"
    if re.fullmatch(r"Item_\d{4}(?:_S)?\.png", name, re.IGNORECASE):
        return "items"
    if name.casefold() in UI_ASSET_NAMES_BY_CASE:
        return "ui"
    return None


def _iter_image_rows(database: Path) -> Iterable[tuple[str, str, str, str]]:
    connection = sqlite3.connect(database)
    try:
        yield from connection.execute(
            """
            SELECT resource_key, type, file, container
            FROM resources
            WHERE type IN ('Sprite', 'Texture2D')
            """
        )
    finally:
        connection.close()


def _safe_asset_path(version_dir: Path, relative_file: str) -> Path:
    root = version_dir.resolve()
    path = (version_dir / relative_file).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"关键图标路径越界: {relative_file}") from exc
    return path


def validate_complete_extraction(version_dir: Path, version: str) -> dict[str, Any]:
    report_path = version_dir / "extraction_report.json"
    required = (
        report_path,
        version_dir / "package_manifest.json",
        version_dir / "manifest.sqlite3",
    )
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(f"版本 {version} 缺少完整提取文件: {', '.join(missing)}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("version") != version:
        raise RuntimeError(f"提取报告版本不匹配: {report.get('version')} != {version}")
    if report.get("status") != "complete":
        raise RuntimeError(f"版本 {version} 的提取状态不是 complete")
    if float(report.get("raw_object_coverage", 0)) != 1.0:
        raise RuntimeError(f"版本 {version} 的原始对象覆盖率不是 1.0")
    if int(report.get("bundles", 0)) <= 0:
        raise RuntimeError(f"版本 {version} 没有 AssetBundle")
    if int(report.get("unity_sources", 0)) <= 0:
        raise RuntimeError(f"版本 {version} 没有 Unity 数据源")
    if int(report.get("objects_total", 0)) <= 0:
        raise RuntimeError(f"版本 {version} 没有 Unity 对象")
    if int(report.get("objects_raw_exported", -1)) != int(report["objects_total"]):
        raise RuntimeError(f"版本 {version} 的原始对象数量不完整")
    if report.get("errors"):
        raise RuntimeError(f"版本 {version} 的提取报告包含错误")
    return report


def collect_critical_assets(version_dir: Path, version: str) -> list[Candidate]:
    validate_complete_extraction(version_dir, version)
    selected: dict[tuple[str, str], Candidate] = {}

    for resource_key, asset_type, relative_file, container in _iter_image_rows(
        version_dir / "manifest.sqlite3"
    ):
        name = _canonical_name(relative_file, container)
        category = _category(name)
        if not category:
            continue
        local_file = _safe_asset_path(version_dir, relative_file)
        if not local_file.is_file():
            continue
        candidate = Candidate(
            resource_key=resource_key,
            asset_type=asset_type,
            relative_file=relative_file.replace("\\", "/"),
            container=container,
            category=category,
            name=name,
            local_file=local_file,
        )
        key = (category, name)
        current = selected.get(key)
        if current is None or candidate.score > current.score:
            selected[key] = candidate

    assets = sorted(selected.values(), key=lambda item: (item.category, item.name))
    if not assets:
        raise RuntimeError(f"版本 {version} 没有找到关键图标")
    missing_ui = sorted(UI_ASSET_NAMES - {asset.name for asset in assets})
    if missing_ui:
        raise RuntimeError(f"版本 {version} 缺少公共 UI 图标: {', '.join(missing_ui)}")
    if len(assets) > MAX_ASSET_COUNT:
        raise RuntimeError(f"关键图标数量异常: {len(assets)} > {MAX_ASSET_COUNT}")
    return assets


def _bundle_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    return sorted(
        (
            info
            for info in archive.infolist()
            if not info.is_dir() and info.filename.lower().endswith(".bundle")
        ),
        key=lambda info: info.filename,
    )


def _extract_asset_apk(package_path: Path, work_dir: Path) -> Path:
    """Return an APK containing Addressables bundles from an APK or XAPK."""
    try:
        with zipfile.ZipFile(package_path) as package:
            if _bundle_members(package):
                return package_path

            nested_apks = sorted(
                (
                    info
                    for info in package.infolist()
                    if not info.is_dir() and info.filename.lower().endswith(".apk")
                ),
                key=lambda info: info.file_size,
                reverse=True,
            )
            for index, info in enumerate(nested_apks, 1):
                target = work_dir / f"nested-{index}.apk"
                with package.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                try:
                    with zipfile.ZipFile(target) as nested:
                        bundles = _bundle_members(nested)
                except zipfile.BadZipFile:
                    bundles = []
                if bundles:
                    print(
                        f"  数据资源包: {info.filename} "
                        f"({len(bundles)} 个 Bundle)"
                    )
                    return target
                target.unlink(missing_ok=True)
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"安装包不是有效 ZIP: {package_path}") from exc
    raise RuntimeError(f"安装包中没有找到 Unity Bundle: {package_path.name}")


def _object_asset_name(container: str, obj: Any) -> str:
    if container:
        name = Path(container.split("#", 1)[0]).name
    else:
        try:
            name = str(obj.peek_name() or "")
        except Exception:
            name = ""
        if name and not name.lower().endswith(".png"):
            name += ".png"
    return UI_ASSET_NAMES_BY_CASE.get(name.casefold(), name)


def scan_package_critical_assets(
    asset_apk: Path,
    *,
    require_ui: bool = True,
) -> tuple[list[PackageCandidate], dict[str, str], int]:
    """Index every bundle but retain only objects needed by the icon repository."""
    selected: dict[tuple[str, str], PackageCandidate] = {}
    cab_index: dict[str, str] = {}
    failures: list[str] = []

    with zipfile.ZipFile(asset_apk) as archive:
        bundles = _bundle_members(archive)
        if not bundles:
            raise RuntimeError(f"数据资源包中没有 Bundle: {asset_apk.name}")
        for index, info in enumerate(bundles, 1):
            try:
                env = UnityPy.load(archive.read(info))
            except Exception as exc:
                failures.append(
                    f"{info.filename}: {type(exc).__name__}: {exc}"
                )
                continue

            for cab_name in env.cabs:
                lowered = str(cab_name).lower()
                if not lowered.endswith((".ress", ".resource")):
                    cab_index[lowered] = info.filename

            container_map = {
                obj_info.path_id: container_path
                for container_path, obj_info in env.container.items()
                if hasattr(obj_info, "path_id")
            }
            for obj in env.objects:
                if obj.type.name not in ("Sprite", "Texture2D"):
                    continue
                container = container_map.get(obj.path_id, "")
                name = _object_asset_name(container, obj)
                category = _category(name)
                if not category:
                    continue
                resource_key = container or (
                    f"{info.filename}#{obj.type.name}:{obj.path_id}"
                )
                candidate = PackageCandidate(
                    resource_key=resource_key,
                    asset_type=obj.type.name,
                    bundle_member=info.filename,
                    path_id=obj.path_id,
                    container=container,
                    category=category,
                    name=name,
                )
                key = (category, name)
                current = selected.get(key)
                if current is None or candidate.score > current.score:
                    selected[key] = candidate

            if index % 500 == 0:
                print(
                    f"  扫描 Bundle: {index}/{len(bundles)}, "
                    f"已找到 {len(selected)} 个关键图标"
                )

    if failures:
        preview = "; ".join(failures[:3])
        raise RuntimeError(
            f"有 {len(failures)} 个 Bundle 无法读取，拒绝生成不完整图标集: "
            f"{preview}"
        )
    assets = sorted(selected.values(), key=lambda item: (item.category, item.name))
    if not assets:
        raise RuntimeError("安装包中没有找到关键图标")
    missing_ui = sorted(UI_ASSET_NAMES - {asset.name for asset in assets})
    if require_ui and missing_ui:
        raise RuntimeError(f"安装包缺少公共 UI 图标: {', '.join(missing_ui)}")
    if len(assets) > MAX_ASSET_COUNT:
        raise RuntimeError(f"关键图标数量异常: {len(assets)} > {MAX_ASSET_COUNT}")
    return assets, cab_index, len(bundles)


def _read_candidate_image(
    archive: zipfile.ZipFile,
    env: Any,
    obj: Any,
    candidate: PackageCandidate,
    cab_index: dict[str, str],
    loaded_dependencies: set[str],
) -> Any:
    try:
        return obj.read().image
    except FileNotFoundError:
        for external in obj.assets_file.externals:
            name = str(getattr(external, "name", "")).lower()
            dependency = cab_index.get(name)
            if (
                dependency
                and dependency != candidate.bundle_member
                and dependency not in loaded_dependencies
            ):
                env.load_file(archive.read(dependency), is_dependency=True)
                loaded_dependencies.add(dependency)
        return obj.read().image


def export_package_critical_assets(
    asset_apk: Path,
    candidates: list[PackageCandidate],
    cab_index: dict[str, str],
    assets_dir: Path,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[PackageCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[candidate.bundle_member].append(candidate)

    entries: list[dict[str, Any]] = []
    with zipfile.ZipFile(asset_apk) as archive:
        for index, bundle_member in enumerate(sorted(grouped), 1):
            env = UnityPy.load(archive.read(bundle_member))
            objects = {obj.path_id: obj for obj in env.objects}
            loaded_dependencies: set[str] = set()
            for candidate in grouped[bundle_member]:
                obj = objects.get(candidate.path_id)
                if obj is None:
                    raise RuntimeError(
                        f"Bundle 对象消失: {bundle_member}#{candidate.path_id}"
                    )
                image = _read_candidate_image(
                    archive,
                    env,
                    obj,
                    candidate,
                    cab_index,
                    loaded_dependencies,
                )
                relative_output = (
                    Path("assets") / candidate.category / candidate.name
                )
                target = assets_dir.parent / relative_output
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".tmp")
                image.save(temporary, format="PNG")
                temporary.replace(target)
                size = target.stat().st_size
                if size > MAX_FILE_BYTES:
                    raise RuntimeError(
                        f"关键图标过大: {candidate.name} ({size} bytes)"
                    )
                entries.append(
                    {
                        "category": candidate.category,
                        "name": candidate.name,
                        "path": relative_output.as_posix(),
                        "sha256": _sha256(target),
                        "size": size,
                        "source_resource_key": candidate.resource_key,
                        "source_bundles": [Path(candidate.bundle_member).name],
                    }
                )
            if index % 250 == 0:
                print(
                    f"  导出关键图标 Bundle: {index}/{len(grouped)}, "
                    f"图片 {len(entries)}"
                )
    return sorted(entries, key=lambda item: (item["category"], item["name"]))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_assets_directory(output_dir: Path) -> Path:
    assets_dir = output_dir / "assets"
    marker = assets_dir / GENERATED_MARKER
    if assets_dir.exists():
        if not marker.is_file():
            raise RuntimeError(
                f"拒绝清理非本工具生成的目录，缺少 {marker}"
            )
        shutil.rmtree(assets_dir)
    assets_dir.mkdir(parents=True)
    (assets_dir / GENERATED_MARKER).write_text(
        "This directory is generated. Do not edit files manually.\n",
        encoding="utf-8",
    )
    return assets_dir


def _write_repository_metadata(
    version: str,
    output_dir: Path,
    repository: str,
    entries: list[dict[str, Any]],
    generated_at: datetime,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository 必须使用 owner/name 格式")
    entries = sorted(entries, key=lambda item: (item["category"], item["name"]))
    seen: set[tuple[str, str]] = set()
    category_counts: dict[str, int] = {}
    total_bytes = 0
    root = output_dir.resolve()
    for entry in entries:
        key = (entry["category"], entry["name"])
        if key in seen:
            raise RuntimeError(f"关键图标清单重复: {key[0]}/{key[1]}")
        seen.add(key)
        path = (output_dir / entry["path"]).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(f"关键图标路径越界: {entry['path']}") from exc
        if not path.is_file():
            raise RuntimeError(f"关键图标文件缺失: {entry['path']}")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise RuntimeError(f"关键图标过大: {entry['name']} ({size} bytes)")
        digest = _sha256(path)
        if entry.get("size") != size or entry.get("sha256") != digest:
            raise RuntimeError(f"关键图标清单校验失败: {entry['path']}")
        total_bytes += size
        category_counts[entry["category"]] = (
            category_counts.get(entry["category"], 0) + 1
        )
    if len(entries) > MAX_ASSET_COUNT:
        raise RuntimeError(f"关键图标数量异常: {len(entries)} > {MAX_ASSET_COUNT}")
    if total_bytes > MAX_TOTAL_BYTES:
        raise RuntimeError(f"关键图标总体积异常: {total_bytes} > {MAX_TOTAL_BYTES}")
    missing_ui = sorted(
        UI_ASSET_NAMES
        - {entry["name"] for entry in entries if entry["category"] == "ui"}
    )
    if missing_ui:
        raise RuntimeError(f"关键图标仓库缺少公共 UI 图标: {', '.join(missing_ui)}")

    archive_ref = f"v{version}"
    raw_base = f"https://raw.githubusercontent.com/{repository}/main"
    metadata = dict(extra or {})
    manifest = {
        "schema_version": 2,
        "game": "MementoMori",
        "package_name": CONFIG["package_name"],
        "version": version,
        "ref": "main",
        "archive_ref": archive_ref,
        "generated_at": generated_at.isoformat(),
        "base_url": raw_base,
        "cache_strategy": "stable-path-sha256",
        "asset_count": len(entries),
        "total_bytes": total_bytes,
        "category_counts": dict(sorted(category_counts.items())),
        **metadata,
        "assets": entries,
    }
    latest = {
        "schema_version": 2,
        "game": "MementoMori",
        "version": version,
        "ref": "main",
        "archive_ref": archive_ref,
        "generated_at": generated_at.isoformat(),
        "manifest_url": f"{raw_base}/manifest.json",
        "base_url": raw_base,
        "cache_strategy": "stable-path-sha256",
        "asset_count": len(entries),
        "total_bytes": total_bytes,
    }
    for name in ("asset_version", "master_version"):
        if name in metadata:
            latest[name] = metadata[name]
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "latest.json").write_text(
        json.dumps(latest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _install_staged_repository(staging: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    current_assets = output_dir / "assets"
    if current_assets.exists() and not (
        current_assets / GENERATED_MARKER
    ).is_file():
        raise RuntimeError(
            f"拒绝替换非本工具生成的目录，缺少 "
            f"{current_assets / GENERATED_MARKER}"
        )
    backup = output_dir.parent / f".{output_dir.name}-assets-backup"
    if backup.exists():
        shutil.rmtree(backup)
    if current_assets.exists():
        current_assets.replace(backup)
    try:
        (staging / "assets").replace(current_assets)
        for name in ("manifest.json", "latest.json"):
            temporary = output_dir / f".{name}.tmp"
            shutil.copyfile(staging / name, temporary)
            temporary.replace(output_dir / name)
    except Exception:
        if current_assets.exists():
            shutil.rmtree(current_assets)
        if backup.exists():
            backup.replace(current_assets)
        raise
    finally:
        if backup.exists():
            shutil.rmtree(backup)


def _load_repository_manifest(output_dir: Path) -> dict[str, Any] | None:
    path = output_dir / "manifest.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取现有备用仓库清单: {path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("assets"), list):
        raise RuntimeError(f"现有备用仓库清单格式无效: {path}")
    return data


def build_fallback_repository(
    version: str,
    output_dir: Path,
    repository: str,
    version_dir: Path | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    version_dir = version_dir or (
        ROOT / CONFIG["dirs"]["extracted"] / version
    )
    candidates = collect_critical_assets(version_dir, version)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = _prepare_assets_directory(output_dir)
    generated_at = generated_at or datetime.now(timezone.utc)

    entries: list[dict[str, Any]] = []
    for candidate in candidates:
        size = candidate.local_file.stat().st_size
        if size > MAX_FILE_BYTES:
            raise RuntimeError(
                f"关键图标过大: {candidate.name} ({size} bytes)"
            )
        relative_output = Path("assets") / candidate.category / candidate.name
        target = output_dir / relative_output
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(candidate.local_file, target)
        digest = _sha256(target)
        entries.append(
            {
                "category": candidate.category,
                "name": candidate.name,
                "path": relative_output.as_posix(),
                "sha256": digest,
                "size": size,
                "source_resource_key": candidate.resource_key,
            }
        )

    return _write_repository_metadata(
        version,
        output_dir,
        repository,
        entries,
        generated_at,
        {"generation_mode": "complete-extraction"},
    )


def build_fallback_from_package(
    version: str,
    package_path: Path,
    output_dir: Path,
    repository: str,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    package_path = package_path.resolve()
    if not verify_zip(package_path):
        raise RuntimeError(f"安装包不完整或不是有效 ZIP: {package_path}")
    detected_version = get_apk_version(package_path)
    if detected_version and detected_version != version:
        raise RuntimeError(
            f"安装包版本不匹配: 期望 {version}, 实际 {detected_version}"
        )
    output_dir = output_dir.resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    generated_at = generated_at or datetime.now(timezone.utc)

    with tempfile.TemporaryDirectory(
        prefix="mmtm-base-icons-", dir=output_dir.parent
    ) as temporary_dir:
        temporary = Path(temporary_dir)
        work_dir = temporary / "package"
        work_dir.mkdir()
        staging = temporary / "repository"
        staging.mkdir()
        asset_apk = _extract_asset_apk(package_path, work_dir)
        print("  扫描基础包关键图片...")
        candidates, cab_index, bundle_count = scan_package_critical_assets(asset_apk)
        assets_dir = _prepare_assets_directory(staging)
        print(f"  导出 {len(candidates)} 个基础关键图片...")
        entries = export_package_critical_assets(
            asset_apk, candidates, cab_index, assets_dir
        )
        manifest = _write_repository_metadata(
            version,
            staging,
            repository,
            entries,
            generated_at,
            {
                "generation_mode": "critical-package-scan",
                "package_bundle_count": bundle_count,
            },
        )
        _install_staged_repository(staging, output_dir)
        return manifest


def _catalog_target_metadata(target: CatalogTarget) -> dict[str, Any]:
    return {
        "source_bundles": list(target.bundle_names),
        "source_catalog_keys": list(target.catalog_keys),
    }


def sync_official_hot_update(
    info: OfficialAssetInfo,
    output_dir: Path,
    repository: str,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    current = _load_repository_manifest(output_dir)
    if current is None:
        raise RuntimeError("热更新前必须先生成基础关键图标仓库")
    if current.get("version") != info.app_version:
        raise RuntimeError(
            f"基础图标版本 {current.get('version')} 与官方版本 "
            f"{info.app_version} 不一致"
        )
    if current.get("asset_version") == info.asset_version:
        print(f"官方资源版本未变化: {info.asset_version}")
        return current
    if not (output_dir / "assets" / GENERATED_MARKER).is_file():
        raise RuntimeError("现有 assets 目录不是本工具生成，拒绝合并热更新")
    generated_at = generated_at or datetime.now(timezone.utc)
    output_dir.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="mmtm-hot-icons-", dir=output_dir.parent
    ) as temporary_dir:
        temporary = Path(temporary_dir)
        catalog_path = download_catalog(info, temporary / "catalog.json")
        targets = resolve_critical_catalog_targets(load_catalog(catalog_path))
        existing_entries = {
            (entry["category"], entry["name"]): dict(entry)
            for entry in current["assets"]
        }
        changed_targets: dict[tuple[str, str], CatalogTarget] = {}
        for key, target in targets.items():
            existing = existing_entries.get(key)
            existing_bundles = tuple(sorted(existing.get("source_bundles", []))) if existing else ()
            existing_path = output_dir / existing["path"] if existing else None
            if (
                existing is None
                or existing_bundles != target.bundle_names
                or existing_path is None
                or not existing_path.is_file()
            ):
                changed_targets[key] = target

        staging = temporary / "repository"
        shutil.copytree(output_dir / "assets", staging / "assets")
        exported_entries: list[dict[str, Any]] = []
        if changed_targets:
            bundle_names = {
                name
                for target in changed_targets.values()
                for name in target.bundle_names
            }
            bundle_paths = download_target_bundles(
                info, bundle_names, temporary / "bundles"
            )
            bundle_archive = temporary / "hot-update-bundles.zip"
            with zipfile.ZipFile(bundle_archive, "w", zipfile.ZIP_STORED) as archive:
                for bundle_path in bundle_paths:
                    archive.write(bundle_path, bundle_path.name)
            candidates, cab_index, _ = scan_package_critical_assets(
                bundle_archive, require_ui=False
            )
            wanted_keys = set(changed_targets)
            candidates = [
                candidate
                for candidate in candidates
                if (candidate.category, candidate.name) in wanted_keys
            ]
            found_keys = {
                (candidate.category, candidate.name) for candidate in candidates
            }
            missing = sorted(wanted_keys - found_keys)
            if missing:
                labels = ", ".join(f"{category}/{name}" for category, name in missing)
                raise RuntimeError(f"热更新 Bundle 未导出 catalog 关键图片: {labels}")
            exported_entries = export_package_critical_assets(
                bundle_archive,
                candidates,
                cab_index,
                staging / "assets",
            )

        for entry in exported_entries:
            key = (entry["category"], entry["name"])
            entry.update(_catalog_target_metadata(changed_targets[key]))
            existing_entries[key] = entry
        entries = list(existing_entries.values())
        category_counts: dict[str, int] = {}
        for target in targets.values():
            category_counts[target.category] = category_counts.get(target.category, 0) + 1
        manifest = _write_repository_metadata(
            info.app_version,
            staging,
            repository,
            entries,
            generated_at,
            {
                "generation_mode": "critical-package-plus-official-hot-update",
                "asset_version": info.asset_version,
                "master_version": info.master_version,
                "hot_update_asset_count": len(targets),
                "hot_update_category_counts": dict(sorted(category_counts.items())),
            },
        )
        _install_staged_repository(staging, output_dir)
        print(
            f"官方热更新已合并: assetVersion={info.asset_version}, "
            f"catalog关键图片={len(targets)}, 更新图片={len(exported_entries)}"
        )
        return manifest


def auto_update_fallback_repository(
    output_dir: Path,
    repository: str,
    package_path: Path | None = None,
) -> dict[str, Any]:
    info = get_official_asset_info()
    output_dir = output_dir.resolve()
    current = _load_repository_manifest(output_dir)
    if current is None or current.get("version") != info.app_version:
        with tempfile.TemporaryDirectory(prefix="mmtm-official-apk-") as temporary_dir:
            source = package_path.resolve() if package_path else None
            if source is None:
                source = download_official_apk(
                    info,
                    Path(temporary_dir) / f"mementomori_{info.app_version}.apk",
                )
            print(f"生成官方基础图片集: appVersion={info.app_version}")
            build_fallback_from_package(
                info.app_version,
                source,
                output_dir,
                repository,
            )
    return sync_official_hot_update(info, output_dir, repository)


def _latest_local_version() -> str | None:
    extracted = ROOT / CONFIG["dirs"]["extracted"]
    if not extracted.is_dir():
        return None
    versions = [path.name for path in extracted.iterdir() if path.is_dir()]
    return sorted(versions, key=version_key)[-1] if versions else None


def parse_args() -> argparse.Namespace:
    fallback_config = CONFIG.get("github_fallback", {})
    parser = argparse.ArgumentParser(
        description="生成并自动更新 GitHub 关键图标仓库。"
    )
    parser.add_argument("--version", help="目标游戏版本；默认使用本地最新版本")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "fallback_dist",
        help="备用仓库工作目录",
    )
    parser.add_argument(
        "--repository",
        default=fallback_config.get(
            "repository", "GuangShiX/mmtm-assets-fallback"
        ),
        help="GitHub owner/name，用于生成 main 分支稳定 Raw URL",
    )
    parser.add_argument(
        "--latest-remote-version",
        action="store_true",
        help="只输出 MementoMori 官方页面最新游戏版本",
    )
    parser.add_argument(
        "--remote-info",
        action="store_true",
        help="输出官方 appVersion、assetVersion 和 masterVersion JSON",
    )
    parser.add_argument(
        "--auto-update",
        action="store_true",
        help="自动检测基础 APK 和热更新资源并合并到输出仓库",
    )
    parser.add_argument(
        "--sync-hot-update",
        action="store_true",
        help="只合并官方 Addressables 热更新关键图片",
    )
    parser.add_argument(
        "--from-package",
        action="store_true",
        help="从 APK/XAPK 直接扫描关键图片，不执行完整解包",
    )
    parser.add_argument(
        "--apk",
        type=Path,
        help="--from-package/--auto-update 使用的本地 APK/XAPK",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.latest_remote_version:
            print(get_official_app_version())
            return 0
        if args.remote_info:
            info = get_official_asset_info(args.version)
            print(
                json.dumps(
                    {
                        "app_version": info.app_version,
                        "asset_version": info.asset_version,
                        "master_version": info.master_version,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.auto_update:
            manifest = auto_update_fallback_repository(
                args.output, args.repository, args.apk
            )
            print(
                f"备用图标已自动更新: version={manifest['version']}, "
                f"assetVersion={manifest.get('asset_version', '')}, "
                f"assets={manifest['asset_count']}, bytes={manifest['total_bytes']}"
            )
            return 0
        if args.sync_hot_update:
            info = get_official_asset_info(args.version)
            manifest = sync_official_hot_update(
                info, args.output, args.repository
            )
            print(
                f"备用图标热更新完成: version={manifest['version']}, "
                f"assetVersion={manifest.get('asset_version', '')}, "
                f"assets={manifest['asset_count']}"
            )
            return 0
        if args.from_package:
            version = args.version or (
                get_apk_version(args.apk) if args.apk else None
            ) or get_official_app_version()
            package_path = args.apk or find_local_apk(version)
            if package_path is None:
                package_path = download_apk(version=version)
            if package_path is None:
                raise RuntimeError(f"无法获得版本 {version} 的安装包")
            manifest = build_fallback_from_package(
                version, package_path, args.output, args.repository
            )
            print(
                f"备用图标已从安装包生成: version={version}, "
                f"assets={manifest['asset_count']}, bytes={manifest['total_bytes']}"
            )
            return 0

        version = args.version or _latest_local_version()
        if not version:
            raise RuntimeError("没有可用的本地完整解包版本")
        manifest = build_fallback_repository(
            version=version,
            output_dir=args.output,
            repository=args.repository,
        )
        print(
            f"备用图标已生成: version={version}, assets={manifest['asset_count']}, "
            f"bytes={manifest['total_bytes']}, output={args.output.resolve()}"
        )
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"生成备用图标失败: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
