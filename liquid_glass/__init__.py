"""liquid_glass -- Liquid Glass for Windows 核心套件。

將桌面光學玻璃圖示算圖、Windhawk 即時引擎橋接、捷徑讀寫備份、
桌面監控、Win32 系統匣與 PyWebView 控制介面拆分為單一職責模組，
以便於維護、單元測試與 PyInstaller 可攜式封裝。
"""
from __future__ import annotations

from liquid_glass.specs import VERSION

APP_NAME = "LiquidGlassForWindows"
APP_TITLE = "Liquid Glass for Windows"
EXE_NAME = "LiquidGlassForWindows.exe"
SELF_SHORTCUT_NAME = "Liquid Glass 圖示中樞.lnk"

__version__ = VERSION

__all__ = [
    "APP_NAME",
    "APP_TITLE",
    "EXE_NAME",
    "SELF_SHORTCUT_NAME",
    "__version__",
]
