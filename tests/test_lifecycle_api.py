"""tests/test_lifecycle_api.py -- 三區連動、UI API、CLI 流程、可選 Windhawk 降級與退出還原生命週期測試。"""
from __future__ import annotations

import json
import os
import threading

from conftest import populate
from liquid_glass import runtime as RT
from liquid_glass import shortcut_manager as SM
from liquid_glass import specs as S
from liquid_glass import tray_controller as TC
from liquid_glass import web_api as WA
from liquid_glass import windhawk_bridge as WB


def test_all_three_scopes_active_and_unlocked(env, monkeypatch):
    populate(env)
    d_lnk = os.path.join(env.home, "Desktop", "Chrome.lnk")
    sm_lnk = os.path.join(env.sm, "Tools", "Editor.lnk")
    sm_before = open(sm_lnk, "rb").read()

    assert S.ACTIVE_SCOPES == ("taskbar", "startmenu", "desktop", "tray")
    assert S.LOCKED_SCOPES == ()
    assert SM.effective_active_scopes() == ["taskbar", "startmenu", "desktop", "tray"]
    assert SM.effective_active_scopes(include_experimental=False) == ["taskbar", "startmenu", "desktop", "tray"]
    for sc in S.SCOPES:
        assert not S.is_scope_locked(sc)
    assert S.is_scope_locked("bogus")

    res = SM.run_batch("apply", ["desktop", "startmenu", "taskbar"], S.default_config())
    r_map = {r["scope"]: r for r in res}
    assert not r_map["startmenu"].get("locked") and r_map["startmenu"]["applied"] == 1
    assert not r_map["taskbar"].get("locked") and r_map["taskbar"]["applied"] == 1
    assert not r_map["desktop"].get("locked") and r_map["desktop"]["applied"] == 2
    assert open(sm_lnk, "rb").read() == sm_before
    assert json.load(open(d_lnk))["icon"].endswith("_framed_glass.ico")
    assert WB.get_status(scope="startmenu")["enabled"] is True

    res_r = SM.run_batch("restore", ["desktop", "startmenu", "taskbar"], S.default_config())
    rr_map = {r["scope"]: r for r in res_r}
    assert rr_map["startmenu"]["restored"] == 1 and open(sm_lnk, "rb").read() == sm_before
    assert rr_map["desktop"]["restored"] == 2
    assert WB.get_status(scope="startmenu")["enabled"] is False


def test_api_unlocks_all_three_scopes_and_exposes_engines(env, monkeypatch):
    monkeypatch.setattr(TC, "spawn_background_tray", lambda: None)
    monkeypatch.setattr(TC, "is_tray_running", lambda: False)
    app = TC.LiquidGlassApp()
    api = WA.LiquidGlassApi(app)
    st = api.get_state()
    assert st["version"] == S.VERSION
    assert st["default_scope"] == "taskbar"
    assert st["locked_scopes"] == []
    for sc in S.SCOPES:
        assert st["specs"][sc]["locked"] is False
    assert "startmenu_engine" in st and st["startmenu_engine"]["scope"] == "startmenu"
    assert "tray_engine" in st and st["tray_engine"]["scope"] == "tray"
    assert st["experimental_scopes"] == []
    assert api.get_engine_status("startmenu")["scope"] == "startmenu"
    assert api.get_engine_status("tray")["scope"] == "tray"
    assert st["watcher_locked"] is False

    assert api.apply_scope("startmenu", S.default_params("startmenu"))["ok"] is True
    assert api.restore_scope("startmenu")["ok"] is True
    assert api.apply_all({sc: S.default_params(sc) for sc in S.SCOPES})["ok"] is True
    assert WB.get_status(scope="taskbar")["enabled"] is True
    assert WB.get_status(scope="startmenu")["enabled"] is True
    assert WB.get_status(scope="tray")["enabled"] is True
    assert api.restore_scope("tray")["ok"] is True
    assert WB.get_status(scope="tray")["enabled"] is False
    assert WB.get_status(scope="taskbar")["enabled"] is True
    assert api.apply_scope("tray", S.default_params("tray"))["ok"] is True
    assert WB.get_status(scope="tray")["enabled"] is True
    assert api.restore_all()["ok"] is True
    assert WB.get_status(scope="tray")["enabled"] is False
    assert WB.get_status(scope="taskbar")["enabled"] is False

    assert api.set_watcher(True) is True
    assert json.load(open(RT.CONFIG_PATH))["watcher"]["enabled"] is True

    for sc in S.SCOPES:
        pv = api.preview(sc, S.default_params(sc), 1.5, 7)
        assert pv["seq"] == 7 and pv["scope"] == sc and len(pv["big"]) == 4 and len(pv["actual"]) == 4
        assert all(u.startswith("data:image/png;base64,") for u in pv["big"] + pv["actual"])


def test_cli_supports_all_three_scopes_and_rejects_bogus(env):
    M = env.main
    assert M._parse_target(["--apply"], "--apply") == ("all", ["taskbar", "startmenu", "desktop", "tray"])
    assert M._parse_target(["--restore", "all"], "--restore") == ("all", ["taskbar", "startmenu", "desktop", "tray"])
    assert M.main(["--apply", "bogus"]) == 2
    assert M.main(["--restore", "bogus"]) == 2
    assert M.main(["--apply"]) == 0
    assert WB.get_status(scope="taskbar")["enabled"] is True
    assert WB.get_status(scope="startmenu")["enabled"] is True
    assert WB.get_status(scope="tray")["enabled"] is True
    assert M.main(["--restore", "tray"]) == 0
    assert WB.get_status(scope="tray")["enabled"] is False
    assert WB.get_status(scope="taskbar")["enabled"] is True
    assert M.main(["--apply", "tray"]) == 0
    st_tr = WB.get_status(scope="tray")
    assert st_tr["enabled"] is True and st_tr["ini_settings"]["Opacity"] == "100"
    assert M.main(["--restore"]) == 0
    assert WB.get_status(scope="tray")["enabled"] is False
    assert M.main(["--apply", "startmenu"]) == 0
    st_sm = WB.get_status(scope="startmenu")
    assert st_sm["enabled"] is True and st_sm["ini_settings"]["Opacity"] == "100"
    assert M.main(["--restore", "startmenu"]) == 0
    assert WB.get_status(scope="startmenu")["enabled"] is False
    assert M.main(["--apply", "desktop"]) == 0
    assert M.main(["--restore", "desktop"]) == 0
    assert M.main(["--apply", "taskbar"]) == 0
    st = WB.get_status()
    assert st["enabled"] is True and st["ini_settings"]["Opacity"] == "100"
    assert M.main(["--restore", "taskbar"]) == 0
    assert WB.get_status()["enabled"] is False


def test_api_apply_scope_exposes_top_level_needs_admin_and_admin_result(env, monkeypatch):
    monkeypatch.setattr(TC, "spawn_background_tray", lambda: None)
    monkeypatch.setattr(TC, "is_tray_running", lambda: False)
    env.mk(os.path.join(env.home, "Desktop", "Mine.lnk"))
    env.mk(os.path.join(env.pub, "Steam.lnk"))
    real_write = SM.write_any_shortcut

    def deny_pub(path, ico):
        if str(env.pdata) in path:
            return "denied"
        return real_write(path, ico)

    monkeypatch.setattr(SM, "write_any_shortcut", deny_pub)
    api = WA.LiquidGlassApi(TC.LiquidGlassApp())
    out = api.apply_scope("desktop", S.default_params("desktop"))
    assert out["ok"] is True and out["kind"] == "apply" and out["needs_admin"] == 1
    assert api.get_admin_result() == {}
    RT.atomic_write_json(RT.ADMIN_RESULT_PATH, {"kind": "apply", "message": "done", "time": 123.0})
    assert api.get_admin_result()["message"] == "done"


def test_parse_startup_scope_and_webview_storage_path(env, monkeypatch):
    M = env.main
    assert M._parse_startup_scope(["--settings", "--scope", "desktop"]) == "desktop"
    assert M._parse_startup_scope(["--settings", "--scope=taskbar"]) == "taskbar"
    assert M._parse_startup_scope(["--settings", "--scope", "startmenu"]) == "startmenu"
    assert M._parse_startup_scope(["--settings", "--scope", "tray"]) == "tray"
    assert M._parse_startup_scope(["--settings", "--scope", "bogus"]) == ""
    assert M._parse_startup_scope([]) == ""

    monkeypatch.setattr(RT, "PROFILE_DIR", "C:\\tmp\\.webview_profile")
    monkeypatch.setattr(RT, "is_admin", lambda: False)
    assert TC._webview_storage_path() == "C:\\tmp\\.webview_profile"
    monkeypatch.setattr(RT, "is_admin", lambda: True)
    assert TC._webview_storage_path() == "C:\\tmp\\.webview_profile_admin"

    app = TC.LiquidGlassApp()
    api = WA.LiquidGlassApi(app)
    assert api.get_startup_scope() == ""
    app.initial_scope = "desktop"
    assert api.get_startup_scope() == "desktop"
    app.initial_scope = "startmenu"
    assert api.get_startup_scope() == "startmenu"
    app.initial_scope = "bogus"
    assert api.get_startup_scope() == ""


def test_api_relaunch_admin_wiztree_banner(env, monkeypatch):
    calls = []

    class DummyWin:
        def __init__(self):
            self.evt = threading.Event()

        def destroy(self):
            self.evt.set()

    app = TC.LiquidGlassApp()
    app.window = DummyWin()
    api = WA.LiquidGlassApi(app)

    monkeypatch.setattr(RT, "is_admin", lambda: True)
    monkeypatch.setattr(RT, "relaunch_as_admin", lambda args, show=0: calls.append((args, show)) or True)
    r_adm = api.relaunch_admin("desktop")
    assert r_adm["ok"] is False and r_adm["already_admin"] is True and calls == []

    monkeypatch.setattr(RT, "is_admin", lambda: False)
    monkeypatch.setattr(RT, "relaunch_as_admin", lambda args, show=0: calls.append((args, show)) or False)
    r_cancel = api.relaunch_admin("desktop")
    assert r_cancel["ok"] is False and not app.window.evt.is_set()
    assert calls == [(["--settings", "--scope", "desktop"], 1)]

    tray_spawns = []
    monkeypatch.setattr(TC, "spawn_background_tray", lambda: tray_spawns.append(1) or None)
    monkeypatch.setattr(TC, "is_tray_running", lambda: False)

    calls.clear()
    monkeypatch.setattr(RT, "relaunch_as_admin", lambda args, show=0: calls.append((args, show)) or True)
    assert api.relaunch_admin("startmenu")["scope"] == "startmenu"
    assert calls[-1] == (["--settings", "--scope", "startmenu"], 1)
    assert len(tray_spawns) == 1
    assert api.relaunch_admin("bogus")["scope"] == "taskbar"
    assert calls[-1] == (["--settings", "--scope", "taskbar"], 1)
    assert app.window.evt.wait(1.5)

    seen = []
    monkeypatch.setattr(TC.LiquidGlassApp, "open_settings_window", lambda self, initial_scope="": seen.append(initial_scope))
    assert env.main.main(["--settings", "--scope", "desktop"]) == 0
    assert seen == ["desktop"]

    if RT.IS_WIN and RT.user32 is not None:
        monkeypatch.setattr(RT.user32, "IsWindowVisible", lambda _h: 1)
        monkeypatch.setattr(RT.user32, "GetWindowTextLengthW", lambda _h: len(RT.APP_TITLE))
        monkeypatch.setattr(RT.user32, "GetWindowTextW", lambda _h, b, _n: b.__setitem__(slice(0, len(RT.APP_TITLE)), RT.APP_TITLE))
        monkeypatch.setattr(RT.user32, "EnumWindows", lambda cb, lp: cb(12345, lp) or 1)
        monkeypatch.setattr(RT.user32, "PostMessageW", lambda *_a: 0)
        assert TC._focus_existing_settings_window() is False
        monkeypatch.setattr(RT.user32, "PostMessageW", lambda *_a: 1)
        monkeypatch.setattr(RT.user32, "ShowWindow", lambda *_a: 1)
        monkeypatch.setattr(RT.user32, "SetForegroundWindow", lambda *_a: 1)
        assert TC._focus_existing_settings_window() is True


def test_ui_html_has_wiztree_non_blocking_banner(env):
    with open(RT.UI_HTML_PATH, "r", encoding="utf-8") as handle:
        html = handle.read()
    for token in (
        "adminPill",
        "btnApplyAll",
        "btnRestoreAll",
        "engCardTitle",
        "renderEngineTelemetry",
        "get_engine_status",
        "whSetupBox",
        "btnAutoSetupWh",
        "auto_setup_windhawk",
        "btnOpenWhSite",
        "btnBrowseWh",
        "txtWhPath",
        "btnSaveWhPath",
        "swHideWhTasks",
        "set_hide_wh_tasks_dialog",
        "initializing_symbols",
        "btnWlAddMemReduct",
        "isWhAvailable",
        "optional-off",
        "repeat(4, 1fr)",
        "tray_engine",
        "ENGINE_META",
        "一鍵套用全部四區",
    ):
        assert token in html
    for stale in (
        "桌面・開始功能表目前封鎖",
        "開始功能表封鎖中",
        "開始功能表暫未開放",
        "id=\"elevateBanner\"",
        "id=\"visualAdvisoryBox\"",
        "id=\"adminHint\"",
    ):
        assert stale not in html


def test_optional_extension_fallback_without_windhawk(env, tmp_path, monkeypatch, capsys):
    missing_wh = tmp_path / "absent_windhawk"
    monkeypatch.setenv("LGFW_WINDHAWK_DIR", str(missing_wh))
    monkeypatch.setenv("LGIC_WINDHAWK_DIR", str(missing_wh))
    env.mk(os.path.join(env.home, "Desktop", "Chrome.lnk"))
    env.mk(os.path.join(env.home, "Desktop", "Notes.lnk"))

    app = TC.LiquidGlassApp()
    api = WA.LiquidGlassApi(app)
    st = api.get_state()
    assert st["windhawk_installed"] is False
    assert st["default_scope"] == "desktop"
    assert st["available_scopes"] == ["desktop"]
    assert st["optional_scopes"] == ["taskbar", "startmenu", "tray"]
    assert st["experimental_scopes"] == []
    assert st["specs"]["desktop"]["requires_windhawk"] is False
    assert st["specs"]["taskbar"]["requires_windhawk"] is True and "深色模式" in st["specs"]["taskbar"]["advisory"]
    assert st["specs"]["startmenu"]["requires_windhawk"] is True and "深色模式" in st["specs"]["startmenu"]["advisory"]
    assert st["specs"]["tray"]["requires_windhawk"] is True and st["specs"]["tray"]["experimental"] is False

    r_all = api.apply_all({sc: S.default_params(sc) for sc in S.SCOPES})
    assert r_all["ok"] is True and [r["scope"] for r in r_all["results"]] == ["desktop"]
    assert r_all["results"][0]["applied"] == 2
    r_rest = api.restore_all()
    assert r_rest["ok"] is True and r_rest["results"][0]["restored"] == 2

    assert env.main.main(["--apply"]) == 0
    out = capsys.readouterr().out
    assert "桌面：更新 2" in out and "略過選用擴充" in out

    r_tb = api.apply_scope("taskbar", S.default_params("taskbar"))
    assert r_tb["ok"] is False and r_tb["results"][0].get("windhawk_missing") == 1
    assert "選用擴充" in r_tb["message"]
    assert WB.is_windhawk_installed() is False


def test_lifecycle_config_and_restore_on_exit(env, monkeypatch):
    populate(env)
    assert S.clamp_lifecycle("junk") == S.default_lifecycle() == {
        "restore_live_on_exit": False,
        "exit_when_window_closes": False,
    }
    assert S.clamp_lifecycle({"restore_live_on_exit": 1, "exit_when_window_closes": "x"}) == {
        "restore_live_on_exit": True,
        "exit_when_window_closes": False,
    }

    tray = TC.LiquidGlassApp()
    tray.config["lifecycle"] = {"restore_live_on_exit": True, "exit_when_window_closes": False}
    tray.save_config(lifecycle_changed=True)
    win_app = TC.LiquidGlassApp()
    win_app.config["lifecycle"] = {"restore_live_on_exit": False, "exit_when_window_closes": False}
    win_app.config["watcher"] = {"enabled": True, "debounce_sec": 6.0}
    win_app.save_config(watcher_changed=True)
    saved = json.load(open(RT.CONFIG_PATH, encoding="utf-8"))
    assert saved["lifecycle"] == {"restore_live_on_exit": True, "exit_when_window_closes": False}
    assert saved["watcher"] == {"enabled": True, "debounce_sec": 6.0}

    spawned = []
    monkeypatch.setattr(TC, "spawn_background_tray", lambda: spawned.append(1))
    monkeypatch.setattr(TC, "is_tray_running", lambda: False)
    monkeypatch.setattr(RT, "is_admin", lambda: False)
    api = WA.LiquidGlassApi(TC.LiquidGlassApp())
    assert api.set_restore_on_exit(True) is True
    assert api.set_exit_on_close(True) is True and spawned == []
    assert api.set_exit_on_close(False) is False and spawned == [1]
    monkeypatch.setattr(RT, "is_admin", lambda: True)
    assert api.set_exit_on_close(False) is False and spawned == [1, 1]
    monkeypatch.setattr(RT, "is_admin", lambda: False)
    st = api.get_state()
    assert st["restore_on_exit"] is True and st["exit_on_close"] is False

    with open(RT.UI_HTML_PATH, "r", encoding="utf-8") as handle:
        html = handle.read()
    for token in ("swExitOnClose", "swRestoreOnExit", "set_exit_on_close", "set_restore_on_exit", "trayWhitelistCard", "txtTrayWhitelist", "set_tray_whitelist", "btnSaveTrayWhitelist"):
        assert token in html

    SM.run_batch("apply", ["desktop", "startmenu", "taskbar"], S.default_config())
    d_lnk = os.path.join(env.home, "Desktop", "Chrome.lnk")
    d_before = open(d_lnk, "rb").read()
    assert WB.get_status(scope="taskbar")["enabled"] is True
    assert WB.get_status(scope="startmenu")["enabled"] is True

    app = TC.LiquidGlassApp()
    app.config["lifecycle"] = {"restore_live_on_exit": True, "exit_when_window_closes": False}
    app.save_config(lifecycle_changed=True)

    stop_calls = []
    monkeypatch.setattr(TC, "_signal_tray_stop", lambda: stop_calls.append(1) or True)
    app._relaunching_admin = True
    app.handle_window_closed()
    assert WB.get_status(scope="taskbar")["enabled"] is True and stop_calls == []
    app._relaunching_admin = False

    monkeypatch.setattr(TC, "is_tray_running", lambda: True)
    app.handle_window_closed()
    assert WB.get_status(scope="taskbar")["enabled"] is True and WB.get_status(scope="startmenu")["enabled"] is True

    class FakeTrayProc:
        def __init__(self):
            self.checks = 0

        def poll(self):
            return None

    app._tray_proc = FakeTrayProc()

    def becomes_ready():
        app._tray_proc.checks += 1
        return app._tray_proc.checks >= 2

    monkeypatch.setattr(TC, "is_tray_running", becomes_ready)
    app.handle_window_closed()
    assert app._tray_proc.checks >= 2
    assert WB.get_status(scope="taskbar")["enabled"] is True
    app._tray_proc = None

    monkeypatch.setattr(TC, "is_tray_running", lambda: False)
    app.handle_window_closed()
    assert WB.get_status(scope="taskbar")["enabled"] is False
    assert WB.get_status(scope="startmenu")["enabled"] is False
    assert open(d_lnk, "rb").read() == d_before

    SM.run_batch("apply", ["startmenu", "taskbar"], S.default_config())
    app.config["lifecycle"] = {"restore_live_on_exit": True, "exit_when_window_closes": True}
    app.save_config(lifecycle_changed=True)
    app.handle_window_closed()
    assert stop_calls == [1]
    assert WB.get_status(scope="taskbar")["enabled"] is False
    assert WB.get_status(scope="startmenu")["enabled"] is False
    assert open(d_lnk, "rb").read() == d_before

    exit_codes = []
    monkeypatch.setattr(TC.os, "_exit", lambda code=0: exit_codes.append(code))
    SM.run_batch("apply", ["startmenu", "taskbar", "tray"], S.default_config())
    app.quit_app(restore=False)
    assert WB.get_status(scope="taskbar")["enabled"] is True and WB.get_status(scope="tray")["enabled"] is True and exit_codes == [0]
    app.quit_app(restore=True)
    assert WB.get_status(scope="taskbar")["enabled"] is False
    assert WB.get_status(scope="startmenu")["enabled"] is False
    assert WB.get_status(scope="tray")["enabled"] is False
    assert open(d_lnk, "rb").read() == d_before
    assert exit_codes == [0, 0]


def test_cli_version_and_self_check(env, capsys):
    M = env.main
    assert M.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == S.VERSION
    assert M.main(["--check"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["version"] == S.VERSION
    assert all(report[k] is True for k in ("ui_html", "app_ico", "app_png", "taskbar_mod_cpp", "startmenu_mod_cpp", "tray_mod_cpp"))


def test_tray_whitelist_api_syncs_and_persists(env):
    populate(env)
    app = TC.LiquidGlassApp()
    api = WA.LiquidGlassApi(app)
    assert api.get_state()["tray_whitelist"] == S.DEFAULT_TRAY_WHITELIST

    r_apply = api.apply_scope("tray", S.default_params("tray"))
    assert r_apply["ok"] is True
    assert WB.get_status(scope="tray")["ini_settings"]["WhitelistApps"] == S.DEFAULT_TRAY_WHITELIST

    r_wl = api.set_tray_whitelist("HWiNFO64.exe, HWiNFO\nCustomApp.exe")
    assert r_wl["ok"] is True and r_wl["synced"] is True
    assert r_wl["tray_whitelist"] == "HWiNFO64.exe;HWiNFO;CustomApp.exe"
    assert WB.get_status(scope="tray")["ini_settings"]["WhitelistApps"] == "HWiNFO64.exe;HWiNFO;CustomApp.exe"
    assert api.save_params("tray", {"opacity": 0.9, "frost": 1.0, "glow": 1.0, "dark_protect": 1.0}) is True
    assert WB.get_status(scope="tray")["ini_settings"]["WhitelistApps"] == "HWiNFO64.exe;HWiNFO;CustomApp.exe"
    saved = json.load(open(RT.CONFIG_PATH, encoding="utf-8"))
    assert saved["tray_whitelist"] == "HWiNFO64.exe;HWiNFO;CustomApp.exe"

    bad_wh = api.set_windhawk_dir(os.path.join(env.home, "NoSuchWindhawk"))
    assert bad_wh["ok"] is False and "找不到 windhawk.exe" in bad_wh["message"]
    ok_wh = api.set_windhawk_dir(os.path.join(os.environ["LGFW_WINDHAWK_DIR"], "windhawk.exe"))
    assert ok_wh["ok"] is True and ok_wh["windhawk_installed"] is True
    assert json.load(open(RT.CONFIG_PATH, encoding="utf-8"))["windhawk_dir"] == os.path.normpath(os.environ["LGFW_WINDHAWK_DIR"])
    reset_wh = api.set_windhawk_dir("")
    assert reset_wh["custom_windhawk_dir"] == ""


def test_auto_setup_windhawk_portable_standard_uac_and_missing(env, tmp_path, monkeypatch):
    populate(env)
    app = TC.LiquidGlassApp()
    api = WA.LiquidGlassApi(app)

    # 1. Portable Windhawk installed -> auto_setup_windhawk deploys & activates all 4 scopes directly
    auto_ok = api.auto_setup_windhawk()
    assert auto_ok["ok"] is True
    assert WB.get_status(scope="taskbar")["enabled"] is True
    assert WB.get_status(scope="startmenu")["enabled"] is True
    assert WB.get_status(scope="tray")["enabled"] is True

    # 2. Standard Windhawk (UTF-16 LE windhawk.ini, Portable=0) when non-admin -> triggers UAC --setup-windhawk
    std_wh = tmp_path / "StdAutoWh"
    (std_wh / "Engine" / "1.7.3" / "64").mkdir(parents=True)
    (std_wh / "windhawk.exe").write_bytes(b"WH")
    (std_wh / "windhawk.ini").write_bytes(
        "[Storage]\r\nPortable=0\r\nEnginePath=Engine\\1.7.3\r\nRegistryKey=HKLM\\SOFTWARE\\Windhawk\r\n".encode("utf-16")
    )
    monkeypatch.setenv("LGFW_WINDHAWK_DIR", str(std_wh))
    monkeypatch.setenv("LGIC_WINDHAWK_DIR", str(std_wh))
    monkeypatch.setattr(RT, "is_admin", lambda: False)
    uac_calls = []
    monkeypatch.setattr(RT, "relaunch_as_admin", lambda args, show=0: uac_calls.append(args) or True)
    auto_uac = api.auto_setup_windhawk()
    assert auto_uac["ok"] is True and auto_uac.get("elevating") is True
    assert uac_calls == [["--setup-windhawk"]]

    # 3. Elevated --setup-windhawk CLI applies scopes & writes admin_last_result.json
    monkeypatch.setattr(RT, "is_admin", lambda: True)
    monkeypatch.setenv("LGFW_WINDHAWK_DIR", str(tmp_path / "windhawk"))
    monkeypatch.setenv("LGIC_WINDHAWK_DIR", str(tmp_path / "windhawk"))
    assert env.main.main(["--setup-windhawk"]) == 0
    ar = api.get_admin_result()
    assert ar["kind"] == "setup_windhawk" and set(ar["scopes"]) == {"taskbar", "startmenu", "desktop", "tray"}

    # 4. Windhawk missing -> graceful missing guidance
    monkeypatch.setenv("LGFW_WINDHAWK_DIR", str(tmp_path / "missing_wh"))
    monkeypatch.setenv("LGIC_WINDHAWK_DIR", str(tmp_path / "missing_wh"))
    auto_miss = api.auto_setup_windhawk()
    assert auto_miss["ok"] is False and auto_miss["windhawk_missing"] is True

