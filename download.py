"""APK 下载模块 — 调用 apkeep 从 APKPure 或 Google Play 下载 MementoMori APK"""

import json
import subprocess
import sys
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def version_key(version: str) -> tuple[int, ...]:
    """把 4.10.0 这类版本号转成可排序 tuple。"""
    return tuple(int(part) for part in version.split(".") if part.isdigit())


def verify_zip(path: Path, deep: bool = False) -> bool:
    """验证 ZIP 目录；deep=True 时读取所有条目并校验 CRC。"""
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        with zipfile.ZipFile(path) as archive:
            if not archive.infolist():
                return False
            return archive.testzip() is None if deep else True
    except (OSError, zipfile.BadZipFile, EOFError):
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
    if not apkeep.exists():
        print(f"未找到 apkeep: {apkeep}")
        return []

    try:
        result = subprocess.run(
            [str(apkeep), "-a", pkg, "-d", "apk-pure", "--list-versions", "."],
            capture_output=True, text=True, timeout=30,
        )
        output = "\n".join([result.stdout, result.stderr])
        versions = set(re.findall(r"\b\d+\.\d+\.\d+\b", output))
        return sorted(versions, key=version_key)
    except Exception as e:
        print(f"查询版本列表失败: {e}")
    return []


def list_local_versions(complete_only: bool = False) -> list[str]:
    """列出本地已下载的所有版本号"""
    apks_dir = ROOT / CONFIG["dirs"]["apks"]
    if not apks_dir.exists():
        return []
    versions = []
    for f in list(apks_dir.glob("*.apk")) + list(apks_dir.glob("*.xapk")):
        if complete_only and not verify_zip(f):
            continue
        v = get_apk_version(f)
        if v:
            versions.append(v)
    return sorted(set(versions), key=version_key)


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
    print("（游戏 XAPK 通常超过 1 GB，请耐心等待...）")

    retries = max(1, int(CONFIG.get("download_retries", 3)))
    timeout = max(60, int(CONFIG.get("download_timeout_seconds", 1800)))
    deep_verify = bool(CONFIG.get("verify_download_crc", True))
    apk_file = None
    for attempt in range(1, retries + 1):
        print(f"下载尝试: {attempt}/{retries}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"下载超时（{timeout // 60} 分钟）")
            result = None
        if result is not None:
            if result.stdout.strip():
                print(result.stdout.strip())
            if result.stderr.strip():
                print(result.stderr.strip())
            if result.returncode != 0:
                print(f"下载失败 (exit code {result.returncode})")

        apk_file = find_local_apk(version) if version else None
        if apk_file is None:
            downloaded = sorted(
                list(apks_dir.glob(f"{pkg}*")),
                key=lambda file: file.stat().st_mtime,
                reverse=True,
            )
            apk_file = downloaded[0] if downloaded else None
        if apk_file is None:
            print("未找到下载的文件")
            continue

        print("验证下载包完整性（CRC）..." if deep_verify else "验证下载包目录...")
        if verify_zip(apk_file, deep=deep_verify):
            break
        size_mb = apk_file.stat().st_size / 1024 / 1024
        print(f"下载包校验失败 ({size_mb:.1f} MB)，删除后重试")
        apk_file.unlink(missing_ok=True)
        apk_file = None

    if apk_file is None:
        print(f"下载失败，已尝试 {retries} 次")
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
