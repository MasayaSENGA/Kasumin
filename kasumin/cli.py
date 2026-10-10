"""コマンドライン引数の解釈とアプリの起動。"""

from __future__ import annotations

import argparse
import logging
import math
import os
import socket
import sys
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, Sequence, Union

from . import __version__

if TYPE_CHECKING:
    from .auth import Fido2Auth
    from .camera import Camera
    from .devices import VideoDevice

log = logging.getLogger("kasumin")


def default_data_dir() -> Path:
    """登録済みのキーなどを保存するフォルダ。Windows では %LOCALAPPDATA%\\Kasumin。"""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Kasumin"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "kasumin"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kasumin",
        description="Windows の Web カメラをブラウザから見られるネットワークカメラ (MJPEG) にします。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    net = parser.add_argument_group("ネットワーク")
    net.add_argument("--host", default="0.0.0.0", help="待ち受けアドレス (LAN に公開しない場合は 127.0.0.1)")
    net.add_argument("-p", "--port", type=int, default=8080, help="待ち受けポート")
    net.add_argument(
        "--user",
        default=os.environ.get("KASUMIN_USER", "admin"),
        help="Basic 認証のユーザー名 (環境変数 KASUMIN_USER でも指定可)",
    )
    net.add_argument(
        "--password",
        default=os.environ.get("KASUMIN_PASSWORD"),
        help="Basic 認証のパスワード。指定すると認証が有効になる (環境変数 KASUMIN_PASSWORD でも指定可)",
    )
    net.add_argument("--tls-cert", metavar="PEM", help="HTTPS で待ち受けるための証明書ファイル (中間証明書を含むもの)")
    net.add_argument("--tls-key", metavar="PEM", help="証明書の秘密鍵ファイル (証明書ファイルに含まれていれば不要)")

    remote = parser.add_argument_group("LAN 外からのアクセス (FIDO2 認証)")
    remote.add_argument(
        "--public-url",
        metavar="URL",
        help="LAN 外から開くときの URL (例: https://cam.example.com)。"
        "指定するとパスキー / セキュリティキーによる FIDO2 認証が有効になる",
    )
    remote.add_argument(
        "--setup-link", action="store_true", help="キーが登録済みでも、新しいキーの登録リンクを表示する (キーをなくしたときなど)"
    )
    remote.add_argument("--session-days", type=float, default=7, metavar="DAYS", help="ブラウザのログイン状態を保つ日数")
    remote.add_argument(
        "--user-verification",
        choices=["required", "preferred"],
        default="required",
        help="キーの PIN / 生体認証を必須にするか。preferred にすると PIN 未設定のセキュリティキーでもログインできる",
    )
    remote.add_argument(
        "--data-dir", default=str(default_data_dir()), metavar="DIR", help="登録済みのキーとアクセストークンの保存先"
    )

    cam = parser.add_argument_group("カメラ")
    cam.add_argument(
        "-c",
        "--camera",
        action="append",
        default=argparse.SUPPRESS,
        metavar="SPEC",
        help="配信するカメラの番号 (--list-cameras で確認。既定は 0)。複数台は --camera 0 --camera 1 のように繰り返す。"
        '"1,name=玄関,rotate=180" のように名前や設定をカメラごとに指定でき、all で接続中のカメラをすべて配信する',
    )
    cam.add_argument("--width", type=int, default=1280, help="要求する横解像度 (0 でカメラの既定値)")
    cam.add_argument("--height", type=int, default=720, help="要求する縦解像度 (0 でカメラの既定値)")
    cam.add_argument("--fps", type=float, default=30, help="要求するフレームレート (0 でカメラの既定値)")
    cam.add_argument(
        "--backend", choices=["dshow", "msmf", "any"], default="dshow", help="OpenCV のキャプチャバックエンド"
    )
    cam.add_argument(
        "--fourcc", default="", help="カメラに要求するピクセル形式 (例: MJPG。高解像度で fps が出ないときに有効)"
    )
    cam.add_argument(
        "--idle-timeout",
        type=float,
        default=0,
        metavar="SEC",
        help="視聴者がいない状態がこの秒数続いたらカメラを解放する (0 で常時キャプチャ)",
    )

    img = parser.add_argument_group("画像")
    img.add_argument("-q", "--quality", type=int, default=80, help="JPEG 品質 (1-100)")
    img.add_argument("--rotate", type=int, choices=[0, 90, 180, 270], default=0, help="回転角度 (時計回り)")
    img.add_argument("--flip", choices=["none", "h", "v", "hv"], default="none", help="反転 (h=左右, v=上下, hv=両方)")
    img.add_argument("--timestamp", action="store_true", help="映像に日時を焼き込む")

    parser.add_argument("--list-cameras", action="store_true", help="利用可能なカメラを表示して終了")
    parser.add_argument("-v", "--verbose", action="store_true", help="詳細ログを出力")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def local_addresses() -> list[str]:
    """この PC の LAN 側 IPv4 アドレスを推定して返す。"""
    addresses: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))  # 実際には送信しない。経路選択で使う自アドレスを得るだけ
            addresses.add(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                addresses.add(ip)
    except OSError:
        pass
    return sorted(addresses)


def _format_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _is_loopback_host(host: str) -> bool:
    return host in ("localhost", "::1") or host.startswith("127.")


def _number(kind: type, value: str) -> Any:
    try:
        number = kind(value)
    except ValueError:
        number = None
    if number is None or not math.isfinite(number):  # float("nan") や "inf" も受け付けない
        raise ValueError("数値で指定してください")
    return number


def _non_negative(kind: type) -> Callable[[str], Any]:
    def convert(value: str) -> Any:
        number = _number(kind, value)
        if number < 0:
            raise ValueError("0 以上で指定してください")
        return number

    return convert


def _choice(choices: Sequence[Any], kind: type = str) -> Callable[[str], Any]:
    def convert(value: str) -> Any:
        result = _number(kind, value) if kind is not str else value.lower()
        if result not in choices:
            raise ValueError(f"{' / '.join(map(str, choices))} のどれかを指定してください")
        return result

    return convert


def _quality(value: str) -> int:
    number = _number(int, value)
    if not 1 <= number <= 100:
        raise ValueError("1〜100 で指定してください")
    return number


def _fourcc(value: str) -> str:
    if len(value) != 4:
        raise ValueError("4 文字で指定してください (例: MJPG)")
    return value.upper()


def _camera_name(value: str) -> str:
    name = " ".join("".join(ch if ch.isprintable() else " " for ch in value).split())
    if not name:
        raise ValueError("名前が空です")
    return name[:40]


# --camera "1,name=玄関,rotate=180" で個別に指定できる設定: キー → (CameraSettings の属性名, 変換と検証)
CAMERA_OPTIONS: dict[str, tuple[str, Callable[[str], Any]]] = {
    "name": ("name", _camera_name),
    "width": ("width", _non_negative(int)),
    "height": ("height", _non_negative(int)),
    "fps": ("fps", _non_negative(float)),
    "rotate": ("rotate", _choice([0, 90, 180, 270], int)),
    "flip": ("flip", _choice(["none", "h", "v", "hv"])),
    "fourcc": ("fourcc", _fourcc),
    "quality": ("jpeg_quality", _quality),
}


def parse_camera_spec(text: str) -> tuple[Union[int, str], dict[str, Any]]:
    """--camera の値を (カメラ番号 または "all", CameraSettings に渡す個別の設定) に分ける。

    例: "1"、"1,name=玄関,rotate=180"、"all"。不正な値なら ValueError (メッセージはそのまま表示する)。
    """
    head, *pairs = [part.strip() for part in text.split(",")]
    if head.lower() == "all":
        if pairs:
            raise ValueError("all には個別の設定を付けられません (全体の設定は --width などで指定してください)")
        return "all", {}
    if not (head.isascii() and head.isdigit()) or int(head) > 99:
        raise ValueError(f"カメラ番号は 0〜99 の数字で指定してください: {text!r}")
    options: dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = (s.strip() for s in pair.partition("="))
        key = key.lower()
        if not sep or key not in CAMERA_OPTIONS:
            raise ValueError(f"「{pair}」は指定できません (使えるキー: {', '.join(CAMERA_OPTIONS)})")
        attr, convert = CAMERA_OPTIONS[key]
        if attr in options:
            raise ValueError(f"{key} が 2 回指定されています: {text!r}")
        try:
            options[attr] = convert(value)
        except ValueError as exc:
            raise ValueError(f"{key}={value}: {exc}") from None
    return int(head), options


def plan_cameras(
    values: Optional[Sequence[str]], devices: Optional[Sequence[VideoDevice]]
) -> list[tuple[int, dict[str, Any]]]:
    """--camera の指定から、配信するカメラの (番号, 個別の設定) の一覧を作る。不正な指定なら ValueError。

    devices は DirectShow で見つかったカメラ (列挙できない環境では None)。カメラの名前と、
    複数台のときに抜き差しで番号がずれても同じカメラを開くための識別情報は、ここから決める。
    """
    specs = [parse_camera_spec(value) for value in (values or ["0"])]
    if any(index == "all" for index, _ in specs):
        if len(specs) > 1:
            raise ValueError("--camera all は、ほかの --camera と一緒には指定できません")
        if devices is None:
            raise ValueError("--camera all は Windows の DirectShow (--backend dshow) でだけ使えます")
        # 1 台も見つからなければ、従来どおりカメラ 0 が接続されるのを待つ
        specs = [(device.index, {}) for device in devices] or [(0, {})]

    seen: set[int] = set()
    for index, _ in specs:
        if index in seen:
            raise ValueError(f"カメラ {index} が 2 回指定されています")
        seen.add(index)

    by_index = {device.index: device for device in devices or []}
    all_names = Counter(device.name for device in devices or [])
    chosen_names = Counter(by_index[index].name for index, _ in specs if index in by_index)
    plan = []
    for index, options in specs:
        options = dict(options)
        device = by_index.get(index)
        if device is not None and device.name:
            if "name" not in options:
                # 同じ機種を 2 台つないだときは番号を付けて区別する
                options["name"] = device.name if chosen_names[device.name] == 1 else f"{device.name} ({index})"
            if len(specs) > 1:
                if device.path:
                    options["device_path"] = device.path
                elif all_names[device.name] == 1:
                    options["device_name"] = device.name
        plan.append((index, options))
    return plan


def _camera_lines(cameras: Sequence[Camera], base: str, suffix: str = "") -> list[str]:
    """カメラの一覧と、アプリから使う URL。1 台なら従来どおり番号なしの URL を表示する。"""
    if len(cameras) == 1:
        camera = cameras[0]
        return [
            f"  カメラ           : [{camera.id}] {camera.label}",
            f"  MJPEG ストリーム : {base}/stream{suffix}",
            f"  スナップショット : {base}/snapshot.jpg{suffix}",
            f"  ステータス (JSON): {base}/api/status{suffix}",
        ]
    lines = [f"  カメラ ({len(cameras)} 台) と MJPEG ストリーム:"]
    lines += [f"    [{c.id}] {c.label}  {base}/camera/{c.id}/stream{suffix}" for c in cameras]
    lines += [
        f"  スナップショット : {base}/camera/<番号>/snapshot.jpg{suffix}",
        f"  ステータス (JSON): {base}/api/status{suffix}  (全カメラ)",
    ]
    if any(not c.settings.fourcc for c in cameras):
        lines.append("  ヒント: 2 台目以降が NO SIGNAL のままなら USB の帯域不足かもしれません (--fourcc MJPG を試してください)")
    return lines


def _print_banner(
    host: str,
    port: int,
    auth_enabled: bool,
    cameras: Sequence[Camera],
    *,
    tls: bool = False,
    fido2: Optional[Fido2Auth] = None,
    setup: Optional[tuple[str, float]] = None,
    others: Sequence[VideoDevice] = (),
) -> None:
    scheme = "https" if tls else "http"
    if host in ("0.0.0.0", "", "::"):
        hosts = ["127.0.0.1", *local_addresses()]
    elif _is_loopback_host(host):
        hosts = ["127.0.0.1"]
    else:
        hosts = [host]
    urls = [f"{scheme}://{_format_host(h)}:{port}" for h in hosts]

    lines = ["", f"Kasumin v{__version__} - ネットワークカメラを起動しました", ""]
    if fido2 is None:
        lines += ["  ブラウザで開く:", *(f"    {url}/" for url in urls), ""]
        lines += _camera_lines(cameras, urls[-1])
        lines += ["", "  Basic 認証: " + ("有効" if auth_enabled else "無効 (--password で有効にできます)")]
    else:
        public = fido2.origin.origin
        active, other = fido2.credential_count()
        lines += [
            f"  ブラウザで開く : {public}/",
            f"  待ち受け       : {', '.join(urls)}",
            f"  認証           : FIDO2 (パスキー / セキュリティキー)  登録済みのキー {active} 個",
            f"  キーの保存先   : {fido2.path}",
        ]
        if other:
            lines.append(f"  ※ 別のホスト名用に登録したキーが {other} 個あります (この URL では使えません)")
        if setup is not None:
            url, expires = setup
            lines += [
                "",
                "  【キーの登録】次の URL をブラウザで開いて、パスキーまたはセキュリティキーを登録してください",
                f"  ({time.strftime('%H:%M', time.localtime(expires))} まで有効・1 回限り。この URL は他の人に見せないでください)",
                f"    {url}",
            ]
        lines += [""]
        lines += _camera_lines(cameras, public, "?token=<アクセストークン>")
        lines += [
            "  (VLC・OBS・Home Assistant などのアプリからは、ログイン後の「設定」で発行するアクセストークンを付けて接続します)"
        ]
    if others:
        found = ", ".join(f"[{d.index}] {d.name or '名前なし'}" for d in others)
        lines += [
            "",
            f"  ほかにも見つかったカメラ: {found}",
            "  (一緒に配信するには --camera 0 --camera 1 のように並べるか、--camera all を指定します)",
        ]
    lines += ["", "  終了するには Ctrl+C を押してください", ""]
    print("\n".join(lines), flush=True)


def _list_cameras(backend: str) -> int:
    from .devices import directshow_devices, probe_cameras, windows_camera_names

    devices = directshow_devices() if backend == "dshow" else None
    if devices is not None:
        # DirectShow ならカメラ番号と名前を正しく対応付けられる。解像度は 1 台ずつ開いて確かめる
        print(f"カメラを確認しています (各カメラを一瞬開きます、backend={backend}) ...", flush=True)
        probed = {cam.index: cam for cam in probe_cameras(backend=backend, indices=[d.index for d in devices])}
        for device in devices:
            cam = probed.get(device.index)
            if cam is None:
                detail = "開けません (ほかのアプリが使用中か、映像を出していない仮想カメラなど)"
            else:
                detail = f"{cam.width}x{cam.height} @ " + (f"{cam.fps:.0f} fps" if cam.fps > 0 else "fps 不明")
            print(f"  [{device.index}] {device.name or '(名前なし)'}  {detail}")
        found = bool(probed)
    else:
        names = windows_camera_names()
        if names:
            print("Windows が認識しているカメラ:")
            for name in names:
                print(f"  - {name}")
            print()
        print(f"OpenCV で開けるカメラを探しています (backend={backend}, index 0-9) ...", flush=True)
        probes = probe_cameras(backend=backend)
        for cam in probes:
            fps = f"{cam.fps:.0f} fps" if cam.fps > 0 else "fps 不明"
            print(f"  [{cam.index}] {cam.width}x{cam.height} @ {fps}")
        found = bool(probes)
    if not found:
        print("  開けるカメラが見つかりませんでした。")
        print("  他のアプリがカメラを使用していないか、Windows の「カメラのプライバシー設定」で")
        print("  デスクトップアプリのアクセスが許可されているか確認し、--backend msmf も試してください。")
        return 1
    print()
    print("使い方: kasumin --camera <番号>   (複数台: --camera 0 --camera 1、接続中のすべて: --camera all)")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # リダイレクトやパイプ経由でも日本語が化けないように出力を UTF-8 に固定する(コンソール直接出力には影響しない)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        # 存在しないインデックスの探索などで出る OpenCV の警告を抑える
        os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
        # fido2 ライブラリは登録・認証のたびに credential ID を INFO で出し、Kasumin 自身のログと重複するので抑える
        logging.getLogger("fido2").setLevel(logging.WARNING)

    if args.list_cameras:
        return _list_cameras(args.backend)

    if not 1 <= args.quality <= 100:
        parser.error("--quality は 1〜100 で指定してください")
    if args.fourcc and len(args.fourcc) != 4:
        parser.error("--fourcc は 4 文字で指定してください (例: MJPG)")
    if args.idle_timeout < 0:
        parser.error("--idle-timeout は 0 以上で指定してください")
    if args.session_days <= 0:
        parser.error("--session-days は 0 より大きい値で指定してください")
    if args.tls_key and not args.tls_cert:
        parser.error("--tls-key は --tls-cert と一緒に指定してください")
    if args.setup_link and not args.public_url:
        parser.error("--setup-link は --public-url と一緒に指定してください")
    if args.public_url and args.password:
        parser.error(
            "--public-url (FIDO2 認証) と --password (Basic 認証) は同時に使えません。"
            "アプリからの接続には、ログイン後の「設定」で発行するアクセストークンを使ってください"
            " (環境変数 KASUMIN_PASSWORD を設定している場合は削除してください)"
        )

    from .devices import directshow_devices

    # カメラの名前と、抜き差しで番号がずれたときの追跡に使う (カメラは開かない)
    devices = directshow_devices() if args.backend == "dshow" else None
    try:
        plan = plan_cameras(getattr(args, "camera", None), devices)
    except ValueError as exc:
        parser.error(f"--camera: {exc}")
    if devices is not None:
        for index, _ in plan:
            if index >= len(devices):
                log.warning("カメラ %d は今は見つかりません (接続されると自動で開きます)", index)
    configured = {index for index, _ in plan}
    others = [d for d in devices or [] if d.index not in configured]

    fido2 = None
    if args.public_url:
        try:
            from .auth import Fido2Auth, parse_public_url
        except ImportError as exc:
            log.error(
                "FIDO2 認証に必要なパッケージがありません (%s)。start.bat を実行し直すか、"
                "pip install -r requirements.txt を実行してください",
                exc,
            )
            return 1
        try:
            origin = parse_public_url(args.public_url)
        except ValueError as exc:
            parser.error(f"--public-url: {exc}")
        if args.tls_cert and not origin.secure:
            parser.error("--tls-cert を使う場合、--public-url は https:// で指定してください")
        try:
            fido2 = Fido2Auth(
                origin,
                Path(args.data_dir) / "auth.json",
                session_ttl=args.session_days * 86400,
                user_verification=args.user_verification,
            )
        except ValueError as exc:
            log.error("%s", exc)
            return 1

    tls = None
    if args.tls_cert:
        from .tls import TLSConfig

        try:
            tls = TLSConfig(args.tls_cert, args.tls_key)
        except OSError as exc:  # ssl.SSLError を含む
            log.error("証明書を読み込めません: %s", exc)
            return 1

    if fido2 is not None and fido2.origin.secure and tls is None and not _is_loopback_host(args.host):
        log.warning(
            "このサーバーは HTTP (暗号化なし) で %s:%d に公開されています。HTTPS を同じ PC のトンネル /"
            " リバースプロキシで終端するなら、--host 127.0.0.1 を指定して LAN からの直接接続を閉じることを推奨します",
            args.host,
            args.port,
        )

    from .camera import Camera, CameraSettings
    from .server import KasuminServer

    # 全カメラ共通の設定。--camera "1,rotate=180" のような個別の指定で上書きする
    common = dict(
        width=args.width,
        height=args.height,
        fps=args.fps,
        backend=args.backend,
        fourcc=args.fourcc.upper(),
        jpeg_quality=args.quality,
        rotate=args.rotate,
        flip=args.flip,
        timestamp=args.timestamp,
        idle_timeout=args.idle_timeout,
    )
    cameras = [Camera(CameraSettings(index=index, **{**common, **options})) for index, options in plan]
    auth = (args.user, args.password) if args.password else None

    for camera in cameras:
        camera.start()
    try:
        server = KasuminServer(args.host, args.port, cameras, auth, fido2=fido2, tls=tls)
    except OSError as exc:
        log.error("%s:%d で待ち受けできません: %s", args.host, args.port, exc)
        for camera in cameras:
            camera.stop()
        return 1

    setup = None
    if fido2 is not None and (args.setup_link or not fido2.has_credentials()):
        setup = fido2.create_setup_link()
    _print_banner(
        args.host,
        args.port,
        auth is not None,
        cameras,
        tls=tls is not None,
        fido2=fido2,
        setup=setup,
        others=others,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        log.info("終了します")
    finally:
        server.server_close()
        for camera in cameras:
            camera.stop()
        if fido2 is not None:
            fido2.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
