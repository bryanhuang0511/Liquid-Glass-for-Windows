"""Liquid Glass 圖示中樞 v1.3.1

三區獨立風格（桌面＝風格 A、開始功能表＝風格 B、工作列＝極細線條），
各自可「只套用 / 只還原」，另有一鍵全部。

v1.3.1（步驟二）新增：
  1. 桌面雙來源合併列舉（個人桌面 + 公用桌面），Item 帶 source / ext。
  2. 支援 .url（網際網路捷徑 / Steam 遊戲）：逐行文字讀寫 IconFile / IconIndex，沿用原編碼。
  3. Watcher 檔案監控：背景執行緒 + 去抖 + 防自我迴圈 + 增量套用（寄居於系統匣行程）。
  4. 母本過期自動更新：現場捷徑是乾淨原始圖示、且來源特徵與母本不同時，以現場最新版覆寫母本。
  5. 權限判定與 needs_admin 統計；--apply-admin / --restore-admin <scope> 以 runas 提權重試。
  6. MSIX / AUMID 取圖：TargetPath 與 IconLocation 皆空的捷徑改走 Shell Item 取 256px。

行程分工（啟動快的關鍵）：
  系統匣行程  只載入 pystray + PIL；不載入 numpy、不載入 webview（Watcher 也在這裡）
  設定視窗    只載入 webview；影像運算在背景執行緒預熱，與 WebView2 開機並行
重型模組一律延遲載入。
"""
import codecs
import ctypes
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

import glass_specs as S  # 純 Python，毫秒級
import windhawk_bridge as WB  # Windhawk 工作列即時引擎橋接（純 Python）

IS_WIN = os.name == "nt"
APP_NAME = "LiquidGlassIconCenter"
APP_TITLE = "Liquid Glass 圖示中樞"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

if IS_WIN:
    import winreg

    # 讓 WebView2 預設背景透明、避免背景視窗被降速；並設定 Per-monitor DPI
    os.environ["WEBVIEW2_DEFAULT_BACKGROUND_COLOR"] = "00000000"
    _prev = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "").strip()
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        _prev + " --disable-backgrounding-occluded-windows --disable-renderer-backgrounding").strip()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                    ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                    ("hbmColor", wintypes.HBITMAP)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        @classmethod
        def from_str(cls, text):
            u = uuid.UUID(text)
            return cls(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))

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
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
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
    # 檔案監控用
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.ReadDirectoryChangesW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.BOOL,
                                               wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                               ctypes.c_void_p, ctypes.c_void_p]
    kernel32.ReadDirectoryChangesW.restype = wintypes.BOOL
    kernel32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.CancelIoEx.restype = wintypes.BOOL
else:  # 非 Windows（僅供開發 / 測試匯入）
    user32 = gdi32 = shell32 = kernel32 = None
    winreg = None

# ==================== 路徑與設定 ====================
if getattr(sys, "frozen", False):
    BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    BASE_DIR = BUNDLE_DIR
BASE_DIR = os.environ.get("LGIC_BASE_DIR") or BASE_DIR  # 測試用覆寫

ICON_VAULT_DIR = os.path.join(BASE_DIR, "IconVault")
BACKUP_DIR = os.path.join(BASE_DIR, "Original_Shortcuts_Backup")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CACHE_PATH = os.path.join(ICON_VAULT_DIR, "_render_cache.json")
ADMIN_RESULT_PATH = os.path.join(BASE_DIR, "admin_last_result.json")
PROFILE_DIR = os.path.join(BASE_DIR, ".webview_profile")
SELF_SHORTCUT_NAME = "Liquid Glass 圖示中樞.lnk"


def resource_path(*parts):
    """同層優先（免重新打包即可熱更新 UI），否則取打包內存。"""
    for root in (BASE_DIR, BUNDLE_DIR):
        p = os.path.join(root, *parts)
        if os.path.exists(p):
            return p
    return os.path.join(BUNDLE_DIR, *parts)


APP_ICO_PATH = resource_path("LiquidGlassIconCenter.ico")
UI_HTML_PATH = resource_path("ui", "index.html")


def ensure_dirs():
    os.makedirs(ICON_VAULT_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)


def atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ==================== 處理程序與 Single-Instance 鎖（ctypes，不載入 pywin32） ====================
_TRAY_MUTEX_NAME = "Global\\LiquidGlassIconCenter_Tray_Mutex"
_TRAY_MUTEX = None
_TRAY_STOP_EVENT_NAME = "Local\\LiquidGlassIconCenter_Tray_Stop"
_TRAY_STOP_EVENT = None


def _init_tray_stop_event():
    global _TRAY_STOP_EVENT
    if not IS_WIN or kernel32 is None:
        return None
    try:
        h = kernel32.CreateEventW(None, True, False, _TRAY_STOP_EVENT_NAME)
        if h:
            kernel32.ResetEvent(h)
            _TRAY_STOP_EVENT = h
        return h
    except Exception:
        return None


def _signal_tray_stop():
    if not IS_WIN or kernel32 is None:
        return False
    try:
        h = kernel32.OpenEventW(0x0002, False, _TRAY_STOP_EVENT_NAME)  # EVENT_MODIFY_STATE
        if h:
            try:
                kernel32.SetEvent(h)
                return True
            finally:
                kernel32.CloseHandle(h)
    except Exception:
        pass
    return False


def _wait_tray_stop(timeout_sec):
    if IS_WIN and kernel32 is not None and _TRAY_STOP_EVENT:
        try:
            ms = max(100, int(timeout_sec * 1000))
            return kernel32.WaitForSingleObject(_TRAY_STOP_EVENT, ms) == 0
        except Exception:
            pass
    time.sleep(timeout_sec)
    return False


def is_tray_running():
    if not IS_WIN:
        return False
    try:
        h = kernel32.OpenMutexW(0x00100000, False, _TRAY_MUTEX_NAME)  # SYNCHRONIZE
        if h:
            kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def acquire_tray_mutex():
    global _TRAY_MUTEX
    if not IS_WIN:
        return True
    try:
        h = kernel32.CreateMutexW(None, False, _TRAY_MUTEX_NAME)
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            return False
        _TRAY_MUTEX = h
        return True
    except Exception:
        return True


def _spawn(flag, extra_flags=0):
    try:
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, flag]
        else:
            cmd = [sys.executable, os.path.abspath(__file__), flag]
        subprocess.Popen(cmd, creationflags=extra_flags)
    except Exception:
        pass


def spawn_background_tray():
    _spawn("--autostart", 0x08000000)  # CREATE_NO_WINDOW


def spawn_settings_ui():
    _spawn("--settings")


# ==================== 捷徑：COM 執行緒初始化 ====================
_tls = threading.local()


def _ensure_com():
    """每個執行緒只初始化一次 COM。"""
    if getattr(_tls, "com", False):
        return
    import pythoncom
    pythoncom.CoInitialize()
    _tls.com = True


def _wsh():
    w = getattr(_tls, "wsh", None)
    if w is None:
        _ensure_com()
        import win32com.client
        w = win32com.client.Dispatch("WScript.Shell")
        _tls.wsh = w
    return w


# 捷徑資訊：target=目標路徑、args=參數（.url 則放網址）、icon_loc / icon_idx=圖示位置
ShortcutInfo = namedtuple("ShortcutInfo", "target args icon_loc icon_idx ok")
_EMPTY_INFO = ShortcutInfo("", "", "", 0, False)
# .url 的讀取結果：欄位是 ShortcutInfo 的超集（可直接當 ShortcutInfo 使用），多帶原始編碼與換行
UrlInfo = namedtuple("UrlInfo", "target args icon_loc icon_idx ok encoding newline")
_EMPTY_URL_INFO = UrlInfo("", "", "", 0, False, "utf-8", "\r\n")


def read_shortcut(lnk_path):
    try:
        sc = _wsh().CreateShortcut(lnk_path)
        raw = sc.IconLocation or ""
        loc, sep, idx = raw.rpartition(",")
        try:
            icon_idx = int(idx)
        except ValueError:
            loc, icon_idx = raw, 0
        if not sep:
            loc, icon_idx = raw, 0
        loc = os.path.expandvars(loc).strip().strip('"')
        target = os.path.expandvars(sc.TargetPath).strip() if sc.TargetPath else ""
        return ShortcutInfo(target, sc.Arguments or "", loc, icon_idx, True)
    except Exception:
        return _EMPTY_INFO


_DENIED_CODES = {5, -2147024891, 0x80070005}  # ERROR_ACCESS_DENIED / E_ACCESSDENIED


def _is_denied(exc, path):
    """判斷例外是否為『權限不足』：PermissionError、winerror 5、COM 錯誤（含 excepinfo 內的 scode）、目錄不可寫。"""
    if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 5:
        return True
    args = getattr(exc, "args", ()) or ()
    codes = {a for a in args if isinstance(a, int)}
    if len(args) > 2 and isinstance(args[2], tuple) and len(args[2]) >= 6 and isinstance(args[2][5], int):
        codes.add(args[2][5])  # com_error 的 excepinfo[5] 才是真正的 scode
    if codes & _DENIED_CODES:
        return True
    return not os.access(os.path.dirname(path) or ".", os.W_OK)


def _write_icon_shelllink(lnk_path, ico_path):
    """以 IShellLink / IPersistFile 直接改圖示：載入後只動 IconLocation，保留 IDList（MSIX / AUMID 捷徑必要）。"""
    _ensure_com()
    import pythoncom
    from win32com.shell import shell
    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None,
                                      pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    pf = link.QueryInterface(pythoncom.IID_IPersistFile)
    pf.Load(lnk_path, 0)
    link.SetIconLocation(ico_path, 0)
    pf.Save(lnk_path, 0)


def write_shortcut_icon(lnk_path, ico_path):
    """回傳 'ok' / 'denied' / 'fail'。權限不足要明確回報，不能默默失敗。"""
    try:
        sc = _wsh().CreateShortcut(lnk_path)
        if not (sc.TargetPath or "").strip():
            # 沒有 TargetPath（商店 / MSIX 捷徑）：WScript.Shell 存檔可能丟掉 IDList，改走 IShellLink
            _write_icon_shelllink(lnk_path, ico_path)
            return "ok"
        sc.IconLocation = f"{ico_path},0"
        sc.Save()
        return "ok"
    except Exception as e:
        return "denied" if _is_denied(e, lnk_path) else "fail"


# ==================== .url（網際網路捷徑，INI 文字檔） ====================
_SECTION_RE = re.compile(r"^\[(.+?)\]$")


def decode_url_bytes(raw):
    """依 S.URL_ENCODINGS 順序嘗試解碼；回傳 (文字, 所用編碼)。寫回時必須沿用這個編碼。

    - 沒有 BOM 的檔案不可判為 utf-8-sig（否則寫回會憑空多出 BOM）。
    - 全部失敗時退到 latin-1：它對任何位元組都能無損往返，寧可亂碼也不損毀檔案。
    """
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        return raw.decode("utf-16"), "utf-16"
    for enc in S.URL_ENCODINGS:
        if enc == "utf-8-sig" and not raw.startswith(codecs.BOM_UTF8):
            continue
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"


def parse_url_text(text):
    """只看 [InternetShortcut] 區段，回傳 {'url','icon_file','icon_index'}（缺少者為 None）。"""
    out = {"url": None, "icon_file": None, "icon_index": None}
    want = S.URL_SECTION.lower()
    sect = None
    for line in text.split("\n"):
        s = line.strip()
        m = _SECTION_RE.match(s)
        if m:
            sect = m.group(1).strip().lower()
            continue
        if sect != want or not s or s[0] in ";#" or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip().lower()
        if k == "url":
            out["url"] = v.strip()
        elif k == S.URL_ICON_KEYS[0].lower():
            out["icon_file"] = v.strip()
        elif k == S.URL_ICON_KEYS[1].lower():
            out["icon_index"] = v.strip()
    return out


def rewrite_url_text(text, icon_file, icon_index=0):
    """逐行文字替換 IconFile= / IconIndex=（絕不使用 configparser 全檔重寫）。

    其他行（註解、[{000214A0-...}] 區段、Prop3、IDList、空白行、換行風格）一字不動；
    鍵不存在時插在 [InternetShortcut] 區段最後一個有內容的行之後；整段區段不存在則附加在檔尾。
    """
    lines = text.split("\n")           # 保留每行尾端的 "\r"，才能無損還原
    crlf = "\r\n" in text
    eol = "\r" if crlf else ""
    values = {S.URL_ICON_KEYS[0].lower(): (S.URL_ICON_KEYS[0], str(icon_file)),
              S.URL_ICON_KEYS[1].lower(): (S.URL_ICON_KEYS[1], str(int(icon_index)))}
    want = S.URL_SECTION.lower()
    sect = None
    start = end = None      # [InternetShortcut] 區段的標題行與結束位置（不含）
    seen = set()
    for i, line in enumerate(lines):
        s = line.strip()
        m = _SECTION_RE.match(s)
        if m:
            if sect == want and end is None:
                end = i
            sect = m.group(1).strip().lower()
            if sect == want and start is None:
                start = i
            continue
        if sect != want or not s or s[0] in ";#" or "=" not in s:
            continue
        key = s.partition("=")[0].strip().lower()
        if key in values:
            keep_cr = "\r" if line.endswith("\r") else ""
            lines[i] = line[:line.index("=") + 1] + values[key][1] + keep_cr
            seen.add(key)
    if start is not None and end is None:
        end = len(lines)
    missing = [values[k] for k in values if k not in seen]
    if missing:
        new_lines = [f"{k}={v}{eol}" for k, v in missing]
        if start is None:                                   # 沒有 [InternetShortcut] 區段
            block = [f"[{S.URL_SECTION}]{eol}"] + new_lines
            if lines and lines[-1] == "":                    # 檔尾已有換行：插在最後的空元素之前
                lines[-1:-1] = block
            else:
                lines.extend(block)
        else:
            last = start
            for j in range(start + 1, end):
                if lines[j].strip():
                    last = j
            if lines[last] and not lines[last].endswith("\r") and crlf:
                lines[last] += "\r"                          # 末行原本缺 CR：補上以維持一致的換行
            lines[last + 1:last + 1] = new_lines
    return "\n".join(lines)


def _parse_url_wide_icon(raw_bytes):
    """從 Windows COM 寫入的 [InternetShortcut.W]（UTF-7）取出無損 Unicode IconFile/IconIndex。"""
    try:
        text = raw_bytes.decode("latin-1")
    except Exception:
        return None, None
    sect = None
    w_loc, w_idx = None, None
    for line in text.split("\n"):
        s = line.strip()
        m = _SECTION_RE.match(s)
        if m:
            sect = m.group(1).strip().lower()
            continue
        if sect != "internetshortcut.w" or not s or s[0] in ";#" or "=" not in s:
            continue
        k, _, v = s.partition("=")
        k = k.strip().lower()
        if k == S.URL_ICON_KEYS[0].lower():
            raw_v = v.strip().strip('"')
            if raw_v:
                try:
                    w_loc = raw_v.encode("ascii").decode("utf-7")
                except Exception:
                    pass
        elif k == S.URL_ICON_KEYS[1].lower():
            try:
                w_idx = int(v.strip())
            except (TypeError, ValueError):
                pass
    return w_loc, w_idx


def _url_has_wide_section(path):
    try:
        with open(path, "rb") as f:
            return b"[InternetShortcut.W]" in f.read()
    except Exception:
        return False


def _write_url_icon_com(path, ico_path, icon_index=0):
    """使用 Windows CLSID_InternetShortcut 寫入 [InternetShortcut.A] + [InternetShortcut.W]（UTF-7）。"""
    _ensure_com()
    import pythoncom
    from win32com.shell import shell
    isu = pythoncom.CoCreateInstance(
        "{FBF23B40-E3F0-101B-8488-00AA003E56F8}", None,
        pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLinkW)
    pf = isu.QueryInterface(pythoncom.IID_IPersistFile)
    pf.Load(path, 0)
    isu.SetIconLocation(ico_path, int(icon_index))
    pf.Save(path, 0)


def read_url_shortcut(path):
    """讀 .url：解析 IconFile / IconIndex（優先讀取 [InternetShortcut.W]），保留原檔編碼與換行。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
        text, enc = decode_url_bytes(raw)
        d = parse_url_text(text)
        w_loc, w_idx = _parse_url_wide_icon(raw)
        loc = os.path.expandvars(w_loc if w_loc else (d["icon_file"] or "")).strip().strip('"')
        if w_idx is not None:
            idx = w_idx
        else:
            try:
                idx = int(d["icon_index"])
            except (TypeError, ValueError):
                idx = 0
        return UrlInfo("", d["url"] or "", loc, idx, True, enc, "\r\n" if "\r\n" in text else "\n")
    except Exception:
        return _EMPTY_URL_INFO


def write_url_shortcut_icon(path, ico_path, icon_index=0):
    """回傳 'ok' / 'denied' / 'fail'。含非 ASCII 路徑時在 Windows 上以 COM 補齊 [InternetShortcut.W]。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
        text, enc = decode_url_bytes(raw)
        need_wide = bool(IS_WIN and not str(ico_path).isascii())
        wide_missing = need_wide and not _url_has_wide_section(path)
        new_text = rewrite_url_text(text, ico_path, icon_index)
        if new_text == text and not wide_missing:
            return "ok"
        try:
            data = new_text.encode(enc)
        except UnicodeEncodeError:
            return "fail"
        if new_text != text:
            with open(path, "wb") as f:
                f.write(data)
        if need_wide:
            try:
                _write_url_icon_com(path, ico_path, icon_index)
            except Exception as e:
                return "denied" if _is_denied(e, path) else "fail"
        return "ok"
    except Exception as e:
        return "denied" if _is_denied(e, path) else "fail"


def _is_url(path):
    return str(path).lower().endswith(".url")


def read_any_shortcut(path):
    """依副檔名分派（呼叫時才查找模組層的函式，方便測試替換）。"""
    return read_url_shortcut(path) if _is_url(path) else read_shortcut(path)


# ----- 防自我迴圈：記錄本工具最後一次寫入捷徑的時間 -----
_SELF_WRITE = {"t": None}


def mark_self_write(t=None):
    _SELF_WRITE["t"] = time.monotonic() if t is None else t


def write_any_shortcut(path, ico_path):
    r = write_url_shortcut_icon(path, ico_path) if _is_url(path) else write_shortcut_icon(path, ico_path)
    mark_self_write()  # 不論成敗都標記：失敗也可能留下事件
    return r


def _norm(p):
    return os.path.normcase(os.path.normpath(p)) if p else ""


def is_ours(icon_loc):
    """圖示是否指向本工具產生的檔案（不依賴安裝路徑，搬資料夾也辨識得出來）。"""
    if not icon_loc:
        return False
    n = _norm(icon_loc)
    return (n.startswith(_norm(ICON_VAULT_DIR) + os.sep)
            or (os.sep + "iconvault" + os.sep) in n
            or n.endswith("_glass.ico"))


# ==================== 開機自啟與桌面捷徑 ====================
def is_autostart_enabled():
    if not IS_WIN:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as k:
            return bool(winreg.QueryValueEx(k, APP_NAME)[0])
    except Exception:
        return False


def _launch_command(flag=""):
    exe = os.path.join(BASE_DIR, "LiquidGlassIconCenter.exe")
    if os.path.exists(exe):
        return exe, flag
    if getattr(sys, "frozen", False):
        return sys.executable, flag
    return sys.executable, f'"{os.path.abspath(__file__)}" {flag}'.strip()


def set_autostart(enable):
    if not IS_WIN:
        return False
    try:
        if enable:
            exe, args = _launch_command("--autostart")
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, f'"{exe}" {args}'.strip())
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass
        return True
    except Exception:
        return False


def desktop_dir():
    """使用者桌面（含被 OneDrive 重新導向的情況）。"""
    if IS_WIN:
        try:
            buf = ctypes.create_unicode_buffer(260)
            if shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0 and buf.value:
                return buf.value
        except Exception:
            pass
    return os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")


def public_desktop_dir():
    """公用桌面：%PUBLIC%\\Desktop（環境變數缺失時退回 C:\\Users\\Public\\Desktop）。"""
    return os.path.join(os.environ.get("PUBLIC") or r"C:\Users\Public", "Desktop")


def get_desktop_shortcut_path():
    return os.path.join(desktop_dir(), SELF_SHORTCUT_NAME)


def desktop_shortcut_exists():
    return os.path.exists(get_desktop_shortcut_path())


def create_desktop_shortcut():
    try:
        sc = _wsh().CreateShortcut(get_desktop_shortcut_path())
        exe, args = _launch_command("")
        sc.TargetPath = exe
        sc.Arguments = args
        sc.WorkingDirectory = BASE_DIR
        sc.Description = "Liquid Glass 圖示中樞（桌面／開始功能表／工作列三區獨立風格）"
        if os.path.exists(APP_ICO_PATH):
            sc.IconLocation = f"{APP_ICO_PATH},0"
        sc.Save()
        return desktop_shortcut_exists()
    except Exception:
        return False


def remove_desktop_shortcut():
    try:
        p = get_desktop_shortcut_path()
        if os.path.exists(p):
            os.remove(p)
        return not os.path.exists(p)
    except Exception:
        return False


# ==================== Win32 乾淨圖示提取 ====================
def hicon_to_pil(hicon):
    import numpy as np
    from PIL import Image
    if not hicon:
        return None
    info = ICONINFO()
    if not user32.GetIconInfo(hicon, ctypes.byref(info)):
        return None
    hdc = None
    try:
        hbm = info.hbmColor if info.hbmColor else info.hbmMask
        if not hbm:
            return None
        hdc = user32.GetDC(0)
        bih = BITMAPINFOHEADER()
        bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        gdi32.GetDIBits(hdc, hbm, 0, 0, None, ctypes.byref(bih), 0)
        w, h = bih.biWidth, abs(bih.biHeight)
        if not info.hbmColor:
            h //= 2
        if w <= 0 or h <= 0:
            return None
        buf = (ctypes.c_byte * (w * h * 4))()
        bih.biHeight = -h
        bih.biBitCount = 32
        bih.biCompression = 0
        gdi32.GetDIBits(hdc, hbm, 0, h, ctypes.byref(buf), ctypes.byref(bih), 0)
    finally:  # 例外路徑也要釋放 GDI 資源
        if hdc:
            user32.ReleaseDC(0, hdc)
        if info.hbmColor:
            gdi32.DeleteObject(info.hbmColor)
        if info.hbmMask:
            gdi32.DeleteObject(info.hbmMask)

    arr = np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4))
    b, g, r, a = arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3]
    if np.max(a) == 0:
        a = np.where((r == 0) & (g == 0) & (b == 0), 0, 255).astype(np.uint8)
    return Image.fromarray(np.stack([r, g, b, a], axis=-1), mode="RGBA")


def hbitmap_to_pil(hbm):
    """HBITMAP（IShellItemImageFactory 給的是 32bpp 預乘 Alpha DIB）→ 一般 RGBA。呼叫端負責 DeleteObject。"""
    import numpy as np
    from PIL import Image
    hdc = user32.GetDC(0)
    try:
        bih = BITMAPINFOHEADER()
        bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        gdi32.GetDIBits(hdc, hbm, 0, 0, None, ctypes.byref(bih), 0)
        w, h = bih.biWidth, abs(bih.biHeight)
        if w <= 0 or h <= 0:
            return None
        buf = (ctypes.c_byte * (w * h * 4))()
        bih.biHeight = -h
        bih.biPlanes = 1
        bih.biBitCount = 32
        bih.biCompression = 0
        gdi32.GetDIBits(hdc, hbm, 0, h, ctypes.byref(buf), ctypes.byref(bih), 0)
    finally:
        if hdc:
            user32.ReleaseDC(0, hdc)
    arr = np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4)).astype(np.float32)
    b, g, r, a = arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3]
    if float(a.max()) == 0.0:                       # 完全沒有 Alpha 通道：視為不透明
        a = np.full_like(a, 255.0)
    else:                                           # 解預乘
        scale = np.where(a > 0, 255.0 / np.maximum(a, 1.0), 0.0)
        r, g, b = np.clip(r * scale, 0, 255), np.clip(g * scale, 0, 255), np.clip(b * scale, 0, 255)
    out = np.stack([r, g, b, a], axis=-1).astype(np.uint8)
    return Image.fromarray(out, mode="RGBA")


# ----- MSIX / AUMID -----
def aumid_from_parsing_name(name):
    """從 Shell 解析名稱抽出 AUMID（'Pkg_xxx!App'）；抽不到回傳空字串。"""
    if not name:
        return ""
    name = str(name).strip()
    low = name.lower()
    marker = "appsfolder\\"
    if marker in low:
        return name[low.index(marker) + len(marker):].strip()
    tail = name.rsplit("\\", 1)[-1]
    return tail.strip() if "!" in tail else ""


def resolve_aumid(lnk_path):
    """取得捷徑的 AUMID：先讀 System.AppUserModel.ID 屬性，沒有再從 IDList 的 Shell 解析名稱推得。"""
    if not IS_WIN:
        return ""
    try:
        _ensure_com()
        import pythoncom
        from win32com.shell import shell, shellcon
        link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None,
                                          pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
        link.QueryInterface(pythoncom.IID_IPersistFile).Load(lnk_path, 0)
    except Exception:
        return ""
    try:
        from win32com.propsys import propsys, pscon
        store = link.QueryInterface(propsys.IID_IPropertyStore)
        v = store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue()
        if v:
            return str(v).strip()
    except Exception:
        pass
    try:
        pidl = link.GetIDList()
        if pidl:
            try:
                name = shell.SHGetNameFromIDList(pidl, shellcon.SIGDN_DESKTOPABSOLUTEPARSING)
            except Exception:
                item = shell.SHCreateItemFromIDList(pidl, shell.IID_IShellItem)
                name = item.GetDisplayName(shellcon.SIGDN_DESKTOPABSOLUTEPARSING)
            return aumid_from_parsing_name(name)
    except Exception:
        pass
    return ""


def _shell_item_image(parsing_name, size=256):
    """SHCreateItemFromParsingName → IShellItemImageFactory::GetImage → PIL RGBA；失敗回傳 None。"""
    if not IS_WIN or not parsing_name:
        return None
    from PIL import Image
    shell32.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                                    ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
    iid = _GUID.from_str("BCC18B79-BA16-442F-80C4-8A59C30C463B")  # IID_IShellItemImageFactory
    pv = ctypes.c_void_p()
    if shell32.SHCreateItemFromParsingName(parsing_name, None, ctypes.byref(iid), ctypes.byref(pv)) != 0 \
            or not pv.value:
        return None
    vtbl = ctypes.cast(ctypes.cast(pv, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    try:
        get_image = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, _SIZE, wintypes.UINT,
                                       ctypes.POINTER(wintypes.HBITMAP))(vtbl[3])
        hbm = wintypes.HBITMAP()
        # SIIGBF_BIGGERSIZEOK(0x1) | SIIGBF_ICONONLY(0x4)：只要圖示、不要縮圖，允許較大尺寸
        if get_image(pv, _SIZE(size, size), 0x5, ctypes.byref(hbm)) != 0 or not hbm.value:
            return None
        try:
            im = hbitmap_to_pil(hbm.value)
        finally:
            gdi32.DeleteObject(hbm.value)
        if im is None:
            return None
        return im.resize((256, 256), Image.Resampling.LANCZOS)
    except Exception:
        return None
    finally:
        try:
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(pv)  # IUnknown::Release
        except Exception:
            pass


def extract_icon_from_shell_item(lnk_path):
    """TargetPath 與 IconLocation 皆空的捷徑（Affinity 等商店應用）：用 shell:AppsFolder\\<AUMID> 取 256px 圖示。"""
    if not IS_WIN:
        return None
    try:
        _ensure_com()
    except Exception:
        return None
    candidates = []
    aumid = resolve_aumid(lnk_path)
    if aumid:
        candidates.append(S.aumid_shell_path(aumid))
    candidates.append(lnk_path)  # 退而求其次：讓 Shell 直接解析這個捷徑本身
    for name in candidates:
        im = _shell_item_image(name, 256)
        if im is not None:
            return im
    return None


def extract_icon_256(path, info=None):
    """取得 256×256 RGBA 圖示。捷徑若已指向本工具的圖示則忽略，改取目標程式本身的圖示。

    .lnk：IconLocation → TargetPath → （兩者皆無）AUMID / Shell Item。
    .url：記錄的 IconFile（.ico 直接讀；.exe 等依 IconIndex 取）。
    """
    from PIL import Image
    if not path or not os.path.exists(path):
        return None
    icon_idx = 0
    try:
        low = path.lower()
        if low.endswith(".url"):
            info = info or read_url_shortcut(path)
            loc = info.icon_loc if (info.icon_loc and not is_ours(info.icon_loc)) else ""
            if not loc or not os.path.exists(loc):
                return None
            if loc.lower().endswith(".ico"):
                try:
                    return Image.open(loc).convert("RGBA").resize((256, 256), Image.Resampling.LANCZOS)
                except Exception:
                    pass
            path, icon_idx = loc, info.icon_idx
        elif low.endswith(".lnk"):
            lnk_path = path
            info = info or read_shortcut(path)
            loc = info.icon_loc if (info.icon_loc and not is_ours(info.icon_loc)) else ""
            if loc and os.path.exists(loc):
                if loc.lower().endswith(".ico"):
                    try:
                        return Image.open(loc).convert("RGBA").resize((256, 256), Image.Resampling.LANCZOS)
                    except Exception:
                        pass
                path, icon_idx = loc, info.icon_idx
            elif info.target and os.path.exists(info.target):
                path = info.target
            else:  # 沒有可用的圖示來源：多半是商店 / MSIX 應用，走 AUMID
                im = extract_icon_from_shell_item(lnk_path)
                if im is not None:
                    return im
        if path.lower().endswith(".ico"):
            try:
                return Image.open(path).convert("RGBA").resize((256, 256), Image.Resampling.LANCZOS)
            except Exception:
                pass
        if not IS_WIN:
            return None
        hicon = ctypes.c_void_p()
        hr = shell32.SHDefExtractIconW(path, icon_idx, 0, ctypes.byref(hicon), None, 256)
        if hr == 0 and hicon.value:
            im = hicon_to_pil(hicon.value)
            user32.DestroyIcon(hicon.value)
            if im and (im.width >= 48 or im.height >= 48):
                return im.resize((256, 256), Image.Resampling.LANCZOS)
    except Exception:
        pass
    try:
        if IS_WIN:
            large = (wintypes.HICON * 1)()
            if shell32.ExtractIconExW(path, icon_idx, large, None, 1) > 0 and large[0]:
                im = hicon_to_pil(large[0])
                user32.DestroyIcon(large[0])
                if im:
                    return im.resize((256, 256), Image.Resampling.LANCZOS)
    except Exception:
        pass
    return None


# ==================== 三區範圍、母本備份、渲染快取 ====================
def _safe_name(s):
    return "".join(c if c.isalnum() or c in (" ", "_", "-") else "_" for c in s).strip().replace(" ", "_")


def taskbar_dir():
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Internet Explorer",
                        "Quick Launch", "User Pinned", "TaskBar")


def start_menu_roots():
    return [
        (os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"), "user"),
        (os.path.join(os.environ.get("ProgramData", ""), "Microsoft", "Windows", "Start Menu", "Programs"), "common"),
    ]


_SKIP_WORDS = ("uninstall", "解除安裝", "卸載", "卸载")


@dataclass
class Item:
    scope: str
    path: str                 # 捷徑本身
    primary: str              # 此區專屬母本位置
    legacy: Optional[str]     # v1.2.0 平面母本位置（相容用）
    ico_path: str
    source: str = "user"      # 'user'（個人）/ 'common'（公用）
    ext: str = ".lnk"         # '.lnk' / '.url'

    @property
    def ico_name(self):
        return os.path.basename(self.ico_path)


def scope_roots(scope):
    """該範圍實際存在的掃描根目錄：[(目錄, 來源標籤, 是否遞迴)]。列舉與 Watcher 共用，兩邊永遠一致。"""
    if scope == "desktop":
        cand = [(desktop_dir(), S.SOURCE_USER, False), (public_desktop_dir(), S.SOURCE_COMMON, False)]
    elif scope == "taskbar":
        cand = [(taskbar_dir(), S.SOURCE_USER, False)]
    elif scope == "startmenu":
        cand = [(p, tag, True) for p, tag in start_menu_roots()]
    else:
        return []
    allowed = S.SCOPE_SOURCES.get(scope, ())
    out, seen = [], set()
    for d, source, rec in cand:
        if source not in allowed or not d or not os.path.isdir(d) or _norm(d) in seen:
            continue
        seen.add(_norm(d))
        out.append((d, source, rec))
    return out


def enumerate_scope(scope):
    items = []
    suffix = S.SPECS[scope]["ico_suffix"]
    for root, source, _rec in scope_roots(scope):
        parts = S.backup_parts(scope, source)
        if scope in ("desktop", "taskbar"):
            for f in sorted(os.listdir(root)):
                ext = S.shortcut_ext(scope, f)
                if not ext or (scope == "desktop" and f == SELF_SHORTCUT_NAME):
                    continue
                stem = f[:-len(ext)]
                items.append(Item(
                    scope, os.path.join(root, f),
                    os.path.join(BACKUP_DIR, *parts, f),
                    os.path.join(BACKUP_DIR, f) if scope == "taskbar" else None,
                    os.path.join(ICON_VAULT_DIR, _safe_name(stem) + S.vault_tag(scope, source, ext) + suffix),
                    source, ext))
        else:  # startmenu：遞迴，母本保留相對路徑，ICO 檔名加路徑雜湊避免同名衝突
            for dp, _dn, fns in os.walk(root):
                for f in sorted(fns):
                    ext = S.shortcut_ext(scope, f)
                    if not ext or any(w in f.lower() for w in _SKIP_WORDS):
                        continue
                    full = os.path.join(dp, f)
                    rel = os.path.relpath(full, root)
                    h = hashlib.sha1((source + "|" + rel.lower()).encode("utf-8")).hexdigest()[:6]
                    items.append(Item(
                        scope, full,
                        os.path.join(BACKUP_DIR, *parts, rel),
                        os.path.join(BACKUP_DIR, f),
                        os.path.join(ICON_VAULT_DIR,
                                     f"{_safe_name(f[:-len(ext)])}_{h}{S.vault_tag(scope, source, ext)}{suffix}"),
                        source, ext))
    return items


def resolve_backup(item, cur_ours):
    """回傳 (母本路徑或 None, 是否剛建立)。

    - 沒有母本且捷徑目前是乾淨的 → 自動複製一份乾淨母本。
    - 沒有母本、捷徑已被本工具改過 → 只能用 v1.2.0 的平面母本；若也沒有則回傳 None（不冒險處理）。
    - 已存在的母本在此函式內不會被覆寫；母本是否過期由 backup_is_stale / refresh_backup 另外處理。
    """
    if os.path.exists(item.primary):
        return item.primary, False
    if not cur_ours:
        try:
            os.makedirs(os.path.dirname(item.primary), exist_ok=True)
            shutil.copy2(item.path, item.primary)
            return item.primary, True
        except Exception:
            return None, False
    if item.legacy and os.path.exists(item.legacy):
        return item.legacy, False
    return None, False


def _mtime_ns(p):
    try:
        return os.stat(p).st_mtime_ns
    except OSError:
        return None


def backup_is_stale(live_path, live_info, backup_path, backup_info):
    """現場是乾淨原始捷徑時，母本是否已落後（軟體升級改寫了捷徑）？

    比對來源特徵：目標路徑、參數（.url 為網址）、圖示路徑與索引、mtime。任何一項不同即視為過期。
    母本由 copy2 建立，會保留 mtime，因此「沒被動過」的母本與現場 mtime 必相同。
    """
    if not getattr(live_info, "ok", False):
        return False  # 現場都讀不到，不冒險覆寫母本
    if not getattr(backup_info, "ok", False):
        return True   # 母本損毀 / 讀不到，而現場是好的
    if _norm(live_info.target) != _norm(backup_info.target):
        return True
    if _norm(live_info.icon_loc) != _norm(backup_info.icon_loc) or live_info.icon_idx != backup_info.icon_idx:
        return True
    if (live_info.args or "") != (backup_info.args or ""):
        return True
    ma, mb = _mtime_ns(live_path), _mtime_ns(backup_path)
    return ma is not None and mb is not None and ma != mb


def refresh_backup(item):
    """以現場最新捷徑覆寫母本（先寫暫存檔再替換，失敗時舊母本原封不動）。"""
    tmp = item.primary + ".tmp"
    try:
        os.makedirs(os.path.dirname(item.primary), exist_ok=True)
        shutil.copy2(item.path, tmp)
        os.replace(tmp, item.primary)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def _sig(p):
    try:
        st = os.stat(p)
        return f"{_norm(p)}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        return f"{_norm(p)}|-"


def source_signature(backup_path, sinfo):
    parts = [_sig(backup_path)]
    if sinfo.icon_loc and not is_ours(sinfo.icon_loc):
        parts += [_sig(sinfo.icon_loc), str(sinfo.icon_idx)]
    elif sinfo.target:
        parts.append(_sig(sinfo.target))
    return "||".join(parts)


def make_key(scope, params, sig):
    blob = json.dumps([S.RENDER_VERSION, scope, params, sig], sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:20]


def load_cache():
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_cache(cache):
    try:
        ensure_dirs()
        atomic_write_json(CACHE_PATH, cache)
    except Exception:
        pass


_PENDING_SHELL_PATHS = []


def _queue_shell_refresh(path):
    if path:
        _PENDING_SHELL_PATHS.append(path)


def notify_shell():
    """每次作業只呼叫一次；先逐檔通知 Explorer 重繪更新過的捷徑，再全域刷新圖示快取。"""
    paths = list(_PENDING_SHELL_PATHS)
    _PENDING_SHELL_PATHS.clear()
    if not IS_WIN:
        return
    for p in paths:
        try:
            shell32.SHChangeNotify(0x00002000, 0x0005, str(p), None)  # SHCNE_UPDATEITEM | SHCNF_PATHW
        except Exception:
            pass
    try:
        shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
    except Exception:
        pass
    try:
        subprocess.run(["ie4uinit.exe", "-show"], check=False, timeout=15,
                       creationflags=0x08000000, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    try:
        subprocess.run(["taskkill", "/f", "/im", "StartMenuExperienceHost.exe"], check=False, timeout=5,
                       creationflags=0x08000000, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ----- 進度（UI 輪詢） -----
PROGRESS = {"active": False, "scope": "", "phase": "", "done": 0, "total": 0}
_APPLY_LOCK = threading.Lock()


def _prog(**kw):
    PROGRESS.update(kw)


def _new_result(scope, total):
    return S.new_result(scope, total)  # 欄位表統一由 glass_specs.RESULT_FIELDS 定義（含 needs_admin）


def is_admin():
    try:
        return bool(IS_WIN and shell32.IsUserAnAdmin())
    except Exception:
        return False


def _needs_admin(item):
    """被拒絕時，這一項是否『換成管理員身分就能解決』：位於公用來源，且目前不是管理員。"""
    return bool(S.SOURCE_NEEDS_ADMIN.get(item.source, False)) and not is_admin()


def _only_sources(items, sources):
    if not sources:
        return items
    allowed = set(sources)
    return [i for i in items if i.source in allowed]


def _restore_legacy_windhawk_shortcuts(scope):
    """還原曾被舊版 v1.x 改寫過 IconLocation 的工作列或開始功能表 .lnk（不碰未改動或無母本項目）。"""
    try:
        items = enumerate_scope(scope)
    except Exception:
        return {"restored": 0, "untouched": 0}
    restored = untouched = 0
    for it in items:
        info = read_any_shortcut(it.path)
        if not is_ours(info.icon_loc):
            untouched += 1
            continue
        src = it.primary if os.path.exists(it.primary) else (
            it.legacy if it.legacy and os.path.exists(it.legacy) else None)
        if not src:
            continue
        try:
            shutil.copy2(src, it.path)
            restored += 1
            _queue_shell_refresh(it.path)
        except Exception:
            pass
    if restored:
        notify_shell()
    return {"restored": restored, "untouched": untouched}


def _restore_legacy_taskbar_shortcuts():
    return _restore_legacy_windhawk_shortcuts("taskbar")


def effective_active_scopes(wh_dir=None):
    """回傳本機目前可用的區域清單；未安裝 Windhawk 時自動僅保留獨立的桌面區。"""
    wh_ok = WB.is_windhawk_installed(wh_dir)
    return [sc for sc in S.ACTIVE_SCOPES if (not S.SPECS[sc].get("requires_windhawk") or wh_ok)]


def restore_live_scopes_on_exit(wh_dir=None):
    """結束程式時自動停用所有已啟用的 Windhawk 即時引擎（工作列／開始功能表），不動桌面實體捷徑。"""
    if not WB.is_windhawk_installed(wh_dir):
        return []
    out = []
    for sc in S.ACTIVE_SCOPES:
        if sc not in WB.MOD_SPECS or S.is_scope_locked(sc):
            continue
        try:
            st = WB.get_status(wh_dir=wh_dir, base_dir=BASE_DIR, scope=sc)
            if st.get("enabled") or (st.get("ini_exists") and str(st.get("mod", {}).get("Disabled", "")).strip() == "0"):
                out.append(WB.restore_windhawk_mod(sc, wh_dir=wh_dir))
            else:
                # 若 ini 仍為啟用狀態（無 telemetry 時），get_status().enabled 已為 True，這裡多一層保險檢查
                cfg_raw = WB.read_mod_config(WB.resolve_windhawk_paths(wh_dir, scope=sc), scope=sc)
                if cfg_raw.get("exists") and str(cfg_raw.get("mod", {}).get("Disabled", "1")).strip() == "0":
                    out.append(WB.restore_windhawk_mod(sc, wh_dir=wh_dir))
        except Exception:
            pass
    return out


def apply_scope(scope, params, notify=True, sources=None):
    """套用指定區域；工作列／開始功能表走 Windhawk 即時引擎，桌面走光學 ICO＋捷徑。"""
    if S.is_scope_locked(scope):
        res = _new_result(scope, 0)
        res["locked"] = True
        return res
    if scope in WB.MOD_SPECS:
        p = S.clamp_params(scope, params)
        _restore_legacy_windhawk_shortcuts(scope)
        wb_res = WB.apply_windhawk_mod(scope, p, base_dir=BASE_DIR)
        res = _new_result(scope, 1)
        if wb_res.get("ok"):
            res["applied"] = 1
        else:
            res["failed"] = 1
        if wb_res.get("needs_admin"):
            res["windhawk_needs_admin"] = 1
        if wb_res.get("windhawk_missing"):
            res["windhawk_missing"] = 1
        res["message"] = wb_res.get("message", "")
        res["windhawk"] = wb_res
        return res

    import glass_render as R
    ensure_dirs()
    params = S.clamp_params(scope, params)
    items = _only_sources(enumerate_scope(scope), sources)
    res = _new_result(scope, len(items))
    cache = load_cache()
    _prog(scope=scope, phase="分析", done=0, total=len(items))

    plan = []  # (item, key)
    jobs = []  # (item, img, key)
    for i, it in enumerate(items):
        _prog(done=i)
        info = read_any_shortcut(it.path)
        cur_ours = is_ours(info.icon_loc)
        backup, created = resolve_backup(it, cur_ours)
        if backup is None:
            res["no_backup"] += 1
            continue
        if created:
            res["backed_up"] += 1
        if created or backup == it.path:
            sinfo = info
        else:
            sinfo = read_any_shortcut(backup)
            # 母本過期自動更新：現場是乾淨原始捷徑，但來源特徵與母本不同（軟體升級改寫了它）
            if not cur_ours and backup == it.primary and backup_is_stale(it.path, info, backup, sinfo):
                if refresh_backup(it):
                    res["backed_up"] += 1
                    sinfo = info
        key = make_key(scope, params, source_signature(backup, sinfo))
        cached = cache.get(it.ico_name) == key and os.path.exists(it.ico_path)
        url_wide_ok = not (IS_WIN and it.ext == ".url" and not it.ico_path.isascii() and not _url_has_wide_section(it.path))
        points = cur_ours and _norm(info.icon_loc) == _norm(it.ico_path) and url_wide_ok
        if cached and points:
            res["unchanged"] += 1
            continue
        if cached:
            plan.append((it, key))
            continue
        _prog(phase="取圖")
        img = extract_icon_256(backup, sinfo)
        if img is None:
            res["no_icon"] += 1
            continue
        jobs.append((it, img, key))
        plan.append((it, key))

    # 平行渲染 + 寫 ICO
    rendered = {}
    if jobs:
        _prog(phase="渲染", done=0, total=len(jobs))
        workers = max(1, min(4, os.cpu_count() or 2))
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [(it, key, ex.submit(R.render_ico, scope, img, params, it.ico_path))
                    for it, img, key in jobs]
            for it, key, fut in futs:
                try:
                    fut.result()
                    rendered[it.ico_name] = key
                except Exception:
                    res["failed"] += 1
                done += 1
                _prog(done=done)
        jobs.clear()

    # 寫捷徑
    _prog(phase="寫入捷徑", done=0, total=len(plan))
    for i, (it, key) in enumerate(plan):
        _prog(done=i)
        if not os.path.exists(it.ico_path) or (it.ico_name not in rendered and cache.get(it.ico_name) != key):
            continue  # 渲染失敗，已計入 failed
        r = write_any_shortcut(it.path, it.ico_path)
        if r == "ok":
            res["applied"] += 1
            cache[it.ico_name] = key
            _queue_shell_refresh(it.path)
        elif r == "denied":
            res["denied"] += 1
            if _needs_admin(it):
                res["needs_admin"] += 1
            cache[it.ico_name] = key  # ICO 本身是好的，下次有權限時可直接寫入
        else:
            res["failed"] += 1
            cache[it.ico_name] = key
    save_cache(cache)
    _prog(done=len(plan), total=len(plan))
    mark_self_write()
    if notify and res["applied"]:
        notify_shell()
    return res


def restore_scope(scope, notify=True, sources=None):
    """還原指定區域；工作列／開始功能表停用 Windhawk 即時引擎＋清除歷史殘留，桌面還原母本。"""
    if S.is_scope_locked(scope):
        res = _new_result(scope, 0)
        res["locked"] = True
        return res
    if scope in WB.MOD_SPECS:
        wb_res = WB.restore_windhawk_mod(scope)
        legacy = _restore_legacy_windhawk_shortcuts(scope)
        res = _new_result(scope, 1)
        if wb_res.get("ok"):
            res["restored"] = 1 + legacy.get("restored", 0)
        else:
            res["failed"] = 1
        if wb_res.get("needs_admin"):
            res["windhawk_needs_admin"] = 1
        res["message"] = wb_res.get("message", "")
        res["windhawk"] = wb_res
        return res

    items = _only_sources(enumerate_scope(scope), sources)
    res = _new_result(scope, len(items))
    _prog(scope=scope, phase="還原", done=0, total=len(items))
    for i, it in enumerate(items):
        _prog(done=i)
        info = read_any_shortcut(it.path)
        if not is_ours(info.icon_loc):
            res["untouched"] += 1
            continue
        src = it.primary if os.path.exists(it.primary) else (
            it.legacy if it.legacy and os.path.exists(it.legacy) else None)
        if not src:
            res["no_backup"] += 1
            continue
        try:
            shutil.copy2(src, it.path)
            res["restored"] += 1
            _queue_shell_refresh(it.path)
        except PermissionError:
            res["denied"] += 1
            if _needs_admin(it):
                res["needs_admin"] += 1
        except Exception:
            res["failed"] += 1
        mark_self_write()
    if notify:
        notify_shell()
    return res


def run_batch(kind, scopes, config, sources=None):
    """kind='apply'|'restore'；整批共用一次系統通知。回傳各區結果 list，或 None（忙碌中）。"""
    if not _APPLY_LOCK.acquire(blocking=False):
        return None
    try:
        _prog(active=True)
        out = []
        for sc in scopes:
            if kind == "apply":
                out.append(apply_scope(sc, config.get(sc, {}), notify=False, sources=sources))
            else:
                out.append(restore_scope(sc, notify=False, sources=sources))
        if any(r["scope"] not in WB.MOD_SPECS and (r["applied"] or r["restored"]) for r in out):
            notify_shell()
        return out
    finally:
        _prog(active=False, phase="", done=0, total=0)
        _APPLY_LOCK.release()


def summarize(results, kind):
    if results is None:
        return "另一項作業進行中，請稍候"
    parts = []
    names = {r["scope"]: S.SPECS[r["scope"]]["label"] for r in results}
    for r in results:
        label = names[r["scope"]]
        if r.get("locked"):
            parts.append(f"{label}：已鎖定")
            continue
        if r.get("message") and r["scope"] in WB.MOD_SPECS:
            parts.append(r["message"])
            continue
        if kind == "apply":
            s = f"{label}：更新 {r['applied']}"
            if r["unchanged"]:
                s += f"、未變更略過 {r['unchanged']}"
        else:
            s = f"{label}：還原 {r['restored']}"
            if r["untouched"]:
                s += f"、本來就是原始 {r['untouched']}"
        for k, txt in (("denied", "權限不足"), ("no_backup", "無母本略過"),
                       ("no_icon", "取不到圖示"), ("failed", "失敗")):
            if r[k]:
                s += f"、{txt} {r[k]}"
        parts.append(s)
    msg = "；".join(parts)
    need = sum(r.get("needs_admin", 0) for r in results)
    if need:
        msg += f"（其中 {need} 項位於公用位置，需以系統管理員權限重試）"
    elif any(r["denied"] for r in results):
        msg += "（權限不足的項目請以系統管理員身分執行）"
    return msg


def count_backups():
    n = 0
    try:
        for _dp, _dn, fns in os.walk(BACKUP_DIR):
            n += sum(1 for f in fns if f.lower().endswith((".lnk", ".url")))
    except Exception:
        pass
    return n


# ==================== 提權重試（平常維持一般權限，被拒絕時才由使用者主動提權） ====================
def relaunch_as_admin(args, show=0):
    """以 runas 提權重啟本程式並帶入參數（會跳 UAC）。回傳『提權請求是否被接受』，不等待子行程結束。"""
    if not IS_WIN:
        return False
    try:
        exe, params = _launch_command(" ".join(args))
        fn = shell32.ShellExecuteW
        fn.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                       wintypes.LPCWSTR, ctypes.c_int]
        fn.restype = ctypes.c_void_p
        ret = fn(None, "runas", exe, params, BASE_DIR, int(show))  # SW_HIDE(0)/SW_SHOWNORMAL(1)；取消 UAC <= 32
        return int(ret or 0) > 32
    except Exception:
        return False


# ==================== Watcher：檔案監控 + 去抖 + 防自我迴圈 + 增量套用 ====================
WatchTarget = namedtuple("WatchTarget", "path scope recursive")

_NOTIFY_FILTER = 0x1 | 0x2 | 0x8 | 0x10 | 0x40  # 檔名 | 目錄名 | 大小 | 最後寫入 | 建立


def watch_targets():
    """要監看的目錄：個人桌面、公用桌面、工作列、開始功能表（個人 + 公用，遞迴）。"""
    out, seen = [], set()
    for sc in S.WATCHER_SCOPES:
        for d, _src, rec in scope_roots(sc):
            k = (sc, _norm(d))
            if k not in seen:
                seen.add(k)
                out.append(WatchTarget(d, sc, rec))
    return out


class _PollBackend(threading.Thread):
    """輪詢式監控（非 Windows 或 ReadDirectoryChangesW 開不起來時的保底）。"""

    def __init__(self, target, feed, stop_evt, interval=2.0):
        super().__init__(daemon=True, name="lgic-watch-poll")
        self.target, self._feed, self._stop_evt, self._interval = target, feed, stop_evt, interval

    def _snap(self):
        snap = {}
        try:
            if self.target.recursive:
                for dp, dn, fns in os.walk(self.target.path):
                    for n in dn + fns:
                        self._add(snap, os.path.join(dp, n))
            else:
                for e in os.scandir(self.target.path):
                    self._add(snap, e.path)
        except OSError:
            pass
        return snap

    @staticmethod
    def _add(snap, p):
        try:
            st = os.stat(p)
            snap[p] = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass

    def run(self):
        prev = self._snap()
        while not self._stop_evt.wait(self._interval):
            cur = self._snap()
            for p in set(prev) | set(cur):
                if prev.get(p) != cur.get(p):
                    self._feed(self.target.scope, p)
            prev = cur

    def cancel(self):
        pass


class _WinBackend(_PollBackend):
    """ReadDirectoryChangesW（同步阻塞，停止時以 CancelIoEx 喚醒）。開不起來就退回輪詢。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._h = None

    def _open(self):
        try:
            h = kernel32.CreateFileW(self.target.path, 0x0001, 0x1 | 0x2 | 0x4, None, 3, 0x02000000, None)
            if h is None or h == ctypes.c_void_p(-1).value:
                return None
            return h
        except Exception:
            return None

    def run(self):
        h = self._open()
        if not h:
            return super().run()
        self._h = h
        scope, base = self.target.scope, self.target.path
        buf = ctypes.create_string_buffer(65536)
        ret = wintypes.DWORD(0)
        try:
            while not self._stop_evt.is_set():
                ok = kernel32.ReadDirectoryChangesW(h, buf, len(buf), bool(self.target.recursive),
                                                    _NOTIFY_FILTER, ctypes.byref(ret), None, None)
                if self._stop_evt.is_set():
                    break
                if not ok:                       # 暫時性錯誤（目錄被移除等）：稍待再試
                    if self._stop_evt.wait(2.0):
                        break
                    continue
                if ret.value == 0:               # 緩衝區溢位：不知道哪個檔案變了，交給增量套用整區重掃
                    self._feed(scope, None)
                    continue
                raw, off = buf.raw[:ret.value], 0
                while True:
                    nxt, _act, ln = struct.unpack_from("<III", raw, off)
                    name = raw[off + 12: off + 12 + ln].decode("utf-16-le", "ignore")
                    self._feed(scope, os.path.join(base, name))
                    if not nxt:
                        break
                    off += nxt
        finally:
            self._h = None
            try:
                kernel32.CloseHandle(h)
            except Exception:
                pass

    def cancel(self):
        h = self._h
        if h:
            try:
                kernel32.CancelIoEx(h, None)
            except Exception:
                pass


class IconWatcher:
    """背景監聽桌面 / 公用桌面 / 工作列 / 開始功能表。

    * 去抖：事件之後要「安靜 debounce_sec 秒」才觸發一次；事件不斷時，最晚 WATCHER_MAX_WAIT_SEC 秒內也會觸發。
    * 防自我迴圈：本工具寫入捷徑後 WATCHER_SELF_IGNORE_SEC 秒內的事件一律忽略（見 mark_self_write）。
    * 保底：每 WATCHER_FALLBACK_RESCAN_SEC 秒整區重掃一次（監控事件可能遺漏）。
    * fire(scopes) 回傳 None 代表「另一項作業進行中」，會稍後重試；其他回傳值視為已處理。
    """

    def __init__(self, fire, debounce_sec=None, clock=time.monotonic, targets_fn=None):
        self._fire = fire
        self._clock = clock
        self._targets_fn = targets_fn or watch_targets
        self._debounce = S.clamp_watcher({"debounce_sec": debounce_sec})["debounce_sec"]
        self._cv = threading.Condition()
        self._scopes = set()
        self._first = None
        self._last = None
        self._next_rescan = None
        self._stop = threading.Event()
        self._thread = None
        self._backends = []
        self.ignored = 0   # 被防自我迴圈擋下的事件數（診斷用）
        self.fired = 0

    # ---- 設定 ----
    @property
    def debounce_sec(self):
        return self._debounce

    def configure(self, debounce_sec):
        with self._cv:
            self._debounce = S.clamp_watcher({"debounce_sec": debounce_sec})["debounce_sec"]
            self._cv.notify_all()

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    # ---- 事件輸入 ----
    @staticmethod
    def _relevant(scope, path):
        name = os.path.basename(str(path))
        if name == SELF_SHORTCUT_NAME or name.lower() == "desktop.ini":
            return False
        if S.shortcut_ext(scope, name):
            return True
        return os.path.splitext(name)[1] == ""   # 無副檔名多半是資料夾異動（開始功能表子資料夾）

    def feed(self, scope, path=None, now=None):
        """收到一個檔案系統事件。回傳是否被接受（False＝無關檔案或自我寫入）。"""
        if scope not in S.WATCHER_SCOPES:
            return False
        if path is not None and not self._relevant(scope, path):
            return False
        now = self._clock() if now is None else now
        last = _SELF_WRITE["t"]
        if last is not None and 0 <= now - last < S.WATCHER_SELF_IGNORE_SEC:
            self.ignored += 1
            return False
        with self._cv:
            if not self._scopes:
                self._first = now
            self._scopes.add(scope)
            self._last = now
            self._cv.notify_all()
        return True

    # ---- 去抖判定（純邏輯，可用注入的時間測試） ----
    def take_due(self, now=None):
        now = self._clock() if now is None else now
        with self._cv:
            if not self._scopes:
                return []
            quiet = now - self._last >= self._debounce
            maxed = now - self._first >= S.WATCHER_MAX_WAIT_SEC
            if not (quiet or maxed):
                return []
            out = sorted(self._scopes)
            self._scopes.clear()
            self._first = self._last = None
            return out

    def _wait_time(self, now):
        if self._scopes:
            deadline = min(self._last + self._debounce, self._first + S.WATCHER_MAX_WAIT_SEC)
            return max(0.05, deadline - now)
        if self._next_rescan is not None:
            return min(5.0, max(0.5, self._next_rescan - now))
        return 5.0

    def _requeue(self, scopes):
        now = self._clock()
        with self._cv:
            if not self._scopes:
                self._first = now
            self._scopes.update(scopes)
            self._last = now

    # ---- 執行緒 ----
    def start(self):
        if self.running:
            return
        self._stop.clear()
        now = self._clock()
        if S.WATCHER_FALLBACK_RESCAN_SEC > 0:
            self._next_rescan = now + S.WATCHER_FALLBACK_RESCAN_SEC
        for t in self._targets_fn():
            cls = _WinBackend if IS_WIN else _PollBackend
            b = cls(t, self.feed, self._stop)
            b.start()
            self._backends.append(b)
        self._thread = threading.Thread(target=self._loop, daemon=True, name="lgic-watcher")
        self._thread.start()

    def stop(self):
        self._stop.set()
        for b in self._backends:
            b.cancel()
        with self._cv:
            self._cv.notify_all()
        for b in self._backends:
            b.join(timeout=2.0)
        if self._thread:
            self._thread.join(timeout=2.0)
        self._backends.clear()
        self._thread = None
        with self._cv:
            self._scopes.clear()
            self._first = self._last = None

    def _loop(self):
        while not self._stop.is_set():
            now = self._clock()
            if self._next_rescan is not None and now >= self._next_rescan:
                self._next_rescan = now + S.WATCHER_FALLBACK_RESCAN_SEC
                with self._cv:                      # 保底整區重掃：立刻到期
                    self._scopes.update(S.WATCHER_SCOPES)
                    self._first = self._last = now - self._debounce
            due = self.take_due(now)
            if due:
                try:
                    res = self._fire(due)
                except Exception:
                    res = False
                if res is None:                     # 忙碌中：稍後重試
                    self._requeue(due)
                else:
                    self.fired += 1
                continue
            with self._cv:
                if self._stop.is_set():
                    break
                self._cv.wait(self._wait_time(self._clock()))


# ==================== JS-Python API ====================
class LiquidGlassApi:
    """注意：pywebview 會掃描 js_api 的「公開」成員建立橋接。
    內部狀態一律用底線開頭（尤其 _app 內含 window 物件，不能被掃描）。"""

    def __init__(self, app):
        self._app = app
        self._pcache = {}
        self._plock = threading.Lock()

    # ---- 狀態（純輕量：不做任何影像運算） ----
    def get_state(self):
        wc = self.get_watcher()
        tb_st = WB.get_status(base_dir=BASE_DIR, scope="taskbar")
        sm_st = WB.get_status(base_dir=BASE_DIR, scope="startmenu")
        wh_installed = bool(tb_st.get("windhawk_installed") or sm_st.get("windhawk_installed"))
        available = [sc for sc in S.ACTIVE_SCOPES if (not S.SPECS[sc].get("requires_windhawk") or wh_installed)]
        optional = [sc for sc in S.ACTIVE_SCOPES if S.SPECS[sc].get("requires_windhawk")]
        lc = self._app.get_lifecycle()
        return {
            "version": S.VERSION,
            "scopes": list(S.SCOPES),
            "active_scopes": list(S.ACTIVE_SCOPES),
            "available_scopes": available,
            "optional_scopes": optional,
            "locked_scopes": list(S.LOCKED_SCOPES),
            "default_scope": S.ACTIVE_SCOPES[0] if wh_installed else "desktop",
            "windhawk_installed": wh_installed,
            "windhawk_dir": tb_st.get("windhawk_dir") or "",
            "specs": S.SPECS,
            "config": {sc: self._app.config[sc] for sc in S.SCOPES},
            "autostart": is_autostart_enabled(),
            "shortcut": desktop_shortcut_exists(),
            "watcher": bool(wc["enabled"]),
            "watcher_locked": bool(wc.get("locked", False)),
            "watcher_config": wc,
            "lifecycle": lc,
            "restore_on_exit": bool(lc["restore_live_on_exit"]),
            "exit_on_close": bool(lc["exit_when_window_closes"]),
            "backup_count": count_backups(),
            "is_admin": is_admin(),
            "taskbar_engine": tb_st,
            "startmenu_engine": sm_st,
        }

    def get_taskbar_status(self):
        return WB.get_status(base_dir=BASE_DIR, scope="taskbar")

    def get_engine_status(self, scope="taskbar"):
        sc = scope if scope in WB.MOD_SPECS else "taskbar"
        return WB.get_status(base_dir=BASE_DIR, scope=sc)

    # ---- 預覽 ----
    def preview(self, scope, params, dpr=1.0, seq=0):
        if scope not in S.SCOPES:
            return {"seq": seq, "error": "bad scope"}
        if S.is_scope_locked(scope):
            return {"seq": seq, "locked": True, "error": "scope locked"}
        p = S.clamp_params(scope, params)
        try:
            dpr = min(3.0, max(1.0, float(dpr)))
        except (TypeError, ValueError):
            dpr = 1.0
        return dict(self._render_preview(scope, p, dpr), seq=seq)

    def _render_preview(self, scope, p, dpr):
        import base64
        import io
        import glass_render as R
        ck = (scope, json.dumps(p, sort_keys=True), round(dpr, 2))
        with self._plock:
            hit = self._pcache.get(ck)
        if hit:
            return hit
        big_css, act_css = 96, S.SPECS[scope]["actual_px"]

        def enc(im):
            buf = io.BytesIO()
            im.save(buf, format="PNG", compress_level=1)
            return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

        samples = R.sample_icons()
        out = {"scope": scope, "big_css": big_css, "actual_css": act_css,
               "big": [enc(R.render_preview(scope, im, p, int(round(big_css * dpr)))) for im in samples],
               "actual": [enc(R.render_preview(scope, im, p, int(round(act_css * dpr)))) for im in samples]}
        with self._plock:
            if len(self._pcache) > 24:
                self._pcache.clear()
            self._pcache[ck] = out
        return out

    def save_params(self, scope, params):
        if scope in S.SCOPES and not S.is_scope_locked(scope):
            self._app.config[scope] = S.clamp_params(scope, params)
            self._app.save_config()
            if scope in WB.MOD_SPECS:
                WB.update_settings_if_active(self._app.config[scope], scope=scope)
        return True

    def _batch_response(self, kind, res):
        ok = res is not None and not any(r.get("failed") for r in res)
        needs_admin = sum(r.get("needs_admin", 0) for r in (res or []))
        wh_needs_admin = any(r.get("windhawk_needs_admin") for r in (res or []))
        tb_st = WB.get_status(base_dir=BASE_DIR, scope="taskbar")
        sm_st = WB.get_status(base_dir=BASE_DIR, scope="startmenu")
        return {
            "ok": ok,
            "kind": kind,
            "needs_admin": needs_admin,
            "windhawk_needs_admin": wh_needs_admin,
            "windhawk_installed": bool(tb_st.get("windhawk_installed") or sm_st.get("windhawk_installed")),
            "results": res,
            "message": summarize(res, kind),
            "taskbar_engine": tb_st,
            "startmenu_engine": sm_st,
        }

    # ---- 套用 / 還原 ----
    def apply_scope(self, scope, params):
        if scope not in S.SCOPES:
            return {"ok": False, "message": "未知的區域"}
        if S.is_scope_locked(scope):
            return {"ok": False, "locked": True,
                    "message": f"「{S.SPECS[scope]['label']}」目前已鎖定"}
        self.save_params(scope, params)
        return self._batch_response("apply", run_batch("apply", [scope], self._app.config))

    def restore_scope(self, scope):
        if scope not in S.SCOPES:
            return {"ok": False, "message": "未知的區域"}
        if S.is_scope_locked(scope):
            return {"ok": False, "locked": True,
                    "message": f"「{S.SPECS[scope]['label']}」目前已鎖定"}
        return self._batch_response("restore", run_batch("restore", [scope], self._app.config))

    def apply_all(self, all_params):
        scopes = effective_active_scopes() or ["desktop"]
        for sc in scopes:
            if isinstance(all_params, dict) and sc in all_params:
                self._app.config[sc] = S.clamp_params(sc, all_params[sc])
        self._app.save_config()
        return self._batch_response("apply", run_batch("apply", scopes, self._app.config))

    def restore_all(self):
        scopes = effective_active_scopes() or ["desktop"]
        return self._batch_response("restore", run_batch("restore", scopes, self._app.config))

    def get_progress(self):
        return dict(PROGRESS)

    def get_admin_result(self):
        try:
            if os.path.exists(ADMIN_RESULT_PATH):
                with open(ADMIN_RESULT_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def get_startup_scope(self):
        sc = getattr(self._app, "initial_scope", "")
        return sc if sc in S.ACTIVE_SCOPES else ""

    def relaunch_admin(self, scope="taskbar"):
        """WizTree 風格頂端橫幅：非強制由使用者主動以系統管理員身分重啟設定視窗。"""
        if is_admin():
            return {"ok": False, "already_admin": True, "message": "目前已是系統管理員身分"}
        sc = scope if scope in S.ACTIVE_SCOPES else "taskbar"
        self._app._relaunching_admin = True
        if not relaunch_as_admin(["--settings", "--scope", sc], show=1):
            self._app._relaunching_admin = False
            return {"ok": False, "message": "未取得系統管理員權限（UAC 已取消或無法提權）"}
        win = getattr(self._app, "window", None)
        if win is not None:
            def _close_old():
                try:
                    win.destroy()
                except Exception:
                    pass
            t = threading.Timer(0.25, _close_old)
            t.daemon = True
            t.start()
        return {"ok": True, "scope": sc, "message": "已啟動系統管理員視窗，正在重啟…"}

    # ---- 提權重試（UI 在結果出現 needs_admin > 0 時顯示「以管理員權限重試此區」） ----
    def apply_admin(self, scope="all"):
        return self._admin("--apply-admin", scope)

    def restore_admin(self, scope="all"):
        return self._admin("--restore-admin", scope)

    @staticmethod
    def _admin(flag, scope):
        if scope != "all" and scope not in S.SCOPES:
            return {"ok": False, "message": "未知的區域"}
        if scope != "all" and S.is_scope_locked(scope):
            return {"ok": False, "locked": True,
                    "message": f"「{S.SPECS[scope]['label']}」目前已鎖定，僅開放工作列／桌面測試"}
        if is_admin():
            return {"ok": False, "message": "目前已是系統管理員身分，請直接套用"}
        ok = relaunch_as_admin([flag, scope])
        return {"ok": ok, "message": "已送出提權請求，請在 UAC 視窗按「是」" if ok else "未取得管理員權限（UAC 被取消或無法提權）"}

    # ---- 系統 ----
    def set_autostart(self, val):
        return set_autostart(bool(val))

    def set_shortcut(self, val):
        return create_desktop_shortcut() if val else remove_desktop_shortcut()

    def get_watcher(self):
        wc = S.clamp_watcher(self._app.config.get("watcher"))
        locked = all(S.is_scope_locked(sc) for sc in S.WATCHER_SCOPES)
        if locked:
            wc["enabled"] = False
        wc["locked"] = locked
        return wc

    def set_watcher(self, val):
        """寫入設定；若監聽對應之桌面／開始功能表目前為鎖定狀態，強制保持關閉。"""
        wc = S.clamp_watcher(self._app.config.get("watcher"))
        locked = all(S.is_scope_locked(sc) for sc in S.WATCHER_SCOPES)
        wc["enabled"] = False if locked else bool(val)
        self._app.config["watcher"] = wc
        self._app.save_config(watcher_changed=True)
        if wc["enabled"] and not is_tray_running():
            spawn_background_tray()
        return wc["enabled"]

    def set_watcher_debounce(self, sec):
        wc = S.clamp_watcher(self._app.config.get("watcher"))
        wc["debounce_sec"] = S.clamp_watcher({"debounce_sec": sec})["debounce_sec"]
        self._app.config["watcher"] = wc
        self._app.save_config(watcher_changed=True)
        return wc["debounce_sec"]

    def set_restore_on_exit(self, val):
        lc = self._app.get_lifecycle()
        lc["restore_live_on_exit"] = bool(val)
        self._app.config["lifecycle"] = lc
        self._app.save_config(lifecycle_changed=True)
        return lc["restore_live_on_exit"]

    def set_exit_on_close(self, val):
        lc = self._app.get_lifecycle()
        lc["exit_when_window_closes"] = bool(val)
        self._app.config["lifecycle"] = lc
        self._app.save_config(lifecycle_changed=True)
        if (not lc["exit_when_window_closes"]) and (not is_admin()) and (not is_tray_running()):
            spawn_background_tray()
        return lc["exit_when_window_closes"]

    def open_folder(self, which):
        path = {"vault": ICON_VAULT_DIR, "backup": BACKUP_DIR}.get(which)
        try:
            if path and IS_WIN:
                os.startfile(path)
                return True
        except Exception:
            pass
        return False


# ==================== 主應用程式 ====================
class LiquidGlassApp:
    def __init__(self):
        self.config = S.default_config()
        self.window = None
        self.tray = None
        self.watcher = None
        self._relaunching_admin = False
        self.load_config()

    def get_lifecycle(self):
        return S.clamp_lifecycle(self.config.get("lifecycle"))

    @staticmethod
    def _read_raw_config():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return raw if isinstance(raw, dict) else None
        except Exception:
            return None

    def load_config(self):
        raw = self._read_raw_config() if os.path.exists(CONFIG_PATH) else None
        self.config = S.migrate_config(raw)
        if isinstance(raw, dict) and raw.get("version") != S.VERSION:
            self.save_config(watcher_changed=True, lifecycle_changed=True)  # 舊格式升級後寫回

    def save_config(self, watcher_changed=False, lifecycle_changed=False):
        """設定視窗與系統匣是兩個行程、共用同一個 config.json。
        除非這次就是要改 watcher/lifecycle，否則保留磁碟上的對應區塊，避免覆寫另一個行程的變更。"""
        try:
            self.config["version"] = S.VERSION
            if not (watcher_changed and lifecycle_changed):
                disk = self._read_raw_config()
                if disk is not None:
                    if not watcher_changed and "watcher" in disk:
                        self.config["watcher"] = S.clamp_watcher(disk["watcher"])
                    if not lifecycle_changed and "lifecycle" in disk:
                        self.config["lifecycle"] = S.clamp_lifecycle(disk["lifecycle"])
            atomic_write_json(CONFIG_PATH, self.config)
        except Exception:
            pass

    # ---- Watcher（跑在系統匣行程） ----
    def _on_watch_fire(self, scopes):
        active = [sc for sc in scopes if not S.is_scope_locked(sc)]
        if not active:
            return []
        res = run_batch("apply", active, self.config)
        if res is not None and any(r["applied"] for r in res):
            try:
                self.tray.notify(summarize(res, "apply"), APP_TITLE)
            except Exception:
                pass
        return res

    def apply_watcher_config(self):
        wc = S.clamp_watcher(self.config.get("watcher"))
        has_active = any(not S.is_scope_locked(sc) for sc in S.WATCHER_SCOPES)
        if wc["enabled"] and has_active:
            if self.watcher is None:
                self.watcher = IconWatcher(self._on_watch_fire, wc["debounce_sec"])
            self.watcher.configure(wc["debounce_sec"])
            if not self.watcher.running:
                self.watcher.start()
        elif self.watcher is not None:
            self.watcher.stop()
            self.watcher = None

    def _config_poll_loop(self):
        """設定視窗（另一個行程）改了 config.json 時，系統匣即時跟上：重載參數並啟停 Watcher；收到停止訊號則退出。"""
        last = _mtime_ns(CONFIG_PATH)
        while True:
            if _wait_tray_stop(1.5):
                self.quit_app(restore=False)
                return
            cur = _mtime_ns(CONFIG_PATH)
            if cur != last:
                last = cur
                try:
                    self.load_config()
                    self.apply_watcher_config()
                except Exception:
                    pass

    def _toggle_watcher(self, *_a):
        wc = S.clamp_watcher(self.config.get("watcher"))
        locked = all(S.is_scope_locked(sc) for sc in S.WATCHER_SCOPES)
        wc["enabled"] = False if locked else (not wc["enabled"])
        self.config["watcher"] = wc
        self.save_config(watcher_changed=True)
        self.apply_watcher_config()

    def _watcher_enabled(self):
        return bool(S.clamp_watcher(self.config.get("watcher"))["enabled"])

    # ---- 設定視窗 ----
    def open_settings_window(self, initial_scope=""):
        self.initial_scope = initial_scope if initial_scope in S.ACTIVE_SCOPES else ""
        api = LiquidGlassApi(self)
        threading.Thread(target=self._warm_previews, args=(api,), daemon=True).start()

        import webview
        kw = dict(title=f"{APP_TITLE} v{S.VERSION}", url=UI_HTML_PATH, width=780, height=900,
                  min_size=(640, 640), resizable=True, js_api=api, background_color="#080b1a")
        try:
            self.window = webview.create_window(hidden=True, **kw)
        except TypeError:
            self.window = webview.create_window(**kw)
        shown = threading.Event()
        show_timer = [None]

        def show(*_a):
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
                if (not is_admin()) and (not is_tray_running()) and (not self.get_lifecycle()["exit_when_window_closes"]):
                    # 視窗出現 1 秒後才在背景啟動系統匣，避免兩個程序同時初始化搶資源（提權或關閉即結束模式不另起常駐匣）
                    t_tray = threading.Timer(1.0, spawn_background_tray)
                    t_tray.daemon = True
                    t_tray.start()

        try:
            self.window.events.loaded += show
        except Exception:
            pass
        show_timer[0] = threading.Timer(5.0, show)  # 保險：就算 loaded 沒觸發也一定顯示
        show_timer[0].daemon = True
        show_timer[0].start()

        start_kw = dict(gui="edgechromium", private_mode=False, storage_path=_webview_storage_path())
        try:
            try:
                webview.start(**start_kw)
            except TypeError:
                webview.start(gui="edgechromium")  # 舊版 pywebview 不支援持久化 profile
        finally:
            self.handle_window_closed()

    def handle_window_closed(self):
        """設定視窗關閉時的收尾：若非管理員提權重啟，依使用者設定決定是否關閉背景匣並自動還原即時引擎。"""
        if self._relaunching_admin:
            return
        self.load_config()
        lc = self.get_lifecycle()
        if lc["exit_when_window_closes"]:
            _signal_tray_stop()
            if lc["restore_live_on_exit"]:
                restore_live_scopes_on_exit()
            return
        if lc["restore_live_on_exit"] and not is_tray_running():
            restore_live_scopes_on_exit()

    def _warm_previews(self, api):
        """與 WebView2 開機並行：先把 numpy / PIL 載入、外觀框算好、預覽渲染一次。"""
        try:
            dpr = 1.0
            if IS_WIN:
                try:
                    dpr = max(1.0, user32.GetDpiForSystem() / 96.0)
                except Exception:
                    pass
            for sc in S.ACTIVE_SCOPES:
                api._render_preview(sc, S.clamp_params(sc, self.config[sc]), min(3.0, dpr))
        except Exception:
            pass

    # ---- 系統匣（只載入 pystray + PIL） ----
    def run_tray_blocking(self):
        import pystray
        from PIL import Image
        img = None
        for name in ("LiquidGlassIconCenter.png", "LiquidGlassIconCenter.ico"):
            p = resource_path(name)
            if os.path.exists(p):
                try:
                    img = Image.open(p).convert("RGBA").resize((64, 64))
                    break
                except Exception:
                    pass
        if img is None:
            import glass_render
            img = glass_render.tray_fallback_icon(64)

        def bg(kind, scopes):
            def run():
                res = run_batch(kind, scopes, self.config)
                try:
                    self.tray.notify(summarize(res, kind), APP_TITLE)
                except Exception:
                    pass
            return lambda: threading.Thread(target=run, daemon=True).start()

        wh_ok = WB.is_windhawk_installed()

        def scope_items(kind):
            items = []
            for sc in S.SCOPES:
                locked = S.is_scope_locked(sc)
                need_wh = bool(S.SPECS[sc].get("requires_windhawk")) and not wh_ok
                suffix = "（🔒 已封鎖）" if locked else ("（🔌 需安裝 Windhawk）" if need_wh else "")
                title = f"{S.SPECS[sc]['icon']} {S.SPECS[sc]['label']}{suffix}"
                items.append(pystray.MenuItem(title, bg(kind, [sc]), enabled=(not locked and not need_wh)))
            return items

        quick_items = [
            pystray.MenuItem("🖥️ 套用桌面 Liquid Glass", bg("apply", ["desktop"])),
            pystray.MenuItem("🛡️ 還原桌面原生圖示", bg("restore", ["desktop"])),
        ]
        if wh_ok:
            quick_items = [
                pystray.MenuItem("📌 啟用工作列 Liquid Glass", bg("apply", ["taskbar"])),
                pystray.MenuItem("🛡️ 還原工作列原生圖示", bg("restore", ["taskbar"])),
            ] + quick_items

        exit_items = []
        if wh_ok:
            exit_items.append(pystray.MenuItem("🛡️ 退出並還原即時效果", lambda *_: self.quit_app(restore=True)))
        exit_items.append(pystray.MenuItem("❌ 退出程式", lambda *_: self.quit_app()))

        menu = pystray.Menu(
            pystray.MenuItem("🪟 開啟設定中樞", lambda: spawn_settings_ui(), default=True),
            pystray.Menu.SEPARATOR,
            *quick_items,
            pystray.MenuItem("區域管理", pystray.Menu(*scope_items("apply"))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("📁 開啟母本備份資料夾（唯讀）", lambda: self._open(BACKUP_DIR)),
            *exit_items,
        )
        _init_tray_stop_event()
        self.tray = pystray.Icon(APP_NAME, img, APP_TITLE, menu)
        self.apply_watcher_config()
        threading.Thread(target=self._config_poll_loop, daemon=True, name="lgic-config-poll").start()
        self.tray.run()

    @staticmethod
    def _open(path):
        try:
            os.startfile(path)
        except Exception:
            pass

    def quit_app(self, *args, restore=None):
        try:
            self.load_config()
            should_restore = bool(self.get_lifecycle()["restore_live_on_exit"]) if restore is None else bool(restore)
            if should_restore:
                restore_live_scopes_on_exit()
        except Exception:
            pass
        try:
            if self.watcher:
                self.watcher.stop()
            if self.tray:
                self.tray.stop()
        finally:
            os._exit(0)


# ==================== 程式主入口 ====================
def _parse_target(argv, flag):
    i = argv.index(flag)
    target = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("--") else "all"
    scopes = list(S.ACTIVE_SCOPES) if target == "all" else [target]
    return target, scopes


def _cli(app, argv):
    """--apply / --restore [desktop|startmenu|taskbar|all]：無介面批次執行。"""
    kind = "apply" if "--apply" in argv else "restore"
    flag = "--apply" if kind == "apply" else "--restore"
    target, scopes = _parse_target(argv, flag)
    if any(sc not in S.SCOPES for sc in scopes):
        print("scope must be one of: all, " + ", ".join(S.SCOPES))
        return 2
    locked = [sc for sc in scopes if S.is_scope_locked(sc)]
    if locked:
        labels = "、".join(S.SPECS[sc]["label"] for sc in locked)
        print(f"已鎖定區域不允許執行：{labels}（目前僅開放：{', '.join(S.ACTIVE_SCOPES)}）")
        return 3
    skipped_opt = []
    if target == "all":
        avail = effective_active_scopes() or ["desktop"]
        skipped_opt = [sc for sc in scopes if sc not in avail]
        scopes = avail
    res = run_batch(kind, scopes, app.config)
    msg = summarize(res, kind)
    if skipped_opt:
        opt_labels = "、".join(S.SPECS[sc]["label"] for sc in skipped_opt)
        msg += f"（未偵測到 Windhawk，略過選用擴充：{opt_labels}）"
    print(msg)
    return 0


def _cli_admin(app, argv):
    """--apply-admin / --restore-admin [scope|all]。

    非管理員：以 runas 提權重啟自己（跳 UAC）；已是管理員：只處理『需管理員』的來源（公用位置），
    並把結果寫到 admin_last_result.json（提權行程沒有主控台，UI 可據此顯示結果）。
    """
    kind = "apply" if "--apply-admin" in argv else "restore"
    flag = "--apply-admin" if kind == "apply" else "--restore-admin"
    target, scopes = _parse_target(argv, flag)
    if any(sc not in S.SCOPES for sc in scopes):
        print("scope must be one of: all, " + ", ".join(S.SCOPES))
        return 2
    locked = [sc for sc in scopes if S.is_scope_locked(sc)]
    if locked:
        labels = "、".join(S.SPECS[sc]["label"] for sc in locked)
        print(f"已鎖定區域不允許執行：{labels}（目前僅開放：{', '.join(S.ACTIVE_SCOPES)}）")
        return 3
    if not is_admin():
        ok = relaunch_as_admin([flag, target])
        print("已送出提權請求" if ok else "未取得管理員權限（UAC 被取消或無法提權）")
        return 0 if ok else 1
    sources = tuple(src for src, need in S.SOURCE_NEEDS_ADMIN.items() if need)
    res = run_batch(kind, scopes, app.config, sources=sources)
    msg = summarize(res, kind)
    print(msg)
    try:
        atomic_write_json(ADMIN_RESULT_PATH, {"kind": kind, "scopes": scopes, "message": msg,
                                              "results": res, "time": time.time()})
    except Exception:
        pass
    return 0


def _webview_storage_path():
    return (PROFILE_DIR + "_admin") if is_admin() else PROFILE_DIR


def _parse_startup_scope(argv):
    for i, a in enumerate(argv or ()):
        if a == "--scope" and i + 1 < len(argv):
            val = str(argv[i + 1]).strip().lower()
            if val in S.ACTIVE_SCOPES:
                return val
        elif isinstance(a, str) and a.startswith("--scope="):
            val = a.split("=", 1)[1].strip().lower()
            if val in S.ACTIVE_SCOPES:
                return val
    return ""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ensure_dirs()
    app = LiquidGlassApp()
    if "--apply-admin" in argv or "--restore-admin" in argv:
        return _cli_admin(app, argv)
    if "--apply" in argv or "--restore" in argv:
        return _cli(app, argv)
    if "--autostart" in argv:
        if acquire_tray_mutex():
            app.run_tray_blocking()
        return 0
    app.open_settings_window(initial_scope=_parse_startup_scope(argv))  # --settings 與一般點擊相同；系統匣由視窗顯示後再背景啟動
    return 0


if __name__ == "__main__":
    sys.exit(main())
