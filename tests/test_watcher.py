"""tests/test_watcher.py -- 桌面背景監控去抖、防自我迴圈與系統匣多行程整合測試。"""
from __future__ import annotations

import json
import os
import time

from conftest import populate, unlock_legacy_scopes
from liquid_glass import desktop_watcher as DW
from liquid_glass import runtime as RT
from liquid_glass import shortcut_manager as SM
from liquid_glass import specs as S
from liquid_glass import tray_controller as TC
from liquid_glass import web_api as WA


def _w(fire=None, debounce: float = 3.0):
    SM._SELF_WRITE["t"] = None
    return DW.IconWatcher(fire or (lambda scopes: []), debounce_sec=debounce, clock=lambda: 0.0)


def test_watcher_debounce_merges_events_into_one_fire(env):
    w = _w()
    assert w.feed("desktop", "C:\\x\\a.lnk", now=100.0)
    assert not w.feed("taskbar", "C:\\x\\b.lnk", now=100.5)
    assert not w.feed("startmenu", "C:\\x\\b.lnk", now=101.0)
    assert w.take_due(now=102.9) == []
    assert w.take_due(now=103.0) == ["desktop"]
    assert w.take_due(now=500.0) == []


def test_watcher_each_new_event_restarts_the_quiet_timer(env):
    w = _w()
    w.feed("desktop", "a.lnk", now=0.0)
    w.feed("desktop", "b.lnk", now=2.5)
    assert w.take_due(now=3.0) == []
    assert w.take_due(now=5.5) == ["desktop"]


def test_watcher_max_wait_bounds_continuous_events(env):
    w = _w()
    for t in range(0, 70, 2):
        w.feed("desktop", "a.lnk", now=1000.0 + t)
    assert w.take_due(now=1000.0 + 61.0) == ["desktop"]


def test_watcher_filters_irrelevant_files(env):
    w = _w()
    j = lambda n: os.path.join("x", n)  # noqa: E731
    assert not w.feed("desktop", j("a.txt"), now=1.0)
    assert not w.feed("desktop", j("desktop.ini"), now=1.0)
    assert not w.feed("desktop", j(RT.SELF_SHORTCUT_NAME), now=1.0)
    assert not w.feed("taskbar", j("a.url"), now=1.0)
    assert not w.feed("bogus", "a.lnk", now=1.0)
    assert w.feed("desktop", j("a.URL"), now=1.0)
    assert not w.feed("startmenu", j("NewFolder"), now=1.0)
    assert w.feed("desktop", None, now=1.0)
    assert w.take_due(now=100.0) == ["desktop"]


def test_watcher_ignores_events_right_after_our_own_write(env):
    w = _w()
    SM.mark_self_write(2000.0)
    assert not w.feed("desktop", "a.lnk", now=2000.0)
    assert not w.feed("desktop", "a.lnk", now=2004.9) and w.ignored == 2
    assert w.feed("desktop", "a.lnk", now=2005.0)
    assert S.WATCHER_SELF_IGNORE_SEC == 5.0


def test_our_own_apply_marks_self_write_so_its_events_are_ignored(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    populate(env)
    SM._SELF_WRITE["t"] = None
    w = DW.IconWatcher(lambda s: [], debounce_sec=1.0)
    SM.run_batch("apply", ["desktop"], S.default_config())
    assert SM._SELF_WRITE["t"] is not None
    assert not w.feed("desktop", os.path.join(env.home, "Desktop", "Chrome.lnk"))
    assert w.take_due(now=time.monotonic() + 100) == []
    SM._SELF_WRITE["t"] = None
    SM.restore_scope("desktop", notify=False)
    assert SM._SELF_WRITE["t"] is not None


def test_watcher_configure_clamps_debounce(env):
    w = _w(debounce=0.2)
    assert w.debounce_sec == 1.0
    w.configure(999)
    assert w.debounce_sec == 30.0
    w.configure("junk")
    assert w.debounce_sec == S.WATCHER_DEFAULTS["debounce_sec"]


def test_watcher_incremental_apply_only_renders_what_changed(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    populate(env)
    cfg = S.default_config()
    SM.run_batch("apply", ["desktop", "startmenu"], cfg)
    fired = []
    w = DW.IconWatcher(
        lambda scopes: fired.append(scopes) or SM.run_batch("apply", scopes, cfg),
        debounce_sec=1.0,
        clock=lambda: 0.0,
    )
    SM._SELF_WRITE["t"] = None
    base = dict(env.calls)
    env.mk(os.path.join(env.home, "Desktop", "Brand_New.lnk"))
    env.mk_url(os.path.join(env.pub, "Game.url"), icon="C:/g.ico")
    assert w.feed("desktop", os.path.join(env.home, "Desktop", "Brand_New.lnk"), now=10.0)
    due = w.take_due(now=11.0)
    assert due == ["desktop"]
    res = w._fire(due)[0]
    assert res["applied"] == 2 and res["unchanged"] == 2
    assert env.calls["render"] - base["render"] == 2
    assert env.calls["extract"] - base["extract"] == 2


def test_watch_targets_cover_desktop_only(env):
    t = DW.watch_targets()
    got = {(x.scope, os.path.normpath(x.path), x.recursive) for x in t}
    assert ("desktop", os.path.normpath(os.path.join(env.home, "Desktop")), False) in got
    assert ("desktop", os.path.normpath(env.pub), False) in got
    assert all(x.scope == "desktop" for x in t)
    os.rmdir(env.pub)
    assert all(os.path.normpath(x.path) != os.path.normpath(env.pub) for x in DW.watch_targets())


def _run_thread_watcher(tmp_path, monkeypatch, fire, debounce: float = 1.0):
    d = tmp_path / "Desk"
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(SM, "scope_roots", lambda sc: [(str(d), "user", False)] if sc == "desktop" else [])
    SM._SELF_WRITE["t"] = None
    w = DW.IconWatcher(fire, debounce_sec=debounce)
    w.start()
    time.sleep(0.4)
    return d, w


def test_watcher_thread_debounces_real_file_events(env, tmp_path, monkeypatch):
    got = []
    d, w = _run_thread_watcher(tmp_path, monkeypatch, lambda sc: got.append((time.monotonic(), sc)) or [])
    try:
        t_first = time.monotonic()
        for i in range(3):
            (d / f"n{i}.lnk").write_text("x")
            time.sleep(0.4)
        t_last = time.monotonic()
        time.sleep(5.0)
    finally:
        w.stop()
    assert len(got) == 1 and got[0][1] == ["desktop"], got
    assert got[0][0] - t_last >= 0.5
    assert got[0][0] > t_first
    assert not w.running


def test_watcher_thread_ignores_changes_caused_by_our_own_write(env, tmp_path, monkeypatch):
    got = []
    d, w = _run_thread_watcher(tmp_path, monkeypatch, lambda sc: got.append(sc) or [])
    try:
        SM.mark_self_write()
        (d / "ours.lnk").write_text("x")
        time.sleep(3.5)
    finally:
        w.stop()
    assert got == [] and w.ignored >= 1


def test_watcher_thread_retries_when_apply_is_busy(env, tmp_path, monkeypatch):
    seen = []

    def fire(scopes):
        seen.append(time.monotonic())
        return None if len(seen) == 1 else []

    d, w = _run_thread_watcher(tmp_path, monkeypatch, fire)
    try:
        (d / "a.lnk").write_text("x")
        time.sleep(6.0)
    finally:
        w.stop()
    assert len(seen) == 2 and seen[1] - seen[0] >= 0.9


def test_watcher_stop_is_idempotent_and_restartable(env, tmp_path, monkeypatch):
    _d, w = _run_thread_watcher(tmp_path, monkeypatch, lambda sc: [])
    assert w.running
    w.stop()
    w.stop()
    assert not w.running
    w.start()
    assert w.running
    w.stop()


def test_app_starts_and_stops_watcher_from_config(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)

    class Fake:
        instances = []

        def __init__(self, fire, debounce_sec=None, **kw):
            self.fire, self.debounce, self.started, self.stopped = fire, debounce_sec, 0, 0
            self.running = False
            Fake.instances.append(self)

        def configure(self, sec):
            self.debounce = sec

        def start(self):
            self.started += 1
            self.running = True

        def stop(self):
            self.stopped += 1
            self.running = False

    monkeypatch.setattr(DW, "IconWatcher", Fake)
    app = TC.LiquidGlassApp()
    app.apply_watcher_config()
    assert app.watcher is None and Fake.instances == []
    app.config["watcher"] = {"enabled": True, "debounce_sec": 5.0}
    app.apply_watcher_config()
    app.apply_watcher_config()
    w = Fake.instances[0]
    assert len(Fake.instances) == 1 and w.started == 1 and w.debounce == 5.0
    app.config["watcher"] = {"enabled": True, "debounce_sec": 9.0}
    app.apply_watcher_config()
    assert w.debounce == 9.0 and w.started == 1
    app.config["watcher"] = {"enabled": False, "debounce_sec": 9.0}
    app.apply_watcher_config()
    assert w.stopped == 1 and app.watcher is None
    app._toggle_watcher()
    assert json.load(open(RT.CONFIG_PATH))["watcher"]["enabled"] is True and app.watcher is not None


def test_watcher_setting_survives_params_save_from_another_process(env):
    tray = TC.LiquidGlassApp()
    tray.config["watcher"] = {"enabled": True, "debounce_sec": 5.0}
    tray.save_config(watcher_changed=True)
    window = TC.LiquidGlassApp()
    window.config["watcher"] = {"enabled": False, "debounce_sec": 3.0}
    window.config["taskbar"]["glow"] = 1.5
    window.save_config()
    saved = json.load(open(RT.CONFIG_PATH))
    assert saved["watcher"] == {"enabled": True, "debounce_sec": 5.0} and saved["taskbar"]["glow"] == 1.5


def test_api_set_watcher_persists_and_spawns_tray(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    spawned = []
    monkeypatch.setattr(TC, "spawn_background_tray", lambda: spawned.append(1))
    monkeypatch.setattr(TC, "is_tray_running", lambda: False)
    api = WA.LiquidGlassApi(TC.LiquidGlassApp())
    assert api.get_state()["watcher"] is False
    assert api.set_watcher(True) is True and spawned == [1]
    assert json.load(open(RT.CONFIG_PATH))["watcher"]["enabled"] is True
    assert api.get_state()["watcher"] is True and api.get_state()["watcher_config"]["enabled"] is True
    monkeypatch.setattr(TC, "is_tray_running", lambda: True)
    assert api.set_watcher_debounce(999) == 30.0
    assert api.set_watcher(False) is False and spawned == [1]
    assert api.apply_admin("bogus")["ok"] is False
    monkeypatch.setattr(RT, "is_admin", lambda: True)
    assert api.apply_admin("desktop")["ok"] is False
