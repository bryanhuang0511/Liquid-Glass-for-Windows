"""liquid_glass/specs.py -- 三區風格規格、參數範圍、來源規範、生命週期與設定遷移。

Functionality:
1. 定義 desktop / startmenu / taskbar 三區光學參數規格與合法邊界（單一真值來源）。
2. 提供來源標籤（user / common）、副檔名支援與母本／IconVault 命名規則。
3. 提供 Watcher 監控去抖、退出還原生命週期與舊版設定檔自動遷移。
4. 純 Python 零重型依賴（刻意不匯入 PIL / NumPy / os），確保系統匣與視窗毫秒級讀取。
"""
from __future__ import annotations

from typing import Any

# Lines 15-35: 版本號與四區範圍狀態（全部四區正式開放，支援一鍵四區＋個別獨立）
VERSION = "2.2.2"

# 演算法或輸出格式變動時 +1，IconVault 的渲染快取會自動失效並重新生成。
RENDER_VERSION = 7

SCOPES: tuple[str, ...] = ("desktop", "startmenu", "taskbar", "tray")
ACTIVE_SCOPES: tuple[str, ...] = ("taskbar", "startmenu", "desktop", "tray")
LOCKED_SCOPES: tuple[str, ...] = ()
ICO_SIZES: tuple[int, ...] = (256, 128, 96, 64, 48, 40, 36, 32, 24, 16)

# 工作列薄膜硬上限：即使設定檔被改壞或滑桿被拉滿，也不可能再變成實心白塊。
TASKBAR_FILM_MAX = 0.15


def is_scope_locked(scope: str) -> bool:
    """回傳指定區域是否目前被鎖定（三區全面開放後僅拒絕未知範圍）。"""
    return scope in LOCKED_SCOPES or scope not in ACTIVE_SCOPES


# Lines 38-115: 來源標籤、副檔名與母本 / IconVault 路徑契約
SOURCE_USER = "user"        # 個人位置：%USERPROFILE%\Desktop、%APPDATA%\...\Start Menu\Programs、工作列釘選
SOURCE_COMMON = "common"    # 公用位置：C:\Users\Public\Desktop、%ProgramData%\...\Start Menu\Programs
SOURCE_TAGS: tuple[str, ...] = (SOURCE_USER, SOURCE_COMMON)
SOURCE_LABELS: dict[str, str] = {SOURCE_USER: "個人", SOURCE_COMMON: "公用"}

# 修改該來源的捷徑通常需要管理員權限（公用桌面刪改會跳 UAC）。
SOURCE_NEEDS_ADMIN: dict[str, bool] = {SOURCE_USER: False, SOURCE_COMMON: True}

# 每個範圍要掃描哪些來源、接受哪些副檔名（全部小寫）。
SCOPE_SOURCES: dict[str, tuple[str, ...]] = {
    "desktop": (SOURCE_USER, SOURCE_COMMON),
    "startmenu": (SOURCE_USER, SOURCE_COMMON),
    "taskbar": (SOURCE_USER,),
    "tray": (SOURCE_USER,),
}
SCOPE_EXTS: dict[str, tuple[str, ...]] = {
    "desktop": (".lnk", ".url"),
    "startmenu": (".lnk",),
    "taskbar": (".lnk",),
    "tray": (".lnk",),
}

# .url（網際網路捷徑，INI 文字檔）
URL_SECTION = "InternetShortcut"
URL_ICON_KEYS: tuple[str, str] = ("IconFile", "IconIndex")
# 逐行讀寫時依序嘗試的編碼；寫回時必須沿用讀到的那一種，不可擅自轉碼。
URL_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "utf-8", "cp950", "cp1252")

# Microsoft Store / MSIX 捷徑（TargetPath 與 IconLocation 皆空，只有 AUMID）
AUMID_SHELL_PREFIX = "shell:AppsFolder\\"


def aumid_shell_path(aumid: str) -> str:
    """將 AUMID 轉為 Shell 可解析路徑（shell:AppsFolder\\<AUMID>）。"""
    return AUMID_SHELL_PREFIX + str(aumid).strip()


def shortcut_ext(scope: str, filename: str) -> str | None:
    """檔名若屬於該範圍可處理的捷徑類型，回傳小寫副檔名（如 '.url'），否則回傳 None。"""
    exts = SCOPE_EXTS.get(scope, ())
    low = str(filename).lower()
    for ext in exts:
        if low.endswith(ext):
            return ext
    return None


def backup_parts(scope: str, source: str) -> tuple[str, ...]:
    """母本備份目錄（相對於 Original_Shortcuts_Backup）的路徑片段。"""
    if scope not in SCOPE_SOURCES:
        raise ValueError(f"unknown scope: {scope!r}")
    if source not in SOURCE_TAGS:
        raise ValueError(f"unknown source: {source!r}")
    if scope == "desktop":
        return () if source == SOURCE_USER else ("PublicDesktop",)
    if scope == "taskbar":
        return ("Taskbar",)
    if scope == "tray":
        return ("Tray",)
    return ("StartMenu", source)


def vault_tag(scope: str, source: str, ext: str) -> str:
    """ICO 檔名中介字串（置於安全名稱與風格後綴之間），避免跨來源或跨副檔名撞名。"""
    tag = ""
    if scope == "desktop" and source == SOURCE_COMMON:
        tag += "_pub"
    if str(ext).lower() == ".url":
        tag += "_url"
    return tag


# Lines 117-185: Watcher 監控參數與程式退出生命週期契約
WATCHER_DEFAULTS: dict[str, Any] = {"enabled": False, "debounce_sec": 3.0}
WATCHER_LIMITS: dict[str, tuple[float, float]] = {"debounce_sec": (1.0, 30.0)}
WATCHER_MAX_WAIT_SEC = 60.0          # 事件持續不斷時，最晚在此秒數內觸發一次
WATCHER_SELF_IGNORE_SEC = 5.0        # 本工具自己寫入捷徑後的事件忽略視窗，防止無限迴圈
WATCHER_FALLBACK_RESCAN_SEC = 900.0  # 保底定期全掃（秒）；0 代表停用
WATCHER_SCOPES: tuple[str, ...] = ("desktop",)  # 工作列與開始功能表改採 Windhawk 即時引擎

LIFECYCLE_DEFAULTS: dict[str, bool] = {
    "restore_live_on_exit": False,     # 結束圖示中樞時自動停用工作列／開始功能表 Windhawk 引擎
    "exit_when_window_closes": False,  # 關閉設定視窗時直接結束圖示中樞，不常駐系統匣
}


def default_watcher() -> dict[str, Any]:
    """回傳一份新的 Watcher 預設設定。"""
    return dict(WATCHER_DEFAULTS)


def clamp_watcher(raw: Any) -> dict[str, Any]:
    """接受 dict、舊式布林或非法值；輸出合法的 {'enabled': bool, 'debounce_sec': float}。"""
    out = default_watcher()
    if isinstance(raw, bool):
        out["enabled"] = raw
        return out
    if not isinstance(raw, dict):
        return out
    en = raw.get("enabled", out["enabled"])
    if isinstance(en, bool):
        out["enabled"] = en
    elif isinstance(en, int) and en in (0, 1):
        out["enabled"] = bool(en)
    lo, hi = WATCHER_LIMITS["debounce_sec"]
    try:
        val = float(raw.get("debounce_sec", out["debounce_sec"]))
        if val != val or val in (float("inf"), float("-inf")):
            raise ValueError
    except (TypeError, ValueError):
        val = float(WATCHER_DEFAULTS["debounce_sec"])
    out["debounce_sec"] = round(min(hi, max(lo, val)), 2)
    return out


def default_lifecycle() -> dict[str, bool]:
    """回傳一份新的生命週期預設設定。"""
    return dict(LIFECYCLE_DEFAULTS)


def clamp_lifecycle(raw: Any) -> dict[str, bool]:
    """正規化 lifecycle 設定，確保布林欄位安全合法。"""
    out = default_lifecycle()
    if not isinstance(raw, dict):
        return out
    for key in LIFECYCLE_DEFAULTS:
        val = raw.get(key, out[key])
        if isinstance(val, bool):
            out[key] = val
        elif isinstance(val, int) and val in (0, 1):
            out[key] = bool(val)
    return out


# Lines 187-265: 套用 / 還原結果統計欄位與三區規格表
RESULT_FIELDS: tuple[str, ...] = (
    "applied",
    "unchanged",
    "no_backup",
    "denied",
    "failed",
    "no_icon",
    "backed_up",
    "restored",
    "untouched",
    "needs_admin",
)


def new_result(scope: str, total: int) -> dict[str, Any]:
    """建立單一區域批次作業統計結果字典。"""
    res: dict[str, Any] = {"scope": scope, "total": total}
    for key in RESULT_FIELDS:
        res[key] = 0
    return res


def _p(key: str, label: str, desc: str, lo: float, hi: float, step: float, default: float) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "desc": desc,
        "min": lo,
        "max": hi,
        "step": step,
        "default": default,
    }


def _optical_params() -> list[dict[str, Any]]:
    return [
        _p("opacity", "玻璃主體透光", "彩色與暗部轉清透玻璃的基礎不透明度倍率", 0.20, 1.80, 0.05, 1.00),
        _p("frost", "霜白高光濃度", "純白與亮面圖騰的半透明霜白飽和度（嚴格低於實心死白）", 0.20, 1.30, 0.05, 1.00),
        _p("glow", "邊緣折光反光", "原位內縮斜角（Rim）與色塊交界捕光（Seam）強度", 0.30, 1.80, 0.05, 1.00),
        _p("dark_protect", "深暗標誌保護", "全暗標誌的玻璃底層亮度補償（亮底黑字維持透空）", 0.50, 2.00, 0.05, 1.00),
    ]


SPECS: dict[str, dict[str, Any]] = {
    "desktop": {
        "id": "desktop",
        "label": "桌面",
        "icon": "🖥️",
        "style": "即時光學液態玻璃（桌面獨立核心）",
        "summary": "免外掛獨立運作；高透光清透本體＋銳利加厚外緣斜角（Rim）與內部接縫捕光（支援個人／公用捷徑與自動監聽）。",
        "requires_windhawk": False,
        "experimental": False,
        "advisory": "",
        "framed": False,
        "actual_px": 48,
        "ico_suffix": "_framed_glass.ico",
        "params": _optical_params(),
    },
    "startmenu": {
        "id": "startmenu",
        "label": "開始功能表",
        "icon": "📋",
        "style": "Windhawk 選用擴充（即時引擎）",
        "summary": "需搭配 Windhawk；直掛 StartMenuExperienceHost 即時轉換（免改捷徑、Win32／商店 App／資料夾皆同步套用，隨時即時還原）。",
        "requires_windhawk": True,
        "experimental": False,
        "advisory": (
            "視覺建議：純冰白玻璃適合 Windows 深色模式或搭配 Windhawk 透明主題"
            "（如 Windows 11 Start Menu Styler）；在「淺色且不透明」開始選單上對比度較低，本模組不修改其他外掛配置。"
        ),
        "framed": False,
        "actual_px": 36,
        "ico_suffix": "_menu_glass.ico",
        "params": _optical_params(),
    },
    "taskbar": {
        "id": "taskbar",
        "label": "工作列",
        "icon": "📌",
        "style": "Windhawk 選用擴充（即時引擎）",
        "summary": "需搭配 Windhawk；僅轉換工作列 App 圖示為清透冰白光學玻璃（系統預設底部與置頂工作列皆獨立相容、完全不與其他工作列模組衝突）。",
        "requires_windhawk": True,
        "experimental": False,
        "advisory": (
            "視覺建議：建議搭配 Windows 深色模式，或加裝 Windhawk「Windows 11 Taskbar Styler"
            "（如 SimplyTransparent）」作為透明底座；若維持淺色不透明工作列，冰白對比會明顯降低。"
            "最左側 Windows 開始按鈕需另以 start-button-replacer 更換。"
        ),
        "framed": False,
        "actual_px": 36,
        "ico_suffix": "_taskbar_glass.ico",
        "params": _optical_params(),
    },
    "tray": {
        "id": "tray",
        "label": "通知列",
        "icon": "🔔",
        "style": "Windhawk 選用擴充（即時引擎）",
        "summary": (
            "需搭配 Windhawk；即時轉換右下角常駐系統匣與「^」溢位區圖示（支援 1024px 高解析點陣圖與 HWiNFO64 預設白名單），"
            "已納入「一鍵套用全部四區」，亦可隨時在此分頁個別獨立啟閉與調整。"
        ),
        "requires_windhawk": True,
        "experimental": False,
        "advisory": (
            "提示：已納入一鍵套用全部四區，亦可於本分頁獨立調整參數、單獨啟閉或編輯下方通知列排除白名單"
            "（預設保護 HWiNFO64 動態感測器保留原色、避免每秒重繪）。"
        ),
        "framed": False,
        "actual_px": 20,
        "ico_suffix": "_tray_glass.ico",
        "params": _optical_params(),
    },
}

# 來源與副檔名只在 SCOPE_SOURCES / SCOPE_EXTS 定義一次，這裡同步進 SPECS 供前端取用。
for _sid in SCOPES:
    SPECS[_sid]["sources"] = list(SCOPE_SOURCES[_sid])
    SPECS[_sid]["exts"] = list(SCOPE_EXTS[_sid])
    SPECS[_sid]["locked"] = is_scope_locked(_sid)


# Lines 267-325: 參數查詢、邊界限制與舊版設定檔遷移
def param_spec(scope: str, key: str) -> dict[str, Any] | None:
    """查詢指定區域與鍵值的滑桿規格；找不到時回傳 None。"""
    for param in SPECS[scope]["params"]:
        if param["key"] == key:
            return param
    return None


def default_params(scope: str) -> dict[str, float]:
    """回傳指定區域的預設光學參數字典。"""
    return {p["key"]: p["default"] for p in SPECS[scope]["params"]}


def clamp_params(scope: str, params: Any) -> dict[str, float]:
    """依規格補齊缺漏、轉型並夾在合法範圍內；不合法的值一律退回預設。"""
    src = params if isinstance(params, dict) else {}
    out: dict[str, float] = {}
    for p in SPECS[scope]["params"]:
        try:
            val = float(src.get(p["key"], p["default"]))
            if val != val or val in (float("inf"), float("-inf")):
                raise ValueError
        except (TypeError, ValueError):
            val = float(p["default"])
        out[p["key"]] = round(min(p["max"], max(p["min"], val)), 3)
    return out


DEFAULT_TRAY_WHITELIST = "HWiNFO64.exe;HWiNFO32.exe;HWiNFO;memreduct.exe;Mem Reduct"


def normalize_tray_whitelist(raw: Any) -> str:
    """正規化通知列白名單（分號／逗號／換行分隔，去重保留順序）；None 時回傳預設值。"""
    if raw is None:
        return DEFAULT_TRAY_WHITELIST
    if isinstance(raw, (list, tuple)):
        chunks = [str(x) for x in raw]
    else:
        chunks = str(raw).replace("\r", "\n").replace(",", ";").replace("\n", ";").split(";")
    seen: set[str] = set()
    cleaned: list[str] = []
    for part in chunks:
        tok = part.strip()
        if not tok:
            continue
        low = tok.casefold()
        if low in seen:
            continue
        seen.add(low)
        cleaned.append(tok)
    return ";".join(cleaned)[:1024]


def default_config() -> dict[str, Any]:
    """回傳完整且合法的初始設定字典。"""
    cfg: dict[str, Any] = {"version": VERSION}
    for scope in SCOPES:
        cfg[scope] = default_params(scope)
    cfg["tray_whitelist"] = DEFAULT_TRAY_WHITELIST
    cfg["windhawk_dir"] = ""
    cfg["watcher"] = default_watcher()
    cfg["lifecycle"] = default_lifecycle()
    return cfg


def migrate_config(raw: Any) -> dict[str, Any]:
    """讀入任何版本的 config.json，安全升級並輸出目前版本格式。"""
    cfg = default_config()
    if not isinstance(raw, dict):
        return cfg
    for scope in SCOPES:
        if isinstance(raw.get(scope), dict):
            if not any(k in raw[scope] for k in ("opacity", "frost", "dark_protect")):
                cfg[scope] = default_params(scope)
            else:
                cfg[scope] = clamp_params(scope, raw[scope])
    if not any(isinstance(raw.get(scope), dict) for scope in SCOPES):
        if any(k in raw for k in ("line_strength", "glow_amount", "film_opacity")):
            cfg["desktop"] = default_params("desktop")
    if "tray_whitelist" in raw:
        cfg["tray_whitelist"] = normalize_tray_whitelist(raw["tray_whitelist"])
    if isinstance(raw.get("windhawk_dir"), str):
        cfg["windhawk_dir"] = raw["windhawk_dir"].strip()[:512]
    if "watcher" in raw:
        cfg["watcher"] = clamp_watcher(raw["watcher"])
    if "lifecycle" in raw:
        cfg["lifecycle"] = clamp_lifecycle(raw["lifecycle"])
    return cfg
