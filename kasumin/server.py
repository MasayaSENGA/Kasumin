"""MJPEG ストリームとスナップショットを配信する HTTP サーバー。"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import socket
import socketserver
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .camera import Camera

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
BOUNDARY = "kasuminframe"
STREAM_PATHS = {"/stream", "/stream.mjpg", "/video", "/mjpeg"}
SNAPSHOT_PATHS = {"/snapshot", "/snapshot.jpg", "/image.jpg", "/jpg"}


class KasuminServer(ThreadingHTTPServer):
    """カメラと認証設定を各リクエストハンドラへ渡すための HTTP サーバー。"""

    daemon_threads = True
    # Windows の SO_REUSEADDR は使用中ポートへの二重バインドを許してしまうため無効にする
    allow_reuse_address = sys.platform != "win32"

    def __init__(
        self, host: str, port: int, camera: Camera, auth: Optional[tuple[str, str]] = None
    ) -> None:
        if ":" in host:
            self.address_family = socket.AF_INET6
        self.camera = camera
        self.auth = auth
        self.index_html = (STATIC_DIR / "index.html").read_bytes()
        super().__init__((host, port), RequestHandler)

    def server_bind(self) -> None:
        # HTTPServer.server_bind は getfqdn() で DNS 逆引きをして起動が遅くなることがあるので省く
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[0], self.server_address[1]

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            log.debug("接続エラー %s: %s", client_address, exc)
        else:
            log.exception("リクエスト処理中にエラーが発生しました (%s)", client_address)


def _float_param(query: dict, name: str) -> Optional[float]:
    values = query.get(name)
    if not values:
        return None
    try:
        return float(values[0])
    except ValueError:
        return None


class RequestHandler(BaseHTTPRequestHandler):
    server: KasuminServer  # 型ヒント用

    protocol_version = "HTTP/1.1"
    server_version = f"Kasumin/{__version__}"
    sys_version = ""
    timeout = 30  # 30 秒読み書きが止まった接続は切る(切断したクライアントの検知用)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 基底クラスの署名に合わせる
        log.debug("%s %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query)
        if not self._authorized():
            return
        if path in ("/", "/index.html"):
            self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", self.server.index_html)
        elif path in STREAM_PATHS:
            self._serve_stream(query)
        elif path in SNAPSHOT_PATHS:
            self._serve_snapshot()
        elif path == "/api/status":
            self._serve_status()
        else:
            self._send_bytes(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"Not Found")

    # ------------------------------------------------------------------ 認証

    def _authorized(self) -> bool:
        auth = self.server.auth
        if auth is None:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                supplied = base64.b64decode(header[6:].strip(), validate=True)
            except ValueError:
                supplied = b""
            expected = f"{auth[0]}:{auth[1]}".encode("utf-8")
            if hmac.compare_digest(supplied, expected):
                return True
        body = b"Unauthorized"
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Kasumin", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    # ------------------------------------------------------------------ 各エンドポイント

    def _send_bytes(
        self,
        status: HTTPStatus,
        content_type: str,
        body: bytes,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _serve_status(self) -> None:
        data = self.server.camera.status()
        data["version"] = __version__
        data["auth"] = self.server.auth is not None
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send_bytes(HTTPStatus.OK, "application/json; charset=utf-8", body)

    def _serve_snapshot(self) -> None:
        camera = self.server.camera
        camera.touch()
        # 必ず新しいフレームを待つ(アイドル解放中でもカメラが起きて撮影するまで待つ)
        result = camera.wait_for_frame(camera.seq, timeout=10.0) or camera.get_jpeg()
        if result is None:
            self._send_bytes(
                HTTPStatus.SERVICE_UNAVAILABLE, "text/plain; charset=utf-8", b"camera not ready"
            )
            return
        self._send_bytes(
            HTTPStatus.OK,
            "image/jpeg",
            result[1],
            {"Content-Disposition": 'inline; filename="snapshot.jpg"'},
        )

    def _serve_stream(self, query: dict) -> None:
        camera = self.server.camera
        fps_cap = _float_param(query, "fps")
        min_interval = 1.0 / fps_cap if fps_cap and fps_cap > 0 else 0.0

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        count = camera.add_client()
        log.info("ストリーム開始 %s (視聴者 %d)", self.address_string(), count)
        seq = 0
        last_sent = 0.0
        try:
            while not camera.stopped:
                if min_interval:
                    remaining = min_interval - (time.monotonic() - last_sent)
                    if remaining > 0:
                        time.sleep(remaining)
                result = camera.wait_for_frame(seq, timeout=5.0)
                if result is None:
                    continue
                seq, jpeg = result
                self.wfile.write(
                    b"".join(
                        (
                            b"--", BOUNDARY.encode(), b"\r\n",
                            b"Content-Type: image/jpeg\r\n",
                            b"Content-Length: ", str(len(jpeg)).encode(), b"\r\n\r\n",
                            jpeg, b"\r\n",
                        )
                    )
                )
                last_sent = time.monotonic()
        except (ConnectionError, TimeoutError, OSError) as exc:
            log.debug("ストリーム切断 %s: %s", self.address_string(), exc)
        finally:
            count = camera.remove_client()
            log.info("ストリーム終了 %s (視聴者 %d)", self.address_string(), count)
