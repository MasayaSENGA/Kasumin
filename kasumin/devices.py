"""カメラデバイスの探索。"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass


@dataclass
class ProbedCamera:
    index: int
    width: int
    height: int
    fps: float
    backend: str


def windows_camera_names() -> list[str]:
    """Windows が認識しているカメラデバイス名を返す(OpenCV のインデックスとの対応は保証されない)。"""
    if sys.platform != "win32":
        return []
    command = [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-PnpDevice -Class Camera,Image -Status OK -ErrorAction SilentlyContinue"
        " | Select-Object -ExpandProperty FriendlyName",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def probe_cameras(max_index: int = 10, backend: str = "dshow") -> list[ProbedCamera]:
    """index 0..max_index-1 を順に開いてみて、フレームを取得できたカメラを返す。"""
    import cv2

    from .camera import BACKENDS

    found: list[ProbedCamera] = []
    for index in range(max_index):
        cap = cv2.VideoCapture(index, BACKENDS[backend])
        try:
            if not cap.isOpened():
                continue
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            found.append(ProbedCamera(index, frame.shape[1], frame.shape[0], fps, backend))
        finally:
            cap.release()
    return found
