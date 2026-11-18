"""liquid_glass/runtime.py -- 路徑解析、Win32 API 宣告、單例設定讀寫與系統整合。

Functionality:
1. 動態解析原始碼執行與 PyInstaller 打包目錄（同層資源熱覆寫優先），支援 LGFW_BASE_DIR / LGIC_BASE_DIR 測試覆寫。
2. 統一管理 Win32 ctypes（DPI 感知、ICON/BITMAP、Shell 通知、提權重啟）。
3. 提供原子寫入 JSON、開機自動啟動（HKCU Run）與本工具桌面捷徑建立／移除。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import subprocess
import sys
import uuid
from typing import Any

from liquid_glass import APP_NAME, APP_TITLE, EXE_NAME, SELF_SHORTCUT_NAME

# Lines 21-108: 作業系統識別與 Win32 ctypes 結構體／函式簽章
IS_WIN = os.name == "nt"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

if IS_WIN:
    import winreg

    # 設定 WebView2 透明預設底色與抗降速旗標，並啟用 Per-monitor DPI 感知
    os.environ["WEBVIEW2_DEFAULT_BACKGROUND_COLOR"] = "00000000"
    _prev_args = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "").strip()
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        f"{_prev_args} --disable-backgrounding-occluded-windows --disable-renderer-backgrounding"
    ).strip()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class ICONINFO(ctypes.Structure):
        _fields_ = [
            ("fIcon", wintypes.BOOL),
            ("xHotspot", wintypes.DWORD),
            ("yHotspot", wintypes.DWORD),
            ("hbmMask", wintypes.HBITMAP),
            ("hbmColor", wintypes.HBITMAP),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

        @classmethod
        def from_str(cls, text: str) -> "_GUID":
            parsed = uuid.UUID(text)
            return cls(parsed.time_low, parsed.time_mid, parsed.time_hi_version, (ctypes.c_ubyte * 8)(*parsed.bytes[8:]))

    class _SIZE(ctypes.Structure):
        _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]

    user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]
    user32.GetIconInfo.restype = wintypes.BOOL
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.DestroyIcon.restype = wintypes.BOOL
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = wintypes.INT
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = wintypes.INT
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
    kernel32.ResetEvent.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.ReadDirectoryChangesW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel32.ReadDirectoryChangesW.restype = wintypes.BOOL
    kernel32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.CancelIoEx.restype = wintypes.BOOL
else:  # 非 Windows 環境（跨平台開發／單元測試匯入用）
    user32 = gdi32 = shell32 = kernel32 = None
    winreg = None
    ICONINFO = BITMAPINFOHEADER = _GUID = _SIZE = None  # type: ignore[assignment]


# Lines 110-165: 動態目錄解析與資源定位
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(PACKAGE_DIR)

if getattr(sys, "frozen", False):
    BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BUNDLE_DIR = PROJECT_ROOT
    BASE_DIR = PROJECT_ROOT

BASE_DIR = os.environ.get("LGFW_BASE_DIR") or os.environ.get("LGIC_BASE_DIR") or BASE_DIR

ICON_VAULT_DIR = os.path.join(BASE_DIR, "IconVault")
BACKUP_DIR = os.path.join(BASE_DIR, "Original_Shortcuts_Backup")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CACHE_PATH = os.path.join(ICON_VAULT_DIR, "_render_cache.json")
ADMIN_RESULT_PATH = os.path.join(BASE_DIR, "admin_last_result.json")
PROFILE_DIR = os.path.join(BASE_DIR, ".webview_profile")


def resource_path(*parts: str) -> str:
    """同層外掛目錄優先（便於熱調整 UI），否則退回 PyInstaller 打包目錄。"""
    for root in (BASE_DIR, BUNDLE_DIR):
        candidate = os.path.join(root, *parts)
        if os.path.exists(candidate):
            return candidate
    return os.path.join(BUNDLE_DIR, *parts)


def resolve_app_icon_path() -> str:
    """依序尋找新版 assets/app.ico 與舊版備援圖示路徑。"""
    for rel in (("assets", "app.ico"), ("LiquidGlassIconCenter.ico",)):
        path = resource_path(*rel)
        if os.path.exists(path):
            return path
    return resource_path("assets", "app.ico")


def resolve_app_png_path() -> str:
    """依序尋找新版 assets/app.png 與舊版備援 PNG 路徑。"""
    for rel in (("assets", "app.png"), ("LiquidGlassIconCenter.png",)):
        path = resource_path(*rel)
        if os.path.exists(path):
            return path
    return resource_path("assets", "app.png")


APP_ICO_PATH = resolve_app_icon_path()
UI_HTML_PATH = resource_path("ui", "index.html")


def ensure_dirs() -> None:
    """確保圖示快取庫與母本備份資料夾已建立。"""
    os.makedirs(ICON_VAULT_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)


def atomic_write_json(path: str, obj: Any) -> None:
    """先寫入 .tmp 暫存再原子替換，避免設定或快取截斷損毀。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _norm(path: str | None) -> str:
    return os.path.normcase(os.path.normpath(path)) if path else ""


def _mtime_ns(path: str) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


# Lines 167-275: Shell 圖示刷新、系統權限、開機自啟與本工具桌面捷徑
_PENDING_SHELL_PATHS: list[str] = []


def _queue_shell_refresh(path: str) -> None:
    if path:
        _PENDING_SHELL_PATHS.append(path)


def notify_shell() -> None:
    """每次批次作業只呼叫一次：逐檔通知 Explorer 更新，再全域重刷圖示快取。"""
    paths = list(_PENDING_SHELL_PATHS)
    _PENDING_SHELL_PATHS.clear()
    if not IS_WIN:
        return
    for path in paths:
        try:
            shell32.SHChangeNotify(0x00002000, 0x0005, str(path), None)  # SHCNE_UPDATEITEM | SHCNF_PATHW
        except Exception:
            pass
    try:
        shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
    except Exception:
        pass
    try:
        subprocess.run(
            ["ie4uinit.exe", "-show"],
            check=False,
            timeout=15,
            creationflags=0x08000000,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
    try:
        subprocess.run(
            ["taskkill", "/f", "/im", "StartMenuExperienceHost.exe"],
            check=False,
            timeout=5,
            creationflags=0x08000000,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def is_admin() -> bool:
    """回傳目前行程是否具備系統管理員權限。"""
    try:
        return bool(IS_WIN and shell32.IsUserAnAdmin())
    except Exception:
        return False


def _launch_command(flag: str = "") -> tuple[str, str]:
    for exe_name in (EXE_NAME, "LiquidGlassIconCenter.exe"):
        exe = os.path.join(BASE_DIR, exe_name)
        if os.path.exists(exe):
            return exe, flag
    if getattr(sys, "frozen", False):
        return sys.executable, flag
    entry = os.path.join(PROJECT_ROOT, "main.py")
    return sys.executable, f'"{entry}" {flag}'.strip()


def relaunch_as_admin(args: list[str], show: int = 0) -> bool:
    """以 runas 提權重啟本程式並帶入參數，回傳 UAC 請求是否被使用者允許。"""
    if not IS_WIN:
        return False
    try:
        exe, params = _launch_command(" ".join(args))
        fn = shell32.ShellExecuteW
        fn.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
        fn.restype = ctypes.c_void_p
        ret = fn(None, "runas", exe, params, BASE_DIR, int(show))
        return int(ret or 0) > 32
    except Exception:
        return False


def is_autostart_enabled() -> bool:
    """查詢 HKCU Run 是否已註冊開機啟動。"""
    if not IS_WIN or winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as key:
            for reg_name in (APP_NAME, "LiquidGlassIconCenter"):
                try:
                    if winreg.QueryValueEx(key, reg_name)[0]:
                        return True
                except OSError:
                    continue
    except Exception:
        return False
    return False


def set_autostart(enable: bool) -> bool:
    """啟用或移除登入 Windows 自動於背景啟動系統匣。"""
    if not IS_WIN or winreg is None:
        return False
    try:
        if enable:
            exe, args = _launch_command("--autostart")
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe}" {args}'.strip())
                try:
                    winreg.DeleteValue(key, "LiquidGlassIconCenter")
                except FileNotFoundError:
                    pass
        else:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                for reg_name in (APP_NAME, "LiquidGlassIconCenter"):
                    try:
                        winreg.DeleteValue(key, reg_name)
                    except FileNotFoundError:
                        pass
        return True
    except Exception:
        return False


def desktop_dir() -> str:
    """取得使用者桌面路徑（支援 OneDrive 重新導向資料夾）。"""
    if IS_WIN:
        try:
            buf = ctypes.create_unicode_buffer(260)
            if shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0 and buf.value:
                return buf.value
        except Exception:
            pass
    return os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")


def public_desktop_dir() -> str:
    """取得公用桌面路徑（環境變數缺失時退回 C:\\Users\\Public\\Desktop）。"""
    return os.path.join(os.environ.get("PUBLIC") or r"C:\Users\Public", "Desktop")


def get_desktop_shortcut_path() -> str:
    """回傳本工具建立於桌面的捷徑完整路徑。"""
    return os.path.join(desktop_dir(), SELF_SHORTCUT_NAME)


def desktop_shortcut_exists() -> bool:
    """檢查桌面上是否已有本工具捷徑。"""
    return os.path.exists(get_desktop_shortcut_path())


def create_desktop_shortcut() -> bool:
    """於個人桌面建立指向本程式的捷徑（若不存在 WSH 則回傳 False）。"""
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        wsh = win32com.client.Dispatch("WScript.Shell")
        sc = wsh.CreateShortcut(get_desktop_shortcut_path())
        exe, args = _launch_command("")
        sc.TargetPath = exe
        sc.Arguments = args
        sc.WorkingDirectory = BASE_DIR
        sc.Description = f"{APP_TITLE}（桌面獨立核心＋Windhawk 選用擴充）"
        ico = resolve_app_icon_path()
        if os.path.exists(ico):
            sc.IconLocation = f"{ico},0"
        sc.Save()
        return desktop_shortcut_exists()
    except Exception:
        return False


def remove_desktop_shortcut() -> bool:
    """刪除個人桌面上的本工具捷徑。"""
    try:
        path = get_desktop_shortcut_path()
        if os.path.exists(path):
            os.remove(path)
        return not os.path.exists(path)
    except Exception:
        return False
