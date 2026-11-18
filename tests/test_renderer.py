"""tests/test_renderer.py -- 光學 Liquid Glass 算圖數學與 ICO 輸出驗證。"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from conftest import solid_disc, white_disc
from liquid_glass import renderer as R
from liquid_glass import specs as S


def _bbox_ratio(im: Image.Image, canvas: int = 256) -> float:
    a = np.asarray(im.getchannel("A"))
    ys, xs = np.nonzero(a > 24)
    return max(xs.max() - xs.min() + 1, ys.max() - ys.min() + 1) / canvas


def test_bbox_normalization_makes_sizes_consistent():
    ratios = [_bbox_ratio(R.normalize_glyph_bbox(im, 0.68)) for im in R.sample_icons()]
    for r in ratios:
        assert abs(r - 0.68) < 0.02, ratios


def test_bbox_normalization_upscale_cap():
    tiny = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    ImageDraw.Draw(tiny).rectangle([120, 120, 135, 135], fill=(255, 0, 0, 255))
    out = R.normalize_glyph_bbox(tiny, 0.8)
    assert _bbox_ratio(out) * 256 <= 16 * 3 + 2


def test_bbox_ignores_faint_shadow():
    im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    ImageDraw.Draw(im).rectangle([0, 0, 255, 255], fill=(0, 0, 0, 10))
    ImageDraw.Draw(im).rectangle([100, 100, 155, 155], fill=(255, 0, 0, 255))
    assert abs(_bbox_ratio(R.normalize_glyph_bbox(im, 0.5)) - 0.5) < 0.02


def test_taskbar_never_becomes_white_block():
    disc = solid_disc()
    white = white_disc()
    for params in (
        S.default_params("taskbar"),
        {"opacity": 99, "frost": 99, "glow": 99, "dark_protect": -5},
    ):
        p = S.clamp_params("taskbar", params)
        assert p["opacity"] <= 1.8 and p["frost"] <= 1.3 and p["glow"] <= 1.8 and p["dark_protect"] >= 0.5
        for size in (16, 24, 32, 36, 48, 64):
            rgba = np.asarray(R.render_preview("taskbar", disc, p, size), dtype=np.float32) / 255.0
            a = rgba[..., 3]
            vis = a > 0.02
            assert vis.any()
            assert rgba[vis, :3].min() >= 0.92
            c = size // 2
            core = a[c - max(1, size // 6) : c + max(1, size // 6), c - max(1, size // 6) : c + max(1, size // 6)]
            assert core.mean() < 0.45, (size, core.mean())
            assert a.max() > core.mean()
        w_rgba = np.asarray(R.render_preview("taskbar", white, p, 36), dtype=np.float32) / 255.0
        assert w_rgba[..., 3].max() <= 0.925


def test_taskbar_optical_36px_fresnel_and_cutout():
    samples = R.sample_icons()
    assert len(samples) == 4
    small_36 = samples[3].resize((36, 36), Image.Resampling.LANCZOS)
    out = np.asarray(R.render_preview("taskbar", small_36, S.default_params("taskbar"), 36), dtype=np.float32) / 255.0
    assert out.shape == (36, 36, 4)
    vis = out[..., 3] > 0.02
    assert out[vis, :3].min() >= 0.92
    assert out[:2, :2, 3].max() == 0.0
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
    disc = solid_disc()
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
    ico = Image.open(str(tmp_path / "taskbar.ico"))
    native = R.render_preview("taskbar", src, S.default_params("taskbar"), 32)
    got = np.asarray(ico.ico.getimage((32, 32)).convert("RGBA"), dtype=np.int32)
    diff = np.abs(got - np.asarray(native, dtype=np.int32)).mean()
    assert diff < 3.0, diff


def test_optical_full_bleed_square_retains_outer_rim_on_all_edges():
    full = Image.new("RGBA", (256, 256), (130, 210, 70, 255))
    ImageDraw.Draw(full).rectangle([80, 70, 176, 186], fill=(15, 18, 15, 255))
    out = np.asarray(R.render_preview("desktop", full, S.default_params("desktop"), 48), dtype=np.float32) / 255.0
    assert out[..., 3].max() > 0.50
    assert out[24, 0:3, 3].max() > 0.35 and out[24, -3:, 3].max() > 0.35
    assert out[0:3, 24, 3].max() > 0.35 and out[-3:, 24, 3].max() > 0.35
    assert out[24, 24, 3] < 0.12


def test_desktop_optical_glass_matches_taskbar_and_never_solid_white():
    disc = solid_disc()
    for sz in (48, 64, 256):
        t = np.asarray(R.render_preview("taskbar", disc, S.default_params("taskbar"), sz), dtype=np.float32) / 255.0
        d = np.asarray(R.render_preview("desktop", disc, S.default_params("desktop"), sz), dtype=np.float32) / 255.0
        assert np.allclose(t, d)
        c = sz // 2
        core = d[c - max(2, sz // 6) : c + max(2, sz // 6), c - max(2, sz // 6) : c + max(2, sz // 6), 3]
        assert core.mean() < 0.25 and d[..., 3].max() > core.mean() * 2.0
