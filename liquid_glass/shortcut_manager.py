"""liquid_glass/shortcut_manager.py -- 捷徑讀寫（.lnk / .url）、256px 圖示擷取、母本備份與批次引擎。

Functionality:
1. 提供 COM WScript.Shell / IShellLink 讀寫 .lnk，以及保留原編碼與換行之 .url 逐行文字替換。
2. 透過 Win32 HICON / HBITMAP 與 IShellItemImageFactory（MSIX / AUMID）無損擷取 256px 原生圖示。
3. 維護 Original_Shortcuts_Backup 母本備份、軟體升級過期偵測刷新與 SHA-1 增量算圖快取。
4. 協調純桌面批次算圖，並針對 taskbar / startmenu 派送 Windhawk 選用擴充暨退出自動還原。
"""
from __future__ import annotations

import codecs
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import json
import os
import re
import shutil
import threading
import time
from typing import Any

from liquid_glass import runtime as RT
from liquid_glass import specs as S
from liquid_glass import windhawk_bridge as WB

# Lines 30-115: COM 執行緒安全初始化與 .lnk 捷徑讀寫
_tls = threading.local()
_DENIED_CODES = {5, -2147024891, 0x80070005}  # ERROR_ACCESS_DENIED / E_ACCESSDENIED

ShortcutInfo = namedtuple("ShortcutInfo", "target args icon_loc icon_idx ok")
_EMPTY_INFO = ShortcutInfo("", "", "", 0, False)

UrlInfo = namedtuple("UrlInfo", "target args icon_loc icon_idx ok encoding newline")
_EMPTY_URL_INFO = UrlInfo("", "", "", 0, False, "utf-8", "\r\n")


def _ensure_com() -> None:
    """每個執行緒只初始化一次 COM。"""
    if getattr(_tls, "com", False):
        return
    import pythoncom

    pythoncom.CoInitialize()
    _tls.com = True


def _wsh() -> Any:
    wsh = getattr(_tls, "wsh", None)
    if wsh is None:
        _ensure_com()
        import win32com.client

        wsh = win32com.client.Dispatch("WScript.Shell")
        _tls.wsh = wsh
    return wsh


def _is_denied(exc: BaseException, path: str) -> bool:
    """判斷例外是否為權限不足（PermissionError、winerror 5、COM scode 或目錄不可寫）。"""
    if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 5:
        return True
    args = getattr(exc, "args", ()) or ()
    codes = {a for a in args if isinstance(a, int)}
    if len(args) > 2 and isinstance(args[2], tuple) and len(args[2]) >= 6 and isinstance(args[2][5], int):
        codes.add(args[2][5])
    if codes & _DENIED_CODES:
        return True
    return not os.access(os.path.dirname(path) or ".", os.W_OK)


def read_shortcut(lnk_path: str) -> ShortcutInfo:
    """讀取 .lnk 捷徑目標、參數與圖示位置。"""
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


def _write_icon_shelllink(lnk_path: str, ico_path: str) -> None:
    """以 IShellLink / IPersistFile 直接修改 IconLocation，保留 MSIX / AUMID 的 IDList。"""
    _ensure_com()
    import pythoncom
    from win32com.shell import shell

    link = pythoncom.CoCreateInstance(
        shell.CLSID_ShellLink,
        None,
        pythoncom.CLSCTX_INPROC_SERVER,
        shell.IID_IShellLink,
    )
    pf = link.QueryInterface(pythoncom.IID_IPersistFile)
    pf.Load(lnk_path, 0)
    link.SetIconLocation(ico_path, 0)
    pf.Save(lnk_path, 0)


def write_shortcut_icon(lnk_path: str, ico_path: str) -> str:
    """寫入 .lnk 圖示路徑；回傳 'ok' / 'denied' / 'fail'。"""
    try:
        sc = _wsh().CreateShortcut(lnk_path)
        if not (sc.TargetPath or "").strip():
            _write_icon_shelllink(lnk_path, ico_path)
            return "ok"
        sc.IconLocation = f"{ico_path},0"
        sc.Save()
        return "ok"
    except Exception as exc:
        return "denied" if _is_denied(exc, lnk_path) else "fail"


# Lines 117-315: .url（網際網路捷徑）無損 INI 讀寫與 UTF-7 Unicode 區段處理
_SECTION_RE = re.compile(r"^\[(.+?)\]$")


def decode_url_bytes(raw: bytes) -> tuple[str, str]:
    """依 S.URL_ENCODINGS 嘗試解碼並保留原編碼，全失敗時以 latin-1 無損保底。"""
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


def parse_url_text(text: str) -> dict[str, str | None]:
    """僅解析 [InternetShortcut] 區段之 URL / IconFile / IconIndex。"""
    out: dict[str, str | None] = {"url": None, "icon_file": None, "icon_index": None}
    want = S.URL_SECTION.lower()
    sect: str | None = None
    for line in text.split("\n"):
        stripped = line.strip()
        match = _SECTION_RE.match(stripped)
        if match:
            sect = match.group(1).strip().lower()
            continue
        if sect != want or not stripped or stripped[0] in ";#" or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        key_low = key.strip().lower()
        if key_low == "url":
            out["url"] = val.strip()
        elif key_low == S.URL_ICON_KEYS[0].lower():
            out["icon_file"] = val.strip()
        elif key_low == S.URL_ICON_KEYS[1].lower():
            out["icon_index"] = val.strip()
    return out


def rewrite_url_text(text: str, icon_file: str, icon_index: int = 0) -> str:
    """逐行替換 .url 之 IconFile= / IconIndex=，不動註解與其餘區段。"""
    lines = text.split("\n")
    crlf = "\r\n" in text
    eol = "\r" if crlf else ""
    values = {
        S.URL_ICON_KEYS[0].lower(): (S.URL_ICON_KEYS[0], str(icon_file)),
        S.URL_ICON_KEYS[1].lower(): (S.URL_ICON_KEYS[1], str(int(icon_index))),
    }
    want = S.URL_SECTION.lower()
    sect: str | None = None
    start: int | None = None
    end: int | None = None
    seen: set[str] = set()

    for idx, line in enumerate(lines):
        stripped = line.strip()
        match = _SECTION_RE.match(stripped)
        if match:
            if sect == want and end is None:
                end = idx
            sect = match.group(1).strip().lower()
            if sect == want and start is None:
                start = idx
            continue
        if sect != want or not stripped or stripped[0] in ";#" or "=" not in stripped:
            continue
        key_low = stripped.partition("=")[0].strip().lower()
        if key_low in values:
            keep_cr = "\r" if line.endswith("\r") else ""
            lines[idx] = line[: line.index("=") + 1] + values[key_low][1] + keep_cr
            seen.add(key_low)

    if start is not None and end is None:
        end = len(lines)
    missing = [values[k] for k in values if k not in seen]
    if missing:
        new_lines = [f"{k}={v}{eol}" for k, v in missing]
        if start is None:
            block = [f"[{S.URL_SECTION}]{eol}"] + new_lines
            if lines and lines[-1] == "":
                lines[-1:-1] = block
            else:
                lines.extend(block)
        else:
            last = start
            for j in range(start + 1, end or len(lines)):
                if lines[j].strip():
                    last = j
            if lines[last] and not lines[last].endswith("\r") and crlf:
                lines[last] += "\r"
            lines[last + 1 : last + 1] = new_lines
    return "\n".join(lines)


def _parse_url_wide_icon(raw_bytes: bytes) -> tuple[str | None, int | None]:
    try:
        text = raw_bytes.decode("latin-1")
    except Exception:
        return None, None
    sect: str | None = None
    w_loc: str | None = None
    w_idx: int | None = None
    for line in text.split("\n"):
        stripped = line.strip()
        match = _SECTION_RE.match(stripped)
        if match:
            sect = match.group(1).strip().lower()
            continue
        if sect != "internetshortcut.w" or not stripped or stripped[0] in ";#" or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        key_low = key.strip().lower()
        if key_low == S.URL_ICON_KEYS[0].lower():
            raw_v = val.strip().strip('"')
            if raw_v:
                try:
                    w_loc = raw_v.encode("ascii").decode("utf-7")
                except Exception:
                    pass
        elif key_low == S.URL_ICON_KEYS[1].lower():
            try:
                w_idx = int(val.strip())
            except (TypeError, ValueError):
                pass
    return w_loc, w_idx


def _url_has_wide_section(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            return b"[InternetShortcut.W]" in handle.read()
    except Exception:
        return False


def _write_url_icon_com(path: str, ico_path: str, icon_index: int = 0) -> None:
    _ensure_com()
    import pythoncom
    from win32com.shell import shell

    isu = pythoncom.CoCreateInstance(
        "{FBF23B40-E3F0-101B-8488-00AA003E56F8}",
        None,
        pythoncom.CLSCTX_INPROC_SERVER,
        shell.IID_IShellLinkW,
    )
    pf = isu.QueryInterface(pythoncom.IID_IPersistFile)
    pf.Load(path, 0)
    isu.SetIconLocation(ico_path, int(icon_index))
    pf.Save(path, 0)


def read_url_shortcut(path: str) -> UrlInfo:
    """讀取 .url 捷徑圖示與目標網址，優先取 [InternetShortcut.W] 的無損 Unicode 路徑。"""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        text, enc = decode_url_bytes(raw)
        parsed = parse_url_text(text)
        w_loc, w_idx = _parse_url_wide_icon(raw)
        loc = os.path.expandvars(w_loc if w_loc else (parsed["icon_file"] or "")).strip().strip('"')
        if w_idx is not None:
            idx = w_idx
        else:
            try:
                idx = int(parsed["icon_index"] or 0)
            except (TypeError, ValueError):
                idx = 0
        return UrlInfo("", parsed["url"] or "", loc, idx, True, enc, "\r\n" if "\r\n" in text else "\n")
    except Exception:
        return _EMPTY_URL_INFO


def write_url_shortcut_icon(path: str, ico_path: str, icon_index: int = 0) -> str:
    """改寫 .url 圖示路徑；若含非 ASCII 字元則在 Windows 上透過 COM 補齊寬字元區段。"""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        text, enc = decode_url_bytes(raw)
        need_wide = bool(RT.IS_WIN and not str(ico_path).isascii())
        wide_missing = need_wide and not _url_has_wide_section(path)
        new_text = rewrite_url_text(text, ico_path, icon_index)
        if new_text == text and not wide_missing:
            return "ok"
        try:
            data = new_text.encode(enc)
        except UnicodeEncodeError:
            return "fail"
        if new_text != text:
            with open(path, "wb") as handle:
                handle.write(data)
        if need_wide:
            try:
                _write_url_icon_com(path, ico_path, icon_index)
            except Exception as exc:
                return "denied" if _is_denied(exc, path) else "fail"
        return "ok"
    except Exception as exc:
        return "denied" if _is_denied(exc, path) else "fail"


def _is_url(path: str) -> bool:
    return str(path).lower().endswith(".url")


def read_any_shortcut(path: str) -> ShortcutInfo | UrlInfo:
    """依副檔名分派讀取 .lnk 或 .url。"""
    return read_url_shortcut(path) if _is_url(path) else read_shortcut(path)


_SELF_WRITE: dict[str, float | None] = {"t": None}


def mark_self_write(t: float | None = None) -> None:
    """記錄本工具寫入捷徑的時間戳，讓 Watcher 過濾自我觸發的事件。"""
    _SELF_WRITE["t"] = time.monotonic() if t is None else t


def write_any_shortcut(path: str, ico_path: str) -> str:
    """依副檔名寫入圖示並自動標記自我寫入時間。"""
    res = write_url_shortcut_icon(path, ico_path) if _is_url(path) else write_shortcut_icon(path, ico_path)
    mark_self_write()
    return res


def is_ours(icon_loc: str | None) -> bool:
    """判斷捷徑目前圖示是否屬於本工具（搬動程式資料夾位置仍可正確辨識）。"""
    if not icon_loc:
        return False
    normed = RT._norm(icon_loc)
    return (
        normed.startswith(RT._norm(RT.ICON_VAULT_DIR) + os.sep)
        or (os.sep + "iconvault" + os.sep) in normed
        or normed.endswith("_glass.ico")
    )


# Lines 317-505: Win32 HICON / HBITMAP / AUMID 256px 乾淨圖示擷取
def hicon_to_pil(hicon: int) -> Any:
    """將 Win32 HICON 轉為 RGBA PIL 影像並確實釋放 GDI 控制代碼。"""
    import numpy as np
    from PIL import Image

    if not hicon or not RT.IS_WIN:
        return None
    info = RT.ICONINFO()
    if not RT.user32.GetIconInfo(hicon, ctypes.byref(info)):
        return None
    hdc = None
    try:
        hbm = info.hbmColor if info.hbmColor else info.hbmMask
        if not hbm:
            return None
        hdc = RT.user32.GetDC(0)
        bih = RT.BITMAPINFOHEADER()
        bih.biSize = ctypes.sizeof(RT.BITMAPINFOHEADER)
        RT.gdi32.GetDIBits(hdc, hbm, 0, 0, None, ctypes.byref(bih), 0)
        w, h = bih.biWidth, abs(bih.biHeight)
        if not info.hbmColor:
            h //= 2
        if w <= 0 or h <= 0:
            return None
        buf = (ctypes.c_byte * (w * h * 4))()
        bih.biHeight = -h
        bih.biBitCount = 32
        bih.biCompression = 0
        RT.gdi32.GetDIBits(hdc, hbm, 0, h, ctypes.byref(buf), ctypes.byref(bih), 0)
    finally:
        if hdc:
            RT.user32.ReleaseDC(0, hdc)
        if info.hbmColor:
            RT.gdi32.DeleteObject(info.hbmColor)
        if info.hbmMask:
            RT.gdi32.DeleteObject(info.hbmMask)

    arr = np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4))
    b, g, r, a = arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3]
    if np.max(a) == 0:
        a = np.where((r == 0) & (g == 0) & (b == 0), 0, 255).astype(np.uint8)
    return Image.fromarray(np.stack([r, g, b, a], axis=-1), mode="RGBA")


def hbitmap_to_pil(hbm: int) -> Any:
    """將 IShellItemImageFactory 回傳之 32bpp 預乘 HBITMAP 轉為一般 RGBA PIL 影像。"""
    import numpy as np
    from PIL import Image

    if not hbm or not RT.IS_WIN:
        return None
    hdc = RT.user32.GetDC(0)
    try:
        bih = RT.BITMAPINFOHEADER()
        bih.biSize = ctypes.sizeof(RT.BITMAPINFOHEADER)
        RT.gdi32.GetDIBits(hdc, hbm, 0, 0, None, ctypes.byref(bih), 0)
        w, h = bih.biWidth, abs(bih.biHeight)
        if w <= 0 or h <= 0:
            return None
        buf = (ctypes.c_byte * (w * h * 4))()
        bih.biHeight = -h
        bih.biPlanes = 1
        bih.biBitCount = 32
        bih.biCompression = 0
        RT.gdi32.GetDIBits(hdc, hbm, 0, h, ctypes.byref(buf), ctypes.byref(bih), 0)
    finally:
        if hdc:
            RT.user32.ReleaseDC(0, hdc)
    arr = np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 4)).astype(np.float32)
    b, g, r, a = arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3]
    if float(a.max()) == 0.0:
        a = np.full_like(a, 255.0)
    else:
        scale = np.where(a > 0, 255.0 / np.maximum(a, 1.0), 0.0)
        r = np.clip(r * scale, 0, 255)
        g = np.clip(g * scale, 0, 255)
        b = np.clip(b * scale, 0, 255)
    out = np.stack([r, g, b, a], axis=-1).astype(np.uint8)
    return Image.fromarray(out, mode="RGBA")


def aumid_from_parsing_name(name: str | None) -> str:
    """從 Shell 解析名稱擷取 AUMID（'Pkg_xxx!App'）；取不到回傳空字串。"""
    if not name:
        return ""
    text = str(name).strip()
    low = text.lower()
    marker = "appsfolder\\"
    if marker in low:
        return text[low.index(marker) + len(marker) :].strip()
    tail = text.rsplit("\\", 1)[-1]
    return tail.strip() if "!" in tail else ""


def resolve_aumid(lnk_path: str) -> str:
    """查詢捷徑的 AppUserModel.ID，依序檢查屬性庫與 IDList Shell 解析名稱。"""
    if not RT.IS_WIN:
        return ""
    try:
        _ensure_com()
        import pythoncom
        from win32com.shell import shell, shellcon

        link = pythoncom.CoCreateInstance(
            shell.CLSID_ShellLink,
            None,
            pythoncom.CLSCTX_INPROC_SERVER,
            shell.IID_IShellLink,
        )
        link.QueryInterface(pythoncom.IID_IPersistFile).Load(lnk_path, 0)
    except Exception:
        return ""
    try:
        from win32com.propsys import propsys, pscon

        store = link.QueryInterface(propsys.IID_IPropertyStore)
        val = store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue()
        if val:
            return str(val).strip()
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


def _shell_item_image(parsing_name: str, size: int = 256) -> Any:
    if not RT.IS_WIN or not parsing_name:
        return None
    from PIL import Image

    RT.shell32.SHCreateItemFromParsingName.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(RT._GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    RT.shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
    iid = RT._GUID.from_str("BCC18B79-BA16-442F-80C4-8A59C30C463B")
    pv = ctypes.c_void_p()
    if RT.shell32.SHCreateItemFromParsingName(parsing_name, None, ctypes.byref(iid), ctypes.byref(pv)) != 0 or not pv.value:
        return None
    vtbl = ctypes.cast(ctypes.cast(pv, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    try:
        get_image = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, RT._SIZE, wintypes.UINT, ctypes.POINTER(wintypes.HBITMAP))(vtbl[3])
        hbm = wintypes.HBITMAP()
        if get_image(pv, RT._SIZE(size, size), 0x5, ctypes.byref(hbm)) != 0 or not hbm.value:
            return None
        try:
            im = hbitmap_to_pil(hbm.value)
        finally:
            RT.gdi32.DeleteObject(hbm.value)
        if im is None:
            return None
        return im.resize((256, 256), Image.Resampling.LANCZOS)
    except Exception:
        return None
    finally:
        try:
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(pv)
        except Exception:
            pass


def extract_icon_from_shell_item(lnk_path: str) -> Any:
    """TargetPath 與 IconLocation 皆為空時，透過 shell:AppsFolder\\<AUMID> 抽取 256px 圖示。"""
    if not RT.IS_WIN:
        return None
    try:
        _ensure_com()
    except Exception:
        return None
    candidates: list[str] = []
    aumid = resolve_aumid(lnk_path)
    if aumid:
        candidates.append(S.aumid_shell_path(aumid))
    candidates.append(lnk_path)
    for name in candidates:
        im = _shell_item_image(name, 256)
        if im is not None:
            return im
    return None


def extract_icon_256(path: str, info: ShortcutInfo | UrlInfo | None = None) -> Any:
    """取得 256x256 RGBA 原圖；自動跳過已指向 IconVault 的路徑以免玻璃疊玻璃。"""
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
            else:
                im = extract_icon_from_shell_item(lnk_path)
                if im is not None:
                    return im
        if path.lower().endswith(".ico"):
            try:
                return Image.open(path).convert("RGBA").resize((256, 256), Image.Resampling.LANCZOS)
            except Exception:
                pass
        if not RT.IS_WIN:
            return None
        hicon = ctypes.c_void_p()
        hr = RT.shell32.SHDefExtractIconW(path, icon_idx, 0, ctypes.byref(hicon), None, 256)
        if hr == 0 and hicon.value:
            im = hicon_to_pil(hicon.value)
            RT.user32.DestroyIcon(hicon.value)
            if im and (im.width >= 48 or im.height >= 48):
                return im.resize((256, 256), Image.Resampling.LANCZOS)
    except Exception:
        pass
    try:
        if RT.IS_WIN:
            large = (wintypes.HICON * 1)()
            if RT.shell32.ExtractIconExW(path, icon_idx, large, None, 1) > 0 and large[0]:
                im = hicon_to_pil(large[0])
                RT.user32.DestroyIcon(large[0])
                if im:
                    return im.resize((256, 256), Image.Resampling.LANCZOS)
    except Exception:
        pass
    return None


# Lines 507-655: 三區列舉、母本備份、過期檢查與渲染快取
_SKIP_WORDS = ("uninstall", "解除安裝", "卸載", "卸载")


def _safe_name(text: str) -> str:
    return "".join(c if c.isalnum() or c in (" ", "_", "-") else "_" for c in text).strip().replace(" ", "_")


def taskbar_dir() -> str:
    """使用者工作列釘選捷徑資料夾。"""
    return os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft",
        "Internet Explorer",
        "Quick Launch",
        "User Pinned",
        "TaskBar",
    )


def start_menu_roots() -> list[tuple[str, str]]:
    """開始功能表個人與公用根目錄清單。"""
    return [
        (os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"), "user"),
        (os.path.join(os.environ.get("ProgramData", ""), "Microsoft", "Windows", "Start Menu", "Programs"), "common"),
    ]


@dataclass
class Item:
    scope: str
    path: str
    primary: str
    legacy: str | None
    ico_path: str
    source: str = "user"
    ext: str = ".lnk"

    @property
    def ico_name(self) -> str:
        return os.path.basename(self.ico_path)


def scope_roots(scope: str) -> list[tuple[str, str, bool]]:
    """回傳指定區域實際存在的掃描根目錄 [(目錄, 來源標籤, 是否遞迴)]。"""
    if scope == "desktop":
        cand = [(RT.desktop_dir(), S.SOURCE_USER, False), (RT.public_desktop_dir(), S.SOURCE_COMMON, False)]
    elif scope == "taskbar":
        cand = [(taskbar_dir(), S.SOURCE_USER, False)]
    elif scope == "startmenu":
        cand = [(path, tag, True) for path, tag in start_menu_roots()]
    else:
        return []
    allowed = S.SCOPE_SOURCES.get(scope, ())
    out: list[tuple[str, str, bool]] = []
    seen: set[str] = set()
    for directory, source, recursive in cand:
        normed = RT._norm(directory)
        if source not in allowed or not directory or not os.path.isdir(directory) or normed in seen:
            continue
        seen.add(normed)
        out.append((directory, source, recursive))
    return out


def enumerate_scope(scope: str) -> list[Item]:
    """列舉指定區域所有符合規格之捷徑項目與對應母本／IconVault 路徑。"""
    items: list[Item] = []
    suffix = S.SPECS[scope]["ico_suffix"]
    for root, source, _rec in scope_roots(scope):
        parts = S.backup_parts(scope, source)
        if scope in ("desktop", "taskbar"):
            for filename in sorted(os.listdir(root)):
                ext = S.shortcut_ext(scope, filename)
                if not ext or (scope == "desktop" and filename == RT.SELF_SHORTCUT_NAME):
                    continue
                stem = filename[: -len(ext)]
                items.append(
                    Item(
                        scope=scope,
                        path=os.path.join(root, filename),
                        primary=os.path.join(RT.BACKUP_DIR, *parts, filename),
                        legacy=os.path.join(RT.BACKUP_DIR, filename) if scope == "taskbar" else None,
                        ico_path=os.path.join(RT.ICON_VAULT_DIR, _safe_name(stem) + S.vault_tag(scope, source, ext) + suffix),
                        source=source,
                        ext=ext,
                    )
                )
        else:
            for dp, _dn, fns in os.walk(root):
                for filename in sorted(fns):
                    ext = S.shortcut_ext(scope, filename)
                    if not ext or any(word in filename.lower() for word in _SKIP_WORDS):
                        continue
                    full = os.path.join(dp, filename)
                    rel = os.path.relpath(full, root)
                    digest = hashlib.sha1((source + "|" + rel.lower()).encode("utf-8")).hexdigest()[:6]
                    items.append(
                        Item(
                            scope=scope,
                            path=full,
                            primary=os.path.join(RT.BACKUP_DIR, *parts, rel),
                            legacy=os.path.join(RT.BACKUP_DIR, filename),
                            ico_path=os.path.join(
                                RT.ICON_VAULT_DIR,
                                f"{_safe_name(filename[:-len(ext)])}_{digest}{S.vault_tag(scope, source, ext)}{suffix}",
                            ),
                            source=source,
                            ext=ext,
                        )
                    )
    return items


def resolve_backup(item: Item, cur_ours: bool) -> tuple[str | None, bool]:
    """定位或首次複製母本捷徑；若無母本且現場已是我方玻璃圖示則拒絕冒險處理。"""
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


def backup_is_stale(live_path: str, live_info: Any, backup_path: str, backup_info: Any) -> bool:
    """比對來源特徵與 mtime；軟體升級重寫捷徑時回傳 True 以觸發母本刷新。"""
    if not getattr(live_info, "ok", False):
        return False
    if not getattr(backup_info, "ok", False):
        return True
    if RT._norm(live_info.target) != RT._norm(backup_info.target):
        return True
    if RT._norm(live_info.icon_loc) != RT._norm(backup_info.icon_loc) or live_info.icon_idx != backup_info.icon_idx:
        return True
    if (live_info.args or "") != (backup_info.args or ""):
        return True
    ma, mb = RT._mtime_ns(live_path), RT._mtime_ns(backup_path)
    return ma is not None and mb is not None and ma != mb


def refresh_backup(item: Item) -> bool:
    """以現場最新乾淨捷徑原子覆寫母本備份，失敗時清理 .tmp 並保留舊母本。"""
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


def _sig(path: str) -> str:
    try:
        st = os.stat(path)
        return f"{RT._norm(path)}|{st.st_size}|{st.st_mtime_ns}"
    except OSError:
        return f"{RT._norm(path)}|-"


def source_signature(backup_path: str, sinfo: ShortcutInfo | UrlInfo) -> str:
    """組合母本與真實圖示來源的檔案特徵字串，供渲染快取比對。"""
    parts = [_sig(backup_path)]
    if sinfo.icon_loc and not is_ours(sinfo.icon_loc):
        parts += [_sig(sinfo.icon_loc), str(sinfo.icon_idx)]
    elif sinfo.target:
        parts.append(_sig(sinfo.target))
    return "||".join(parts)


def make_key(scope: str, params: dict[str, float], sig: str) -> str:
    """產生結合 RENDER_VERSION、區域參數與來源簽章的 SHA-1 快取鍵。"""
    blob = json.dumps([S.RENDER_VERSION, scope, params, sig], sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:20]


def load_cache() -> dict[str, str]:
    """讀取 _render_cache.json 算圖雜湊對照表。"""
    try:
        with open(RT.CACHE_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cache(cache: dict[str, str]) -> None:
    """原子儲存算圖快取表。"""
    try:
        RT.ensure_dirs()
        RT.atomic_write_json(RT.CACHE_PATH, cache)
    except Exception:
        pass


def count_backups() -> int:
    """統計目前已安全保存的原始捷徑母本數量。"""
    count = 0
    try:
        for _dp, _dn, fns in os.walk(RT.BACKUP_DIR):
            count += sum(1 for fn in fns if fn.lower().endswith((".lnk", ".url")))
    except Exception:
        pass
    return count


# Lines 657-885: 批次套用 / 還原指揮中心、進度統計與退出自動還原
PROGRESS: dict[str, Any] = {"active": False, "scope": "", "phase": "", "done": 0, "total": 0}
_APPLY_LOCK = threading.Lock()


def _prog(**kw: Any) -> None:
    PROGRESS.update(kw)


def _needs_admin(item: Item) -> bool:
    return bool(S.SOURCE_NEEDS_ADMIN.get(item.source, False)) and not RT.is_admin()


def _only_sources(items: list[Item], sources: tuple[str, ...] | None) -> list[Item]:
    if not sources:
        return items
    allowed = set(sources)
    return [item for item in items if item.source in allowed]


def _resolve_existing_backup(item: Item) -> str | None:
    """依序檢查主要母本路徑與舊版平面備份路徑是否存在。"""
    if os.path.exists(item.primary):
        return item.primary
    if item.legacy and os.path.exists(item.legacy):
        return item.legacy
    return None


def _restore_legacy_windhawk_shortcuts(scope: str) -> dict[str, int]:
    """還原舊版 v1.x 曾直接改寫過的 taskbar / startmenu .lnk。"""
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
        src = _resolve_existing_backup(it)
        if not src:
            continue
        try:
            shutil.copy2(src, it.path)
            restored += 1
            RT._queue_shell_refresh(it.path)
        except Exception:
            pass
    if restored:
        RT.notify_shell()
    return {"restored": restored, "untouched": untouched}


def effective_active_scopes(wh_dir: str | None = None, *, include_experimental: bool = False) -> list[str]:
    """回傳本機目前可用的區域；未安裝 Windhawk 時自動退回純桌面核心，且預設不含實驗性獨立分頁。"""
    wh_ok = WB.is_windhawk_installed(wh_dir)
    return [
        sc
        for sc in S.ACTIVE_SCOPES
        if (not S.SPECS[sc].get("requires_windhawk") or wh_ok)
        and (include_experimental or not S.SPECS[sc].get("experimental"))
    ]


def restore_live_scopes_on_exit(wh_dir: str | None = None) -> list[dict[str, Any]]:
    """退出時自動停用所有已啟用的 Windhawk 即時引擎（工作列／開始功能表），不動桌面實體捷徑。"""
    if not WB.is_windhawk_installed(wh_dir):
        return []
    out: list[dict[str, Any]] = []
    for sc in S.ACTIVE_SCOPES:
        if sc not in WB.MOD_SPECS or S.is_scope_locked(sc):
            continue
        try:
            st = WB.get_status(wh_dir=wh_dir, base_dir=RT.BASE_DIR, scope=sc)
            enabled_in_status = st.get("enabled") or (
                st.get("ini_exists") and str(st.get("mod", {}).get("Disabled", "")).strip() == "0"
            )
            if enabled_in_status:
                out.append(WB.restore_windhawk_mod(sc, wh_dir=wh_dir))
            else:
                cfg_raw = WB.read_mod_config(WB.resolve_windhawk_paths(wh_dir, scope=sc), scope=sc)
                if cfg_raw.get("exists") and str(cfg_raw.get("mod", {}).get("Disabled", "1")).strip() == "0":
                    out.append(WB.restore_windhawk_mod(sc, wh_dir=wh_dir))
        except Exception:
            pass
    return out


def apply_scope(scope: str, params: dict[str, Any], notify: bool = True, sources: tuple[str, ...] | None = None) -> dict[str, Any]:
    """套用單一區域；工作列／開始功能表派送 Windhawk 引擎，桌面平行算圖並更新捷徑。"""
    if S.is_scope_locked(scope):
        res = S.new_result(scope, 0)
        res["locked"] = True
        return res
    if scope in WB.MOD_SPECS:
        clamped = S.clamp_params(scope, params)
        apply_p: dict[str, Any] = dict(clamped)
        if scope == "tray" and isinstance(params, dict) and "whitelist_apps" in params:
            apply_p["whitelist_apps"] = params["whitelist_apps"]
        _restore_legacy_windhawk_shortcuts(scope)
        wb_res = WB.apply_windhawk_mod(scope, apply_p, base_dir=RT.BASE_DIR)
        res = S.new_result(scope, 1)
        res["applied" if wb_res.get("ok") else "failed"] = 1
        if wb_res.get("needs_admin"):
            res["windhawk_needs_admin"] = 1
        if wb_res.get("windhawk_missing"):
            res["windhawk_missing"] = 1
        res["message"] = wb_res.get("message", "")
        res["windhawk"] = wb_res
        return res

    from liquid_glass import renderer as R

    RT.ensure_dirs()
    clamped = S.clamp_params(scope, params)
    items = _only_sources(enumerate_scope(scope), sources)
    res = S.new_result(scope, len(items))
    cache = load_cache()
    _prog(scope=scope, phase="分析", done=0, total=len(items))

    plan: list[tuple[Item, str]] = []
    jobs: list[tuple[Item, Any, str]] = []
    for idx, it in enumerate(items):
        _prog(done=idx)
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
            if not cur_ours and backup == it.primary and backup_is_stale(it.path, info, backup, sinfo):
                if refresh_backup(it):
                    res["backed_up"] += 1
                    sinfo = info
        key = make_key(scope, clamped, source_signature(backup, sinfo))
        cached = cache.get(it.ico_name) == key and os.path.exists(it.ico_path)
        url_wide_ok = not (RT.IS_WIN and it.ext == ".url" and not it.ico_path.isascii() and not _url_has_wide_section(it.path))
        points = cur_ours and RT._norm(info.icon_loc) == RT._norm(it.ico_path) and url_wide_ok
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

    rendered: dict[str, str] = {}
    if jobs:
        _prog(phase="渲染", done=0, total=len(jobs))
        workers = max(1, min(4, os.cpu_count() or 2))
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [(it, key, ex.submit(R.render_ico, scope, img, clamped, it.ico_path)) for it, img, key in jobs]
            for it, key, fut in futs:
                try:
                    fut.result()
                    rendered[it.ico_name] = key
                except Exception:
                    res["failed"] += 1
                done += 1
                _prog(done=done)
        jobs.clear()

    _prog(phase="寫入捷徑", done=0, total=len(plan))
    for idx, (it, key) in enumerate(plan):
        _prog(done=idx)
        if not os.path.exists(it.ico_path) or (it.ico_name not in rendered and cache.get(it.ico_name) != key):
            continue
        status = write_any_shortcut(it.path, it.ico_path)
        if status == "ok":
            res["applied"] += 1
            cache[it.ico_name] = key
            RT._queue_shell_refresh(it.path)
        elif status == "denied":
            res["denied"] += 1
            if _needs_admin(it):
                res["needs_admin"] += 1
            cache[it.ico_name] = key
        else:
            res["failed"] += 1
            cache[it.ico_name] = key
    save_cache(cache)
    _prog(done=len(plan), total=len(plan))
    mark_self_write()
    if notify and res["applied"]:
        RT.notify_shell()
    return res


def restore_scope(scope: str, notify: bool = True, sources: tuple[str, ...] | None = None) -> dict[str, Any]:
    """還原指定區域；Windhawk 區域停用模組，桌面則位元組級複製還原母本。"""
    if S.is_scope_locked(scope):
        res = S.new_result(scope, 0)
        res["locked"] = True
        return res
    if scope in WB.MOD_SPECS:
        wb_res = WB.restore_windhawk_mod(scope)
        legacy = _restore_legacy_windhawk_shortcuts(scope)
        res = S.new_result(scope, 1)
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
    res = S.new_result(scope, len(items))
    _prog(scope=scope, phase="還原", done=0, total=len(items))
    for idx, it in enumerate(items):
        _prog(done=idx)
        info = read_any_shortcut(it.path)
        if not is_ours(info.icon_loc):
            res["untouched"] += 1
            continue
        src = _resolve_existing_backup(it)
        if not src:
            res["no_backup"] += 1
            continue
        try:
            shutil.copy2(src, it.path)
            res["restored"] += 1
            RT._queue_shell_refresh(it.path)
        except PermissionError:
            res["denied"] += 1
            if _needs_admin(it):
                res["needs_admin"] += 1
        except Exception:
            res["failed"] += 1
        mark_self_write()
    if notify:
        RT.notify_shell()
    return res


def run_batch(
    kind: str,
    scopes: list[str] | tuple[str, ...],
    config: dict[str, Any],
    sources: tuple[str, ...] | None = None,
) -> list[dict[str, Any]] | None:
    """鎖定排程執行多區套用或還原；同批次僅觸發一次 Shell 全域刷新。"""
    if not _APPLY_LOCK.acquire(blocking=False):
        return None
    try:
        _prog(active=True)
        out: list[dict[str, Any]] = []
        for sc in scopes:
            if kind == "apply":
                sc_params = config.get(sc, {})
                if sc == "tray" and "tray_whitelist" in config:
                    sc_params = dict(sc_params)
                    sc_params["whitelist_apps"] = config["tray_whitelist"]
                out.append(apply_scope(sc, sc_params, notify=False, sources=sources))
            else:
                out.append(restore_scope(sc, notify=False, sources=sources))
        if any(r["scope"] not in WB.MOD_SPECS and (r["applied"] or r["restored"]) for r in out):
            RT.notify_shell()
        return out
    finally:
        _prog(active=False, phase="", done=0, total=0)
        _APPLY_LOCK.release()


def summarize(results: list[dict[str, Any]] | None, kind: str) -> str:
    """將批次統計結果整理成正體中文通知摘要。"""
    if results is None:
        return "另一項作業進行中，請稍候"
    parts: list[str] = []
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
            summary = f"{label}：更新 {r['applied']}"
            if r["unchanged"]:
                summary += f"、未變更略過 {r['unchanged']}"
        else:
            summary = f"{label}：還原 {r['restored']}"
            if r["untouched"]:
                summary += f"、本來就是原始 {r['untouched']}"
        for key, text in (("denied", "權限不足"), ("no_backup", "無母本略過"), ("no_icon", "取不到圖示"), ("failed", "失敗")):
            if r[key]:
                summary += f"、{text} {r[key]}"
        parts.append(summary)
    msg = "；".join(parts)
    need = sum(r.get("needs_admin", 0) for r in results)
    if need:
        msg += f"（其中 {need} 項位於公用位置，需以系統管理員權限重試）"
    elif any(r["denied"] for r in results):
        msg += "（權限不足的項目請以系統管理員身分執行）"
    return msg
