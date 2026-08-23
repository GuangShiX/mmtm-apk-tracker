import base64
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from asset_cdn import (
    OfficialAssetInfo,
    download_file,
    resolve_catalog_key_bundles,
    resolve_critical_catalog_targets,
)


class FakeStreamingResponse:
    def __init__(self, status_code, headers, chunks):
        self.status_code = status_code
        self.headers = headers
        self.chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        self.chunk_size = chunk_size
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


def encode_keys(keys):
    data = bytearray(struct.pack("<i", len(keys)))
    offsets = {}
    for key in keys:
        offsets[key] = len(data)
        raw = key.encode("utf-8")
        data.extend(b"\x00")
        data.extend(struct.pack("<i", len(raw)))
        data.extend(raw)
    return base64.b64encode(data).decode(), offsets


def encode_buckets(buckets, offsets):
    data = bytearray(struct.pack("<i", len(buckets)))
    for key, indexes in buckets:
        data.extend(struct.pack("<ii", offsets[key], len(indexes)))
        for index in indexes:
            data.extend(struct.pack("<i", index))
    return base64.b64encode(data).decode()


def encode_entries(entries):
    data = bytearray(struct.pack("<i", len(entries)))
    for internal_id_index, dependency_bucket_index in entries:
        data.extend(
            struct.pack(
                "<7i",
                internal_id_index,
                0,
                dependency_bucket_index,
                0,
                0,
                0,
                0,
            )
        )
    return base64.b64encode(data).decode()


class AssetCdnTests(unittest.TestCase):
    def test_download_resumes_partial_file_after_connection_failure(self):
        responses = [
            FakeStreamingResponse(
                200,
                {"content-length": "6"},
                [b"abc", requests.ConnectionError("disconnected")],
            ),
            FakeStreamingResponse(
                206,
                {
                    "content-length": "3",
                    "content-range": "bytes 3-5/6",
                },
                [b"def"],
            ),
        ]
        request_headers = []

        def fake_get(*_args, **kwargs):
            request_headers.append(kwargs["headers"])
            return responses.pop(0)

        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "package.apk"
            with (
                patch("asset_cdn.requests.get", side_effect=fake_get),
                patch("asset_cdn.time.sleep"),
            ):
                result = download_file(
                    "https://example.test/package.apk",
                    target,
                    headers={"user-agent": "test"},
                )

            self.assertEqual(result.read_bytes(), b"abcdef")
            self.assertNotIn("range", request_headers[0])
            self.assertEqual(request_headers[1]["range"], "bytes=3-")

    def test_download_restarts_when_server_ignores_range(self):
        response = FakeStreamingResponse(
            200,
            {"content-length": "3"},
            [b"new"],
        )
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "package.apk"
            target.with_name("package.apk.tmp").write_bytes(b"old-partial")
            with patch("asset_cdn.requests.get", return_value=response) as get:
                download_file("https://example.test/package.apk", target)

            self.assertEqual(target.read_bytes(), b"new")
            self.assertEqual(
                get.call_args.kwargs["headers"]["range"],
                "bytes=11-",
            )

    def test_official_asset_url_uses_fixed_format(self):
        info = OfficialAssetInfo(
            app_version="1.2.3",
            asset_version="asset-hash",
            master_version="master-hash",
            asset_uri_format="https://cdn.example/{0}",
            master_uri_format="https://master.example/{0}/{1}",
        )
        self.assertEqual(
            info.catalog_url,
            "https://cdn.example/Android/asset-hash.json",
        )
        self.assertEqual(
            info.asset_url("Android/icon.bundle"),
            "https://cdn.example/Android/icon.bundle",
        )
        self.assertEqual(
            info.master_url("CharacterMB"),
            "https://master.example/master-hash/CharacterMB",
        )

    def test_official_master_url_rejects_path_traversal(self):
        info = OfficialAssetInfo(
            app_version="1.2.3",
            asset_version="asset-hash",
            master_version="master-hash",
            asset_uri_format="https://cdn.example/{0}",
            master_uri_format="https://master.example/{0}/{1}",
        )
        with self.assertRaisesRegex(RuntimeError, "非法 MasterBook"):
            info.master_url("../CharacterMB")

    def test_resolves_critical_asset_and_dependency_bundles(self):
        target_key = "CharacterIcon/CHR_000149/CHR_000149_00_s"
        dependency_key = "dependency-key"
        key_data, offsets = encode_keys([target_key, dependency_key])
        catalog = {
            "m_KeyDataString": key_data,
            "m_BucketDataString": encode_buckets(
                [(target_key, [0]), (dependency_key, [1])], offsets
            ),
            "m_EntryDataString": encode_entries([(0, 1), (1, -1)]),
            "m_InternalIds": [
                "{RuntimePath}/Android/icon.bundle",
                "{RuntimePath}/Android/texture.bundle",
            ],
        }

        targets = resolve_critical_catalog_targets(catalog)

        target = targets[("characters", "CHR_000149_00_s.png")]
        self.assertEqual(target.catalog_keys, (target_key,))
        self.assertEqual(
            target.bundle_names,
            ("icon.bundle", "texture.bundle"),
        )

        self.assertEqual(
            resolve_catalog_key_bundles(catalog, [target_key]),
            {target_key: ("icon.bundle", "texture.bundle")},
        )

    def test_exact_catalog_resolver_requires_every_key(self):
        key_data, offsets = encode_keys(["CharacterIcon/CHR_000150"])
        catalog = {
            "m_KeyDataString": key_data,
            "m_BucketDataString": encode_buckets(
                [("CharacterIcon/CHR_000150", [0])], offsets
            ),
            "m_EntryDataString": encode_entries([(0, -1)]),
            "m_InternalIds": ["{RuntimePath}/Android/character.bundle"],
        }
        with self.assertRaisesRegex(RuntimeError, "catalog 缺少"):
            resolve_catalog_key_bundles(catalog, ["missing-key"])

    def test_ignores_non_critical_catalog_keys(self):
        key_data, offsets = encode_keys(["Music/Theme"])
        catalog = {
            "m_KeyDataString": key_data,
            "m_BucketDataString": encode_buckets([("Music/Theme", [0])], offsets),
            "m_EntryDataString": encode_entries([(0, -1)]),
            "m_InternalIds": ["{RuntimePath}/Android/music.bundle"],
        }

        self.assertEqual(resolve_critical_catalog_targets(catalog), {})


if __name__ == "__main__":
    unittest.main()
