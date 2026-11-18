"""glass_specs.py -- 三區風格規格、參數範圍、來源/副檔名規格、Watcher 參數與設定遷移。

純 Python：刻意不 import numpy / PIL / os，讓設定視窗與系統匣程序可以毫秒級取用。
滑桿的範圍、預設值、說明文字全部由這裡產生，前後端不會再有範圍不一致的問題。

v1.3.1（切片 1）新增：
  * 來源標籤 user / common（個人與公用位置），桌面與開始功能表會同時掃描兩處。
  * 副檔名規格：桌面支援 .lnk 與 .url（Steam 遊戲等網際網路捷徑）。
  * 母本備份目錄與 ICO 檔名的純字串規則（backup_parts / vault_tag），避免個人與公用同名衝突。
  * Watcher（檔案監控）去抖參數與設定欄位，設定檔多一個 "watcher" 區塊。
  * 結果欄位表 RESULT_FIELDS（新增 needs_admin，供 UI 顯示「以管理員權限重試此區」）。

本檔不改變任何渲染輸出，因此 RENDER_VERSION 維持不變，既有 ICO 快取不會失效。
"""

VERSION = "2.0.0"

# 演算法或輸出格式變動時 +1，IconVault 的渲染快取會自動失效並重新生成。
RENDER_VERSION = 7

SCOPES = ("desktop", "startmenu", "taskbar")
ACTIVE_SCOPES = ("taskbar", "startmenu", "desktop")
LOCKED_SCOPES = ()
ICO_SIZES = (256, 128, 96, 64, 48, 40, 36, 32, 24, 16)

# 工作列薄膜硬上限：即使設定檔被改壞或滑桿被拉滿，也不可能再變成實心白塊。
TASKBAR_FILM_MAX = 0.15


def is_scope_locked(scope):
    """回傳指定區域是否目前被鎖定（三區全面開放後僅拒絕未知範圍）。"""
    return scope in LOCKED_SCOPES or scope not in ACTIVE_SCOPES


# ============================================================================
# 來源標籤與副檔名
# ============================================================================
SOURCE_USER = "user"        # 個人位置：%USERPROFILE%\Desktop、%APPDATA%\...\Start Menu\Programs、工作列釘選
SOURCE_COMMON = "common"    # 公用位置：C:\Users\Public\Desktop、%ProgramData%\...\Start Menu\Programs
SOURCE_TAGS = (SOURCE_USER, SOURCE_COMMON)
SOURCE_LABELS = {SOURCE_USER: "個人", SOURCE_COMMON: "公用"}

# 修改該來源的捷徑通常需要管理員權限（公用桌面刪改會跳 UAC）。
# 平常維持一般權限，遇到 denied 時由 UI 提示「以管理員權限重新執行此區」。
SOURCE_NEEDS_ADMIN = {SOURCE_USER: False, SOURCE_COMMON: True}

# 每個範圍要掃描哪些來源、接受哪些副檔名（全部小寫）。
SCOPE_SOURCES = {
    "desktop": (SOURCE_USER, SOURCE_COMMON),
    "startmenu": (SOURCE_USER, SOURCE_COMMON),
    "taskbar": (SOURCE_USER,),
}
SCOPE_EXTS = {
    "desktop": (".lnk", ".url"),
    "startmenu": (".lnk",),
    "taskbar": (".lnk",),
}

# ---- .url（網際網路捷徑，INI 文字檔）----
URL_SECTION = "InternetShortcut"
URL_ICON_KEYS = ("IconFile", "IconIndex")
# 逐行讀寫時依序嘗試的編碼；寫回時必須沿用讀到的那一種，不可擅自轉碼。
URL_ENCODINGS = ("utf-8-sig", "utf-8", "cp950", "cp1252")

# ---- Microsoft Store / MSIX 捷徑（TargetPath 與 IconLocation 皆空，只有 AUMID）----
AUMID_SHELL_PREFIX = "shell:AppsFolder\\"


def aumid_shell_path(aumid):
    """AUMID（例如 'Canva.Affinity_8a0j1tnjnt4a4!Canva.Affinity'）→ 可交給 Shell 的解析路徑。"""
    return AUMID_SHELL_PREFIX + str(aumid).strip()


def shortcut_ext(scope, filename):
    """檔名若屬於該範圍可處理的捷徑類型，回傳小寫副檔名（如 '.url'），否則回傳 None。"""
    exts = SCOPE_EXTS.get(scope, ())
    low = str(filename).lower()
    for e in exts:
        if low.endswith(e):
            return e
    return None


def backup_parts(scope, source):
    """母本備份目錄（相對於 Original_Shortcuts_Backup）的路徑片段。

    桌面的個人位置沿用 v1.2.0 / v1.3.0 的平面佈局（空 tuple），既有母本完全不用搬動。
    """
    if scope not in SCOPE_SOURCES:
        raise ValueError("unknown scope: %r" % (scope,))
    if source not in SOURCE_TAGS:
        raise ValueError("unknown source: %r" % (source,))
    if scope == "desktop":
        return () if source == SOURCE_USER else ("PublicDesktop",)
    if scope == "taskbar":
        return ("Taskbar",)
    return ("StartMenu", source)


def vault_tag(scope, source, ext):
    """ICO 檔名中介字串（放在『安全名稱』與『風格後綴』之間），避免不同來源或副檔名撞名。

    桌面公用位置 → '_pub'；任何範圍的 .url → '_url'；兩者並存 → '_pub_url'；其餘為空字串。
    開始功能表已由呼叫端用路徑雜湊區分，因此不在此處加標籤，維持既有檔名不變。
    """
    tag = ""
    if scope == "desktop" and source == SOURCE_COMMON:
        tag += "_pub"
    if str(ext).lower() == ".url":
        tag += "_url"
    return tag


# ============================================================================
# Watcher（檔案監控）
# ============================================================================
# 監聽到變動後，等到「安靜了 debounce_sec 秒」才觸發一次增量套用（快取命中者不會重畫）。
WATCHER_DEFAULTS = {"enabled": False, "debounce_sec": 3.0}
WATCHER_LIMITS = {"debounce_sec": (1.0, 30.0)}
WATCHER_MAX_WAIT_SEC = 60.0          # 事件持續不斷時，最晚也要在這個秒數內觸發一次
WATCHER_SELF_IGNORE_SEC = 5.0        # 本工具自己寫捷徑造成的事件，在此秒數內忽略，避免無限迴圈
WATCHER_FALLBACK_RESCAN_SEC = 900.0  # 保底定期全掃（監控事件可能遺漏）；0 代表停用
WATCHER_SCOPES = ("desktop",)          # 工作列與開始功能表已改用 Windhawk 即時引擎，不再監看 .lnk 捷徑檔


def default_watcher():
    return dict(WATCHER_DEFAULTS)


def clamp_watcher(raw):
    """接受 dict、舊式布林或任何垃圾；輸出一定合法的 {'enabled': bool, 'debounce_sec': float}。"""
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
        v = float(raw.get("debounce_sec", out["debounce_sec"]))
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError
    except (TypeError, ValueError):
        v = WATCHER_DEFAULTS["debounce_sec"]
    out["debounce_sec"] = round(min(hi, max(lo, v)), 2)
    return out


# ============================================================================
# 程式生命週期（退出時是否自動還原即時引擎 / 關閉視窗是否結束系統匣）
# ============================================================================
LIFECYCLE_DEFAULTS = {
    "restore_live_on_exit": False,     # 結束圖示中樞時自動停用工作列／開始功能表 Windhawk 引擎，回復原廠外觀
    "exit_when_window_closes": False,  # 關閉設定視窗時直接結束圖示中樞，不再常駐背景系統匣
}


def default_lifecycle():
    return dict(LIFECYCLE_DEFAULTS)


def clamp_lifecycle(raw):
    """正規化 lifecycle 設定，確保布林欄位安全合法。"""
    out = default_lifecycle()
    if not isinstance(raw, dict):
        return out
    for k in LIFECYCLE_DEFAULTS:
        v = raw.get(k, out[k])
        if isinstance(v, bool):
            out[k] = v
        elif isinstance(v, int) and v in (0, 1):
            out[k] = bool(v)
    return out


# ============================================================================
# 套用 / 還原 結果欄位
# ============================================================================
RESULT_FIELDS = ("applied", "unchanged", "no_backup", "denied", "failed", "no_icon",
                 "backed_up", "restored", "untouched",
                 "needs_admin")   # needs_admin：denied 之中，位於「需管理員」來源（公用位置）的件數


def new_result(scope, total):
    res = {"scope": scope, "total": total}
    for k in RESULT_FIELDS:
        res[k] = 0
    return res


# ============================================================================
# 三區風格規格
# ============================================================================
def _p(key, label, desc, lo, hi, step, default):
    return {"key": key, "label": label, "desc": desc,
            "min": lo, "max": hi, "step": step, "default": default}


def _optical_params():
    return [
        _p("opacity", "玻璃主體透光", "彩色與暗部轉清透玻璃的基礎不透明度倍率", 0.20, 1.80, 0.05, 1.00),
        _p("frost", "霜白高光濃度", "純白與亮面圖騰的半透明霜白飽和度（嚴格低於實心死白）", 0.20, 1.30, 0.05, 1.00),
        _p("glow", "邊緣折光反光", "原位內縮斜角（Rim）與色塊交界捕光（Seam）強度", 0.30, 1.80, 0.05, 1.00),
        _p("dark_protect", "深暗標誌保護", "全暗標誌的玻璃底層亮度補償（亮底黑字維持透空）", 0.50, 2.00, 0.05, 1.00),
    ]


SPECS = {
    "desktop": {
        "id": "desktop",
        "label": "桌面",
        "icon": "🖥️",
        "style": "即時光學液態玻璃（桌面獨立核心）",
        "summary": "免外掛獨立運作；高透光清透本體＋銳利加厚外緣斜角（Rim）與內部接縫捕光（支援個人／公用捷徑與自動監聽）。",
        "requires_windhawk": False,
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
        "advisory": "視覺建議：純冰白玻璃適合 Windows 深色模式或搭配 Windhawk 透明主題（如 Windows 11 Start Menu Styler）；在「淺色且不透明」開始選單上對比度較低，本模組不修改其他外掛配置。",
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
        "advisory": "視覺建議：建議搭配 Windows 深色模式，或加裝 Windhawk「Windows 11 Taskbar Styler（如 SimplyTransparent）」作為透明底座；若維持淺色不透明工作列，冰白對比會明顯降低。最左側 Windows 開始按鈕需另以 start-button-replacer 更換。",
        "framed": False,
        "actual_px": 36,
        "ico_suffix": "_taskbar_glass.ico",
        "params": _optical_params(),
    },
}

# 來源與副檔名只在 SCOPE_SOURCES / SCOPE_EXTS 定義一次，這裡同步進 SPECS（前端 get_state 可直接取用）。
for _sid in SCOPES:
    SPECS[_sid]["sources"] = list(SCOPE_SOURCES[_sid])
    SPECS[_sid]["exts"] = list(SCOPE_EXTS[_sid])
    SPECS[_sid]["locked"] = is_scope_locked(_sid)


def param_spec(scope, key):
    for p in SPECS[scope]["params"]:
        if p["key"] == key:
            return p
    return None


def default_params(scope):
    return {p["key"]: p["default"] for p in SPECS[scope]["params"]}


def clamp_params(scope, params):
    """依規格補齊缺漏、轉型並夾在合法範圍內。不合法的值一律退回預設。"""
    src = params if isinstance(params, dict) else {}
    out = {}
    for p in SPECS[scope]["params"]:
        try:
            v = float(src.get(p["key"], p["default"]))
            if v != v or v in (float("inf"), float("-inf")):
                raise ValueError
        except (TypeError, ValueError):
            v = p["default"]
        out[p["key"]] = round(min(p["max"], max(p["min"], v)), 3)
    return out


def default_config():
    cfg = {"version": VERSION}
    for s in SCOPES:
        cfg[s] = default_params(s)
    cfg["watcher"] = default_watcher()
    cfg["lifecycle"] = default_lifecycle()
    return cfg


def migrate_config(raw):
    """讀入任何版本的 config.json，輸出目前版本格式。"""
    cfg = default_config()
    if not isinstance(raw, dict):
        return cfg
    for s in SCOPES:
        if isinstance(raw.get(s), dict):
            # 舊版 v1.x/v2.0 使用線框參數（line/film）者，升級至光學模型時以合法預設重建。
            if not any(k in raw[s] for k in ("opacity", "frost", "dark_protect")):
                cfg[s] = default_params(s)
            else:
                cfg[s] = clamp_params(s, raw[s])
    if not any(isinstance(raw.get(s), dict) for s in SCOPES):
        # 最早期 v1.2 單層格式已升級為光學模型預設值，絕不將舊白塊係數套進新桌面或工作列。
        if any(k in raw for k in ("line_strength", "glow_amount", "film_opacity")):
            cfg["desktop"] = default_params("desktop")
    if "watcher" in raw:
        cfg["watcher"] = clamp_watcher(raw["watcher"])
    if "lifecycle" in raw:
        cfg["lifecycle"] = clamp_lifecycle(raw["lifecycle"])
    return cfg
