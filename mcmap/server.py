"""HTTP server: the map UI plus a small JSON API over the job manager."""

from __future__ import annotations

import json
import mimetypes
import re
import shutil
import sys
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .arnis import MAX_AREA_KM2, MAX_HEIGHT_MULT, MAX_SCALE, MIN_HEIGHT_MULT, MIN_SCALE, ArnisInstaller, GenerationRequest
from .config import STATIC_DIR, Config
from .jobs import JobManager
from .worlds import is_within, reveal, zip_world

MAX_BODY = 64 * 1024
LOOPBACK = ("127.0.0.1", "localhost", "[::1]")


class App:
    def __init__(self, config: Config):
        config.ensure_dirs()
        self.config = config
        self.installer = ArnisInstaller(config)
        self.jobs = JobManager(config, self.installer)

    def status(self) -> dict[str, Any]:
        saves = self.config.saves_dir
        return {
            "version": __version__,
            "platform": sys.platform,
            "arnis": self.installer.to_dict(),
            "saves_dir": str(saves) if saves else None,
            "worlds_dir": str(self.config.worlds_dir),
            "limits": {
                "max_area_km2": MAX_AREA_KM2,
                "min_scale": MIN_SCALE,
                "max_scale": MAX_SCALE,
                "min_height_multiplier": MIN_HEIGHT_MULT,
                "max_height_multiplier": MAX_HEIGHT_MULT,
            },
        }


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


Route = tuple[str, re.Pattern[str], Callable[..., None]]


class Handler(BaseHTTPRequestHandler):
    server_version = f"MCMap/{__version__}"
    app: App  # injected by make_server
    allowed_hosts: set[str] | None

    # ---- plumbing ------------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def log_message(self, format: str, *args: Any) -> None:
        pass  # the UI polls constantly; errors are logged in send_error_json

    def _dispatch(self, method: str) -> None:
        url = urllib.parse.urlsplit(self.path)
        self.query = urllib.parse.parse_qs(url.query)
        try:
            self._check_origin(method)
            for route_method, pattern, handler in self.routes():
                if route_method == method and (m := pattern.fullmatch(url.path)):
                    handler(*m.groups())
                    return
            raise ApiError(HTTPStatus.NOT_FOUND, "Not found")
        except ApiError as exc:
            self.send_json({"error": str(exc)}, exc.status)
        except KeyError:
            self.send_json({"error": "Job not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.log_error("Unhandled error on %s %s: %r", method, self.path, exc)
            self.send_json({"error": f"Internal error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _check_origin(self, method: str) -> None:
        # Refuse requests addressed to some other hostname (DNS rebinding) and
        # state-changing requests from other websites (CSRF).
        host = self.headers.get("Host", "")
        if self.allowed_hosts is not None and host not in self.allowed_hosts:
            raise ApiError(HTTPStatus.FORBIDDEN, "Unexpected Host header")
        if method != "GET":
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{host}":
                raise ApiError(HTTPStatus.FORBIDDEN, "Cross-origin request refused")
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Expected application/json")

    def read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid JSON") from None

    def send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, content_type: str | None = None, download_name: str | None = None) -> None:
        if not path.is_file():
            raise ApiError(HTTPStatus.NOT_FOUND, "File not found")
        ctype = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-cache")
        if download_name:
            ascii_name = download_name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "")
            quoted = urllib.parse.quote(download_name)
            self.send_header("Content-Disposition", f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}")
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)

    # ---- routes --------------------------------------------------------------

    def routes(self) -> list[Route]:
        job = r"/api/jobs/([0-9a-f]{6,32})"
        return [
            ("GET", re.compile(r"/"), self.index),
            ("GET", re.compile(r"/static/(.+)"), self.static),
            ("GET", re.compile(r"/api/status"), self.get_status),
            ("POST", re.compile(r"/api/arnis/install"), self.install_arnis),
            ("GET", re.compile(r"/api/jobs"), self.list_jobs),
            ("POST", re.compile(r"/api/jobs"), self.create_job),
            ("GET", re.compile(job), self.get_job),
            ("DELETE", re.compile(job), self.delete_job),
            ("POST", re.compile(job + r"/cancel"), self.cancel_job),
            ("POST", re.compile(job + r"/retry"), self.retry_job),
            ("POST", re.compile(job + r"/install"), self.install_job),
            ("POST", re.compile(job + r"/reveal"), self.reveal_job),
            ("GET", re.compile(job + r"/preview\.png"), self.job_preview),
            ("GET", re.compile(job + r"/download"), self.download_job),
        ]

    def index(self) -> None:
        self.send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")

    def static(self, rel: str) -> None:
        path = (STATIC_DIR / urllib.parse.unquote(rel)).resolve()
        if not is_within(path, STATIC_DIR):
            raise ApiError(HTTPStatus.NOT_FOUND, "Not found")
        self.send_file(path)

    def get_status(self) -> None:
        self.send_json(self.app.status())

    def install_arnis(self) -> None:
        self.read_json()
        self.app.installer.install_async()
        self.send_json(self.app.installer.to_dict())

    def list_jobs(self) -> None:
        self.send_json({"jobs": self.app.jobs.list()})

    def create_job(self) -> None:
        req = GenerationRequest.from_dict(self.read_json())
        self.send_json(self.app.jobs.submit(req), HTTPStatus.CREATED)

    def get_job(self, job_id: str) -> None:
        snap = self.app.jobs.snapshot(job_id)
        if snap is None:
            raise KeyError(job_id)
        self.send_json(snap)

    def delete_job(self, job_id: str) -> None:
        self.app.jobs.remove(job_id, delete_files=self.query.get("files") == ["1"])
        self.send_json({"ok": True})

    def cancel_job(self, job_id: str) -> None:
        self.read_json()
        self.app.jobs.cancel(job_id)
        self.send_json({"ok": True})

    def retry_job(self, job_id: str) -> None:
        self.read_json()
        self.send_json(self.app.jobs.retry(job_id), HTTPStatus.CREATED)

    def install_job(self, job_id: str) -> None:
        self.read_json()
        self.send_json(self.app.jobs.install(job_id))

    def reveal_job(self, job_id: str) -> None:
        self.read_json()
        job = self._finished_job(job_id)
        reveal(Path(job.world_path))
        self.send_json({"ok": True})

    def job_preview(self, job_id: str) -> None:
        job = self.app.jobs.get(job_id)
        if not job or not job.preview_path:
            raise ApiError(HTTPStatus.NOT_FOUND, "No preview for this job")
        self.send_file(Path(job.preview_path), "image/png")

    def download_job(self, job_id: str) -> None:
        job = self._finished_job(job_id)
        world = Path(job.world_path)
        if world.is_file():  # Bedrock .mcworld is already a single archive
            self.send_file(world, "application/octet-stream", world.name)
            return
        archive = zip_world(world)
        try:
            self.send_file(archive, "application/zip", f"{world.name}.zip")
        finally:
            archive.unlink(missing_ok=True)

    def _finished_job(self, job_id: str):
        job = self.app.jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        if job.status != "done" or not job.world_path or not Path(job.world_path).exists():
            raise ApiError(HTTPStatus.CONFLICT, "World is not available")
        return job


def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app, "allowed_hosts": None})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    if host not in ("0.0.0.0", "::", ""):  # explicitly exposed on the network: any Host is fine
        bound_port = server.server_address[1]  # differs from `port` when port is 0
        handler.allowed_hosts = {f"{h}:{bound_port}" for h in (*LOOPBACK, host)}
    return server
