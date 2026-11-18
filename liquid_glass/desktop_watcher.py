"""liquid_glass/desktop_watcher.py -- 桌面捷徑檔案背景監控與防自我迴圈去抖引擎。

Functionality:
1. 監聽個人與公用桌面資料夾異動（Windows 使用 ReadDirectoryChangesW，非 Windows 退回輪詢）。
2. 提供去抖（debounce_sec）、最大等待上限（WATCHER_MAX_WAIT_SEC）與自我寫入時間視窗過濾。
3. 僅針對 WATCHER_SCOPES（桌面）觸發增量重新渲染，不干擾工作列／開始功能表即時引擎。
"""
from __future__ import annotations

from collections import namedtuple
import ctypes
from ctypes import wintypes
import os
import struct
import threading
import time
from typing import Any, Callable

from liquid_glass import runtime as RT
from liquid_glass import shortcut_manager as SM
from liquid_glass import specs as S

# Lines 23-40: 監聽目標結構與 Win32 過濾旗標
WatchTarget = namedtuple("WatchTarget", "path scope recursive")
_NOTIFY_FILTER = 0x1 | 0x2 | 0x8 | 0x10 | 0x40  # 檔名 | 目錄名 | 大小 | 最後寫入 | 建立


def watch_targets() -> list[WatchTarget]:
    """取得實際存在的監看目錄清單（個人桌面與公用桌面）。"""
    out: list[WatchTarget] = []
    seen: set[tuple[str, str]] = set()
    for sc in S.WATCHER_SCOPES:
        for directory, _src, rec in SM.scope_roots(sc):
            key = (sc, RT._norm(directory))
            if key not in seen:
                seen.add(key)
                out.append(WatchTarget(directory, sc, rec))
    return out


# Lines 42-135: 輪詢與 Windows ReadDirectoryChangesW 後端執行緒
class _PollBackend(threading.Thread):
    """跨平台輪詢式目錄監控（或作為 Win32 開啟失敗時的保底實作）。"""

    def __init__(
        self,
        target: WatchTarget,
        feed: Callable[[str, str | None], Any],
        stop_evt: threading.Event,
        interval: float = 2.0,
    ) -> None:
        super().__init__(daemon=True, name="lgfw-watch-poll")
        self.target = target
        self._feed = feed
        self._stop_evt = stop_evt
        self._interval = interval

    def _snap(self) -> dict[str, tuple[int, int]]:
        snap: dict[str, tuple[int, int]] = {}
        try:
            if self.target.recursive:
                for dp, dn, fns in os.walk(self.target.path):
                    for name in dn + fns:
                        self._add(snap, os.path.join(dp, name))
            else:
                for entry in os.scandir(self.target.path):
                    self._add(snap, entry.path)
        except OSError:
            pass
        return snap

    @staticmethod
    def _add(snap: dict[str, tuple[int, int]], path: str) -> None:
        try:
            st = os.stat(path)
            snap[path] = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass

    def run(self) -> None:
        prev = self._snap()
        while not self._stop_evt.wait(self._interval):
            cur = self._snap()
            for path in set(prev) | set(cur):
                if prev.get(path) != cur.get(path):
                    self._feed(self.target.scope, path)
            prev = cur

    def cancel(self) -> None:
        """結束輪詢執行緒之介面相容預留。"""


class _WinBackend(_PollBackend):
    """Windows ReadDirectoryChangesW 同步阻塞監聽（停止時以 CancelIoEx 立即喚醒）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._h: Any = None

    def _open(self) -> Any:
        try:
            handle = RT.kernel32.CreateFileW(self.target.path, 0x0001, 0x1 | 0x2 | 0x4, None, 3, 0x02000000, None)
            if handle is None or handle == ctypes.c_void_p(-1).value:
                return None
            return handle
        except Exception:
            return None

    def run(self) -> None:
        handle = self._open()
        if not handle:
            super().run()
            return
        self._h = handle
        scope, base = self.target.scope, self.target.path
        buf = ctypes.create_string_buffer(65536)
        ret = wintypes.DWORD(0)
        try:
            while not self._stop_evt.is_set():
                ok = RT.kernel32.ReadDirectoryChangesW(
                    handle,
                    buf,
                    len(buf),
                    bool(self.target.recursive),
                    _NOTIFY_FILTER,
                    ctypes.byref(ret),
                    None,
                    None,
                )
                if self._stop_evt.is_set():
                    break
                if not ok:
                    if self._stop_evt.wait(2.0):
                        break
                    continue
                if ret.value == 0:
                    self._feed(scope, None)
                    continue
                raw, offset = buf.raw[: ret.value], 0
                while True:
                    nxt, _act, ln = struct.unpack_from("<III", raw, offset)
                    name = raw[offset + 12 : offset + 12 + ln].decode("utf-16-le", "ignore")
                    self._feed(scope, os.path.join(base, name))
                    if not nxt:
                        break
                    offset += nxt
        finally:
            self._h = None
            try:
                RT.kernel32.CloseHandle(handle)
            except Exception:
                pass

    def cancel(self) -> None:
        handle = self._h
        if handle:
            try:
                RT.kernel32.CancelIoEx(handle, None)
            except Exception:
                pass


# Lines 137-265: IconWatcher 去抖與事件調度器
class IconWatcher:
    """背景監聽桌面捷徑變化並進行去抖與自我迴圈防護。"""

    def __init__(
        self,
        fire: Callable[[list[str]], Any],
        debounce_sec: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        targets_fn: Callable[[], list[WatchTarget]] | None = None,
    ) -> None:
        self._fire = fire
        self._clock = clock
        self._targets_fn = targets_fn or watch_targets
        self._debounce = S.clamp_watcher({"debounce_sec": debounce_sec})["debounce_sec"]
        self._cv = threading.Condition()
        self._scopes: set[str] = set()
        self._first: float | None = None
        self._last: float | None = None
        self._next_rescan: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._backends: list[_PollBackend] = []
        self.ignored = 0
        self.fired = 0

    @property
    def debounce_sec(self) -> float:
        return self._debounce

    def configure(self, debounce_sec: Any) -> None:
        """動態更新去抖等待秒數。"""
        with self._cv:
            self._debounce = S.clamp_watcher({"debounce_sec": debounce_sec})["debounce_sec"]
            self._cv.notify_all()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @staticmethod
    def _relevant(scope: str, path: Any) -> bool:
        name = os.path.basename(str(path))
        if name == RT.SELF_SHORTCUT_NAME or name.lower() == "desktop.ini":
            return False
        if S.shortcut_ext(scope, name):
            return True
        return os.path.splitext(name)[1] == ""

    def feed(self, scope: str, path: str | None = None, now: float | None = None) -> bool:
        """輸入檔案異動事件；若屬於非監看區域、無關副檔名或剛自我寫入則略過。"""
        if scope not in S.WATCHER_SCOPES:
            return False
        if path is not None and not self._relevant(scope, path):
            return False
        cur = self._clock() if now is None else now
        last = SM._SELF_WRITE["t"]
        if last is not None and 0 <= cur - last < S.WATCHER_SELF_IGNORE_SEC:
            self.ignored += 1
            return False
        with self._cv:
            if not self._scopes:
                self._first = cur
            self._scopes.add(scope)
            self._last = cur
            self._cv.notify_all()
        return True

    def take_due(self, now: float | None = None) -> list[str]:
        """若已滿足安靜等待時間或達到最長等待上限，取出待處理區域名單。"""
        cur = self._clock() if now is None else now
        with self._cv:
            if not self._scopes or self._last is None or self._first is None:
                return []
            quiet = cur - self._last >= self._debounce
            maxed = cur - self._first >= S.WATCHER_MAX_WAIT_SEC
            if not (quiet or maxed):
                return []
            out = sorted(self._scopes)
            self._scopes.clear()
            self._first = self._last = None
            return out

    def _wait_time(self, now: float) -> float:
        if self._scopes and self._last is not None and self._first is not None:
            deadline = min(self._last + self._debounce, self._first + S.WATCHER_MAX_WAIT_SEC)
            return max(0.05, deadline - now)
        if self._next_rescan is not None:
            return min(5.0, max(0.5, self._next_rescan - now))
        return 5.0

    def _requeue(self, scopes: list[str]) -> None:
        now = self._clock()
        with self._cv:
            if not self._scopes:
                self._first = now
            self._scopes.update(scopes)
            self._last = now

    def start(self) -> None:
        """啟動各目標目錄監聽執行緒與背景調度迴圈。"""
        if self.running:
            return
        self._stop.clear()
        now = self._clock()
        if S.WATCHER_FALLBACK_RESCAN_SEC > 0:
            self._next_rescan = now + S.WATCHER_FALLBACK_RESCAN_SEC
        for target in self._targets_fn():
            backend_cls = _WinBackend if RT.IS_WIN else _PollBackend
            backend = backend_cls(target, self.feed, self._stop)
            backend.start()
            self._backends.append(backend)
        self._thread = threading.Thread(target=self._loop, daemon=True, name="lgfw-watcher")
        self._thread.start()

    def stop(self) -> None:
        """安全中止所有後端執行緒與調度迴圈（可重複呼叫並重啟）。"""
        self._stop.set()
        for backend in self._backends:
            backend.cancel()
        with self._cv:
            self._cv.notify_all()
        for backend in self._backends:
            backend.join(timeout=2.0)
        if self._thread:
            self._thread.join(timeout=2.0)
        self._backends.clear()
        self._thread = None
        with self._cv:
            self._scopes.clear()
            self._first = self._last = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = self._clock()
            if self._next_rescan is not None and now >= self._next_rescan:
                self._next_rescan = now + S.WATCHER_FALLBACK_RESCAN_SEC
                with self._cv:
                    self._scopes.update(S.WATCHER_SCOPES)
                    self._first = self._last = now - self._debounce
            due = self.take_due(now)
            if due:
                try:
                    res = self._fire(due)
                except Exception:
                    res = False
                if res is None:
                    self._requeue(due)
                else:
                    self.fired += 1
                continue
            with self._cv:
                if self._stop.is_set():
                    break
                self._cv.wait(self._wait_time(self._clock()))
