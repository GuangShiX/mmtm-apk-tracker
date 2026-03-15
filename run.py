"""一键执行全流程 — 下载 → 解包 → 对比"""

import json
import sys
from pathlib import Path

from download import download_apk, get_apk_version, list_local_versions
from extract import extract_version, find_apk
from diff import run_diff, list_extracted_versions

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def get_latest_extracted_version() -> str | None:
    """获取最新的已解包版本"""
    versions = list_extracted_versions()
    return versions[-1] if versions else None


def main():
    print("=" * 60)
    print("MementoMori APK Tracker — 一键执行")
    print("=" * 60)

    source = None
    skip_download = False

    for arg in sys.argv[1:]:
        if arg == "--skip-download":
            skip_download = True
        elif arg in ("apk-pure", "google-play"):
            source = arg

    # Step 0: 记录旧版本
    old_version = get_latest_extracted_version()
    if old_version:
        print(f"\n当前最新已解包版本: {old_version}")
    else:
        print("\n暂无已解包版本（首次运行）")

    # Step 1: 下载
    if not skip_download:
        print("\n--- Step 1: 下载 APK ---")
        apk_path = download_apk(source)
        if not apk_path:
            print("下载失败，退出")
            sys.exit(1)
        new_version = get_apk_version(apk_path)
    else:
        print("\n--- Step 1: 跳过下载 ---")
        # 找本地最新的 APK
        local = list_local_versions()
        if not local:
            print("本地无 APK 文件，请先下载")
            sys.exit(1)
        new_version = local[-1]
        print(f"使用本地最新版本: {new_version}")

    if not new_version:
        print("无法确定版本号，退出")
        sys.exit(1)

    # Step 2: 解包
    print(f"\n--- Step 2: 解包 v{new_version} ---")
    out_dir = extract_version(new_version)
    if not out_dir:
        print("解包失败，退出")
        sys.exit(1)

    # Step 3: 对比
    if old_version and old_version != new_version:
        print(f"\n--- Step 3: 对比 {old_version} → {new_version} ---")
        run_diff(old_version, new_version)
    elif old_version == new_version:
        print(f"\n--- Step 3: 版本未变化 ({new_version})，跳过对比 ---")
    else:
        print(f"\n--- Step 3: 首次运行，无旧版本可对比 ---")

    print("\n完成!")


if __name__ == "__main__":
    main()
