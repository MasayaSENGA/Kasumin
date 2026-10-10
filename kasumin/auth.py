"""FIDO2 (WebAuthn) 認証。

パスキー / セキュリティキーでのログイン、ブラウザのセッション、VLC などのアプリ用アクセストークンを管理する。
登録済みのキーとアクセストークン (SHA-256 のみ) は JSON ファイルに保存し、セッションはメモリ上だけに持つ
(サーバーを再起動するとブラウザはログインし直しになる)。
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import math
import secrets
import sys
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from fido2.cose import ES256, RS256, EdDSA
from fido2.server import Fido2Server
from fido2.utils import websafe_decode, websafe_encode
from fido2.webauthn import (
    AttestedCredentialData,
    AuthenticationResponse,
    AuthenticatorTransport,
    CollectedClientData,
    PublicKeyCredentialDescriptor,
    PublicKeyCredentialParameters,
    PublicKeyCredentialRpEntity,
    PublicKeyCredentialType,
    PublicKeyCredentialUserEntity,
    RegistrationResponse,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .storage import write_json_atomic

log = logging.getLogger(__name__)

# 受け付ける署名方式。ES256 は FIDO2 の必須方式、RS256 は Windows Hello、EdDSA は一部のセキュリティキーが使う
ALGORITHMS = (ES256.ALGORITHM, EdDSA.ALGORITHM, RS256.ALGORITHM)
TRANSPORTS = {t.value for t in AuthenticatorTransport}

SETUP_LINK_TTL = 60 * 60  # 起動時に表示する登録リンクの有効期間
INVITE_TTL = 15 * 60  # ログイン中に作る登録リンクの有効期間
CEREMONY_TTL = 5 * 60  # 登録・ログインを始めてから完了するまでの猶予
TIMEOUT_MS = 120_000  # ブラウザがキーの操作を待つ時間
USAGE_SAVE_INTERVAL = 10 * 60  # アクセストークンの最終使用日時をファイルへ書き出す最短間隔

MAX_CREDENTIALS = 50
MAX_TOKENS = 50
MAX_SESSIONS = 200
MAX_CEREMONIES = 500
MAX_SETUP_LINKS = 20
MAX_NAME_LENGTH = 40


class AuthError(Exception):
    """認証 API のエラー。message はそのまま画面に表示する。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class PublicOrigin:
    """LAN 外からアクセスするときの URL。WebAuthn の origin と RP ID はここから決まる。"""

    origin: str  # 例: https://cam.example.com (既定ポートは付けない。ブラウザが送る Origin と同じ形)
    rp_id: str  # 例: cam.example.com

    @property
    def secure(self) -> bool:
        return self.origin.startswith("https://")


def parse_public_url(url: str) -> PublicOrigin:
    """--public-url の値を検証して PublicOrigin を返す。使えない値なら ValueError。"""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    if scheme not in ("http", "https") or not host:
        raise ValueError("https://cam.example.com のような URL を指定してください")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError("パスやクエリを含まない URL を指定してください (例: https://cam.example.com)")
    if not host.isascii():
        raise ValueError("国際化ドメイン名は xn-- で始まる表記 (punycode) で指定してください")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("FIDO2 (WebAuthn) は IP アドレスでは使えません。ドメイン名の URL を指定してください")
    if scheme == "http" and host != "localhost" and not host.endswith(".localhost"):
        raise ValueError(
            "FIDO2 (WebAuthn) には HTTPS が必要です。https:// で始まる URL を指定してください"
            " (http:// は localhost での動作確認にだけ使えます)"
        )
    try:
        port = parts.port
    except ValueError:
        raise ValueError("ポート番号が正しくありません") from None
    default_port = 443 if scheme == "https" else 80
    origin = f"{scheme}://{host}" + (f":{port}" if port and port != default_port else "")
    return PublicOrigin(origin=origin, rp_id=host)


def clean_name(value: Any) -> str:
    """利用者が付ける名前を表示・ログ用に整える (制御文字の除去、空白の正規化、長さの制限)。"""
    if not isinstance(value, str):
        return ""
    text = "".join(ch if ch.isprintable() else " " for ch in value)
    return " ".join(text.split())[:MAX_NAME_LENGTH]


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _check_client_data(client_data: CollectedClientData) -> None:
    # 他サイトの iframe に埋め込まれた状態で行われた操作は受け付けない
    if client_data.cross_origin:
        raise ValueError("crossOrigin is set")


@dataclass
class Credential:
    """登録済みのパスキー / セキュリティキー。"""

    id: str  # credential ID (base64url)
    rp_id: str
    name: str
    data: AttestedCredentialData
    sign_count: int
    created: float
    last_used: Optional[float] = None
    transports: list[str] = field(default_factory=list)
    attachment: Optional[str] = None  # "platform" (端末内蔵) / "cross-platform" (USB キーや別のスマホ)
    backup_eligible: bool = False  # iCloud キーチェーンなどで同期されるパスキーなら True

    def to_json(self) -> dict[str, Any]:
        # asdict() は AttestedCredentialData (bytes の派生 dataclass) の中まで分解してしまうので使わない
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data["data"] = websafe_encode(self.data)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Credential:
        return cls(**{**data, "data": AttestedCredentialData(websafe_decode(data["data"]))})

    def descriptor(self) -> PublicKeyCredentialDescriptor:
        transports = [AuthenticatorTransport(t) for t in self.transports if t in TRANSPORTS]
        return PublicKeyCredentialDescriptor(
            type=PublicKeyCredentialType.PUBLIC_KEY,
            id=self.data.credential_id,
            transports=transports or None,
        )

    def summary(self) -> dict[str, Any]:
        data = self.to_json()
        del data["data"], data["sign_count"]
        return data


@dataclass
class AccessToken:
    """アプリ (VLC・OBS・Home Assistant など) 用のアクセストークン。トークン本体は保存せず SHA-256 だけを持つ。"""

    id: str
    name: str
    hash: str
    created: float
    last_used: Optional[float] = None

    def summary(self) -> dict[str, Any]:
        data = asdict(self)
        del data["hash"]
        return data


@dataclass
class Session:
    """ブラウザのログイン状態。"""

    id: str  # 一覧表示と個別ログアウト用。Cookie の値とは別の乱数
    credential_id: str
    created: float
    expires: float
    last_seen: float
    ip: str
    user_agent: str


@dataclass
class _Ceremony:
    """登録・ログインの開始から完了までの間だけ保持する状態 (チャレンジなど)。"""

    kind: str  # "register" / "login"
    state: Any
    expires: float
    setup_hash: Optional[str] = None  # 登録リンクからの登録
    session_id: Optional[str] = None  # ログイン中のセッションからの登録


class _ProcessLock:
    """保存先のフォルダを 1 つの Kasumin だけが使うためのロック。プロセスが終了すると自動で外れる。

    2 つの Kasumin が同じファイルを使うと、片方で削除したキーを、もう片方が古い内容で書き戻して復活させてしまう。
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "a+b")  # noqa: SIM115 - 閉じるまでロックを保持する
        try:
            if sys.platform == "win32":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._file.close()
            raise ValueError(
                f"{path.parent} は別の Kasumin が使用中です。"
                "複数の Kasumin を同時に起動する場合は --data-dir で保存先を分けてください"
            ) from None

    def release(self) -> None:
        self._file.close()


class _Store:
    """登録済みのキーとアクセストークンを保存する JSON ファイル。スレッド間のロックは呼び出し側が持つ。"""

    VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = path
        self.user_id = secrets.token_bytes(32)
        self.credentials: list[Credential] = []
        self.tokens: list[AccessToken] = []
        self.lock = _ProcessLock(path.with_name(path.name + ".lock"))
        try:
            if path.exists():
                self._load()
        except ValueError:
            self.lock.release()
            raise

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != self.VERSION:
                raise ValueError(f"対応していない形式です (version={data.get('version')!r})")
            self.user_id = websafe_decode(data["user_id"])
            self.credentials = [Credential.from_json(c) for c in data.get("credentials", [])]
            self.tokens = [AccessToken(**t) for t in data.get("tokens", [])]
        except Exception as exc:  # 壊れたファイルの解析エラー (struct.error など) もまとめて分かりやすく伝える
            raise ValueError(f"{self.path} を読み込めません: {exc!r}") from exc

    def save(self) -> None:
        data = {
            "version": self.VERSION,
            "user_id": websafe_encode(self.user_id),
            "credentials": [c.to_json() for c in self.credentials],
            "tokens": [asdict(t) for t in self.tokens],
        }
        write_json_atomic(self.path, data)


class Fido2Auth:
    """FIDO2 によるログインと、セッション・アクセストークンの管理。全メソッドはスレッドセーフ。"""

    def __init__(
        self,
        origin: PublicOrigin,
        path: Path,
        *,
        session_ttl: float = 7 * 86400,
        user_verification: str = "required",
    ) -> None:
        self.origin = origin
        self.path = Path(path)
        self.session_ttl = session_ttl
        self.user_verification = UserVerificationRequirement(user_verification)
        # __Host- 付きの Cookie は HTTPS・Path=/・Domain なしでしか設定できず、サブドメインから上書きされない
        self.cookie_name = "__Host-kasumin" if origin.secure else "kasumin_session"
        self._lock = threading.RLock()
        self._store = _Store(self.path)
        self._server = Fido2Server(
            PublicKeyCredentialRpEntity(name="Kasumin", id=origin.rp_id),
            attestation="none",
            # 既定ではサブドメインからの操作も許すので、公開 URL の origin と完全一致だけを受け付ける
            verify_origin=lambda value: value == origin.origin,
        )
        self._server.timeout = TIMEOUT_MS
        self._server.allowed_algorithms = [
            PublicKeyCredentialParameters(type=PublicKeyCredentialType.PUBLIC_KEY, alg=alg) for alg in ALGORITHMS
        ]
        self._sessions: dict[str, Session] = {}  # Cookie の値の SHA-256 → セッション
        self._ceremonies: dict[str, _Ceremony] = {}
        self._setup_links: dict[str, float] = {}  # 登録リンクのトークンの SHA-256 → 有効期限
        self._usage_saved = 0.0

    def close(self) -> None:
        """保存先のロックを外す (プロセスの終了時には自動で外れる)。"""
        with self._lock:
            self._store.lock.release()

    # ------------------------------------------------------------------ 状態

    def has_credentials(self) -> bool:
        with self._lock:
            return bool(self._active())

    def credential_count(self) -> tuple[int, int]:
        """(この公開 URL で使えるキーの数, 別のホスト名用に登録されたキーの数)"""
        with self._lock:
            active = len(self._active())
            return active, len(self._store.credentials) - active

    def describe(self, session: Optional[Session]) -> dict[str, Any]:
        """ログインページ用の状態 (未ログインでも返す)。"""
        with self._lock:
            data: dict[str, Any] = {
                "mode": "fido2",
                "origin": self.origin.origin,
                "has_keys": bool(self._active()),
                "authenticated": session is not None,
            }
            if session is not None:
                credential = self._find(session.credential_id)
                data["session"] = {"expires": session.expires, "key": credential.name if credential else ""}
            return data

    def overview(self, current: Session) -> dict[str, Any]:
        """設定ページ用の一覧 (キー・アクセストークン・セッション)。"""
        with self._lock:
            self._prune(time.time())
            names = {c.id: c.name for c in self._store.credentials}
            sessions = sorted(self._sessions.values(), key=lambda s: s.created, reverse=True)
            return {
                "rp_id": self.origin.rp_id,
                "keys": [c.summary() for c in self._store.credentials],
                "tokens": [t.summary() for t in self._store.tokens],
                "sessions": [
                    {
                        "id": s.id,
                        "key": names.get(s.credential_id, ""),
                        "created": s.created,
                        "expires": s.expires,
                        "last_seen": s.last_seen,
                        "ip": s.ip,
                        "user_agent": s.user_agent,
                    }
                    for s in sessions
                    if self._find(s.credential_id) is not None
                ],
                "current_session": current.id,
                "current_key": current.credential_id,
            }

    # ------------------------------------------------------------------ 登録リンク

    def create_setup_link(self, ttl: float = SETUP_LINK_TTL) -> tuple[str, float]:
        """キーを登録するための 1 回限りの URL と有効期限を返す。

        トークンは URL のフラグメント (# 以降) に置くので、サーバーやトンネル/プロキシのアクセスログには残らない。
        """
        value = secrets.token_urlsafe(24)
        with self._lock:
            now = time.time()
            self._prune(now)
            if len(self._setup_links) >= MAX_SETUP_LINKS:
                del self._setup_links[min(self._setup_links, key=self._setup_links.__getitem__)]
            expires = now + ttl
            self._setup_links[_digest(value)] = expires
        return f"{self.origin.origin}/setup#t={value}", expires

    def create_invite(self) -> tuple[str, float]:
        """ログイン中の利用者が、別の端末でキーを登録するための URL を作る。"""
        url, expires = self.create_setup_link(INVITE_TTL)
        log.info("別の端末用の登録リンクを作成しました")
        return url, expires

    # ------------------------------------------------------------------ 登録

    def register_begin(self, *, setup_token: Any = None, session: Optional[Session] = None) -> dict[str, Any]:
        """キー登録を始める。登録リンクのトークンか、ログイン中のセッションが必要。"""
        with self._lock:
            now = time.time()
            self._prune(now)
            setup_hash = None
            if setup_token:
                if not isinstance(setup_token, str) or _digest(setup_token) not in self._setup_links:
                    raise AuthError(403, "登録リンクが無効か、有効期限が切れています。新しい登録リンクを発行してください")
                setup_hash = _digest(setup_token)
            elif session is None:
                raise AuthError(401, "ログインが必要です")
            if len(self._store.credentials) >= MAX_CREDENTIALS:
                raise AuthError(409, f"登録できるキーは {MAX_CREDENTIALS} 個までです。使っていないキーを削除してください")
            user = PublicKeyCredentialUserEntity(
                id=self._store.user_id, name="kasumin", display_name=f"Kasumin ({self.origin.rp_id})"
            )
            options, state = self._server.register_begin(
                user,
                [c.descriptor() for c in self._active()],  # 同じ認証器での二重登録を防ぐ
                resident_key_requirement=ResidentKeyRequirement.PREFERRED,
                user_verification=self.user_verification,
            )
            ceremony = _Ceremony(
                "register",
                state,
                now + CEREMONY_TTL,
                setup_hash=setup_hash,
                session_id=None if setup_hash else session.id,
            )
            return {"ceremony": self._add_ceremony(ceremony), **dict(options)}

    def register_complete(
        self,
        ceremony_id: Any,
        response: Any,
        *,
        name: Any = "",
        session: Optional[Session] = None,
        ip: str = "",
        user_agent: str = "",
    ) -> tuple[Optional[str], Credential]:
        """キーの登録を完了する。登録リンク経由ならログインもして、(Cookie に入れるセッショントークン, キー) を返す。"""
        with self._lock:
            now = time.time()
            ceremony = self._take_ceremony(ceremony_id, "register", now)
            if ceremony.setup_hash is not None:
                if self._setup_links.get(ceremony.setup_hash, 0) <= now:
                    raise AuthError(403, "登録リンクが無効か、有効期限が切れています。新しい登録リンクを発行してください")
            elif session is None or session.id != ceremony.session_id:
                raise AuthError(401, "ログインが必要です")
            try:
                registration = RegistrationResponse.from_dict(response)
                _check_client_data(registration.response.client_data)
                auth_data = self._server.register_complete(ceremony.state, registration)
            except Exception as exc:  # 外部から来た値の解析・検証の失敗はすべて登録失敗として扱う
                log.warning("キーの登録に失敗しました (%s): %s", ip, exc)
                raise AuthError(400, "キーの登録に失敗しました。もう一度お試しください") from None
            data = auth_data.credential_data
            if data is None or data.public_key.ALGORITHM not in ALGORITHMS:
                raise AuthError(400, "このキーの署名方式には対応していません")
            credential_id = websafe_encode(data.credential_id)
            if len(data.credential_id) > 1023 or any(c.id == credential_id for c in self._store.credentials):
                raise AuthError(409, "このキーは登録済みです")

            transports = response.get("response", {}).get("transports") if isinstance(response, dict) else None
            if not isinstance(transports, list):
                transports = []
            credential = Credential(
                id=credential_id,
                rp_id=self.origin.rp_id,
                name=clean_name(name) or "キー",
                data=data,
                sign_count=auth_data.counter,
                created=now,
                last_used=now,
                transports=list(dict.fromkeys(t for t in transports if isinstance(t, str) and t in TRANSPORTS)),
                attachment=registration.authenticator_attachment.value
                if registration.authenticator_attachment
                else None,
                backup_eligible=auth_data.is_backup_eligible(),
            )
            self._store.credentials.append(credential)
            self._store.save()
            log.info("キー「%s」を登録しました (%s)", credential.name, ip)

            token = None
            if ceremony.setup_hash is not None:
                del self._setup_links[ceremony.setup_hash]
                token, _ = self._new_session(credential.id, ip, user_agent, now)
            return token, credential

    # ------------------------------------------------------------------ ログイン

    def login_begin(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            self._prune(now)
            credentials = self._active()
            if not credentials:
                raise AuthError(409, "キーがまだ登録されていません。サーバーの起動時に表示される登録リンクを開いてください")
            # 登録済みのキーを列挙するので、ユーザー名の入力なしで、端末に保存されないタイプのキーも使える
            options, state = self._server.authenticate_begin(
                [c.descriptor() for c in credentials], user_verification=self.user_verification
            )
            return {"ceremony": self._add_ceremony(_Ceremony("login", state, now + CEREMONY_TTL)), **dict(options)}

    def login_complete(
        self, ceremony_id: Any, response: Any, *, ip: str = "", user_agent: str = ""
    ) -> tuple[str, Session]:
        """署名を検証してログインし、(Cookie に入れるセッショントークン, セッション) を返す。"""
        with self._lock:
            now = time.time()
            ceremony = self._take_ceremony(ceremony_id, "login", now)
            credentials = self._active()
            try:
                assertion = AuthenticationResponse.from_dict(response)
                _check_client_data(assertion.response.client_data)
                matched = self._server.authenticate_complete(ceremony.state, [c.data for c in credentials], assertion)
            except Exception as exc:  # 外部から来た値の解析・検証の失敗はすべてログイン失敗として扱う
                log.warning("ログインに失敗しました (%s): %s", ip, exc)
                raise AuthError(401, "認証に失敗しました。登録済みのキーを使っているか確認してください") from None
            credential = next(c for c in credentials if c.data.credential_id == matched.credential_id)

            # 署名カウンタが増えていなければ、キーが複製された可能性がある (同期パスキーは常に 0 なので対象外)
            counter = assertion.response.authenticator_data.counter
            if (counter or credential.sign_count) and counter <= credential.sign_count:
                log.warning(
                    "キー「%s」の署名カウンタが増えていません (保存値 %d, 今回 %d)。キーが複製された可能性があるため拒否しました (%s)",
                    credential.name,
                    credential.sign_count,
                    counter,
                    ip,
                )
                raise AuthError(401, "このキーの署名カウンタに異常があるため、ログインを拒否しました")
            credential.sign_count = counter
            credential.last_used = now
            self._store.save()
            log.info("キー「%s」でログインしました (%s)", credential.name, ip)
            return self._new_session(credential.id, ip, user_agent, now)

    # ------------------------------------------------------------------ セッション

    def session_cookie(self, token: str) -> str:
        max_age = math.ceil(self.session_ttl)
        attrs = [f"{self.cookie_name}={token}", "Path=/", f"Max-Age={max_age}", "HttpOnly", "SameSite=Strict"]
        if self.origin.secure:
            attrs.append("Secure")
        return "; ".join(attrs)

    def clear_cookie(self) -> str:
        attrs = [f"{self.cookie_name}=", "Path=/", "Max-Age=0", "HttpOnly", "SameSite=Strict"]
        if self.origin.secure:
            attrs.append("Secure")
        return "; ".join(attrs)

    def session(self, token: Optional[str]) -> Optional[Session]:
        """Cookie の値から有効なセッションを返す。期限切れやキー削除済みなら None。"""
        if not token or len(token) > 100:
            return None
        key = _digest(token)
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            now = time.time()
            if session.expires <= now or self._find(session.credential_id) is None:
                del self._sessions[key]
                return None
            session.last_seen = now
            return session

    def session_alive(self, session_id: str) -> bool:
        with self._lock:
            now = time.time()
            return any(
                s.id == session_id and s.expires > now and self._find(s.credential_id) is not None
                for s in self._sessions.values()
            )

    def logout(self, token: Optional[str]) -> None:
        if token:
            with self._lock:
                self._sessions.pop(_digest(token), None)

    def revoke_session(self, session_id: Any) -> None:
        with self._lock:
            keys = [k for k, s in self._sessions.items() if s.id == session_id]
            if not keys:
                raise AuthError(404, "セッションが見つかりません")
            for key in keys:
                del self._sessions[key]

    def revoke_other_sessions(self, current: Session) -> int:
        with self._lock:
            keys = [k for k, s in self._sessions.items() if s.id != current.id]
            for key in keys:
                del self._sessions[key]
            if keys:
                log.info("他のセッション %d 件をログアウトさせました", len(keys))
            return len(keys)

    # ------------------------------------------------------------------ キーの管理

    def delete_credential(self, credential_id: Any) -> Credential:
        """キーを削除し、そのキーでログインしているセッションも無効にする。"""
        with self._lock:
            target = next((c for c in self._store.credentials if c.id == credential_id), None)
            if target is None:
                raise AuthError(404, "キーが見つかりません")
            if target.rp_id == self.origin.rp_id and len(self._active()) <= 1:
                raise AuthError(409, "最後のキーは削除できません。先に別のキーを登録してください")
            self._store.credentials.remove(target)
            self._store.save()
            for key in [k for k, s in self._sessions.items() if s.credential_id == target.id]:
                del self._sessions[key]
            log.info("キー「%s」を削除しました", target.name)
            return target

    # ------------------------------------------------------------------ アクセストークン

    def create_token(self, name: Any) -> tuple[str, AccessToken]:
        """アクセストークンを発行し、(トークン本体, 情報) を返す。本体はこのときしか分からない。"""
        with self._lock:
            if len(self._store.tokens) >= MAX_TOKENS:
                raise AuthError(409, f"発行できるアクセストークンは {MAX_TOKENS} 個までです。使っていないものを削除してください")
            value = "ksm_" + secrets.token_urlsafe(32)
            token = AccessToken(
                id=secrets.token_hex(6), name=clean_name(name) or "アプリ", hash=_digest(value), created=time.time()
            )
            self._store.tokens.append(token)
            self._store.save()
            log.info("アクセストークン「%s」を発行しました", token.name)
            return value, token

    def delete_token(self, token_id: Any) -> AccessToken:
        with self._lock:
            target = next((t for t in self._store.tokens if t.id == token_id), None)
            if target is None:
                raise AuthError(404, "アクセストークンが見つかりません")
            self._store.tokens.remove(target)
            self._store.save()
            log.info("アクセストークン「%s」を削除しました", target.name)
            return target

    def token(self, value: Optional[str]) -> Optional[AccessToken]:
        """アプリから渡されたトークンが有効なら、その情報を返す。"""
        if not value or len(value) > 200:
            return None
        digest = _digest(value)
        with self._lock:
            for token in self._store.tokens:
                if hmac.compare_digest(token.hash, digest):
                    now = time.time()
                    token.last_used = now
                    if now - self._usage_saved >= USAGE_SAVE_INTERVAL:
                        self._usage_saved = now
                        self._store.save()
                    return token
        return None

    def token_alive(self, token_id: str) -> bool:
        with self._lock:
            return any(t.id == token_id for t in self._store.tokens)

    # ------------------------------------------------------------------ 内部処理

    def _active(self) -> list[Credential]:
        """この公開 URL (RP ID) で使えるキー。別のホスト名で登録したキーは使えない。"""
        return [c for c in self._store.credentials if c.rp_id == self.origin.rp_id]

    def _find(self, credential_id: str) -> Optional[Credential]:
        return next((c for c in self._active() if c.id == credential_id), None)

    def _new_session(self, credential_id: str, ip: str, user_agent: str, now: float) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        session = Session(
            id=secrets.token_urlsafe(9),
            credential_id=credential_id,
            created=now,
            expires=now + self.session_ttl,
            last_seen=now,
            ip=ip,
            user_agent=user_agent[:200],
        )
        self._prune(now)
        if len(self._sessions) >= MAX_SESSIONS:
            del self._sessions[min(self._sessions, key=lambda k: self._sessions[k].created)]
        self._sessions[_digest(token)] = session
        return token, session

    def _add_ceremony(self, ceremony: _Ceremony) -> str:
        if len(self._ceremonies) >= MAX_CEREMONIES:
            del self._ceremonies[min(self._ceremonies, key=lambda k: self._ceremonies[k].expires)]
        ceremony_id = secrets.token_urlsafe(16)
        self._ceremonies[ceremony_id] = ceremony
        return ceremony_id

    def _take_ceremony(self, ceremony_id: Any, kind: str, now: float) -> _Ceremony:
        """開始時の状態を取り出す。チャレンジを使い回せないよう、取り出したものは消す。"""
        ceremony = self._ceremonies.pop(ceremony_id, None) if isinstance(ceremony_id, str) else None
        if ceremony is None or ceremony.kind != kind or ceremony.expires <= now:
            raise AuthError(400, "操作の有効期限が切れました。もう一度やり直してください")
        return ceremony

    def _prune(self, now: float) -> None:
        for key in [k for k, s in self._sessions.items() if s.expires <= now]:
            del self._sessions[key]
        for key in [k for k, c in self._ceremonies.items() if c.expires <= now]:
            del self._ceremonies[key]
        for key in [k for k, expires in self._setup_links.items() if expires <= now]:
            del self._setup_links[key]
