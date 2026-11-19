"""tests/test_windhawk_bridge.py -- Windhawk 自動偵測、雙儲存模式、遙測、過期 DLL 清理與 Win32 行程偵測。"""
from __future__ import annotations

import json
import os
import re
import sys

from liquid_glass import specs as S
from liquid_glass import windhawk_bridge as WB


def test_windhawk_bridge_ini_settings_restore_and_status(env):
    for sc, target_proc, telem_name in (
        ("taskbar", "explorer.exe", "lgic_windhawk_status.json"),
        ("startmenu", "StartMenuExperienceHost.exe", "lgic_windhawk_startmenu_status.json"),
        ("tray", "explorer.exe", "lgic_windhawk_tray_status.json"),
    ):
        r1 = WB.apply_windhawk_mod(sc, {"opacity": 0.85, "frost": 1.15, "glow": 1.40, "dark_protect": 0.90})
        assert r1["ok"] is True and r1["compiled"] is True
        ini = WB.read_mod_ini(WB.resolve_windhawk_paths(scope=sc)["ini_path"])
        assert ini["mod"]["Disabled"] == "0"
        assert ini["mod"]["Include"] == target_proc
        expected = {"Enabled": "1", "Opacity": "85", "Frost": "115", "Glow": "140", "DarkProtect": "90"}
        if sc == "tray":
            expected["WhitelistApps"] = S.DEFAULT_TRAY_WHITELIST
        assert ini["settings"] == expected

        sync1 = WB.update_settings_if_active({"opacity": 1.0, "frost": 0.95, "glow": 1.25, "dark_protect": 1.1}, scope=sc)
        assert sync1 == {"ok": True, "synced": True}
        assert WB.read_mod_ini(WB.resolve_windhawk_paths(scope=sc)["ini_path"])["settings"]["Glow"] == "125"

        telem_path = os.path.join(env.temp_dir, telem_name)
        with open(telem_path, "w", encoding="utf-8") as tf:
            json.dump({"active": True, "tracked_buttons": 12, "cache_entries": 9, "cache_bytes": 40960}, tf)
        st = WB.get_status(scope=sc)
        assert st["enabled"] is True and st["telemetry"]["tracked_buttons"] == 12

        r2 = WB.restore_windhawk_mod(sc)
        assert r2["ok"] is True
        assert WB.read_mod_ini(WB.resolve_windhawk_paths(scope=sc)["ini_path"])["mod"]["Disabled"] == "1"
        assert WB.update_settings_if_active({"opacity": 0.5}, scope=sc) == {"ok": True, "synced": False}
    assert os.listdir(env.RT.ICON_VAULT_DIR) == []


def test_windhawk_auto_detection_and_standard_storage(env, tmp_path, monkeypatch):
    monkeypatch.delenv("LGFW_WINDHAWK_DIR", raising=False)
    monkeypatch.delenv("LGIC_WINDHAWK_DIR", raising=False)
    monkeypatch.setattr(WB, "DEFAULT_WINDHAWK_DIR", str(tmp_path / "no_portable"))
    monkeypatch.setattr(WB, "_registry_windhawk_dirs", lambda: [])

    pf = tmp_path / "ProgramFiles"
    std_wh = pf / "Windhawk"
    std_pdata = tmp_path / "StdProgramData"
    (std_wh / "Compiler" / "bin").mkdir(parents=True)
    (std_wh / "Compiler" / "include").mkdir(parents=True)
    (std_wh / "Engine" / "1.7.3" / "64").mkdir(parents=True)
    (std_wh / "windhawk.exe").write_bytes(b"WH")
    (std_wh / "Compiler" / "bin" / "clang++.exe").write_bytes(b"CC")
    (std_wh / "Engine" / "1.7.3" / "64" / "windhawk.lib").write_bytes(b"LIB")
    (std_wh / "windhawk.ini").write_text(
        "[Storage]\nPortable=0\nCompilerPath=Compiler\nEnginePath=Engine\\1.7.3\n"
        "AppDataPath=%ProgramData%\\Windhawk\nRegistryKey=HKLM\\SOFTWARE\\Windhawk\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ProgramFiles", str(pf))
    monkeypatch.setenv("ProgramW6432", str(pf))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "PF86"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalApp"))
    monkeypatch.setenv("ProgramData", str(std_pdata))

    assert WB.find_windhawk_dir() == os.path.normpath(str(std_wh))
    assert WB.is_windhawk_installed() is True
    paths = WB.resolve_windhawk_paths(scope="taskbar")
    assert paths["storage_mode"] == "registry"
    assert paths["registry_key"] == r"HKLM\SOFTWARE\Windhawk"
    assert os.path.normpath(paths["mods64_dir"]) == os.path.normpath(str(std_pdata / "Windhawk" / "Engine" / "Mods" / "64"))

    store = {}
    deny_write = {"flag": False}

    class FakeReg:
        HKEY_LOCAL_MACHINE = object()
        HKEY_CURRENT_USER = object()
        KEY_READ = 1
        KEY_WRITE = 2
        REG_SZ = 1
        REG_DWORD = 4

        class _Handle:
            def __init__(self, hive, sub):
                self.hive = hive
                self.sub = sub

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        @classmethod
        def OpenKey(cls, hive, sub, *_a):
            if (hive, sub) not in store:
                raise FileNotFoundError(sub)
            return cls._Handle(hive, sub)

        @classmethod
        def CreateKeyEx(cls, hive, sub, *_a):
            if deny_write["flag"]:
                raise PermissionError(5, "Access denied")
            store.setdefault((hive, sub), {})
            return cls._Handle(hive, sub)

        @classmethod
        def QueryInfoKey(cls, h):
            return (0, len(store[(h.hive, h.sub)]), 0)

        @classmethod
        def EnumValue(cls, h, idx):
            k = list(store[(h.hive, h.sub)].keys())[idx]
            return (k, store[(h.hive, h.sub)][k], 1)

        @classmethod
        def SetValueEx(cls, h, name, _res, _typ, val):
            if deny_write["flag"]:
                raise PermissionError(5, "Access denied")
            store[(h.hive, h.sub)][name] = val

    monkeypatch.setattr(WB, "winreg", FakeReg)
    r_on = WB.apply_windhawk_mod("startmenu", {"opacity": 0.9, "frost": 1.1, "glow": 1.3, "dark_protect": 0.8})
    assert r_on["ok"] is True
    st = WB.get_status(scope="startmenu")
    assert st["storage_mode"] == "registry" and st["enabled"] is True and st["ini_settings"]["Glow"] == "130"
    assert WB.update_settings_if_active({"opacity": 0.75}, scope="startmenu") == {"ok": True, "synced": True}
    assert WB.get_status(scope="startmenu")["ini_settings"]["Opacity"] == "75"
    r_off = WB.restore_windhawk_mod("startmenu")
    assert r_off["ok"] is True and WB.get_status(scope="startmenu")["enabled"] is False

    deny_write["flag"] = True
    r_denied = WB.apply_windhawk_mod("startmenu", S.default_params("startmenu"))
    assert r_denied["ok"] is False and r_denied.get("needs_admin") is True

    deny_write["flag"] = False
    r_tray = WB.apply_windhawk_mod(
        "tray",
        {"opacity": 1.0, "frost": 1.0, "glow": 1.0, "dark_protect": 1.0},
        whitelist="HWiNFO64.exe;AIDA64.exe",
    )
    assert r_tray["ok"] is True
    assert WB.get_status(scope="tray")["ini_settings"]["WhitelistApps"] == "HWiNFO64.exe;AIDA64.exe"
    assert WB.update_settings_if_active({"opacity": 0.8}, scope="tray") == {"ok": True, "synced": True}
    assert WB.get_status(scope="tray")["ini_settings"]["WhitelistApps"] == "HWiNFO64.exe;AIDA64.exe"
    assert WB.get_status(scope="tray")["ini_settings"]["Opacity"] == "80"


def test_stale_mod_dll_cleanup_and_win32_process_probe(env, monkeypatch):
    paths = WB.resolve_windhawk_paths(scope="taskbar")
    mods64 = paths["mods64_dir"]
    stale = os.path.join(mods64, "local@liquid-glass-taskbar-icons_1.0.0_000001.dll")
    keep = os.path.join(mods64, "local@liquid-glass-taskbar-icons_1.0.0_123456.dll")
    other = os.path.join(mods64, "taskbar-on-top_1.3.1_321078.dll")
    for p in (stale, keep, other):
        with open(p, "wb") as handle:
            handle.write(b"MZ")
    removed = WB._cleanup_stale_mod_dlls(paths, "taskbar", "local@liquid-glass-taskbar-icons_1.0.0_123456.dll")
    assert removed == 1
    assert not os.path.exists(stale) and os.path.exists(keep) and os.path.exists(other)

    if os.name == "nt":
        cur_exe = os.path.basename(sys.executable)
        assert WB._is_process_running_win32(cur_exe) is True
        assert WB._is_process_running_win32("__nonexistent_lgfw_proc__.exe") is False
        assert isinstance(WB.is_windhawk_running(), bool)


def _parse_windhawk_mod_header(cpp_text: str) -> dict[str, str]:
    match = re.search(
        r"^//[ \t]+==WindhawkMod==[ \t]*$\r?\n(.*?)\r?\n^//[ \t]+==/WindhawkMod==[ \t]*$",
        cpp_text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "missing // ==WindhawkMod== metadata block"
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        item = re.match(r"^//[ \t]+@([a-zA-Z]+)[ \t]+(.*)$", line)
        assert item is not None, f"unparseable Windhawk metadata line: {line!r}"
        meta[item.group(1)] = item.group(2).strip()
    return meta


def test_windhawk_source_metadata_matches_windhawk_validator():
    for scope, spec in WB.MOD_SPECS.items():
        cpp_path = WB.find_source_cpp(scope=scope)
        assert cpp_path is not None and os.path.exists(cpp_path)
        with open(cpp_path, "r", encoding="utf-8") as handle:
            meta = _parse_windhawk_mod_header(handle.read())
        mod_id = meta.get("id", "")
        assert re.fullmatch(r"[0-9a-z-]+", mod_id), f"{scope}: invalid @id {mod_id!r}"
        assert mod_id == spec["mod_id"].removeprefix("local@")
        assert meta.get("name") and meta.get("description") and meta.get("author")
        assert meta.get("version") == spec["version"]
        assert meta.get("include") == spec["include"]
        assert meta.get("architecture") in {"x86", "x86-64", "amd64", "arm64"}


def test_windhawk_prebuilt_fallback_and_custom_dir(env, tmp_path, monkeypatch):
    # Restore real compile_mod to test compiler-less Windhawk 2.0 fallback & permission handling.
    import importlib
    real_compile = importlib.reload(WB).compile_mod
    monkeypatch.setattr(WB, "compile_mod", real_compile)
    monkeypatch.setattr(WB, "is_windhawk_running", lambda: True)
    monkeypatch.setattr(WB, "ensure_windhawk_running", lambda paths: True)

    # Create a compiler-less Windhawk 2.0 directory (only windhawk.exe, no Compiler/clang++.exe)
    wh2 = tmp_path / "CustomDriveWindhawk"
    (wh2 / "AppData" / "Engine" / "Mods" / "64").mkdir(parents=True)
    (wh2 / "windhawk.exe").write_bytes(b"WH20")

    monkeypatch.delenv("LGFW_WINDHAWK_DIR", raising=False)
    monkeypatch.delenv("LGIC_WINDHAWK_DIR", raising=False)
    WB.set_custom_windhawk_dir(str(wh2 / "windhawk.exe"))
    assert WB.get_custom_windhawk_dir() == os.path.normpath(str(wh2))
    assert WB.find_windhawk_dir() == os.path.normpath(str(wh2))

    # Stage fake prebuilt DLL + runtime shims under env.RT.BASE_DIR / windhawk / prebuilt
    prebuilt_dir = tmp_path / "app" / "windhawk" / "prebuilt"
    prebuilt_dir.mkdir(parents=True, exist_ok=True)
    for sc, spec in WB.MOD_SPECS.items():
        (prebuilt_dir / f"{spec['mod_id']}.dll").write_bytes(b"MZ_PREBUILT")
    for _src, dst in WB.RUNTIME_LIB_PAIRS:
        (prebuilt_dir / dst).write_bytes(b"MZ_RUNTIME")

    st_before = WB.get_status(base_dir=str(tmp_path / "app"), scope="taskbar")
    assert st_before["compiler_available"] is False
    assert st_before["prebuilt_available"] is True

    res = WB.apply_windhawk_mod("taskbar", S.default_params("taskbar"), base_dir=str(tmp_path / "app"))
    assert res["ok"] is True and res["compiled"] is True
    mods64 = wh2 / "AppData" / "Engine" / "Mods" / "64"
    assert (mods64 / res["dll_filename"]).exists()
    assert (mods64 / "libc++.whl").exists()
    assert (mods64 / "libunwind.whl").exists()
    assert (mods64 / "windhawk-mod-shim.dll").exists()

    # Verify access-denied during atomic copy into an existing protected mods64 folder returns needs_admin=True
    def deny_copy(_src, _dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(WB, "_copy_file_atomic", deny_copy)
    (mods64 / res["dll_filename"]).unlink()
    denied = WB.apply_windhawk_mod("taskbar", S.default_params("taskbar"), base_dir=str(tmp_path / "app"))
    assert denied["ok"] is False and denied.get("needs_admin") is True
    WB.set_custom_windhawk_dir("")


def test_utf16_windhawk_ini_misplaced_cleanup_and_tasks_dialog(env, tmp_path, monkeypatch):
    std_wh = tmp_path / "StdWhUtf16"
    pdata = tmp_path / "StdPdataUtf16"
    (std_wh / "Compiler" / "bin").mkdir(parents=True)
    (std_wh / "Compiler" / "include").mkdir(parents=True)
    (std_wh / "Engine" / "1.7.3" / "64").mkdir(parents=True)
    (std_wh / "windhawk.exe").write_bytes(b"WH")
    (std_wh / "Compiler" / "bin" / "clang++.exe").write_bytes(b"CC")
    (std_wh / "Engine" / "1.7.3" / "64" / "windhawk.lib").write_bytes(b"LIB")

    # Official Windhawk installer writes windhawk.ini in UTF-16 LE with BOM.
    ini_text = (
        "[Storage]\r\nPortable=0\r\nCompilerPath=Compiler\r\n"
        "EnginePath=Engine\\1.7.3\r\nAppDataPath=%ProgramData%\\Windhawk\r\n"
        "RegistryKey=HKLM\\SOFTWARE\\Windhawk\r\n"
    )
    (std_wh / "windhawk.ini").write_bytes(ini_text.encode("utf-16"))
    monkeypatch.setenv("ProgramData", str(pdata))
    monkeypatch.setenv("LGFW_WINDHAWK_DIR", str(std_wh))
    monkeypatch.setenv("LGIC_WINDHAWK_DIR", str(std_wh))

    paths = WB.resolve_windhawk_paths(scope="taskbar")
    assert paths["storage_mode"] == "registry"
    assert paths["registry_key"] == r"HKLM\SOFTWARE\Windhawk"
    assert os.path.normpath(paths["mods_dir"]) == os.path.normpath(str(pdata / "Windhawk" / "Engine" / "Mods"))

    # Verify misplaced local@liquid-glass-* files in <WindhawkRoot>\AppData\Engine\Mods are cleaned up
    # while unrelated files stay untouched.
    mis_mods = std_wh / "AppData" / "Engine" / "Mods"
    mis_64 = mis_mods / "64"
    mis_64.mkdir(parents=True)
    mis_ini = mis_mods / "local@liquid-glass-taskbar-icons.ini"
    mis_dll = mis_64 / "local@liquid-glass-taskbar-icons_1.0.0_111111.dll"
    unrelated = mis_mods / "other-mod.ini"
    mis_ini.write_text("bad", encoding="utf-8")
    mis_dll.write_bytes(b"BAD")
    unrelated.write_text("keep", encoding="utf-8")
    assert WB._cleanup_misplaced_standard_files(paths) == 2
    assert not mis_ini.exists() and not mis_dll.exists() and unrelated.exists()

    # Verify Portable settings.ini ModTasksDialogDelay toggle + initializing_symbols detection
    port_wh = tmp_path / "PortWh"
    (port_wh / "AppData" / "Engine" / "Mods" / "64").mkdir(parents=True)
    (port_wh / "windhawk.exe").write_bytes(b"WH")
    (port_wh / "windhawk.ini").write_text("[Storage]\nPortable=1\nAppDataPath=AppData\n", encoding="utf-8")
    monkeypatch.setenv("LGFW_WINDHAWK_DIR", str(port_wh))
    monkeypatch.setenv("LGIC_WINDHAWK_DIR", str(port_wh))

    assert WB.get_tasks_dialog_hidden()["hidden"] is False
    set_on = WB.set_tasks_dialog_hidden(True)
    assert set_on["ok"] is True and set_on["hidden"] is True and set_on["delay_ms"] == 3600000
    assert WB.get_tasks_dialog_hidden()["hidden"] is True
    set_off = WB.set_tasks_dialog_hidden(False)
    assert set_off["ok"] is True and set_off["hidden"] is False and set_off["delay_ms"] == 2000

    p_paths = WB.resolve_windhawk_paths(scope="taskbar")
    (port_wh / "AppData" / "Engine" / "Mods" / "64" / "local@liquid-glass-taskbar-icons_1.0.0_123456.dll").write_bytes(b"MZ")
    WB.write_mod_config(p_paths, "local@liquid-glass-taskbar-icons_1.0.0_123456.dll", "abcd", S.default_params("taskbar"), enabled=True)
    monkeypatch.setattr(WB, "is_windhawk_running", lambda: True)
    st_sym = WB.get_status(scope="taskbar")
    assert st_sym["initializing_symbols"] is True
