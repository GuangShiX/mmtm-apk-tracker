"""APK 下载模块 — 调用 apkeep 从 APKPure 或 Google Play 下载 MementoMori APK"""

import json
import subprocess
import sys
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def verify_zip(path: Path) -> bool:
    """验证文件是否是完整的 ZIP/APK/XAPK"""
    try:
        with open(path, "rb") as f:
            header = f.read(4)
            if header[:2] != b"PK":
                return False
            # 检查尾部有 EOCD 标记
            f.seek(0, 2)
            size = f.tell()
            search_size = min(size, 1024 * 1024)
            f.seek(size - search_size)
            tail = f.read()
            return tail.rfind(b"PK\x05\x06") >= 0 or tail.rfind(b"PK\x06\x06") >= 0
    except OSError:
        return False


def get_apk_version(apk_path: Path) -> str | None:
    """从文件名提取版本号"""
    # apkeep 格式: package@version.apk 或 package@version.xapk
    match = re.search(r"@([\d.]+)\.(x?apk)", apk_path.name)
    if match:
        return match.group(1)
    # 通用格式: 含版本号
    match = re.search(r"([\d]+\.[\d]+\.[\d]+)", apk_path.name)
    if match:
        return match.group(1)
    return None


def list_remote_versions() -> list[str]:
    """查询 APKPure 上可用的版本列表"""
    apkeep = ROOT / CONFIG["apkeep_path"]
    pkg = CONFIG["package_name"]
    try:
        result = subprocess.run(
            [str(apkeep), "-a", pkg, "-d", "apk-pure", "--list-versions", "."],
            capture_output=True, text=True, timeout=30,
        )
        # 解析输出: "| 3.19.1, 3.19.2, ..."
        for line in result.stdout.splitlines():
            if line.startswith("|"):
                versions = [v.strip() for v in line.lstrip("| ").split(",")]
                return sorted(versions, key=lambda v: [int(x) for x in v.split(".")])
    except Exception as e:
        print(f"查询版本列表失败: {e}")
    return []


def list_local_versions() -> list[str]:
    """列出本地已下载的所有版本号"""
    apks_dir = ROOT / CONFIG["dirs"]["apks"]
    if not apks_dir.exists():
        return []
    versions = []
    for f in list(apks_dir.glob("*.apk")) + list(apks_dir.glob("*.xapk")):
        v = get_apk_version(f)
        if v:
            versions.append(v)
    return sorted(set(versions), key=lambda v: [int(x) for x in v.split(".")])


def find_local_apk(version: str) -> Path | None:
    """根据版本号找到本地 APK 文件"""
    apks_dir = ROOT / CONFIG["dirs"]["apks"]
    for f in list(apks_dir.glob(f"*{version}*")) + list(apks_dir.glob("*.xapk")) + list(apks_dir.glob("*.apk")):
        v = get_apk_version(f)
        if v == version:
            return f
    return None


def download_apk(source: str | None = None, version: str | None = None) -> Path | None:
    """下载 APK，返回下载的文件路径。version=None 下载最新版。"""
    apkeep = ROOT / CONFIG["apkeep_path"]
    apks_dir = ROOT / CONFIG["dirs"]["apks"]
    apks_dir.mkdir(parents=True, exist_ok=True)

    source = source or CONFIG.get("source", "apk-pure")
    pkg = CONFIG["package_name"]

    # 确定要下载的版本
    if not version:
        remote = list_remote_versions()
        if remote:
            version = remote[-1]
            print(f"最新版本: {version}")
        else:
            print("无法获取版本列表，将下载默认最新版")

    # 检查是否已有
    if version:
        existing = find_local_apk(version)
        if existing and verify_zip(existing):
            print(f"版本 {version} 已存在且完整: {existing.name}")
            return existing
        elif existing:
            print(f"版本 {version} 文件不完整，重新下载...")
            existing.unlink()

    # 构建命令
    app_spec = f"{pkg}@{version}" if version else pkg
    cmd = [str(apkeep), "-a", app_spec]

    if source == "google-play":
        gp = CONFIG.get("google_play", {})
        email = gp.get("email", "")
        token = gp.get("token", "")
        if not email or not token:
            print("错误: Google Play 源需要在 config.json 中配置 email 和 token")
            print("获取 token: https://github.com/EFForg/apkeep/blob/master/USAGE-google-play.md")
            return None
        cmd += ["-d", "google-play", "-e", email, "-t", token]
    else:
        cmd += ["-d", "apk-pure"]

    cmd += [str(apks_dir)]

    print(f"正在下载 {app_spec} (源: {source})...")
    print(f"命令: {' '.join(cmd)}")
    print("（游戏 XAPK 约 600MB+，请耐心等待...）")

    try:
        # 大文件需要更长超时（20分钟）
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1200)
        if result.stdout.strip():
            print(result.stdout.strip())
        if result.stderr.strip():
            print(result.stderr.strip())
        if result.returncode != 0:
            print(f"下载失败 (exit code {result.returncode})")
            return None
    except subprocess.TimeoutExpired:
        print("下载超时（20分钟），请检查网络后重试")
        return None

    # 找到新下载的文件
    downloaded = sorted(
        list(apks_dir.glob(f"{pkg}*")),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not downloaded:
        print("未找到下载的文件")
        return None

    apk_file = downloaded[0]

    # 验证完整性
    if not verify_zip(apk_file):
        size_mb = apk_file.stat().st_size / 1024 / 1024
        print(f"警告: 下载的文件不完整 ({size_mb:.1f} MB)，可能需要重试")
        # 如果文件名没有版本号，尝试重命名
        if version and version not in apk_file.name:
            new_name = apk_file.with_name(f"{pkg}@{version}{apk_file.suffix}")
            apk_file.rename(new_name)
            apk_file = new_name
        return None

    # 重命名加上版本号（如果文件名没有）
    detected_ver = get_apk_version(apk_file)
    if not detected_ver and version:
        new_name = apks_dir / f"{pkg}@{version}{apk_file.suffix}"
        apk_file.rename(new_name)
        apk_file = new_name
        detected_ver = version

    print(f"下载完成: {apk_file.name} ({apk_file.stat().st_size / 1024 / 1024:.1f} MB)")
    if detected_ver:
        print(f"版本号: {detected_ver}")

    return apk_file


def main():
    source = None
    version = None

    for arg in sys.argv[1:]:
        if arg in ("apk-pure", "google-play"):
            source = arg
        elif re.match(r"^\d+\.\d+", arg):
            version = arg

    print("=== MementoMori APK 下载器 ===")
    print(f"本地已有版本: {list_local_versions() or '无'}")

    apk = download_apk(source, version)
    if apk:
        print(f"\nAPK 已保存到: {apk}")
    else:
        print("\n下载失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
