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
