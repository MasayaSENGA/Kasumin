"""MJPEG ストリームとスナップショットを配信する HTTP サーバー。"""

from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import logging
import re
import socket
import socketserver
import ssl
import sys
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Sequence, Union
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .camera import ROTATIONS, Camera
from .preferences import CameraPreferences

if TYPE_CHECKING:  # fido2 パッケージは FIDO2 認証を使うときだけ読み込む
    from .auth import Fido2Auth, Session
    from .tls import TLSConfig

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
BOUNDARY = "kasuminframe"
STREAM_PATHS = {"/stream", "/stream.mjpg", "/video", "/mjpeg"}
SNAPSHOT_PATHS = {"/snapshot", "/snapshot.jpg", "/image.jpg", "/jpg"}
STATUS_PATH = "/api/status"
# FIDO2 認証のとき、アプリがアクセストークンで取得できるもの (カメラの映像と状態だけ)
MEDIA_PATHS = STREAM_PATHS | SNAPSHOT_PATHS | {STATUS_PATH}
# 2 台目以降のカメラは /camera/<番号>/stream のように番号を前に付ける。番号なしの /stream などは 1 台目
_CAMERA_PATH = re.compile(r"/camera/(\d{1,3})(/.*)?")

# 配信するファイル: パス → static/ 内のファイル名 (と Content-Type)
HTML = "text/html; charset=utf-8"
JS = "text/javascript; charset=utf-8"
PAGES = {"/": "index.html", "/index.html": "index.html"}
AUTH_PAGES = {"/login": "login.html", "/setup": "login.html", "/settings": "settings.html"}
ASSETS = {
    "/static/kasumin.css": ("kasumin.css", "text/css; charset=utf-8"),
    "/static/viewer.js": ("viewer.js", JS),
    "/static/auth.js": ("auth.js", JS),
    "/static/login.js": ("login.js", JS),
    "/static/settings.js": ("settings.js", JS),
}

# HTML ページに付けるヘッダー。スクリプトは同じサーバーのファイルだけを実行し、他サイトへの埋め込みも禁止する
PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}

# FIDO2 認証の POST API: パス → (処理するメソッド名, ログインが必要か)
POST_ROUTES = {
    "/api/auth/login/begin": ("_api_login_begin", False),
    "/api/auth/login/complete": ("_api_login_complete", False),
    "/api/auth/register/begin": ("_api_register_begin", False),
    "/api/auth/register/complete": ("_api_register_complete", False),
    "/api/auth/logout": ("_api_logout", False),
    "/api/auth/invite": ("_api_invite", True),
    "/api/auth/keys/delete": ("_api_delete_key", True),
    "/api/auth/tokens/create": ("_api_create_token", True),
    "/api/auth/tokens/delete": ("_api_delete_token", True),
    "/api/auth/sessions/delete": ("_api_delete_session", True),
    "/api/auth/sessions/delete-others": ("_api_delete_other_sessions", True),
    "/api/cameras/update": ("_api_update_camera", True),
    "/api/cameras/reset": ("_api_reset_camera", True),
}

MAX_BODY = 64 * 1024  # POST で受け付ける JSON の上限
TLS_HANDSHAKE_TIMEOUT = 10.0
REVALIDATE_INTERVAL = 5.0  # ログアウトやトークン削除の後もストリームが流れ続けないよう、認証を確かめ直す間隔
_TOKEN_IN_QUERY = re.compile(r"(?i)(token=)[^&\s]*")


def split_camera_path(path: str) -> tuple[Optional[int], str]:
    """/camera/<番号>/<残り> を (番号, 残り) に分ける。番号が付いていなければ (None, path)。"""
    match = _CAMERA_PATH.fullmatch(path)
    if match is None:
        return None, path
    return int(match.group(1)), match.group(2) or "/"


@dataclass
class Principal:
    """FIDO2 認証で、リクエストを誰として扱うか (ブラウザのセッション、またはアプリのアクセストークン)。"""

    session: Optional[Session] = None
    token_id: Optional[str] = None


class KasuminServer(ThreadingHTTPServer):
    """カメラと認証設定を各リクエストハンドラへ渡すための HTTP サーバー。"""

    daemon_threads = True
    # Windows の SO_REUSEADDR は使用中ポートへの二重バインドを許してしまうため無効にする
    allow_reuse_address = sys.platform != "win32"
    # 同時接続数の上限。インターネットに公開したとき、接続を開いたままにするクライアントにスレッドを使い切られないようにする
    max_connections = 100

    def __init__(
        self,
        host: str,
        port: int,
        camera: Union[Camera, Sequence[Camera]],
        auth: Optional[tuple[str, str]] = None,
        *,
        fido2: Optional[Fido2Auth] = None,
        tls: Optional[TLSConfig] = None,
        preferences: Optional[CameraPreferences] = None,
    ) -> None:
        if ":" in host:
            self.address_family = socket.AF_INET6
        cameras = [camera] if isinstance(camera, Camera) else list(camera)
        if not cameras:
            raise ValueError("カメラが 1 台もありません")
        # 番号 → カメラ (指定した順)。先頭のカメラは /stream など番号なしの URL でも見られる
        self.cameras = {c.id: c for c in cameras}
        if len(self.cameras) != len(cameras):
            raise ValueError("同じ番号のカメラが重複しています")
        self.camera = cameras[0]
        # 設定画面で変えたカメラの名前と回転の保存先 (省略時はメモリ上だけで、再起動すると元に戻る)
        self.preferences = preferences if preferences is not None else CameraPreferences()
        self.auth = auth
        self.fido2 = fido2
        self.tls = tls
        names = {*PAGES.values(), *AUTH_PAGES.values(), *(name for name, _ in ASSETS.values())}
        self.files = {name: (STATIC_DIR / name).read_bytes() for name in names}
        self._slots = threading.BoundedSemaphore(self.max_connections)
        self._limit_warned = -60.0
        super().__init__((host, port), RequestHandler)

    @property
    def auth_mode(self) -> str:
        if self.fido2 is not None:
            return "fido2"
        return "basic" if self.auth is not None else "none"

    def server_bind(self) -> None:
        # HTTPServer.server_bind は getfqdn() で DNS 逆引きをして起動が遅くなることがあるので省く
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[0], self.server_address[1]

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            now = time.monotonic()
            if now - self._limit_warned >= 60:  # 大量の接続を受けたときにログが溢れないよう間引く
                self._limit_warned = now
                log.warning(
                    "同時接続数が上限 (%d) に達したため、新しい接続を閉じています (%s など)",
                    self.max_connections,
                    client_address[0],
                )
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def finish_request(self, request, client_address) -> None:
        if self.tls is None:
            super().finish_request(request, client_address)
            return
        # TLS のハンドシェイクは接続ごとのスレッドで行う (受け付けループで行うと、遅いクライアントに全体を止められる)
        request.settimeout(TLS_HANDSHAKE_TIMEOUT)
        try:
            conn = self.tls.context().wrap_socket(request, server_side=True)
        except OSError as exc:  # ssl.SSLError を含む
            log.debug("TLS ハンドシェイクに失敗しました %s: %s", client_address[0], exc)
            return
        try:
            super().finish_request(conn, client_address)
        finally:
            self.shutdown_request(conn)

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError, ssl.SSLError)):
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


def _is_loopback(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback


class RequestHandler(BaseHTTPRequestHandler):
    server: KasuminServer  # 型ヒント用

    protocol_version = "HTTP/1.1"
    server_version = f"Kasumin/{__version__}"
    sys_version = ""
    timeout = 30  # 30 秒読み書きが止まった接続は切る(切断したクライアントの検知用)

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - 基底クラスの署名に合わせる
        # URL に付いたアクセストークン (?token=...) はログに残さない
        log.debug("%s %s", self.address_string(), _TOKEN_IN_QUERY.sub(r"\1***", format % args))

    def end_headers(self) -> None:
        # すべての応答に付けるヘッダー
        self.send_header("X-Content-Type-Options", "nosniff")
        fido2 = self.server.fido2
        if fido2 is not None and fido2.origin.secure:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = parse_qs(parts.query)
        if self.server.fido2 is not None:
            self._fido2_get(path, query)
        elif self._authorized():
            self._route(path, query, None)

    def _route(self, path: str, query: dict, principal: Optional[Principal]) -> None:
        if path in PAGES:
            self._serve_file(PAGES[path], HTML)
            return
        if path in ASSETS:
            self._serve_file(*ASSETS[path])
            return
        camera_id, rest = split_camera_path(path)
        camera = self.server.camera if camera_id is None else self.server.cameras.get(camera_id)
        if camera is None:
            self._send_bytes(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"Not Found")
        elif rest in STREAM_PATHS:
            self._serve_stream(camera, query, principal)
        elif rest in SNAPSHOT_PATHS:
            self._serve_snapshot(camera)
        elif rest == STATUS_PATH:
            self._serve_status(camera if camera_id is not None else None)
        elif camera_id is not None and rest == "/":
            # ブラウザで /camera/<番号>/ を開いたときは、視聴ページでそのカメラを表示する
            location = {"Location": f"/#camera={camera.id}"}
            self._send_bytes(HTTPStatus.SEE_OTHER, "text/plain; charset=utf-8", b"See Other", location)
        else:
            self._send_bytes(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"Not Found")

    # ------------------------------------------------------------------ Basic 認証

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

    # ------------------------------------------------------------------ FIDO2 認証

    def _fido2_get(self, path: str, query: dict) -> None:
        fido2 = self.server.fido2
        if path in ASSETS:
            self._serve_file(*ASSETS[path])
            return
        if path in ("/login", "/setup"):
            self._serve_file(AUTH_PAGES[path], HTML)
            return
        session = fido2.session(self._cookie(fido2.cookie_name))
        if path == "/api/auth/state":
            self._send_json(HTTPStatus.OK, fido2.describe(session))
        elif session is not None:
            if path == "/settings":
                self._serve_file(AUTH_PAGES[path], HTML)
            elif path == "/api/auth/overview":
                self._send_json(HTTPStatus.OK, {**fido2.overview(session), "cameras": self._camera_settings()})
            else:
                self._route(path, query, Principal(session=session))
        else:
            media = split_camera_path(path)[1] in MEDIA_PATHS
            token = fido2.token(self._supplied_token(query)) if media else None
            if token is not None:
                self._route(path, query, Principal(token_id=token.id))
            else:
                self._unauthenticated(path)

    def _unauthenticated(self, path: str) -> None:
        fido2 = self.server.fido2
        headers = {}
        if self._cookie(fido2.cookie_name):
            headers["Set-Cookie"] = fido2.clear_cookie()  # 期限切れなどで無効になった Cookie を消す
        camera_id, rest = split_camera_path(path)
        if path in PAGES or path == "/settings" or (camera_id is not None and rest == "/"):
            headers["Location"] = f"/login?next={path}" if path == "/settings" else "/login"
            self._send_bytes(HTTPStatus.SEE_OTHER, "text/plain; charset=utf-8", b"Login required", headers)
            return
        # VLC や ffmpeg などはこの応答を受けて Basic 認証 (パスワード欄にアクセストークン) で接続し直す。
        # ブラウザ (Sec-Fetch-* ヘッダーを送る) に付けると認証ダイアログが出てしまうので付けない
        if "Sec-Fetch-Site" not in self.headers:
            headers["WWW-Authenticate"] = 'Basic realm="Kasumin", charset="UTF-8"'
        if path.startswith("/api/"):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "ログインが必要です"}, headers)
        else:
            self._send_bytes(HTTPStatus.UNAUTHORIZED, "text/plain; charset=utf-8", b"Unauthorized", headers)

    def _supplied_token(self, query: dict) -> Optional[str]:
        """アプリが渡したアクセストークン (Bearer / Basic 認証のパスワード / ?token=)。"""
        scheme, _, value = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() == "bearer":
            return value.strip()
        if scheme.lower() == "basic":
            try:
                decoded = base64.b64decode(value.strip(), validate=True).decode("utf-8")
            except ValueError:  # binascii.Error と UnicodeDecodeError を含む
                return None
            return decoded.partition(":")[2]  # ユーザー名は何でもよい
        values = query.get("token")
        return values[0] if values else None

    def _cookie(self, name: str) -> Optional[str]:
        for part in self.headers.get("Cookie", "").split(";"):
            key, sep, value = part.strip().partition("=")
            if sep and key == name:
                return value
        return None

    def _client_ip(self) -> str:
        """ログと画面表示用の接続元 IP。アクセスの可否の判断には使わない。

        トンネルやリバースプロキシ経由 (接続元がループバック) のときは、プロキシが付ける X-Forwarded-For の
        最後の値 (プロキシから見た接続元) を使う。
        """
        ip = self.client_address[0]
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded and _is_loopback(ip):
            return forwarded.split(",")[-1].strip()[:64] or ip
        return ip

    def _still_authorized(self, principal: Principal) -> bool:
        fido2 = self.server.fido2
        if principal.session is not None:
            return fido2.session_alive(principal.session.id)
        return principal.token_id is not None and fido2.token_alive(principal.token_id)

    def do_POST(self) -> None:
        # 本文は先に読み切る (読み残したまま接続を閉じると、エラーの応答がクライアントに届かないことがある)
        raw = self._read_body()
        if raw is None:
            return
        fido2 = self.server.fido2
        path = urlsplit(self.path).path.rstrip("/")
        route = POST_ROUTES.get(path) if fido2 is not None else None
        if route is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not Found"})
            return
        # CSRF 対策: 公開 URL で開いたページからのリクエストだけを受け付ける
        if self.headers.get("Origin") != fido2.origin.origin:
            message = f"{fido2.origin.origin} で開いたページから操作してください"
            self._send_json(HTTPStatus.FORBIDDEN, {"error": message})
            return
        if self.headers.get_content_type() != "application/json":
            self._send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "JSON で送信してください"})
            return
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "リクエストの形式が正しくありません"})
            return

        from .auth import AuthError

        method, login_required = route
        session = fido2.session(self._cookie(fido2.cookie_name))
        try:
            if login_required and session is None:
                raise AuthError(401, "ログインが必要です")
            getattr(self, method)(body, session)
        except AuthError as exc:
            self._send_json(exc.status, {"error": exc.message})

    def _read_body(self) -> Optional[bytes]:
        """POST の本文を読む。長さが分からない・大きすぎる場合はエラーを返して None。"""
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        error = None
        if "Transfer-Encoding" in self.headers or length < 0:
            error = (HTTPStatus.LENGTH_REQUIRED, "Content-Length が必要です")
        elif length > MAX_BODY:
            error = (HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "リクエストが大きすぎます")
        if error is not None:
            # 本文を読まずに応答するので、残りが次のリクエストと解釈されないよう接続を閉じる
            self._send_json(error[0], {"error": error[1]}, close=True)
            return None
        return self.rfile.read(length)

    def _api_login_begin(self, body: dict, session: Optional[Session]) -> None:
        self._send_json(HTTPStatus.OK, self.server.fido2.login_begin())

    def _api_login_complete(self, body: dict, session: Optional[Session]) -> None:
        fido2 = self.server.fido2
        token, _ = fido2.login_complete(
            body.get("ceremony"),
            body.get("credential"),
            ip=self._client_ip(),
            user_agent=self.headers.get("User-Agent", ""),
        )
        fido2.logout(self._cookie(fido2.cookie_name))  # ログインし直す前のセッションは捨てる
        self._send_json(HTTPStatus.OK, {"ok": True}, {"Set-Cookie": fido2.session_cookie(token)})

    def _api_register_begin(self, body: dict, session: Optional[Session]) -> None:
        options = self.server.fido2.register_begin(setup_token=body.get("setup_token"), session=session)
        self._send_json(HTTPStatus.OK, options)

    def _api_register_complete(self, body: dict, session: Optional[Session]) -> None:
        fido2 = self.server.fido2
        token, credential = fido2.register_complete(
            body.get("ceremony"),
            body.get("credential"),
            name=body.get("name"),
            session=session,
            ip=self._client_ip(),
            user_agent=self.headers.get("User-Agent", ""),
        )
        headers = {}
        if token is not None:  # 登録リンクからの登録は、そのままログインした状態にする
            fido2.logout(self._cookie(fido2.cookie_name))
            headers["Set-Cookie"] = fido2.session_cookie(token)
        self._send_json(HTTPStatus.OK, {"key": credential.summary()}, headers)

    def _api_logout(self, body: dict, session: Optional[Session]) -> None:
        fido2 = self.server.fido2
        fido2.logout(self._cookie(fido2.cookie_name))
        self._send_json(HTTPStatus.OK, {"ok": True}, {"Set-Cookie": fido2.clear_cookie()})

    def _api_invite(self, body: dict, session: Optional[Session]) -> None:
        url, expires = self.server.fido2.create_invite()
        self._send_json(HTTPStatus.OK, {"url": url, "expires": expires})

    def _api_delete_key(self, body: dict, session: Optional[Session]) -> None:
        self.server.fido2.delete_credential(body.get("id"))
        self._send_json(HTTPStatus.OK, {"ok": True})

    def _api_create_token(self, body: dict, session: Optional[Session]) -> None:
        value, token = self.server.fido2.create_token(body.get("name"))
        self._send_json(HTTPStatus.OK, {"token": value, **token.summary()})

    def _api_delete_token(self, body: dict, session: Optional[Session]) -> None:
        self.server.fido2.delete_token(body.get("id"))
        self._send_json(HTTPStatus.OK, {"ok": True})

    def _api_delete_session(self, body: dict, session: Optional[Session]) -> None:
        self.server.fido2.revoke_session(body.get("id"))
        self._send_json(HTTPStatus.OK, {"ok": True})

    def _api_delete_other_sessions(self, body: dict, session: Optional[Session]) -> None:
        count = self.server.fido2.revoke_other_sessions(session)
        self._send_json(HTTPStatus.OK, {"count": count})

    # ------------------------------------------------------------------ カメラの設定 (設定画面)

    def _camera_settings(self) -> list[dict[str, Any]]:
        preferences = self.server.preferences
        return [
            {
                "id": c.id,
                "name": c.label,
                "rotate": c.settings.rotate,
                "default_name": c.defaults["name"] or f"カメラ {c.id}",
                "default_rotate": c.defaults["rotate"],
                "customized": preferences.customized(c),
            }
            for c in self.server.cameras.values()
        ]

    def _requested_camera(self, body: dict) -> Camera:
        from .auth import AuthError

        camera_id = body.get("id")
        camera = self.server.cameras.get(camera_id) if type(camera_id) is int else None  # True なども除く
        if camera is None:
            raise AuthError(404, "カメラが見つかりません")
        return camera

    def _api_update_camera(self, body: dict, session: Optional[Session]) -> None:
        from .auth import AuthError, clean_name

        camera = self._requested_camera(body)
        name = clean_name(body.get("name"))
        rotate = body.get("rotate")
        if not name:
            raise AuthError(400, "カメラの名前を入力してください")
        if type(rotate) is not int or rotate not in ROTATIONS:
            raise AuthError(400, "回転は 0・90・180・270 度のどれかを選んでください")
        old = camera.label
        self.server.preferences.update(camera, name=name, rotate=rotate)
        log.info("カメラ「%s」の設定を変更しました (名前「%s」, 回転 %d°)", old, name, rotate)
        self._send_json(HTTPStatus.OK, {"cameras": self._camera_settings()})

    def _api_reset_camera(self, body: dict, session: Optional[Session]) -> None:
        camera = self._requested_camera(body)
        self.server.preferences.reset(camera)
        log.info("カメラ「%s」の設定を起動時の値に戻しました", camera.label)
        self._send_json(HTTPStatus.OK, {"cameras": self._camera_settings()})

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

    def _send_json(
        self, status: HTTPStatus, data: Any, extra_headers: Optional[dict[str, str]] = None, close: bool = False
    ) -> None:
        headers = dict(extra_headers or {})
        if close:
            headers["Connection"] = "close"
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", body, headers)

    def _serve_file(self, name: str, content_type: str) -> None:
        headers = PAGE_HEADERS if content_type == HTML else None
        self._send_bytes(HTTPStatus.OK, content_type, self.server.files[name], headers)

    def _serve_status(self, camera: Optional[Camera]) -> None:
        """camera を指定するとそのカメラだけ、省略すると全カメラ (cameras) の状態を返す。

        全カメラのときも、1 台目の値を従来どおりの位置 (トップレベル) に入れる。
        """
        if camera is not None:
            data = camera.status()
        else:
            data = self.server.camera.status()
            data["cameras"] = [c.status() for c in self.server.cameras.values()]
        data["version"] = __version__
        data["auth"] = self.server.auth_mode != "none"
        data["auth_mode"] = self.server.auth_mode
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send_bytes(HTTPStatus.OK, "application/json; charset=utf-8", body)

    def _serve_snapshot(self, camera: Camera) -> None:
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

    def _serve_stream(self, camera: Camera, query: dict, principal: Optional[Principal]) -> None:
        fps_cap = _float_param(query, "fps")
        min_interval = 1.0 / fps_cap if fps_cap and fps_cap > 0 else 0.0

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Accel-Buffering", "no")  # nginx などのリバースプロキシにバッファさせない
        self.send_header("Connection", "close")
        self.end_headers()

        client = f"{self._client_ip()} →「{camera.label}」"
        count = camera.add_client()
        log.info("ストリーム開始 %s (視聴者 %d)", client, count)
        seq = 0
        last_sent = 0.0
        next_check = time.monotonic() + REVALIDATE_INTERVAL
        delimiter = b"--" + BOUNDARY.encode() + b"\r\n"
        try:
            # 区切りは各フレームの直後に送る (流れるバイト列は同じ)。ブラウザは次の区切りが届いた時点でフレームを
            # 表示するので、区切りを次のフレームと一緒に送ると表示が 1 フレーム分 (fps を絞った配信では数秒) 遅れる
            self.wfile.write(delimiter)
            while not camera.stopped:
                if principal is not None and time.monotonic() >= next_check:
                    if not self._still_authorized(principal):
                        log.info("ログアウトまたはアクセストークンの削除により、ストリームを終了します %s", client)
                        break
                    next_check = time.monotonic() + REVALIDATE_INTERVAL
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
                            b"Content-Type: image/jpeg\r\n",
                            b"Content-Length: ", str(len(jpeg)).encode(), b"\r\n\r\n",
                            jpeg, b"\r\n",
                            delimiter,
                        )
                    )
                )
                last_sent = time.monotonic()
        except (ConnectionError, TimeoutError, OSError) as exc:
            log.debug("ストリーム切断 %s: %s", client, exc)
        finally:
            count = camera.remove_client()
            log.info("ストリーム終了 %s (視聴者 %d)", client, count)
