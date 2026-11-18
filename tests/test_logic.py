"""v1.3.1 邏輯測試：渲染演算法 + 規格表 + 備份/套用/還原/快取 + 公用桌面 + .url + 母本過期更新
+ Watcher（以假的 Windows 層模擬，可在任何平台執行）。

執行：  python -m pytest tests -q
"""
import json
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

import glass_render as R  # noqa: E402
import glass_specs as S  # noqa: E402


# ------------------------------------------------------------------ 渲染演算法
def _bbox_ratio(im, canvas=256):
    a = np.asarray(im.getchannel("A"))
    ys, xs = np.nonzero(a > 24)
    return max(xs.max() - xs.min() + 1, ys.max() - ys.min() + 1) / canvas


def test_bbox_normalization_makes_sizes_consistent():
    ratios = [_bbox_ratio(R.normalize_glyph_bbox(im, 0.68)) for im in R.sample_icons()]
    for r in ratios:
        assert abs(r - 0.68) < 0.02, ratios


def test_bbox_normalization_upscale_cap():
    tiny = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    ImageDraw.Draw(tiny).rectangle([120, 120, 135, 135], fill=(255, 0, 0, 255))  # 16px
    out = R.normalize_glyph_bbox(tiny, 0.8)
    assert _bbox_ratio(out) * 256 <= 16 * 3 + 2  # 放大上限 3 倍


def test_bbox_ignores_faint_shadow():
    im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle([0, 0, 255, 255], fill=(0, 0, 0, 10))   # 淡陰影 alpha=10
    ImageDraw.Draw(im).rectangle([100, 100, 155, 155], fill=(255, 0, 0, 255))
    assert abs(_bbox_ratio(R.normalize_glyph_bbox(im, 0.5)) - 0.5) < 0.02


def _solid_disc():
    im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse([8, 8, 247, 247], fill=(66, 133, 244, 255))
    return im


def _white_disc():
    im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    ImageDraw.Draw(im).ellipse([8, 8, 247, 247], fill=(255, 255, 255, 255))
    return im


def test_taskbar_never_becomes_white_block():
    disc = _solid_disc()
    white = _white_disc()
    for params in (S.default_params("taskbar"),
                   {"opacity": 99, "frost": 99, "glow": 99, "dark_protect": -5}):
        p = S.clamp_params("taskbar", params)
        assert p["opacity"] <= 1.8 and p["frost"] <= 1.3 and p["glow"] <= 1.8 and p["dark_protect"] >= 0.5
        for size in (16, 24, 32, 36, 48, 64):
            rgba = np.asarray(R.render_preview("taskbar", disc, p, size), dtype=np.float32) / 255.0
            a = rgba[..., 3]
            vis = a > 0.02
            assert vis.any()
            assert rgba[vis, :3].min() >= 0.92                      # 可見像素零黑灰顏料
            c = size // 2
            core = a[c - max(1, size // 6):c + max(1, size // 6),
                     c - max(1, size // 6):c + max(1, size // 6)]
            assert core.mean() < 0.45, (size, core.mean())          # 飽和色彩圓心為清透玻璃，不變成不透明白塊
            assert a.max() > core.mean()                            # 內緣斜角光暈高於中心
        w_rgba = np.asarray(R.render_preview("taskbar", white, p, 36), dtype=np.float32) / 255.0
        assert w_rgba[..., 3].max() <= 0.925                        # 純白圖樣為半透明霜白（上限 ~0.92）


def test_taskbar_optical_36px_fresnel_and_cutout():
    samples = R.sample_icons()
    assert len(samples) == 4
    small_36 = samples[3].resize((36, 36), Image.Resampling.LANCZOS)  # 模擬 150% DPI 工作列 36px 來源
    out = np.asarray(R.render_preview("taskbar", small_36, S.default_params("taskbar"), 36), dtype=np.float32) / 255.0
    assert out.shape == (36, 36, 4)
    vis = out[..., 3] > 0.02
    assert out[vis, :3].min() >= 0.92
    # 外圈全透明背景不被膨脹污損
    assert out[:2, :2, 3].max() == 0.0
    # 深字鏤空中心 (18, 18) 比亮底邊框更透
    center_alpha = out[18:22, 16:20, 3].mean()
    rim_alpha = out[4:10, 4:10, 3].max()
    assert rim_alpha > center_alpha


def test_menu_and_desktop_have_expected_structure():
    smp = R.sample_icons()[0]
    for sc in ("desktop", "startmenu", "taskbar"):
        out = np.asarray(R.render_preview(sc, smp, S.default_params(sc), 256), dtype=np.float32) / 255.0
        assert out.shape == (256, 256, 4)
        assert out[:2, :2, 3].max() == 0.0
        assert out[..., 3].max() <= 0.905
        vis = out[..., 3] > 0.02
        assert out[vis, :3].min() >= 0.92


def test_startmenu_optical_glass_matches_taskbar():
    disc = _solid_disc()
    for sz in (36, 48, 128):
        t = np.asarray(R.render_preview("taskbar", disc, S.default_params("taskbar"), sz), dtype=np.float32) / 255.0
        m = np.asarray(R.render_preview("startmenu", disc, S.default_params("startmenu"), sz), dtype=np.float32) / 255.0
        assert np.allclose(t, m, atol=1e-5)
        assert m[..., 3].max() <= 0.905


def test_ico_contains_all_native_sizes(tmp_path):
    src = R.sample_icons()[0]
    for scope in S.SCOPES:
        path = str(tmp_path / f"{scope}.ico")
        R.render_ico(scope, src, S.default_params(scope), path)
        ico = Image.open(path)
        assert set(ico.info["sizes"]) == {(s, s) for s in S.ICO_SIZES}, scope
        for s in (16, 32, 48, 256):
            assert ico.ico.getimage((s, s)).size == (s, s)
    # 原生合成：ICO 內的 32px 層就是原生 32px 渲染，而不是 256 縮小的
    ico = Image.open(str(tmp_path / "taskbar.ico"))
    native = R.render_preview("taskbar", src, S.default_params("taskbar"), 32)
    got = np.asarray(ico.ico.getimage((32, 32)).convert("RGBA"), dtype=np.int32)
    diff = np.abs(got - np.asarray(native, dtype=np.int32)).mean()
    assert diff < 3.0, diff


def test_config_migration_does_not_pollute_taskbar():
    cfg = S.migrate_config({"line_strength": 1.9, "glow_amount": 0.35, "film_opacity": 0.88})
    assert cfg["desktop"] == S.default_params("desktop") == {"opacity": 1.0, "frost": 1.0, "glow": 1.0, "dark_protect": 1.0}
    assert cfg["taskbar"] == S.default_params("taskbar") == {"opacity": 1.0, "frost": 1.0, "glow": 1.0, "dark_protect": 1.0}
    assert cfg["startmenu"] == S.default_params("startmenu") == {"opacity": 1.0, "frost": 1.0, "glow": 1.0, "dark_protect": 1.0}
    # 舊版 v1.x/v2.0 使用線框參數（line/film）自動歸位為光學預設值
    v13 = S.migrate_config({
        "desktop": {"line": 1.9, "glow": 0.35, "film": 0.88, "ratio": 0.68},
        "startmenu": {"line": 1.8, "glow": 0.40, "film": 0.45, "emboss": 0.50, "ratio": 0.80},
        "taskbar": {"line": 1.5, "glow": 0.35, "film": 0.06, "ratio": 0.8},
    })
    assert v13["desktop"] == S.default_params("desktop")
    assert v13["startmenu"] == S.default_params("startmenu")
    assert v13["taskbar"] == S.default_params("taskbar")


def test_clamp_rejects_garbage():
    p = S.clamp_params("taskbar", {"opacity": "abc", "frost": float("nan"), "glow": -5, "dark_protect": None, "x": 1})
    assert p == {"opacity": 1.0, "frost": 1.0, "glow": 0.3, "dark_protect": 1.0}
    assert S.migrate_config("not a dict") == S.default_config()


# ------------------------------------------------------------------ 備份 / 套用 / 還原 / 快取
@pytest.fixture()
def env(tmp_path, monkeypatch):
    base = tmp_path / "app"
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    pdata = tmp_path / "ProgramData"
    wh = tmp_path / "windhawk"
    temp_dir = tmp_path / "temp"
    for d in (base, home / "Desktop", wh / "Compiler" / "bin", wh / "Compiler" / "include",
              wh / "Engine" / "64", wh / "AppData" / "Engine" / "Mods" / "64", temp_dir,
              appdata / "Microsoft/Internet Explorer/Quick Launch/User Pinned/TaskBar",
              appdata / "Microsoft/Windows/Start Menu/Programs",
              pdata / "Microsoft/Windows/Start Menu/Programs"):
        d.mkdir(parents=True, exist_ok=True)
    (wh / "windhawk.exe").write_bytes(b"WH")
    (wh / "Compiler" / "bin" / "clang++.exe").write_bytes(b"CC")
    (wh / "Engine" / "64" / "windhawk.lib").write_bytes(b"LIB")
    import tempfile
    monkeypatch.setenv("LGIC_BASE_DIR", str(base))
    monkeypatch.setenv("LGIC_WINDHAWK_DIR", str(wh))
    monkeypatch.setenv("TEMP", str(temp_dir))
    monkeypatch.setenv("TMP", str(temp_dir))
    monkeypatch.setattr(tempfile, "tempdir", str(temp_dir))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("ProgramData", str(pdata))
    sys.modules.pop("LiquidGlassIconCenter", None)
    import LiquidGlassIconCenter as M
    import windhawk_bridge as WB
    M.ensure_dirs()

    calls = {"render": 0, "write": 0, "extract": 0, "wh_apply": 0, "wh_restore": 0}
    real_render = R.render_ico

    def fake_compile(paths, cpp_path, scope="taskbar"):
        calls["wh_compile"] = calls.get("wh_compile", 0) + 1
        spec = WB._spec(scope)
        dll_name = f"{spec['mod_id']}_{spec['version']}_123456.dll"
        dll_path = os.path.join(paths["mods64_dir"], dll_name)
        os.makedirs(paths["mods64_dir"], exist_ok=True)
        with open(dll_path, "wb") as f:
            f.write(b"MZ_FAKE_DLL")
        return {"ok": True, "compiled": True, "dll_name": dll_name, "dll_path": dll_path, "source_hash": "deadbeef"}

    monkeypatch.setattr(WB, "is_windhawk_running", lambda: True)
    monkeypatch.setattr(WB, "ensure_windhawk_running", lambda paths: True)
    monkeypatch.setattr(WB, "compile_mod", fake_compile)

    def fake_render(scope, img, params, path):
        calls["render"] += 1
        return real_render(scope, img, params, path)

    def fake_read(p):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            return M._EMPTY_INFO
        return M.ShortcutInfo(d.get("target", ""), "", d.get("icon", ""), 0, True)

    def fake_write(p, ico):
        calls["write"] += 1
        if str(pdata) in p:
            return "denied"
        d = json.load(open(p, encoding="utf-8"))
        d["icon"] = ico
        json.dump(d, open(p, "w", encoding="utf-8"))
        return "ok"

    def fake_extract(path, info=None):
        calls["extract"] += 1
        return R.sample_icons()[len(os.path.basename(path)) % 3]

    monkeypatch.setattr(R, "render_ico", fake_render)
    monkeypatch.setattr(M, "read_shortcut", fake_read)
    monkeypatch.setattr(M, "write_shortcut_icon", fake_write)
    real_extract = M.extract_icon_256
    monkeypatch.setattr(M, "extract_icon_256", fake_extract)
    calls["notify"] = 0

    def fake_notify():
        calls["notify"] += 1

    monkeypatch.setattr(M, "desktop_dir", lambda: str(home / "Desktop"))
    pub = pdata / "PublicRoot" / "Desktop"          # 公用桌面放在 ProgramData 之下：假寫入層會對它回報 denied
    pub.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(M, "public_desktop_dir", lambda: str(pub))
    monkeypatch.setattr(M, "notify_shell", fake_notify)

    def mk(path, target="C:/app.exe", icon=""):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json.dump({"target": target, "icon": icon}, open(path, "w", encoding="utf-8"))
        return path

    class E:  # noqa
        pass
    e = E()
    def mk_url(path, icon=None, idx=0, enc="utf-8", nl="\r\n", url="steam://rungameid/1"):
        lines = ["[{000214A0-0000-0000-C000-000000000046}]", "Prop3=19,0", "[InternetShortcut]",
                 "IDList=", "URL=" + url, "; 註解 遊戲"]
        if icon is not None:
            lines += ["IconFile=" + icon, "IconIndex=" + str(idx)]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write((nl.join(lines) + nl).encode(enc))
        return path

    e.M, e.calls, e.mk, e.mk_url, e.real_extract = M, calls, mk, mk_url, real_extract
    e.pub = str(pub)
    e.home, e.appdata, e.pdata, e.base = str(home), str(appdata), str(pdata), str(base)
    e.wh, e.temp_dir = str(wh), str(temp_dir)
    e.tb = str(appdata / "Microsoft/Internet Explorer/Quick Launch/User Pinned/TaskBar")
    e.sm = str(appdata / "Microsoft/Windows/Start Menu/Programs")
    e.smc = str(pdata / "Microsoft/Windows/Start Menu/Programs")
    return e


def _unlock_legacy_scopes(monkeypatch):
    """僅供驗證既有桌面／開始功能表捷徑備份演算法未退化，正式環境預設封鎖該兩區。"""
    monkeypatch.setattr(S, "LOCKED_SCOPES", ())
    monkeypatch.setattr(S, "ACTIVE_SCOPES", ("desktop", "startmenu", "taskbar"))


def _populate(e):
    d = os.path.join(e.home, "Desktop")
    e.mk(os.path.join(d, "Chrome.lnk"))
    e.mk(os.path.join(d, "Notes.lnk"))
    e.mk(os.path.join(d, e.M.SELF_SHORTCUT_NAME))               # 本工具自己的捷徑：必須略過
    e.mk(os.path.join(e.tb, "Chrome.lnk"))
    e.mk(os.path.join(e.tb, "Files.lnk"))
    e.mk(os.path.join(e.sm, "Tools", "Editor.lnk"))
    e.mk(os.path.join(e.sm, "Games", "Editor.lnk"))             # 同名不同資料夾：不能互相覆蓋
    e.mk(os.path.join(e.sm, "Tools", "Uninstall Editor.lnk"))   # 必須略過
    e.mk(os.path.join(e.sm, "Tools", "解除安裝 Foo.lnk"))        # 必須略過
    e.mk(os.path.join(e.smc, "Office.lnk"))                     # ProgramData：模擬權限不足


def test_enumeration_rules(env):
    _populate(env)
    M = env.M
    assert sorted(os.path.basename(i.path) for i in M.enumerate_scope("desktop")) == ["Chrome.lnk", "Notes.lnk"]
    assert len(M.enumerate_scope("taskbar")) == 2
    sm = M.enumerate_scope("startmenu")
    assert len(sm) == 3                                          # 兩個 Editor + Office，略過兩個解除安裝
    assert len({i.ico_name for i in sm}) == 3                    # ICO 檔名唯一
    assert len({i.primary for i in sm}) == 3                     # 母本路徑唯一（保留相對路徑）


def test_apply_all_creates_backups_icons_and_points_shortcuts(env, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    _populate(env)
    M = env.M
    import windhawk_bridge as WB
    cfg = S.default_config()
    sm_lnk = os.path.join(env.sm, "Tools", "Editor.lnk")
    sm_before = open(sm_lnk, "rb").read()

    res = M.run_batch("apply", ["desktop", "startmenu"], cfg)
    r = {x["scope"]: x for x in res}
    assert r["desktop"]["applied"] == 2
    # startmenu 改走 Windhawk 即時引擎：設定 INI 啟用，不修改 .lnk，不觸發 notify_shell
    assert r["startmenu"]["applied"] == 1 and r["startmenu"]["denied"] == 0
    assert open(sm_lnk, "rb").read() == sm_before
    sm_ini = WB.read_mod_ini(WB.resolve_windhawk_paths(scope="startmenu")["ini_path"])
    assert sm_ini["mod"]["Disabled"] == "0"
    assert sm_ini["mod"]["Include"] == "StartMenuExperienceHost.exe"
    assert os.path.exists(os.path.join(M.BACKUP_DIR, "Chrome.lnk"))
    names = os.listdir(M.ICON_VAULT_DIR)
    assert any(n.endswith("_framed_glass.ico") for n in names)
    assert not any(n.endswith("_menu_glass.ico") for n in names)
    # 系統通知僅於 desktop 有變更時呼叫一次；startmenu 不應呼叫 taskkill/notify_shell
    assert env.calls["notify"] == 1
    M.run_batch("apply", ["desktop"], cfg)                                # 桌面快取命中 → 不需要再通知
    assert env.calls["notify"] == 1
    M.run_batch("apply", ["startmenu"], cfg)                              # startmenu 不呼叫 notify_shell
    assert env.calls["notify"] == 1


def test_second_apply_is_fully_cached(env, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    _populate(env)
    M = env.M
    M.run_batch("apply", ["desktop", "startmenu"], S.default_config())
    base = dict(env.calls)
    res = M.run_batch("apply", ["desktop"], S.default_config())
    assert [x["applied"] for x in res] == [0] and [x["unchanged"] for x in res] == [2]
    assert env.calls["render"] == base["render"]       # 0 張重渲染
    assert env.calls["write"] == base["write"]         # 0 次捷徑寫入
    assert env.calls["extract"] == base["extract"]     # 連取圖都省了


def test_param_change_rerenders_only_that_scope(env, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    _populate(env)
    M = env.M
    cfg = S.default_config()
    M.run_batch("apply", ["desktop", "startmenu"], cfg)
    before = env.calls["render"]
    cfg["desktop"]["opacity"] = 0.7
    M.run_batch("apply", ["desktop", "startmenu"], cfg)
    assert env.calls["render"] - before == 2           # 只有桌面 2 個捷徑重新渲染


def test_existing_backup_is_refreshed_when_live_is_clean_and_differs(env, monkeypatch):
    """v1.3.1：舊規格『母本存在就永不覆寫』已作廢。現場是乾淨原始捷徑且來源特徵不同 → 以現場最新版覆寫母本。"""
    _unlock_legacy_scopes(monkeypatch)
    _populate(env)
    M = env.M
    d = os.path.join(env.home, "Desktop", "Chrome.lnk")
    bk = os.path.join(M.BACKUP_DIR, "Chrome.lnk")
    os.makedirs(M.BACKUP_DIR, exist_ok=True)
    env.mk(bk, target="C:/OLD_VERSION.exe")
    res = M.run_batch("apply", ["desktop"], S.default_config())[0]
    assert json.load(open(bk))["target"] == "C:/app.exe"           # 母本已更新為現場版本
    assert res["backed_up"] >= 1
    assert json.load(open(d))["icon"].endswith("_framed_glass.ico")


def test_existing_backup_kept_when_live_is_already_ours(env, monkeypatch):
    """現場已是本工具的玻璃圖示時，它不是『乾淨原始』，絕不可拿來覆寫母本。"""
    _unlock_legacy_scopes(monkeypatch)
    M = env.M
    d = os.path.join(env.home, "Desktop", "Chrome.lnk")
    bk = os.path.join(M.BACKUP_DIR, "Chrome.lnk")
    env.mk(bk, target="C:/ORIGINAL.exe")
    env.mk(d, target="C:/glassed.exe", icon=os.path.join(M.ICON_VAULT_DIR, "Chrome_framed_glass.ico"))
    before = open(bk, "rb").read()
    M.run_batch("apply", ["desktop"], S.default_config())
    assert open(bk, "rb").read() == before


def test_glass_on_glass_guard_skips_when_no_backup(env, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    M = env.M
    ico = os.path.join(M.ICON_VAULT_DIR, "Zoom_framed_glass.ico")
    env.mk(os.path.join(env.home, "Desktop", "Zoom.lnk"), icon=ico)   # 已是玻璃圖示，且沒有母本
    res = M.run_batch("apply", ["desktop"], S.default_config())[0]
    assert res["no_backup"] == 1 and res["applied"] == 0 and env.calls["render"] == 0


def test_legacy_flat_backup_is_used_for_already_modified_taskbar(env):
    """v2.0.0 工作列改走 Windhawk 即時光學引擎：若曾被 v1.2/1.3 改寫 .lnk，會先以母本還原並啟用模組。"""
    M = env.M
    ico = os.path.join(M.ICON_VAULT_DIR, "Chrome_taskbar_glass.ico")      # v1.2.0 的白塊版本
    env.mk(os.path.join(env.tb, "Chrome.lnk"), icon=ico)
    env.mk(os.path.join(M.BACKUP_DIR, "Chrome.lnk"), target="C:/chrome.exe")   # v1.2.0 平面母本
    res = M.run_batch("apply", ["taskbar"], S.default_config())[0]
    assert res["applied"] == 1 and env.calls["render"] == 0
    # 歷史工作列捷徑已被安全還原回乾淨母本
    assert json.load(open(os.path.join(env.tb, "Chrome.lnk")))["target"] == "C:/chrome.exe"
    assert json.load(open(os.path.join(env.tb, "Chrome.lnk")))["icon"] == ""
    import windhawk_bridge as WB
    ini = WB.read_mod_ini(WB.resolve_windhawk_paths()["ini_path"])
    assert ini["exists"] and ini["mod"]["Disabled"] == "0"
    M.run_batch("restore", ["taskbar"], S.default_config())
    ini2 = WB.read_mod_ini(WB.resolve_windhawk_paths()["ini_path"])
    assert ini2["mod"]["Disabled"] == "1"


def test_restore_is_byte_exact_and_only_touches_ours(env, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    _populate(env)
    M = env.M
    import windhawk_bridge as WB
    originals = {}
    for it in M.enumerate_scope("desktop"):
        originals[it.path] = open(it.path, "rb").read()
    mine = env.mk(os.path.join(env.home, "Desktop", "Mine.lnk"), icon="C:/custom/my_own.ico")  # 使用者自己改過
    mine_bytes = open(mine, "rb").read()
    # 模擬舊版 v1.x 留下的開始功能表 .lnk 母本：apply 與 restore 皆會將舊殘留還原
    sm_items = [it for it in M.enumerate_scope("startmenu") if it.source == "user"]
    sm_it = sm_items[0]
    os.makedirs(os.path.dirname(sm_it.primary), exist_ok=True)
    sm_orig = open(sm_it.path, "rb").read()
    open(sm_it.primary, "wb").write(sm_orig)
    env.mk(sm_it.path, icon=os.path.join(M.ICON_VAULT_DIR, "Editor_old_menu_glass.ico"))

    M.run_batch("apply", ["desktop", "startmenu"], S.default_config())
    assert open(mine, "rb").read() != mine_bytes                   # Mine 不是玻璃圖示 → 會被套用
    assert open(sm_it.path, "rb").read() == sm_orig                # apply 時已同步清除舊 .lnk 殘留

    # 再模擬另一份舊殘留並於 restore 時驗證亦會清除
    sm_it2 = sm_items[1]
    os.makedirs(os.path.dirname(sm_it2.primary), exist_ok=True)
    sm_orig2 = open(sm_it2.path, "rb").read()
    open(sm_it2.primary, "wb").write(sm_orig2)
    env.mk(sm_it2.path, icon=os.path.join(M.ICON_VAULT_DIR, "Editor2_old_menu_glass.ico"))

    # 重新放回「使用者自己改過、且不是我們的」另一個捷徑，驗證還原不碰它
    other = env.mk(os.path.join(env.home, "Desktop", "Other.lnk"), icon="C:/custom/other.ico")
    other_bytes = open(other, "rb").read()
    res = M.run_batch("restore", ["desktop", "startmenu"], S.default_config())
    r = {x["scope"]: x for x in res}
    assert open(other, "rb").read() == other_bytes                 # 沒動
    assert r["desktop"]["untouched"] >= 1
    assert r["startmenu"]["restored"] >= 2                         # Windhawk 停用 + 舊殘留 .lnk 還原
    assert WB.get_status(scope="startmenu")["enabled"] is False
    assert open(sm_it2.path, "rb").read() == sm_orig2
    for path, data in originals.items():
        assert open(path, "rb").read() == data, path               # 位元組級還原
    assert open(os.path.join(M.BACKUP_DIR, "Chrome.lnk"), "rb").read() == originals[
        os.path.join(env.home, "Desktop", "Chrome.lnk")]


def test_restore_detection_does_not_depend_on_install_path(env, monkeypatch):
    """搬資料夾後，仍能辨識出『指向 IconVault 的圖示』。"""
    _unlock_legacy_scopes(monkeypatch)
    M = env.M
    env.mk(os.path.join(M.BACKUP_DIR, "App.lnk"), target="C:/a.exe")
    env.mk(os.path.join(env.home, "Desktop", "App.lnk"), target="C:/a.exe",
           icon="D:/Moved/IconVault/App_framed_glass.ico")
    assert M.restore_scope("desktop", notify=False)["restored"] == 1


def test_busy_lock(env):
    M = env.M
    assert M._APPLY_LOCK.acquire(blocking=False)
    try:
        assert M.run_batch("apply", ["desktop"], S.default_config()) is None
        assert "進行中" in M.summarize(None, "apply")
    finally:
        M._APPLY_LOCK.release()


def test_config_roundtrip_is_atomic_and_legacy_upgrades(env):
    M = env.M
    json.dump({"line_strength": 1.9, "glow_amount": 0.35, "film_opacity": 0.88},
              open(M.CONFIG_PATH, "w", encoding="utf-8"))
    app = M.LiquidGlassApp()
    assert app.config["desktop"] == S.default_params("desktop")
    saved = json.load(open(M.CONFIG_PATH, encoding="utf-8"))          # 舊格式已升級寫回
    assert saved["version"] == S.VERSION and "taskbar" in saved
    assert not os.path.exists(M.CONFIG_PATH + ".tmp")


# ====================================================================== v1.3.1 新增
# ------------------------------------------------------------------ 規格表（步驟一介面）
def test_specs_paths_and_tags():
    assert S.backup_parts("desktop", "user") == ()                       # 既有 155 個母本不搬家
    assert S.backup_parts("desktop", "common") == ("PublicDesktop",)
    assert S.backup_parts("taskbar", "user") == ("Taskbar",)
    assert S.backup_parts("startmenu", "common") == ("StartMenu", "common")
    assert S.vault_tag("desktop", "user", ".lnk") == ""
    assert S.vault_tag("desktop", "common", ".lnk") == "_pub"
    assert S.vault_tag("desktop", "user", ".URL") == "_url"
    assert S.vault_tag("desktop", "common", ".url") == "_pub_url"
    assert S.vault_tag("startmenu", "common", ".lnk") == ""
    with pytest.raises(ValueError):
        S.backup_parts("desktop", "nobody")


def test_specs_ext_and_result_fields_and_watcher_clamp():
    assert S.shortcut_ext("desktop", "A.URL") == ".url" and S.shortcut_ext("desktop", "a.LNK") == ".lnk"
    assert S.shortcut_ext("startmenu", "a.url") is None and S.shortcut_ext("taskbar", "a.url") is None
    assert S.new_result("desktop", 3)["needs_admin"] == 0
    assert S.clamp_watcher(True) == {"enabled": True, "debounce_sec": 3.0}
    assert S.clamp_watcher({"enabled": 1, "debounce_sec": 999})["debounce_sec"] == 30.0
    assert S.clamp_watcher({"debounce_sec": 0.1})["debounce_sec"] == 1.0
    assert S.clamp_watcher({"debounce_sec": "x"}) == S.default_watcher()
    assert S.clamp_watcher("junk") == S.default_watcher()
    cfg = S.migrate_config({"desktop": {"line": 2.0}, "watcher": {"enabled": True, "debounce_sec": 7}})
    assert cfg["watcher"] == {"enabled": True, "debounce_sec": 7.0}
    assert S.migrate_config({})["watcher"] == S.default_watcher()          # v1.3.0 設定檔補上預設


# ------------------------------------------------------------------ 公用桌面 + 個人桌面 合併列舉
def test_desktop_merges_user_and_public(env):
    M = env.M
    d = os.path.join(env.home, "Desktop")
    env.mk(os.path.join(d, "Same.lnk"))
    env.mk(os.path.join(env.pub, "Same.lnk"))                 # 同名：個人與公用不可互相覆蓋
    env.mk(os.path.join(env.pub, "Steam.LNK"))                # 副檔名大小寫不拘
    env.mk(os.path.join(d, M.SELF_SHORTCUT_NAME))             # 本工具自己的捷徑
    env.mk(os.path.join(env.pub, M.SELF_SHORTCUT_NAME))
    env.mk_url(os.path.join(d, "Balatro.url"), icon="C:/g/b.ico")
    env.mk_url(os.path.join(env.pub, "Web.URL"))
    open(os.path.join(d, "notes.txt"), "w").write("x")        # 非捷徑
    items = M.enumerate_scope("desktop")
    got = {(os.path.basename(i.path), i.source, i.ext) for i in items}
    assert got == {("Same.lnk", "user", ".lnk"), ("Same.lnk", "common", ".lnk"),
                   ("Steam.LNK", "common", ".lnk"), ("Balatro.url", "user", ".url"),
                   ("Web.URL", "common", ".url")}
    by = {(os.path.basename(i.path), i.source): i for i in items}
    assert by[("Same.lnk", "user")].primary == os.path.join(M.BACKUP_DIR, "Same.lnk")
    assert by[("Same.lnk", "common")].primary == os.path.join(M.BACKUP_DIR, "PublicDesktop", "Same.lnk")
    assert by[("Same.lnk", "user")].ico_name == "Same_framed_glass.ico"
    assert by[("Same.lnk", "common")].ico_name == "Same_pub_framed_glass.ico"
    assert by[("Balatro.url", "user")].ico_name == "Balatro_url_framed_glass.ico"
    assert by[("Web.URL", "common")].ico_name == "Web_pub_url_framed_glass.ico"
    assert len({i.ico_name for i in items}) == len(items) and len({i.primary for i in items}) == len(items)
    assert all(i.legacy is None for i in items)


def test_desktop_without_public_folder_and_same_dir_dedup(env):
    M = env.M
    d = os.path.join(env.home, "Desktop")
    env.mk(os.path.join(d, "A.lnk"))
    M.public_desktop_dir = lambda: os.path.join(env.base, "does-not-exist")
    assert [i.source for i in M.enumerate_scope("desktop")] == ["user"]
    M.public_desktop_dir = lambda: d                          # 兩個位置指向同一目錄：不可重複列舉
    assert len(M.enumerate_scope("desktop")) == 1


def test_same_stem_lnk_and_url_do_not_collide(env):
    M = env.M
    d = os.path.join(env.home, "Desktop")
    env.mk(os.path.join(d, "Foo.lnk"))
    env.mk_url(os.path.join(d, "Foo.url"), icon="C:/x.ico")
    items = M.enumerate_scope("desktop")
    assert sorted(i.ico_name for i in items) == ["Foo_framed_glass.ico", "Foo_url_framed_glass.ico"]
    assert len({i.primary for i in items}) == 2


def test_url_only_accepted_on_desktop(env):
    env.mk_url(os.path.join(env.sm, "A.url"), icon="C:/x.ico")
    env.mk_url(os.path.join(env.tb, "B.url"), icon="C:/x.ico")
    assert env.M.enumerate_scope("startmenu") == [] and env.M.enumerate_scope("taskbar") == []


def test_public_apply_denied_reports_needs_admin(env, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    M = env.M
    d = os.path.join(env.home, "Desktop")
    env.mk(os.path.join(d, "Mine.lnk"))
    env.mk(os.path.join(env.pub, "Steam.lnk"))
    env.mk(os.path.join(env.pub, "FxSound.lnk"))
    res = M.run_batch("apply", ["desktop"], S.default_config())[0]
    assert res["applied"] == 1 and res["denied"] == 2 and res["needs_admin"] == 2
    # 母本照樣建在 PublicDesktop\；ICO 已渲染好，等提權後可直接寫入
    assert os.path.exists(os.path.join(M.BACKUP_DIR, "PublicDesktop", "Steam.lnk"))
    assert os.path.exists(os.path.join(M.ICON_VAULT_DIR, "Steam_pub_framed_glass.ico"))
    assert "需以系統管理員權限重試" in M.summarize([res], "apply") and "2" in M.summarize([res], "apply")


def test_needs_admin_not_counted_when_already_admin(env, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    M = env.M
    M.is_admin = lambda: True
    env.mk(os.path.join(env.pub, "Steam.lnk"))
    res = M.run_batch("apply", ["desktop"], S.default_config())[0]
    assert res["denied"] == 1 and res["needs_admin"] == 0       # 已是管理員還被拒：換身分也沒用


def test_admin_retry_only_touches_common_and_skips_rerender(env, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    M = env.M
    env.mk(os.path.join(env.home, "Desktop", "Mine.lnk"))
    env.mk(os.path.join(env.pub, "Steam.lnk"))
    M.run_batch("apply", ["desktop"], S.default_config())
    rendered = env.calls["render"]
    M.write_shortcut_icon = lambda p, ico: (                    # 模擬提權後可寫
        json.dump(dict(json.load(open(p, encoding="utf-8")), icon=ico), open(p, "w", encoding="utf-8")) or "ok")
    res = M.run_batch("apply", ["desktop"], S.default_config(), sources=("common",))[0]
    assert res["total"] == 1 and res["applied"] == 1 and res["denied"] == 0 and res["needs_admin"] == 0
    assert env.calls["render"] == rendered                      # ICO 已快取，不重畫
    assert json.load(open(os.path.join(env.pub, "Steam.lnk")))["icon"].endswith("Steam_pub_framed_glass.ico")


def test_restore_denied_in_public_counts_needs_admin(env, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    M = env.M
    p = os.path.join(env.pub, "Steam.lnk")
    env.mk(os.path.join(M.BACKUP_DIR, "PublicDesktop", "Steam.lnk"), target="C:/steam.exe")
    env.mk(p, icon=os.path.join(M.ICON_VAULT_DIR, "Steam_pub_framed_glass.ico"))
    real = M.shutil.copy2

    def deny(src, dst, *a, **kw):
        if os.path.normpath(dst) == os.path.normpath(p):
            raise PermissionError(5, "denied")
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(M.shutil, "copy2", deny)
    res = M.restore_scope("desktop", notify=False)
    assert res["restored"] == 0 and res["denied"] == 1 and res["needs_admin"] == 1


def test_windhawk_scopes_need_no_admin_while_public_desktop_does(env, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    _populate(env)
    env.mk(os.path.join(env.pub, "Steam.lnk"))
    res = {r["scope"]: r for r in env.M.run_batch("apply", list(S.SCOPES), S.default_config())}
    assert res["desktop"]["denied"] == 1 and res["desktop"]["needs_admin"] == 1
    assert res["startmenu"]["needs_admin"] == 0 and res["startmenu"]["applied"] == 1
    assert res["taskbar"]["needs_admin"] == 0 and res["taskbar"]["applied"] == 1


def test_denied_detection_covers_com_errors(env, tmp_path):
    M = env.M
    p = str(tmp_path / "x.lnk")
    assert M._is_denied(PermissionError(13, "no"), p)
    com_err = Exception(-2147352567, "例外", (0, None, None, None, 0, -2147024891), None)   # excepinfo[5]=E_ACCESSDENIED
    assert M._is_denied(com_err, p)
    assert M._is_denied(Exception(-2147024891), p)
    assert not M._is_denied(Exception(-2147220987, "other"), p)                # 可寫目錄 + 非存取拒絕


def test_cli_admin_flow(env, capsys, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    M = env.M
    env.mk(os.path.join(env.home, "Desktop", "Mine.lnk"))
    env.mk(os.path.join(env.pub, "Steam.lnk"))
    assert M.main(["--apply-admin", "desktop"]) == 1            # 非 Windows / 非管理員：提權不成，回傳 1 不崩潰
    assert M.main(["--apply-admin", "nonsense"]) == 2
    M.is_admin = lambda: True
    M.write_shortcut_icon = lambda p, ico: (
        json.dump(dict(json.load(open(p, encoding="utf-8")), icon=ico), open(p, "w", encoding="utf-8")) or "ok")
    assert M.main(["--apply-admin", "desktop"]) == 0
    assert json.load(open(os.path.join(env.pub, "Steam.lnk")))["icon"].endswith("_framed_glass.ico")
    assert json.load(open(os.path.join(env.home, "Desktop", "Mine.lnk")))["icon"] == ""   # 個人項目不在提權處理範圍
    out = json.load(open(M.ADMIN_RESULT_PATH, encoding="utf-8"))
    assert out["kind"] == "apply" and out["results"][0]["applied"] == 1
    assert M._parse_target(["--restore-admin"], "--restore-admin") == ("all", list(S.ACTIVE_SCOPES))


# ------------------------------------------------------------------ .url：解碼 / 解析 / 改寫
URL_BODY = ("[{000214A0-0000-0000-C000-000000000046}]\nProp3=19,0\n[InternetShortcut]\nIDList=\n"
            "URL=steam://rungameid/1\n; 註解 遊戲\nIconFile=C:\\old.ico\nIconIndex=3\n")


@pytest.mark.parametrize("enc,nl,bom", [("utf-8", "\r\n", False), ("utf-8-sig", "\r\n", True),
                                        ("cp950", "\n", False), ("utf-8", "\n", False)])
def test_url_rewrite_keeps_encoding_bom_newlines_and_other_lines(env, enc, nl, bom):
    M = env.M
    body = URL_BODY.replace("\n", nl)
    raw = body.encode(enc)
    text, used = M.decode_url_bytes(raw)
    assert used == enc
    assert M.rewrite_url_text(text, "C:\\v\\X_framed_glass.ico", 0).encode(used).startswith(b"\xef\xbb\xbf") == bom
    new = M.rewrite_url_text(text, "C:\\v\\X_framed_glass.ico", 0)
    a, b = body.split(nl), new.split(nl)
    assert len(a) == len(b)
    assert [x for x, y in zip(a, b) if x != y] == ["IconFile=C:\\old.ico", "IconIndex=3"]   # 只有這兩行不同
    assert M.rewrite_url_text(new, "C:\\old.ico", 3) == body                                    # 可逆
    assert "註解 遊戲".encode("cp950" if enc == "cp950" else "utf-8") in new.encode(used)


def test_url_decode_fallback_is_lossless(env):
    M = env.M
    raw = b"[InternetShortcut]\r\nURL=a\x81\x9d\xffz\r\n"       # 不是任何一種合法編碼
    text, enc = M.decode_url_bytes(raw)
    assert text.encode(enc) == raw                                # 無損往返，絕不損毀檔案
    assert M.decode_url_bytes(b"\xff\xfe[\x00I\x00")[1] == "utf-16"


def test_url_rewrite_inserts_missing_keys_in_right_section(env):
    M = env.M
    t = "[InternetShortcut]\r\nURL=http://a\r\n\r\n[Other]\r\nIconFile=keep\r\n"
    assert M.rewrite_url_text(t, "C:\\n.ico", 0) == (
        "[InternetShortcut]\r\nURL=http://a\r\nIconFile=C:\\n.ico\r\nIconIndex=0\r\n\r\n[Other]\r\nIconFile=keep\r\n")
    assert M.rewrite_url_text("URL=x\n", "i", 0).endswith("[InternetShortcut]\nIconFile=i\nIconIndex=0\n")
    only_file = "[InternetShortcut]\nIconFile = C:\\a.ico\nURL=u\n"                    # 鍵名後有空白、缺 IconIndex
    out = M.rewrite_url_text(only_file, "N", 5)
    assert "IconFile =N" in out and "IconIndex=5" in out and out.count("IconFile") == 1
    dup = "[InternetShortcut]\nIconFile=a\nIconFile=b\n"                                   # 重複鍵：全部同步，避免殘留
    assert M.rewrite_url_text(dup, "N", 0).count("IconFile=N") == 2


def test_url_parse_only_reads_internet_shortcut_section(env):
    M = env.M
    d = M.parse_url_text("[Other]\nIconFile=wrong\n[InternetShortcut]\nURL=u\nIconFile=right\n; IconIndex=9\n")
    assert d == {"url": "u", "icon_file": "right", "icon_index": None}
    assert M.parse_url_text("") == {"url": None, "icon_file": None, "icon_index": None}


def test_read_url_shortcut(env, tmp_path):
    M = env.M
    p = env.mk_url(str(tmp_path / "g.url"), icon='"C:\\icons\\a.ico"', idx=2, enc="cp950")
    i = M.read_url_shortcut(p)
    assert i.ok and i.icon_loc == "C:\\icons\\a.ico" and i.icon_idx == 2           # 引號被去除
    assert i.args == "steam://rungameid/1" and i.target == "" and i.newline == "\r\n"
    assert M.read_url_shortcut(env.mk_url(str(tmp_path / "n.url"))).icon_loc == ""   # 沒有 IconFile
    assert M.read_url_shortcut(env.mk_url(str(tmp_path / "b.url"), icon="x", idx="zz")).icon_idx == 0
    assert M.read_url_shortcut(str(tmp_path / "missing.url")).ok is False
    assert M.read_any_shortcut(p).icon_idx == 2                                       # 依副檔名分派


def test_write_url_shortcut_icon_ok_denied_and_unencodable(env, tmp_path, monkeypatch):
    M = env.M
    p = env.mk_url(str(tmp_path / "g.url"), icon="C:\\old.ico", idx=1)
    assert M.write_url_shortcut_icon(p, "C:\\vault\\g_url_framed_glass.ico") == "ok"
    j = M.read_url_shortcut(p)
    assert j.icon_loc.endswith("g_url_framed_glass.ico") and j.icon_idx == 0 and M.is_ours(j.icon_loc)
    before = open(p, "rb").read()
    assert M.write_url_shortcut_icon(p, "C:\\vault\\g_url_framed_glass.ico") == "ok"   # 已是目標值：不重寫
    assert open(p, "rb").read() == before
    # cp950 檔案寫入 cp950 無法表示的字元：回報 fail，檔案原封不動，不擅自轉碼
    q = env.mk_url(str(tmp_path / "c.url"), icon="C:\\old.ico", enc="cp950")
    qb = open(q, "rb").read()
    assert M.write_url_shortcut_icon(q, "C:\\vault\\😀.ico") == "fail" and open(q, "rb").read() == qb
    # 權限不足
    import builtins
    real_open = builtins.open

    def deny_open(path, mode="r", *a, **kw):
        if "w" in mode and str(path) == p:
            raise PermissionError(13, "denied")
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", deny_open)
    assert M.write_url_shortcut_icon(p, "C:\\vault\\other_framed_glass.ico") == "denied"


def test_url_full_apply_restore_cycle_is_byte_exact(env, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    M = env.M
    d = os.path.join(env.home, "Desktop")
    p = env.mk_url(os.path.join(d, "Balatro.url"), icon="C:\\Steam\\games\\b.ico", idx=0, enc="utf-8-sig")
    pub = env.mk_url(os.path.join(env.pub, "Web.url"), icon="C:\\Steam\\w.ico")
    original, original_pub = open(p, "rb").read(), open(pub, "rb").read()
    res = M.run_batch("apply", ["desktop"], S.default_config())[0]
    assert res["applied"] == 2 and res["denied"] == 0 and res["no_icon"] == 0
    bk = os.path.join(M.BACKUP_DIR, "Balatro.url")
    assert open(bk, "rb").read() == original                                   # 母本 = 乾淨原檔
    assert os.path.exists(os.path.join(M.BACKUP_DIR, "PublicDesktop", "Web.url"))
    now = open(p, "rb").read()
    assert now.startswith(b"\xef\xbb\xbf") and b"Prop3=19,0" in now and "註解 遊戲".encode() in now
    info = M.read_url_shortcut(p)
    assert info.icon_loc == os.path.join(M.ICON_VAULT_DIR, "Balatro_url_framed_glass.ico") and M.is_ours(info.icon_loc)
    base = dict(env.calls)
    res = M.run_batch("apply", ["desktop"], S.default_config())[0]               # 二次套用：全快取
    assert res["unchanged"] == 2 and res["applied"] == 0
    assert env.calls["render"] == base["render"] and env.calls["extract"] == base["extract"]
    res = M.run_batch("restore", ["desktop"], S.default_config())[0]
    assert res["restored"] == 2
    assert open(p, "rb").read() == original and open(pub, "rb").read() == original_pub   # 位元組級還原


def test_url_without_icon_file_is_no_icon(env, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    env.mk_url(os.path.join(env.home, "Desktop", "Plain.url"))                         # 沒有 IconFile
    env.M.extract_icon_256 = env.real_extract                                           # 用真的取圖
    res = env.M.run_batch("apply", ["desktop"], S.default_config())[0]
    assert res["no_icon"] == 1 and res["applied"] == 0


def test_extract_icon_256_for_url_reads_recorded_icon(env, tmp_path):
    M = env.M
    ico = str(tmp_path / "game.ico")
    R.sample_icons()[0].save(ico, format="ICO", sizes=[(64, 64)])
    url = env.mk_url(str(tmp_path / "g.url"), icon=ico)
    im = env.real_extract(url)
    assert im is not None and im.size == (256, 256) and im.mode == "RGBA"
    ours = env.mk_url(str(tmp_path / "o.url"), icon=os.path.join(M.ICON_VAULT_DIR, "o_glass.ico"))
    assert env.real_extract(ours) is None                                               # 指向本工具圖示者不可當原圖
    assert env.real_extract(env.mk_url(str(tmp_path / "m.url"), icon=str(tmp_path / "nope.ico"))) is None


def test_count_backups_includes_url(env):
    M = env.M
    env.mk(os.path.join(M.BACKUP_DIR, "A.lnk"))
    env.mk_url(os.path.join(M.BACKUP_DIR, "B.url"))
    open(os.path.join(M.BACKUP_DIR, "x.txt"), "w").write("")
    assert M.count_backups() == 2


# ------------------------------------------------------------------ MSIX / AUMID
def test_aumid_helpers(env):
    M = env.M
    a = "Canva.Affinity_8a0j1tnjnt4a4!Canva.Affinity"
    assert M.aumid_from_parsing_name("shell:AppsFolder\\" + a) == a
    assert M.aumid_from_parsing_name("SHELL:APPSFOLDER\\" + a) == a
    assert M.aumid_from_parsing_name("shell:::{4234d49b-0245-4df3-b780-3893943456e1}\\" + a) == a
    assert M.aumid_from_parsing_name("C:\\Windows\\notepad.exe") == "" and M.aumid_from_parsing_name("") == ""
    assert S.aumid_shell_path(a) == "shell:AppsFolder\\" + a


def test_extract_falls_back_to_shell_item_only_when_no_target_and_no_icon(env, tmp_path, monkeypatch):
    M = env.M
    sentinel = R.sample_icons()[0]
    seen = []
    monkeypatch.setattr(M, "extract_icon_from_shell_item", lambda p: seen.append(p) or sentinel)
    lnk = str(tmp_path / "Affinity.lnk")
    open(lnk, "w").write("x")
    empty = M.ShortcutInfo("", "", "", 0, True)                                          # TargetPath 與 IconLocation 皆空
    assert env.real_extract(lnk, empty) is sentinel and seen == [lnk]
    exe = str(tmp_path / "app.exe")
    open(exe, "w").write("x")
    seen.clear()
    env.real_extract(lnk, M.ShortcutInfo(exe, "", "", 0, True))                          # 有目標：不走 AUMID
    assert seen == []
    ours = M.ShortcutInfo("", "", os.path.join(M.ICON_VAULT_DIR, "A_glass.ico"), 0, True)
    assert env.real_extract(lnk, ours) is sentinel                                       # 圖示是我們的 → 視同無來源，仍走 AUMID


# ------------------------------------------------------------------ 母本過期自動更新
def test_backup_is_stale_rules(env, tmp_path):
    M = env.M
    live, bk = str(tmp_path / "live.lnk"), str(tmp_path / "bk.lnk")
    open(live, "w").write("a"); open(bk, "w").write("a")
    os.utime(bk, ns=(os.stat(live).st_atime_ns, os.stat(live).st_mtime_ns))
    I = M.ShortcutInfo
    base = I("C:/a.exe", "-x", "C:/a.ico", 0, True)
    assert not M.backup_is_stale(live, base, bk, base)                                   # 完全相同
    assert M.backup_is_stale(live, I("C:/b.exe", "-x", "C:/a.ico", 0, True), bk, base)   # 目標變
    assert M.backup_is_stale(live, I("C:/a.exe", "-x", "C:/b.ico", 0, True), bk, base)   # 圖示路徑變
    assert M.backup_is_stale(live, I("C:/a.exe", "-x", "C:/a.ico", 2, True), bk, base)   # 圖示索引變
    assert M.backup_is_stale(live, I("C:/a.exe", "-y", "C:/a.ico", 0, True), bk, base)   # 參數（.url 為網址）變
    if os.name == "nt":                                                                  # Windows 路徑不分大小寫與斜線方向
        assert not M.backup_is_stale(live, I("c:\\A.EXE", "-x", "C:\\a.ico", 0, True), bk, base)
    os.utime(live, ns=(1, 10 ** 9))
    assert M.backup_is_stale(live, base, bk, base)                                       # 只有 mtime 不同
    assert not M.backup_is_stale(live, M._EMPTY_INFO, bk, base)                          # 現場讀不到：不冒險覆寫
    assert M.backup_is_stale(bk, base, live, M._EMPTY_INFO)                              # 母本損毀：換新


def test_stale_backup_refresh_full_cycle(env, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    M = env.M
    d = os.path.join(env.home, "Desktop", "App.lnk")
    bk = os.path.join(M.BACKUP_DIR, "App.lnk")
    env.mk(d, target="C:/v1.exe")
    M.run_batch("apply", ["desktop"], S.default_config())
    assert json.load(open(bk))["target"] == "C:/v1.exe"
    time.sleep(0.02)
    env.mk(d, target="C:/v2.exe")                                                        # 軟體升級：捷徑被改寫回乾淨狀態
    before = env.calls["render"]
    r = M.run_batch("apply", ["desktop"], S.default_config())[0]
    assert json.load(open(bk))["target"] == "C:/v2.exe"                                  # 母本更新
    assert r["applied"] == 1 and r["backed_up"] == 1 and env.calls["render"] == before + 1   # 簽章變了 → 重新渲染
    assert not os.path.exists(bk + ".tmp")
    r = M.run_batch("apply", ["desktop"], S.default_config())[0]                         # 之後穩定：不再重複更新
    assert r["unchanged"] == 1 and r["backed_up"] == 0
    M.run_batch("restore", ["desktop"], S.default_config())
    assert json.load(open(d))["target"] == "C:/v2.exe" and json.load(open(d))["icon"] == ""   # 還原到最新版


def test_identical_clean_shortcut_does_not_refresh_backup(env, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    M = env.M
    env.mk(os.path.join(env.home, "Desktop", "App.lnk"))
    M.run_batch("apply", ["desktop"], S.default_config())
    M.restore_scope("desktop", notify=False)                                             # 還原後現場 == 母本（mtime 也相同）
    calls = []
    monkeypatch.setattr(M, "refresh_backup", lambda it: calls.append(it) or True)
    M.run_batch("apply", ["desktop"], S.default_config())
    assert calls == []


def test_touch_only_change_refreshes_backup(env, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    M = env.M
    d = os.path.join(env.home, "Desktop", "App.lnk")
    env.mk(d)
    M.run_batch("apply", ["desktop"], S.default_config())
    M.restore_scope("desktop", notify=False)
    bk = os.path.join(M.BACKUP_DIR, "App.lnk")
    st = os.stat(bk)
    os.utime(d, ns=(st.st_atime_ns, st.st_mtime_ns + 5 * 10 ** 9))                        # 內容特徵相同、只有 mtime 不同
    r = M.run_batch("apply", ["desktop"], S.default_config())[0]
    assert r["backed_up"] == 1 and r["applied"] == 1                                      # mtime 不同即視為過期，母本被更新
    assert json.load(open(d))["icon"].endswith("_framed_glass.ico")


def test_refresh_failure_keeps_old_backup_and_cleans_tmp(env, monkeypatch):
    M = env.M
    d = os.path.join(env.home, "Desktop", "App.lnk")
    bk = os.path.join(M.BACKUP_DIR, "App.lnk")
    env.mk(bk, target="C:/old.exe")
    env.mk(d, target="C:/new.exe")
    before = open(bk, "rb").read()
    real = M.shutil.copy2

    def boom(src, dst, *a, **kw):
        if str(dst).endswith(".tmp"):
            real(src, dst, *a, **kw)                                                     # 先寫出暫存檔再失敗，驗證清理
            raise OSError("disk full")
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(M.shutil, "copy2", boom)
    assert M.refresh_backup(M.enumerate_scope("desktop")[0]) is False
    assert open(bk, "rb").read() == before and not os.path.exists(bk + ".tmp")


def test_stale_refresh_in_public_desktop_and_url(env, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    M = env.M
    pub = os.path.join(env.pub, "Steam.lnk")
    url = os.path.join(env.home, "Desktop", "G.url")
    bks = {pub: os.path.join(M.BACKUP_DIR, "PublicDesktop", "Steam.lnk"),
           url: os.path.join(M.BACKUP_DIR, "G.url")}
    env.mk(pub, target="C:/s1.exe")
    env.mk_url(url, icon="C:/g.ico", url="steam://rungameid/1")
    for p, b in bks.items():
        os.makedirs(os.path.dirname(b), exist_ok=True)
        open(b, "wb").write(open(p, "rb").read())                                         # 先放一份舊母本
    time.sleep(0.02)
    env.mk(pub, target="C:/s2.exe")
    env.mk_url(url, icon="C:/g.ico", url="steam://rungameid/2")                           # 網址（args）變了
    M.run_batch("apply", ["desktop"], S.default_config())
    assert json.load(open(bks[pub]))["target"] == "C:/s2.exe"
    assert b"rungameid/2" in open(bks[url], "rb").read()


# ------------------------------------------------------------------ Watcher
def _w(fire=None, debounce=3.0):
    import LiquidGlassIconCenter as M
    M._SELF_WRITE["t"] = None
    return M, M.IconWatcher(fire or (lambda scopes: []), debounce_sec=debounce, clock=lambda: 0.0)


def test_watcher_debounce_merges_events_into_one_fire(env):
    M, w = _w()
    assert w.feed("desktop", "C:\\x\\a.lnk", now=100.0)
    assert not w.feed("taskbar", "C:\\x\\b.lnk", now=100.5)         # v2.0.0 工作列不走捷徑監聽
    assert not w.feed("startmenu", "C:\\x\\b.lnk", now=101.0)       # 開始功能表亦改走 Windhawk 即時引擎
    assert w.take_due(now=102.9) == []                              # 最後一次接受事件 100 + 3 = 103 才算安靜
    assert w.take_due(now=103.0) == ["desktop"]                     # 僅觸發桌面一次
    assert w.take_due(now=500.0) == []                              # 取走後清空


def test_watcher_each_new_event_restarts_the_quiet_timer(env):
    M, w = _w()
    w.feed("desktop", "a.lnk", now=0.0)
    w.feed("desktop", "b.lnk", now=2.5)
    assert w.take_due(now=3.0) == []                                # 第一個事件的 3 秒已過，但被第二個事件重新計時
    assert w.take_due(now=5.5) == ["desktop"]


def test_watcher_max_wait_bounds_continuous_events(env):
    M, w = _w()
    for t in range(0, 70, 2):
        w.feed("desktop", "a.lnk", now=1000.0 + t)
    assert w.take_due(now=1000.0 + 61.0) == ["desktop"]            # 事件不斷也最晚 WATCHER_MAX_WAIT_SEC 觸發


def test_watcher_filters_irrelevant_files(env):
    M, w = _w()
    j = lambda n: os.path.join("x", n)                              # noqa: E731  跨平台路徑
    assert not w.feed("desktop", j("a.txt"), now=1.0)
    assert not w.feed("desktop", j("desktop.ini"), now=1.0)
    assert not w.feed("desktop", j(M.SELF_SHORTCUT_NAME), now=1.0)
    assert not w.feed("taskbar", j("a.url"), now=1.0)               # 工作列不收 .url
    assert not w.feed("bogus", "a.lnk", now=1.0)
    assert w.feed("desktop", j("a.URL"), now=1.0)                   # 桌面收 .url，大小寫不拘
    assert not w.feed("startmenu", j("NewFolder"), now=1.0)         # startmenu 不走檔案監聽
    assert w.feed("desktop", None, now=1.0)                         # 事件緩衝溢位：整區重掃
    assert w.take_due(now=100.0) == ["desktop"]


def test_watcher_ignores_events_right_after_our_own_write(env):
    M, w = _w()
    M.mark_self_write(2000.0)
    assert not w.feed("desktop", "a.lnk", now=2000.0)
    assert not w.feed("desktop", "a.lnk", now=2004.9) and w.ignored == 2
    assert w.feed("desktop", "a.lnk", now=2005.0)                   # 滿 WATCHER_SELF_IGNORE_SEC 後恢復
    assert S.WATCHER_SELF_IGNORE_SEC == 5.0


def test_our_own_apply_marks_self_write_so_its_events_are_ignored(env, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    _populate(env)
    M = env.M
    M._SELF_WRITE["t"] = None
    w = M.IconWatcher(lambda s: [], debounce_sec=1.0)
    M.run_batch("apply", ["desktop"], S.default_config())
    assert M._SELF_WRITE["t"] is not None
    assert not w.feed("desktop", os.path.join(env.home, "Desktop", "Chrome.lnk"))   # 寫入造成的檔案事件被擋下
    assert w.take_due(now=time.monotonic() + 100) == []
    M._SELF_WRITE["t"] = None
    M.restore_scope("desktop", notify=False)                         # 還原同樣會標記
    assert M._SELF_WRITE["t"] is not None


def test_watcher_configure_clamps_debounce(env):
    M, w = _w(debounce=0.2)
    assert w.debounce_sec == 1.0
    w.configure(999)
    assert w.debounce_sec == 30.0
    w.configure("junk")
    assert w.debounce_sec == S.WATCHER_DEFAULTS["debounce_sec"]


def test_watcher_incremental_apply_only_renders_what_changed(env, monkeypatch):
    """Watcher 觸發的 apply 走快取：只處理新增 / 變更者，其餘 unchanged，0 次重渲染。"""
    _unlock_legacy_scopes(monkeypatch)
    _populate(env)
    M = env.M
    cfg = S.default_config()
    M.run_batch("apply", ["desktop", "startmenu"], cfg)
    fired = []
    w = M.IconWatcher(lambda scopes: fired.append(scopes) or M.run_batch("apply", scopes, cfg),
                      debounce_sec=1.0, clock=lambda: 0.0)
    M._SELF_WRITE["t"] = None
    base = dict(env.calls)
    env.mk(os.path.join(env.home, "Desktop", "Brand_New.lnk"))        # 新安裝的軟體
    env.mk_url(os.path.join(env.pub, "Game.url"), icon="C:/g.ico")
    assert w.feed("desktop", os.path.join(env.home, "Desktop", "Brand_New.lnk"), now=10.0)
    due = w.take_due(now=11.0)
    assert due == ["desktop"]
    res = w._fire(due)[0]
    assert res["applied"] == 2 and res["unchanged"] == 2           # 新增 2 個；原有 2 個快取命中
    assert env.calls["render"] - base["render"] == 2               # 只畫新的 2 張
    assert env.calls["extract"] - base["extract"] == 2


def test_watch_targets_cover_desktop_only(env):
    M = env.M
    t = M.watch_targets()
    got = {(x.scope, os.path.normpath(x.path), x.recursive) for x in t}
    assert ("desktop", os.path.normpath(os.path.join(env.home, "Desktop")), False) in got
    assert ("desktop", os.path.normpath(env.pub), False) in got
    assert all(x.scope == "desktop" for x in t)                  # 工作列與開始功能表不監看 .lnk
    os.rmdir(env.pub)
    assert all(os.path.normpath(x.path) != os.path.normpath(env.pub) for x in M.watch_targets())   # 不存在的不監看


def _run_thread_watcher(M, tmp_path, monkeypatch, fire, debounce=1.0):
    d = tmp_path / "Desk"
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(M, "scope_roots", lambda sc: [(str(d), "user", False)] if sc == "desktop" else [])
    M._SELF_WRITE["t"] = None
    w = M.IconWatcher(fire, debounce_sec=debounce)
    w.start()
    time.sleep(0.4)
    return d, w


def test_watcher_thread_debounces_real_file_events(env, tmp_path, monkeypatch):
    M = env.M
    got = []
    d, w = _run_thread_watcher(M, tmp_path, monkeypatch, lambda sc: got.append((time.monotonic(), sc)) or [])
    try:
        t_first = time.monotonic()
        for i in range(3):                                          # 連續三次建立：只能觸發一次
            (d / f"n{i}.lnk").write_text("x")
            time.sleep(0.4)
        t_last = time.monotonic()
        time.sleep(5.0)
    finally:
        w.stop()
    assert len(got) == 1 and got[0][1] == ["desktop"], got
    assert got[0][0] - t_last >= 0.5                                # 最後一次事件之後才觸發（至少接近 debounce）
    assert got[0][0] > t_first
    assert not w.running


def test_watcher_thread_ignores_changes_caused_by_our_own_write(env, tmp_path, monkeypatch):
    M = env.M
    got = []
    d, w = _run_thread_watcher(M, tmp_path, monkeypatch, lambda sc: got.append(sc) or [])
    try:
        M.mark_self_write()                                          # 模擬本工具剛寫完捷徑
        (d / "ours.lnk").write_text("x")
        time.sleep(3.5)
    finally:
        w.stop()
    assert got == [] and w.ignored >= 1


def test_watcher_thread_retries_when_apply_is_busy(env, tmp_path, monkeypatch):
    M = env.M
    seen = []

    def fire(scopes):
        seen.append(time.monotonic())
        return None if len(seen) == 1 else []                       # 第一次回報忙碌（None）

    d, w = _run_thread_watcher(M, tmp_path, monkeypatch, fire)
    try:
        (d / "a.lnk").write_text("x")
        time.sleep(6.0)
    finally:
        w.stop()
    assert len(seen) == 2 and seen[1] - seen[0] >= 0.9              # 重新排隊並再等一次去抖，不丟事件


def test_watcher_stop_is_idempotent_and_restartable(env, tmp_path, monkeypatch):
    M = env.M
    d, w = _run_thread_watcher(M, tmp_path, monkeypatch, lambda sc: [])
    assert w.running
    w.stop()
    w.stop()
    assert not w.running
    w.start()
    assert w.running
    w.stop()


# ------------------------------------------------------------------ Watcher 設定與系統匣整合
def test_app_starts_and_stops_watcher_from_config(env, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    M = env.M

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

    monkeypatch.setattr(M, "IconWatcher", Fake)
    app = M.LiquidGlassApp()
    app.apply_watcher_config()
    assert app.watcher is None and Fake.instances == []              # 預設關閉
    app.config["watcher"] = {"enabled": True, "debounce_sec": 5.0}
    app.apply_watcher_config()
    app.apply_watcher_config()                                       # 重複套用不會重複啟動
    w = Fake.instances[0]
    assert len(Fake.instances) == 1 and w.started == 1 and w.debounce == 5.0
    app.config["watcher"] = {"enabled": True, "debounce_sec": 9.0}
    app.apply_watcher_config()
    assert w.debounce == 9.0 and w.started == 1
    app.config["watcher"] = {"enabled": False, "debounce_sec": 9.0}
    app.apply_watcher_config()
    assert w.stopped == 1 and app.watcher is None
    app._toggle_watcher()
    assert json.load(open(M.CONFIG_PATH))["watcher"]["enabled"] is True and app.watcher is not None


def test_watcher_setting_survives_params_save_from_another_process(env):
    M = env.M
    tray = M.LiquidGlassApp()
    tray.config["watcher"] = {"enabled": True, "debounce_sec": 5.0}
    tray.save_config(watcher_changed=True)
    window = M.LiquidGlassApp()                                      # 設定視窗行程：載入後才由系統匣改了開關
    window.config["watcher"] = {"enabled": False, "debounce_sec": 3.0}   # 它手上的是舊值
    window.config["taskbar"]["glow"] = 1.5
    window.save_config()                                             # 存參數不可把 watcher 蓋回舊值
    saved = json.load(open(M.CONFIG_PATH))
    assert saved["watcher"] == {"enabled": True, "debounce_sec": 5.0} and saved["taskbar"]["glow"] == 1.5


def test_api_set_watcher_persists_and_spawns_tray(env, monkeypatch):
    _unlock_legacy_scopes(monkeypatch)
    M = env.M
    spawned = []
    monkeypatch.setattr(M, "spawn_background_tray", lambda: spawned.append(1))
    monkeypatch.setattr(M, "is_tray_running", lambda: False)
    api = M.LiquidGlassApi(M.LiquidGlassApp())
    assert api.get_state()["watcher"] is False
    assert api.set_watcher(True) is True and spawned == [1]
    assert json.load(open(M.CONFIG_PATH))["watcher"]["enabled"] is True
    assert api.get_state()["watcher"] is True and api.get_state()["watcher_config"]["enabled"] is True
    monkeypatch.setattr(M, "is_tray_running", lambda: True)
    assert api.set_watcher_debounce(999) == 30.0                     # 夾在合法範圍
    assert api.set_watcher(False) is False and spawned == [1]        # 關閉或系統匣已在跑：不再啟動
    assert api.apply_admin("bogus")["ok"] is False
    M.is_admin = lambda: True
    assert api.apply_admin("desktop")["ok"] is False                 # 已是管理員：不需再提權


# ====================================================================== 滿版外緣零填充 & 桌面光學轉換 + 開始功能表封鎖
def test_optical_full_bleed_square_retains_outer_rim_on_all_edges():
    full = Image.new("RGBA", (256, 256), (130, 210, 70, 255))
    ImageDraw.Draw(full).rectangle([80, 70, 176, 186], fill=(15, 18, 15, 255))
    out = np.asarray(R.render_preview("desktop", full, S.default_params("desktop"), 48), dtype=np.float32) / 255.0
    assert out[..., 3].max() > 0.50
    # 滿版方塊的上下左右四條直邊中間都不能斷線（零填充 Alpha 模糊確保四邊皆有亮框）
    assert out[24, 0:3, 3].max() > 0.35 and out[24, -3:, 3].max() > 0.35
    assert out[0:3, 24, 3].max() > 0.35 and out[-3:, 24, 3].max() > 0.35
    # 中央深暗鏤空仍保持超清透
    assert out[24, 24, 3] < 0.12


def test_desktop_optical_glass_matches_taskbar_and_never_solid_white():
    disc = _solid_disc()
    for sz in (48, 64, 256):
        t = np.asarray(R.render_preview("taskbar", disc, S.default_params("taskbar"), sz), dtype=np.float32) / 255.0
        d = np.asarray(R.render_preview("desktop", disc, S.default_params("desktop"), sz), dtype=np.float32) / 255.0
        assert np.allclose(t, d)
        c = sz // 2
        core = d[c - max(2, sz // 6):c + max(2, sz // 6), c - max(2, sz // 6):c + max(2, sz // 6), 3]
        assert core.mean() < 0.25 and d[..., 3].max() > core.mean() * 2.0


def test_all_three_scopes_active_and_unlocked(env, monkeypatch):
    _populate(env)
    M = env.M
    import windhawk_bridge as WB
    d_lnk = os.path.join(env.home, "Desktop", "Chrome.lnk")
    sm_lnk = os.path.join(env.sm, "Tools", "Editor.lnk")
    sm_before = open(sm_lnk, "rb").read()

    assert S.ACTIVE_SCOPES == ("taskbar", "startmenu", "desktop")
    assert S.LOCKED_SCOPES == ()
    for sc in S.SCOPES:
        assert not S.is_scope_locked(sc)
    assert S.is_scope_locked("bogus")

    res = M.run_batch("apply", ["desktop", "startmenu", "taskbar"], S.default_config())
    r_map = {r["scope"]: r for r in res}
    assert not r_map["startmenu"].get("locked") and r_map["startmenu"]["applied"] == 1
    assert not r_map["taskbar"].get("locked") and r_map["taskbar"]["applied"] == 1
    assert not r_map["desktop"].get("locked") and r_map["desktop"]["applied"] == 2
    assert open(sm_lnk, "rb").read() == sm_before
    assert json.load(open(d_lnk))["icon"].endswith("_framed_glass.ico")
    assert WB.get_status(scope="startmenu")["enabled"] is True

    res_r = M.run_batch("restore", ["desktop", "startmenu", "taskbar"], S.default_config())
    rr_map = {r["scope"]: r for r in res_r}
    assert rr_map["startmenu"]["restored"] == 1 and open(sm_lnk, "rb").read() == sm_before
    assert rr_map["desktop"]["restored"] == 2
    assert WB.get_status(scope="startmenu")["enabled"] is False


def test_api_unlocks_all_three_scopes_and_exposes_engines(env, monkeypatch):
    M = env.M
    monkeypatch.setattr(M, "spawn_background_tray", lambda: None)
    monkeypatch.setattr(M, "is_tray_running", lambda: False)
    app = M.LiquidGlassApp()
    api = M.LiquidGlassApi(app)
    st = api.get_state()
    assert st["version"] == S.VERSION
    assert st["default_scope"] == "taskbar"
    assert st["locked_scopes"] == []
    for sc in S.SCOPES:
        assert st["specs"][sc]["locked"] is False
    assert "startmenu_engine" in st and st["startmenu_engine"]["scope"] == "startmenu"
    assert api.get_engine_status("startmenu")["scope"] == "startmenu"
    assert st["watcher_locked"] is False

    # 三區套用與還原皆可使用
    assert api.apply_scope("startmenu", S.default_params("startmenu"))["ok"] is True
    assert api.restore_scope("startmenu")["ok"] is True
    assert api.apply_all({sc: S.default_params(sc) for sc in S.SCOPES})["ok"] is True
    assert api.restore_all()["ok"] is True

    # 開放桌面後，桌面即時監聽可正常啟用並存入設定
    assert api.set_watcher(True) is True
    assert json.load(open(M.CONFIG_PATH))["watcher"]["enabled"] is True

    # 預覽對三區皆有效，各回傳 4 張 PNG Data URI
    for sc in S.SCOPES:
        pv = api.preview(sc, S.default_params(sc), 1.5, 7)
        assert pv["seq"] == 7 and pv["scope"] == sc and len(pv["big"]) == 4 and len(pv["actual"]) == 4
        assert all(u.startswith("data:image/png;base64,") for u in pv["big"] + pv["actual"])


def test_cli_supports_all_three_scopes_and_rejects_bogus(env):
    M = env.M
    assert M._parse_target(["--apply"], "--apply") == ("all", ["taskbar", "startmenu", "desktop"])
    assert M._parse_target(["--restore", "all"], "--restore") == ("all", ["taskbar", "startmenu", "desktop"])
    assert M.main(["--apply", "bogus"]) == 2
    assert M.main(["--restore", "bogus"]) == 2
    assert M.main(["--apply", "startmenu"]) == 0
    import windhawk_bridge as WB
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


def test_windhawk_bridge_ini_settings_restore_and_status(env):
    import windhawk_bridge as WB
    for sc, target_proc, telem_name in (
        ("taskbar", "explorer.exe", "lgic_windhawk_status.json"),
        ("startmenu", "StartMenuExperienceHost.exe", "lgic_windhawk_startmenu_status.json"),
    ):
        r1 = WB.apply_windhawk_mod(sc, {"opacity": 0.85, "frost": 1.15, "glow": 1.40, "dark_protect": 0.90})
        assert r1["ok"] is True and r1["compiled"] is True
        ini = WB.read_mod_ini(WB.resolve_windhawk_paths(scope=sc)["ini_path"])
        assert ini["mod"]["Disabled"] == "0"
        assert ini["mod"]["Include"] == target_proc
        assert ini["settings"] == {"Enabled": "1", "Opacity": "85", "Frost": "115", "Glow": "140", "DarkProtect": "90"}

        # 熱更新滑桿設定（僅在啟用時同步）
        sync1 = WB.update_settings_if_active({"opacity": 1.0, "frost": 0.95, "glow": 1.25, "dark_protect": 1.1}, scope=sc)
        assert sync1 == {"ok": True, "synced": True}
        assert WB.read_mod_ini(WB.resolve_windhawk_paths(scope=sc)["ini_path"])["settings"]["Glow"] == "125"

        # Telemetry JSON 讀取
        telem_path = os.path.join(env.temp_dir, telem_name)
        with open(telem_path, "w", encoding="utf-8") as tf:
            json.dump({"active": True, "tracked_buttons": 12, "cache_entries": 9, "cache_bytes": 40960}, tf)
        st = WB.get_status(scope=sc)
        assert st["enabled"] is True and st["telemetry"]["tracked_buttons"] == 12

        # 停用還原 + 停用後不再同步滑桿
        r2 = WB.restore_windhawk_mod(sc)
        assert r2["ok"] is True
        assert WB.read_mod_ini(WB.resolve_windhawk_paths(scope=sc)["ini_path"])["mod"]["Disabled"] == "1"
        assert WB.update_settings_if_active({"opacity": 0.5}, scope=sc) == {"ok": True, "synced": False}
    # 確認工作目錄與 IconVault 未被寫入任何 .ico/.png 暫存
    assert os.listdir(env.M.ICON_VAULT_DIR) == []


def test_api_apply_scope_exposes_top_level_needs_admin_and_admin_result(env, monkeypatch):
    M = env.M
    monkeypatch.setattr(M, "spawn_background_tray", lambda: None)
    monkeypatch.setattr(M, "is_tray_running", lambda: False)
    env.mk(os.path.join(env.home, "Desktop", "Mine.lnk"))
    env.mk(os.path.join(env.pub, "Steam.lnk"))
    real_write = M.write_any_shortcut

    def deny_pub(path, ico):
        if str(env.pdata) in path:
            return "denied"
        return real_write(path, ico)

    monkeypatch.setattr(M, "write_any_shortcut", deny_pub)
    api = M.LiquidGlassApi(M.LiquidGlassApp())
    out = api.apply_scope("desktop", S.default_params("desktop"))
    assert out["ok"] is True and out["kind"] == "apply" and out["needs_admin"] == 1
    assert api.get_admin_result() == {}
    M.atomic_write_json(M.ADMIN_RESULT_PATH, {"kind": "apply", "message": "done", "time": 123.0})
    assert api.get_admin_result()["message"] == "done"


def test_url_wide_section_decode_and_repair(env, tmp_path, monkeypatch):
    M = env.M
    raw = (
        b"[InternetShortcut]\r\nURL=steam://rungameid/1\r\n"
        b"IconFile=C:\\Mojibake\\path.ico\r\nIconIndex=0\r\n"
        b"[InternetShortcut.W]\r\n"
        + ("IconFile=" + "C:\\任務\\圖示_url_framed_glass.ico".encode("utf-7").decode("ascii") + "\r\n").encode("ascii")
    )
    p = tmp_path / "Game.url"
    p.write_bytes(raw)
    info = M.read_url_shortcut(str(p))
    assert info.icon_loc == "C:\\任務\\圖示_url_framed_glass.ico" and info.icon_idx == 0

    # 若既有的非 ASCII .url 指向我們的 IconVault 但缺 [InternetShortcut.W]，下次套用會自動補齊而非略過
    u = os.path.join(env.home, "Desktop", "Broken.url")
    ico_dir = os.path.join(str(tmp_path), "任務圖示")
    os.makedirs(ico_dir, exist_ok=True)
    monkeypatch.setattr(M, "ICON_VAULT_DIR", ico_dir)
    env.mk_url(u, icon="C:\\Steam\\g.ico")
    monkeypatch.setattr(M, "write_url_shortcut_icon", lambda path, ico, idx=0: M._orig_write_url(path, ico, idx) if hasattr(M, "_orig_write_url") else "ok")
    res_first = M.run_batch("apply", ["desktop"], S.default_config())[0]
    assert res_first["applied"] == 1
    # 寫成缺 [InternetShortcut.W] 的舊式 UTF-8 狀態並更新 cache
    ico_target = M.enumerate_scope("desktop")[0].ico_path
    with open(u, "wb") as f:
        f.write(f"[InternetShortcut]\r\nURL=steam://rungameid/1\r\nIconFile={ico_target}\r\nIconIndex=0\r\n".encode("utf-8"))
    if M.IS_WIN:
        assert not M._url_has_wide_section(u)
        calls = []
        monkeypatch.setattr(M, "write_any_shortcut", lambda path, ico: (calls.append(path) or "ok"))
        M.run_batch("apply", ["desktop"], S.default_config())
        assert calls == [u]


def test_parse_startup_scope_and_webview_storage_path(env, monkeypatch):
    M = env.M
    assert M._parse_startup_scope(["--settings", "--scope", "desktop"]) == "desktop"
    assert M._parse_startup_scope(["--settings", "--scope=taskbar"]) == "taskbar"
    assert M._parse_startup_scope(["--settings", "--scope", "startmenu"]) == "startmenu"
    assert M._parse_startup_scope(["--settings", "--scope", "bogus"]) == ""
    assert M._parse_startup_scope([]) == ""

    monkeypatch.setattr(M, "PROFILE_DIR", "C:\\tmp\\.webview_profile")
    monkeypatch.setattr(M, "is_admin", lambda: False)
    assert M._webview_storage_path() == "C:\\tmp\\.webview_profile"
    monkeypatch.setattr(M, "is_admin", lambda: True)
    assert M._webview_storage_path() == "C:\\tmp\\.webview_profile_admin"

    app = M.LiquidGlassApp()
    api = M.LiquidGlassApi(app)
    assert api.get_startup_scope() == ""
    app.initial_scope = "desktop"
    assert api.get_startup_scope() == "desktop"
    app.initial_scope = "startmenu"
    assert api.get_startup_scope() == "startmenu"
    app.initial_scope = "bogus"
    assert api.get_startup_scope() == ""


def test_api_relaunch_admin_wiztree_banner(env, monkeypatch):
    M = env.M
    import threading
    calls = []

    class DummyWin:
        def __init__(self):
            self.evt = threading.Event()
        def destroy(self):
            self.evt.set()

    app = M.LiquidGlassApp()
    app.window = DummyWin()
    api = M.LiquidGlassApi(app)

    # 已是系統管理員：不重複提權
    monkeypatch.setattr(M, "is_admin", lambda: True)
    monkeypatch.setattr(M, "relaunch_as_admin", lambda args, show=0: calls.append((args, show)) or True)
    r_adm = api.relaunch_admin("desktop")
    assert r_adm["ok"] is False and r_adm["already_admin"] is True and calls == []

    # 非管理員但 UAC 被取消：保留目前視窗
    monkeypatch.setattr(M, "is_admin", lambda: False)
    monkeypatch.setattr(M, "relaunch_as_admin", lambda args, show=0: calls.append((args, show)) or False)
    r_cancel = api.relaunch_admin("desktop")
    assert r_cancel["ok"] is False and not app.window.evt.is_set()
    assert calls == [(["--settings", "--scope", "desktop"], 1)]

    # 非管理員且授權成功：以 SW_SHOWNORMAL + 合法 scope（非法回退 taskbar）啟動，並排程關閉舊視窗
    calls.clear()
    monkeypatch.setattr(M, "relaunch_as_admin", lambda args, show=0: calls.append((args, show)) or True)
    assert api.relaunch_admin("startmenu")["scope"] == "startmenu"
    assert calls[-1] == (["--settings", "--scope", "startmenu"], 1)
    assert api.relaunch_admin("bogus")["scope"] == "taskbar"
    assert calls[-1] == (["--settings", "--scope", "taskbar"], 1)
    assert app.window.evt.wait(1.5)

    # main --scope 解析傳遞至 open_settings_window
    seen = []
    monkeypatch.setattr(M.LiquidGlassApp, "open_settings_window", lambda self, initial_scope="": seen.append(initial_scope))
    assert M.main(["--settings", "--scope", "desktop"]) == 0
    assert seen == ["desktop"]


def test_ui_html_has_wiztree_non_blocking_banner(env):
    with open(env.M.UI_HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()
    for token in (
        "elevateBanner", "btnElevateRelaunch", "adminPill", "relaunch_admin",
        "get_startup_scope", "is_admin", "btnApplyAll", "btnRestoreAll",
        "engCardTitle", "renderEngineTelemetry", "get_engine_status",
        "whOptionalBox", "visualAdvisoryBox", "btnRecheckWh", "btnCopyWhUrl",
        "isWhAvailable", "optional-off", "僅桌面模式",
    ):
        assert token in html
    for stale in ("桌面・開始功能表目前封鎖", "開始功能表封鎖中", "開始功能表暫未開放"):
        assert stale not in html


def test_windhawk_auto_detection_and_standard_storage(env, tmp_path, monkeypatch):
    import windhawk_bridge as WB
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

    # 純記憶體模擬 winreg：驗證標準安裝版的登錄檔讀寫、熱更新、還原與權限不足回報
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

    # 無系統管理員權限寫入 HKLM 時：回報 needs_admin=True，不拋出未處理例外
    deny_write["flag"] = True
    r_denied = WB.apply_windhawk_mod("startmenu", S.default_params("startmenu"))
    assert r_denied["ok"] is False and r_denied.get("needs_admin") is True


def test_optional_extension_fallback_without_windhawk(env, tmp_path, monkeypatch, capsys):
    M = env.M
    import windhawk_bridge as WB
    missing_wh = tmp_path / "absent_windhawk"
    monkeypatch.setenv("LGIC_WINDHAWK_DIR", str(missing_wh))
    env.mk(os.path.join(env.home, "Desktop", "Chrome.lnk"))
    env.mk(os.path.join(env.home, "Desktop", "Notes.lnk"))

    app = M.LiquidGlassApp()
    api = M.LiquidGlassApi(app)
    st = api.get_state()
    assert st["windhawk_installed"] is False
    assert st["default_scope"] == "desktop"
    assert st["available_scopes"] == ["desktop"]
    assert st["optional_scopes"] == ["taskbar", "startmenu"]
    assert st["specs"]["desktop"]["requires_windhawk"] is False
    assert st["specs"]["taskbar"]["requires_windhawk"] is True and "深色模式" in st["specs"]["taskbar"]["advisory"]
    assert st["specs"]["startmenu"]["requires_windhawk"] is True and "深色模式" in st["specs"]["startmenu"]["advisory"]

    # 未安裝 Windhawk 時，一鍵套用／還原全部自動僅處理獨立的桌面區，零報錯
    r_all = api.apply_all({sc: S.default_params(sc) for sc in S.SCOPES})
    assert r_all["ok"] is True and [r["scope"] for r in r_all["results"]] == ["desktop"]
    assert r_all["results"][0]["applied"] == 2
    r_rest = api.restore_all()
    assert r_rest["ok"] is True and r_rest["results"][0]["restored"] == 2

    # CLI --apply（全區模式）亦乾淨跳過選用擴充
    assert M.main(["--apply"]) == 0
    out = capsys.readouterr().out
    assert "桌面：更新 2" in out and "略過選用擴充" in out

    # 明確呼叫缺少 Windhawk 的選用區時，給予友善選用提示
    r_tb = api.apply_scope("taskbar", S.default_params("taskbar"))
    assert r_tb["ok"] is False and r_tb["results"][0].get("windhawk_missing") == 1
    assert "選用擴充" in r_tb["message"]
    assert WB.is_windhawk_installed() is False


def test_lifecycle_config_and_restore_on_exit(env, monkeypatch):
    _populate(env)
    M = env.M
    import windhawk_bridge as WB
    assert S.clamp_lifecycle("junk") == S.default_lifecycle() == {
        "restore_live_on_exit": False,
        "exit_when_window_closes": False,
    }
    assert S.clamp_lifecycle({"restore_live_on_exit": 1, "exit_when_window_closes": "x"}) == {
        "restore_live_on_exit": True,
        "exit_when_window_closes": False,
    }

    # 跨行程存檔隔離：改 watcher 不蓋掉 lifecycle；改 lifecycle 不蓋掉 watcher
    tray = M.LiquidGlassApp()
    tray.config["lifecycle"] = {"restore_live_on_exit": True, "exit_when_window_closes": False}
    tray.save_config(lifecycle_changed=True)
    win_app = M.LiquidGlassApp()
    win_app.config["lifecycle"] = {"restore_live_on_exit": False, "exit_when_window_closes": False}
    win_app.config["watcher"] = {"enabled": True, "debounce_sec": 6.0}
    win_app.save_config(watcher_changed=True)
    saved = json.load(open(M.CONFIG_PATH, encoding="utf-8"))
    assert saved["lifecycle"] == {"restore_live_on_exit": True, "exit_when_window_closes": False}
    assert saved["watcher"] == {"enabled": True, "debounce_sec": 6.0}

    # API 讀寫 lifecycle 與 exit_when_window_closes=False 時補起系統匣
    spawned = []
    monkeypatch.setattr(M, "spawn_background_tray", lambda: spawned.append(1))
    monkeypatch.setattr(M, "is_tray_running", lambda: False)
    monkeypatch.setattr(M, "is_admin", lambda: False)
    api = M.LiquidGlassApi(M.LiquidGlassApp())
    assert api.set_restore_on_exit(True) is True
    assert api.set_exit_on_close(True) is True and spawned == []
    assert api.set_exit_on_close(False) is False and spawned == [1]
    st = api.get_state()
    assert st["restore_on_exit"] is True and st["exit_on_close"] is False

    # UI 頁面確認含生命週期開關識別碼
    with open(M.UI_HTML_PATH, "r", encoding="utf-8") as f:
        html = f.read()
    for token in ("swExitOnClose", "swRestoreOnExit", "set_exit_on_close", "set_restore_on_exit"):
        assert token in html

    # 套用三區，準備驗證各種退出行為（桌面 .lnk 必須維持不動）
    M.run_batch("apply", ["desktop", "startmenu", "taskbar"], S.default_config())
    d_lnk = os.path.join(env.home, "Desktop", "Chrome.lnk")
    d_before = open(d_lnk, "rb").read()
    assert WB.get_status(scope="taskbar")["enabled"] is True
    assert WB.get_status(scope="startmenu")["enabled"] is True

    app = M.LiquidGlassApp()
    app.config["lifecycle"] = {"restore_live_on_exit": True, "exit_when_window_closes": False}
    app.save_config(lifecycle_changed=True)

    # 1. 管理員提權重啟交接期：絕不觸發還原或停匣
    stop_calls = []
    monkeypatch.setattr(M, "_signal_tray_stop", lambda: stop_calls.append(1) or True)
    app._relaunching_admin = True
    app.handle_window_closed()
    assert WB.get_status(scope="taskbar")["enabled"] is True and stop_calls == []
    app._relaunching_admin = False

    # 2. 系統匣仍在背景常駐且未勾 exit_when_window_closes：關設定窗時保持透明
    monkeypatch.setattr(M, "is_tray_running", lambda: True)
    app.handle_window_closed()
    assert WB.get_status(scope="taskbar")["enabled"] is True and WB.get_status(scope="startmenu")["enabled"] is True

    # 3. 系統匣未執行（例如管理員視窗）且開啟 restore_live_on_exit：關窗即還原即時區、不動桌面
    monkeypatch.setattr(M, "is_tray_running", lambda: False)
    app.handle_window_closed()
    assert WB.get_status(scope="taskbar")["enabled"] is False
    assert WB.get_status(scope="startmenu")["enabled"] is False
    assert open(d_lnk, "rb").read() == d_before

    # 4. 重新套用並啟用 exit_when_window_closes + restore_live_on_exit：關窗時通知系統匣結束並還原即時區
    M.run_batch("apply", ["startmenu", "taskbar"], S.default_config())
    app.config["lifecycle"] = {"restore_live_on_exit": True, "exit_when_window_closes": True}
    app.save_config(lifecycle_changed=True)
    app.handle_window_closed()
    assert stop_calls == [1]
    assert WB.get_status(scope="taskbar")["enabled"] is False
    assert WB.get_status(scope="startmenu")["enabled"] is False
    assert open(d_lnk, "rb").read() == d_before

    # 5. quit_app(restore=False/None/True) 驗證
    exit_codes = []
    monkeypatch.setattr(M.os, "_exit", lambda code=0: exit_codes.append(code))
    M.run_batch("apply", ["startmenu", "taskbar"], S.default_config())
    app.quit_app(restore=False)
    assert WB.get_status(scope="taskbar")["enabled"] is True and exit_codes == [0]
    app.quit_app(restore=True)
    assert WB.get_status(scope="taskbar")["enabled"] is False
    assert WB.get_status(scope="startmenu")["enabled"] is False
    assert open(d_lnk, "rb").read() == d_before
    assert exit_codes == [0, 0]


def test_stale_mod_dll_cleanup_and_win32_process_probe(env, monkeypatch):
    import windhawk_bridge as WB
    paths = WB.resolve_windhawk_paths(scope="taskbar")
    mods64 = paths["mods64_dir"]
    stale = os.path.join(mods64, "local@liquid-glass-taskbar-icons_1.0.0_000001.dll")
    keep = os.path.join(mods64, "local@liquid-glass-taskbar-icons_1.0.0_123456.dll")
    other = os.path.join(mods64, "taskbar-on-top_1.3.1_321078.dll")
    for p in (stale, keep, other):
        open(p, "wb").write(b"MZ")
    removed = WB._cleanup_stale_mod_dlls(paths, "taskbar", "local@liquid-glass-taskbar-icons_1.0.0_123456.dll")
    assert removed == 1
    assert not os.path.exists(stale) and os.path.exists(keep) and os.path.exists(other)

    # 驗證 Win32 Toolhelp32 行程偵測：現存 python.exe 必為 True，不存在名稱必為 False
    if os.name == "nt":
        cur_exe = os.path.basename(sys.executable)
        assert WB._is_process_running_win32(cur_exe) is True
        assert WB._is_process_running_win32("__nonexistent_lgic_proc__.exe") is False
        assert isinstance(WB.is_windhawk_running(), bool)
