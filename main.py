"""main.py -- Liquid Glass for Windows 應用程式主入口與 CLI 調度器。

Functionality:
1. 支援圖形設定介面啟動（--settings / 無參數預設）與無主控台背景系統匣啟動（--autostart）。
2. 支援無介面批次指令模式：--apply / --restore [desktop|startmenu|taskbar|all]。
3. 支援系統管理員權限重試模式：--apply-admin / --restore-admin [scope|all]。
4. 啟動時不預先載入 NumPy / PyWebView，保持系統匣常駐最小記憶體佔用。
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Sequence

from liquid_glass import runtime as RT
from liquid_glass import shortcut_manager as SM
from liquid_glass import specs as S
from liquid_glass import tray_controller as TC


# Lines 22-85: CLI 參數解析與批次作業分支
def _parse_target(argv: Sequence[str], flag: str) -> tuple[str, list[str]]:
    idx = list(argv).index(flag)
    target = argv[idx + 1] if idx + 1 < len(argv) and not str(argv[idx + 1]).startswith("--") else "all"
    scopes = list(S.ACTIVE_SCOPES) if target == "all" else [target]
    return target, scopes


def _validate_scopes(scopes: list[str]) -> int | None:
    if any(sc not in S.SCOPES for sc in scopes):
        print("scope must be one of: all, " + ", ".join(S.SCOPES))
        return 2
    locked = [sc for sc in scopes if S.is_scope_locked(sc)]
    if locked:
        labels = "、".join(S.SPECS[sc]["label"] for sc in locked)
        print(f"已鎖定區域不允許執行：{labels}（目前僅開放：{', '.join(S.ACTIVE_SCOPES)}）")
        return 3
    return None


def _cli(app: TC.LiquidGlassApp, argv: Sequence[str]) -> int:
    """無介面批次套用或還原（未偵測到 Windhawk 時自動略過選用區域）。"""
    kind = "apply" if "--apply" in argv else "restore"
    flag = "--apply" if kind == "apply" else "--restore"
    target, scopes = _parse_target(argv, flag)
    err = _validate_scopes(scopes)
    if err is not None:
        return err
    skipped_opt: list[str] = []
    if target == "all":
        avail = SM.effective_active_scopes() or ["desktop"]
        skipped_opt = [sc for sc in scopes if sc not in avail]
        scopes = avail
    res = SM.run_batch(kind, scopes, app.config)
    msg = SM.summarize(res, kind)
    if skipped_opt:
        opt_labels = "、".join(S.SPECS[sc]["label"] for sc in skipped_opt)
        msg += f"（未偵測到 Windhawk，略過選用擴充：{opt_labels}）"
    print(msg)
    return 0


def _cli_admin(app: TC.LiquidGlassApp, argv: Sequence[str]) -> int:
    """提權批次執行公用位置項目並回寫 admin_last_result.json。"""
    kind = "apply" if "--apply-admin" in argv else "restore"
    flag = "--apply-admin" if kind == "apply" else "--restore-admin"
    target, scopes = _parse_target(argv, flag)
    err = _validate_scopes(scopes)
    if err is not None:
        return err
    if not RT.is_admin():
        ok = RT.relaunch_as_admin([flag, target])
        print("已送出提權請求" if ok else "未取得管理員權限（UAC 被取消或無法提權）")
        return 0 if ok else 1
    sources = tuple(src for src, need in S.SOURCE_NEEDS_ADMIN.items() if need)
    res = SM.run_batch(kind, scopes, app.config, sources=sources)
    msg = SM.summarize(res, kind)
    print(msg)
    try:
        RT.atomic_write_json(
            RT.ADMIN_RESULT_PATH,
            {"kind": kind, "scopes": scopes, "message": msg, "results": res, "time": time.time()},
        )
    except Exception:
        pass
    return 0


def _parse_startup_scope(argv: Sequence[str] | None) -> str:
    """解析啟動參數中的 --scope / --scope= 以決定 UI 預設停駐分頁。"""
    for i, arg in enumerate(argv or ()):
        if arg == "--scope" and argv is not None and i + 1 < len(argv):
            val = str(argv[i + 1]).strip().lower()
            if val in S.ACTIVE_SCOPES:
                return val
        elif isinstance(arg, str) and arg.startswith("--scope="):
            val = arg.split("=", 1)[1].strip().lower()
            if val in S.ACTIVE_SCOPES:
                return val
    return ""


def _run_self_check() -> int:
    """無副作用自我診斷：確認打包執行檔內嵌核心套件與必要靜態資源皆完整可用。"""
    import numpy  # noqa: F401
    import pystray  # noqa: F401
    import webview  # noqa: F401
    from PIL import Image  # noqa: F401
    from liquid_glass import renderer as _R  # noqa: F401
    from liquid_glass import windhawk_bridge as _WB

    report = {
        "version": S.VERSION,
        "ui_html": os.path.exists(RT.UI_HTML_PATH),
        "app_ico": os.path.exists(RT.resolve_app_icon_path()),
        "app_png": os.path.exists(RT.resolve_app_png_path()),
        "taskbar_mod_cpp": _WB.find_source_cpp(RT.BASE_DIR, scope="taskbar") is not None,
        "startmenu_mod_cpp": _WB.find_source_cpp(RT.BASE_DIR, scope="startmenu") is not None,
        "tray_mod_cpp": _WB.find_source_cpp(RT.BASE_DIR, scope="tray") is not None,
    }
    print(json.dumps(report, ensure_ascii=False))
    return 0 if all(report.values()) else 1


# Lines 87-125: 主進入點
def main(argv: Sequence[str] | None = None) -> int:
    """應用程式主入口函式。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" in args:
        print(S.VERSION)
        return 0
    if "--check" in args:
        return _run_self_check()
    RT.ensure_dirs()
    app = TC.LiquidGlassApp()
    if "--apply-admin" in args or "--restore-admin" in args:
        return _cli_admin(app, args)
    if "--apply" in args or "--restore" in args:
        return _cli(app, args)
    if "--autostart" in args:
        if TC.acquire_tray_mutex():
            app.run_tray_blocking()
        return 0
    app.open_settings_window(initial_scope=_parse_startup_scope(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
