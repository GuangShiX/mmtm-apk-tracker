import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from botocore.exceptions import ClientError

from publish_r2 import R2Settings, publish_to_r2


class FakeR2Client:
    def __init__(self):
        self.objects = {}
        self.events = []

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "missing"}},
                "GetObject",
            )
        return {"Body": io.BytesIO(self.objects[Key])}

    def upload_file(self, filename, bucket, key, ExtraArgs):
        self.objects[key] = Path(filename).read_bytes()
        self.events.append(("upload", key, ExtraArgs))

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[Key] = Body
        self.events.append(("put", Key, kwargs))


def create_source(root: Path, entries: dict[str, bytes]) -> Path:
    assets = []
    for relative, content in entries.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        assets.append(
            {
                "category": "characters",
                "name": path.name,
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        )
    manifest = {
        "schema_version": 2,
        "game": "MementoMori",
        "version": "1.2.3",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "base_url": "https://github.invalid/main",
        "asset_count": len(assets),
        "total_bytes": sum(len(value) for value in entries.values()),
        "assets": assets,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


class R2PublisherTests(unittest.TestCase):
    def setUp(self):
        self.settings = R2Settings(
            enabled=True,
            account_id="account-id",
            bucket_name="icons",
            prefix="mementomori",
            public_origin="https://assets.example.com",
            access_key_id="access-key",
            secret_access_key="secret",
        )

    def test_initial_publish_uses_stable_paths_and_writes_manifest_last(self):
        with tempfile.TemporaryDirectory() as temp:
            source = create_source(
                Path(temp),
                {"assets/characters/CHR_000001_00_s.png": b"first"},
            )
            client = FakeR2Client()
            result = publish_to_r2(source, self.settings, client=client)

        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(client.events[0][1], "mementomori/assets/characters/CHR_000001_00_s.png")
        self.assertEqual(client.events[-2][1], "mementomori/manifest.json")
        self.assertEqual(client.events[-1][1], "mementomori/latest.json")
        remote = json.loads(client.objects["mementomori/manifest.json"])
        self.assertEqual(remote["base_url"], "https://assets.example.com/mementomori")
        self.assertNotIn("v1.2.3", remote["base_url"])

    def test_repeated_publish_skips_unchanged_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            source = create_source(
                Path(temp),
                {"assets/characters/CHR_000001_00_s.png": b"first"},
            )
            client = FakeR2Client()
            publish_to_r2(source, self.settings, client=client)
            client.events.clear()
            result = publish_to_r2(source, self.settings, client=client)

        self.assertEqual(result["status"], "up_to_date")
        self.assertEqual(client.events, [])

    def test_changed_asset_is_overwritten_and_stale_object_is_retained(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = create_source(
                root,
                {
                    "assets/characters/CHR_000001_00_s.png": b"first",
                    "assets/characters/OLD.png": b"old",
                },
            )
            client = FakeR2Client()
            publish_to_r2(source, self.settings, client=client)

            source = create_source(
                root,
                {"assets/characters/CHR_000001_00_s.png": b"changed"},
            )
            client.events.clear()
            result = publish_to_r2(source, self.settings, client=client)

        uploads = [event for event in client.events if event[0] == "upload"]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(result["retained_stale"], 1)
        self.assertIn("mementomori/assets/characters/OLD.png", client.objects)


if __name__ == "__main__":
    unittest.main()
