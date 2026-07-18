"""Incrementally publish the critical icon set to Cloudflare R2.

Object paths stay stable across game updates. The manifest SHA-256 values are
the cache identity, and existing R2 objects are uploaded only when their hash
changes. Stale objects are intentionally retained for old helper references.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from botocore.exceptions import ClientError

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
ENV_FILE = ROOT / ".env"
ASSET_CACHE_CONTROL = "public, max-age=3600, stale-while-revalidate=86400"
JSON_CACHE_CONTROL = "no-cache, max-age=0, must-revalidate"


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
        if value.startswith(("\"", "'")):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = value[1:-1]
        values[name] = value
    return values


def _env(name: str, default: str, file_values: dict[str, str]) -> str:
    return os.environ.get(name, file_values.get(name, default)).strip()


def _as_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class R2Settings:
    enabled: bool
    account_id: str
    bucket_name: str
    prefix: str
    public_origin: str
    access_key_id: str
    secret_access_key: str

    @property
    def endpoint_url(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"

    @property
    def public_base_url(self) -> str:
        values = [self.public_origin.rstrip("/"), self.prefix.strip("/")]
        return "/".join(value for value in values if value)

    def key(self, relative: str) -> str:
        values = [self.prefix.strip("/"), relative.strip("/")]
        return "/".join(value for value in values if value)

    def validate(self, require_credentials: bool = True) -> None:
        required = {
            "R2_ACCOUNT_ID": self.account_id,
            "R2_BUCKET": self.bucket_name,
            "R2_PUBLIC_BASE_URL": self.public_origin,
        }
        if require_credentials:
            required.update(
                {
                    "R2_ACCESS_KEY_ID": self.access_key_id,
                    "R2_SECRET_ACCESS_KEY": self.secret_access_key,
                }
            )
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"缺少 R2 配置: {', '.join(missing)}")
        parsed = urlparse(self.public_origin)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("R2_PUBLIC_BASE_URL 必须是 https:// 开头的公开域名")


def load_r2_settings(config: dict[str, Any] | None = None) -> R2Settings:
    configured = (config or CONFIG).get("r2", {})
    file_values = _read_env_file()
    return R2Settings(
        enabled=_as_bool(
            _env("MMTM_R2_ENABLED", str(configured.get("enabled", False)), file_values)
        ),
        account_id=_env(
            "R2_ACCOUNT_ID", str(configured.get("account_id", "")), file_values
        ),
        bucket_name=_env(
            "R2_BUCKET", str(configured.get("bucket", "")), file_values
        ),
        prefix=_env("R2_PREFIX", str(configured.get("prefix", "")), file_values).strip(
            "/"
        ),
        public_origin=_env(
            "R2_PUBLIC_BASE_URL",
            str(configured.get("public_base_url", "")),
            file_values,
        ).rstrip("/"),
        access_key_id=_env("R2_ACCESS_KEY_ID", "", file_values),
        secret_access_key=_env("R2_SECRET_ACCESS_KEY", "", file_values),
    )


def create_client(settings: R2Settings):
    settings.validate()
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("缺少 boto3，请先运行 pip install -r requirements.txt") from exc
    return boto3.client(
        service_name="s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key,
        region_name="auto",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_source_file(source_dir: Path, relative: str) -> Path:
    normalized = PurePosixPath(relative.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError(f"资源路径越界: {relative}")
    root = source_dir.resolve()
    path = (root / Path(*normalized.parts)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"资源路径越界: {relative}") from exc
    if not path.is_file():
        raise ValueError(f"资源文件不存在: {relative}")
    return path


def load_source_manifest(source_dir: Path) -> tuple[dict[str, Any], dict[str, Path]]:
    source_dir = source_dir.resolve()
    manifest_path = source_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"缺少关键图标清单: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ValueError("manifest.json 缺少 assets")
    if manifest.get("asset_count") != len(assets):
        raise ValueError("manifest.json 的 asset_count 与 assets 数量不一致")

    files: dict[str, Path] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise ValueError("manifest.json 包含无效资源记录")
        relative = asset.get("path")
        digest = asset.get("sha256")
        size = asset.get("size")
        if not isinstance(relative, str) or not relative.startswith("assets/"):
            raise ValueError(f"无效资源路径: {relative}")
        if relative in files:
            raise ValueError(f"重复资源路径: {relative}")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError(f"无效 SHA-256: {relative}")
        path = _safe_source_file(source_dir, relative)
        if path.stat().st_size != size:
            raise ValueError(f"资源大小与清单不一致: {relative}")
        if _sha256(path) != digest:
            raise ValueError(f"资源 SHA-256 与清单不一致: {relative}")
        files[relative] = path
    return manifest, files


def _json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _read_remote_json(client, settings: R2Settings, relative: str) -> dict[str, Any] | None:
    try:
        response = client.get_object(
            Bucket=settings.bucket_name,
            Key=settings.key(relative),
        )
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"NoSuchKey", "404", "NotFound"}:
            return None
        raise
    body = response["Body"].read()
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"R2 上的 {relative} 不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"R2 上的 {relative} 不是 JSON 对象")
    return value


def _asset_identity(manifest: dict[str, Any]) -> tuple[Any, ...]:
    assets = manifest.get("assets", [])
    return (
        manifest.get("version"),
        manifest.get("base_url"),
        tuple(
            sorted(
                (asset.get("path"), asset.get("sha256"), asset.get("size"))
                for asset in assets
                if isinstance(asset, dict)
            )
        ),
    )


def _public_documents(
    source: dict[str, Any], settings: R2Settings
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = dict(source)
    manifest["base_url"] = settings.public_base_url
    manifest["cache_strategy"] = "stable-path-sha256"
    latest = {
        "schema_version": manifest.get("schema_version", 2),
        "game": manifest.get("game", "MementoMori"),
        "version": manifest.get("version"),
        "generated_at": manifest.get("generated_at"),
        "manifest_url": f"{settings.public_base_url}/manifest.json",
        "base_url": settings.public_base_url,
        "cache_strategy": "stable-path-sha256",
        "asset_count": manifest.get("asset_count", 0),
        "total_bytes": manifest.get("total_bytes", 0),
    }
    return manifest, latest


def _put_json(client, settings: R2Settings, relative: str, value: dict[str, Any]) -> None:
    client.put_object(
        Bucket=settings.bucket_name,
        Key=settings.key(relative),
        Body=_json_bytes(value),
        ContentType="application/json; charset=utf-8",
        CacheControl=JSON_CACHE_CONTROL,
    )


def publish_to_r2(
    source_dir: Path,
    settings: R2Settings | None = None,
    client=None,
    force: bool = False,
) -> dict[str, Any]:
    settings = settings or load_r2_settings()
    settings.validate()
    client = client or create_client(settings)
    source, files = load_source_manifest(source_dir)
    manifest, latest = _public_documents(source, settings)
    remote_manifest = _read_remote_json(client, settings, "manifest.json")
    remote_latest = _read_remote_json(client, settings, "latest.json")

    if (
        not force
        and remote_manifest is not None
        and _asset_identity(remote_manifest) == _asset_identity(manifest)
        and remote_latest is not None
        and remote_latest.get("version") == latest.get("version")
        and remote_latest.get("base_url") == latest.get("base_url")
    ):
        print(f"R2 已是最新关键图标集合: {manifest.get('version')}")
        return {"status": "up_to_date", "uploaded": 0, "retained_stale": 0}

    remote_assets = {
        asset.get("path"): asset.get("sha256")
        for asset in (remote_manifest or {}).get("assets", [])
        if isinstance(asset, dict) and isinstance(asset.get("path"), str)
    }
    pending = [
        asset
        for asset in manifest["assets"]
        if force or remote_assets.get(asset["path"]) != asset["sha256"]
    ]
    stale = set(remote_assets) - set(files)

    for index, asset in enumerate(pending, start=1):
        relative = asset["path"]
        path = files[relative]
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        client.upload_file(
            str(path),
            settings.bucket_name,
            settings.key(relative),
            ExtraArgs={
                "ContentType": content_type,
                "CacheControl": ASSET_CACHE_CONTROL,
                "Metadata": {"sha256": asset["sha256"]},
            },
        )
        if index == 1 or index % 100 == 0 or index == len(pending):
            print(f"R2 上传进度: {index}/{len(pending)}")

    _put_json(client, settings, "manifest.json", manifest)
    _put_json(client, settings, "latest.json", latest)
    print(
        f"R2 发布完成: version={manifest.get('version')}, "
        f"uploaded={len(pending)}, retained_stale={len(stale)}"
    )
    return {
        "status": "published",
        "version": manifest.get("version"),
        "uploaded": len(pending),
        "retained_stale": len(stale),
        "asset_count": manifest.get("asset_count", 0),
        "base_url": settings.public_base_url,
    }


def check_connection(settings: R2Settings | None = None, client=None) -> str | None:
    settings = settings or load_r2_settings()
    settings.validate()
    client = client or create_client(settings)
    client.head_bucket(Bucket=settings.bucket_name)
    manifest = _read_remote_json(client, settings, "manifest.json")
    version = manifest.get("version") if manifest else None
    print(f"R2 连接成功: {settings.bucket_name}")
    print(f"已发布版本: {version or '无'}")
    return version


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="增量发布关键图标到 Cloudflare R2。")
    parser.add_argument(
        "--source",
        type=Path,
        default=ROOT / "fallback_dist",
        help="包含 assets/ 和 manifest.json 的关键图标目录",
    )
    parser.add_argument("--force", action="store_true", help="重新上传清单中的全部图片")
    parser.add_argument("--dry-run", action="store_true", help="只校验本地关键图标集合")
    parser.add_argument("--check", action="store_true", help="检查 R2 凭据和存储桶连接")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.dry_run:
            manifest, files = load_source_manifest(args.source)
            print(
                f"R2 发布集合有效: version={manifest.get('version')}, "
                f"assets={len(files)}, bytes={manifest.get('total_bytes')}"
            )
            return 0
        settings = load_r2_settings()
        if args.check:
            check_connection(settings)
            return 0
        if not settings.enabled:
            raise ValueError("R2 未启用，请设置 MMTM_R2_ENABLED=true")
        publish_to_r2(args.source, settings=settings, force=args.force)
        return 0
    except (ClientError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"R2 发布失败: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
