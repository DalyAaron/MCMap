"""End-to-end: real HTTP server + job runner, with tests/fake_arnis.py standing in for Arnis."""

import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from mcmap import nbt
from mcmap.config import Config
from mcmap.server import App, make_server

FAKE_ARNIS = Path(__file__).resolve().parent / "fake_arnis.py"
BBOX = {"south": 51.50, "west": -0.13, "north": 51.51, "east": -0.12}


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.saves = root / "saves"
        config = Config(data_dir=root / "data", arnis_path=FAKE_ARNIS, saves_dir=cls.saves)
        cls.app = App(config)
        cls.server = make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        os.environ["FAKE_ARNIS_DELAY"] = "0.05"

    @classmethod
    def tearDownClass(cls):
        cls.app.jobs.shutdown()
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else (b"{}" if method != "GET" else None)
        hdrs = {"Content-Type": "application/json"} if method != "GET" else {}
        hdrs.update(headers or {})
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                return resp.status, (json.loads(raw) if ctype.startswith("application/json") else raw), resp.headers
        except urllib.error.HTTPError as err:
            with err:
                return err.code, json.loads(err.read() or b"{}"), err.headers

    def submit(self, **overrides):
        status, job, _ = self.request("POST", "/api/jobs", {"bbox": BBOX, "name": "Test World", **overrides})
        self.assertEqual(status, 201, job)
        return job

    def wait(self, job_id, until=("done", "failed", "cancelled"), timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, job, _ = self.request("GET", f"/api/jobs/{job_id}")
            if job["status"] in until:
                return job
            time.sleep(0.1)
        self.fail(f"job {job_id} did not reach {until}: {job}")

    def test_index_and_static(self):
        status, body, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"MCMap", body)
        status, _, headers = self.request("GET", "/static/vendor/leaflet/leaflet.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        status, _, _ = self.request("GET", "/static/../config.py")
        self.assertEqual(status, 404)

    def test_status(self):
        status, data, _ = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["arnis"]["status"], "installed")
        self.assertEqual(data["saves_dir"], str(self.saves))

    def test_java_world_is_installed_and_renamed(self):
        job = self.wait(self.submit(name="Big Ben ⏰", spawn={"lat": 51.505, "lng": -0.125})["id"])
        self.assertEqual(job["status"], "done", job.get("log"))
        self.assertTrue(job["installed"])
        world = Path(job["world_path"])
        self.assertEqual(world.parent, self.saves)
        self.assertEqual(world.name, "Big Ben ⏰")
        self.assertEqual(nbt.get_level_name(world / "level.dat"), "Big Ben ⏰")
        self.assertEqual(job["progress"], 1.0)
        self.assertTrue(any("--spawn-lat=51.505000" in c for c in job["command"]))
        self.assertFalse(any("elements" in line for line in job["log"]), "progress frames leaked into log")

        status, png, headers = self.request("GET", f"/api/jobs/{job['id']}/preview.png")
        self.assertEqual(status, 200)
        self.assertTrue(png.startswith(b"\x89PNG"))

        status, data, headers = self.request("GET", f"/api/jobs/{job['id']}/download")
        self.assertEqual(status, 200)
        self.assertIn("filename*=UTF-8''Big%20Ben", headers["Content-Disposition"])
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertIn("Big Ben ⏰/level.dat", names)
        self.assertIn("Big Ben ⏰/region/r.0.0.mca", names)

        # Same name again lands next to it instead of overwriting.
        second = self.wait(self.submit(name="Big Ben ⏰")["id"])
        self.assertEqual(Path(second["world_path"]).name, "Big Ben ⏰ (2)")
        self.assertEqual(nbt.get_level_name(Path(second["world_path"]) / "level.dat"), "Big Ben ⏰ (2)")

    def test_manual_install(self):
        job = self.wait(self.submit(name="Later", install=False)["id"])
        self.assertFalse(job["installed"])
        self.assertIn(str(Path(self.tmp.name) / "data" / "worlds"), job["world_path"])
        status, job, _ = self.request("POST", f"/api/jobs/{job['id']}/install")
        self.assertEqual(status, 200, job)
        self.assertTrue(job["installed"])
        self.assertTrue((self.saves / "Later" / "level.dat").is_file())
        self.assertTrue(job["has_preview"], "preview path follows the world when it moves")

    def test_bedrock_world(self):
        job = self.wait(self.submit(name="Bedrock Test", edition="bedrock")["id"])
        self.assertEqual(job["status"], "done", job.get("log"))
        self.assertFalse(job["installed"])
        self.assertTrue(job["world_path"].endswith("Bedrock Test.mcworld"))
        self.assertTrue(job["has_preview"])
        status, data, headers = self.request("GET", f"/api/jobs/{job['id']}/download")
        self.assertEqual(status, 200)
        self.assertIn("levelname.txt", zipfile.ZipFile(io.BytesIO(data)).namelist())

    def test_failure_is_reported(self):
        os.environ["FAKE_ARNIS_FAIL"] = "1"
        try:
            job = self.wait(self.submit(name="Broken")["id"])
        finally:
            del os.environ["FAKE_ARNIS_FAIL"]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], "Failed to fetch data: Overpass API timed out")
        status, retried, _ = self.request("POST", f"/api/jobs/{job['id']}/retry")
        self.assertEqual(status, 201)
        self.assertEqual(self.wait(retried["id"])["status"], "done")

    def test_cancel_running_job(self):
        os.environ["FAKE_ARNIS_DELAY"] = "2"
        try:
            job = self.submit(name="Cancel me")
            self.wait(job["id"], until=("running",))
            time.sleep(0.5)
            self.request("POST", f"/api/jobs/{job['id']}/cancel")
            job = self.wait(job["id"], timeout=10)
        finally:
            os.environ["FAKE_ARNIS_DELAY"] = "0.05"
        self.assertEqual(job["status"], "cancelled")
        self.assertFalse((Path(self.tmp.name) / "data" / "worlds" / job["id"]).exists())

    def test_remove_with_files(self):
        job = self.wait(self.submit(name="Throwaway", install=False)["id"])
        job_dir = Path(self.tmp.name) / "data" / "worlds" / job["id"]
        self.assertTrue(job_dir.exists())
        status, _, _ = self.request("DELETE", f"/api/jobs/{job['id']}?files=1")
        self.assertEqual(status, 200)
        self.assertFalse(job_dir.exists())
        self.assertEqual(self.request("GET", f"/api/jobs/{job['id']}")[0], 404)

    def test_validation_error(self):
        status, data, _ = self.request("POST", "/api/jobs", {"bbox": {**BBOX, "north": 51.0}})
        self.assertEqual(status, 400)
        self.assertIn("south < north", data["error"])

    def test_cross_site_requests_refused(self):
        status, _, _ = self.request("POST", "/api/jobs", {"bbox": BBOX}, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        status, _, _ = self.request("POST", "/api/jobs", {"bbox": BBOX}, {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("GET", "/api/jobs", headers={"Host": "evil.example:80"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
