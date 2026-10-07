"""HTTPS (TLS) の設定。

Let's Encrypt などの証明書は数か月ごとに更新されるので、ファイルの更新を検出したら
再起動しなくても次の接続から新しい証明書を使う。
"""

from __future__ import annotations

import logging
import os
import ssl
import threading
import time
from pathlib import Path
from typing import Optional, Union

log = logging.getLogger(__name__)

RELOAD_CHECK_INTERVAL = 60.0  # 証明書ファイルの更新を確認する間隔 (秒)

PathLike = Union[str, "os.PathLike[str]"]


class TLSConfig:
    """証明書と秘密鍵 (PEM) から SSLContext を作る。読み込めない場合は OSError (ssl.SSLError を含む)。"""

    def __init__(self, certfile: PathLike, keyfile: Optional[PathLike] = None) -> None:
        self.certfile = Path(certfile)
        self.keyfile = Path(keyfile) if keyfile else None
        self._lock = threading.Lock()
        self._stamp = self._file_stamp()
        self._context = self._load()
        self._checked = time.monotonic()

    def context(self) -> ssl.SSLContext:
        with self._lock:
            now = time.monotonic()
            if now - self._checked >= RELOAD_CHECK_INTERVAL:
                self._checked = now
                self._reload_if_changed()
            return self._context

    def _file_stamp(self) -> tuple[int, ...]:
        files = [self.certfile] + ([self.keyfile] if self.keyfile else [])
        return tuple(os.stat(f).st_mtime_ns for f in files)

    def _reload_if_changed(self) -> None:
        try:
            stamp = self._file_stamp()
            if stamp == self._stamp:
                return
            context = self._load()
        except OSError as exc:
            # 更新ツールが書き込んでいる途中などで読めないときは、今の証明書のまま次回また試す
            log.warning("証明書を再読み込みできませんでした (現在の証明書を使い続けます): %s", exc)
            return
        self._context, self._stamp = context, stamp
        log.info("証明書の更新を検出したため再読み込みしました")

    def _load(self) -> ssl.SSLContext:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.certfile, self.keyfile)
        return context
