"""カメラデバイスの探索。"""

from __future__ import annotations

import subprocess
import sys
import uuid
from dataclasses import dataclass
from typing import Optional


@dataclass
class ProbedCamera:
    index: int
    width: int
    height: int
    fps: float
    backend: str


@dataclass(frozen=True)
class VideoDevice:
    """DirectShow のビデオ入力デバイス。index は OpenCV (CAP_DSHOW) のカメラ番号と同じ並び。"""

    index: int
    name: str
    path: str  # USB の接続先などを含む一意な ID。仮想カメラなどでは空


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


def directshow_devices() -> Optional[list[VideoDevice]]:
    """DirectShow のビデオ入力デバイスを、OpenCV のカメラ番号と同じ順で返す。カメラは開かない。

    Windows 以外や、列挙に失敗したときは None。
    """
    if sys.platform != "win32":
        return None
    try:
        return _DirectShow().devices()
    except (OSError, ValueError, AttributeError):
        return None


def probe_cameras(max_index: int = 10, backend: str = "dshow", indices: Optional[list[int]] = None) -> list[ProbedCamera]:
    """カメラを順に開いてみて、フレームを取得できたものを返す (indices を省略すると 0..max_index-1)。"""
    import cv2

    from .camera import BACKENDS

    found: list[ProbedCamera] = []
    for index in indices if indices is not None else range(max_index):
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


class _DirectShow:
    """ICreateDevEnum で DirectShow のビデオ入力デバイスを列挙する (ctypes で COM を直接呼ぶ)。

    OpenCV の CAP_DSHOW も同じ列挙順でカメラ番号を振るので、番号と名前を正しく対応付けられる。
    """

    CLSCTX_INPROC_SERVER = 1
    COINIT_MULTITHREADED = 0  # OpenCV の DirectShow 実装と同じアパートメント
    RPC_E_CHANGED_MODE = -2147417850  # 0x80010106: スレッドが別の方式で初期化済み (そのまま使える)
    VT_BSTR = 8

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        class VARIANT(ctypes.Structure):
            # vt と予約領域 (計 8 バイト) の後に値。64 ビットでは 24 バイト、32 ビットでは 16 バイト
            _fields_ = [("vt", ctypes.c_ushort), ("reserved", ctypes.c_ushort * 3), ("value", ctypes.c_void_p),
                        ("extra", ctypes.c_void_p)]

        self.GUID, self.VARIANT = GUID, VARIANT
        p = ctypes.POINTER
        self.ole32 = ole32 = ctypes.WinDLL("ole32")
        self.oleaut32 = oleaut32 = ctypes.WinDLL("oleaut32")
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        ole32.CoInitializeEx.restype = ctypes.c_long
        ole32.CoUninitialize.argtypes = []
        ole32.CoUninitialize.restype = None
        ole32.CoCreateInstance.argtypes = [p(GUID), ctypes.c_void_p, wintypes.DWORD, p(GUID), p(ctypes.c_void_p)]
        ole32.CoCreateInstance.restype = ctypes.c_long
        oleaut32.VariantClear.argtypes = [p(VARIANT)]
        oleaut32.VariantClear.restype = ctypes.c_long

    def _guid(self, text: str):
        return self.GUID.from_buffer_copy(uuid.UUID(text).bytes_le)

    def _call(self, obj, index: int, argtypes: tuple, *args) -> int:
        """COM インターフェイスの vtable の index 番目のメソッドを呼ぶ。"""
        ctypes = self.ctypes
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        method = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[index])
        return method(obj, *args)

    def _release(self, obj) -> None:
        if obj:
            ctypes = self.ctypes
            vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(obj)  # IUnknown::Release

    def devices(self) -> list[VideoDevice]:
        hr = self.ole32.CoInitializeEx(None, self.COINIT_MULTITHREADED)
        if hr < 0 and hr != self.RPC_E_CHANGED_MODE:
            raise OSError(f"CoInitializeEx failed: 0x{hr & 0xFFFFFFFF:08x}")
        try:
            return self._enumerate()
        finally:
            if hr >= 0:  # S_OK / S_FALSE のときだけ、初期化した分を戻す
                self.ole32.CoUninitialize()

    def _enumerate(self) -> list[VideoDevice]:
        ctypes = self.ctypes
        c_void_p, p = ctypes.c_void_p, ctypes.POINTER
        dev_enum, enum_moniker = c_void_p(), c_void_p()
        hr = self.ole32.CoCreateInstance(
            self._guid("62BE5D10-60EB-11d0-BD3B-00A0C911CE86"),  # CLSID_SystemDeviceEnum
            None,
            self.CLSCTX_INPROC_SERVER,
            self._guid("29840822-5B84-11D0-BD3B-00A0C911CE86"),  # IID_ICreateDevEnum
            ctypes.byref(dev_enum),
        )
        if hr < 0:
            raise OSError(f"CoCreateInstance failed: 0x{hr & 0xFFFFFFFF:08x}")
        try:
            # ICreateDevEnum::CreateClassEnumerator(CLSID_VideoInputDeviceCategory)。カメラがなければ S_FALSE で NULL
            category = self._guid("860BB310-5D01-11d0-BD3B-00A0C911CE86")
            hr = self._call(dev_enum, 3, (p(self.GUID), p(c_void_p), ctypes.c_ulong), ctypes.byref(category),
                            ctypes.byref(enum_moniker), 0)
            if hr < 0:
                raise OSError(f"CreateClassEnumerator failed: 0x{hr & 0xFFFFFFFF:08x}")
            found: list[VideoDevice] = []
            while enum_moniker:
                moniker, fetched = c_void_p(), ctypes.c_ulong()
                # IEnumMoniker::Next(1, &moniker, &fetched)
                hr = self._call(enum_moniker, 3, (ctypes.c_ulong, p(c_void_p), p(ctypes.c_ulong)), 1,
                                ctypes.byref(moniker), ctypes.byref(fetched))
                if hr != 0 or not fetched.value:
                    break
                try:
                    props = self._properties(moniker)
                finally:
                    self._release(moniker)
                found.append(VideoDevice(len(found), props.get("FriendlyName", ""), props.get("DevicePath", "")))
            return found
        finally:
            self._release(enum_moniker)
            self._release(dev_enum)

    def _properties(self, moniker) -> dict[str, str]:
        ctypes = self.ctypes
        c_void_p, p = ctypes.c_void_p, ctypes.POINTER
        bag = c_void_p()
        # IMoniker::BindToStorage(NULL, NULL, IID_IPropertyBag, &bag)
        iid = self._guid("55272A00-42CB-11CE-8135-00AA004BB851")
        hr = self._call(moniker, 9, (c_void_p, c_void_p, p(self.GUID), p(c_void_p)), None, None,
                        ctypes.byref(iid), ctypes.byref(bag))
        if hr < 0:
            return {}
        props: dict[str, str] = {}
        try:
            for name in ("FriendlyName", "DevicePath"):
                var = self.VARIANT()
                # IPropertyBag::Read(name, &var, NULL)
                hr = self._call(bag, 3, (ctypes.c_wchar_p, p(self.VARIANT), c_void_p), name, ctypes.byref(var), None)
                try:
                    if hr == 0 and var.vt == self.VT_BSTR and var.value:
                        props[name] = ctypes.wstring_at(var.value)
                finally:
                    self.oleaut32.VariantClear(ctypes.byref(var))
        finally:
            self._release(bag)
        return props
