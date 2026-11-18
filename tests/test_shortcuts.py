"""tests/test_shortcuts.py -- 捷徑列舉、母本備份、過期自動更新、公用權限重試與 .url / AUMID 測試。"""
from __future__ import annotations

import builtins
import json
import os
import time

import pytest

from conftest import populate, unlock_legacy_scopes
from liquid_glass import renderer as R
from liquid_glass import runtime as RT
from liquid_glass import shortcut_manager as SM
from liquid_glass import specs as S
from liquid_glass import tray_controller as TC
from liquid_glass import windhawk_bridge as WB


def test_enumeration_rules(env):
    populate(env)
    assert sorted(os.path.basename(i.path) for i in SM.enumerate_scope("desktop")) == ["Chrome.lnk", "Notes.lnk"]
    assert len(SM.enumerate_scope("taskbar")) == 2
    sm = SM.enumerate_scope("startmenu")
    assert len(sm) == 3
    assert len({i.ico_name for i in sm}) == 3
    assert len({i.primary for i in sm}) == 3


def test_apply_all_creates_backups_icons_and_points_shortcuts(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    populate(env)
    cfg = S.default_config()
    sm_lnk = os.path.join(env.sm, "Tools", "Editor.lnk")
    sm_before = open(sm_lnk, "rb").read()

    res = SM.run_batch("apply", ["desktop", "startmenu"], cfg)
    r = {x["scope"]: x for x in res}
    assert r["desktop"]["applied"] == 2
    assert r["startmenu"]["applied"] == 1 and r["startmenu"]["denied"] == 0
    assert open(sm_lnk, "rb").read() == sm_before
    sm_ini = WB.read_mod_ini(WB.resolve_windhawk_paths(scope="startmenu")["ini_path"])
    assert sm_ini["mod"]["Disabled"] == "0"
    assert sm_ini["mod"]["Include"] == "StartMenuExperienceHost.exe"
    assert os.path.exists(os.path.join(RT.BACKUP_DIR, "Chrome.lnk"))
    names = os.listdir(RT.ICON_VAULT_DIR)
    assert any(n.endswith("_framed_glass.ico") for n in names)
    assert not any(n.endswith("_menu_glass.ico") for n in names)
    assert env.calls["notify"] == 1
    SM.run_batch("apply", ["desktop"], cfg)
    assert env.calls["notify"] == 1
    SM.run_batch("apply", ["startmenu"], cfg)
    assert env.calls["notify"] == 1


def test_second_apply_is_fully_cached(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    populate(env)
    SM.run_batch("apply", ["desktop", "startmenu"], S.default_config())
    base = dict(env.calls)
    res = SM.run_batch("apply", ["desktop"], S.default_config())
    assert [x["applied"] for x in res] == [0] and [x["unchanged"] for x in res] == [2]
    assert env.calls["render"] == base["render"]
    assert env.calls["write"] == base["write"]
    assert env.calls["extract"] == base["extract"]


def test_param_change_rerenders_only_that_scope(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    populate(env)
    cfg = S.default_config()
    SM.run_batch("apply", ["desktop", "startmenu"], cfg)
    before = env.calls["render"]
    cfg["desktop"]["opacity"] = 0.7
    SM.run_batch("apply", ["desktop", "startmenu"], cfg)
    assert env.calls["render"] - before == 2


def test_existing_backup_is_refreshed_when_live_is_clean_and_differs(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    populate(env)
    d = os.path.join(env.home, "Desktop", "Chrome.lnk")
    bk = os.path.join(RT.BACKUP_DIR, "Chrome.lnk")
    os.makedirs(RT.BACKUP_DIR, exist_ok=True)
    env.mk(bk, target="C:/OLD_VERSION.exe")
    res = SM.run_batch("apply", ["desktop"], S.default_config())[0]
    assert json.load(open(bk))["target"] == "C:/app.exe"
    assert res["backed_up"] >= 1
    assert json.load(open(d))["icon"].endswith("_framed_glass.ico")


def test_existing_backup_kept_when_live_is_already_ours(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    d = os.path.join(env.home, "Desktop", "Chrome.lnk")
    bk = os.path.join(RT.BACKUP_DIR, "Chrome.lnk")
    env.mk(bk, target="C:/ORIGINAL.exe")
    env.mk(d, target="C:/glassed.exe", icon=os.path.join(RT.ICON_VAULT_DIR, "Chrome_framed_glass.ico"))
    before = open(bk, "rb").read()
    SM.run_batch("apply", ["desktop"], S.default_config())
    assert open(bk, "rb").read() == before


def test_glass_on_glass_guard_skips_when_no_backup(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    ico = os.path.join(RT.ICON_VAULT_DIR, "Zoom_framed_glass.ico")
    env.mk(os.path.join(env.home, "Desktop", "Zoom.lnk"), icon=ico)
    res = SM.run_batch("apply", ["desktop"], S.default_config())[0]
    assert res["no_backup"] == 1 and res["applied"] == 0 and env.calls["render"] == 0


def test_legacy_flat_backup_is_used_for_already_modified_taskbar(env):
    ico = os.path.join(RT.ICON_VAULT_DIR, "Chrome_taskbar_glass.ico")
    env.mk(os.path.join(env.tb, "Chrome.lnk"), icon=ico)
    env.mk(os.path.join(RT.BACKUP_DIR, "Chrome.lnk"), target="C:/chrome.exe")
    res = SM.run_batch("apply", ["taskbar"], S.default_config())[0]
    assert res["applied"] == 1 and env.calls["render"] == 0
    assert json.load(open(os.path.join(env.tb, "Chrome.lnk")))["target"] == "C:/chrome.exe"
    assert json.load(open(os.path.join(env.tb, "Chrome.lnk")))["icon"] == ""
    ini = WB.read_mod_ini(WB.resolve_windhawk_paths()["ini_path"])
    assert ini["exists"] and ini["mod"]["Disabled"] == "0"
    SM.run_batch("restore", ["taskbar"], S.default_config())
    ini2 = WB.read_mod_ini(WB.resolve_windhawk_paths()["ini_path"])
    assert ini2["mod"]["Disabled"] == "1"


def test_restore_is_byte_exact_and_only_touches_ours(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    populate(env)
    originals = {it.path: open(it.path, "rb").read() for it in SM.enumerate_scope("desktop")}
    mine = env.mk(os.path.join(env.home, "Desktop", "Mine.lnk"), icon="C:/custom/my_own.ico")
    mine_bytes = open(mine, "rb").read()
    sm_items = [it for it in SM.enumerate_scope("startmenu") if it.source == "user"]
    sm_it = sm_items[0]
    os.makedirs(os.path.dirname(sm_it.primary), exist_ok=True)
    sm_orig = open(sm_it.path, "rb").read()
    open(sm_it.primary, "wb").write(sm_orig)
    env.mk(sm_it.path, icon=os.path.join(RT.ICON_VAULT_DIR, "Editor_old_menu_glass.ico"))

    SM.run_batch("apply", ["desktop", "startmenu"], S.default_config())
    assert open(mine, "rb").read() != mine_bytes
    assert open(sm_it.path, "rb").read() == sm_orig

    sm_it2 = sm_items[1]
    os.makedirs(os.path.dirname(sm_it2.primary), exist_ok=True)
    sm_orig2 = open(sm_it2.path, "rb").read()
    open(sm_it2.primary, "wb").write(sm_orig2)
    env.mk(sm_it2.path, icon=os.path.join(RT.ICON_VAULT_DIR, "Editor2_old_menu_glass.ico"))

    other = env.mk(os.path.join(env.home, "Desktop", "Other.lnk"), icon="C:/custom/other.ico")
    other_bytes = open(other, "rb").read()
    res = SM.run_batch("restore", ["desktop", "startmenu"], S.default_config())
    r = {x["scope"]: x for x in res}
    assert open(other, "rb").read() == other_bytes
    assert r["desktop"]["untouched"] >= 1
    assert r["startmenu"]["restored"] >= 2
    assert WB.get_status(scope="startmenu")["enabled"] is False
    assert open(sm_it2.path, "rb").read() == sm_orig2
    for path, data in originals.items():
        assert open(path, "rb").read() == data, path
    assert open(os.path.join(RT.BACKUP_DIR, "Chrome.lnk"), "rb").read() == originals[
        os.path.join(env.home, "Desktop", "Chrome.lnk")
    ]


def test_restore_detection_does_not_depend_on_install_path(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    env.mk(os.path.join(RT.BACKUP_DIR, "App.lnk"), target="C:/a.exe")
    env.mk(os.path.join(env.home, "Desktop", "App.lnk"), target="C:/a.exe", icon="D:/Moved/IconVault/App_framed_glass.ico")
    assert SM.restore_scope("desktop", notify=False)["restored"] == 1


def test_busy_lock(env):
    assert SM._APPLY_LOCK.acquire(blocking=False)
    try:
        assert SM.run_batch("apply", ["desktop"], S.default_config()) is None
        assert "進行中" in SM.summarize(None, "apply")
    finally:
        SM._APPLY_LOCK.release()


def test_config_roundtrip_is_atomic_and_legacy_upgrades(env):
    json.dump(
        {"line_strength": 1.9, "glow_amount": 0.35, "film_opacity": 0.88},
        open(RT.CONFIG_PATH, "w", encoding="utf-8"),
    )
    app = TC.LiquidGlassApp()
    assert app.config["desktop"] == S.default_params("desktop")
    saved = json.load(open(RT.CONFIG_PATH, encoding="utf-8"))
    assert saved["version"] == S.VERSION and "taskbar" in saved
    assert not os.path.exists(RT.CONFIG_PATH + ".tmp")


def test_desktop_merges_user_and_public(env):
    d = os.path.join(env.home, "Desktop")
    env.mk(os.path.join(d, "Same.lnk"))
    env.mk(os.path.join(env.pub, "Same.lnk"))
    env.mk(os.path.join(env.pub, "Steam.LNK"))
    env.mk(os.path.join(d, RT.SELF_SHORTCUT_NAME))
    env.mk(os.path.join(env.pub, RT.SELF_SHORTCUT_NAME))
    env.mk_url(os.path.join(d, "Balatro.url"), icon="C:/g/b.ico")
    env.mk_url(os.path.join(env.pub, "Web.URL"))
    open(os.path.join(d, "notes.txt"), "w").write("x")
    items = SM.enumerate_scope("desktop")
    got = {(os.path.basename(i.path), i.source, i.ext) for i in items}
    assert got == {
        ("Same.lnk", "user", ".lnk"),
        ("Same.lnk", "common", ".lnk"),
        ("Steam.LNK", "common", ".lnk"),
        ("Balatro.url", "user", ".url"),
        ("Web.URL", "common", ".url"),
    }
    by = {(os.path.basename(i.path), i.source): i for i in items}
    assert by[("Same.lnk", "user")].primary == os.path.join(RT.BACKUP_DIR, "Same.lnk")
    assert by[("Same.lnk", "common")].primary == os.path.join(RT.BACKUP_DIR, "PublicDesktop", "Same.lnk")
    assert by[("Same.lnk", "user")].ico_name == "Same_framed_glass.ico"
    assert by[("Same.lnk", "common")].ico_name == "Same_pub_framed_glass.ico"
    assert by[("Balatro.url", "user")].ico_name == "Balatro_url_framed_glass.ico"
    assert by[("Web.URL", "common")].ico_name == "Web_pub_url_framed_glass.ico"
    assert len({i.ico_name for i in items}) == len(items) and len({i.primary for i in items}) == len(items)
    assert all(i.legacy is None for i in items)


def test_desktop_without_public_folder_and_same_dir_dedup(env, monkeypatch):
    d = os.path.join(env.home, "Desktop")
    env.mk(os.path.join(d, "A.lnk"))
    monkeypatch.setattr(RT, "public_desktop_dir", lambda: os.path.join(env.base, "does-not-exist"))
    assert [i.source for i in SM.enumerate_scope("desktop")] == ["user"]
    monkeypatch.setattr(RT, "public_desktop_dir", lambda: d)
    assert len(SM.enumerate_scope("desktop")) == 1


def test_same_stem_lnk_and_url_do_not_collide(env):
    d = os.path.join(env.home, "Desktop")
    env.mk(os.path.join(d, "Foo.lnk"))
    env.mk_url(os.path.join(d, "Foo.url"), icon="C:/x.ico")
    items = SM.enumerate_scope("desktop")
    assert sorted(i.ico_name for i in items) == ["Foo_framed_glass.ico", "Foo_url_framed_glass.ico"]
    assert len({i.primary for i in items}) == 2


def test_url_only_accepted_on_desktop(env):
    env.mk_url(os.path.join(env.sm, "A.url"), icon="C:/x.ico")
    env.mk_url(os.path.join(env.tb, "B.url"), icon="C:/x.ico")
    assert SM.enumerate_scope("startmenu") == [] and SM.enumerate_scope("taskbar") == []


def test_public_apply_denied_reports_needs_admin(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    d = os.path.join(env.home, "Desktop")
    env.mk(os.path.join(d, "Mine.lnk"))
    env.mk(os.path.join(env.pub, "Steam.lnk"))
    env.mk(os.path.join(env.pub, "FxSound.lnk"))
    res = SM.run_batch("apply", ["desktop"], S.default_config())[0]
    assert res["applied"] == 1 and res["denied"] == 2 and res["needs_admin"] == 2
    assert os.path.exists(os.path.join(RT.BACKUP_DIR, "PublicDesktop", "Steam.lnk"))
    assert os.path.exists(os.path.join(RT.ICON_VAULT_DIR, "Steam_pub_framed_glass.ico"))
    assert "需以系統管理員權限重試" in SM.summarize([res], "apply") and "2" in SM.summarize([res], "apply")


def test_needs_admin_not_counted_when_already_admin(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    monkeypatch.setattr(RT, "is_admin", lambda: True)
    env.mk(os.path.join(env.pub, "Steam.lnk"))
    res = SM.run_batch("apply", ["desktop"], S.default_config())[0]
    assert res["denied"] == 1 and res["needs_admin"] == 0


def test_admin_retry_only_touches_common_and_skips_rerender(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    env.mk(os.path.join(env.home, "Desktop", "Mine.lnk"))
    env.mk(os.path.join(env.pub, "Steam.lnk"))
    SM.run_batch("apply", ["desktop"], S.default_config())
    rendered = env.calls["render"]
    monkeypatch.setattr(
        SM,
        "write_shortcut_icon",
        lambda p, ico: (json.dump(dict(json.load(open(p, encoding="utf-8")), icon=ico), open(p, "w", encoding="utf-8")) or "ok"),
    )
    res = SM.run_batch("apply", ["desktop"], S.default_config(), sources=("common",))[0]
    assert res["total"] == 1 and res["applied"] == 1 and res["denied"] == 0 and res["needs_admin"] == 0
    assert env.calls["render"] == rendered
    assert json.load(open(os.path.join(env.pub, "Steam.lnk")))["icon"].endswith("Steam_pub_framed_glass.ico")


def test_restore_denied_in_public_counts_needs_admin(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    p = os.path.join(env.pub, "Steam.lnk")
    env.mk(os.path.join(RT.BACKUP_DIR, "PublicDesktop", "Steam.lnk"), target="C:/steam.exe")
    env.mk(p, icon=os.path.join(RT.ICON_VAULT_DIR, "Steam_pub_framed_glass.ico"))
    real = SM.shutil.copy2

    def deny(src, dst, *a, **kw):
        if os.path.normpath(dst) == os.path.normpath(p):
            raise PermissionError(5, "denied")
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(SM.shutil, "copy2", deny)
    res = SM.restore_scope("desktop", notify=False)
    assert res["restored"] == 0 and res["denied"] == 1 and res["needs_admin"] == 1


def test_windhawk_scopes_need_no_admin_while_public_desktop_does(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    populate(env)
    env.mk(os.path.join(env.pub, "Steam.lnk"))
    res = {r["scope"]: r for r in SM.run_batch("apply", list(S.SCOPES), S.default_config())}
    assert res["desktop"]["denied"] == 1 and res["desktop"]["needs_admin"] == 1
    assert res["startmenu"]["needs_admin"] == 0 and res["startmenu"]["applied"] == 1
    assert res["taskbar"]["needs_admin"] == 0 and res["taskbar"]["applied"] == 1


def test_denied_detection_covers_com_errors(env, tmp_path):
    p = str(tmp_path / "x.lnk")
    assert SM._is_denied(PermissionError(13, "no"), p)
    com_err = Exception(-2147352567, "例外", (0, None, None, None, 0, -2147024891), None)
    assert SM._is_denied(com_err, p)
    assert SM._is_denied(Exception(-2147024891), p)
    assert not SM._is_denied(Exception(-2147220987, "other"), p)


def test_cli_admin_flow(env, capsys, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    env.mk(os.path.join(env.home, "Desktop", "Mine.lnk"))
    env.mk(os.path.join(env.pub, "Steam.lnk"))
    assert env.main.main(["--apply-admin", "desktop"]) == 1
    assert env.main.main(["--apply-admin", "nonsense"]) == 2
    monkeypatch.setattr(RT, "is_admin", lambda: True)
    monkeypatch.setattr(
        SM,
        "write_shortcut_icon",
        lambda p, ico: (json.dump(dict(json.load(open(p, encoding="utf-8")), icon=ico), open(p, "w", encoding="utf-8")) or "ok"),
    )
    assert env.main.main(["--apply-admin", "desktop"]) == 0
    assert json.load(open(os.path.join(env.pub, "Steam.lnk")))["icon"].endswith("_framed_glass.ico")
    assert json.load(open(os.path.join(env.home, "Desktop", "Mine.lnk")))["icon"] == ""
    out = json.load(open(RT.ADMIN_RESULT_PATH, encoding="utf-8"))
    assert out["kind"] == "apply" and out["results"][0]["applied"] == 1
    assert env.main._parse_target(["--restore-admin"], "--restore-admin") == ("all", list(S.ACTIVE_SCOPES))


URL_BODY = (
    "[{000214A0-0000-0000-C000-000000000046}]\nProp3=19,0\n[InternetShortcut]\nIDList=\n"
    "URL=steam://rungameid/1\n; 註解 遊戲\nIconFile=C:\\old.ico\nIconIndex=3\n"
)


@pytest.mark.parametrize(
    "enc,nl,bom",
    [("utf-8", "\r\n", False), ("utf-8-sig", "\r\n", True), ("cp950", "\n", False), ("utf-8", "\n", False)],
)
def test_url_rewrite_keeps_encoding_bom_newlines_and_other_lines(env, enc, nl, bom):
    body = URL_BODY.replace("\n", nl)
    raw = body.encode(enc)
    text, used = SM.decode_url_bytes(raw)
    assert used == enc
    assert SM.rewrite_url_text(text, "C:\\v\\X_framed_glass.ico", 0).encode(used).startswith(b"\xef\xbb\xbf") == bom
    new = SM.rewrite_url_text(text, "C:\\v\\X_framed_glass.ico", 0)
    a, b = body.split(nl), new.split(nl)
    assert len(a) == len(b)
    assert [x for x, y in zip(a, b) if x != y] == ["IconFile=C:\\old.ico", "IconIndex=3"]
    assert SM.rewrite_url_text(new, "C:\\old.ico", 3) == body
    assert "註解 遊戲".encode("cp950" if enc == "cp950" else "utf-8") in new.encode(used)


def test_url_decode_fallback_is_lossless(env):
    raw = b"[InternetShortcut]\r\nURL=a\x81\x9d\xffz\r\n"
    text, enc = SM.decode_url_bytes(raw)
    assert text.encode(enc) == raw
    assert SM.decode_url_bytes(b"\xff\xfe[\x00I\x00")[1] == "utf-16"


def test_url_rewrite_inserts_missing_keys_in_right_section(env):
    t = "[InternetShortcut]\r\nURL=http://a\r\n\r\n[Other]\r\nIconFile=keep\r\n"
    assert SM.rewrite_url_text(t, "C:\\n.ico", 0) == (
        "[InternetShortcut]\r\nURL=http://a\r\nIconFile=C:\\n.ico\r\nIconIndex=0\r\n\r\n[Other]\r\nIconFile=keep\r\n"
    )
    assert SM.rewrite_url_text("URL=x\n", "i", 0).endswith("[InternetShortcut]\nIconFile=i\nIconIndex=0\n")
    only_file = "[InternetShortcut]\nIconFile = C:\\a.ico\nURL=u\n"
    out = SM.rewrite_url_text(only_file, "N", 5)
    assert "IconFile =N" in out and "IconIndex=5" in out and out.count("IconFile") == 1
    dup = "[InternetShortcut]\nIconFile=a\nIconFile=b\n"
    assert SM.rewrite_url_text(dup, "N", 0).count("IconFile=N") == 2


def test_url_parse_only_reads_internet_shortcut_section(env):
    d = SM.parse_url_text("[Other]\nIconFile=wrong\n[InternetShortcut]\nURL=u\nIconFile=right\n; IconIndex=9\n")
    assert d == {"url": "u", "icon_file": "right", "icon_index": None}
    assert SM.parse_url_text("") == {"url": None, "icon_file": None, "icon_index": None}


def test_read_url_shortcut(env, tmp_path):
    p = env.mk_url(str(tmp_path / "g.url"), icon='"C:\\icons\\a.ico"', idx=2, enc="cp950")
    i = SM.read_url_shortcut(p)
    assert i.ok and i.icon_loc == "C:\\icons\\a.ico" and i.icon_idx == 2
    assert i.args == "steam://rungameid/1" and i.target == "" and i.newline == "\r\n"
    assert SM.read_url_shortcut(env.mk_url(str(tmp_path / "n.url"))).icon_loc == ""
    assert SM.read_url_shortcut(env.mk_url(str(tmp_path / "b.url"), icon="x", idx="zz")).icon_idx == 0
    assert SM.read_url_shortcut(str(tmp_path / "missing.url")).ok is False
    assert SM.read_any_shortcut(p).icon_idx == 2


def test_write_url_shortcut_icon_ok_denied_and_unencodable(env, tmp_path, monkeypatch):
    p = env.mk_url(str(tmp_path / "g.url"), icon="C:\\old.ico", idx=1)
    assert SM.write_url_shortcut_icon(p, "C:\\vault\\g_url_framed_glass.ico") == "ok"
    j = SM.read_url_shortcut(p)
    assert j.icon_loc.endswith("g_url_framed_glass.ico") and j.icon_idx == 0 and SM.is_ours(j.icon_loc)
    before = open(p, "rb").read()
    assert SM.write_url_shortcut_icon(p, "C:\\vault\\g_url_framed_glass.ico") == "ok"
    assert open(p, "rb").read() == before
    q = env.mk_url(str(tmp_path / "c.url"), icon="C:\\old.ico", enc="cp950")
    qb = open(q, "rb").read()
    assert SM.write_url_shortcut_icon(q, "C:\\vault\\😀.ico") == "fail" and open(q, "rb").read() == qb
    real_open = builtins.open

    def deny_open(path, mode="r", *a, **kw):
        if "w" in mode and str(path) == p:
            raise PermissionError(13, "denied")
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", deny_open)
    assert SM.write_url_shortcut_icon(p, "C:\\vault\\other_framed_glass.ico") == "denied"


def test_url_full_apply_restore_cycle_is_byte_exact(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    d = os.path.join(env.home, "Desktop")
    p = env.mk_url(os.path.join(d, "Balatro.url"), icon="C:\\Steam\\games\\b.ico", idx=0, enc="utf-8-sig")
    pub = env.mk_url(os.path.join(env.pub, "Web.url"), icon="C:\\Steam\\w.ico")
    original, original_pub = open(p, "rb").read(), open(pub, "rb").read()
    res = SM.run_batch("apply", ["desktop"], S.default_config())[0]
    assert res["applied"] == 2 and res["denied"] == 0 and res["no_icon"] == 0
    bk = os.path.join(RT.BACKUP_DIR, "Balatro.url")
    assert open(bk, "rb").read() == original
    assert os.path.exists(os.path.join(RT.BACKUP_DIR, "PublicDesktop", "Web.url"))
    now = open(p, "rb").read()
    assert now.startswith(b"\xef\xbb\xbf") and b"Prop3=19,0" in now and "註解 遊戲".encode() in now
    info = SM.read_url_shortcut(p)
    assert info.icon_loc == os.path.join(RT.ICON_VAULT_DIR, "Balatro_url_framed_glass.ico") and SM.is_ours(info.icon_loc)
    base = dict(env.calls)
    res = SM.run_batch("apply", ["desktop"], S.default_config())[0]
    assert res["unchanged"] == 2 and res["applied"] == 0
    assert env.calls["render"] == base["render"] and env.calls["extract"] == base["extract"]
    res = SM.run_batch("restore", ["desktop"], S.default_config())[0]
    assert res["restored"] == 2
    assert open(p, "rb").read() == original and open(pub, "rb").read() == original_pub


def test_url_without_icon_file_is_no_icon(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    env.mk_url(os.path.join(env.home, "Desktop", "Plain.url"))
    monkeypatch.setattr(SM, "extract_icon_256", env.real_extract)
    res = SM.run_batch("apply", ["desktop"], S.default_config())[0]
    assert res["no_icon"] == 1 and res["applied"] == 0


def test_extract_icon_256_for_url_reads_recorded_icon(env, tmp_path):
    ico = str(tmp_path / "game.ico")
    R.sample_icons()[0].save(ico, format="ICO", sizes=[(64, 64)])
    url = env.mk_url(str(tmp_path / "g.url"), icon=ico)
    im = env.real_extract(url)
    assert im is not None and im.size == (256, 256) and im.mode == "RGBA"
    ours = env.mk_url(str(tmp_path / "o.url"), icon=os.path.join(RT.ICON_VAULT_DIR, "o_glass.ico"))
    assert env.real_extract(ours) is None
    assert env.real_extract(env.mk_url(str(tmp_path / "m.url"), icon=str(tmp_path / "nope.ico"))) is None


def test_count_backups_includes_url(env):
    env.mk(os.path.join(RT.BACKUP_DIR, "A.lnk"))
    env.mk_url(os.path.join(RT.BACKUP_DIR, "B.url"))
    open(os.path.join(RT.BACKUP_DIR, "x.txt"), "w").write("")
    assert SM.count_backups() == 2


def test_aumid_helpers(env):
    a = "Canva.Affinity_8a0j1tnjnt4a4!Canva.Affinity"
    assert SM.aumid_from_parsing_name("shell:AppsFolder\\" + a) == a
    assert SM.aumid_from_parsing_name("SHELL:APPSFOLDER\\" + a) == a
    assert SM.aumid_from_parsing_name("shell:::{4234d49b-0245-4df3-b780-3893943456e1}\\" + a) == a
    assert SM.aumid_from_parsing_name("C:\\Windows\\notepad.exe") == "" and SM.aumid_from_parsing_name("") == ""
    assert S.aumid_shell_path(a) == "shell:AppsFolder\\" + a


def test_extract_falls_back_to_shell_item_only_when_no_target_and_no_icon(env, tmp_path, monkeypatch):
    sentinel = R.sample_icons()[0]
    seen = []
    monkeypatch.setattr(SM, "extract_icon_from_shell_item", lambda p: seen.append(p) or sentinel)
    lnk = str(tmp_path / "Affinity.lnk")
    open(lnk, "w").write("x")
    empty = SM.ShortcutInfo("", "", "", 0, True)
    assert env.real_extract(lnk, empty) is sentinel and seen == [lnk]
    exe = str(tmp_path / "app.exe")
    open(exe, "w").write("x")
    seen.clear()
    env.real_extract(lnk, SM.ShortcutInfo(exe, "", "", 0, True))
    assert seen == []
    ours = SM.ShortcutInfo("", "", os.path.join(RT.ICON_VAULT_DIR, "A_glass.ico"), 0, True)
    assert env.real_extract(lnk, ours) is sentinel


def test_backup_is_stale_rules(env, tmp_path):
    live, bk = str(tmp_path / "live.lnk"), str(tmp_path / "bk.lnk")
    open(live, "w").write("a")
    open(bk, "w").write("a")
    os.utime(bk, ns=(os.stat(live).st_atime_ns, os.stat(live).st_mtime_ns))
    I = SM.ShortcutInfo
    base = I("C:/a.exe", "-x", "C:/a.ico", 0, True)
    assert not SM.backup_is_stale(live, base, bk, base)
    assert SM.backup_is_stale(live, I("C:/b.exe", "-x", "C:/a.ico", 0, True), bk, base)
    assert SM.backup_is_stale(live, I("C:/a.exe", "-x", "C:/b.ico", 0, True), bk, base)
    assert SM.backup_is_stale(live, I("C:/a.exe", "-x", "C:/a.ico", 2, True), bk, base)
    assert SM.backup_is_stale(live, I("C:/a.exe", "-y", "C:/a.ico", 0, True), bk, base)
    if os.name == "nt":
        assert not SM.backup_is_stale(live, I("c:\\A.EXE", "-x", "C:\\a.ico", 0, True), bk, base)
    os.utime(live, ns=(1, 10**9))
    assert SM.backup_is_stale(live, base, bk, base)
    assert not SM.backup_is_stale(live, SM._EMPTY_INFO, bk, base)
    assert SM.backup_is_stale(bk, base, live, SM._EMPTY_INFO)


def test_stale_backup_refresh_full_cycle(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    d = os.path.join(env.home, "Desktop", "App.lnk")
    bk = os.path.join(RT.BACKUP_DIR, "App.lnk")
    env.mk(d, target="C:/v1.exe")
    SM.run_batch("apply", ["desktop"], S.default_config())
    assert json.load(open(bk))["target"] == "C:/v1.exe"
    time.sleep(0.02)
    env.mk(d, target="C:/v2.exe")
    before = env.calls["render"]
    r = SM.run_batch("apply", ["desktop"], S.default_config())[0]
    assert json.load(open(bk))["target"] == "C:/v2.exe"
    assert r["applied"] == 1 and r["backed_up"] == 1 and env.calls["render"] == before + 1
    assert not os.path.exists(bk + ".tmp")
    r = SM.run_batch("apply", ["desktop"], S.default_config())[0]
    assert r["unchanged"] == 1 and r["backed_up"] == 0
    SM.run_batch("restore", ["desktop"], S.default_config())
    assert json.load(open(d))["target"] == "C:/v2.exe" and json.load(open(d))["icon"] == ""


def test_identical_clean_shortcut_does_not_refresh_backup(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    env.mk(os.path.join(env.home, "Desktop", "App.lnk"))
    SM.run_batch("apply", ["desktop"], S.default_config())
    SM.restore_scope("desktop", notify=False)
    calls = []
    monkeypatch.setattr(SM, "refresh_backup", lambda it: calls.append(it) or True)
    SM.run_batch("apply", ["desktop"], S.default_config())
    assert calls == []


def test_touch_only_change_refreshes_backup(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    d = os.path.join(env.home, "Desktop", "App.lnk")
    env.mk(d)
    SM.run_batch("apply", ["desktop"], S.default_config())
    SM.restore_scope("desktop", notify=False)
    bk = os.path.join(RT.BACKUP_DIR, "App.lnk")
    st = os.stat(bk)
    os.utime(d, ns=(st.st_atime_ns, st.st_mtime_ns + 5 * 10**9))
    r = SM.run_batch("apply", ["desktop"], S.default_config())[0]
    assert r["backed_up"] == 1 and r["applied"] == 1
    assert json.load(open(d))["icon"].endswith("_framed_glass.ico")


def test_refresh_failure_keeps_old_backup_and_cleans_tmp(env, monkeypatch):
    d = os.path.join(env.home, "Desktop", "App.lnk")
    bk = os.path.join(RT.BACKUP_DIR, "App.lnk")
    env.mk(bk, target="C:/old.exe")
    env.mk(d, target="C:/new.exe")
    before = open(bk, "rb").read()
    real = SM.shutil.copy2

    def boom(src, dst, *a, **kw):
        if str(dst).endswith(".tmp"):
            real(src, dst, *a, **kw)
            raise OSError("disk full")
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(SM.shutil, "copy2", boom)
    assert SM.refresh_backup(SM.enumerate_scope("desktop")[0]) is False
    assert open(bk, "rb").read() == before and not os.path.exists(bk + ".tmp")


def test_stale_refresh_in_public_desktop_and_url(env, monkeypatch):
    unlock_legacy_scopes(monkeypatch)
    pub = os.path.join(env.pub, "Steam.lnk")
    url = os.path.join(env.home, "Desktop", "G.url")
    bks = {
        pub: os.path.join(RT.BACKUP_DIR, "PublicDesktop", "Steam.lnk"),
        url: os.path.join(RT.BACKUP_DIR, "G.url"),
    }
    env.mk(pub, target="C:/s1.exe")
    env.mk_url(url, icon="C:/g.ico", url="steam://rungameid/1")
    for p, b in bks.items():
        os.makedirs(os.path.dirname(b), exist_ok=True)
        open(b, "wb").write(open(p, "rb").read())
    time.sleep(0.02)
    env.mk(pub, target="C:/s2.exe")
    env.mk_url(url, icon="C:/g.ico", url="steam://rungameid/2")
    SM.run_batch("apply", ["desktop"], S.default_config())
    assert json.load(open(bks[pub]))["target"] == "C:/s2.exe"
    assert b"rungameid/2" in open(bks[url], "rb").read()


def test_url_wide_section_decode_and_repair(env, tmp_path, monkeypatch):
    raw = (
        b"[InternetShortcut]\r\nURL=steam://rungameid/1\r\n"
        b"IconFile=C:\\Mojibake\\path.ico\r\nIconIndex=0\r\n"
        b"[InternetShortcut.W]\r\n"
        + ("IconFile=" + "C:\\任務\\圖示_url_framed_glass.ico".encode("utf-7").decode("ascii") + "\r\n").encode("ascii")
    )
    p = tmp_path / "Game.url"
    p.write_bytes(raw)
    info = SM.read_url_shortcut(str(p))
    assert info.icon_loc == "C:\\任務\\圖示_url_framed_glass.ico" and info.icon_idx == 0

    u = os.path.join(env.home, "Desktop", "Broken.url")
    ico_dir = os.path.join(str(tmp_path), "任務圖示")
    os.makedirs(ico_dir, exist_ok=True)
    monkeypatch.setattr(RT, "ICON_VAULT_DIR", ico_dir)
    env.mk_url(u, icon="C:\\Steam\\g.ico")
    monkeypatch.setattr(SM, "write_url_shortcut_icon", lambda path, ico, idx=0: "ok")
    res_first = SM.run_batch("apply", ["desktop"], S.default_config())[0]
    assert res_first["applied"] == 1
    ico_target = SM.enumerate_scope("desktop")[0].ico_path
    with open(u, "wb") as handle:
        handle.write(f"[InternetShortcut]\r\nURL=steam://rungameid/1\r\nIconFile={ico_target}\r\nIconIndex=0\r\n".encode("utf-8"))
    if RT.IS_WIN:
        assert not SM._url_has_wide_section(u)
        calls = []
        monkeypatch.setattr(SM, "write_any_shortcut", lambda path, ico: (calls.append(path) or "ok"))
        SM.run_batch("apply", ["desktop"], S.default_config())
        assert calls == [u]
