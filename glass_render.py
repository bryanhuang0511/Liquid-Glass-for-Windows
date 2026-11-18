"""glass_render.py -- Liquid Glass 渲染核心（只依賴 PIL + numpy，不碰 Windows API，可獨立測試）。
三種風格：
  desktop   風格 A：1:1 Squircle 霜白外框 + 飽滿霜白剪影（公式沿用已驗收版本）
  startmenu 風格 B：雕刻加粗玻璃（無框；加粗邊線 + 透光膜 + 左上受光/右下收光浮雕）
  taskbar   極細鏤空發光線條（無框；嚴禁填滿）
共用前處理：Alpha 邊界自適應正規化（normalize_glyph_bbox），讓各家軟體的圖騰視覺大小一致。
風格 B / 工作列的每個 ICO 尺寸層都「原生合成」：線寬以輸出像素為準，32px 不會變成 0.2px 虛線。
"""
import functools
import math
import os
import numpy as np
from PIL import Image, ImageDraw
from glass_specs import ICO_SIZES, clamp_params
F32 = np.float32
_LANCZOS = Image.Resampling.LANCZOS
_BOX = Image.Resampling.BOX
_STYLE_A_FILL = 0.86  # 風格 A 在 256 畫布內先以此比例渲染，留出光暈空間，之後再縮放到目標佔比
# ============================================================================
# float32 可分離影像運算（不經 8-bit 量化）
# ============================================================================
def _conv_axis(a, k, axis, reduce_max=False, zero_pad=False):
    r = len(k) // 2 if not reduce_max else k
    pad = [(0, 0), (0, 0)]
    pad[axis] = (r, r)
    if zero_pad:
        p = np.pad(a, pad, mode="constant", constant_values=0.0)
    else:
        p = np.pad(a, pad, mode="edge")
    n = a.shape[axis]
    if reduce_max:
        out = None
        for i in range(2 * r + 1):
            sl = [slice(None), slice(None)]
            sl[axis] = slice(i, i + n)
            v = p[tuple(sl)]
            out = v.copy() if out is None else np.maximum(out, v, out=out)
        return out
    out = np.zeros_like(a)
    for i, w in enumerate(k):
        sl = [slice(None), slice(None)]
        sl[axis] = slice(i, i + n)
        out += F32(w) * p[tuple(sl)]
    return out
@functools.lru_cache(maxsize=64)
def _gauss_kernel(sigma):
    r = max(1, int(math.ceil(3.0 * sigma)))
    x = np.arange(-r, r + 1, dtype=np.float64)
    k = np.exp(-(x * x) / (2.0 * sigma * sigma))
    return (k / k.sum()).astype(F32)
def blur(a, sigma, zero_pad=False):
    if sigma < 0.05:
        return a
    k = _gauss_kernel(round(float(sigma), 3))
    return _conv_axis(_conv_axis(a, k, 0, False, zero_pad), k, 1, False, zero_pad)
def dilate(a, radius):
    r = int(radius)
    if r <= 0:
        return a
    return _conv_axis(_conv_axis(a, r, 0, True), r, 1, True)
def sobel(a):
    """回傳 (gx, gy)，單位 = 每像素的階躍高度（理想階躍邊 ≈ 1.0）。x 向右、y 向下。"""
    p = np.pad(a, 1, mode="edge")
    gx = (p[:-2, 2:] + 2 * p[1:-1, 2:] + p[2:, 2:]) - (p[:-2, :-2] + 2 * p[1:-1, :-2] + p[2:, :-2])
    gy = (p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:]) - (p[:-2, :-2] + 2 * p[:-2, 1:-1] + p[:-2, 2:])
    return gx / 4.0, gy / 4.0
def find_edges(a):
    """與 PIL FIND_EDGES 同核（8 鄰域拉普拉斯），float 版本、負值截斷。"""
    p = np.pad(a, 1, mode="edge")
    c = p[1:-1, 1:-1]
    s = (p[:-2, :-2] + p[:-2, 1:-1] + p[:-2, 2:] + p[1:-1, :-2]
         + p[1:-1, 2:] + p[2:, :-2] + p[2:, 1:-1] + p[2:, 2:])
    return np.clip(8.0 * c - s, 0.0, 1.0)
# ============================================================================
# 前處理：Alpha 邊界自適應正規化
# ============================================================================


# ============================================================================
# 底板偵測、前景分離與品牌色擷取（保守模式）
# ============================================================================
def _rgb_array(im):
    """回傳 float32、[0,1] 的 RGB 陣列。"""
    return np.asarray(im.convert("RGBA"), dtype=F32)[..., :3] / 255.0


def _outer_ring_mask(alpha, inner=0.75, outer=0.92):
    """以影像中心及短邊定義外環；忽略透明像素。"""
    h, w = alpha.shape
    yy, xx = np.mgrid[0:h, 0:w].astype(F32)
    cx, cy = (w - 1) * 0.5, (h - 1) * 0.5
    # 以橢圓半徑正規化，避免非正方形來源偏向某一軸。
    rx, ry = max(1.0, w * 0.5), max(1.0, h * 0.5)
    rr = np.sqrt(((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2)
    return (rr >= inner) & (rr <= outer) & (alpha > 0.85)


def _fit_background_rgb(rgb, sample_mask):
    """分別擬合常數 RGB 與平面 RGB(x,y)，回傳較佳模型及正規化 RMS 殘差。"""
    h, w, _ = rgb.shape
    ys, xs = np.nonzero(sample_mask)
    if xs.size < max(32, int(0.01 * h * w)):
        return None, 1.0, None

    x = (xs.astype(F32) / max(1, w - 1)) * 2.0 - 1.0
    y = (ys.astype(F32) / max(1, h - 1)) * 2.0 - 1.0
    values = rgb[ys, xs, :].astype(np.float64)

    # 常數模型：對單色底板較穩定。
    constant = np.median(values, axis=0)
    pred_const = np.broadcast_to(constant, values.shape)
    err_const = float(np.sqrt(np.mean((values - pred_const) ** 2)))

    # 線性平面模型：允許緩慢、近似線性的漸層底板。
    design = np.column_stack((np.ones_like(x), x, y))
    try:
        coeff, _, _, _ = np.linalg.lstsq(design, values, rcond=None)
        pred_plane = design @ coeff
        err_plane = float(np.sqrt(np.mean((values - pred_plane) ** 2)))
    except np.linalg.LinAlgError:
        coeff, pred_plane, err_plane = None, pred_const, 1.0

    if err_const <= err_plane:
        return ("constant", constant), err_const, constant.astype(F32)
    return ("plane", coeff), err_plane, np.median(values, axis=0).astype(F32)


def _predict_background(model, shape):
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w].astype(F32)
    x = (xx / max(1, w - 1)) * 2.0 - 1.0
    y = (yy / max(1, h - 1)) * 2.0 - 1.0
    kind, data = model
    if kind == "constant":
        return np.broadcast_to(np.asarray(data, dtype=F32), (h, w, 3)).copy()
    coeff = np.asarray(data, dtype=F32)
    return (coeff[0][None, None, :] +
            x[..., None] * coeff[1][None, None, :] +
            y[..., None] * coeff[2][None, None, :])


def _soften_binary_mask(mask, sigma=0.85):
    """將二值前景遮罩轉成平滑 alpha，保留核心不透明度。"""
    soft = blur(mask.astype(F32), sigma)
    if np.any(mask):
        soft = np.where(mask, np.maximum(soft, 0.72), soft)
    return np.clip(soft, 0.0, 1.0)


def separate_solid_backplate(im, *, min_fill=0.70, max_fit_rmse=0.095,
                             min_foreground=0.02, max_foreground=0.60,
                             delta_threshold=0.16):
    """
    嘗試從單色/緩慢線性漸層底板抽出前景圖騰。
    回傳 (RGBA 圖片, brand_rgb, diagnostics)。
    未通過任何安全條件時原圖不變、brand_rgb=None。
    """
    rgba = im.convert("RGBA")
    arr = np.asarray(rgba, dtype=F32) / 255.0
    alpha = arr[..., 3]
    rgb = arr[..., :3]
    fill_ratio = float(np.mean(alpha > 0.90))
    diagnostics = {
        "detected": False,
        "reason": "fallback",
        "alpha_fill": fill_ratio,
        "fit_rmse": None,
        "foreground_ratio": None,
        "model": None,
    }

    if fill_ratio < min_fill:
        diagnostics["reason"] = "alpha_fill_below_threshold"
        return rgba.copy(), None, diagnostics

    ring = _outer_ring_mask(alpha)
    model, rmse, brand = _fit_background_rgb(rgb, ring)
    diagnostics["fit_rmse"] = float(rmse)
    diagnostics["model"] = model[0] if model else None

    # 多色環（例如 Chrome）通常無法由常數或單一 RGB 平面解釋。
    if model is None or not np.isfinite(rmse) or rmse > max_fit_rmse:
        diagnostics["reason"] = "outer_ring_not_solid_or_linear"
        return rgba.copy(), None, diagnostics

    predicted = _predict_background(model, alpha.shape)
    # RGB 歐氏距離；除以 sqrt(3) 後落在 [0,1] 範圍。
    distance = np.sqrt(np.sum((rgb - predicted) ** 2, axis=2) / 3.0)
    # 邊界採較寬鬆門檻，降低底板抗鋸齒像素被誤判為前景的機率。
    foreground = (distance > delta_threshold) & (alpha > 0.08)

    # 只保留與中心區域連通的主體像素，避免外圍壓縮雜訊被當成圖騰。
    # 不使用 OpenCV；以簡單的中心種子判斷作為保守防呆。
    h, w = alpha.shape
    center = foreground[max(0, h // 2 - 2):min(h, h // 2 + 3),
                        max(0, w // 2 - 2):min(w, w // 2 + 3)]
    if not np.any(center):
        diagnostics["reason"] = "no_foreground_near_center"
        return rgba.copy(), None, diagnostics

    fg_ratio = float(np.count_nonzero(foreground) / max(1, np.count_nonzero(alpha > 0.08)))
    diagnostics["foreground_ratio"] = fg_ratio
    if not (min_foreground <= fg_ratio <= max_foreground):
        diagnostics["reason"] = "foreground_area_out_of_safe_range"
        return rgba.copy(), None, diagnostics

    # 以距離建立柔化過渡帶；高差異核心維持不透明，底板區域透明。
    transition = np.clip((distance - (delta_threshold * 0.72)) /
                         max(1e-5, delta_threshold * 0.75), 0.0, 1.0)
    mask = _soften_binary_mask(foreground, 0.85)
    out_alpha = alpha * np.maximum(mask, transition * foreground.astype(F32))
    # 將前景 RGB 保留原色；桌面風格後續只使用 alpha 作白色剪影。
    out = np.dstack((rgb, np.clip(out_alpha, 0.0, 1.0)))
    result = Image.fromarray((np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8), "RGBA")
    diagnostics["detected"] = True
    diagnostics["reason"] = "solid_backplate_removed"
    return result, np.clip(brand, 0.0, 1.0), diagnostics


def normalize_glyph_inscribed(im, target_ratio=0.80, canvas=256,
                              alpha_thresh=12, corner_radius_ratio=0.22):
    """
    將前景圖騰放大至 Squircle 內圈可容納的最大尺寸。
    先按長邊放大，再以內接圓角遮罩裁切，確保不碰外框高光。
    """
    rgba = im.convert("RGBA")
    a = np.asarray(rgba.getchannel("A"))
    ys, xs = np.nonzero(a > alpha_thresh)
    if xs.size == 0:
        return Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    bbox = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    crop = rgba.crop(bbox)
    cw, ch = crop.size
    scale = min(canvas * float(target_ratio) / max(cw, ch), 4.0)
    nw = max(1, int(round(cw * scale)))
    nh = max(1, int(round(ch * scale)))
    resized = crop.resize((nw, nh), _LANCZOS)

    # 以內縮的 Squircle 遮罩限制角隅；若圖騰四角超出，逐步縮小至完全容納。
    inset = max(5, int(round(canvas * 0.045)))
    radius = int(round(canvas * corner_radius_ratio))
    mask = Image.new("L", (canvas, canvas), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (inset, inset, canvas - 1 - inset, canvas - 1 - inset),
        radius=max(2, radius - inset), fill=255)
    mask_arr = np.asarray(mask) > 250

    for _ in range(24):
        x0, y0 = (canvas - nw) // 2, (canvas - nh) // 2
        test = np.zeros((canvas, canvas), dtype=bool)
        x1, y1 = min(canvas, x0 + nw), min(canvas, y0 + nh)
        if x0 >= 0 and y0 >= 0 and x1 > x0 and y1 > y0:
            local = np.asarray(resized.getchannel("A")) > alpha_thresh
            test[y0:y1, x0:x1] = local[:y1-y0, :x1-x0]
        if np.all(mask_arr[test]):
            break
        nw = max(1, int(nw * 0.96))
        nh = max(1, int(nh * 0.96))
        resized = crop.resize((nw, nh), _LANCZOS)

    out = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    x0, y0 = (canvas - nw) // 2, (canvas - nh) // 2
    out.alpha_composite(resized, (x0, y0))
    # 使用高品質遮罩縮放，柔化 Squircle 角隅交界。
    alpha_out = np.asarray(out.getchannel("A"), dtype=F32) / 255.0
    clipped = alpha_out * (np.asarray(mask, dtype=F32) / 255.0)
    out.putalpha(Image.fromarray((np.clip(clipped, 0, 1) * 255 + 0.5).astype(np.uint8)))
    return out


def _tinted_frame(size, radius, brand_rgb, tint_strength=0.12):
    """將底板品牌色以低比例混入玻璃底層，不改變白色圖騰本體。"""
    frame = create_liquid_glass_frame(size, radius).convert("RGBA")
    if brand_rgb is None or tint_strength <= 0:
        return frame
    arr = np.asarray(frame, dtype=F32).copy() / 255.0
    rgb = np.asarray(brand_rgb, dtype=F32).reshape(1, 1, 3)
    h, w = arr.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(F32)
    # 低頻柔光：中心較淡，左上與右下折射區稍明顯。
    field = 0.30 + 0.35 * np.exp(-(((xx / max(1, w-1) - 0.18) ** 2 +
                                    (yy / max(1, h-1) - 0.18) ** 2) / 0.12))
    amount = np.clip(float(tint_strength) * field, 0.0, 0.15)
    arr[..., :3] = arr[..., :3] * (1.0 - amount[..., None]) + rgb * amount[..., None]
    return Image.fromarray((np.clip(arr, 0, 1) * 255 + 0.5).astype(np.uint8), "RGBA")


def normalize_glyph_bbox(im, target_ratio=0.68, canvas=256, alpha_thresh=24, max_upscale=3.0):
    """偵測圖騰真實非透明邊界 → 等比縮放到畫布的 target_ratio → 置中。
    alpha_thresh：忽略淡陰影，避免邊界被撐大。
    max_upscale：相對於裁切後原生像素的放大上限，避免極小圖騰被放大到糊掉。
    """
    im = im.convert("RGBA")
    a = np.asarray(im.getchannel("A"))
    ys, xs = np.nonzero(a > alpha_thresh)
    if xs.size == 0:
        bbox = (0, 0, im.width, im.height)
    else:
        bbox = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
    crop = im.crop(bbox)
    cw, ch = crop.size
    scale = min(canvas * target_ratio / float(max(cw, ch)), max_upscale)
    nw, nh = max(1, int(round(cw * scale))), max(1, int(round(ch * scale)))
    resized = crop.resize((nw, nh), _LANCZOS)
    out = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    out.paste(resized, ((canvas - nw) // 2, (canvas - nh) // 2))
    return out
def _channels(im):
    arr = np.asarray(im, dtype=F32) / 255.0
    r, g, b, a = arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3]
    return a, 0.299 * r + 0.587 * g + 0.114 * b
def _pack(alpha, rgb=None):
    h, w = alpha.shape
    if rgb is None:
        rgb = np.ones((h, w, 3), dtype=F32)
    out = np.dstack([rgb, alpha[..., None]])
    return Image.fromarray((np.clip(out, 0, 1) * 255.0 + 0.5).astype(np.uint8), "RGBA")
# ============================================================================
# 風格 A：高亮霜白剪影 + Squircle 外框（桌面）
# ============================================================================
def style_a_core(glyph256, line, glow, film):
    """公式與 v1.2.0 已驗收的 transform_to_pure_white_glass 相同（改為 float32 運算）。"""
    alpha, lum = _channels(glyph256)
    body = alpha * (0.65 + 0.32 * lum) * film
    lum_a = lum * alpha
    edge = blur(find_edges(lum_a), 1.0) * line
    halo = blur(lum_a, 4.0) * glow
    final = np.clip(body + edge + halo, 0.0, 1.0) * (alpha > 0.05)
    return _pack(final)
@functools.lru_cache(maxsize=4)
def create_liquid_glass_frame(size=256, radius=56):
    scale = 2
    s = size * scale
    r = radius * scale
    pad = 6 * scale
    def rr(inset, rad):
        m = Image.new("L", (s, s), 0)
        ImageDraw.Draw(m).rounded_rectangle(
            [pad + inset, pad + inset, s - 1 - pad - inset, s - 1 - pad - inset],
            radius=max(2, rad - inset), fill=255)
        return np.asarray(m, dtype=F32) / 255.0
    om = rr(0, r)
    w1 = int(2.8 * scale)
    rim_outer = np.clip(om - rr(w1, r), 0, 1)
    wb = int(12 * scale)
    bevel_band = np.clip(om - rr(wb, r), 0, 1)
    bevel_smooth = blur(bevel_band, 6.0) * om
    in_gap = int(7.5 * scale)
    w2 = int(1.4 * scale)
    rim_inner = np.clip(rr(in_gap, r) - rr(in_gap + w2, r), 0, 1)
    yy, xx = np.mgrid[0:s, 0:s] / float(s - 1)
    tl = np.clip(1.0 - np.sqrt(xx ** 2 + yy ** 2), 0, 1) ** 1.3
    br = np.clip(1.0 - np.sqrt((1.0 - xx) ** 2 + (1.0 - yy) ** 2) * 1.1, 0, 1) ** 1.5
    rim_light = 0.45 + 0.55 * np.clip(tl * 1.2 + br * 0.9, 0, 1)
    top_sheen = np.clip((0.25 - yy) / 0.25, 0, 1) ** 2.2 * 0.12
    base_clear = 0.05 + 0.06 * ((1.0 - xx) * 0.5 + (1.0 - yy) * 0.5)
    fa = np.clip((base_clear + top_sheen) * om + rim_outer * rim_light * 0.98
                 + bevel_smooth * rim_light * 0.38 + rim_inner * 0.35, 0, 0.98) * om
    frame = _pack(fa.astype(F32))
    return frame.resize((size, size), _LANCZOS)
def render_desktop_256(src, p):
    # 只在底板模型通過保守門檻時分離前景；失敗時完全沿用原始圖示。
    separated, brand_rgb, _diagnostics = separate_solid_backplate(src)
    work = normalize_glyph_inscribed(
        separated, target_ratio=min(0.84, max(0.72, p["ratio"])), canvas=256
    )
    inner = style_a_core(work, p["line"], p["glow"], p["film"])
    canvas = _tinted_frame(256, 56, brand_rgb, tint_strength=0.12)
    # 內接式遮罩已完成角隅保護；直接合成避免再次縮小圖騰。
    canvas.alpha_composite(inner)
    return canvas
def _crisp_layer(im256, size):
    resized = im256.resize((size, size), _LANCZOS)
    if size <= 64:
        arr = np.asarray(resized, dtype=F32) / 255.0
        a = arr[..., 3]
        # 小尺寸 alpha 銳化：沿用 v1.2.0 create_crisp_layer 的曲線
        sharp = np.where(a > 0.08, np.clip(a ** 0.60 * 1.45, 0, 1.0), a * 0.5)
        arr[..., 3] = sharp
        resized = Image.fromarray((arr * 255.0 + 0.5).astype(np.uint8), "RGBA")
    return resized
# ============================================================================
# 風格 B（開始功能表）與 工作列細線：以輸出像素為準的原生合成
# ============================================================================
def _work_scale(size):
    return max(1, 256 // size)
def _sobel_optical(arr, zero_pad=False):
    p = np.pad(arr, 1, mode="constant", constant_values=0.0) if zero_pad else np.pad(arr, 1, mode="edge")
    gx = ((p[:-2, 2:] + 2 * p[1:-1, 2:] + p[2:, 2:]) - (p[:-2, :-2] + 2 * p[1:-1, :-2] + p[2:, :-2])) / 8.0
    gy = ((p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:]) - (p[:-2, :-2] + 2 * p[:-2, 1:-1] + p[:-2, 2:])) / 8.0
    return gx, gy, np.hypot(gx, gy)


def _smoothstep(e0, e1, x):
    t = np.clip((x - e0) / np.maximum(e1 - e0, 1e-6), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def premul_resize(im, size, resample=_LANCZOS):
    """預乘 Alpha 高品質縮放（防止半透明邊緣混出黑灰暗緣）。"""
    rgba = im.convert("RGBA")
    if rgba.size == (size, size):
        return rgba
    arr = np.asarray(rgba, dtype=F32) / 255.0
    a = arr[..., 3:4]
    pm = np.concatenate([arr[..., :3] * a, a], axis=-1)
    ch = [
        Image.fromarray(np.clip(pm[..., i] * 255.0 + 0.5, 0, 255).astype(np.uint8), "L").resize(
            (size, size), resample
        )
        for i in range(4)
    ]
    out = np.stack([np.asarray(c, dtype=F32) / 255.0 for c in ch], axis=-1)
    oa = np.maximum(out[..., 3:4], 1e-5)
    rgb = np.where(out[..., 3:4] > 1e-4, np.clip(out[..., :3] / oa, 0.0, 1.0), 0.0)
    res = np.concatenate([rgb, out[..., 3:4]], axis=-1)
    return Image.fromarray(np.clip(res * 255.0 + 0.5, 0, 255).astype(np.uint8), "RGBA")


def render_optical_liquid_glass(src, p=None, *, scope="taskbar"):
    """即時光學 Liquid Glass 核心（與 Windhawk C++ 實機引擎數學完全一致，工作列與桌面共用）：
    1. 零黑、零灰 RGB（所有可見像素 RGB >= 0.94）。
    2. 深色與高飽和色彩轉化為低 Alpha 清透玻璃，使後方背景自然透出。
    3. 真實白光／高光圖案轉為半透明霜白（總不透明度上限 0.90，絕不死白）。
    4. 100% 保留原生形狀與反鋸齒邊界，外輪廓採平頂銳化加厚、內部接縫具細節密度保護。
    """
    p = clamp_params(scope, p) if p is not None else clamp_params(scope, {})
    sw, sh = src.size
    work = premul_resize(src, 256, Image.Resampling.BICUBIC if max(sw, sh) < 256 else _LANCZOS)
    arr = np.asarray(work, dtype=F32) / 255.0
    r, g, b, a = arr[..., 0], arr[..., 1], arr[..., 2], arr[..., 3]
    h, w = a.shape
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    c_min = np.minimum(np.minimum(r, g), b)
    c_max = np.maximum(np.maximum(r, g), b)
    chroma = c_max - c_min

    # 分辨率補償：當來源已是 24~48px 點陣圖（如工作列實機擷取）時，上縮 256 後還原銳利 Rim/Seam 梯度。
    src_scale = 256.0 / max(1.0, float(min(sw, sh)))
    t_low = float(np.clip((src_scale - 1.0) / 6.0, 0.0, 1.0))
    sigma1 = 1.9 + 1.0 * t_low
    sigma2 = 5.4 + 2.8 * t_low
    edge_gain = 1.0 + 1.45 * t_low
    grad_gain = 1.0 + 2.35 * t_low

    interior = a > 0.25
    p90 = float(np.percentile(lum[interior], 90)) if np.any(interior) else 0.5
    p15 = float(np.percentile(lum[interior], 15)) if np.any(interior) else 0.1
    mean_l = float(np.mean(lum[interior])) if np.any(interior) else 0.5

    has_dark_cutout = (p90 > 0.62) and (p15 < 0.12)
    is_dark_logo = mean_l < 0.34

    neutral_white = 1.0 - _smoothstep(0.18, 0.50, chroma)
    frost_signal = _smoothstep(0.45, 0.88, c_min) * neutral_white

    if has_dark_cutout:
        cutout = 1.0 - _smoothstep(0.03, 0.22, lum)
        body_alpha = (0.048 + 0.080 * (np.clip(lum, 0, 1) ** 0.90)) * (1.0 - 0.86 * cutout)
    elif is_dark_logo:
        rel_lum = np.clip((lum - 0.03) / max(p90, 0.22), 0.0, 1.0)
        body_alpha = 0.065 + 0.070 * (np.clip(lum, 0, 1) ** 0.85) + 0.065 * p["dark_protect"] * rel_lum
    else:
        body_alpha = 0.050 + 0.075 * (np.clip(lum, 0, 1) ** 0.90)

    body_alpha = np.clip(body_alpha * p["opacity"], 0.0, 1.0)
    frost_alpha = np.clip(0.58 * p["frost"], 0.0, 1.0) * np.clip(p["opacity"] * 0.35 + 0.65, 0.35, 1.25)
    base_alpha = frost_alpha * frost_signal + body_alpha * (1.0 - frost_signal)

    yy, xx = np.mgrid[0:h, 0:w].astype(F32)
    nx, ny = (xx / (w - 1)) * 2.0 - 1.0, (yy / (h - 1)) * 2.0 - 1.0
    diag = np.clip(0.58 - 0.42 * (0.68 * nx + 0.73 * ny), 0.0, 1.0)
    arc = np.exp(-((nx + 0.38) ** 2 + (ny + 0.42) ** 2) * 2.4)
    sup = (1.0 - _smoothstep(0.03, 0.16, lum)) if has_dark_cutout else 0.0
    sheen = (0.060 * (diag ** 1.7) + 0.095 * arc) * (1.0 - 0.55 * frost_signal) * (1.0 - 0.85 * sup) * p["glow"]

    a_b1, a_b2 = blur(a, sigma1, zero_pad=True), blur(a, sigma2, zero_pad=True)
    edge_raw = np.clip((a - a_b1) * 3.0, 0, 1) + np.clip((a_b1 - a_b2) * 2.1, 0, 1)
    edge_plateau = _smoothstep(0.14, 0.46, np.clip(edge_raw * edge_gain, 0, 1.5))
    edge_band = edge_plateau * _smoothstep(0.28, 0.80, a)
    gxs, gys, ms = _sobel_optical(a_b1, zero_pad=True)
    inv_m = 1.0 / np.maximum(ms, 1e-4)
    onx, ony = -gxs * inv_m, -gys * inv_m
    rim = edge_band * (0.32 + 0.72 * (np.clip(-0.70710678 * (onx + ony), 0, 1) ** 1.10) + 0.40 * (np.clip(0.70710678 * (onx + ony), 0, 1) ** 1.35)) * p["glow"]

    _, _, gr = _sobel_optical(blur(r, 0.6))
    _, _, gg = _sobel_optical(blur(g, 0.6))
    _, _, gb = _sobel_optical(blur(b, 0.6))
    _, _, gl = _sobel_optical(blur(lum, 0.6))
    c_edge = np.sqrt(gr * gr + gg * gg + gb * gb) / 1.7320508
    raw_grad = np.maximum(gl, 1.25 * c_edge) * grad_gain
    grad_density = blur(np.clip(raw_grad * 4.0, 0, 1), 3.6)
    seam_thresh_hi = 0.16 + 0.10 * np.clip(grad_density, 0, 1)
    seam = _smoothstep(0.020, seam_thresh_hi, raw_grad) * _smoothstep(0.48, 0.92, a)
    seam_light = 0.58 * p["glow"] * seam * (1.0 - 0.30 * edge_band)

    out_a = np.clip(base_alpha + sheen + rim + seam_light, 0.0, 0.90) * a
    spec = np.clip(frost_signal + 0.95 * rim + 0.80 * seam_light + 0.65 * sheen, 0.0, 1.0)
    out_rgb = np.stack([0.94 + 0.06 * spec, 0.97 + 0.03 * spec, np.ones_like(spec)], axis=-1)
    out_rgb[out_a <= 1e-4] = 0.0
    return Image.fromarray((np.clip(np.dstack([out_rgb, out_a]), 0, 1) * 255.0 + 0.5).astype(np.uint8), "RGBA")


def _finalize_optical_size(base256, size):
    """從 256×256 光學成品無損預乘下縮至目標尺寸，並鎖定可見像素 RGB >= 0.94。"""
    if size == 256 and base256.size == (256, 256):
        return base256
    out = premul_resize(base256, size, _LANCZOS)
    arr = np.asarray(out, dtype=F32) / 255.0
    arr[..., 3] = np.clip(arr[..., 3], 0.0, 0.90)  # 抑制 Lanczos 下縮邊緣振鈴超調，維持半透明清透上限
    vis = arr[..., 3] > 1e-4
    arr[vis, 0] = np.maximum(arr[vis, 0], 0.94)
    arr[vis, 1] = np.maximum(arr[vis, 1], 0.97)
    arr[vis, 2] = 1.0
    arr[~vis, :3] = 0.0
    return Image.fromarray(np.clip(arr * 255.0 + 0.5, 0, 255).astype(np.uint8), "RGBA")


def _taskbar_optical_layer(src, p, size):
    return _finalize_optical_size(render_optical_liquid_glass(src, p, scope="taskbar"), size)


def _taskbar_layer(arr_img, p, size, ss):
    return _taskbar_optical_layer(Image.fromarray((np.clip(arr_img, 0, 1) * 255).astype(np.uint8), "RGBA"), p, size)
def _menu_layer(arr_img, p, size, ss):
    alpha, lum = _channels(arr_img)
    lum_a = lum * alpha
    wpx = p["line"] * (0.7 + size / 256.0 * 1.6)
    wk = wpx * ss
    gxl, gyl = sobel(lum_a)
    gxa, gya = sobel(alpha)
    edge = np.maximum(np.clip(np.hypot(gxl, gyl) * 1.6, 0, 1),
                      np.clip(np.hypot(gxa, gya) * 0.9, 0, 1))
    thick = dilate(edge, int(round((wk - 1.0) / 2.0)))
    thick = blur(thick, 0.4 * ss)
    edge_a = np.clip(thick * 1.6, 0, 1)
    body = alpha * p["film"] * (0.7 + 0.3 * lum)
    halo = blur(thick, max(0.8, 0.012 * size * 3.0) * ss) * p["glow"]
    # 浮雕：高度場 = 模糊後的 alpha（含亮度起伏）；光源在左上
    se = max(1.0, wk * 0.5)
    h = blur(alpha * (0.6 + 0.4 * lum), se)
    hx, hy = sobel(h)
    m = (hx + hy) / math.sqrt(2.0)
    m = m / (0.8 / se)
    lit = np.clip(m, 0, 1)
    shade = np.clip(-m, 0, 1)
    e = p["emboss"]
    rim_mask = np.clip(thick * 2.0, 0, 1)
    final = edge_a + body + halo + e * 0.55 * lit * rim_mask - e * 0.45 * shade * rim_mask
    mask = np.clip(dilate((alpha > 0.05).astype(F32), int(math.ceil(wk / 2.0 + ss))), 0, 1)
    final = np.clip(final, 0, 1) * mask
    rgb = np.ones(alpha.shape + (3,), dtype=F32)
    rgb *= (1.0 - e * 0.28 * shade * rim_mask)[..., None]
    return _pack(final, rgb)
_NATIVE = {"startmenu": _menu_layer, "taskbar": _taskbar_layer}
def render_native(scope, src, p, size):
    if scope == "taskbar":
        return _taskbar_optical_layer(src, p, size)
    ss = _work_scale(size)
    w = size * ss
    glyph = normalize_glyph_bbox(src, p["ratio"], canvas=w)
    img = _NATIVE[scope](glyph, p, size, ss)
    if ss > 1:
        img = img.resize((size, size), _BOX)
    return img
# ============================================================================
# 對外 API
# ============================================================================
def render_layers(scope, src, params, sizes=ICO_SIZES):
    """回傳 {尺寸: RGBA 影像}。"""
    p = clamp_params(scope, params)
    if scope in ("desktop", "taskbar", "startmenu"):
        base = render_optical_liquid_glass(src, p, scope=scope)
        return {s: _finalize_optical_size(base, s) for s in sizes}
    return {s: render_native(scope, src, p, s) for s in sizes}
def write_ico(layers, path, sizes=ICO_SIZES):
    base = layers[sizes[0]]
    others = [layers[s] for s in sizes[1:]]
    tmp = path + ".tmp"
    base.save(tmp, format="ICO", sizes=[(s, s) for s in sizes], append_images=others)
    os.replace(tmp, path)
def render_ico(scope, src, params, path):
    write_ico(render_layers(scope, src, params), path)
def render_preview(scope, src, params, size):
    """UI 預覽：回傳 size×size 的 RGBA。size 為實際輸出像素。"""
    p = clamp_params(scope, params)
    size = int(max(8, min(256, size)))
    if scope in ("desktop", "taskbar", "startmenu"):
        return _finalize_optical_size(render_optical_liquid_glass(src, p, scope=scope), size)
    return render_native(scope, src, p, size)
# ============================================================================
# 內建範例圖示（預覽用；彩色、含外輪廓與內部細節，另含滿版與大留白兩種邊距情境）
# ============================================================================
def _grad(size, c0, c1, vertical=True):
    t = np.linspace(0, 1, size, dtype=F32)
    t = t[:, None] if vertical else t[None, :]
    t = np.broadcast_to(t, (size, size))[..., None]
    return np.array(c0, dtype=F32) * (1 - t) + np.array(c1, dtype=F32) * t
def _masked(size, color_arr, draw_mask):
    m = Image.new("L", (size, size), 0)
    draw_mask(ImageDraw.Draw(m))
    a = np.asarray(m, dtype=F32)[..., None] / 255.0
    rgba = np.concatenate([color_arr / 255.0, a], axis=-1)
    return Image.fromarray((np.clip(rgba, 0, 1) * 255).astype(np.uint8), "RGBA")
@functools.lru_cache(maxsize=1)
def sample_icons():
    """三個範例：圓形徽章（留白適中）、資料夾（滿版貼邊）、小方塊（自帶大留白）。"""
    S = 4 * 256
    out = []
    # 1) 圓形徽章 + 對話泡泡
    badge = _masked(S, _grad(S, (70, 130, 250), (140, 70, 230)),
                    lambda d: d.ellipse([40, 40, S - 40, S - 40], fill=255))
    d = ImageDraw.Draw(badge)
    d.rounded_rectangle([250, 300, S - 250, 700], radius=90, fill=(255, 255, 255, 255))
    d.polygon([(380, 690), (380, 860), (560, 690)], fill=(255, 255, 255, 255))
    for i, c in enumerate([(70, 130, 250), (140, 70, 230), (240, 90, 160)]):
        cx = 362 + i * 150
        d.ellipse([cx - 46, 500 - 46, cx + 46, 500 + 46], fill=c + (255,))
    out.append(badge)
    # 2) 資料夾（滿版貼邊）
    folder = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    back = _masked(S, _grad(S, (240, 180, 50), (220, 140, 30)),
                    lambda d: (d.rounded_rectangle([0, 150, S - 1, S - 40], radius=70, fill=255),
                               d.rounded_rectangle([0, 60, 500, 330], radius=60, fill=255)))
    front = _masked(S, _grad(S, (255, 214, 102), (240, 170, 50)),
                    lambda d: d.rounded_rectangle([0, 330, S - 1, S - 1], radius=80, fill=255))
    folder.alpha_composite(back)
    folder.alpha_composite(front)
    ImageDraw.Draw(folder).rounded_rectangle([60, 400, S - 60, 450], radius=25, fill=(255, 255, 255, 120))
    out.append(folder)
    # 3) 小方塊（圖騰自帶大留白；邊緣另有淡陰影，用來驗證 alpha 門檻）
    small = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sh = _masked(S, np.zeros((S, S, 3), dtype=F32),
                 lambda d: d.rounded_rectangle([340, 360, S - 340, S - 320], radius=110, fill=40))
    small.alpha_composite(sh)
    sq = _masked(S, _grad(S, (255, 150, 60), (235, 80, 70)),
                 lambda d: d.rounded_rectangle([330, 330, S - 330, S - 330], radius=110, fill=255))
    small.alpha_composite(sq)
    ImageDraw.Draw(small).polygon([(440, 400), (440, S - 400), (S - 400, S // 2)], fill=(255, 255, 255, 255))
    out.append(small)
    # 4) 亮底深字鏤空磚（Affinity 型：高飽和亮底＋深黑幾何字元，驗證鏤空透底）
    tile = _masked(S, _grad(S, (182, 232, 84), (112, 208, 64)),
                   lambda d: d.rounded_rectangle([80, 80, S - 80, S - 80], radius=180, fill=255))
    td = ImageDraw.Draw(tile)
    td.polygon([(S // 2, 210), (770, 790), (630, 790), (S // 2, 430), (394, 790), (254, 790)],
               fill=(18, 22, 18, 255))
    td.rectangle([360, 560, 664, 650], fill=(18, 22, 18, 255))
    out.append(tile)
    return tuple(im.resize((256, 256), _LANCZOS) for im in out)
def tray_fallback_icon(size=64):
    """純 PIL 的 3x3 圓點圖示（系統匣備援，不需要 numpy）。"""
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    r = size * 0.085
    step = size * 0.27
    m = (size - (step * 2 + r * 2)) / 2 + r
    for i in range(3):
        for j in range(3):
            cx, cy = m + j * step, m + i * step
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255))
    return im


def analyze_backplate(src):
    """公開診斷介面：回傳底板偵測結果與原因，方便單元測試及 UI 除錯。"""
    _image, _brand, diagnostics = separate_solid_backplate(src)
    return dict(diagnostics)


def extract_brand_color(src):
    """若成功辨識並移除單色/線性漸層底板，回傳 RGB 0..1；否則回傳 None。"""
    _image, brand, _diagnostics = separate_solid_backplate(src)
    if brand is None:
        return None
    return tuple(float(v) for v in brand)
