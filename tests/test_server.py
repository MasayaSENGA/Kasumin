"""HTTP サーバーとカメラ管理の結合テスト。実カメラの代わりに合成フレームを使う。"""

from __future__ import annotations

import base64
import json
import threading
import time
import unittest
import urllib.error
import urllib.request

import numpy as np

from kasumin.camera import Camera, CameraSettings
from kasumin.server import BOUNDARY, KasuminServer


def wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class FakeCapture:
    """cv2.VideoCapture の最小限の代替。読むたびに明るさが変わる 160x120 のフレームを返す。"""

    def __init__(self):
        self.count = 0

    def read(self):
        self.count += 1
        time.sleep(0.02)  # 50 fps 相当
        return True, np.full((120, 160, 3), self.count % 256, dtype=np.uint8)

    def release(self):
        pass


class FakeCamera(Camera):
    def _open(self):
        with self._cond:
            self._capture_size = (160, 120)
            self._capture_fps = 50.0
            self._backend_used = "fake"
        return FakeCapture()


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

    def test_status(self):
        self.assertTrue(wait_until(lambda: self.camera.status()["opened"]))
        with self.get("/api/status") as res:
            data = json.loads(res.read())
        self.assertTrue(data["opened"])
        self.assertEqual((data["width"], data["height"]), (160, 120))
        self.assertEqual(data["backend"], "fake")

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
