"""tests/conftest.py -- 測試共用 Fixtures（模擬沙盒目錄、假 Windhawk 環境與假捷徑系統）。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import Any

import pytest
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from liquid_glass import desktop_watcher as DW  # noqa: E402
from liquid_glass import renderer as R  # noqa: E402
from liquid_glass import runtime as RT  # noqa: E402
from liquid_glass import shortcut_manager as SM  # noqa: E402
from liquid_glass import specs as S  # noqa: E402
from liquid_glass import tray_controller as TC  # noqa: E402
from liquid_glass import web_api as WA  # noqa: E402
from liquid_glass import windhawk_bridge as WB  # noqa: E402
import main as APP_MAIN  # noqa: E402


def solid_disc() -> Image.Image:
    """建立 256x256 飽和藍色實心圓測試圖示。"""
    im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse([8, 8, 247, 247], fill=(66, 133, 244, 255))
    return im


def white_disc() -> Image.Image:
    """建立 256x256 純白實心圓測試圖示。"""
    im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse([8, 8, 247, 247], fill=(255, 255, 255, 255))
    return im


def unlock_legacy_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    """確保測試情境中三區全面開放。"""
    monkeypatch.setattr(S, "LOCKED_SCOPES", ())
    monkeypatch.setattr(S, "ACTIVE_SCOPES", ("desktop", "startmenu", "taskbar"))


def populate(e: Any) -> None:
    """於模擬桌面／工作列／開始功能表建立標準測試捷徑樣本。"""
    desk = os.path.join(e.home, "Desktop")
    e.mk(os.path.join(desk, "Chrome.lnk"))
    e.mk(os.path.join(desk, "Notes.lnk"))
    e.mk(os.path.join(desk, RT.SELF_SHORTCUT_NAME))
    e.mk(os.path.join(e.tb, "Chrome.lnk"))
    e.mk(os.path.join(e.tb, "Files.lnk"))
    e.mk(os.path.join(e.sm, "Tools", "Editor.lnk"))
    e.mk(os.path.join(e.sm, "Games", "Editor.lnk"))
    e.mk(os.path.join(e.sm, "Tools", "Uninstall Editor.lnk"))
    e.mk(os.path.join(e.sm, "Tools", "解除安裝 Foo.lnk"))
    e.mk(os.path.join(e.smc, "Office.lnk"))


@pytest.fixture()
def env(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """提供完全隔離於真實磁碟與 Windhawk 的端到端測試沙盒。"""
    base = tmp_path / "app"
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    pdata = tmp_path / "ProgramData"
    wh = tmp_path / "windhawk"
    temp_dir = tmp_path / "temp"
    for directory in (
        base,
        home / "Desktop",
        wh / "Compiler" / "bin",
        wh / "Compiler" / "include",
        wh / "Engine" / "64",
        wh / "AppData" / "Engine" / "Mods" / "64",
        temp_dir,
        appdata / "Microsoft/Internet Explorer/Quick Launch/User Pinned/TaskBar",
        appdata / "Microsoft/Windows/Start Menu/Programs",
        pdata / "Microsoft/Windows/Start Menu/Programs",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    (wh / "windhawk.exe").write_bytes(b"WH")
    (wh / "Compiler" / "bin" / "clang++.exe").write_bytes(b"CC")
    (wh / "Engine" / "64" / "windhawk.lib").write_bytes(b"LIB")

    monkeypatch.setenv("LGFW_BASE_DIR", str(base))
    monkeypatch.setenv("LGIC_BASE_DIR", str(base))
    monkeypatch.setenv("LGFW_WINDHAWK_DIR", str(wh))
    monkeypatch.setenv("LGIC_WINDHAWK_DIR", str(wh))
    monkeypatch.setenv("TEMP", str(temp_dir))
    monkeypatch.setenv("TMP", str(temp_dir))
    monkeypatch.setattr(tempfile, "tempdir", str(temp_dir))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("ProgramData", str(pdata))

    monkeypatch.setattr(RT, "BASE_DIR", str(base))
    monkeypatch.setattr(RT, "ICON_VAULT_DIR", str(base / "IconVault"))
    monkeypatch.setattr(RT, "BACKUP_DIR", str(base / "Original_Shortcuts_Backup"))
    monkeypatch.setattr(RT, "CONFIG_PATH", str(base / "config.json"))
    monkeypatch.setattr(RT, "CACHE_PATH", str(base / "IconVault" / "_render_cache.json"))
    monkeypatch.setattr(RT, "ADMIN_RESULT_PATH", str(base / "admin_last_result.json"))
    monkeypatch.setattr(RT, "PROFILE_DIR", str(base / ".webview_profile"))
    monkeypatch.setattr(RT, "is_admin", lambda: False)

    SM._SELF_WRITE["t"] = None
    RT._PENDING_SHELL_PATHS.clear()
    SM.PROGRESS.update({"active": False, "scope": "", "phase": "", "done": 0, "total": 0})
    RT.ensure_dirs()

    calls = {"render": 0, "write": 0, "extract": 0, "wh_apply": 0, "wh_restore": 0, "notify": 0}
    real_render = R.render_ico

    def fake_compile(paths: dict[str, Any], cpp_path: str, scope: str = "taskbar") -> dict[str, Any]:
        calls["wh_compile"] = calls.get("wh_compile", 0) + 1
        spec = WB._spec(scope)
        dll_name = f"{spec['mod_id']}_{spec['version']}_123456.dll"
        dll_path = os.path.join(paths["mods64_dir"], dll_name)
        os.makedirs(paths["mods64_dir"], exist_ok=True)
        with open(dll_path, "wb") as handle:
            handle.write(b"MZ_FAKE_DLL")
        return {"ok": True, "compiled": True, "dll_name": dll_name, "dll_path": dll_path, "source_hash": "deadbeef"}

    monkeypatch.setattr(WB, "is_windhawk_running", lambda: True)
    monkeypatch.setattr(WB, "ensure_windhawk_running", lambda paths: True)
    monkeypatch.setattr(WB, "compile_mod", fake_compile)

    def fake_render(scope: str, img: Any, params: dict[str, Any], path: str) -> None:
        calls["render"] += 1
        real_render(scope, img, params, path)

    def fake_read(path: str) -> SM.ShortcutInfo:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            return SM._EMPTY_INFO
        return SM.ShortcutInfo(data.get("target", ""), "", data.get("icon", ""), 0, True)

    def fake_write(path: str, ico: str) -> str:
        calls["write"] += 1
        if str(pdata) in path:
            return "denied"
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        data["icon"] = ico
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return "ok"

    def fake_extract(path: str, info: Any = None) -> Image.Image:
        calls["extract"] += 1
        return R.sample_icons()[len(os.path.basename(path)) % 3]

    def fake_notify() -> None:
        calls["notify"] += 1

    monkeypatch.setattr(R, "render_ico", fake_render)
    monkeypatch.setattr(SM, "read_shortcut", fake_read)
    monkeypatch.setattr(SM, "write_shortcut_icon", fake_write)
    real_extract = SM.extract_icon_256
    monkeypatch.setattr(SM, "extract_icon_256", fake_extract)

    pub = pdata / "PublicRoot" / "Desktop"
    pub.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(RT, "desktop_dir", lambda: str(home / "Desktop"))
    monkeypatch.setattr(RT, "public_desktop_dir", lambda: str(pub))
    monkeypatch.setattr(RT, "notify_shell", fake_notify)

    def mk(path: str, target: str = "C:/app.exe", icon: str = "") -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"target": target, "icon": icon}, handle)
        return path

    def mk_url(
        path: str,
        icon: str | None = None,
        idx: int = 0,
        enc: str = "utf-8",
        nl: str = "\r\n",
        url: str = "steam://rungameid/1",
    ) -> str:
        lines = [
            "[{000214A0-0000-0000-C000-000000000046}]",
            "Prop3=19,0",
            "[InternetShortcut]",
            "IDList=",
            "URL=" + url,
            "; 註解 遊戲",
        ]
        if icon is not None:
            lines += ["IconFile=" + icon, "IconIndex=" + str(idx)]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write((nl.join(lines) + nl).encode(enc))
        return path

    class Sandbox:
        pass

    box = Sandbox()
    box.RT = RT
    box.SM = SM
    box.DW = DW
    box.TC = TC
    box.WA = WA
    box.WB = WB
    box.S = S
    box.R = R
    box.main = APP_MAIN
    box.calls = calls
    box.mk = mk
    box.mk_url = mk_url
    box.real_extract = real_extract
    box.pub = str(pub)
    box.home = str(home)
    box.appdata = str(appdata)
    box.pdata = str(pdata)
    box.base = str(base)
    box.wh = str(wh)
    box.temp_dir = str(temp_dir)
    box.tb = str(appdata / "Microsoft/Internet Explorer/Quick Launch/User Pinned/TaskBar")
    box.sm = str(appdata / "Microsoft/Windows/Start Menu/Programs")
    box.smc = str(pdata / "Microsoft/Windows/Start Menu/Programs")
    return box
