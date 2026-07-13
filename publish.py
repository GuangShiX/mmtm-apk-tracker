"""Publish decoded image assets to Alibaba Cloud OSS.

Objects are addressed by their SHA-256 digest. A version manifest is uploaded
after all objects succeed, and ``latest.json`` is updated last. This keeps CDN
caches immutable and prevents consumers from seeing a partially published
version.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import mimetypes
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlparse

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
ENV_FILE = ROOT / ".env"
IMAGE_TYPES = ("Sprite", "Texture2D")


def _read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if value.startswith(('"', "'")):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        values[name] = value
    return values


def _env(name: str, default: str = "", file_values: dict[str, str] | None = None) -> str:
    if name in os.environ:
        return os.environ[name]
    return (file_values or {}).get(name, default)


def _as_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _clean_prefix(value: str) -> str:
    return value.strip().strip("/")


@dataclass(frozen=True)
class OssSettings:
    enabled: bool
    endpoint: str
    bucket_name: str
    prefix: str
    cdn_base_url: str
    access_key_id: str
    access_key_secret: str
    security_token: str = ""

    @property
    def public_base_url(self) -> str:
        if self.cdn_base_url:
            return self.cdn_base_url.rstrip("/")
        parsed = urlparse(self.endpoint)
        if parsed.scheme and parsed.netloc and self.bucket_name:
            return f"{parsed.scheme}://{self.bucket_name}.{parsed.netloc}"
        return ""

    def key(self, *parts: str) -> str:
        values = [self.prefix, *(part.strip("/") for part in parts)]
        return "/".join(part for part in values if part)

    def validate(self, require_credentials: bool = True) -> None:
        missing = []
        if not self.endpoint:
            missing.append("OSS_ENDPOINT")
        if not self.bucket_name:
            missing.append("OSS_BUCKET")
        if require_credentials and not self.access_key_id:
            missing.append("OSS_ACCESS_KEY_ID")
        if require_credentials and not self.access_key_secret:
            missing.append("OSS_ACCESS_KEY_SECRET")
        if missing:
            raise ValueError(f"缺少 OSS 配置: {', '.join(missing)}")
        if not self.endpoint.startswith("https://"):
            raise ValueError("OSS_ENDPOINT 必须使用 https://")


def load_oss_settings(config: dict[str, Any] | None = None) -> OssSettings:
    config = config or CONFIG
    configured = config.get("oss", {})
    file_values = _read_env_file()
    return OssSettings(
        enabled=_as_bool(
            _env("MMTM_OSS_ENABLED", str(configured.get("enabled", False)), file_values)
        ),
        endpoint=_env("OSS_ENDPOINT", str(configured.get("endpoint", "")), file_values).rstrip("/"),
        bucket_name=_env("OSS_BUCKET", str(configured.get("bucket", "")), file_values),
        prefix=_clean_prefix(
            _env("OSS_PREFIX", str(configured.get("prefix", "mementomori")), file_values)
        ),
        cdn_base_url=_env(
            "CDN_BASE_URL", str(configured.get("cdn_base_url", "")), file_values
        ).rstrip("/"),
        access_key_id=_env("OSS_ACCESS_KEY_ID", file_values=file_values),
        access_key_secret=_env("OSS_ACCESS_KEY_SECRET", file_values=file_values),
        security_token=_env("OSS_SECURITY_TOKEN", file_values=file_values),
    )


def create_bucket(settings: OssSettings):
    settings.validate()
    try:
        import oss2
    except ImportError as exc:
        raise RuntimeError("缺少 oss2，请先运行 pip install -r requirements.txt") from exc

    if settings.security_token:
        auth = oss2.StsAuth(
            settings.access_key_id,
            settings.access_key_secret,
            settings.security_token,
        )
    else:
        auth = oss2.Auth(settings.access_key_id, settings.access_key_secret)
    return oss2.Bucket(auth, settings.endpoint, settings.bucket_name)


def _object_exists(bucket, key: str) -> bool:
    return bool(bucket.object_exists(key))


def get_remote_latest(settings: OssSettings, bucket=None) -> dict[str, Any] | None:
    settings.validate()
    bucket = bucket or create_bucket(settings)
    key = settings.key("manifests", "latest.json")
    if not _object_exists(bucket, key):
        return None
    payload = bucket.get_object(key).read()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"OSS 上的 {key} 不是有效 JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("version"), str):
        raise RuntimeError(f"OSS 上的 {key} 缺少 version")
    return value


def get_remote_latest_version(settings: OssSettings, bucket=None) -> str | None:
    latest = get_remote_latest(settings, bucket=bucket)
    return latest["version"] if latest else None


def _resolve_version_dir(version: str, config: dict[str, Any] | None = None) -> Path:
    config = config or CONFIG
    return ROOT / config["dirs"]["extracted"] / version


def validate_complete_extraction(version_dir: Path, version: str) -> dict[str, Any]:
    report_path = version_dir / "extraction_report.json"
    package_manifest = version_dir / "package_manifest.json"
    database = version_dir / "manifest.sqlite3"
    missing = [
        str(path.name)
        for path in (report_path, package_manifest, database)
        if not path.is_file()
    ]
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


def _iter_image_rows(database: Path) -> Iterable[tuple[str, str, str, str, str]]:
    connection = sqlite3.connect(database)
    try:
        placeholders = ",".join("?" for _ in IMAGE_TYPES)
        query = f"""
            SELECT resource_key, type, hash, file, container
            FROM resources
            WHERE type IN ({placeholders})
            ORDER BY resource_key
        """
        yield from connection.execute(query, IMAGE_TYPES)
    finally:
        connection.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset_category(value: str) -> str:
    normalized = value.replace("\\", "/").lower()
    categories = (
        ("/charactericon/", "character"),
        ("/icon/enemy/", "enemy"),
        ("/icon/equipment/", "equipment"),
        ("/icon/sphere/", "sphere"),
        ("/icon/item/", "item"),
        ("/icon/skill/", "skill"),
    )
    for marker, category in categories:
        if marker in normalized:
            return category
    return "other"


def _safe_local_asset(version_dir: Path, relative: str) -> Path | None:
    if not relative.lower().endswith(".png"):
        return None
    root = version_dir.resolve()
    path = (version_dir / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise RuntimeError(f"资源路径越界: {relative}")
    return path if path.is_file() else None


def build_asset_manifest(
    version: str,
    settings: OssSettings,
    version_dir: Path | None = None,
    generated_at: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Path]]:
    version_dir = version_dir or _resolve_version_dir(version)
    report = validate_complete_extraction(version_dir, version)
    generated_at = generated_at or datetime.now(timezone.utc)
    files_by_object: dict[str, Path] = {}
    assets: list[dict[str, Any]] = []
    seen_files: set[str] = set()

    for resource_key, asset_type, resource_hash, relative_file, container in _iter_image_rows(
        version_dir / "manifest.sqlite3"
    ):
        relative_file = str(PurePosixPath(relative_file.replace("\\", "/")))
        if relative_file in seen_files:
            continue
        local_file = _safe_local_asset(version_dir, relative_file)
        if local_file is None:
            continue
        seen_files.add(relative_file)

        digest = _sha256(local_file)
        extension = local_file.suffix.lower()
        object_key = settings.key("objects", digest[:2], f"{digest}{extension}")
        files_by_object.setdefault(object_key, local_file)
        logical_path = container or resource_key
        assets.append(
            {
                "id": resource_key,
                "type": asset_type,
                "category": _asset_category(logical_path),
                "name": Path(logical_path.split("#", 1)[0]).stem,
                "container": container,
                "source_file": relative_file,
                "source_object_hash": resource_hash,
                "sha256": digest,
                "size": local_file.stat().st_size,
                "object_key": object_key,
            }
        )

    if not assets:
        raise RuntimeError(f"版本 {version} 没有可发布的 PNG")

    category_counts: dict[str, int] = {}
    for asset in assets:
        category = asset["category"]
        category_counts[category] = category_counts.get(category, 0) + 1

    manifest = {
        "schema_version": 1,
        "game": "MementoMori",
        "package_name": CONFIG["package_name"],
        "version": version,
        "generated_at": generated_at.isoformat(),
        "source_package_sha256": report.get("source_sha256", ""),
        "base_url": settings.public_base_url,
        "asset_count": len(assets),
        "unique_object_count": len(files_by_object),
        "total_bytes": sum(path.stat().st_size for path in files_by_object.values()),
        "category_counts": dict(sorted(category_counts.items())),
        "assets": assets,
    }
    return manifest, files_by_object


def _list_existing_objects(bucket, prefix: str) -> set[str]:
    keys: set[str] = set()
    token = ""
    while True:
        kwargs: dict[str, Any] = {"prefix": prefix, "max_keys": 1000}
        if token:
            kwargs["continuation_token"] = token
        result = bucket.list_objects_v2(**kwargs)
        keys.update(item.key for item in result.object_list)
        if not result.is_truncated:
            break
        token = result.next_continuation_token
        if not token:
            raise RuntimeError("OSS 对象列表分页缺少 continuation token")
    return keys


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _manifest_url(settings: OssSettings, key: str) -> str:
    return f"{settings.public_base_url}/{key}" if settings.public_base_url else ""


def _latest_document(
    settings: OssSettings,
    version: str,
    manifest_key: str,
    manifest: dict[str, Any],
    published_at: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "game": "MementoMori",
        "version": version,
        "published_at": published_at,
        "manifest_key": manifest_key,
        "manifest_url": _manifest_url(settings, manifest_key),
        "asset_count": manifest.get("asset_count", 0),
        "unique_object_count": manifest.get("unique_object_count", 0),
        "total_bytes": manifest.get("total_bytes", 0),
    }


def _put_latest(
    bucket,
    settings: OssSettings,
    version: str,
    manifest_key: str,
    manifest: dict[str, Any],
    published_at: str,
) -> dict[str, Any]:
    latest = _latest_document(
        settings, version, manifest_key, manifest, published_at
    )
    bucket.put_object(
        settings.key("manifests", "latest.json"),
        _json_bytes(latest),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "public, max-age=60, must-revalidate",
        },
    )
    return latest


def publish_version(
    version: str,
    settings: OssSettings | None = None,
    bucket=None,
    force: bool = False,
    version_dir: Path | None = None,
) -> dict[str, Any]:
    settings = settings or load_oss_settings()
    settings.validate()
    bucket = bucket or create_bucket(settings)
    manifest_key = settings.key("manifests", "versions", f"{version}.json")
    published_at = datetime.now(timezone.utc).isoformat()

    if not force and _object_exists(bucket, manifest_key):
        manifest = json.loads(bucket.get_object(manifest_key).read().decode("utf-8"))
        latest = _put_latest(
            bucket, settings, version, manifest_key, manifest, published_at
        )
        print(f"OSS 已存在完整版本 {version}，已确认 latest.json")
        return {"status": "already_published", "latest": latest}

    manifest, files_by_object = build_asset_manifest(
        version, settings, version_dir=version_dir
    )
    existing = _list_existing_objects(bucket, settings.key("objects") + "/")
    pending = [(key, path) for key, path in files_by_object.items() if key not in existing]
    print(
        f"发布图片: {manifest['asset_count']} 条记录，"
        f"{manifest['unique_object_count']} 个唯一文件，新增 {len(pending)} 个"
    )

    for index, (key, path) in enumerate(pending, start=1):
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        bucket.put_object_from_file(
            key,
            str(path),
            headers={
                "Content-Type": content_type,
                "Cache-Control": "public, max-age=31536000, immutable",
            },
        )
        if index == 1 or index % 100 == 0 or index == len(pending):
            print(f"OSS 上传进度: {index}/{len(pending)}")

    bucket.put_object(
        manifest_key,
        _json_bytes(manifest),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "public, max-age=31536000, immutable",
        },
    )
    latest = _put_latest(
        bucket, settings, version, manifest_key, manifest, published_at
    )

    reports_dir = ROOT / CONFIG["dirs"]["reports"]
    reports_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "published",
        "version": version,
        "published_at": published_at,
        "asset_count": manifest["asset_count"],
        "unique_object_count": manifest["unique_object_count"],
        "uploaded_object_count": len(pending),
        "reused_object_count": len(files_by_object) - len(pending),
        "manifest_key": manifest_key,
        "latest": latest,
    }
    (reports_dir / f"publish_{version}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"OSS 发布完成: {version}")
    return report


def check_connection(settings: OssSettings | None = None, bucket=None) -> str | None:
    settings = settings or load_oss_settings()
    settings.validate()
    bucket = bucket or create_bucket(settings)
    bucket.list_objects_v2(prefix=settings.key("manifests") + "/", max_keys=1)
    latest = get_remote_latest_version(settings, bucket=bucket)
    print(f"OSS 连接成功: {settings.bucket_name}")
    print(f"已发布版本: {latest or '无'}")
    return latest


def configure_env() -> None:
    current = _read_env_file()

    def ask(name: str, prompt: str, default: str = "", secret: bool = False) -> str:
        existing = current.get(name, default)
        suffix = f" [{existing}]" if existing and not secret else ""
        value = (getpass.getpass if secret else input)(f"{prompt}{suffix}: ").strip()
        return value or existing

    values = {
        "MMTM_OSS_ENABLED": "true",
        "OSS_ENDPOINT": ask(
            "OSS_ENDPOINT", "OSS Endpoint", "https://oss-cn-beijing.aliyuncs.com"
        ),
        "OSS_BUCKET": ask("OSS_BUCKET", "Bucket 名称"),
        "OSS_PREFIX": ask("OSS_PREFIX", "对象前缀", "mementomori"),
        "CDN_BASE_URL": ask(
            "CDN_BASE_URL", "CDN 地址（尚未配置可留空）", ""
        ),
        "OSS_ACCESS_KEY_ID": ask("OSS_ACCESS_KEY_ID", "RAM AccessKey ID"),
        "OSS_ACCESS_KEY_SECRET": ask(
            "OSS_ACCESS_KEY_SECRET", "RAM AccessKey Secret", secret=True
        ),
    }
    required = ("OSS_ENDPOINT", "OSS_BUCKET", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET")
    missing = [name for name in required if not values[name]]
    if missing:
        raise ValueError(f"配置未完成: {', '.join(missing)}")

    lines = ["# Local credentials. Never commit this file."]
    lines.extend(f"{name}={json.dumps(value)}" for name, value in values.items())
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"配置已写入: {ENV_FILE}")
    print("下一步运行: python publish.py --check")


def _latest_local_version() -> str | None:
    extracted = ROOT / CONFIG["dirs"]["extracted"]
    if not extracted.is_dir():
        return None
    versions = [path.name for path in extracted.iterdir() if path.is_dir()]
    if not versions:
        return None
    from download import version_key

    return sorted(versions, key=version_key)[-1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将完整解包的图片增量发布到阿里云 OSS。")
    parser.add_argument("--version", help="发布指定版本；默认使用本地最新完整版本")
    parser.add_argument("--force", action="store_true", help="重新生成并上传版本清单")
    parser.add_argument("--dry-run", action="store_true", help="只生成清单统计，不连接 OSS")
    parser.add_argument("--check", action="store_true", help="检查 OSS 配置和连接")
    parser.add_argument("--remote-version", action="store_true", help="只输出 OSS 已发布版本")
    parser.add_argument("--configure", action="store_true", help="交互式创建本地 .env 配置")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.configure:
            configure_env()
            return 0
        settings = load_oss_settings()
        if args.check:
            check_connection(settings)
            return 0
        if args.remote_version:
            version = get_remote_latest_version(settings)
            print(version or "")
            return 0

        version = args.version or _latest_local_version()
        if not version:
            print("没有可发布的本地解包版本")
            return 1
        if args.dry_run:
            manifest, files = build_asset_manifest(version, settings)
            print(
                f"清单有效: version={version}, assets={manifest['asset_count']}, "
                f"objects={len(files)}, bytes={manifest['total_bytes']}"
            )
            return 0
        publish_version(version, settings=settings, force=args.force)
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"OSS 发布失败: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
