"""FIDO2 認証の結合テスト。ブラウザとパスキーの代わりに、ソフトウェアの認証器で WebAuthn の応答を作る。"""

from __future__ import annotations

import base64
import datetime
import http.client
import ipaddress
import json
import os
import socket
import ssl
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fido2.cose import ES256
from fido2.utils import sha256, websafe_encode
from fido2.webauthn import (
    Aaguid,
    AttestationObject,
    AttestedCredentialData,
    AuthenticatorData,
    CollectedClientData,
)

from kasumin.auth import Fido2Auth, parse_public_url
from kasumin.camera import CameraSettings
from kasumin.server import BOUNDARY, KasuminServer
from kasumin.tls import TLSConfig
from test_server import FakeCamera, jpeg_size, wait_until

FLAG = AuthenticatorData.FLAG


class SoftAuthenticator:
    """テスト用のソフトウェア認証器 (ES256)。ブラウザ + パスキーが返すのと同じ形の JSON を作る。"""

    def __init__(self, origin: str, *, user_verified: bool = True) -> None:
        self.origin = origin
        self.user_verified = user_verified
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)
        self.counter = 0

    def _flags(self) -> int:
        return FLAG.UP | (FLAG.UV if self.user_verified else 0)

    def create(self, options: dict, *, origin: str | None = None) -> dict:
        pk = options["publicKey"]
        client_data = CollectedClientData.create("webauthn.create", pk["challenge"], origin or self.origin)
        credential = AttestedCredentialData.create(
            Aaguid.NONE, self.credential_id, ES256.from_cryptography_key(self.key.public_key())
        )
        auth_data = AuthenticatorData.create(
            sha256(pk["rp"]["id"].encode()), self._flags() | FLAG.AT | FLAG.BE, self.counter, credential
        )
        attestation = AttestationObject.create("none", auth_data, {})
        raw_id = websafe_encode(self.credential_id)
        return {
            "id": raw_id,
            "rawId": raw_id,
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": client_data.b64,
                "attestationObject": websafe_encode(attestation),
                "transports": ["internal", "hybrid", "carrier-pigeon"],
            },
        }

    def get(self, options: dict, *, origin: str | None = None, counter: int | None = None, tamper=False) -> dict:
        pk = options["publicKey"]
        self.counter = self.counter + 1 if counter is None else counter
        client_data = CollectedClientData.create("webauthn.get", pk["challenge"], origin or self.origin)
        auth_data = AuthenticatorData.create(sha256(pk["rpId"].encode()), self._flags(), self.counter)
        signature = self.key.sign(auth_data + client_data.hash, ec.ECDSA(hashes.SHA256()))
        if tamper:
            signature = signature[:-1] + bytes([signature[-1] ^ 1])
        raw_id = websafe_encode(self.credential_id)
        return {
            "id": raw_id,
            "rawId": raw_id,
            "type": "public-key",
            "clientExtensionResults": {},
            "response": {
                "clientDataJSON": client_data.b64,
                "authenticatorData": websafe_encode(auth_data),
                "signature": websafe_encode(signature),
            },
        }


class Response:
    def __init__(self, status: int, headers, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body)


class Browser:
    """Cookie と Origin ヘッダーを扱う最小限の HTTP クライアント (ブラウザの代わり)。"""

    def __init__(self, port: int, origin: str, context: ssl.SSLContext | None = None) -> None:
        self.port = port
        self.origin = origin
        self.context = context
        self.cookies: dict[str, str] = {}

    def request(self, method: str, path: str, body=None, headers=None, browser=True) -> Response:
        if self.context is not None:
            conn = http.client.HTTPSConnection("127.0.0.1", self.port, timeout=5, context=self.context)
        else:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        send = {}
        if browser:  # 最近のブラウザが付けるヘッダー
            send["Sec-Fetch-Site"] = "same-origin"
            if method == "POST":
                send["Origin"] = self.origin
        if self.cookies:
            send["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            send["Content-Type"] = "application/json"
        send.update(headers or {})
        try:
            conn.request(method, path, body=data, headers=send)
            res = conn.getresponse()
            payload = res.read()
        finally:
            conn.close()
        for cookie in res.headers.get_all("Set-Cookie") or []:
            name, _, rest = cookie.partition("=")
            if "Max-Age=0" in cookie:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = rest.split(";", 1)[0]
        return Response(res.status, res.headers, payload)

    def get(self, path: str, **kwargs) -> Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, body=None, **kwargs) -> Response:
        return self.request("POST", path, body={} if body is None else body, **kwargs)


class Fido2ServerBase(unittest.TestCase):
    public_url = "https://cam.example.test"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store_path = Path(tmp.name) / "auth.json"
        self.cameras = self.make_cameras()
        for camera in self.cameras:
            camera.start()
            self.addCleanup(camera.stop)
        self.camera = self.cameras[0]
        self.origin = parse_public_url(self.public_url).origin
        self.start_server()

    def make_cameras(self) -> list[FakeCamera]:
        return [FakeCamera(CameraSettings(width=160, height=120))]

    def start_server(self, **auth_options) -> None:
        self.auth = Fido2Auth(parse_public_url(self.public_url), self.store_path, **auth_options)
        self.addCleanup(self.auth.close)
        self.server = KasuminServer("127.0.0.1", 0, self.cameras, fido2=self.auth, tls=self.tls_config())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.auth.close()

    def tls_config(self) -> TLSConfig | None:
        return None

    def browser(self) -> Browser:
        return Browser(self.port, self.origin)

    def register(self, browser: Browser, authenticator: SoftAuthenticator, *, setup_url=None, name="テストキー"):
        body = {"setup_token": setup_url.split("#t=", 1)[1]} if setup_url else {}
        begin = browser.post("/api/auth/register/begin", body)
        if begin.status != 200:
            return begin
        options = begin.json()
        complete = {"ceremony": options["ceremony"], "credential": authenticator.create(options), "name": name}
        return browser.post("/api/auth/register/complete", complete)

    def login(self, browser: Browser, authenticator: SoftAuthenticator, headers=None, **kwargs) -> Response:
        options = browser.post("/api/auth/login/begin").json()
        complete = {"ceremony": options["ceremony"], "credential": authenticator.get(options, **kwargs)}
        return browser.post("/api/auth/login/complete", complete, headers=headers)

    def setup_owner(self) -> tuple[Browser, SoftAuthenticator]:
        """登録リンクから最初のキーを登録し、ログイン済みのブラウザを返す。"""
        browser, key = self.browser(), SoftAuthenticator(self.origin)
        url, _ = self.auth.create_setup_link()
        res = self.register(browser, key, setup_url=url)
        self.assertEqual(res.status, 200, res.body)
        return browser, key

    def app_get(self, path: str, headers=None) -> Response:
        """ブラウザ以外のアプリ (VLC など) としての GET。Cookie も Sec-Fetch-* も送らない。"""
        return Browser(self.port, self.origin).get(path, headers=headers, browser=False)


class Fido2FlowTest(Fido2ServerBase):
    def test_everything_except_login_requires_authentication(self):
        browser = self.browser()
        for path in ("/", "/index.html"):
            res = browser.get(path)
            self.assertEqual(res.status, 303)
            self.assertEqual(res.headers["Location"], "/login")
        self.assertEqual(browser.get("/settings").headers["Location"], "/login?next=/settings")
        self.assertEqual(browser.get("/login").status, 200)
        self.assertEqual(browser.get("/setup").status, 200)
        self.assertEqual(browser.get("/static/auth.js").status, 200)
        for path in ("/stream", "/snapshot.jpg", "/api/status", "/api/auth/overview", "/nope"):
            res = browser.get(path)
            self.assertEqual(res.status, 401, path)
            self.assertNotIn("WWW-Authenticate", res.headers, "ブラウザに Basic 認証のダイアログを出さない")
        # アプリ (Sec-Fetch-* なし) には Basic 認証でトークンを送るよう促す
        self.assertIn("Basic", self.app_get("/stream").headers["WWW-Authenticate"])
        self.assertEqual(self.camera.status()["clients"], 0)

        state = browser.get("/api/auth/state").json()
        self.assertEqual(state, {"mode": "fido2", "origin": self.origin, "has_keys": False, "authenticated": False})

    def test_setup_link_registers_first_key_and_logs_in(self):
        browser, key = self.browser(), SoftAuthenticator(self.origin)
        url, _ = self.auth.create_setup_link()
        self.assertTrue(url.startswith(self.origin + "/setup#t="))
        res = self.register(browser, key, setup_url=url, name="  iPhone\n(仕事用)  ")
        self.assertEqual(res.status, 200, res.body)
        self.assertEqual(res.json()["key"]["name"], "iPhone (仕事用)")

        cookie = res.headers["Set-Cookie"]
        self.assertTrue(cookie.startswith("__Host-kasumin="))
        for attr in ("Path=/", "HttpOnly", "SameSite=Strict", "Secure"):
            self.assertIn(attr, cookie)

        self.assertEqual(browser.get("/").status, 200)
        status = browser.get("/api/status").json()
        self.assertEqual(status["auth_mode"], "fido2")
        self.assertTrue(browser.get("/api/auth/state").json()["authenticated"])

        stored = json.loads(self.store_path.read_text(encoding="utf-8"))
        self.assertEqual(len(stored["credentials"]), 1)
        self.assertEqual(stored["credentials"][0]["rp_id"], "cam.example.test")
        self.assertEqual(stored["credentials"][0]["transports"], ["internal", "hybrid"])
        self.assertTrue(stored["credentials"][0]["backup_eligible"])

    def test_setup_link_is_single_use_and_expires(self):
        url, _ = self.auth.create_setup_link()
        self.assertEqual(self.register(self.browser(), SoftAuthenticator(self.origin), setup_url=url).status, 200)
        reused = self.register(self.browser(), SoftAuthenticator(self.origin), setup_url=url)
        self.assertEqual(reused.status, 403)

        expired, _ = self.auth.create_setup_link(ttl=-1)
        self.assertEqual(self.register(self.browser(), SoftAuthenticator(self.origin), setup_url=expired).status, 403)
        self.assertEqual(self.register(self.browser(), SoftAuthenticator(self.origin), setup_url=url + "x").status, 403)
        # トークンもセッションもなければ登録を始められない
        self.assertEqual(self.browser().post("/api/auth/register/begin").status, 401)

    def test_registration_rejects_wrong_origin(self):
        browser, key = self.browser(), SoftAuthenticator("https://evil.example.test")
        url, _ = self.auth.create_setup_link()
        with self.assertLogs("kasumin.auth", "WARNING"):
            self.assertEqual(self.register(browser, key, setup_url=url).status, 400)
        self.assertFalse(self.auth.has_credentials())
        self.assertNotIn("__Host-kasumin", browser.cookies)

    def test_login_with_registered_key(self):
        _, key = self.setup_owner()
        browser = self.browser()
        self.assertEqual(browser.post("/api/auth/login/begin").json()["publicKey"]["userVerification"], "required")
        res = self.login(browser, key, headers={"X-Forwarded-For": "198.51.100.1, 203.0.113.9"})
        self.assertEqual(res.status, 200, res.body)
        self.assertIn("__Host-kasumin", browser.cookies)
        self.assertEqual(browser.get("/").status, 200)
        stored = json.loads(self.store_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["credentials"][0]["sign_count"], key.counter)
        # プロキシ経由 (接続元がループバック) なら、X-Forwarded-For の最後の値を接続元として表示する
        sessions = browser.get("/api/auth/overview").json()["sessions"]
        self.assertIn("203.0.113.9", [s["ip"] for s in sessions])

    def test_login_rejects_invalid_assertions(self):
        _, key = self.setup_owner()
        self.assertEqual(self.login(self.browser(), key).status, 200)  # 署名カウンタが 1 になる
        cases = {
            "別サイトでの署名": dict(origin="https://evil.example.test"),
            "改ざんされた署名": dict(tamper=True),
            "増えていない署名カウンタ (複製されたキー)": dict(counter=1),
        }
        for label, kwargs in cases.items():
            with self.subTest(label), self.assertLogs("kasumin.auth", "WARNING"):
                browser = self.browser()
                self.assertEqual(self.login(browser, key, **kwargs).status, 401)
                self.assertEqual(browser.get("/api/status").status, 401)
        key.user_verified = False
        with self.assertLogs("kasumin.auth", "WARNING"):
            self.assertEqual(self.login(self.browser(), key).status, 401, "PIN / 生体認証なし")

    def test_challenge_cannot_be_reused(self):
        _, key = self.setup_owner()
        browser = self.browser()
        options = browser.post("/api/auth/login/begin").json()
        first = {"ceremony": options["ceremony"], "credential": key.get(options)}
        self.assertEqual(browser.post("/api/auth/login/complete", first).status, 200)
        replay = {"ceremony": options["ceremony"], "credential": key.get(options)}
        self.assertEqual(self.browser().post("/api/auth/login/complete", replay).status, 400)

    def test_user_verification_preferred_accepts_presence_only(self):
        self.stop_server()
        self.start_server(user_verification="preferred")
        url, _ = self.auth.create_setup_link()
        key = SoftAuthenticator(self.origin, user_verified=False)
        self.assertEqual(self.register(self.browser(), key, setup_url=url).status, 200)
        self.assertEqual(self.login(self.browser(), key).status, 200)

    def test_unknown_key_cannot_log_in(self):
        self.setup_owner()
        with self.assertLogs("kasumin.auth", "WARNING") as logs:
            self.assertEqual(self.login(self.browser(), SoftAuthenticator(self.origin)).status, 401)
        self.assertIn("ログインに失敗しました", logs.output[0])

    def test_post_requires_same_origin_and_json(self):
        browser = self.browser()
        for origin in ("https://evil.example.test", "null", None):
            with self.subTest(origin=origin):
                headers = {"Origin": origin} if origin else {}
                res = browser.request("POST", "/api/auth/login/begin", body={}, headers=headers, browser=False)
                self.assertEqual(res.status, 403)
        self.assertEqual(browser.post("/api/auth/login/begin", b"{}", headers={"Content-Type": "text/plain"}).status, 415)
        self.assertEqual(browser.post("/api/auth/login/begin", b"[1]").status, 400)
        self.assertEqual(browser.post("/api/auth/login/begin", b"{").status, 400)
        too_large = {"Content-Length": str(65 * 1024), "Content-Type": "application/json"}
        self.assertEqual(browser.request("POST", "/api/auth/login/begin", headers=too_large).status, 413)
        self.assertEqual(browser.post("/api/auth/nope").status, 404)

    def test_access_tokens_for_apps(self):
        browser, _ = self.setup_owner()
        created = browser.post("/api/auth/tokens/create", {"name": "Home Assistant"}).json()
        token = created["token"]
        self.assertTrue(token.startswith("ksm_"))
        self.assertNotIn(token, self.store_path.read_text(encoding="utf-8"), "トークン本体は保存しない")

        res = self.app_get(f"/snapshot.jpg?token={token}")
        self.assertEqual(res.status, 200)
        self.assertTrue(res.body.startswith(b"\xff\xd8"))
        self.assertEqual(self.app_get("/api/status", {"Authorization": f"Bearer {token}"}).status, 200)
        basic = base64.b64encode(f"anyone:{token}".encode()).decode()
        self.assertEqual(self.app_get("/api/status", {"Authorization": f"Basic {basic}"}).status, 200)

        # トークンで使えるのはカメラの映像と状態だけ
        self.assertEqual(self.app_get(f"/?token={token}").status, 303)
        self.assertEqual(self.app_get(f"/api/auth/overview?token={token}").status, 401)
        self.assertEqual(self.app_get("/api/status", {"Authorization": "Bearer ksm_wrong"}).status, 401)

        listed = browser.get("/api/auth/overview").json()["tokens"]
        self.assertEqual([t["name"] for t in listed], ["Home Assistant"])
        self.assertNotIn("hash", listed[0])
        self.assertIsNotNone(listed[0]["last_used"])

        self.assertEqual(browser.post("/api/auth/tokens/delete", {"id": created["id"]}).status, 200)
        self.assertEqual(self.app_get(f"/snapshot.jpg?token={token}").status, 401)

    def test_token_is_not_written_to_logs(self):
        browser, _ = self.setup_owner()
        token = browser.post("/api/auth/tokens/create", {"name": "VLC"}).json()["token"]
        with self.assertLogs("kasumin.server", level="DEBUG") as logs:
            self.app_get(f"/api/status?token={token}")
        output = "\n".join(logs.output)
        self.assertIn("token=***", output)
        self.assertNotIn(token, output)

    def test_stream_stops_when_token_is_deleted(self):
        browser, _ = self.setup_owner()
        created = browser.post("/api/auth/tokens/create", {"name": "OBS"}).json()
        with mock.patch("kasumin.server.REVALIDATE_INTERVAL", 0.2):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            self.addCleanup(conn.close)
            conn.request("GET", f"/stream?token={created['token']}")
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            self.assertIn(b"--" + BOUNDARY.encode(), res.read(2048))
            browser.post("/api/auth/tokens/delete", {"id": created["id"]})
            deadline = time.monotonic() + 5
            while res.read(4096) and time.monotonic() < deadline:
                pass
            self.assertLess(time.monotonic(), deadline, "トークン削除後もストリームが続いている")
        self.assertTrue(wait_until(lambda: self.camera.status()["clients"] == 0))

    def test_stream_stops_when_session_is_logged_out(self):
        browser, key = self.setup_owner()
        viewer = self.browser()
        self.assertEqual(self.login(viewer, key).status, 200)
        cookie = "; ".join(f"{k}={v}" for k, v in viewer.cookies.items())
        with mock.patch("kasumin.server.REVALIDATE_INTERVAL", 0.2):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            self.addCleanup(conn.close)
            conn.request("GET", "/stream", headers={"Cookie": cookie, "Sec-Fetch-Site": "same-origin"})
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            res.read(2048)
            with self.assertLogs("kasumin.server", "INFO") as logs:
                browser.post("/api/auth/sessions/delete-others")
                deadline = time.monotonic() + 5
                while res.read(4096) and time.monotonic() < deadline:
                    pass
            self.assertLess(time.monotonic(), deadline, "ログアウト後もストリームが続いている")
        self.assertTrue(any("ストリームを終了します" in line for line in logs.output))

    def test_deleting_a_key_logs_out_its_sessions(self):
        owner, first_key = self.setup_owner()
        # ログイン中のセッションから 2 つ目のキーを追加し、別のブラウザでそのキーを使ってログインする
        second_key = SoftAuthenticator(self.origin)
        self.assertEqual(self.register(owner, second_key, name="YubiKey").status, 200)
        other = self.browser()
        self.assertEqual(self.login(other, second_key).status, 200)

        keys = owner.get("/api/auth/overview").json()["keys"]
        second_id = next(k["id"] for k in keys if k["name"] == "YubiKey")
        self.assertEqual(owner.post("/api/auth/keys/delete", {"id": second_id}).status, 200)
        self.assertEqual(other.get("/api/status").status, 401)
        with self.assertLogs("kasumin.auth", "WARNING"):
            self.assertEqual(self.login(self.browser(), second_key).status, 401)
        # 最後のキーは削除できない
        first_id = owner.get("/api/auth/overview").json()["keys"][0]["id"]
        self.assertEqual(owner.post("/api/auth/keys/delete", {"id": first_id}).status, 409)
        self.assertEqual(self.login(self.browser(), first_key).status, 200)

    def test_registration_started_by_a_session_must_finish_in_it(self):
        owner, _ = self.setup_owner()
        options = owner.post("/api/auth/register/begin").json()
        complete = {"ceremony": options["ceremony"], "credential": SoftAuthenticator(self.origin).create(options)}
        self.assertEqual(self.browser().post("/api/auth/register/complete", complete).status, 401)

    def test_logout_and_revoke_other_sessions(self):
        owner, key = self.setup_owner()
        others = [self.browser(), self.browser()]
        for browser in others:
            self.assertEqual(self.login(browser, key).status, 200)
        sessions = owner.get("/api/auth/overview").json()["sessions"]
        self.assertEqual(len(sessions), 3)

        target = next(s for s in sessions if s["id"] != owner.get("/api/auth/overview").json()["current_session"])
        self.assertEqual(owner.post("/api/auth/sessions/delete", {"id": target["id"]}).status, 200)
        self.assertEqual(owner.post("/api/auth/sessions/delete-others").json()["count"], 1)
        for browser in others:
            self.assertEqual(browser.get("/api/status").status, 401)

        self.assertEqual(owner.post("/api/auth/logout").status, 200)
        self.assertEqual(owner.cookies, {}, "Cookie が消される")
        self.assertEqual(owner.get("/").status, 303)

    def test_invite_link_adds_a_key_on_another_device(self):
        owner, _ = self.setup_owner()
        self.assertEqual(self.browser().post("/api/auth/invite").status, 401)
        invite = owner.post("/api/auth/invite").json()
        self.assertTrue(invite["url"].startswith(self.origin + "/setup#t="))
        phone = self.browser()
        self.assertEqual(self.register(phone, SoftAuthenticator(self.origin), setup_url=invite["url"]).status, 200)
        self.assertEqual(phone.get("/").status, 200)
        self.assertEqual(len(owner.get("/api/auth/overview").json()["keys"]), 2)

    def test_same_key_cannot_be_registered_twice(self):
        owner, key = self.setup_owner()
        options = owner.post("/api/auth/register/begin").json()
        excluded = [c["id"] for c in options["publicKey"]["excludeCredentials"]]
        self.assertEqual(excluded, [websafe_encode(key.credential_id)])
        complete = {"ceremony": options["ceremony"], "credential": key.create(options)}
        self.assertEqual(owner.post("/api/auth/register/complete", complete).status, 409)

    def test_keys_and_tokens_survive_restart(self):
        browser, key = self.setup_owner()
        token = browser.post("/api/auth/tokens/create", {"name": "VLC"}).json()["token"]
        self.stop_server()
        self.start_server()
        browser.port = self.port
        self.assertTrue(self.auth.has_credentials())
        self.assertEqual(browser.get("/api/status").status, 401, "セッションはメモリ上だけ")
        self.assertEqual(self.login(browser, key).status, 200)
        self.assertEqual(self.app_get(f"/api/status?token={token}").status, 200)

    def test_data_dir_cannot_be_shared_by_two_instances(self):
        # 片方で削除したキーを、もう片方が古い内容で書き戻して復活させないように
        with self.assertRaisesRegex(ValueError, "--data-dir"):
            Fido2Auth(parse_public_url(self.public_url), self.store_path)
        self.stop_server()
        Fido2Auth(parse_public_url(self.public_url), self.store_path).close()

    def test_transports_are_sanitized(self):
        browser = self.browser()
        url, _ = self.auth.create_setup_link()
        options = browser.post("/api/auth/register/begin", {"setup_token": url.split("#t=", 1)[1]}).json()
        credential = SoftAuthenticator(self.origin).create(options)
        credential["response"]["transports"] = [{"x": 1}, ["usb"], "usb", "usb", 7, "nfc"]
        res = browser.post("/api/auth/register/complete", {"ceremony": options["ceremony"], "credential": credential})
        self.assertEqual(res.status, 200, res.body)
        self.assertEqual(res.json()["key"]["transports"], ["usb", "nfc"])

    def test_keys_for_another_host_name_are_not_usable(self):
        _, key = self.setup_owner()
        self.stop_server()
        self.public_url = "https://other.example.test"
        self.origin = "https://other.example.test"
        self.start_server()
        self.assertFalse(self.auth.has_credentials())
        self.assertEqual(self.auth.credential_count(), (0, 1))
        self.assertEqual(self.browser().post("/api/auth/login/begin").status, 409)

    def test_sessions_expire(self):
        self.stop_server()
        self.start_server(session_ttl=1.0)
        browser, _ = self.setup_owner()
        self.assertEqual(browser.get("/api/status").status, 200)
        time.sleep(1.2)
        res = browser.get("/")
        self.assertEqual(res.status, 303)
        self.assertIn("Max-Age=0", res.headers["Set-Cookie"], "無効な Cookie は消す")

    def test_security_headers(self):
        res = self.browser().get("/login")
        csp = res.headers["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(res.headers["X-Frame-Options"], "DENY")
        self.assertEqual(res.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("max-age=", res.headers["Strict-Transport-Security"])


class MultiCameraFido2Test(Fido2ServerBase):
    """複数のカメラでも、カメラごとの URL がログインかアクセストークンで保護されていること。"""

    def make_cameras(self) -> list[FakeCamera]:
        return [
            FakeCamera(CameraSettings(index=0, width=160, height=120, name="リビング")),
            FakeCamera(CameraSettings(index=2, width=320, height=240, name="玄関")),
        ]

    def test_camera_urls_require_authentication(self):
        browser = self.browser()
        for path in ("/camera/2/stream", "/camera/2/snapshot.jpg", "/camera/2/api/status", "/camera/7/stream"):
            with self.subTest(path):
                # 存在しない番号も 401 (ログイン前にはどのカメラがあるか分からない)
                self.assertEqual(browser.get(path).status, 401)
        res = browser.get("/camera/2/")
        self.assertEqual((res.status, res.headers["Location"]), (303, "/login"))
        self.assertEqual(self.camera.status()["clients"] + self.cameras[1].status()["clients"], 0)

    def test_access_token_works_for_every_camera(self):
        browser, _ = self.setup_owner()
        token = browser.post("/api/auth/tokens/create", {"name": "Home Assistant"}).json()["token"]
        res = self.app_get(f"/camera/2/snapshot.jpg?token={token}")
        self.assertEqual(res.status, 200)
        self.assertEqual(jpeg_size(res.body), (320, 240))
        self.assertEqual(jpeg_size(self.app_get(f"/snapshot.jpg?token={token}").body), (160, 120))
        self.assertEqual(self.app_get(f"/camera/7/snapshot.jpg?token={token}").status, 404)
        self.assertEqual(self.app_get(f"/camera/2/?token={token}").status, 303, "トークンでは視聴ページは開けない")

    def test_logged_in_viewer_sees_all_cameras(self):
        browser, _ = self.setup_owner()
        cameras = browser.get("/api/status").json()["cameras"]
        self.assertEqual([(c["id"], c["name"]) for c in cameras], [(0, "リビング"), (2, "玄関")])
        self.assertEqual(jpeg_size(browser.get("/camera/2/snapshot.jpg").body), (320, 240))


class LocalhostTest(Fido2ServerBase):
    """http://localhost での動作確認用の設定。Secure Cookie と HSTS は使わない。"""

    public_url = "http://localhost:8080"

    def test_cookie_without_secure_attribute(self):
        url, _ = self.auth.create_setup_link()
        res = self.register(self.browser(), SoftAuthenticator(self.origin), setup_url=url)
        self.assertEqual(res.status, 200)
        cookie = res.headers["Set-Cookie"]
        self.assertTrue(cookie.startswith("kasumin_session="))
        self.assertNotIn("Secure", cookie)
        self.assertNotIn("Strict-Transport-Security", res.headers)


def make_certificate(directory: Path, name: str) -> tuple[Path, Path, bytes]:
    """localhost 用の自己署名証明書を作り、(証明書, 秘密鍵, 証明書の DER) を返す。"""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / f"{name}.pem", directory / f"{name}-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_path, key_path, cert.public_bytes(serialization.Encoding.DER)


def insecure_context() -> ssl.SSLContext:
    # テスト用の自己署名証明書なので検証はしない (どの証明書が使われたかは DER を比べて確かめる)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class TLSTest(Fido2ServerBase):
    def tls_config(self) -> TLSConfig:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cert_dir = Path(tmp.name)
        self.cert_path, self.key_path, self.cert_der = make_certificate(self.cert_dir, "first")
        return TLSConfig(self.cert_path, self.key_path)

    def browser(self) -> Browser:
        return Browser(self.port, self.origin, insecure_context())

    def peer_certificate(self) -> bytes:
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as raw:
            with insecure_context().wrap_socket(raw, server_hostname="localhost") as tls:
                return tls.getpeercert(binary_form=True)

    def test_login_flow_over_https(self):
        browser, key = self.setup_owner()
        self.assertEqual(browser.get("/").status, 200)
        other = self.browser()
        self.assertEqual(self.login(other, key).status, 200)
        self.assertEqual(self.peer_certificate(), self.cert_der)

    def test_stalled_client_does_not_block_others(self):
        stalled = socket.create_connection(("127.0.0.1", self.port))
        self.addCleanup(stalled.close)
        started = time.monotonic()
        self.assertEqual(self.browser().get("/login").status, 200)
        self.assertLess(time.monotonic() - started, 3)

    def test_plain_http_is_rejected_without_breaking_the_server(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        with self.assertRaises((http.client.HTTPException, OSError)):
            conn.request("GET", "/login")
            conn.getresponse().read()
        conn.close()
        self.assertEqual(self.browser().get("/login").status, 200)

    def test_renewed_certificate_is_picked_up_without_restart(self):
        new_cert, new_key, new_der = make_certificate(self.cert_dir, "second")
        os.replace(new_cert, self.cert_path)
        os.replace(new_key, self.key_path)
        with mock.patch("kasumin.tls.RELOAD_CHECK_INTERVAL", 0):
            self.assertEqual(self.peer_certificate(), new_der)


class ConnectionLimitTest(unittest.TestCase):
    def test_connections_over_the_limit_are_closed(self):
        class LimitedServer(KasuminServer):
            max_connections = 2

        camera = FakeCamera(CameraSettings(width=160, height=120))
        camera.start()
        self.addCleanup(camera.stop)
        server = LimitedServer("127.0.0.1", 0, camera)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        address = ("127.0.0.1", server.server_address[1])

        idle = [socket.create_connection(address) for _ in range(2)]
        time.sleep(0.3)
        with self.assertLogs("kasumin.server", "WARNING"), socket.create_connection(address, timeout=3) as rejected:
            try:
                self.assertEqual(rejected.recv(1), b"")
            except ConnectionResetError:
                pass
        for sock in idle:
            sock.close()

        def status_ok():
            conn = http.client.HTTPConnection(*address, timeout=3)
            try:
                conn.request("GET", "/api/status")
                return conn.getresponse().status == 200
            except OSError:
                return False
            finally:
                conn.close()

        self.assertTrue(wait_until(status_ok, timeout=5))


class ParsePublicUrlTest(unittest.TestCase):
    def test_valid_urls(self):
        cases = {
            "https://cam.example.com": ("https://cam.example.com", "cam.example.com"),
            "https://Cam.Example.com:443/": ("https://cam.example.com", "cam.example.com"),
            "https://cam.example.com:8443": ("https://cam.example.com:8443", "cam.example.com"),
            "http://localhost:8080": ("http://localhost:8080", "localhost"),
            "https://pc.tail1234.ts.net": ("https://pc.tail1234.ts.net", "pc.tail1234.ts.net"),
        }
        for url, (origin, rp_id) in cases.items():
            with self.subTest(url):
                parsed = parse_public_url(url)
                self.assertEqual((parsed.origin, parsed.rp_id), (origin, rp_id))

    def test_rejected_urls(self):
        for url in (
            "cam.example.com",
            "ftp://cam.example.com",
            "http://cam.example.com",
            "https://192.168.1.23:8443",
            "https://[::1]",
            "https://cam.example.com/kasumin",
            "https://user:pass@cam.example.com",
            "https://cam.example.com:99999",
            "https://カメラ.example.com",
        ):
            with self.subTest(url), self.assertRaises(ValueError):
                parse_public_url(url)


if __name__ == "__main__":
    unittest.main()
