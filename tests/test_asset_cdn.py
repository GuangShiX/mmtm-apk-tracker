import base64
import struct
import unittest

from asset_cdn import OfficialAssetInfo, resolve_critical_catalog_targets


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
    def test_official_asset_url_uses_fixed_format(self):
        info = OfficialAssetInfo(
            app_version="1.2.3",
            asset_version="asset-hash",
            master_version="master-hash",
            asset_uri_format="https://cdn.example/{0}",
        )
        self.assertEqual(
            info.catalog_url,
            "https://cdn.example/Android/asset-hash.json",
        )
        self.assertEqual(
            info.asset_url("Android/icon.bundle"),
            "https://cdn.example/Android/icon.bundle",
        )

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
