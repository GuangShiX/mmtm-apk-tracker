"""完整提取 XAPK/APK、Unity Bundle 和可解码资源。"""

import hashlib
import json
import re
import shutil
import sqlite3
import sys
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath

import UnityPy
from UnityPy.enums import ClassIDType

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))

SPECIALIZED_TYPES = {
    "AnimationClip",
    "AudioClip",
    "Cubemap",
    "Font",
    "Material",
    "Mesh",
    "MonoBehaviour",
    "MovieTexture",
    "Shader",
    "Sprite",
    "TextAsset",
    "Texture2D",
    "Texture2DArray",
    "VideoClip",
}

JSON_SPECIALIZED_TYPES = {"AnimationClip", "Material", "MonoBehaviour", "Shader"}

# Prefab 结构类型（用于提取 UI 层级）
PREFAB_STRUCTURE_TYPES = {
    ClassIDType.GameObject,
    ClassIDType.Transform,
    ClassIDType.RectTransform,
    ClassIDType.CanvasRenderer,
    ClassIDType.Canvas,
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


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ManifestWriter:
    """流式写入全对象索引，避免百万对象 JSON 常驻内存。"""

    def __init__(self, path: Path, resume: bool = False):
        self.path = path
        self.temp_path = path.with_suffix(path.suffix + ".tmp")
        if not resume:
            self.temp_path.unlink(missing_ok=True)
        database_exists = self.temp_path.exists()
        self.connection = sqlite3.connect(self.temp_path)
        self.connection.executescript("""
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = NORMAL;
            PRAGMA wal_autocheckpoint = 1000;
            CREATE TABLE IF NOT EXISTS resources (
                resource_key TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                hash TEXT NOT NULL,
                size INTEGER NOT NULL,
                file TEXT NOT NULL,
                bundle TEXT NOT NULL,
                path_id INTEGER NOT NULL,
                container TEXT NOT NULL,
                raw_file TEXT NOT NULL,
                typetree_file TEXT NOT NULL,
                decoded_files TEXT NOT NULL,
                errors TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS resources_type_idx ON resources(type);
            CREATE INDEX IF NOT EXISTS resources_hash_idx ON resources(hash);
            CREATE INDEX IF NOT EXISTS resources_bundle_idx ON resources(bundle);
        """)
        self.pending = 0
        self.total = self.connection.execute("SELECT COUNT(*) FROM resources").fetchone()[0] if database_exists else 0

    def add(self, resource_key: str, record: dict) -> str:
        key = resource_key
        suffix = 1
        while True:
            try:
                self.connection.execute(
                    """INSERT INTO resources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        key,
                        record["type"],
                        record.get("hash", ""),
                        int(record.get("size", 0)),
                        record.get("file", ""),
                        record.get("bundle", ""),
                        int(record.get("path_id", 0)),
                        record.get("container", ""),
                        record.get("raw_file", ""),
                        record.get("typetree_file", ""),
                        json.dumps(record.get("decoded_files", []), ensure_ascii=False),
                        json.dumps(record.get("errors", []), ensure_ascii=False),
                    ),
                )
                break
            except sqlite3.IntegrityError:
                suffix += 1
                key = f"{resource_key}#{record['type']}:{record['path_id']}:{suffix}"
        self.pending += 1
        self.total += 1
        if self.pending >= 5000:
            self.connection.commit()
            self.pending = 0
        return key

    def source_count(self, bundle: str) -> int:
        return self.connection.execute(
            "SELECT COUNT(*) FROM resources WHERE bundle = ?", (bundle,)
        ).fetchone()[0]

    def delete_source(self, bundle: str):
        count = self.source_count(bundle)
        if count:
            self.connection.execute("DELETE FROM resources WHERE bundle = ?", (bundle,))
            self.connection.commit()
            self.total -= count

    def stats(self) -> dict:
        total = self.connection.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
        raw = self.connection.execute("SELECT COUNT(*) FROM resources WHERE raw_file <> ''").fetchone()[0]
        typetree = self.connection.execute(
            "SELECT COUNT(*) FROM resources WHERE typetree_file <> ''"
        ).fetchone()[0]
        decoded = self.connection.execute(
            "SELECT COALESCE(SUM(json_array_length(decoded_files)), 0) FROM resources"
        ).fetchone()[0]
        type_counts = dict(self.connection.execute(
            "SELECT type, COUNT(*) FROM resources GROUP BY type ORDER BY COUNT(*) DESC"
        ))
        fallback_counts = dict(self.connection.execute(
            """SELECT type, COUNT(*) FROM resources
               WHERE type IN ({}) AND json_array_length(decoded_files) = 0
               GROUP BY type ORDER BY COUNT(*) DESC""".format(
                   ",".join("?" for _ in SPECIALIZED_TYPES)
               ),
            tuple(sorted(SPECIALIZED_TYPES)),
        ))
        errors = []
        for bundle, path_id, type_name, raw_errors in self.connection.execute(
            "SELECT bundle, path_id, type, errors FROM resources WHERE errors <> '[]'"
        ):
            for error in json.loads(raw_errors):
                errors.append({
                    "bundle": bundle,
                    "path_id": path_id,
                    "type": type_name,
                    **error,
                })
        return {
            "total": total,
            "raw": raw,
            "typetree": typetree,
            "decoded": int(decoded),
            "type_counts": type_counts,
            "fallback_counts": fallback_counts,
            "errors": errors,
        }

    def close(self):
        self.connection.commit()
        self.total = self.connection.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
        self.connection.execute("ANALYZE")
        self.connection.commit()
        self.connection.close()
        self.path.unlink(missing_ok=True)
        self.temp_path.replace(self.path)
        return self.total


def _safe_component(value: str, fallback: str = "unnamed") -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value[:180] or fallback


def _safe_archive_path(name: str) -> Path:
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"不安全的压缩包路径: {name}")
    parts = [_safe_component(part) for part in pure.parts if part not in ("", ".")]
    if not parts:
        raise ValueError(f"无效的压缩包路径: {name}")
    return Path(*parts)


def _copy_stream(source, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with target.open("wb") as output:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


def _extract_zip(zip_path: Path, destination: Path, archive_label: str) -> list[dict]:
    entries = []
    with zipfile.ZipFile(zip_path) as archive:
        files = [info for info in archive.infolist() if not info.is_dir()]
        for index, info in enumerate(files, 1):
            rel_path = _safe_archive_path(info.filename)
            target = destination / rel_path
            with archive.open(info) as source:
                digest = _copy_stream(source, target)
            entries.append({
                "archive": archive_label,
                "path": info.filename,
                "file": target.as_posix(),
                "size": info.file_size,
                "compressed_size": info.compress_size,
                "crc32": f"{info.CRC:08x}",
                "sha256": digest,
            })
            if index % 1000 == 0:
                print(f"    {archive_label}: {index}/{len(files)}")
    return entries


def extract_package(xapk_path: Path, out_dir: Path) -> tuple[Path, list[Path], dict]:
    """完整展开外层包及所有嵌套 APK，返回原始目录和 Bundle 列表。"""
    raw_dir = out_dir / "raw"
    package_manifest_path = out_dir / "package_manifest.json"
    if package_manifest_path.exists() and raw_dir.exists():
        package_manifest = json.loads(package_manifest_path.read_text(encoding="utf-8"))
        bundles = sorted(raw_dir.rglob("*.bundle"))
        if len(bundles) == package_manifest.get("bundle_count", -1):
            print(f"  原始包已完整提取，复用 {len(bundles)} 个 Bundle")
            return raw_dir, bundles, package_manifest

    if raw_dir.exists():
        shutil.rmtree(raw_dir)
    archives_dir = raw_dir / "archives"
    outer_dir = raw_dir / "outer"
    apks_dir = raw_dir / "apks"
    entries: list[dict] = []
    nested_apks: list[Path] = []

    with zipfile.ZipFile(xapk_path) as outer:
        file_infos = [info for info in outer.infolist() if not info.is_dir()]
        apk_infos = [info for info in file_infos if info.filename.lower().endswith(".apk")]
        if apk_infos:
            for index, info in enumerate(file_infos, 1):
                rel_path = _safe_archive_path(info.filename)
                if info in apk_infos:
                    target = archives_dir / rel_path
                    nested_apks.append(target)
                else:
                    target = outer_dir / rel_path
                with outer.open(info) as source:
                    digest = _copy_stream(source, target)
                entries.append({
                    "archive": xapk_path.name,
                    "path": info.filename,
                    "file": target.relative_to(out_dir).as_posix(),
                    "size": info.file_size,
                    "compressed_size": info.compress_size,
                    "crc32": f"{info.CRC:08x}",
                    "sha256": digest,
                })
                if index % 100 == 0:
                    print(f"    外层包: {index}/{len(file_infos)}")
        else:
            direct_entries = _extract_zip(xapk_path, apks_dir / "main", xapk_path.name)
            for entry in direct_entries:
                entry["file"] = str(Path(entry["file"]).relative_to(out_dir)).replace("\\", "/")
            entries.extend(direct_entries)

    for index, apk_path in enumerate(nested_apks, 1):
        apk_name = _safe_component(apk_path.stem)
        print(f"  展开嵌套 APK [{index}/{len(nested_apks)}]: {apk_path.name}")
        nested_entries = _extract_zip(apk_path, apks_dir / apk_name, apk_path.name)
        for entry in nested_entries:
            entry["file"] = str(Path(entry["file"]).relative_to(out_dir)).replace("\\", "/")
        entries.extend(nested_entries)

    bundles = sorted(raw_dir.rglob("*.bundle"))
    package_manifest = {
        "source_file": xapk_path.name,
        "source_size": xapk_path.stat().st_size,
        "source_sha256": sha256_file(xapk_path),
        "entry_count": len(entries),
        "nested_apk_count": len(nested_apks),
        "bundle_count": len(bundles),
        "entries": entries,
    }
    package_manifest_path.write_text(
        json.dumps(package_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return raw_dir, bundles, package_manifest


def extract_catalog_from_raw(raw_dir: Path, out_dir: Path) -> dict | None:
    catalogs = sorted(raw_dir.rglob("catalog.json"))
    for source in catalogs:
        if "assets" not in {part.lower() for part in source.parts}:
            continue
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        shutil.copy2(source, out_dir / "catalog.json")
        return data
    return None


def find_unity_sources(raw_dir: Path) -> list[Path]:
    """查找 Bundle、场景和 Unity SerializedFile。"""
    sources = []
    for path in raw_dir.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if (
            path.suffix.lower() in (".bundle", ".assets", ".unity3d")
            or name in ("globalgamemanagers", "globalgamemanagers.assets", "maindata")
            or re.fullmatch(r"level\d+", name)
            or re.fullmatch(r"sharedassets\d+\.assets", name)
        ):
            sources.append(path)
    return sorted(set(sources))


def build_cab_index(bundle_files: list[Path]) -> dict[str, Path]:
    """建立 CAB 名称到 Bundle 的映射，用于跨 Bundle Sprite/Texture 依赖。"""
    index = {}
    for bundle_path in bundle_files:
        try:
            env = UnityPy.load(str(bundle_path))
        except Exception:
            continue
        for cab_name in env.cabs:
            if not str(cab_name).lower().endswith((".ress", ".resource")):
                index[str(cab_name).lower()] = bundle_path
    return index


def export_with_dependencies(
    source_path: Path,
    path_id: int,
    cab_index: dict[str, Path],
    tree: dict | None,
    export_dir: Path,
    rel_path: str,
) -> list[dict]:
    """加载对象声明的外部 CAB 后重试可用格式导出。"""
    env = UnityPy.load(str(source_path))
    obj = next(item for item in env.objects if item.path_id == path_id)
    for external in obj.assets_file.externals:
        name = str(getattr(external, "name", "")).lower()
        dependency = cab_index.get(name)
        if dependency is not None and dependency != source_path:
            env.load_file(str(dependency), is_dependency=True)
    data = obj.read()
    return export_specialized(obj, data, tree, export_dir, rel_path)


def parse_catalog_keys(catalog: dict) -> list[str]:
    """从 catalog 中提取所有资源的 address key"""
    ids = catalog.get("m_InternalIds", [])
    return [id for id in ids if id.startswith("Assets/")]


def extract_unity_data(raw_dir: Path, out_dir: Path):
    """从完整原始目录复制 Unity 项目数据到便捷目录。"""
    data_dir = out_dir / "unity_data"
    if data_dir.exists() and any(data_dir.iterdir()):
        print(f"  unity_data 已存在，跳过")
        return

    data_dir.mkdir(parents=True, exist_ok=True)
    print("  收集 Unity 项目数据...")
    copied = set()
    for source in raw_dir.rglob("*"):
        if not source.is_file():
            continue
        lowered = [part.lower() for part in source.parts]
        try:
            assets_index = lowered.index("assets")
        except ValueError:
            continue
        tail = lowered[assets_index:assets_index + 3]
        if tail != ["assets", "bin", "data"]:
            continue
        rel_parts = source.parts[assets_index + 3:]
        if not rel_parts:
            continue
        target = data_dir.joinpath(*rel_parts)
        if target in copied:
            target = data_dir / _safe_component(source.parents[3].name) / Path(*rel_parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.add(target)

    count = sum(1 for _ in data_dir.rglob("*") if _.is_file())
    print(f"  提取完成: {count} 个文件")


def _safe_asset_path(rel_path: str) -> Path:
    pure = PurePosixPath(rel_path.replace("\\", "/").lstrip("/"))
    parts = [_safe_component(part) for part in pure.parts if part not in ("", ".", "..")]
    return Path(*parts) if parts else Path("unnamed")


def _with_suffix(rel_path: Path, suffix: str, append: bool = False) -> Path:
    if append:
        return rel_path.with_name(rel_path.name + suffix)
    return rel_path.with_suffix(suffix)


def _unique_output(base_dir: Path, rel_path: Path, path_id: int) -> Path:
    path_tag = f"__{path_id}"
    if not rel_path.stem.endswith(path_tag):
        rel_path = rel_path.with_name(f"{rel_path.stem}{path_tag}{rel_path.suffix}")
    target = base_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _export_result(path: Path, base_dir: Path, kind: str) -> dict:
    return {
        "file": path.relative_to(base_dir).as_posix(),
        "hash": sha256_file(path)[:16],
        "size": path.stat().st_size,
        "kind": kind,
    }


def export_image(obj, data, out_dir: Path, rel_path: str) -> list[dict]:
    target = _unique_output(out_dir, _with_suffix(_safe_asset_path(rel_path), ".png"), obj.path_id)
    data.image.save(target)
    return [_export_result(target, out_dir, "image")]


def export_text_asset(obj, data, out_dir: Path, rel_path: str) -> list[dict]:
    raw = getattr(data, "m_Script", getattr(data, "script", b""))
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "surrogateescape")
    if raw[:1] == b"{" or raw[:1] == b"[":
        ext = ".json"
    elif raw[:5] == b"<?xml":
        ext = ".xml"
    elif b"," in raw[:200] and b"\n" in raw[:500]:
        ext = ".csv"
    else:
        ext = ".txt"
    target = _unique_output(out_dir, _with_suffix(_safe_asset_path(rel_path), ext, append=True), obj.path_id)
    target.write_bytes(raw)
    return [_export_result(target, out_dir, "text")]


def export_audio(obj, data, out_dir: Path, rel_path: str) -> list[dict]:
    samples = data.samples
    results = []
    for index, (name, audio_data) in enumerate(samples.items()):
        ext = Path(name).suffix or ".wav"
        base = _safe_asset_path(rel_path)
        if len(samples) > 1:
            base = base.parent / f"{base.name}_{index}_{_safe_component(Path(name).stem)}"
        target = _unique_output(out_dir, _with_suffix(base, ext), obj.path_id)
        target.write_bytes(audio_data)
        results.append(_export_result(target, out_dir, "audio"))
    return results


def export_font(obj, data, out_dir: Path, rel_path: str) -> list[dict]:
    raw = bytes(data.m_FontData)
    if raw[:4] == b"\x00\x01\x00\x00" or raw[:4] == b"true":
        ext = ".ttf"
    elif raw[:4] == b"OTTO":
        ext = ".otf"
    elif raw[:4] == b"wOFF":
        ext = ".woff"
    else:
        ext = ".ttf"
    target = _unique_output(out_dir, _with_suffix(_safe_asset_path(rel_path), ext), obj.path_id)
    target.write_bytes(raw)
    return [_export_result(target, out_dir, "font")]


def _json_safe(value, binary_limit: int = 4096):
    if isinstance(value, bytes):
        if len(value) <= binary_limit:
            return {"__bytes_hex__": value.hex()}
        return {"__binary__": True, "size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, dict):
        return {str(key): _json_safe(item, binary_limit) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        if len(value) > binary_limit and all(isinstance(item, int) and 0 <= item <= 255 for item in value):
            raw = bytes(value)
            return {"__byte_array__": True, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        return [_json_safe(item, binary_limit) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def export_typetree(obj, tree: dict, out_dir: Path, bundle_name: str, name: str) -> dict:
    asset_name = _safe_component(getattr(obj.assets_file, "name", "asset"))
    source_slug = _safe_component(str(Path(bundle_name).with_suffix("")).replace("\\", "__").replace("/", "__"))
    rel_path = Path(source_slug) / asset_name / obj.type.name
    rel_path /= f"{_safe_component(name)}__{obj.path_id}.json"
    target = _unique_output(out_dir, rel_path, obj.path_id)
    target.write_text(json.dumps(_json_safe(tree), ensure_ascii=False, indent=2), encoding="utf-8")
    return _export_result(target, out_dir, "typetree")


def export_raw_object(obj, out_dir: Path, bundle_name: str) -> dict:
    asset_name = _safe_component(getattr(obj.assets_file, "name", "asset"))
    source_slug = _safe_component(str(Path(bundle_name).with_suffix("")).replace("\\", "__").replace("/", "__"))
    rel_path = Path(source_slug) / asset_name / obj.type.name / f"{obj.path_id}.bin"
    target = out_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = obj.get_raw_data()
    digest = hashlib.sha256(raw).hexdigest()
    existing_matches = (
        target.exists()
        and target.stat().st_size == len(raw)
        and sha256_file(target) == digest
    )
    if not existing_matches:
        temp_target = target.with_name(target.name + ".tmp")
        temp_target.write_bytes(raw)
        temp_target.replace(target)
    return {
        "file": rel_path.as_posix(),
        "hash": digest[:16],
        "size": len(raw),
        "kind": "raw_object",
    }


def export_json_asset(obj, tree: dict, out_dir: Path, rel_path: str, suffix: str) -> list[dict]:
    target = _unique_output(out_dir, _with_suffix(_safe_asset_path(rel_path), suffix, append=True), obj.path_id)
    target.write_text(json.dumps(_json_safe(tree), ensure_ascii=False, indent=2), encoding="utf-8")
    return [_export_result(target, out_dir, "json")]


def export_specialized(obj, data, tree: dict | None, out_dir: Path, rel_path: str) -> list[dict]:
    type_name = obj.type.name
    if type_name in ("Sprite", "Texture2D", "Cubemap"):
        if type_name == "Texture2D":
            stream_data = getattr(data, "m_StreamData", None)
            inline_data = getattr(data, "image_data", b"")
            if not inline_data and (stream_data is None or getattr(stream_data, "size", 0) == 0):
                return []
        return export_image(obj, data, out_dir, rel_path)
    if type_name == "Texture2DArray":
        results = []
        for index, image in enumerate(data.images):
            rel = _with_suffix(_safe_asset_path(rel_path), f"_{index}.png", append=True)
            target = _unique_output(out_dir, rel, obj.path_id)
            image.save(target)
            results.append(_export_result(target, out_dir, "image"))
        return results
    if type_name == "TextAsset":
        return export_text_asset(obj, data, out_dir, rel_path)
    if type_name == "AudioClip":
        return export_audio(obj, data, out_dir, rel_path)
    if type_name == "Font":
        return export_font(obj, data, out_dir, rel_path)
    if type_name == "Mesh":
        target = _unique_output(out_dir, _with_suffix(_safe_asset_path(rel_path), ".obj"), obj.path_id)
        target.write_text(data.export(), encoding="utf-8", newline="")
        return [_export_result(target, out_dir, "mesh")]
    if type_name == "MovieTexture" and getattr(data, "m_MovieData", None):
        raw = bytes(data.m_MovieData)
        target = _unique_output(out_dir, _with_suffix(_safe_asset_path(rel_path), ".movie"), obj.path_id)
        target.write_bytes(raw)
        return [_export_result(target, out_dir, "video")]
    if type_name == "VideoClip":
        resource = data.m_ExternalResources
        from UnityPy.helpers.ResourceReader import get_resource_data
        raw = get_resource_data(resource.m_Source, obj.assets_file, resource.m_Offset, resource.m_Size)
        ext = Path(getattr(data, "m_OriginalPath", "")).suffix or ".video"
        target = _unique_output(out_dir, _with_suffix(_safe_asset_path(rel_path), ext), obj.path_id)
        target.write_bytes(raw)
        return [_export_result(target, out_dir, "video")]
    if type_name in ("AnimationClip", "Material", "MonoBehaviour", "Shader") and tree is not None:
        suffix = {
            "AnimationClip": ".anim.json",
            "Material": ".mat.json",
            "MonoBehaviour": ".json",
            "Shader": ".shader.json",
        }[type_name]
        return export_json_asset(obj, tree, out_dir, rel_path, suffix)
    return []


def object_name(obj, data=None) -> str:
    for candidate in (
        getattr(data, "m_Name", "") if data is not None else "",
        getattr(data, "name", "") if data is not None else "",
    ):
        if candidate:
            return str(candidate)
    try:
        name = obj.peek_name()
        if name:
            return str(name)
    except Exception:
        pass
    return f"pathid_{obj.path_id}"


def object_asset_path(container: str, bundle_name: str, type_name: str, name: str, path_id: int) -> str:
    if container:
        return _safe_asset_path(container).as_posix()
    return (
        Path("_by_bundle")
        / _safe_component(str(Path(bundle_name).with_suffix("")).replace("\\", "__").replace("/", "__"))
        / type_name
        / f"{_safe_component(name)}__{path_id}"
    ).as_posix()


def _read_vec2(val):
    if val is None: return None
    return {"x": round(val.x, 2), "y": round(val.y, 2)}

def _read_vec3(val):
    if val is None: return None
    return {"x": round(val.x, 2), "y": round(val.y, 2), "z": round(val.z, 2)}

def _read_color(val):
    if val is None: return None
    return {"r": round(val.r, 3), "g": round(val.g, 3), "b": round(val.b, 3), "a": round(val.a, 3)}


def _extract_image_data_tt(tree, obj_map, sprite_index=None):
    """从 typetree dict 提取 Image 组件数据: color, type, sprite name, 9-slice border"""
    result = {}
    c = tree.get('m_Color')
    if c and isinstance(c, dict):
        result["color"] = {"r": round(c.get("r", 0), 3), "g": round(c.get("g", 0), 3),
                           "b": round(c.get("b", 0), 3), "a": round(c.get("a", 0), 3)}
    if 'm_Type' in tree:
        result["type"] = int(tree['m_Type'])
    # 解析 m_Sprite PPtr → 读取 Sprite 获取 name/border/rect
    sp = tree.get('m_Sprite')
    if sp and isinstance(sp, dict) and sp.get('m_PathID', 0) != 0:
        sprite_pid = sp['m_PathID']
        # 优先从本 bundle 的 obj_map 读取（可获取 border/rect）
        if sprite_pid in obj_map:
            try:
                sprite = obj_map[sprite_pid].read()
                if hasattr(sprite, 'm_Name'):
                    result["spriteName"] = sprite.m_Name
                if hasattr(sprite, 'm_Border'):
                    b = sprite.m_Border
                    result["spriteBorder"] = {
                        "left": round(b.x, 1), "bottom": round(b.y, 1),
                        "right": round(b.z, 1), "top": round(b.w, 1)
                    }
                if hasattr(sprite, 'm_Rect'):
                    r = sprite.m_Rect
                    result["spriteRect"] = {
                        "x": round(r.x, 1), "y": round(r.y, 1),
                        "w": round(r.width, 1), "h": round(r.height, 1)
                    }
            except Exception:
                pass
        # 回退: 从全局 sprite_index 查找名称
        if 'spriteName' not in result and sprite_index and sprite_pid in sprite_index:
            result["spriteName"] = sprite_index[sprite_pid]
    if 'm_RaycastTarget' in tree:
        result["raycastTarget"] = bool(tree['m_RaycastTarget'])
    return result if result else None


def _extract_text_data_tt(tree):
    """从 typetree dict 提取 Text/OrtegaText 组件数据"""
    result = {}
    c = tree.get('m_Color')
    if c and isinstance(c, dict):
        result["color"] = {"r": round(c.get("r", 0), 3), "g": round(c.get("g", 0), 3),
                           "b": round(c.get("b", 0), 3), "a": round(c.get("a", 0), 3)}
    if 'm_Text' in tree:
        result["text"] = str(tree['m_Text']) if tree['m_Text'] else ""
    fd = tree.get('m_FontData')
    if fd and isinstance(fd, dict):
        for src, key, conv in [
            ('m_FontSize', 'fontSize', int), ('m_Alignment', 'alignment', int),
            ('m_BestFit', 'bestFit', bool), ('m_MinSize', 'minSize', int),
            ('m_MaxSize', 'maxSize', int), ('m_HorizontalOverflow', 'horizontalOverflow', int),
            ('m_VerticalOverflow', 'verticalOverflow', int),
        ]:
            if src in fd:
                result[key] = conv(fd[src])
        if 'm_LineSpacing' in fd:
            result["lineSpacing"] = round(float(fd['m_LineSpacing']), 2)
    return result if result else None


def _extract_outline_data_tt(tree):
    """从 typetree dict 提取 Outline/Shadow 组件数据"""
    result = {}
    c = tree.get('m_EffectColor')
    if c and isinstance(c, dict):
        result["color"] = {"r": round(c.get("r", 0), 3), "g": round(c.get("g", 0), 3),
                           "b": round(c.get("b", 0), 3), "a": round(c.get("a", 0), 3)}
    d = tree.get('m_EffectDistance')
    if d and isinstance(d, dict):
        result["distance"] = {"x": round(d.get("x", 0), 1), "y": round(d.get("y", 0), 1)}
    if 'm_Enabled' in tree:
        result["enabled"] = bool(tree['m_Enabled'])
    return result if result else None


def _extract_layout_element_data_tt(tree):
    """从 typetree dict 提取 LayoutElement 数据"""
    result = {}
    for src in ['m_MinWidth', 'm_MinHeight', 'm_PreferredWidth', 'm_PreferredHeight',
                'm_FlexibleWidth', 'm_FlexibleHeight']:
        if src in tree:
            val = tree[src]
            if val != -1:
                result[src.replace('m_', '')] = round(float(val), 1)
    return result if result else None


# script_id → 组件类型名 的缓存（运行时自动填充）
_script_id_cache: dict[bytes, str] = {}


def _identify_monobehaviour(comp_obj, obj_map):
    """识别 MonoBehaviour 组件类型并提取数据，使用 typetree + script_id 缓存"""
    st = comp_obj.serialized_type
    script_id = getattr(st, 'script_id', None)

    # 先查缓存
    if script_id and script_id in _script_id_cache:
        cached = _script_id_cache[script_id]
        if cached == '_skip':
            return cached, None
        # 有缓存类型名，读 typetree 提取数据
        tree = comp_obj.read_typetree()
        return cached, tree

    # 无缓存 → 读 typetree 判断类型
    if not (st and st.nodes):
        if script_id:
            _script_id_cache[script_id] = '_skip'
        return '_skip', None

    tree = comp_obj.read_typetree()
    keys = set(tree.keys())

    # 按字段签名识别 Unity 内置组件
    class_name = 'MonoBehaviour'  # 默认
    if 'm_Sprite' in keys and 'm_Type' in keys:
        class_name = 'Image'
    elif 'm_Texture' in keys and 'm_Color' in keys and 'm_Sprite' not in keys:
        class_name = 'RawImage'
    elif 'm_Text' in keys and 'm_FontData' in keys:
        class_name = 'Text'  # 也覆盖 OrtegaText
    elif 'm_EffectColor' in keys and 'm_EffectDistance' in keys:
        # Outline 和 Shadow 字段相同，用 m_UseGraphicAlpha 区分不了
        # 但 script_id 不同，先标记为 Outline，后续遇到第二个同结构的标记为 Shadow
        class_name = 'Outline'
    elif 'm_Alpha' in keys and 'm_Interactable' in keys and 'm_BlocksRaycasts' in keys:
        class_name = 'CanvasGroup'
    elif 'm_MinWidth' in keys and 'm_PreferredWidth' in keys:
        class_name = 'LayoutElement'
    elif 'm_Navigation' in keys and 'm_Transition' in keys:
        class_name = 'Button'

    if script_id:
        _script_id_cache[script_id] = class_name

    return class_name, tree


def _build_node(rt_obj, obj_map, sprite_index=None):
    """递归构建树形节点，包含 RectTransform 布局 + 完整组件数据"""
    data = rt_obj.read()
    node = {}

    # GameObject 名称 + 激活状态
    if hasattr(data, 'm_GameObject') and data.m_GameObject:
        try:
            go = data.m_GameObject.read()
            node["gameObject"] = go.m_Name
            node["active"] = bool(go.m_IsActive) if hasattr(go, 'm_IsActive') else True
        except Exception:
            pass

    # RectTransform 布局属性
    for attr, reader in [
        ('m_AnchoredPosition', _read_vec2), ('m_SizeDelta', _read_vec2),
        ('m_AnchorMin', _read_vec2), ('m_AnchorMax', _read_vec2),
        ('m_Pivot', _read_vec2), ('m_LocalScale', _read_vec3),
        ('m_LocalPosition', _read_vec3), ('m_LocalRotation', lambda v: {
            "x": round(v.x, 4), "y": round(v.y, 4), "z": round(v.z, 4), "w": round(v.w, 4)
        } if v else None),
    ]:
        if hasattr(data, attr):
            val = reader(getattr(data, attr))
            if val:
                node[attr] = val

    # 提取 GameObject 上的组件数据
    if hasattr(data, 'm_GameObject') and data.m_GameObject:
        try:
            go = data.m_GameObject.read()
            comp_types = []
            outline_count = 0
            for comp_ref in go.m_Components:
                comp_pid = comp_ref.path_id
                if comp_pid not in obj_map:
                    continue
                comp_obj = obj_map[comp_pid]
                comp_type = comp_obj.type.name

                if comp_type == 'MonoBehaviour':
                    try:
                        class_name, tree = _identify_monobehaviour(comp_obj, obj_map)
                        if class_name == '_skip' or tree is None:
                            comp_types.append('MonoBehaviour')
                            continue
                        comp_type = f"MonoBehaviour:{class_name}"

                        if class_name == 'Image':
                            d = _extract_image_data_tt(tree, obj_map, sprite_index)
                            if d: node["imageData"] = d
                        elif class_name == 'RawImage':
                            c = tree.get('m_Color')
                            if c and isinstance(c, dict):
                                node["rawImageData"] = {"color": {
                                    "r": round(c.get("r", 0), 3), "g": round(c.get("g", 0), 3),
                                    "b": round(c.get("b", 0), 3), "a": round(c.get("a", 0), 3)}}
                        elif class_name == 'Text':
                            d = _extract_text_data_tt(tree)
                            if d: node["textData"] = d
                        elif class_name == 'Outline':
                            d = _extract_outline_data_tt(tree)
                            if d:
                                # 同一 GO 上第一个是 Outline，第二个是 Shadow
                                if outline_count == 0:
                                    node["outlineData"] = d
                                else:
                                    node["shadowData"] = d
                                outline_count += 1
                        elif class_name == 'LayoutElement':
                            d = _extract_layout_element_data_tt(tree)
                            if d: node["layoutElementData"] = d
                        elif class_name == 'CanvasGroup':
                            if 'm_Alpha' in tree:
                                node["canvasGroupAlpha"] = round(float(tree['m_Alpha']), 3)
                    except Exception:
                        pass

                elif comp_type == 'CanvasGroup':
                    try:
                        cg = comp_obj.read()
                        if hasattr(cg, 'm_Alpha'):
                            node["canvasGroupAlpha"] = round(float(cg.m_Alpha), 3)
                    except Exception:
                        pass

                if comp_type not in ('RectTransform', 'Transform', 'CanvasRenderer'):
                    comp_types.append(comp_type)

            if comp_types:
                node["components"] = comp_types
        except Exception:
            pass

    # 递归子节点
    children = []
    if hasattr(data, 'm_Children'):
        for child_ref in data.m_Children:
            child_pid = child_ref.path_id
            if child_pid in obj_map:
                child_node = _build_node(obj_map[child_pid], obj_map, sprite_index)
                if child_node:
                    children.append(child_node)
    if children:
        node["children"] = children

    return node


def extract_prefab_hierarchy(env, bundle_name: str, out_dir: Path, sprite_index=None) -> list[dict]:
    """提取 bundle 中的 Prefab/GameObject 层级结构（树形 + 完整组件数据）"""
    # 建立 path_id → object 索引
    obj_map = {}
    for obj in env.objects:
        obj_map[obj.path_id] = obj

    # 找到所有 RectTransform，确定哪些是根节点（没有父引用指向它们）
    all_rt_ids = set()
    child_ids = set()
    for obj in env.objects:
        if obj.type == ClassIDType.RectTransform:
            all_rt_ids.add(obj.path_id)
            try:
                data = obj.read()
                if hasattr(data, 'm_Children'):
                    for child in data.m_Children:
                        child_ids.add(child.path_id)
            except Exception:
                continue

    root_ids = all_rt_ids - child_ids
    if not root_ids:
        # 回退: 找 Transform 根节点
        all_t_ids = set()
        for obj in env.objects:
            if obj.type == ClassIDType.Transform:
                all_t_ids.add(obj.path_id)
                try:
                    data = obj.read()
                    if hasattr(data, 'm_Children'):
                        for child in data.m_Children:
                            child_ids.add(child.path_id)
                except Exception:
                    continue
        root_ids = all_t_ids - child_ids

    if not root_ids:
        return []

    trees = []
    for root_id in root_ids:
        tree = _build_node(obj_map[root_id], obj_map, sprite_index)
        if tree:
            trees.append(tree)

    if not trees:
        return []

    # 如果只有一棵树，直接返回；多棵树合并到一个结构
    result = {"bundle": bundle_name}
    if len(trees) == 1:
        result["tree"] = trees[0]
    else:
        result["tree"] = trees

    return [result]


def extract_version(version: str, force: bool = False) -> Path | None:
    """完整解包指定版本，并验证每个 Bundle 对象都有原始导出。"""
    apk_path = find_apk(version)
    if not apk_path:
        print(f"未找到版本 {version} 的 APK 文件")
        return None

    out_dir = ROOT / CONFIG["dirs"]["extracted"] / version
    manifest_path = out_dir / "manifest.sqlite3"
    temp_manifest_path = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    report_path = out_dir / "extraction_report.json"
    if manifest_path.exists() and report_path.exists() and not force:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report.get("status") == "complete":
            print(f"版本 {version} 已完整解包，跳过（用 --force 强制重新解包）")
            return out_dir

    resume_manifest = temp_manifest_path.exists() and not force
    if force and out_dir.exists():
        shutil.rmtree(out_dir)
        resume_manifest = False
    elif (
        out_dir.exists()
        and report_path.exists()
        and not manifest_path.exists()
        and not resume_manifest
    ):
        print("检测到旧版 JSON manifest，保留 raw/objects_raw 并重建派生索引...")
        for name in ("assets", "objects_json", "prefabs"):
            path = out_dir / name
            if path.exists():
                shutil.rmtree(path)
        (out_dir / "manifest.json").unlink(missing_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"解包版本 {version}: {apk_path.name} ({apk_path.stat().st_size / 1024 / 1024:.0f} MB)")

    print("\nStep 1: 完整展开 XAPK/APK...")
    try:
        raw_dir, bundle_files, package_manifest = extract_package(apk_path, out_dir)
    except Exception as exc:
        report_path.write_text(json.dumps({
            "version": version,
            "status": "partial",
            "stage": "package",
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  完整展开失败: {type(exc).__name__}: {exc}")
        return None
    print(f"  包内文件: {package_manifest['entry_count']}")
    print(f"  嵌套 APK: {package_manifest['nested_apk_count']}")
    print(f"  Bundle: {len(bundle_files)}")
    source_files = find_unity_sources(raw_dir)
    print(f"  Unity 数据源: {len(source_files)}")

    print("\nStep 2: 提取 Addressable catalog...")
    catalog = extract_catalog_from_raw(raw_dir, out_dir)
    if catalog:
        print(f"  catalog 包含 {len(parse_catalog_keys(catalog))} 个资源地址")
    else:
        print("  未找到 catalog.json")

    print("\nStep 3: 收集 Unity 项目数据...")
    extract_unity_data(raw_dir, out_dir)

    print("\nStep 4: 导出全部 Unity 对象...")
    manifest = ManifestWriter(manifest_path, resume=resume_manifest)
    if resume_manifest:
        print(f"  恢复临时清单: 已索引 {manifest.total} 个对象")
    export_dir = out_dir / "assets"
    raw_objects_dir = out_dir / "objects_raw"
    typetree_dir = out_dir / "objects_json"
    prefab_dir = out_dir / "prefabs"
    for directory in (export_dir, raw_objects_dir, typetree_dir, prefab_dir):
        directory.mkdir(parents=True, exist_ok=True)

    total_objects = 0
    raw_exported = 0
    typetree_exported = 0
    specialized_exported = 0
    total_prefabs = 0
    type_counts: Counter = Counter()
    fallback_counts: Counter = Counter()
    current_errors: list[dict] = []
    prefab_errors: list[dict] = []
    bundle_failures: list[dict] = []
    prefab_bundles: list[Path] = []
    cab_index: dict[str, Path] | None = None
    resumed_sources = 0

    for index, bundle_path in enumerate(source_files, 1):
        bundle_name = bundle_path.relative_to(raw_dir).as_posix()
        try:
            env = UnityPy.load(str(bundle_path))
        except Exception as exc:
            bundle_failures.append({
                "bundle": bundle_path.relative_to(out_dir).as_posix(),
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue

        objects = list(env.objects)
        if any(obj.type in PREFAB_STRUCTURE_TYPES for obj in objects):
            prefab_bundles.append(bundle_path)
        existing_count = manifest.source_count(bundle_name) if resume_manifest else 0
        if resume_manifest and existing_count == len(objects):
            resumed_sources += 1
            if index % 200 == 0:
                print(
                    f"  [{index}/{len(source_files)}] 已恢复 {resumed_sources} 个数据源, "
                    f"清单对象 {manifest.total}"
                )
            continue
        if existing_count:
            print(
                f"  重做未完整数据源: {bundle_name} "
                f"({existing_count}/{len(objects)} 个对象)"
            )
            manifest.delete_source(bundle_name)

        container_map = {}
        if hasattr(env, "container"):
            for container_path, obj_info in env.container.items():
                if hasattr(obj_info, "path_id"):
                    container_map[obj_info.path_id] = container_path

        for obj in objects:
            total_objects += 1
            type_name = obj.type.name
            type_counts[type_name] += 1
            container = container_map.get(obj.path_id, "")
            record = {
                "container": container,
                "type": type_name,
                "path_id": obj.path_id,
                "bundle": bundle_name,
                "asset_file": getattr(obj.assets_file, "name", ""),
                "decoded_files": [],
                "errors": [],
            }

            try:
                raw_info = export_raw_object(obj, raw_objects_dir, bundle_name)
                raw_exported += 1
                record["raw_file"] = f"objects_raw/{raw_info['file']}"
                record["hash"] = raw_info["hash"]
                record["size"] = raw_info["size"]
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                record["errors"].append({"stage": "raw", "error": message})
                current_errors.append({"bundle": bundle_name, "path_id": obj.path_id, "type": type_name, "stage": "raw", "error": message})

            name = object_name(obj)
            tree = None
            serialized_type = getattr(obj, "serialized_type", None)
            if serialized_type is not None and getattr(serialized_type, "nodes", None):
                try:
                    tree = obj.read_typetree()
                    tree_info = export_typetree(obj, tree, typetree_dir, bundle_name, name)
                    record["typetree_file"] = f"objects_json/{tree_info['file']}"
                    typetree_exported += 1
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    record["errors"].append({"stage": "typetree", "error": message})
                    current_errors.append({"bundle": bundle_name, "path_id": obj.path_id, "type": type_name, "stage": "typetree", "error": message})

            if type_name in SPECIALIZED_TYPES:
                try:
                    if type_name in JSON_SPECIALIZED_TYPES:
                        if tree is not None:
                            name = str(tree.get("m_Name") or name)
                        data = None
                    else:
                        data = obj.read()
                        name = object_name(obj, data)
                    rel_path = object_asset_path(container, bundle_name, type_name, name, obj.path_id)
                    decoded = export_specialized(obj, data, tree, export_dir, rel_path)
                    record["decoded_files"] = [f"assets/{item['file']}" for item in decoded]
                    specialized_exported += len(decoded)
                    if not decoded:
                        fallback_counts[type_name] += 1
                except Exception as exc:
                    retry_error = exc
                    if isinstance(exc, FileNotFoundError) and bundle_path.suffix.lower() == ".bundle":
                        try:
                            if cab_index is None:
                                print("  建立跨 Bundle CAB 依赖索引...")
                                cab_index = build_cab_index(bundle_files)
                            rel_path = object_asset_path(container, bundle_name, type_name, name, obj.path_id)
                            decoded = export_with_dependencies(
                                bundle_path, obj.path_id, cab_index, tree, export_dir, rel_path
                            )
                            record["decoded_files"] = [f"assets/{item['file']}" for item in decoded]
                            specialized_exported += len(decoded)
                            retry_error = None
                        except Exception as dependency_exc:
                            retry_error = dependency_exc
                    if retry_error is not None:
                        message = f"{type(retry_error).__name__}: {retry_error}"
                        record["errors"].append({"stage": "specialized", "error": message})
                        current_errors.append({"bundle": bundle_name, "path_id": obj.path_id, "type": type_name, "stage": "specialized", "error": message})
                        fallback_counts[type_name] += 1

            logical_key = object_asset_path(container, bundle_name, type_name, name, obj.path_id)
            record["file"] = (
                record["decoded_files"][0]
                if record["decoded_files"]
                else record.get("typetree_file", record.get("raw_file", ""))
            )
            manifest.add(logical_key, record)

        if index % 200 == 0:
            print(
                f"  [{index}/{len(source_files)}] 对象 {total_objects}, "
                f"原始 {raw_exported}, 可用文件 {specialized_exported}, 错误 {len(current_errors)}"
            )

    print(f"\nStep 5: 提取 Prefab 层级 ({len(prefab_bundles)} 个 Bundle)...")
    _script_id_cache.clear()
    for bundle_path in prefab_bundles:
        bundle_name = bundle_path.name
        try:
            env = UnityPy.load(str(bundle_path))
            sprite_index = {}
            for obj in env.objects:
                if obj.type == ClassIDType.Sprite:
                    try:
                        sprite = obj.read()
                        if getattr(sprite, "m_Name", ""):
                            sprite_index[obj.path_id] = sprite.m_Name
                    except Exception:
                        continue
            for hierarchy in extract_prefab_hierarchy(env, bundle_name, prefab_dir, sprite_index):
                if hierarchy.get("tree"):
                    prefab_name = _safe_component(bundle_name.replace("/", "__").replace("\\", "__"))
                    prefab_path = prefab_dir / f"{Path(prefab_name).stem}.json"
                    prefab_path.write_text(
                        json.dumps(hierarchy, ensure_ascii=False, default=str, indent=1),
                        encoding="utf-8",
                    )
                    total_prefabs += 1
        except Exception as exc:
            prefab_errors.append({"bundle": bundle_name, "stage": "prefab", "error": f"{type(exc).__name__}: {exc}"})

    manifest_stats = manifest.stats()
    total_objects = manifest_stats["total"]
    raw_exported = manifest_stats["raw"]
    typetree_exported = manifest_stats["typetree"]
    specialized_exported = manifest_stats["decoded"]
    type_counts = Counter(manifest_stats["type_counts"])
    fallback_counts = Counter(manifest_stats["fallback_counts"])
    errors = manifest_stats["errors"] + prefab_errors
    manifest_rows = manifest.close()
    raw_complete = (
        bool(source_files)
        and bool(bundle_files)
        and total_objects > 0
        and not bundle_failures
        and raw_exported == total_objects
        and manifest_rows == total_objects
    )
    report = {
        "version": version,
        "status": "complete" if raw_complete else "partial",
        "source_file": apk_path.name,
        "source_sha256": package_manifest["source_sha256"],
        "package_entries": package_manifest["entry_count"],
        "nested_apks": package_manifest["nested_apk_count"],
        "bundles": len(bundle_files),
        "unity_sources": len(source_files),
        "bundle_failures": bundle_failures,
        "objects_total": total_objects,
        "objects_raw_exported": raw_exported,
        "manifest_rows": manifest_rows,
        "objects_typetree_exported": typetree_exported,
        "specialized_files_exported": specialized_exported,
        "prefabs_exported": total_prefabs,
        "raw_object_coverage": (raw_exported / total_objects) if total_objects else 1.0,
        "type_counts": dict(type_counts.most_common()),
        "fallback_counts": dict(fallback_counts.most_common()),
        "errors": errors,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n解包结果:")
    print(f"  Bundle: {len(bundle_files)}")
    print(f"  Unity 数据源: {len(source_files)}")
    print(f"  Unity 对象: {total_objects}")
    print(f"  原始对象: {raw_exported}/{total_objects}")
    print(f"  TypeTree JSON: {typetree_exported}")
    print(f"  可用格式文件: {specialized_exported}")
    print(f"  Prefab: {total_prefabs}")
    print(f"  Bundle 失败: {len(bundle_failures)}")
    print(f"  对象/派生错误: {len(errors)}")
    print(f"  完整性报告: {report_path}")
    if not raw_complete:
        print("  状态: partial（原始对象覆盖率不足）")
        return None
    print("  状态: complete（包文件与 Unity 原始对象均已保留）")
    return out_dir


def reextract_prefabs(version: str) -> None:
    """使用已保留的原始 Bundle 重新生成 Prefab 层级。"""
    out_dir = ROOT / CONFIG["dirs"]["extracted"] / version
    prefab_dir = out_dir / "prefabs"
    bundle_files = sorted((out_dir / "raw").rglob("*.bundle")) if (out_dir / "raw").exists() else []
    if not bundle_files:
        apk_path = find_apk(version)
        if not apk_path:
            print(f"未找到版本 {version} 的 APK 文件")
            return
        _, bundle_files, _ = extract_package(apk_path, out_dir)

    if prefab_dir.exists():
        shutil.rmtree(prefab_dir)
    prefab_dir.mkdir(parents=True, exist_ok=True)
    _script_id_cache.clear()
    total_prefabs = 0
    errors = 0
    print(f"提取 Prefab 层级 ({len(bundle_files)} 个 Bundle)...")

    for index, bundle_path in enumerate(bundle_files, 1):
        bundle_name = bundle_path.name
        try:
            env = UnityPy.load(str(bundle_path))
        except Exception:
            errors += 1
            continue
        if not any(obj.type in PREFAB_STRUCTURE_TYPES for obj in env.objects):
            continue
        sprite_index = {}
        for obj in env.objects:
            if obj.type == ClassIDType.Sprite:
                try:
                    sprite = obj.read()
                    if getattr(sprite, "m_Name", ""):
                        sprite_index[obj.path_id] = sprite.m_Name
                except Exception:
                    continue
        try:
            for hierarchy in extract_prefab_hierarchy(env, bundle_name, prefab_dir, sprite_index):
                if hierarchy.get("tree"):
                    prefab_path = prefab_dir / f"{Path(bundle_name).stem}.json"
                    prefab_path.write_text(
                        json.dumps(hierarchy, ensure_ascii=False, default=str, indent=1),
                        encoding="utf-8",
                    )
                    total_prefabs += 1
        except Exception:
            errors += 1
        if index % 200 == 0:
            print(f"  [{index}/{len(bundle_files)}] {total_prefabs} Prefab, {errors} 错误")

    print("\nPrefab 重新提取完成:")
    print(f"  Prefab: {total_prefabs}")
    print(f"  错误: {errors}")
    print(f"  输出目录: {prefab_dir}")


def main():
    if len(sys.argv) < 2:
        print("用法: python extract.py <version> [--force] [--prefabs-only]")
        print("示例: python extract.py 4.10.0")
        print("      python extract.py 4.10.0 --force         # 强制重新解包")
        print("      python extract.py 4.10.0 --prefabs-only  # 仅重新提取 Prefab")
        apks_dir = ROOT / CONFIG["dirs"]["apks"]
        if apks_dir.exists():
            files = list(apks_dir.glob("*.*apk*"))
            if files:
                print(f"\n可用的 APK 文件:")
                for f in sorted(files):
                    print(f"  {f.name} ({f.stat().st_size / 1024 / 1024:.0f} MB)")
        sys.exit(1)

    version = sys.argv[1]
    if "--prefabs-only" in sys.argv:
        reextract_prefabs(version)
    else:
        force = "--force" in sys.argv
        extract_version(version, force=force)


if __name__ == "__main__":
    main()
