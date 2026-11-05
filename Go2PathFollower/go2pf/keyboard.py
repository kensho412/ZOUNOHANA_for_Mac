"""ターミナルからの 1 文字キー入力（Enter 不要）。Linux / macOS 用。"""
from __future__ import annotations

import logging
import os
import select
import sys
import threading
from typing import Callable

log = logging.getLogger(__name__)


class TerminalKeys:
    def __init__(self, on_key: Callable[[str], None]):
        self.on_key = on_key
        self._stop = threading.Event()
        self._old = None
        self._thread = threading.Thread(target=self._run, name="keys", daemon=True)

    def start(self) -> bool:
        if not sys.stdin.isatty():
            log.warning("標準入力が端末ではないため、キー操作は可視化ウィンドウからのみ受け付けます")
            return False
        import termios
        import tty
        fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(fd)
        tty.setcbreak(fd)  # Ctrl-C (SIGINT) は有効のまま
        self._thread.start()
        return True

    def _run(self) -> None:
        fd = sys.stdin.fileno()
        while not self._stop.is_set():
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                ch = os.read(fd, 1).decode(errors="ignore")
                if ch:
                    self.on_key(ch)

    def stop(self) -> None:
        self._stop.set()
        if self._old is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old)
            self._old = None
