"""Official MementoMori app and Addressables update discovery."""

from __future__ import annotations

import base64
import json
import re
import struct
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import msgpack
import requests

VARS_URL = "https://mememori-game.com/apps/vars.js"
AUTH_URL = "https://prd1-auth.mememori-boi.com/api/auth/getDataUri"
OFFICIAL_APK_URL = "https://mememori-game.com/apps/mementomori_{version}.apk"
REQUEST_TIMEOUT_SECONDS = 120
ASSET_HEADERS = {
    # Range resumption and Content-Length validation are defined over the
    # transferred representation. Request identity encoding so requests does
    # not transparently expand gzip data and make a valid response look longer
    # than the server's Content-Length.
    "accept-encoding": "identity",
    "user-agent": "BestHTTP/2 v2.3.0",
    "pragma": "no-cache",
    "cache-control": "no-cache",
}


@dataclass(frozen=True)
class OfficialAssetInfo:
    app_version: str
    asset_version: str
    master_version: str
    asset_uri_format: str
    master_uri_format: str = ""

    def asset_url(self, relative_path: str) -> str:
        if "{0}" not in self.asset_uri_format:
            raise RuntimeError("官方资源 URL 格式缺少 {0} 占位符")
        return self.asset_uri_format.replace("{0}", relative_path)

    def master_url(self, master_book_name: str) -> str:
        if "{0}" not in self.master_uri_format or "{1}" not in self.master_uri_format:
            raise RuntimeError("官方 Master URL 格式缺少 {0}/{1} 占位符")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", master_book_name):
            raise RuntimeError(f"非法 MasterBook 名称: {master_book_name}")
        return self.master_uri_format.replace(
            "{0}", self.master_version
        ).replace("{1}", master_book_name)

    @property
    def catalog_url(self) -> str:
        return self.asset_url(f"Android/{self.asset_version}.json")

    @property
    def apk_url(self) -> str:
        return OFFICIAL_APK_URL.format(version=self.app_version)


@dataclass(frozen=True)
class CatalogTarget:
    category: str
    name: str
    catalog_keys: tuple[str, ...]
    bundle_names: tuple[str, ...]


@dataclass(frozen=True)
class _CatalogEntry:
    bundle_name: str
    dependencies_bucket_index: int


def _request_with_retries(
    method: str,
    url: str,
    *,
    retries: int = 3,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
    **kwargs: Any,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(attempt * 2)
    raise RuntimeError(f"请求失败: {url}: {last_error}") from last_error


def get_official_app_version() -> str:
    response = _request_with_retries(
        "GET", VARS_URL, headers={"user-agent": "mmtm-apk-tracker"}, timeout=30
    )
    raw = response.content
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8")
    match = re.search(r"mementomori_(\d+\.\d+\.\d+)\.apk", text)
    if not match:
        match = re.search(r"apkVersion\s*=\s*['\"](\d+\.\d+\.\d+)['\"]", text)
    if not match:
        raise RuntimeError("官方 vars.js 中没有找到游戏版本")
    return match.group(1)


def get_official_asset_info(app_version: str | None = None) -> OfficialAssetInfo:
    app_version = app_version or get_official_app_version()
    headers = {
        "content-type": "application/json; charset=UTF-8",
        "ortegaaccesstoken": "",
        "ortegaappversion": app_version,
        "ortegadevicetype": "2",
        "ortegauuid": "0123456789abcdef0123456789abcdef",
        "accept-encoding": "gzip",
        "user-agent": "BestHTTP/2 v2.3.0",
    }
    body = msgpack.packb({"CountryCode": "CN", "UserId": 0})
    response = _request_with_retries(
        "POST", AUTH_URL, headers=headers, data=body, timeout=30
    )
    status_code = response.headers.get("ortegastatuscode")
    if status_code and status_code != "0":
        raise RuntimeError(f"官方资源接口返回 ortegastatuscode={status_code}")
    data = msgpack.unpackb(response.content, raw=False, timestamp=3)
    asset_version = response.headers.get("ortegaassetversion", "")
    master_version = response.headers.get("ortegamasterversion", "")
    asset_uri_format = data.get("AssetCatalogFixedUriFormat", "")
    master_uri_format = data.get("MasterUriFormat", "")
    reported_version = data.get("AppAssetVersionInfo", {}).get("Version", "")
    if reported_version and reported_version != app_version:
        raise RuntimeError(
            f"官方页面版本 {app_version} 与资源接口版本 {reported_version} 不一致"
        )
    if not asset_version or not asset_uri_format:
        raise RuntimeError("官方资源接口缺少 assetVersion 或资源 URL")
    return OfficialAssetInfo(
        app_version=app_version,
        asset_version=asset_version,
        master_version=master_version,
        asset_uri_format=asset_uri_format,
        master_uri_format=master_uri_format,
    )


def download_file(
    url: str,
    target: Path,
    *,
    headers: dict[str, str] | None = None,
    retries: int = 3,
) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        resume_from = temporary.stat().st_size if temporary.is_file() else 0
        request_headers = dict(headers or ASSET_HEADERS)
        if resume_from:
            request_headers["range"] = f"bytes={resume_from}-"
        try:
            with requests.get(
                url,
                headers=request_headers,
                stream=True,
                timeout=REQUEST_TIMEOUT_SECONDS,
            ) as response:
                if response.status_code == 416:
                    match = re.fullmatch(
                        r"bytes \*/(\d+)",
                        response.headers.get("content-range", ""),
                    )
                    expected = int(match.group(1)) if match else 0
                    if expected and resume_from == expected:
                        temporary.replace(target)
                        return target
                    if expected and resume_from > expected:
                        temporary.unlink(missing_ok=True)
                    response.raise_for_status()
                response.raise_for_status()
                content_length = int(response.headers.get("content-length", 0))
                mode = "wb"
                size = 0
                expected = content_length
                if resume_from and response.status_code == 206:
                    match = re.fullmatch(
                        r"bytes (\d+)-(\d+)/(\d+|\*)",
                        response.headers.get("content-range", ""),
                    )
                    if not match or int(match.group(1)) != resume_from:
                        raise RuntimeError(
                            f"断点响应无效: {target.name}: "
                            f"{response.headers.get('content-range', '')}"
                        )
                    mode = "ab"
                    size = resume_from
                    if match.group(3) != "*":
                        expected = int(match.group(3))
                    elif content_length:
                        expected = resume_from + content_length
                progress_step = 100 * 1024 * 1024
                next_progress = ((size // progress_step) + 1) * progress_step
                with temporary.open(mode) as output:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            output.write(chunk)
                            size += len(chunk)
                            if size >= next_progress:
                                total = f"/{expected}" if expected else ""
                                print(
                                    f"  下载进度: {size}{total} bytes",
                                    flush=True,
                                )
                                next_progress += progress_step
                if expected and size != expected:
                    raise RuntimeError(
                        f"下载长度不一致: {target.name}: {size}/{expected}"
                    )
            temporary.replace(target)
            return target
        except (OSError, requests.RequestException, RuntimeError) as exc:
            last_error = exc
            if attempt < retries:
                retained = temporary.stat().st_size if temporary.is_file() else 0
                print(
                    f"  下载重试: {attempt + 1}/{retries}, "
                    f"保留 {retained} bytes",
                    flush=True,
                )
                time.sleep(attempt * 2)
    raise RuntimeError(f"下载失败: {url}: {last_error}") from last_error


def download_catalog(info: OfficialAssetInfo, target: Path) -> Path:
    return download_file(info.catalog_url, target, headers=ASSET_HEADERS)


def download_official_apk(info: OfficialAssetInfo, target: Path) -> Path:
    return download_file(
        info.apk_url,
        target,
        headers={"user-agent": "mmtm-apk-tracker"},
    )


def download_target_bundles(
    info: OfficialAssetInfo,
    bundle_names: Iterable[str],
    output_dir: Path,
) -> list[Path]:
    names = sorted(set(bundle_names))
    paths: list[Path] = []
    for index, name in enumerate(names, 1):
        if Path(name).name != name or not name.lower().endswith(".bundle"):
            raise RuntimeError(f"catalog 包含非法 Bundle 名称: {name}")
        print(f"  下载热更新 Bundle: {index}/{len(names)} {name}")
        target = output_dir / name
        download_file(info.asset_url(f"Android/{name}"), target)
        paths.append(target)
    return paths


def _decode_catalog_keys(data_string: str) -> tuple[list[str], dict[int, str]]:
    data = base64.b64decode(data_string)
    if len(data) < 4:
        return [], {}
    count = struct.unpack_from("<i", data, 0)[0]
    offset = 4
    keys: list[str] = []
    key_by_offset: dict[int, str] = {}
    for _ in range(count):
        key_offset = offset
        if offset + 5 > len(data):
            raise RuntimeError("Addressables key 数据被截断")
        key_type = data[offset]
        offset += 1
        if key_type != 0:
            raise RuntimeError(f"不支持的 Addressables key 类型: {key_type}")
        length = struct.unpack_from("<i", data, offset)[0]
        offset += 4
        if length < 0 or offset + length > len(data):
            raise RuntimeError("Addressables key 长度无效")
        key = data[offset : offset + length].decode("utf-8")
        offset += length
        keys.append(key)
        key_by_offset[key_offset] = key
    return keys, key_by_offset


def _decode_catalog_buckets(
    data_string: str,
    key_by_offset: dict[int, str],
) -> tuple[list[list[int]], dict[str, list[int]]]:
    data = base64.b64decode(data_string)
    if len(data) < 4:
        return [], {}
    count = struct.unpack_from("<i", data, 0)[0]
    offset = 4
    buckets: list[list[int]] = []
    bucket_by_key: dict[str, list[int]] = defaultdict(list)
    for _ in range(count):
        if offset + 8 > len(data):
            raise RuntimeError("Addressables bucket 数据被截断")
        key_offset, entry_count = struct.unpack_from("<ii", data, offset)
        offset += 8
        if entry_count < 0 or offset + entry_count * 4 > len(data):
            raise RuntimeError("Addressables bucket 条目数量无效")
        entry_indexes = list(
            struct.unpack_from(f"<{entry_count}i", data, offset)
        ) if entry_count else []
        offset += entry_count * 4
        buckets.append(entry_indexes)
        key = key_by_offset.get(key_offset, "")
        if key:
            bucket_by_key[key].extend(entry_indexes)
    return buckets, dict(bucket_by_key)


def _decode_catalog_entries(catalog: dict[str, Any]) -> list[_CatalogEntry]:
    data = base64.b64decode(catalog.get("m_EntryDataString", ""))
    if len(data) < 4:
        return []
    count = struct.unpack_from("<i", data, 0)[0]
    expected = 4 + count * 28
    if count < 0 or len(data) < expected:
        raise RuntimeError("Addressables entry 数据被截断")
    internal_ids = catalog.get("m_InternalIds", [])
    entries: list[_CatalogEntry] = []
    for index in range(count):
        values = struct.unpack_from("<7i", data, 4 + index * 28)
        internal_id_index = values[0]
        internal_id = (
            internal_ids[internal_id_index]
            if 0 <= internal_id_index < len(internal_ids)
            else ""
        )
        normalized = str(internal_id).replace("\\", "/")
        bundle_name = (
            PurePosixPath(normalized).name
            if normalized.lower().endswith(".bundle")
            else ""
        )
        entries.append(
            _CatalogEntry(
                bundle_name=bundle_name,
                dependencies_bucket_index=values[2],
            )
        )
    return entries


def _critical_catalog_name(key: str) -> tuple[str, str] | None:
    patterns = (
        ("characters", r"CharacterIcon/CHR_\d{6}/(CHR_\d{6}_\d{2}_s)"),
        ("enemies", r"Icon/Enemy/(ENE_\d{6})"),
        ("equipment", r"Icon/Equipment/(EQP_\d{6})"),
        ("spheres", r"Icon/Sphere/(SPH_\d{4})"),
        ("items", r"Icon/Item/(Item_\d{4}(?:_S)?)"),
    )
    for category, pattern in patterns:
        match = re.fullmatch(pattern, key, re.IGNORECASE)
        if match:
            return category, match.group(1) + ".png"
    return None


def resolve_critical_catalog_targets(
    catalog: dict[str, Any],
) -> dict[tuple[str, str], CatalogTarget]:
    keys, key_by_offset = _decode_catalog_keys(catalog.get("m_KeyDataString", ""))
    buckets, bucket_by_key = _decode_catalog_buckets(
        catalog.get("m_BucketDataString", ""), key_by_offset
    )
    entries = _decode_catalog_entries(catalog)
    target_keys: dict[tuple[str, str], set[str]] = defaultdict(set)
    target_bundles: dict[tuple[str, str], set[str]] = defaultdict(set)

    for key in keys:
        target = _critical_catalog_name(key)
        if not target:
            continue
        target_keys[target].add(key)
        for asset_entry_index in bucket_by_key.get(key, []):
            if not 0 <= asset_entry_index < len(entries):
                continue
            asset_entry = entries[asset_entry_index]
            if asset_entry.bundle_name:
                target_bundles[target].add(asset_entry.bundle_name)
            dependency_index = asset_entry.dependencies_bucket_index
            if not 0 <= dependency_index < len(buckets):
                continue
            for entry_index in buckets[dependency_index]:
                if 0 <= entry_index < len(entries):
                    bundle_name = entries[entry_index].bundle_name
                    if bundle_name:
                        target_bundles[target].add(bundle_name)

    targets: dict[tuple[str, str], CatalogTarget] = {}
    for target, catalog_keys in target_keys.items():
        bundle_names = target_bundles.get(target, set())
        if not bundle_names:
            raise RuntimeError(
                f"catalog 关键图片没有对应 Bundle: {target[0]}/{target[1]}"
            )
        targets[target] = CatalogTarget(
            category=target[0],
            name=target[1],
            catalog_keys=tuple(sorted(catalog_keys)),
            bundle_names=tuple(sorted(bundle_names)),
        )
    return targets


def resolve_catalog_key_bundles(
    catalog: dict[str, Any], requested_keys: Iterable[str]
) -> dict[str, tuple[str, ...]]:
    """Resolve exact Addressables keys to their asset and dependency bundles."""
    requested = set(requested_keys)
    if not requested:
        return {}
    keys, key_by_offset = _decode_catalog_keys(catalog.get("m_KeyDataString", ""))
    buckets, bucket_by_key = _decode_catalog_buckets(
        catalog.get("m_BucketDataString", ""), key_by_offset
    )
    entries = _decode_catalog_entries(catalog)
    available = set(keys)
    missing = sorted(requested - available)
    if missing:
        raise RuntimeError(f"catalog 缺少 Addressables key: {', '.join(missing)}")

    result: dict[str, tuple[str, ...]] = {}
    for key in sorted(requested):
        bundle_names: set[str] = set()
        for asset_entry_index in bucket_by_key.get(key, []):
            if not 0 <= asset_entry_index < len(entries):
                continue
            asset_entry = entries[asset_entry_index]
            if asset_entry.bundle_name:
                bundle_names.add(asset_entry.bundle_name)
            dependency_index = asset_entry.dependencies_bucket_index
            if not 0 <= dependency_index < len(buckets):
                continue
            for entry_index in buckets[dependency_index]:
                if 0 <= entry_index < len(entries):
                    bundle_name = entries[entry_index].bundle_name
                    if bundle_name:
                        bundle_names.add(bundle_name)
        if not bundle_names:
            raise RuntimeError(f"catalog key 没有对应 Bundle: {key}")
        result[key] = tuple(sorted(bundle_names))
    return result


def load_catalog(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取 Addressables catalog: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Addressables catalog 根节点不是对象")
    return data
