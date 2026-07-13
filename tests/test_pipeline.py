import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from download import verify_zip, version_key
from extract import (
    ManifestWriter,
    _json_safe,
    _safe_archive_path,
    export_font,
    export_raw_object,
    export_specialized,
    extract_package,
    object_asset_path,
)
from run import ensure_apk, single_instance_lock


class PackageExtractionTests(unittest.TestCase):
    def test_extracts_nested_apk_and_finds_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            apk_path = root / "UnityDataAssetPack.apk"
            with zipfile.ZipFile(apk_path, "w", zipfile.ZIP_DEFLATED) as apk:
                apk.writestr("assets/aa/catalog.json", json.dumps({"m_InternalIds": []}))
                apk.writestr("assets/aa/Android/example.bundle", b"bundle-data")
                apk.writestr("assets/bin/Data/globalgamemanagers", b"unity-data")

            xapk_path = root / "game.xapk"
            with zipfile.ZipFile(xapk_path, "w", zipfile.ZIP_STORED) as xapk:
                xapk.writestr("manifest.json", "{}")
                xapk.write(apk_path, apk_path.name)

            out_dir = root / "output"
            raw_dir, bundles, manifest = extract_package(xapk_path, out_dir)

            self.assertTrue(raw_dir.exists())
            self.assertEqual(len(bundles), 1)
            self.assertEqual(bundles[0].read_bytes(), b"bundle-data")
            self.assertEqual(manifest["nested_apk_count"], 1)
            self.assertEqual(manifest["bundle_count"], 1)
            self.assertTrue((out_dir / "package_manifest.json").exists())
            for entry in manifest["entries"]:
                self.assertFalse(Path(entry["file"]).is_absolute())

    def test_rejects_archive_path_traversal(self):
        with self.assertRaises(ValueError):
            _safe_archive_path("../escape.txt")


class DownloadVerificationTests(unittest.TestCase):
    def test_deep_zip_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            archive_path = Path(temp) / "valid.xapk"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("manifest.json", "{}")
            self.assertTrue(verify_zip(archive_path))
            self.assertTrue(verify_zip(archive_path, deep=True))

    def test_semantic_version_sorting(self):
        versions = ["4.9.0", "4.16.1", "4.10.0"]
        self.assertEqual(sorted(versions, key=version_key)[-1], "4.16.1")

    def test_existing_package_uses_deep_crc_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp) / "game@4.18.0.xapk"
            package.touch()
            with (
                patch("run.find_local_apk", return_value=package),
                patch("run.verify_zip", return_value=True) as verify,
            ):
                self.assertEqual(ensure_apk("4.18.0", "apk-pure", False), package)
            verify.assert_called_once_with(package, deep=True)

    def test_automatic_update_allows_only_one_instance(self):
        with single_instance_lock() as first:
            self.assertTrue(first)
            with single_instance_lock() as second:
                self.assertFalse(second)


class ObjectExportHelpersTests(unittest.TestCase):
    def test_container_path_is_sanitized(self):
        path = object_asset_path("Assets/UI/icon:bad?.png", "bundle", "Sprite", "icon", 1)
        self.assertEqual(path, "Assets/UI/icon_bad_.png")

    def test_large_binary_json_is_summarized_without_data_loss_claim(self):
        raw = bytes(range(256)) * 32
        value = _json_safe({"payload": raw})
        self.assertTrue(value["payload"]["__binary__"])
        self.assertEqual(value["payload"]["size"], len(raw))
        self.assertEqual(len(value["payload"]["sha256"]), 64)

    def test_font_byte_list_is_exported(self):
        with tempfile.TemporaryDirectory() as temp:
            obj = SimpleNamespace(path_id=7)
            data = SimpleNamespace(m_FontData=[0, 1, 0, 0, 65, 66])
            result = export_font(obj, data, Path(temp), "font/test")
            self.assertEqual(len(result), 1)
            self.assertEqual((Path(temp) / result[0]["file"]).read_bytes(), bytes(data.m_FontData))
            repeated = export_font(obj, data, Path(temp), "font/test")
            self.assertEqual(repeated[0]["file"], result[0]["file"])

    def test_raw_object_replaces_incomplete_existing_file(self):
        with tempfile.TemporaryDirectory() as temp:
            obj = SimpleNamespace(
                path_id=9,
                assets_file=SimpleNamespace(name="CAB-test"),
                type=SimpleNamespace(name="Texture2D"),
                get_raw_data=lambda: b"complete-object",
            )
            first = export_raw_object(obj, Path(temp), "example.bundle")
            target = Path(temp) / first["file"]
            target.write_bytes(b"partial")
            second = export_raw_object(obj, Path(temp), "example.bundle")
            self.assertEqual(second["file"], first["file"])
            self.assertEqual(target.read_bytes(), b"complete-object")

    def test_empty_runtime_texture_is_a_reported_fallback(self):
        obj = SimpleNamespace(path_id=8, type=SimpleNamespace(name="Texture2D"))
        data = SimpleNamespace(image_data=b"", m_StreamData=SimpleNamespace(size=0))
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(export_specialized(obj, data, None, Path(temp), "font/texture"), [])

    def test_manifest_writer_streams_records_to_sqlite(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.sqlite3"
            writer = ManifestWriter(path)
            writer.add("Assets/test", {
                "type": "Sprite",
                "hash": "abc",
                "size": 3,
                "file": "assets/test.png",
                "bundle": "bundle",
                "path_id": 1,
                "container": "Assets/test",
                "raw_file": "objects_raw/1.bin",
                "typetree_file": "objects_json/1.json",
                "decoded_files": ["assets/test.png"],
                "errors": [],
            })
            self.assertEqual(writer.close(), 1)
            import sqlite3
            connection = sqlite3.connect(path)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM resources").fetchone()[0], 1)
            finally:
                connection.close()

    def test_manifest_writer_resumes_committed_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.sqlite3"
            record = {
                "type": "TextAsset",
                "hash": "abc",
                "size": 3,
                "file": "assets/test.txt",
                "bundle": "bundle-a",
                "path_id": 1,
                "container": "",
                "raw_file": "objects_raw/1.bin",
                "typetree_file": "objects_json/1.json",
                "decoded_files": ["assets/test.txt"],
                "errors": [],
            }
            writer = ManifestWriter(path)
            writer.add("Assets/test", record)
            writer.connection.commit()
            writer.connection.close()

            resumed = ManifestWriter(path, resume=True)
            self.assertEqual(resumed.total, 1)
            self.assertEqual(resumed.source_count("bundle-a"), 1)
            resumed.add("Assets/test-2", {**record, "path_id": 2})
            self.assertEqual(resumed.stats()["total"], 2)
            self.assertEqual(resumed.close(), 2)

    def test_manifest_writer_survives_abrupt_process_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.sqlite3"
            script = f"""
import os
from pathlib import Path
from extract import ManifestWriter
writer = ManifestWriter(Path({str(path)!r}))
writer.add('Assets/test', {{
    'type': 'TextAsset', 'hash': 'abc', 'size': 3,
    'file': 'assets/test.txt', 'bundle': 'bundle-a', 'path_id': 1,
    'container': '', 'raw_file': 'objects_raw/1.bin',
    'typetree_file': 'objects_json/1.json',
    'decoded_files': ['assets/test.txt'], 'errors': [],
}})
writer.connection.commit()
os._exit(0)
"""
            subprocess.run(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                check=True,
            )
            resumed = ManifestWriter(path, resume=True)
            self.assertEqual(resumed.source_count("bundle-a"), 1)
            self.assertEqual(resumed.close(), 1)


if __name__ == "__main__":
    unittest.main()
