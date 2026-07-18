"""Build a small, GitHub-friendly fallback set of critical game icons."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from download import list_remote_versions, version_key
from publish import validate_complete_extraction

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
    "tab_bg.png",
}


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
    if name in UI_ASSET_NAMES:
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


def build_fallback_repository(
    version: str,
    output_dir: Path,
    repository: str,
    version_dir: Path | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository 必须使用 owner/name 格式")
    version_dir = version_dir or (
        ROOT / CONFIG["dirs"]["extracted"] / version
    )
    candidates = collect_critical_assets(version_dir, version)
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = _prepare_assets_directory(output_dir)
    generated_at = generated_at or datetime.now(timezone.utc)

    entries: list[dict[str, Any]] = []
    category_counts: dict[str, int] = {}
    total_bytes = 0
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
        total_bytes += size
        category_counts[candidate.category] = (
            category_counts.get(candidate.category, 0) + 1
        )
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

    if total_bytes > MAX_TOTAL_BYTES:
        raise RuntimeError(
            f"关键图标总体积异常: {total_bytes} > {MAX_TOTAL_BYTES}"
        )

    archive_ref = f"v{version}"
    raw_base = f"https://raw.githubusercontent.com/{repository}/main"
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
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "latest.json").write_text(
        json.dumps(latest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _latest_local_version() -> str | None:
    extracted = ROOT / CONFIG["dirs"]["extracted"]
    if not extracted.is_dir():
        return None
    versions = [path.name for path in extracted.iterdir() if path.is_dir()]
    return sorted(versions, key=version_key)[-1] if versions else None


def parse_args() -> argparse.Namespace:
    fallback_config = CONFIG.get("github_fallback", {})
    parser = argparse.ArgumentParser(
        description="从完整解包结果生成 GitHub 备用关键图标仓库。"
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
        help="只输出 APKPure 最新游戏版本",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.latest_remote_version:
            versions = list_remote_versions()
            if not versions:
                raise RuntimeError("无法获取 APKPure 远端版本")
            print(sorted(versions, key=version_key)[-1])
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
