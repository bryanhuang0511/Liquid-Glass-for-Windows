r"""liquid_glass/windhawk_bridge.py -- Liquid Glass for Windows <-> Windhawk 本機即時引擎控制橋接。

Functionality:
1. 自動偵測本機 Windhawk 安裝（可攜版 C:\Windhawk、標準安裝 C:\Program Files\Windhawk、登錄檔或環境變數覆寫）與執行狀態。
2. 視需要自動呼叫 Windhawk 內建 clang++ 編譯專屬即時模組（local@liquid-glass-taskbar-icons / local@liquid-glass-startmenu-icons）。
3. 依 Windhawk 儲存模式（可攜版 INI 或標準安裝版 HKLM 登錄檔）讀寫模組設定，達成一鍵啟用、滑桿即時熱更新與秒速還原。
4. 讀取本機固定路徑 Telemetry JSON 與清理過期版本 DLL，不觸碰任何其他已安裝模組或磁碟圖示暫存。
"""
from __future__ import annotations

import configparser
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

try:
    import winreg
except ImportError:
    winreg = None

from liquid_glass import specs as S

MOD_ID = "local@liquid-glass-taskbar-icons"
MOD_VERSION = "1.0.0"
MOD_SOURCE_REL = ("windhawk", "liquid_glass_taskbar.wh.cpp")
STATUS_FILENAME = "lgic_windhawk_status.json"
DEFAULT_WINDHAWK_DIR = r"C:\Windhawk"

MOD_SPECS = {
    "taskbar": {
        "mod_id": MOD_ID,
        "version": MOD_VERSION,
        "source_rel": MOD_SOURCE_REL,
        "include": "explorer.exe",
        "status_filename": STATUS_FILENAME,
        "label": "工作列",
    },
    "startmenu": {
        "mod_id": "local@liquid-glass-startmenu-icons",
        "version": "1.0.0",
        "source_rel": ("windhawk", "liquid_glass_startmenu.wh.cpp"),
        "include": "StartMenuExperienceHost.exe",
        "status_filename": "lgic_windhawk_startmenu_status.json",
        "label": "開始功能表",
    },
    "tray": {
        "mod_id": "local@liquid-glass-tray-icons",
        "version": "1.0.0",
        "source_rel": ("windhawk", "liquid_glass_tray.wh.cpp"),
        "include": "explorer.exe",
        "status_filename": "lgic_windhawk_tray_status.json",
        "label": "通知列",
    },
}


def _spec(scope="taskbar"):
    if scope not in MOD_SPECS:
        raise ValueError(f"unknown windhawk scope: {scope!r}")
    return MOD_SPECS[scope]


def _default_resource_root():
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_source_cpp(base_dir=None, scope="taskbar"):
    rel = _spec(scope)["source_rel"]
    roots = []
    if base_dir:
        roots.append(base_dir)
    roots.append(_default_resource_root())
    roots.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for root in roots:
        cand = os.path.join(root, *rel)
        if os.path.exists(cand):
            return cand
    return None


def _is_access_denied(exc):
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (5, 1314)


def _clean_dir_candidate(raw):
    if not raw:
        return ""
    s = os.path.expandvars(str(raw).strip())
    if s.startswith('"'):
        end = s.find('"', 1)
        s = s[1:end] if end > 0 else s.strip('"')
    else:
        for ext in (".exe", ".EXE"):
            idx = s.find(ext)
            if idx != -1:
                s = s[:idx + len(ext)]
                break
        if "," in s:
            s = s.split(",", 1)[0].strip()
    if s.lower().endswith(".exe"):
        s = os.path.dirname(s)
    return os.path.normpath(s) if s else ""


def _registry_windhawk_dirs():
    if os.name != "nt" or winreg is None:
        return []
    found = []
    uninstall_roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, base_subkey in uninstall_roots:
        try:
            with winreg.OpenKey(hive, base_subkey) as parent:
                count, _, _ = winreg.QueryInfoKey(parent)
                for i in range(count):
                    try:
                        name = winreg.EnumKey(parent, i)
                        with winreg.OpenKey(parent, name) as sub:
                            disp = ""
                            try:
                                disp = str(winreg.QueryValueEx(sub, "DisplayName")[0] or "")
                            except OSError:
                                pass
                            if "windhawk" not in name.lower() and "windhawk" not in disp.lower():
                                continue
                            for val_name in ("InstallLocation", "DisplayIcon", "UninstallString"):
                                try:
                                    raw, _ = winreg.QueryValueEx(sub, val_name)
                                    cand = _clean_dir_candidate(raw)
                                    if cand and cand not in found:
                                        found.append(cand)
                                except OSError:
                                    continue
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def _candidate_windhawk_dirs():
    cands = [DEFAULT_WINDHAWK_DIR]
    cands.extend(_registry_windhawk_dirs())
    for env_key, sub in (
        ("ProgramFiles", "Windhawk"),
        ("ProgramW6432", "Windhawk"),
        ("ProgramFiles(x86)", "Windhawk"),
        ("LOCALAPPDATA", os.path.join("Programs", "Windhawk")),
        ("LOCALAPPDATA", "Windhawk"),
    ):
        base = os.environ.get(env_key)
        if base:
            cands.append(os.path.join(base, sub))
    seen, out = set(), []
    for c in cands:
        if not c:
            continue
        norm = os.path.normcase(os.path.normpath(c))
        if norm not in seen:
            seen.add(norm)
            out.append(os.path.normpath(c))
    return out


def find_windhawk_dir(wh_dir=None):
    if wh_dir:
        return wh_dir
    env_dir = os.environ.get("LGFW_WINDHAWK_DIR") or os.environ.get("LGIC_WINDHAWK_DIR")
    if env_dir:
        return env_dir
    for cand in _candidate_windhawk_dirs():
        if os.path.exists(os.path.join(cand, "windhawk.exe")):
            return cand
    return DEFAULT_WINDHAWK_DIR


def is_windhawk_installed(wh_dir=None):
    return os.path.exists(resolve_windhawk_paths(wh_dir)["exe"])


def _resolve_storage_path(root, raw_val, fallback_rel):
    val = str(raw_val or "").strip().strip('"')
    expanded = os.path.expandvars(val) if val else ""
    if not expanded:
        return os.path.normpath(os.path.join(root, fallback_rel))
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    return os.path.normpath(os.path.join(root, expanded))


def resolve_windhawk_paths(wh_dir=None, scope="taskbar"):
    mod_id = _spec(scope)["mod_id"]
    root = find_windhawk_dir(wh_dir)
    compiler_path = os.path.join(root, "Compiler")
    engine_path = os.path.join(root, "Engine")
    app_data_path = os.path.join(root, "AppData")
    portable_flag = "1"
    registry_key = ""

    wh_ini = os.path.join(root, "windhawk.ini")
    if os.path.exists(wh_ini):
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str
        try:
            cp.read(wh_ini, encoding="utf-8-sig")
            if cp.has_section("Storage"):
                st = cp["Storage"]
                portable_flag = str(st.get("Portable", "1")).strip()
                registry_key = str(st.get("RegistryKey", "")).strip()
                if st.get("CompilerPath"):
                    compiler_path = _resolve_storage_path(root, st["CompilerPath"], "Compiler")
                if st.get("EnginePath"):
                    engine_path = _resolve_storage_path(root, st["EnginePath"], "Engine")
                if st.get("AppDataPath"):
                    app_data_path = _resolve_storage_path(root, st["AppDataPath"], "AppData")
                elif portable_flag == "0":
                    app_data_path = _resolve_storage_path(root, r"%ProgramData%\Windhawk", "AppData")
        except Exception:
            pass

    if not os.path.exists(os.path.join(engine_path, "64", "windhawk.lib")) and os.path.isdir(engine_path):
        # 若 EnginePath 指向母目錄（如 Engine），自動搜尋底下版本資料夾
        for sub in sorted(os.listdir(engine_path), reverse=True):
            cand = os.path.join(engine_path, sub)
            if os.path.exists(os.path.join(cand, "64", "windhawk.lib")):
                engine_path = cand
                break

    storage_mode = "registry" if (portable_flag == "0" and registry_key) else "ini"
    mods_dir = os.path.join(app_data_path, "Engine", "Mods")
    return {
        "root": root,
        "exe": os.path.join(root, "windhawk.exe"),
        "compiler_clang": os.path.join(compiler_path, "bin", "clang++.exe"),
        "compiler_include": os.path.join(compiler_path, "include"),
        "windhawk_lib": os.path.join(engine_path, "64", "windhawk.lib"),
        "mods_dir": mods_dir,
        "mods64_dir": os.path.join(mods_dir, "64"),
        "mods_source_dir": os.path.join(app_data_path, "ModsSource"),
        "ini_path": os.path.join(mods_dir, f"{mod_id}.ini"),
        "storage_mode": storage_mode,
        "registry_key": registry_key if storage_mode == "registry" else "",
        "portable": (storage_mode == "ini"),
    }


def _source_tag(cpp_path):
    h = hashlib.sha256()
    with open(cpp_path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()[:8]


def _target_dll_name(short_hash, scope="taskbar"):
    spec = _spec(scope)
    num = str(int(short_hash, 16) % 900000 + 100000)
    return f"{spec['mod_id']}_{spec['version']}_{num}.dll"


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _is_process_running_win32(exe_name):
    """使用 Win32 Toolhelp 快照列舉行程（<1ms、零子行程建立），避免輪詢反覆呼叫 tasklist.exe。"""
    if os.name != "nt":
        return False
    kernel32 = ctypes.windll.kernel32
    snap = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    invalid = ctypes.c_void_p(-1).value
    if not snap or snap == invalid:
        return False
    target = exe_name.lower()
    try:
        pe = _PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            return False
        while True:
            if str(pe.szExeFile).lower() == target:
                return True
            if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                break
        return False
    finally:
        kernel32.CloseHandle(snap)


def is_windhawk_running():
    if os.name != "nt":
        return False
    try:
        return _is_process_running_win32("windhawk.exe")
    except Exception:
        return False


def ensure_windhawk_running(paths):
    if is_windhawk_running():
        return True
    exe = paths["exe"]
    if not os.path.exists(exe):
        return False
    try:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen([exe, "-tray-only"], cwd=paths["root"], creationflags=flags)
        return True
    except Exception:
        return False


def read_mod_ini(ini_path):
    if not os.path.exists(ini_path):
        return {"exists": False, "mod": {}, "settings": {}}
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str
    try:
        cp.read(ini_path, encoding="utf-8-sig")
    except Exception:
        return {"exists": True, "mod": {}, "settings": {}}
    return {
        "exists": True,
        "mod": dict(cp["Mod"]) if cp.has_section("Mod") else {},
        "settings": dict(cp["Settings"]) if cp.has_section("Settings") else {},
    }


def _mod_payload(spec, dll_filename, source_hash, params, enabled=True, scope="taskbar", whitelist=None):
    p = S.clamp_params(scope, params)
    now_ts = int(time.time())
    mod_fields = {
        "LibraryFileName": str(dll_filename),
        "Disabled": 0 if enabled else 1,
        "Include": str(spec["include"]),
        "Exclude": "",
        "Architecture": "x86-64",
        "Version": str(spec["version"]),
        "SourceHash": str(source_hash),
        "SettingsChangeTime": now_ts,
    }
    setting_fields = {
        "Enabled": 1 if enabled else 0,
        "Opacity": int(round(p["opacity"] * 100)),
        "Frost": int(round(p["frost"] * 100)),
        "Glow": int(round(p["glow"] * 100)),
        "DarkProtect": int(round(p["dark_protect"] * 100)),
    }
    if scope == "tray":
        raw_wl = whitelist
        if raw_wl is None and isinstance(params, dict) and "whitelist_apps" in params:
            raw_wl = params.get("whitelist_apps")
        setting_fields["WhitelistApps"] = S.normalize_tray_whitelist(raw_wl)
    return mod_fields, setting_fields


def write_mod_ini(ini_path, dll_filename, source_hash, params, enabled=True, scope="taskbar", whitelist=None):
    spec = _spec(scope)
    mod_fields, setting_fields = _mod_payload(
        spec, dll_filename, source_hash, params, enabled=enabled, scope=scope, whitelist=whitelist
    )
    os.makedirs(os.path.dirname(ini_path), exist_ok=True)
    lines = ["[Mod]"]
    lines.extend(f"{k}={v}" for k, v in mod_fields.items())
    lines.append("")
    lines.append("[Settings]")
    lines.extend(f"{k}={v}" for k, v in setting_fields.items())
    lines.append("")
    tmp = ini_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, ini_path)


def _parse_registry_root(reg_key):
    if winreg is None or not reg_key or "\\" not in reg_key:
        return None, ""
    head, tail = str(reg_key).split("\\", 1)
    roots = {
        "HKLM": winreg.HKEY_LOCAL_MACHINE,
        "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
        "HKCU": winreg.HKEY_CURRENT_USER,
        "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
    }
    hive = roots.get(head.strip().upper())
    return (hive, tail.strip("\\")) if hive and tail.strip("\\") else (None, "")


def _read_reg_values(hive, key_path):
    out = {}
    with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ) as k:
        _, nvals, _ = winreg.QueryInfoKey(k)
        for i in range(nvals):
            name, val, _ = winreg.EnumValue(k, i)
            out[name] = "" if val is None else str(val)
    return out


def _read_mod_registry(paths, scope="taskbar"):
    hive, base_sub = _parse_registry_root(paths.get("registry_key"))
    if hive is None:
        return {"exists": False, "mod": {}, "settings": {}}
    mod_id = _spec(scope)["mod_id"]
    mod_key = f"{base_sub}\\Engine\\Mods\\{mod_id}"
    try:
        mod_vals = _read_reg_values(hive, mod_key)
    except FileNotFoundError:
        return {"exists": False, "mod": {}, "settings": {}}
    except OSError:
        return {"exists": False, "mod": {}, "settings": {}}
    try:
        set_vals = _read_reg_values(hive, f"{mod_key}\\Settings")
    except OSError:
        set_vals = {}
    return {"exists": True, "mod": mod_vals, "settings": set_vals}


def _write_mod_registry(paths, dll_filename, source_hash, params, enabled=True, scope="taskbar", whitelist=None):
    hive, base_sub = _parse_registry_root(paths.get("registry_key"))
    if hive is None:
        raise OSError("invalid Windhawk RegistryKey")
    spec = _spec(scope)
    mod_fields, setting_fields = _mod_payload(
        spec, dll_filename, source_hash, params, enabled=enabled, scope=scope, whitelist=whitelist
    )
    mod_key = f"{base_sub}\\Engine\\Mods\\{spec['mod_id']}"
    set_key = f"{mod_key}\\Settings"
    access = winreg.KEY_WRITE | winreg.KEY_READ
    with winreg.CreateKeyEx(hive, mod_key, 0, access) as mk:
        for k, v in mod_fields.items():
            if isinstance(v, int):
                winreg.SetValueEx(mk, k, 0, winreg.REG_DWORD, v & 0xFFFFFFFF)
            else:
                winreg.SetValueEx(mk, k, 0, winreg.REG_SZ, v)
    with winreg.CreateKeyEx(hive, set_key, 0, access) as sk:
        for k, v in setting_fields.items():
            if isinstance(v, int):
                winreg.SetValueEx(sk, k, 0, winreg.REG_DWORD, int(v) & 0xFFFFFFFF)
            else:
                winreg.SetValueEx(sk, k, 0, winreg.REG_SZ, str(v))


def read_mod_config(paths, scope="taskbar"):
    if paths.get("storage_mode") == "registry":
        return _read_mod_registry(paths, scope=scope)
    return read_mod_ini(paths["ini_path"])


def write_mod_config(paths, dll_filename, source_hash, params, enabled=True, scope="taskbar", whitelist=None):
    if paths.get("storage_mode") == "registry":
        return _write_mod_registry(
            paths, dll_filename, source_hash, params, enabled=enabled, scope=scope, whitelist=whitelist
        )
    return write_mod_ini(
        paths["ini_path"], dll_filename, source_hash, params, enabled=enabled, scope=scope, whitelist=whitelist
    )


def compile_mod(paths, cpp_path, scope="taskbar"):
    spec = _spec(scope)
    clang = paths["compiler_clang"]
    wh_lib = paths["windhawk_lib"]
    inc_dir = paths["compiler_include"]
    if not (os.path.exists(clang) and os.path.exists(wh_lib) and os.path.exists(inc_dir)):
        return {"ok": False, "message": "找不到 Windhawk 編譯器（clang++.exe / windhawk.lib）"}

    tag = _source_tag(cpp_path)
    dll_name = _target_dll_name(tag, scope=scope)
    dll_path = os.path.join(paths["mods64_dir"], dll_name)
    try:
        os.makedirs(paths["mods64_dir"], exist_ok=True)
    except OSError as exc:
        if _is_access_denied(exc):
            return {"ok": False, "needs_admin": True,
                    "message": "寫入標準安裝版 Windhawk 需要系統管理員權限，請點選最上方「以系統管理員身分重新啟動」後再試一次"}
        return {"ok": False, "message": f"無法建立模組目錄：{exc}"}

    if os.path.exists(paths["mods_source_dir"]):
        try:
            shutil.copy2(cpp_path, os.path.join(paths["mods_source_dir"], f"{spec['mod_id']}.wh.cpp"))
        except Exception:
            pass

    if os.path.exists(dll_path):
        return {"ok": True, "compiled": False, "dll_name": dll_name, "dll_path": dll_path, "source_hash": tag}

    tmp_dll = dll_path + ".tmp.dll"
    cmd = [
        clang,
        "-std=c++23",
        "-O2",
        "-shared",
        "-DUNICODE",
        "-D_UNICODE",
        "-DWINVER=0x0A00",
        "-D_WIN32_WINNT=0x0A00",
        "-D_WIN32_IE=0x0A00",
        "-DNTDDI_VERSION=0x0A000008",
        "-D__USE_MINGW_ANSI_STDIO=0",
        "-DWH_MOD",
        f'-DWH_MOD_ID=L"{spec["mod_id"]}"',
        f'-DWH_MOD_VERSION=L"{spec["version"]}"',
        f"-I{inc_dir}",
        "-include",
        "windhawk_api.h",
        "-target",
        "x86_64-w64-mingw32",
        "-Wl,--export-all-symbols",
        cpp_path,
        wh_lib,
        "-lole32",
        "-loleaut32",
        "-lruntimeobject",
        "-lshcore",
        "-lwindowscodecs",
        "-o",
        tmp_dll,
    ]
    try:
        res = subprocess.run(
            cmd,
            cwd=os.path.dirname(clang),
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return {"ok": False, "message": f"編譯模組時發生異常：{exc}"}

    if res.returncode != 0 or not os.path.exists(tmp_dll):
        err = (res.stderr or res.stdout or "").strip()[:300]
        return {"ok": False, "message": f"Windhawk 模組編譯失敗：{err}"}

    try:
        os.replace(tmp_dll, dll_path)
    except OSError as exc:
        if _is_access_denied(exc):
            return {"ok": False, "needs_admin": True,
                    "message": "寫入標準安裝版 Windhawk 需要系統管理員權限，請點選最上方「以系統管理員身分重新啟動」後再試一次"}
        return {"ok": False, "message": f"寫入模組 DLL 失敗：{exc}"}
    return {"ok": True, "compiled": True, "dll_name": dll_name, "dll_path": dll_path, "source_hash": tag}


def read_telemetry_status(scope="taskbar"):
    path = os.path.join(tempfile.gettempdir(), _spec(scope)["status_filename"])
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def get_status(wh_dir=None, base_dir=None, scope="taskbar"):
    paths = resolve_windhawk_paths(wh_dir, scope=scope)
    ini = read_mod_config(paths, scope=scope)
    dll_name = ini["mod"].get("LibraryFileName", "")
    dll_exists = bool(dll_name and os.path.exists(os.path.join(paths["mods64_dir"], dll_name)))
    disabled_flag = ini["mod"].get("Disabled", "1")
    mod_enabled = ini["exists"] and str(disabled_flag).strip() == "0"
    telem = read_telemetry_status(scope=scope)
    return {
        "scope": scope,
        "windhawk_installed": os.path.exists(paths["exe"]),
        "windhawk_dir": paths["root"],
        "storage_mode": paths["storage_mode"],
        "windhawk_running": is_windhawk_running(),
        "compiler_available": os.path.exists(paths["compiler_clang"]),
        "mod_source_available": find_source_cpp(base_dir, scope=scope) is not None,
        "ini_exists": ini["exists"],
        "dll_filename": dll_name,
        "dll_exists": dll_exists,
        "enabled": bool(mod_enabled and (telem.get("active", True) if telem else True)),
        "telemetry": telem,
        "ini_settings": ini["settings"],
    }


def _cleanup_stale_mod_dlls(paths, scope, keep_dll):
    """切換啟用新版 DLL 後，嘗試清理同一個 mod_id 遺留的舊版 .dll（若仍被行程鎖定則安全略過）。"""
    spec = _spec(scope)
    prefix = f"{spec['mod_id']}_"
    mods64 = paths.get("mods64_dir", "")
    if not mods64 or not os.path.isdir(mods64):
        return 0
    removed = 0
    for fn in os.listdir(mods64):
        if fn.startswith(prefix) and fn.lower().endswith(".dll") and fn != keep_dll:
            try:
                os.remove(os.path.join(mods64, fn))
                removed += 1
            except OSError:
                pass
    return removed


def apply_windhawk_mod(scope, params, wh_dir=None, base_dir=None, whitelist=None):
    spec = _spec(scope)
    paths = resolve_windhawk_paths(wh_dir, scope=scope)
    if not os.path.exists(paths["exe"]):
        return {"ok": False, "windhawk_missing": True, "message": f"未偵測到 Windhawk（{paths['exe']}）；{spec['label']}屬於選用擴充功能"}
    cpp = find_source_cpp(base_dir, scope=scope)
    if not cpp:
        return {"ok": False, "message": f"找不到{spec['label']} Windhawk C++ 模組原始碼"}

    try:
        comp = compile_mod(paths, cpp, scope=scope)
    except TypeError:
        comp = compile_mod(paths, cpp)
    if not comp["ok"]:
        return comp

    prev_ini = read_mod_config(paths, scope=scope)
    prev_enabled = prev_ini["exists"] and str(prev_ini["mod"].get("Disabled", "1")).strip() == "0"
    prev_dll = prev_ini["mod"].get("LibraryFileName", "")
    wl = whitelist
    if wl is None and scope == "tray" and not (isinstance(params, dict) and "whitelist_apps" in params):
        if prev_ini["exists"] and "WhitelistApps" in prev_ini["settings"]:
            wl = prev_ini["settings"]["WhitelistApps"]
    try:
        if prev_enabled and prev_dll and prev_dll != comp["dll_name"]:
            # 若先前版本的 DLL 仍載入中，先短暫停用卸載再載入新版 DLL
            write_mod_config(
                paths, prev_dll, prev_ini["mod"].get("SourceHash", ""), params,
                enabled=False, scope=scope, whitelist=wl,
            )
            time.sleep(0.5)

        write_mod_config(
            paths, comp["dll_name"], comp["source_hash"], params,
            enabled=True, scope=scope, whitelist=wl,
        )
    except OSError as exc:
        if _is_access_denied(exc):
            return {"ok": False, "needs_admin": True,
                    "message": "寫入標準安裝版 Windhawk 需要系統管理員權限，請點選最上方「以系統管理員身分重新啟動」後再試一次"}
        return {"ok": False, "message": f"寫入 Windhawk 設定失敗：{exc}"}
    _cleanup_stale_mod_dlls(paths, scope, comp["dll_name"])
    ensure_windhawk_running(paths)
    return {
        "ok": True,
        "compiled": comp["compiled"],
        "dll_filename": comp["dll_name"],
        "message": f"{spec['label']}即時 Liquid Glass 已啟用（純記憶體運算，無磁碟暫存；Windhawk 將在數秒內套用）",
    }


def apply_taskbar(params, wh_dir=None, base_dir=None):
    return apply_windhawk_mod("taskbar", params, wh_dir=wh_dir, base_dir=base_dir)


def apply_startmenu(params, wh_dir=None, base_dir=None):
    return apply_windhawk_mod("startmenu", params, wh_dir=wh_dir, base_dir=base_dir)


def apply_tray(params, wh_dir=None, base_dir=None, whitelist=None):
    return apply_windhawk_mod("tray", params, wh_dir=wh_dir, base_dir=base_dir, whitelist=whitelist)


def update_settings_if_active(params, wh_dir=None, scope="taskbar", whitelist=None):
    paths = resolve_windhawk_paths(wh_dir, scope=scope)
    ini = read_mod_config(paths, scope=scope)
    if not ini["exists"] or str(ini["mod"].get("Disabled", "1")).strip() != "0":
        return {"ok": True, "synced": False}
    dll_name = ini["mod"].get("LibraryFileName", "")
    if not dll_name:
        return {"ok": False, "synced": False}
    src_hash = ini["mod"].get("SourceHash", "")
    wl = whitelist
    if wl is None and scope == "tray" and not (isinstance(params, dict) and "whitelist_apps" in params):
        if "WhitelistApps" in ini["settings"]:
            wl = ini["settings"]["WhitelistApps"]
    try:
        write_mod_config(paths, dll_name, src_hash, params, enabled=True, scope=scope, whitelist=wl)
    except OSError as exc:
        if _is_access_denied(exc):
            return {"ok": False, "synced": False, "needs_admin": True}
        return {"ok": False, "synced": False}
    return {"ok": True, "synced": True}


def restore_windhawk_mod(scope, params=None, wh_dir=None):
    spec = _spec(scope)
    paths = resolve_windhawk_paths(wh_dir, scope=scope)
    ini = read_mod_config(paths, scope=scope)
    if not ini["exists"]:
        return {"ok": True, "message": f"{spec['label']}模組尚未啟用，原生圖示維持不變"}
    dll_name = ini["mod"].get("LibraryFileName", "")
    src_hash = ini["mod"].get("SourceHash", "")
    wl = ini["settings"].get("WhitelistApps") if (scope == "tray" and "WhitelistApps" in ini["settings"]) else None
    try:
        write_mod_config(
            paths,
            dll_name,
            src_hash,
            params or S.default_params(scope),
            enabled=False,
            scope=scope,
            whitelist=wl,
        )
    except OSError as exc:
        if _is_access_denied(exc):
            return {"ok": False, "needs_admin": True,
                    "message": "停用標準安裝版 Windhawk 模組需要系統管理員權限，請點選最上方「以系統管理員身分重新啟動」後再試一次"}
        return {"ok": False, "message": f"停用 Windhawk 模組失敗：{exc}"}
    return {"ok": True, "message": f"已停用{spec['label']} Liquid Glass，{spec['label']}圖示即時還原原生外觀"}


def restore_taskbar(params=None, wh_dir=None):
    return restore_windhawk_mod("taskbar", params=params, wh_dir=wh_dir)


def restore_startmenu(params=None, wh_dir=None):
    return restore_windhawk_mod("startmenu", params=params, wh_dir=wh_dir)


def restore_tray(params=None, wh_dir=None):
    return restore_windhawk_mod("tray", params=params, wh_dir=wh_dir)
