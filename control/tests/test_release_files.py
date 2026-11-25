import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app


class ReleaseFilesTests(unittest.TestCase):
    def test_serves_manifests_as_published_and_supports_package_ranges(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(
            patch.dict(
                "os.environ",
                {
                    "CONTROL_DATA_DIR": str(root),
                    "CONTROL_RELEASE_DIR": str(root / "releases"),
                },
            )
        )
        client = self.enterContext(TestClient(app))
        self.assertEqual(client.get("/releases/dev.json").status_code, 503)

        release_dir = root / "releases"
        artifacts = release_dir / "dev" / "artifacts"
        artifacts.mkdir(parents=True)
        manifest = b'{"schema":1,"key_id":"test","payload":"published","signature":"signature"}\n'
        (release_dir / "dev.json").write_bytes(manifest)
        response = client.get("/releases/dev.json")
        self.assertEqual(response.content, manifest)
        self.assertEqual(response.headers["cache-control"], "no-cache")
        self.assertEqual(client.get("/releases/stable.json").status_code, 503)

        name = "MinecraftTools-update-2026-11-25-2-darwin-arm64.tar.gz"
        (artifacts / name).write_bytes(b"test-package")
        response = client.get(
            f"/releases/artifacts/dev/{name}", headers={"Range": "bytes=0-3"}
        )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b"test")

        installer = "MinecraftTools-2026-11-25-2-darwin-arm64.dmg"
        (artifacts / installer).write_bytes(b"installer")
        response = client.get(f"/releases/artifacts/dev/{installer}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"installer")

        self.assertEqual(
            client.get(f"/releases/artifacts/stable/{name}").status_code, 404
        )
        self.assertEqual(client.get("/releases/artifacts/dev/dev.json").status_code, 404)
        self.assertEqual(
            client.get(f"/releases/artifacts/other/{name}").status_code, 422
        )
