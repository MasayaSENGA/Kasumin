"""Web カメラのキャプチャと、複数クライアントで共有する JPEG フレームの管理。"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import cv2
import numpy as np

from .devices import directshow_devices

log = logging.getLogger(__name__)

# OpenCV のキャプチャバックエンド。Windows では DirectShow が最も安定して速く開ける。
BACKENDS = {
    "dshow": cv2.CAP_DSHOW,
    "msmf": cv2.CAP_MSMF,
    "any": cv2.CAP_ANY,
}

_ROTATIONS = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}
ROTATIONS = (0, *_ROTATIONS)  # 指定できる回転角度 (時計回り)

# cv2.flip の flipCode: 1=左右反転, 0=上下反転, -1=両方
_FLIPS = {"h": 1, "v": 0, "hv": -1}


@dataclass
class CameraSettings:
    """カメラ関連の設定値。CLI 引数から組み立てる。"""

    index: int = 0
    width: int = 1280
    height: int = 720
    fps: float = 30.0
    backend: str = "dshow"
    fourcc: str = ""
    jpeg_quality: int = 80
    rotate: int = 0
    flip: str = "none"
    timestamp: bool = False
    reconnect_delay: float = 3.0
    idle_timeout: float = 0.0
    read_failure_limit: int = 15
    name: str = ""  # 画面とログに出す名前 (空なら「カメラ <番号>」)
    # 複数台のとき、抜き差しでカメラ番号がずれても同じカメラを開くための識別情報 (DirectShow のみ)。
    # device_path があればそれで、なければ device_name で探す。どちらも空なら index のとおりに開く
    device_path: str = ""
    device_name: str = ""
    # 設定画面で変えた名前・回転を保存するときの、カメラの識別子 (接続先など)。空ならカメラ番号で区別する
    key: str = ""


class Camera:
    """バックグラウンドでカメラを読み続け、最新フレームを配信する。カメラ 1 台につき 1 つ作る。

    - キャプチャは 1 本のスレッドで行い、視聴者が何人いてもカメラは 1 回しか開かない。
    - JPEG エンコードは要求されたときに 1 フレームにつき 1 回だけ行い、結果を全クライアントで共有する。
    - カメラを開けない・抜かれた場合は自動で再接続を試み、その間は「NO SIGNAL」画像を配信する。
    - idle_timeout を設定すると、視聴者がいない間はカメラを解放する(カメラの LED が消える)。
    """

    def __init__(self, settings: CameraSettings) -> None:
        self.settings = settings
        self._cond = threading.Condition()
        self._encode_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # 以下のフィールドは self._cond で保護する
        self._frame: Optional[np.ndarray] = None
        self._seq = 0
        self._jpeg_cache: Optional[tuple[int, bytes]] = None
        self._frame_times: deque[float] = deque(maxlen=90)
        self._clients = 0
        self._last_activity = time.monotonic()
        self._opened = False
        self._idle = False
        self._error: Optional[str] = None
        self._capture_size = (0, 0)
        self._capture_fps = 0.0
        self._backend_used = ""
        self._device_index = settings.index
        self._total_frames = 0
        self._started_at = time.time()
        self._open_error: Optional[str] = None  # 直近に開けなかった理由 (キャプチャスレッドだけが使う)
        # 起動オプションで決まった名前と回転。設定画面の「既定に戻す」でこの値に戻す
        self.defaults = {"name": settings.name, "rotate": settings.rotate}

    @property
    def id(self) -> int:
        """URL (/camera/<id>/...) で使う番号。起動時に指定したカメラ番号で、抜き差しで番号がずれても変わらない。"""
        return self.settings.index

    @property
    def label(self) -> str:
        return self.settings.name or f"カメラ {self.settings.index}"

    @property
    def key(self) -> str:
        """設定画面で変えた値を保存するときの識別子。番号が入れ替わっても同じカメラに付くよう、接続先を優先する。"""
        return self.settings.key or f"index:{self.settings.index}"

    def configure(self, *, name: str, rotate: int) -> None:
        """名前と回転を変える。回転は次に撮影するフレームから反映される。"""
        if rotate not in ROTATIONS:
            raise ValueError(f"回転は {ROTATIONS} のどれかです: {rotate!r}")
        self.settings.name = name
        self.settings.rotate = rotate

    # ------------------------------------------------------------------ 公開 API

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="camera-capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._cond:
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    @property
    def seq(self) -> int:
        with self._cond:
            return self._seq

    def add_client(self) -> int:
        """ストリーム視聴者を登録する。戻り値は登録後の視聴者数。"""
        with self._cond:
            self._clients += 1
            self._last_activity = time.monotonic()
            count = self._clients
        self._wake.set()
        return count

    def remove_client(self) -> int:
        with self._cond:
            self._clients = max(0, self._clients - 1)
            self._last_activity = time.monotonic()
            return self._clients

    def touch(self) -> None:
        """スナップショットなど短時間の利用をカメラに知らせる(アイドル解放からの復帰用)。"""
        with self._cond:
            self._last_activity = time.monotonic()
        self._wake.set()

    def wait_for_frame(self, last_seq: int, timeout: float) -> Optional[tuple[int, bytes]]:
        """last_seq より新しいフレームが届くまで待ち、(seq, jpeg) を返す。タイムアウト時は None。"""
        with self._cond:
            if self._seq == last_seq:
                self._cond.wait_for(lambda: self._seq != last_seq or self._stop.is_set(), timeout)
            if self._seq == last_seq or self._frame is None:
                return None
        return self.get_jpeg()

    def get_jpeg(self) -> Optional[tuple[int, bytes]]:
        """最新フレームの JPEG を返す。同じフレームのエンコードは 1 回しか行わない。"""
        with self._cond:
            if self._frame is None:
                return None
            if self._jpeg_cache is not None and self._jpeg_cache[0] == self._seq:
                return self._jpeg_cache
            seq, frame = self._seq, self._frame

        with self._encode_lock:
            with self._cond:
                # ロック待ちの間に他のクライアントがエンコード済みなら、それを使う
                if self._jpeg_cache is not None and self._jpeg_cache[0] >= seq:
                    return self._jpeg_cache
            ok, buf = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, int(self.settings.jpeg_quality)]
            )
            if not ok:
                return None
            data = buf.tobytes()
            with self._cond:
                if self._jpeg_cache is None or self._jpeg_cache[0] < seq:
                    self._jpeg_cache = (seq, data)
            return seq, data

    def status(self) -> dict:
        """ブラウザ UI や監視用のステータス情報。"""
        with self._cond:
            times = list(self._frame_times)
            frame = self._frame
            data = {
                "id": self.id,
                "name": self.label,
                "opened": self._opened,
                "idle": self._idle,
                "error": self._error,
                "backend": self._backend_used,
                "camera_index": self._device_index,
                "capture_width": self._capture_size[0],
                "capture_height": self._capture_size[1],
                "capture_fps": round(self._capture_fps, 2),
                "clients": self._clients,
                "frames": self._total_frames,
                "jpeg_quality": self.settings.jpeg_quality,
            }
        now = time.monotonic()
        fps = 0.0
        if len(times) >= 2 and now - times[-1] < 2.0 and times[-1] > times[0]:
            fps = (len(times) - 1) / (times[-1] - times[0])
        height, width = (frame.shape[0], frame.shape[1]) if frame is not None else (0, 0)
        data.update(
            {
                "width": width,
                "height": height,
                "fps": round(fps, 1),
                "uptime": round(time.time() - self._started_at, 1),
            }
        )
        return data

    # ------------------------------------------------------------------ 内部処理

    def _run(self) -> None:
        s = self.settings
        while not self._stop.is_set():
            if s.idle_timeout > 0 and not self._has_demand():
                # 視聴者がいない間はカメラを閉じたまま待機する
                self._set_state(opened=False, idle=True)
                self._wake.clear()
                self._wake.wait(timeout=1.0)
                continue

            cap = self._open()
            if cap is None:
                self._set_state(opened=False, error=self._open_error or f"「{self.label}」を開けません")
                self._publish(self._placeholder("NO SIGNAL", f"camera {s.index} not available"), real=False)
                self._stop.wait(s.reconnect_delay)
                continue

            self._set_state(opened=True)
            try:
                self._capture_loop(cap)
            finally:
                cap.release()
                log.info("「%s」を解放しました", self.label)
        self._set_state(opened=False)

    def _capture_loop(self, cap: cv2.VideoCapture) -> None:
        s = self.settings
        failures = 0
        while not self._stop.is_set():
            if s.idle_timeout > 0 and not self._has_demand():
                log.info("視聴者がいないため「%s」を解放します (idle_timeout=%.0fs)", self.label, s.idle_timeout)
                return
            ok, frame = cap.read()
            if not ok or frame is None:
                failures += 1
                if failures >= s.read_failure_limit:
                    log.warning("「%s」からのフレーム取得に連続 %d 回失敗しました。再接続します", self.label, failures)
                    self._set_state(opened=False, error="フレームを取得できません")
                    self._publish(self._placeholder("NO SIGNAL", "reconnecting..."), real=False)
                    self._stop.wait(s.reconnect_delay)
                    return
                time.sleep(0.05)
                continue
            failures = 0
            self._publish(self._transform(frame))

    def _has_demand(self) -> bool:
        with self._cond:
            if self._clients > 0:
                return True
            return (time.monotonic() - self._last_activity) < self.settings.idle_timeout

    def _set_state(self, *, opened: bool, idle: bool = False, error: Optional[str] = None) -> None:
        with self._cond:
            self._opened = opened
            self._idle = idle
            self._error = error
            if not opened:
                self._frame_times.clear()

    def _tracked(self) -> bool:
        s = self.settings
        return s.backend == "dshow" and bool(s.device_path or s.device_name)

    def _resolve_index(self) -> Optional[int]:
        """開くカメラ番号を決める。識別情報があれば今の番号を探し、接続されていなければ None。

        Windows ではカメラを抜き差しすると番号が振り直されるので、番号のまま開くと別のカメラが映ってしまう。
        """
        s = self.settings
        if not self._tracked():
            return s.index
        devices = directshow_devices()
        if devices is None:  # 列挙できないときは指定の番号のまま開く
            return s.index
        for device in devices:
            if (device.path == s.device_path) if s.device_path else (device.name == s.device_name):
                return device.index
        return None

    def _open(self) -> Optional[cv2.VideoCapture]:
        s = self.settings
        self._open_error = None
        index = self._resolve_index()
        if index is None:
            self._open_error = f"「{self.label}」が接続されていません"
            return None
        if index != self._device_index:
            log.info("「%s」のカメラ番号が %d から %d に変わりました", self.label, self._device_index, index)
            with self._cond:
                self._device_index = index
        # 指定バックエンドで開けなければ残りを順に試す。ただし識別情報で探した番号は DirectShow での並びなので、
        # 並びが違う他のバックエンドでは別のカメラを開いてしまう恐れがあり、試さない
        order = [s.backend] + ([] if self._tracked() else [name for name in BACKENDS if name != s.backend])
        for name in order:
            cap = cv2.VideoCapture(index, BACKENDS[name])
            if not cap.isOpened():
                cap.release()
                log.debug("backend=%s では「%s」(番号 %d) を開けませんでした", name, self.label, index)
                continue
            if s.fourcc:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*s.fourcc))
            if s.width > 0:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, s.width)
            if s.height > 0:
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, s.height)
            if s.fps > 0:
                cap.set(cv2.CAP_PROP_FPS, s.fps)
            ok, frame = cap.read()  # ウォームアップ(設定の反映と動作確認)
            if not ok or frame is None:
                cap.release()
                log.debug("backend=%s で「%s」(番号 %d) からフレームを読めませんでした", name, self.label, index)
                continue
            width, height = frame.shape[1], frame.shape[0]
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            with self._cond:
                self._capture_size = (width, height)
                self._capture_fps = fps
                self._backend_used = name
            if name != s.backend:
                log.warning("backend=%s では開けなかったため %s を使用します", s.backend, name)
            log.info(
                "「%s」を開きました (番号 %d, backend=%s, %dx%d, %.1f fps)", self.label, index, name, width, height, fps
            )
            return cap
        return None

    def _publish(self, frame: np.ndarray, real: bool = True) -> None:
        with self._cond:
            self._frame = frame
            self._seq += 1
            if real:
                self._total_frames += 1
                self._frame_times.append(time.monotonic())
            else:
                self._frame_times.clear()
            self._cond.notify_all()

    def _transform(self, frame: np.ndarray) -> np.ndarray:
        s = self.settings
        if s.rotate in _ROTATIONS:
            frame = cv2.rotate(frame, _ROTATIONS[s.rotate])
        if s.flip in _FLIPS:
            frame = cv2.flip(frame, _FLIPS[s.flip])
        if s.timestamp:
            self._draw_timestamp(frame)
        return frame

    @staticmethod
    def _draw_timestamp(frame: np.ndarray) -> None:
        text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        height = frame.shape[0]
        scale = max(0.5, height / 720 * 0.8)
        thickness = max(1, int(round(scale * 2)))
        origin = (int(12 * scale), height - int(14 * scale))
        font = cv2.FONT_HERSHEY_SIMPLEX
        # 黒い縁取りの上に白文字を描いて、明るい背景でも読めるようにする
        cv2.putText(frame, text, origin, font, scale, (0, 0, 0), thickness + 3, cv2.LINE_AA)
        cv2.putText(frame, text, origin, font, scale, (255, 255, 255), thickness, cv2.LINE_AA)

    def _placeholder(self, title: str, detail: str) -> np.ndarray:
        """カメラが使えないときに配信する画像を作る。"""
        width, height = self._capture_size
        if width <= 0 or height <= 0:
            width, height = (self.settings.width or 640, self.settings.height or 360)
        width, height = min(width, 1280), min(height, 720)
        img = np.full((height, width, 3), 24, dtype=np.uint8)
        font = cv2.FONT_HERSHEY_SIMPLEX
        for text, scale, color, offset in (
            (title, height / 300, (80, 80, 200), -int(height * 0.04)),
            (detail, height / 800, (160, 160, 160), int(height * 0.08)),
        ):
            thickness = max(1, int(round(scale * 2)))
            (text_w, text_h), _ = cv2.getTextSize(text, font, scale, thickness)
            origin = ((width - text_w) // 2, height // 2 + text_h // 2 + offset)
            cv2.putText(img, text, origin, font, scale, color, thickness, cv2.LINE_AA)
        return img
