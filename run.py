"""自动更新流程：检查远端版本 -> 下载 -> 解包 -> 生成差异报告。"""

import argparse
import json
import sys
from pathlib import Path

from diff import list_extracted_versions, run_diff
from download import (
    download_apk,
    find_local_apk,
    list_local_versions,
    list_remote_versions,
    verify_zip,
    version_key,
)

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def latest_version(versions: list[str]) -> str | None:
    return sorted(versions, key=version_key)[-1] if versions else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="检查 MementoMori 最新 APK，发现更新后自动下载并解包。"
    )
    parser.add_argument(
        "source",
        nargs="?",
        choices=("apk-pure", "google-play"),
        default=None,
        help="下载源，默认读取 config.json",
    )
    parser.add_argument(
        "--version",
        help="指定目标版本；不指定时自动检查 APKPure 远端最新版",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="只检查是否有新版本，不下载或解包",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使 manifest.json 已存在也重新解包",
    )
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="只使用本地已有完整 APK/XAPK；缺失时直接失败",
    )
    return parser.parse_args()


def resolve_target_version(requested: str | None) -> str | None:
    if requested:
        return requested

    print("检查远端版本...")
    remote_versions = list_remote_versions()
    if not remote_versions:
        print("未能获取远端版本列表")
        return None

    remote_latest = latest_version(remote_versions)
    print(f"远端最新版本: {remote_latest}")
    return remote_latest


def ensure_apk(version: str, source: str | None, skip_download: bool) -> Path | None:
    local = find_local_apk(version)
    if local and verify_zip(local):
        print(f"使用本地完整包: {local.name}")
        return local

    if local:
        print(f"本地包不完整，将重新下载: {local.name}")
        local.unlink()

    if skip_download:
        print(f"本地没有完整的 {version} APK/XAPK，且指定了 --skip-download")
        return None

    return download_apk(source=source, version=version)


def main() -> int:
    args = parse_args()
    source = args.source or CONFIG.get("source", "apk-pure")

    print("=" * 60)
    print("MementoMori APK Tracker")
    print("=" * 60)
    print(f"下载源: {source}")

    extracted_versions = list_extracted_versions()
    local_versions = list_local_versions(complete_only=True)
    current_version = latest_version(extracted_versions)

    print(f"已解包版本: {', '.join(extracted_versions) if extracted_versions else '无'}")
    print(f"本地完整包: {', '.join(local_versions) if local_versions else '无'}")

    target_version = resolve_target_version(args.version)
    if not target_version:
        return 1

    if args.check_only:
        if current_version == target_version:
            print(f"当前已是最新版本: {current_version}")
        else:
            print(f"发现可更新版本: {current_version or '无'} -> {target_version}")
        return 0

    if current_version == target_version and not args.force:
        print(f"当前已是最新版本且已解包: {target_version}")
        return 0

    apk_path = ensure_apk(target_version, source, args.skip_download)
    if not apk_path:
        return 1

    print(f"\n开始解包: {target_version}")
    from extract import extract_version

    out_dir = extract_version(target_version, force=args.force)
    if not out_dir:
        return 1

    if current_version and current_version != target_version:
        print(f"\n生成差异报告: {current_version} -> {target_version}")
        run_diff(current_version, target_version)
    elif not current_version:
        print("\n首次解包，没有旧版本可对比")

    print("\n完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
