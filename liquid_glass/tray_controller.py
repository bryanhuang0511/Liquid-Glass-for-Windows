"""liquid_glass/tray_controller.py -- 單例系統匣鎖定、退出停止訊號與應用程式生命週期管理。

Functionality:
1. 透過 Win32 Named Mutex 與 Local Event 管理背景系統匣單例與安全停止交接。
2. 實作 LiquidGlassApp：負責跨行程設定檔隔離讀寫、Desktop Watcher 啟停、PyWebView 設定視窗與系統匣選單。
3. 遵循生命週期契約（restore_live_on_exit / exit_when_window_closes），關閉視窗或退出時安全還原 Windhawk 即時引擎。
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from typing import Any

from liquid_glass import desktop_watcher as DW
from liquid_glass import runtime as RT
from liquid_glass import shortcut_manager as SM
from liquid_glass import specs as S
from liquid_glass import windhawk_bridge as WB

# Lines 23-105: Win32 系統匣單例鎖、命名停止事件與子行程派送
_TRAY_MUTEX_NAME = "Global\\LiquidGlassForWindows_Tray_Mutex"
_TRAY_MUTEX: Any = None
_TRAY_STOP_EVENT_NAME = "Local\\LiquidGlassForWindows_Tray_Stop"
_TRAY_STOP_EVENT: Any = None


def _make_same_user_sa() -> tuple[Any, Any]:
    """建立同登入使用者（含標準與管理員權限）跨 Integrity Level 共享的 Security Attributes。"""
    if not RT.IS_WIN:
        return None, None
    try:
        from ctypes import wintypes

        TOKEN_QUERY = 0x0008
        TokenUser = 1
        k32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.LocalFree.argtypes = [ctypes.c_void_p]
        k32.LocalFree.restype = ctypes.c_void_p
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_uint32,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
        ]
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

        class _SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wintypes.BOOL),
            ]

        h_tok = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(h_tok)):
            return None, None
        try:
            needed = wintypes.DWORD(0)
            advapi32.GetTokenInformation(h_tok, TokenUser, None, 0, ctypes.byref(needed))
            if not needed.value:
                return None, None
            buf = (ctypes.c_ubyte * needed.value)()
            if not advapi32.GetTokenInformation(h_tok, TokenUser, buf, needed, ctypes.byref(needed)):
                return None, None
            sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            str_ptr = ctypes.c_void_p()
            if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(str_ptr)):
                return None, None
            try:
                user_sid = ctypes.wstring_at(str_ptr.value)
            finally:
                k32.LocalFree(str_ptr)
        finally:
            RT.kernel32.CloseHandle(h_tok)

        if not user_sid:
            return None, None
        sddl = f"D:(A;;GA;;;{user_sid})(A;;GA;;;BA)S:(ML;;NW;;;LW)"
        sd_ptr = ctypes.c_void_p()
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd_ptr), None):
            return None, None
        sa = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), sd_ptr.value, False)
        return sa, sd_ptr
    except Exception:
        return None, None


def _init_tray_stop_event() -> Any:
    """於系統匣行程初始化可跨行程觸發的命名停止事件。"""
    global _TRAY_STOP_EVENT
    if not RT.IS_WIN or RT.kernel32 is None:
        return None
    sa, sd_ptr = _make_same_user_sa()
    try:
        sa_ref = ctypes.byref(sa) if sa is not None else None
        handle = RT.kernel32.CreateEventW(sa_ref, True, False, _TRAY_STOP_EVENT_NAME)
        if handle:
            RT.kernel32.ResetEvent(handle)
            _TRAY_STOP_EVENT = handle
        return handle
    except Exception:
        return None
    finally:
        if sd_ptr:
            try:
                ctypes.windll.kernel32.LocalFree(sd_ptr)
            except Exception:
                pass


def _signal_tray_stop() -> bool:
    """觸發停止事件，通知背景系統匣結束常駐。"""
    if not RT.IS_WIN or RT.kernel32 is None:
        return False
    try:
        handle = RT.kernel32.OpenEventW(0x0002, False, _TRAY_STOP_EVENT_NAME)  # EVENT_MODIFY_STATE
        if handle:
            try:
                RT.kernel32.SetEvent(handle)
                return True
            finally:
                RT.kernel32.CloseHandle(handle)
    except Exception:
        pass
    return False


def _wait_tray_stop(timeout_sec: float) -> bool:
    """等待停止事件或逾時；收到停止訊號時回傳 True。"""
    if RT.IS_WIN and RT.kernel32 is not None and _TRAY_STOP_EVENT:
        try:
            ms = max(100, int(timeout_sec * 1000))
            return RT.kernel32.WaitForSingleObject(_TRAY_STOP_EVENT, ms) == 0
        except Exception:
            pass
    time.sleep(timeout_sec)
    return False


def is_tray_running() -> bool:
    """查詢背景系統匣是否正在執行（含跨權限鎖偵測）。"""
    if not RT.IS_WIN or RT.kernel32 is None:
        return False
    try:
        handle = RT.kernel32.OpenMutexW(0x00100000, False, _TRAY_MUTEX_NAME)  # SYNCHRONIZE
        if handle:
            RT.kernel32.CloseHandle(handle)
            return True
        if ctypes.get_last_error() == 5:  # ERROR_ACCESS_DENIED：不同權限之系統匣互斥鎖已存在
            return True
    except Exception:
        pass
    return False


def acquire_tray_mutex() -> bool:
    """嘗試取得系統匣單例鎖，已存在實例時回傳 False。"""
    global _TRAY_MUTEX
    if not RT.IS_WIN or RT.kernel32 is None:
        return True
    sa, sd_ptr = _make_same_user_sa()
    try:
        sa_ref = ctypes.byref(sa) if sa is not None else None
        handle = RT.kernel32.CreateMutexW(sa_ref, False, _TRAY_MUTEX_NAME)
        err = ctypes.get_last_error()
        if not handle:
            return False if err == 5 else True
        if err == 183:  # ERROR_ALREADY_EXISTS
            RT.kernel32.CloseHandle(handle)
            return False
        _TRAY_MUTEX = handle
        _init_tray_stop_event()
        return True
    except Exception:
        return True
    finally:
        if sd_ptr:
            try:
                ctypes.windll.kernel32.LocalFree(sd_ptr)
            except Exception:
                pass


def _spawn(flag: str, extra_flags: int = 0) -> Any:
    try:
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, flag]
        else:
            cmd = [sys.executable, os.path.join(RT.PROJECT_ROOT, "main.py"), flag]
        return subprocess.Popen(cmd, creationflags=extra_flags)
    except Exception:
        return None


def _log_exc(where: str) -> None:
    """於無主控台執行時期記錄未預期例外至暫存日誌，避免靜默失敗。"""
    try:
        path = os.path.join(tempfile.gettempdir(), "lgfw_app.log")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {where}\n{traceback.format_exc()}\n")
    except Exception:
        pass


def _focus_existing_settings_window() -> bool:
    """若控制中心視窗已開啟，將其還原並帶至前景，避免重複開啟多視窗（跨權限封鎖時退回新開）。"""
    if not RT.IS_WIN or RT.user32 is None:
        return False
    found: list[int] = []
    try:
        from ctypes import wintypes

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, ctypes.c_void_p)
        RT.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        RT.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        RT.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        RT.user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        RT.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        RT.user32.SetForegroundWindow.argtypes = [wintypes.HWND]

        def _cb(hwnd: int, _lp: int) -> int:
            if not RT.user32.IsWindowVisible(hwnd):
                return 1
            length = RT.user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return 1
            buf = ctypes.create_unicode_buffer(length + 1)
            RT.user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value.startswith(RT.APP_TITLE):
                found.append(hwnd)
                return 0
            return 1

        RT.user32.EnumWindows(WNDENUMPROC(_cb), 0)
        if found:
            hwnd = found[0]
            if not RT.user32.PostMessageW(hwnd, 0, 0, 0):  # UIPI 阻擋跨權限操作（如一般權限無法喚醒管理員視窗）
                return False
            RT.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            RT.user32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        _log_exc("_focus_existing_settings_window")
    return False


def spawn_background_tray() -> Any:
    """於背景無主控台啟動系統匣常駐行程。"""
    return _spawn("--autostart", 0x08000000)


def spawn_settings_ui() -> None:
    """啟動或喚起圖形設定視窗行程。"""
    if not _focus_existing_settings_window():
        _spawn("--settings")


def _webview_storage_path() -> str:
    """一般權限與提權模式分開使用獨立 WebView2 Profile 目錄。"""
    return (RT.PROFILE_DIR + "_admin") if RT.is_admin() else RT.PROFILE_DIR


# Lines 107-360: LiquidGlassApp 應用程式核心控制器
class LiquidGlassApp:
    """管理設定持久化、監控排程、PyWebView 設定視窗與系統匣生命週期。"""

    def __init__(self) -> None:
        self.config: dict[str, Any] = S.default_config()
        self.window: Any = None
        self.tray: Any = None
        self.watcher: Any = None
        self.initial_scope: str = ""
        self._relaunching_admin: bool = False
        self._tray_proc: Any = None
        self.load_config()

    def _ensure_tray_spawned(self) -> None:
        """確保背景系統匣常駐已啟動（標準與系統管理員模式皆一體適用）。"""
        if is_tray_running():
            return
        proc = self._tray_proc
        if proc is not None and getattr(proc, "poll", lambda: 0)() is None:
            return
        self._tray_proc = spawn_background_tray()

    def get_lifecycle(self) -> dict[str, bool]:
        """取得正規化後的退出還原與關閉視窗策略設定。"""
        return S.clamp_lifecycle(self.config.get("lifecycle"))

    @staticmethod
    def _read_raw_config() -> dict[str, Any] | None:
        try:
            with open(RT.CONFIG_PATH, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
            return raw if isinstance(raw, dict) else None
        except Exception:
            return None

    def load_config(self) -> None:
        """載入設定檔並自動完成舊版格式升級。"""
        raw = self._read_raw_config() if os.path.exists(RT.CONFIG_PATH) else None
        self.config = S.migrate_config(raw)
        if isinstance(raw, dict) and raw.get("version") != S.VERSION:
            self.save_config(watcher_changed=True, lifecycle_changed=True)

    def save_config(self, watcher_changed: bool = False, lifecycle_changed: bool = False) -> None:
        """雙行程（設定視窗／系統匣）共用 config.json 時保留未修改區塊，避免相互覆寫。"""
        try:
            self.config["version"] = S.VERSION
            if not (watcher_changed and lifecycle_changed):
                disk = self._read_raw_config()
                if disk is not None:
                    if not watcher_changed and "watcher" in disk:
                        self.config["watcher"] = S.clamp_watcher(disk["watcher"])
                    if not lifecycle_changed and "lifecycle" in disk:
                        self.config["lifecycle"] = S.clamp_lifecycle(disk["lifecycle"])
            RT.atomic_write_json(RT.CONFIG_PATH, self.config)
        except Exception:
            pass

    def _on_watch_fire(self, scopes: list[str]) -> list[dict[str, Any]] | None:
        active = [sc for sc in scopes if not S.is_scope_locked(sc)]
        if not active:
            return []
        res = SM.run_batch("apply", active, self.config)
        if res is not None and any(r["applied"] for r in res):
            try:
                if self.tray is not None:
                    self.tray.notify(SM.summarize(res, "apply"), RT.APP_TITLE)
            except Exception:
                pass
        return res

    def apply_watcher_config(self) -> None:
        """依據目前 config 啟動、重設或停止背景桌面監控。"""
        wc = S.clamp_watcher(self.config.get("watcher"))
        has_active = any(not S.is_scope_locked(sc) for sc in S.WATCHER_SCOPES)
        if wc["enabled"] and has_active:
            if self.watcher is None:
                self.watcher = DW.IconWatcher(self._on_watch_fire, wc["debounce_sec"])
            self.watcher.configure(wc["debounce_sec"])
            if not self.watcher.running:
                self.watcher.start()
        elif self.watcher is not None:
            self.watcher.stop()
            self.watcher = None

    def _config_poll_loop(self) -> None:
        """系統匣輪詢 config.json 與停止事件；設定變動時同步調整 Watcher。"""
        last = RT._mtime_ns(RT.CONFIG_PATH)
        while True:
            if _wait_tray_stop(1.5):
                self.quit_app(restore=False)
                return
            cur = RT._mtime_ns(RT.CONFIG_PATH)
            if cur != last:
                last = cur
                try:
                    self.load_config()
                    self.apply_watcher_config()
                except Exception:
                    pass

    def _toggle_watcher(self, *_args: Any) -> None:
        wc = S.clamp_watcher(self.config.get("watcher"))
        locked = all(S.is_scope_locked(sc) for sc in S.WATCHER_SCOPES)
        wc["enabled"] = False if locked else (not wc["enabled"])
        self.config["watcher"] = wc
        self.save_config(watcher_changed=True)
        self.apply_watcher_config()

    def _watcher_enabled(self) -> bool:
        return bool(S.clamp_watcher(self.config.get("watcher"))["enabled"])

    def open_settings_window(self, initial_scope: str = "") -> None:
        """開啟 PyWebView 控制中心視窗並於背景預熱預覽圖。"""
        from liquid_glass.web_api import LiquidGlassApi
        import webview

        self.initial_scope = initial_scope if initial_scope in S.ACTIVE_SCOPES else ""
        api = LiquidGlassApi(self)
        threading.Thread(target=self._warm_previews, args=(api,), daemon=True).start()

        kw = dict(
            title=f"{RT.APP_TITLE} v{S.VERSION}",
            url=RT.UI_HTML_PATH,
            width=780,
            height=900,
            min_size=(640, 640),
            resizable=True,
            js_api=api,
            background_color="#080b1a",
        )
        try:
            self.window = webview.create_window(hidden=True, **kw)
        except TypeError:
            self.window = webview.create_window(**kw)
        shown = threading.Event()
        show_timer: list[threading.Timer | None] = [None]

        def show(*_args: Any) -> None:
            if not shown.is_set():
                shown.set()
                if show_timer[0] is not None:
                    try:
                        show_timer[0].cancel()
                    except Exception:
                        pass
                try:
                    self.window.show()
                except Exception:
                    pass
                if (not self.get_lifecycle()["exit_when_window_closes"]) and (not is_tray_running()):
                    self._ensure_tray_spawned()

        try:
            self.window.events.loaded += show
        except Exception:
            pass
        show_timer[0] = threading.Timer(5.0, show)
        show_timer[0].daemon = True
        show_timer[0].start()

        start_kw = dict(gui="edgechromium", private_mode=False, storage_path=_webview_storage_path())
        try:
            try:
                webview.start(**start_kw)
            except TypeError:
                webview.start(gui="edgechromium")
        except Exception:
            _log_exc("open_settings_window")
        finally:
            self.handle_window_closed()
        # 確保 WebView2 / pythonnet 背景執行緒不會把已關閉的視窗卡在無頭殭屍行程狀態。
        if not os.environ.get("PYTEST_CURRENT_TEST"):
            os._exit(0)

    def handle_window_closed(self) -> None:
        """視窗關閉收尾：管理員提權重啟交接期間不動作，否則依生命週期縮小常駐通知區或還原引擎。"""
        if self._relaunching_admin:
            return
        self.load_config()
        lc = self.get_lifecycle()
        if lc["exit_when_window_closes"]:
            _signal_tray_stop()
            if lc["restore_live_on_exit"]:
                SM.restore_live_scopes_on_exit()
            return
        proc = getattr(self, "_tray_proc", None)
        if proc is not None and not is_tray_running():
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline and not is_tray_running():
                if getattr(proc, "poll", lambda: 0)() is not None:
                    break
                time.sleep(0.05)
        if lc["restore_live_on_exit"] and not is_tray_running():
            SM.restore_live_scopes_on_exit()

    def _warm_previews(self, api: Any) -> None:
        """在背景提前熱載入 NumPy / PIL 並快取全部四區預設預覽圖。"""
        try:
            dpr = 1.0
            if RT.IS_WIN and RT.user32 is not None:
                try:
                    dpr = max(1.0, RT.user32.GetDpiForSystem() / 96.0)
                except Exception:
                    pass
            for sc in S.ACTIVE_SCOPES:
                if not S.SPECS[sc].get("experimental"):
                    api._render_preview(sc, S.clamp_params(sc, self.config[sc]), min(3.0, dpr))
        except Exception:
            pass

    def run_tray_blocking(self) -> None:
        """啟動系統匣常駐圖示並監控設定與停止訊號。"""
        import pystray
        from PIL import Image

        img = None
        for path in (RT.resolve_app_png_path(), RT.resolve_app_icon_path()):
            if os.path.exists(path):
                try:
                    img = Image.open(path).convert("RGBA").resize((64, 64))
                    break
                except Exception:
                    pass
        if img is None:
            from liquid_glass import renderer as R

            img = R.tray_fallback_icon(64)

        def bg(kind: str, scopes: list[str]) -> Any:
            def run() -> None:
                res = SM.run_batch(kind, scopes, self.config)
                try:
                    if self.tray is not None:
                        self.tray.notify(SM.summarize(res, kind), RT.APP_TITLE)
                except Exception:
                    pass

            return lambda: threading.Thread(target=run, daemon=True).start()

        wh_ok = WB.is_windhawk_installed()

        def scope_items(kind: str) -> list[Any]:
            items = []
            for sc in S.SCOPES:
                locked = S.is_scope_locked(sc)
                need_wh = bool(S.SPECS[sc].get("requires_windhawk")) and not wh_ok
                suffix = "（🔒 已封鎖）" if locked else ("（🔌 需安裝 Windhawk）" if need_wh else "")
                title = f"{S.SPECS[sc]['icon']} {S.SPECS[sc]['label']}{suffix}"
                items.append(pystray.MenuItem(title, bg(kind, [sc]), enabled=(not locked and not need_wh)))
            return items

        all_avail = SM.effective_active_scopes() or ["desktop"]
        quick_items = [
            pystray.MenuItem(
                "✨ 一鍵套用全部四區" if wh_ok else "🖥️ 一鍵套用可用區域（桌面）",
                bg("apply", all_avail),
            ),
            pystray.MenuItem(
                "🛡️ 一鍵還原全部區域" if wh_ok else "🛡️ 還原桌面原生圖示",
                bg("restore", all_avail),
            ),
        ]

        exit_items = []
        if wh_ok:
            exit_items.append(pystray.MenuItem("🛡️ 退出並還原即時效果", lambda *_: self.quit_app(restore=True)))
        exit_items.append(pystray.MenuItem("❌ 退出程式", lambda *_: self.quit_app()))

        menu = pystray.Menu(
            pystray.MenuItem("🪟 開啟控制中心", lambda: spawn_settings_ui(), default=True),
            pystray.Menu.SEPARATOR,
            *quick_items,
            pystray.MenuItem("區域套用", pystray.Menu(*scope_items("apply"))),
            pystray.MenuItem("區域還原", pystray.Menu(*scope_items("restore"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("📁 開啟母本備份資料夾（唯讀）", lambda: self._open(RT.BACKUP_DIR)),
            *exit_items,
        )
        if not _TRAY_STOP_EVENT:
            _init_tray_stop_event()
        self.tray = pystray.Icon(RT.APP_NAME, img, f"{RT.APP_TITLE}（點擊開啟控制中心）", menu)
        self.apply_watcher_config()
        threading.Thread(target=self._config_poll_loop, daemon=True, name="lgfw-config-poll").start()
        self.tray.run()

    @staticmethod
    def _open(path: str) -> None:
        try:
            os.startfile(path)
        except Exception:
            pass

    def quit_app(self, *_args: Any, restore: bool | None = None) -> None:
        """退出應用程式並視設定自動停用 Windhawk 即時引擎（不動桌面捷徑）。"""
        try:
            self.load_config()
            should_restore = bool(self.get_lifecycle()["restore_live_on_exit"]) if restore is None else bool(restore)
            if should_restore:
                SM.restore_live_scopes_on_exit()
        except Exception:
            pass
        try:
            if self.watcher:
                self.watcher.stop()
            if self.tray:
                self.tray.stop()
        finally:
            os._exit(0)
