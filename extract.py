"""AssetBundle 解包模块 — 解压 XAPK，用 UnityPy 提取资源并保留原始路径"""

import hashlib
import json
import sys
import zipfile
import tempfile
import shutil
from pathlib import Path

import UnityPy
from UnityPy.enums import ClassIDType

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

# 需要导出的资源类型
EXPORT_TYPES = {
    ClassIDType.Sprite,
    ClassIDType.Texture2D,
    ClassIDType.TextAsset,
    ClassIDType.MonoBehaviour,
}


def find_apk(version: str) -> Path | None:
    """根据版本号找到对应的 APK/XAPK 文件"""
    apks_dir = ROOT / CONFIG["dirs"]["apks"]
    for pattern in [f"*@{version}.*apk*", f"*{version}*"]:
        matches = [f for f in apks_dir.glob(pattern) if f.suffix in (".apk", ".xapk")]
        if matches:
            return matches[0]
    return None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def extract_xapk_manifest(xapk_path: Path) -> dict | None:
    """从 XAPK 中读取 manifest.json"""
    try:
        with zipfile.ZipFile(xapk_path) as zf:
            if "manifest.json" in zf.namelist():
                return json.loads(zf.read("manifest.json"))
    except Exception:
        pass
    return None


def extract_catalog(xapk_path: Path, out_dir: Path) -> dict | None:
    """从 XAPK 中提取 Addressable catalog.json"""
    catalog_out = out_dir / "catalog.json"
    if catalog_out.exists():
        return json.loads(catalog_out.read_text(encoding="utf-8"))

    try:
        with zipfile.ZipFile(xapk_path) as xapk:
            # 找 UnityDataAssetPack.apk
            unity_apk_name = None
            for name in xapk.namelist():
                if "Unity" in name and name.endswith(".apk"):
                    unity_apk_name = name
                    break
            if not unity_apk_name:
                # 可能是普通 APK
                if "assets/aa/catalog.json" in xapk.namelist():
                    data = xapk.read("assets/aa/catalog.json")
                    catalog_out.write_bytes(data)
                    return json.loads(data)
                return None

            with xapk.open(unity_apk_name) as inner_f:
                with zipfile.ZipFile(inner_f) as apk:
                    if "assets/aa/catalog.json" in apk.namelist():
                        data = apk.read("assets/aa/catalog.json")
                        catalog_out.write_bytes(data)
                        return json.loads(data)
    except Exception as e:
        print(f"  提取 catalog 失败: {e}")
    return None


def parse_catalog_keys(catalog: dict) -> list[str]:
    """从 catalog 中提取所有资源的 address key（即 m_InternalIds 中 Assets/ 开头的）"""
    ids = catalog.get("m_InternalIds", [])
    return [id for id in ids if id.startswith("Assets/")]


def extract_bundles_from_xapk(xapk_path: Path, work_dir: Path) -> Path:
    """从 XAPK 中提取所有 AssetBundle 到工作目录"""
    bundles_dir = work_dir / "bundles"
    if bundles_dir.exists() and any(bundles_dir.glob("*.bundle")):
        count = len(list(bundles_dir.glob("*.bundle")))
        print(f"  已提取 {count} 个 bundle，跳过")
        return bundles_dir

    bundles_dir.mkdir(parents=True, exist_ok=True)
    print(f"  从 XAPK 提取 AssetBundle...")

    with zipfile.ZipFile(xapk_path) as xapk:
        # 找 UnityDataAssetPack.apk
        unity_apk_name = None
        for name in xapk.namelist():
            if "Unity" in name and name.endswith(".apk"):
                unity_apk_name = name
                break

        if unity_apk_name:
            with xapk.open(unity_apk_name) as inner_f:
                with zipfile.ZipFile(inner_f) as apk:
                    bundle_names = [n for n in apk.namelist()
                                    if n.startswith("assets/aa/Android/") and n.endswith(".bundle")]
                    print(f"  找到 {len(bundle_names)} 个 bundle")
                    for i, name in enumerate(bundle_names):
                        out_path = bundles_dir / Path(name).name
                        out_path.write_bytes(apk.read(name))
                        if (i + 1) % 500 == 0:
                            print(f"    提取进度: {i+1}/{len(bundle_names)}")
        else:
            # 普通 APK，直接找 bundle
            bundle_names = [n for n in xapk.namelist()
                            if n.startswith("assets/aa/Android/") and n.endswith(".bundle")]
            print(f"  找到 {len(bundle_names)} 个 bundle")
            for i, name in enumerate(bundle_names):
                out_path = bundles_dir / Path(name).name
                out_path.write_bytes(xapk.read(name))
                if (i + 1) % 500 == 0:
                    print(f"    提取进度: {i+1}/{len(bundle_names)}")

    count = len(list(bundles_dir.glob("*.bundle")))
    print(f"  提取完成: {count} 个 bundle")
    return bundles_dir


def export_object(obj, data, out_dir: Path, container: str) -> dict | None:
    """导出单个资源对象，返回 manifest 条目"""
    type_name = obj.type.name

    if container:
        rel_path = container.lstrip("/")
    else:
        rel_path = f"_no_container/{type_name}/{data.name}" if hasattr(data, "name") and data.name else None
        if not rel_path:
            return None

    info = {
        "container": container or "",
        "type": type_name,
        "path_id": obj.path_id,
    }

    try:
        if obj.type in {ClassIDType.Sprite, ClassIDType.Texture2D}:
            img = data.image
            out_path = out_dir / f"{rel_path}.png"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(out_path)
            info["hash"] = sha256_bytes(out_path.read_bytes())
            info["file"] = str(out_path.relative_to(out_dir))

        elif obj.type == ClassIDType.TextAsset:
            raw = data.script
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            ext = ".json" if raw[:1] == b"{" else ".xml" if raw[:5] == b"<?xml" else ".txt"
            out_path = out_dir / f"{rel_path}{ext}"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(raw)
            info["hash"] = sha256_bytes(raw)
            info["file"] = str(out_path.relative_to(out_dir))

        elif obj.type == ClassIDType.MonoBehaviour:
            if data.serialized_type and data.serialized_type.nodes:
                tree = obj.read_typetree()
                raw = json.dumps(tree, ensure_ascii=False, default=str).encode("utf-8")
            else:
                raw = data.raw_data
            out_path = out_dir / f"{rel_path}.json"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(raw)
            info["hash"] = sha256_bytes(raw)
            info["file"] = str(out_path.relative_to(out_dir))

        else:
            return None

    except Exception as e:
        info["error"] = str(e)
        return info if "hash" in info else None

    return info


def extract_version(version: str) -> Path | None:
    """解包指定版本的 APK/XAPK，返回输出目录"""
    apk_path = find_apk(version)
    if not apk_path:
        print(f"未找到版本 {version} 的 APK 文件")
        return None

    out_dir = ROOT / CONFIG["dirs"]["extracted"] / version
    manifest_path = out_dir / "manifest.json"

    if manifest_path.exists():
        print(f"版本 {version} 已解包，跳过（删除 {out_dir} 可重新解包）")
        return out_dir

    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = out_dir / "_work"
    work_dir.mkdir(exist_ok=True)

    print(f"解包版本 {version}: {apk_path.name} ({apk_path.stat().st_size / 1024 / 1024:.0f} MB)")

    # Step 1: 提取 catalog
    print("\nStep 1: 提取 Addressable catalog...")
    catalog = extract_catalog(apk_path, out_dir)
    if catalog:
        asset_keys = parse_catalog_keys(catalog)
        print(f"  catalog 包含 {len(asset_keys)} 个资源地址")
    else:
        print("  未找到 catalog.json")

    # Step 2: 提取 AssetBundle 文件
    print("\nStep 2: 提取 AssetBundle 文件...")
    bundles_dir = extract_bundles_from_xapk(apk_path, work_dir)

    # Step 3: UnityPy 解包
    print("\nStep 3: UnityPy 解包资源...")
    manifest = {}
    export_dir = out_dir / "assets"
    total_exported = 0
    errors = 0

    bundle_files = sorted(bundles_dir.glob("*.bundle"))
    total_bundles = len(bundle_files)

    for i, bundle_path in enumerate(bundle_files):
        try:
            env = UnityPy.load(str(bundle_path))
        except Exception as e:
            errors += 1
            continue

        # 建立 path_id → container 映射
        container_map = {}
        if hasattr(env, "container"):
            for container_path, obj_info in env.container.items():
                if hasattr(obj_info, "path_id"):
                    container_map[obj_info.path_id] = container_path

        for obj in env.objects:
            if obj.type not in EXPORT_TYPES:
                continue

            container = container_map.get(obj.path_id, "")

            try:
                data = obj.read()
            except Exception:
                errors += 1
                continue

            info = export_object(obj, data, export_dir, container)
            if info and "file" in info:
                manifest[info["file"]] = info
                total_exported += 1

        if (i + 1) % 200 == 0:
            print(f"  [{i+1}/{total_bundles}] 已导出 {total_exported} 资源, {errors} 错误")

    # Step 4: 保存 manifest
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 统计
    type_counts = {}
    for info in manifest.values():
        t = info.get("type", "Unknown")
        type_counts[t] = type_counts.get(t, 0) + 1

    print(f"\n解包完成:")
    print(f"  版本: {version}")
    print(f"  Bundle 数: {total_bundles}")
    print(f"  导出资源: {total_exported}")
    print(f"  错误: {errors}")
    print(f"  按类型:")
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"    {t}: {c}")
    print(f"  输出目录: {out_dir}")

    # Step 5: 清理工作目录
    if work_dir.exists():
        size_mb = sum(f.stat().st_size for f in work_dir.rglob("*") if f.is_file()) / 1024 / 1024
        print(f"\n清理工作目录 ({size_mb:.0f} MB)...")
        shutil.rmtree(work_dir)

    return out_dir


def main():
    if len(sys.argv) < 2:
        print("用法: python extract.py <version>")
        print("示例: python extract.py 4.9.0")
        apks_dir = ROOT / CONFIG["dirs"]["apks"]
        if apks_dir.exists():
            files = list(apks_dir.glob("*.*apk*"))
            if files:
                print(f"\n可用的 APK 文件:")
                for f in sorted(files):
                    print(f"  {f.name} ({f.stat().st_size / 1024 / 1024:.0f} MB)")
        sys.exit(1)

    version = sys.argv[1]
    extract_version(version)


if __name__ == "__main__":
    main()
