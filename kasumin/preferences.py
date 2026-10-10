"""設定画面で変更したカメラの名前と回転の保存。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

from .camera import ROTATIONS, Camera
from .storage import write_json_atomic


class CameraPreferences:
    """カメラごとの名前と回転を JSON ファイルに保存し、起動時に反映する。

    キーはカメラの接続先 (Camera.key) なので、抜き差しや配線の変更で番号が入れ替わっても別のカメラには付かない。
    保存した値は起動オプション (--camera "1,name=..." や --rotate) より優先する。path が None なら保存しない。
    """

    VERSION = 1

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._saved: dict[str, dict[str, Any]] = {}
        if path is not None and path.exists():
            self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != self.VERSION:
                raise ValueError(f"対応していない形式です (version={data.get('version')!r})")
            for key, values in data["cameras"].items():
                name, rotate = values["name"], values["rotate"]
                if not isinstance(name, str) or type(rotate) is not int or rotate not in ROTATIONS:
                    raise ValueError(f"{key} の値が正しくありません")
                self._saved[key] = {"name": name, "rotate": rotate}
        except Exception as exc:  # 壊れたファイルは、どこが悪いか分かるように伝える
            raise ValueError(f"{self.path} を読み込めません: {exc!r}") from exc

    def _save(self) -> None:
        if self.path is not None:
            write_json_atomic(self.path, {"version": self.VERSION, "cameras": self._saved})

    def apply(self, camera: Camera) -> None:
        """保存してある値があれば、そのカメラに反映する。"""
        with self._lock:
            saved = self._saved.get(camera.key)
        if saved is not None:
            camera.configure(**saved)

    def update(self, camera: Camera, *, name: str, rotate: int) -> None:
        with self._lock:
            camera.configure(name=name, rotate=rotate)
            self._saved[camera.key] = {"name": name, "rotate": rotate}
            self._save()

    def reset(self, camera: Camera) -> None:
        """保存した値を消して、起動オプションで決まった名前と回転に戻す。"""
        with self._lock:
            camera.configure(**camera.defaults)
            if self._saved.pop(camera.key, None) is not None:
                self._save()

    def customized(self, camera: Camera) -> bool:
        with self._lock:
            return camera.key in self._saved
