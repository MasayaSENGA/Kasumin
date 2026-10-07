"""コマンドライン引数の解釈とアプリの起動。"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
from typing import Optional, Sequence

from . import __version__

log = logging.getLogger("kasumin")


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

    cam = parser.add_argument_group("カメラ")
    cam.add_argument("-c", "--camera", type=int, default=0, help="カメラのインデックス (--list-cameras で確認)")
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


def _print_banner(host: str, port: int, auth_enabled: bool) -> None:
    if host in ("0.0.0.0", "", "::"):
        hosts = ["127.0.0.1", *local_addresses()]
    elif host in ("localhost", "127.0.0.1", "::1"):
        hosts = ["127.0.0.1"]
    else:
        hosts = [host]
    primary = _format_host(hosts[-1])

    lines = ["", f"Kasumin v{__version__} - ネットワークカメラを起動しました", "", "  ブラウザで開く:"]
    lines += [f"    http://{_format_host(h)}:{port}/" for h in hosts]
    lines += [
        "",
        f"  MJPEG ストリーム : http://{primary}:{port}/stream",
        f"  スナップショット : http://{primary}:{port}/snapshot.jpg",
        f"  ステータス (JSON): http://{primary}:{port}/api/status",
        "",
        "  Basic 認証: " + ("有効" if auth_enabled else "無効 (--password で有効にできます)"),
        "",
        "  終了するには Ctrl+C を押してください",
        "",
    ]
    print("\n".join(lines), flush=True)


def _list_cameras(backend: str) -> int:
    from .devices import probe_cameras, windows_camera_names

    names = windows_camera_names()
    if names:
        print("Windows が認識しているカメラ:")
        for name in names:
            print(f"  - {name}")
        print()

    print(f"OpenCV で開けるカメラを探しています (backend={backend}, index 0-9) ...", flush=True)
    found = probe_cameras(backend=backend)
    if not found:
        print("  見つかりませんでした。")
        print("  他のアプリがカメラを使用していないか、Windows の「カメラのプライバシー設定」で")
        print("  デスクトップアプリのアクセスが許可されているか確認し、--backend msmf も試してください。")
        return 1
    for cam in found:
        fps = f"{cam.fps:.0f} fps" if cam.fps > 0 else "fps 不明"
        print(f"  [{cam.index}] {cam.width}x{cam.height} @ {fps}")
    print()
    print("使い方: kasumin --camera <index>")
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

    if args.list_cameras:
        return _list_cameras(args.backend)

    if not 1 <= args.quality <= 100:
        parser.error("--quality は 1〜100 で指定してください")
    if args.fourcc and len(args.fourcc) != 4:
        parser.error("--fourcc は 4 文字で指定してください (例: MJPG)")
    if args.idle_timeout < 0:
        parser.error("--idle-timeout は 0 以上で指定してください")

    from .camera import Camera, CameraSettings
    from .server import KasuminServer

    settings = CameraSettings(
        index=args.camera,
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
    auth = (args.user, args.password) if args.password else None

    camera = Camera(settings)
    camera.start()
    try:
        server = KasuminServer(args.host, args.port, camera, auth)
    except OSError as exc:
        log.error("%s:%d で待ち受けできません: %s", args.host, args.port, exc)
        camera.stop()
        return 1

    _print_banner(args.host, args.port, auth is not None)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        log.info("終了します")
    finally:
        server.server_close()
        camera.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
