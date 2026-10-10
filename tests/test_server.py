"""HTTP サーバーとカメラ管理の結合テスト。実カメラの代わりに合成フレームを使う。"""

from __future__ import annotations

import base64
import http.client
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import cv2
import numpy as np

from kasumin.camera import Camera, CameraSettings
from kasumin.devices import VideoDevice
from kasumin.server import BOUNDARY, KasuminServer, split_camera_path


def wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class FakeCapture:
    """cv2.VideoCapture の最小限の代替。読むたびに明るさが変わるフレームを返す (既定 160x120)。"""

    def __init__(self, width=160, height=120):
        self.count = 0
        self.size = (height, width)

    def read(self):
        self.count += 1
        time.sleep(0.02)  # 50 fps 相当
        return True, np.full((*self.size, 3), self.count % 256, dtype=np.uint8)

    def release(self):
        pass


class FakeCamera(Camera):
    """設定の解像度 (width / height) のフレームを返す合成カメラ。"""

    def _open(self):
        width, height = self.settings.width or 160, self.settings.height or 120
        with self._cond:
            self._capture_size = (width, height)
            self._capture_fps = 50.0
            self._backend_used = "fake"
        return FakeCapture(width, height)


class BrokenCamera(Camera):
    """常に開けないカメラ。NO SIGNAL 画像の配信を確認するために使う。"""

    def _open(self):
        return None


class ServerBase(unittest.TestCase):
    camera_cls = FakeCamera
    settings = CameraSettings(width=160, height=120, timestamp=True)
    auth = None

    @classmethod
    def setUpClass(cls):
        cls.camera = cls.camera_cls(cls.settings)
        cls.camera.start()
        cls.server = KasuminServer("127.0.0.1", 0, cls.camera, cls.auth)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.camera.stop()

    def get(self, path, headers=None):
        request = urllib.request.Request(self.base + path, headers=headers or {})
        return urllib.request.urlopen(request, timeout=5)


class ServerTest(ServerBase):
    def test_index_html(self):
        with self.get("/") as res:
            self.assertEqual(res.status, 200)
            self.assertIn("text/html", res.headers["Content-Type"])
            self.assertIn(b"Kasumin", res.read())

    def test_static_files(self):
        for path in ("/static/kasumin.css", "/static/viewer.js"):
            with self.get(path) as res:
                self.assertEqual(res.status, 200)
                self.assertEqual(res.headers["X-Content-Type-Options"], "nosniff")
        with self.get("/") as res:
            self.assertIn("script-src 'self'", res.headers["Content-Security-Policy"])

    def test_login_pages_need_fido2(self):
        for path in ("/login", "/settings", "/api/auth/state"):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.get(path)
            self.assertEqual(ctx.exception.code, 404)
            ctx.exception.close()

    def test_status(self):
        self.assertTrue(wait_until(lambda: self.camera.status()["opened"]))
        with self.get("/api/status") as res:
            data = json.loads(res.read())
        self.assertTrue(data["opened"])
        self.assertEqual((data["width"], data["height"]), (160, 120))
        self.assertEqual(data["backend"], "fake")
        self.assertEqual(data["auth_mode"], "none")

    def test_snapshot_is_jpeg(self):
        with self.get("/snapshot.jpg") as res:
            self.assertEqual(res.headers["Content-Type"], "image/jpeg")
            body = res.read()
        self.assertTrue(body.startswith(b"\xff\xd8"))
        self.assertTrue(body.endswith(b"\xff\xd9"))

    def test_stream_delivers_frames_and_tracks_clients(self):
        with self.get("/stream?fps=20") as res:
            self.assertIn(f"boundary={BOUNDARY}", res.headers["Content-Type"])
            self.assertTrue(wait_until(lambda: self.camera.status()["clients"] == 1))
            marker = b"--" + BOUNDARY.encode()
            buf = b""
            deadline = time.monotonic() + 5
            while buf.count(marker) < 3 and time.monotonic() < deadline:
                buf += res.read(4096)
        self.assertGreaterEqual(buf.count(marker), 3)
        self.assertIn(b"Content-Type: image/jpeg", buf)
        self.assertIn(b"\xff\xd8", buf)
        # 切断後は視聴者数が 0 に戻る
        self.assertTrue(wait_until(lambda: self.camera.status()["clients"] == 0))

    def test_stream_wire_format(self):
        # VLC や ffmpeg が読む multipart の形式: 区切り → ヘッダー → JPEG → CRLF → 次の区切り
        delimiter = b"--" + BOUNDARY.encode() + b"\r\n"
        with self.get("/stream") as res:
            buf = b""
            deadline = time.monotonic() + 5
            while buf.count(delimiter) < 3 and time.monotonic() < deadline:
                buf += res.read(1024)
        self.assertTrue(buf.startswith(delimiter + b"Content-Type: image/jpeg\r\nContent-Length: "))
        header_end = buf.index(b"\r\n\r\n")
        length = int(buf[len(delimiter) : header_end].split(b"Content-Length: ")[1])
        body = header_end + 4
        self.assertTrue(buf[body : body + length].startswith(b"\xff\xd8"))
        self.assertEqual(buf[body + length : body + length + 2 + len(delimiter)], b"\r\n" + delimiter)

    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/nope")
        self.assertEqual(ctx.exception.code, 404)


class AuthTest(ServerBase):
    auth = ("user", "pass")

    def test_rejects_without_credentials(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/status")
        self.assertEqual(ctx.exception.code, 401)
        self.assertIn("Basic", ctx.exception.headers["WWW-Authenticate"])

    def test_rejects_wrong_password(self):
        token = base64.b64encode(b"user:wrong").decode()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/status", {"Authorization": f"Basic {token}"})
        self.assertEqual(ctx.exception.code, 401)

    def test_accepts_correct_credentials(self):
        token = base64.b64encode(b"user:pass").decode()
        with self.get("/api/status", {"Authorization": f"Basic {token}"}) as res:
            self.assertEqual(res.status, 200)
            self.assertEqual(json.loads(res.read())["auth_mode"], "basic")


class NoSignalTest(ServerBase):
    camera_cls = BrokenCamera
    settings = CameraSettings(width=160, height=120, reconnect_delay=0.2)

    def test_placeholder_is_served_while_camera_is_down(self):
        self.assertTrue(wait_until(lambda: self.camera.status()["error"]))
        with self.get("/api/status") as res:
            data = json.loads(res.read())
        self.assertFalse(data["opened"])
        self.assertTrue(data["error"])
        with self.get("/snapshot.jpg") as res:
            self.assertTrue(res.read().startswith(b"\xff\xd8"))


def jpeg_size(data: bytes) -> tuple[int, int]:
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    return image.shape[1], image.shape[0]


class MultiCameraTest(unittest.TestCase):
    """2 台のカメラ (番号 0 と 2) を配信する。番号ごとに解像度を変えて、どちらの映像かを見分ける。"""

    @classmethod
    def setUpClass(cls):
        cls.cameras = [
            FakeCamera(CameraSettings(index=0, width=160, height=120, name="リビング")),
            FakeCamera(CameraSettings(index=2, width=320, height=240, name="玄関")),
        ]
        for camera in cls.cameras:
            camera.start()
        cls.server = KasuminServer("127.0.0.1", 0, cls.cameras)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for camera in cls.cameras:
            camera.stop()

    def get(self, path):
        return urllib.request.urlopen(self.base + path, timeout=5)

    def status_code(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            res = conn.getresponse()
            res.read()
            return res.status, res.headers
        finally:
            conn.close()

    def test_status_lists_every_camera(self):
        self.assertTrue(wait_until(lambda: all(c.status()["opened"] for c in self.cameras)))
        with self.get("/api/status") as res:
            data = json.loads(res.read())
        self.assertEqual([c["id"] for c in data["cameras"]], [0, 2])
        self.assertEqual([c["name"] for c in data["cameras"]], ["リビング", "玄関"])
        self.assertEqual([(c["width"], c["height"]) for c in data["cameras"]], [(160, 120), (320, 240)])
        # 1 台目の状態は、1 台だけだったときと同じくトップレベルにもある
        self.assertEqual((data["id"], data["width"], data["height"]), (0, 160, 120))

        with self.get("/camera/2/api/status") as res:
            data = json.loads(res.read())
        self.assertEqual((data["id"], data["name"], data["width"]), (2, "玄関", 320))
        self.assertNotIn("cameras", data)

    def test_snapshot_comes_from_the_requested_camera(self):
        cases = {
            "/snapshot.jpg": (160, 120),  # 番号なしは 1 台目
            "/camera/0/snapshot.jpg": (160, 120),
            "/camera/2/snapshot.jpg": (320, 240),
            "/camera/2/image.jpg": (320, 240),
        }
        for path, size in cases.items():
            with self.subTest(path), self.get(path) as res:
                self.assertEqual(jpeg_size(res.read()), size)

    def test_stream_counts_viewers_per_camera(self):
        with self.get("/camera/2/stream?fps=20") as res:
            self.assertTrue(wait_until(lambda: self.cameras[1].status()["clients"] == 1))
            self.assertEqual(self.cameras[0].status()["clients"], 0)
            buf = b""
            deadline = time.monotonic() + 5
            while buf.count(b"\xff\xd9") < 2 and time.monotonic() < deadline:
                buf += res.read(4096)
        start = buf.index(b"\xff\xd8")
        self.assertEqual(jpeg_size(buf[start : buf.index(b"\xff\xd9", start) + 2]), (320, 240))
        self.assertTrue(wait_until(lambda: self.cameras[1].status()["clients"] == 0))

    def test_unknown_camera_or_path_is_404(self):
        for path in ("/camera/1/stream", "/camera/999/snapshot.jpg", "/camera/2/nope", "/camera/x/stream", "/camera/2/index.html"):
            with self.subTest(path):
                self.assertEqual(self.status_code(path)[0], 404)

    def test_camera_page_redirects_to_viewer(self):
        status, headers = self.status_code("/camera/2/")
        self.assertEqual(status, 303)
        self.assertEqual(headers["Location"], "/#camera=2")

    def test_duplicate_camera_numbers_are_rejected(self):
        twins = [FakeCamera(CameraSettings(index=1)), FakeCamera(CameraSettings(index=1))]
        with self.assertRaises(ValueError):
            KasuminServer("127.0.0.1", 0, twins)


class SplitCameraPathTest(unittest.TestCase):
    def test_split(self):
        self.assertEqual(split_camera_path("/camera/3/stream"), (3, "/stream"))
        self.assertEqual(split_camera_path("/camera/12/api/status"), (12, "/api/status"))
        self.assertEqual(split_camera_path("/camera/3"), (3, "/"))
        self.assertEqual(split_camera_path("/stream"), (None, "/stream"))
        self.assertEqual(split_camera_path("/camera/abc/stream"), (None, "/camera/abc/stream"))
        self.assertEqual(split_camera_path("/xcamera/3/stream"), (None, "/xcamera/3/stream"))


class DeviceTrackingTest(unittest.TestCase):
    """抜き差しでカメラ番号がずれても、記録した接続先 (デバイスパス / 名前) のカメラを開く。実カメラは開かない。"""

    def closed_capture(self):
        capture = mock.MagicMock()
        capture.isOpened.return_value = False
        return capture

    def test_moved_camera_is_found_by_device_path(self):
        camera = Camera(CameraSettings(index=1, name="玄関", device_path="usb#b"))
        moved = [VideoDevice(0, "C920", "usb#b"), VideoDevice(1, "C920", "usb#c")]
        with mock.patch("kasumin.camera.directshow_devices", return_value=moved):
            self.assertEqual(camera._resolve_index(), 0)

    def test_camera_without_path_is_found_by_unique_name(self):
        camera = Camera(CameraSettings(index=0, device_name="OBS Virtual Camera"))
        devices = [VideoDevice(0, "C920", "usb#b"), VideoDevice(1, "OBS Virtual Camera", "")]
        with mock.patch("kasumin.camera.directshow_devices", return_value=devices):
            self.assertEqual(camera._resolve_index(), 1)

    def test_unplugged_camera_is_not_opened(self):
        camera = Camera(CameraSettings(index=1, name="玄関", device_path="usb#b", reconnect_delay=0.1))
        others = [VideoDevice(0, "C920", "usb#c")]  # 玄関のカメラが抜かれ、別のカメラが番号 0 になった
        with mock.patch("kasumin.camera.directshow_devices", return_value=others), mock.patch.object(
            cv2, "VideoCapture"
        ) as capture:
            camera.start()
            try:
                self.assertTrue(wait_until(lambda: camera.status()["error"]))
                self.assertEqual(camera.status()["error"], "「玄関」が接続されていません")
            finally:
                camera.stop()
            capture.assert_not_called()  # 番号 1 のまま開いて、別のカメラを映してしまわない

    def test_tracked_camera_only_uses_directshow(self):
        # 番号は DirectShow での並びなので、並びの違う他のバックエンドでは開き直さない
        camera = Camera(CameraSettings(index=0, device_path="usb#b"))
        with mock.patch("kasumin.camera.directshow_devices", return_value=[VideoDevice(0, "C920", "usb#b")]), \
             mock.patch.object(cv2, "VideoCapture", return_value=self.closed_capture()) as capture:
            self.assertIsNone(camera._open())
        self.assertEqual([c.args for c in capture.call_args_list], [(0, cv2.CAP_DSHOW)])

    def test_untracked_camera_opens_by_number_with_fallbacks(self):
        camera = Camera(CameraSettings(index=3))
        with mock.patch("kasumin.camera.directshow_devices") as devices, \
             mock.patch.object(cv2, "VideoCapture", return_value=self.closed_capture()) as capture:
            self.assertIsNone(camera._open())
        devices.assert_not_called()
        self.assertEqual([c.args for c in capture.call_args_list], [(3, cv2.CAP_DSHOW), (3, cv2.CAP_MSMF), (3, cv2.CAP_ANY)])

    def test_number_is_kept_when_devices_cannot_be_listed(self):
        camera = Camera(CameraSettings(index=2, device_path="usb#b"))
        with mock.patch("kasumin.camera.directshow_devices", return_value=None):
            self.assertEqual(camera._resolve_index(), 2)

    def test_status_shows_name(self):
        self.assertEqual(Camera(CameraSettings(index=2, name="玄関")).status()["name"], "玄関")
        self.assertEqual(Camera(CameraSettings(index=2)).status()["name"], "カメラ 2")


class IdleReleaseTest(unittest.TestCase):
    def test_camera_released_when_idle_and_reopened_on_demand(self):
        camera = FakeCamera(CameraSettings(width=160, height=120, idle_timeout=0.3))
        camera.start()
        try:
            self.assertTrue(wait_until(lambda: camera.status()["opened"]))
            self.assertTrue(wait_until(lambda: camera.status()["idle"], timeout=3))
            self.assertFalse(camera.status()["opened"])
            camera.add_client()
            self.assertTrue(wait_until(lambda: camera.status()["opened"], timeout=3))
            self.assertIsNotNone(camera.wait_for_frame(camera.seq, timeout=3))
            camera.remove_client()
            self.assertTrue(wait_until(lambda: camera.status()["idle"], timeout=3))
        finally:
            camera.stop()


if __name__ == "__main__":
    unittest.main()
