import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import publish
from publish import OssSettings, build_asset_manifest, publish_version


class FakeObject:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return self.payload


class FakeBucket:
    def __init__(self):
        self.objects = {}
        self.events = []

    def object_exists(self, key):
        return key in self.objects

    def get_object(self, key):
        return FakeObject(self.objects[key])

    def list_objects_v2(self, prefix="", max_keys=1000, continuation_token=""):
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        return SimpleNamespace(
            object_list=[SimpleNamespace(key=key) for key in keys[:max_keys]],
            is_truncated=False,
            next_continuation_token="",
        )

    def put_object_from_file(self, key, path, headers=None):
        self.objects[key] = Path(path).read_bytes()
        self.events.append(("file", key, headers or {}))

    def put_object(self, key, payload, headers=None):
        self.objects[key] = bytes(payload)
        self.events.append(("bytes", key, headers or {}))


def create_complete_version(root: Path, version="1.2.3") -> Path:
    version_dir = root / "extracted" / version
    version_dir.mkdir(parents=True)
    (version_dir / "package_manifest.json").write_text("{}", encoding="utf-8")
    (version_dir / "extraction_report.json").write_text(
        json.dumps(
            {
                "version": version,
                "status": "complete",
                "source_sha256": "package-sha",
                "bundles": 1,
                "unity_sources": 1,
                "objects_total": 2,
                "objects_raw_exported": 2,
                "raw_object_coverage": 1.0,
                "errors": [],
            }
        ),
        encoding="utf-8",
    )
    database = sqlite3.connect(version_dir / "manifest.sqlite3")
    database.execute(
        """
        CREATE TABLE resources (
          resource_key TEXT PRIMARY KEY, type TEXT NOT NULL, hash TEXT NOT NULL,
          size INTEGER NOT NULL, file TEXT NOT NULL, bundle TEXT NOT NULL,
          path_id INTEGER NOT NULL, container TEXT NOT NULL, raw_file TEXT NOT NULL,
          typetree_file TEXT NOT NULL, decoded_files TEXT NOT NULL, errors TEXT NOT NULL
        )
        """
    )
    image = b"valid-image-content"
    rows = (
        (
            "Assets/CharacterIcon/CHR_000001.png",
            "Sprite",
            "raw-a",
            "assets/character.png",
            "Assets/CharacterIcon/CHR_000001.png",
        ),
        (
            "Assets/Icon/Equipment/EQP_000001.png",
            "Texture2D",
            "raw-b",
            "assets/equipment.png",
            "Assets/Icon/Equipment/EQP_000001.png",
        ),
    )
    for path in ("assets/character.png", "assets/equipment.png"):
        target = version_dir / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(image)
    for index, row in enumerate(rows, start=1):
        database.execute(
            "INSERT INTO resources VALUES (?, ?, ?, ?, ?, '', ?, ?, '', '', '[]', '[]')",
            (row[0], row[1], row[2], len(image), row[3], index, row[4]),
        )
    database.commit()
    database.close()
    return version_dir


class OssPublishTests(unittest.TestCase):
    def setUp(self):
        self.settings = OssSettings(
            enabled=True,
            endpoint="https://oss-cn-beijing.aliyuncs.com",
            bucket_name="test-bucket",
            prefix="mementomori",
            cdn_base_url="https://assets.example.com",
            access_key_id="id",
            access_key_secret="secret",
        )

    def test_manifest_deduplicates_identical_image_content(self):
        with tempfile.TemporaryDirectory() as temp:
            version_dir = create_complete_version(Path(temp))
            manifest, files = build_asset_manifest(
                "1.2.3",
                self.settings,
                version_dir=version_dir,
                generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )

        self.assertEqual(manifest["asset_count"], 2)
        self.assertEqual(manifest["unique_object_count"], 1)
        self.assertEqual(manifest["category_counts"]["character"], 1)
        self.assertEqual(manifest["category_counts"]["equipment"], 1)
        self.assertEqual(len(files), 1)
        object_keys = {asset["object_key"] for asset in manifest["assets"]}
        self.assertEqual(len(object_keys), 1)

    def test_publish_updates_latest_only_after_objects_and_version_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            version_dir = create_complete_version(root)
            bucket = FakeBucket()
            with patch.object(publish, "ROOT", root), patch.dict(
                publish.CONFIG, {"dirs": {"reports": "reports"}}, clear=False
            ):
                report = publish_version(
                    "1.2.3",
                    settings=self.settings,
                    bucket=bucket,
                    version_dir=version_dir,
                )

        self.assertEqual(report["status"], "published")
        self.assertEqual(report["uploaded_object_count"], 1)
        self.assertEqual(bucket.events[-2][1], "mementomori/manifests/versions/1.2.3.json")
        self.assertEqual(bucket.events[-1][1], "mementomori/manifests/latest.json")
        self.assertIn("immutable", bucket.events[0][2]["Cache-Control"])
        latest = json.loads(bucket.objects[bucket.events[-1][1]])
        self.assertEqual(latest["version"], "1.2.3")
        self.assertEqual(latest["asset_count"], 2)

    def test_repeated_publish_reuses_completed_remote_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            version_dir = create_complete_version(root)
            bucket = FakeBucket()
            with patch.object(publish, "ROOT", root), patch.dict(
                publish.CONFIG, {"dirs": {"reports": "reports"}}, clear=False
            ):
                publish_version(
                    "1.2.3",
                    settings=self.settings,
                    bucket=bucket,
                    version_dir=version_dir,
                )
                event_count = len(bucket.events)
                result = publish_version(
                    "1.2.3",
                    settings=self.settings,
                    bucket=bucket,
                    version_dir=version_dir,
                )

        self.assertEqual(result["status"], "already_published")
        self.assertEqual(len(bucket.events), event_count + 1)
        self.assertEqual(bucket.events[-1][1], "mementomori/manifests/latest.json")

    def test_incomplete_extraction_is_not_publishable(self):
        with tempfile.TemporaryDirectory() as temp:
            version_dir = Path(temp) / "1.2.3"
            version_dir.mkdir()
            with self.assertRaisesRegex(RuntimeError, "缺少完整提取文件"):
                build_asset_manifest("1.2.3", self.settings, version_dir=version_dir)


if __name__ == "__main__":
    unittest.main()
