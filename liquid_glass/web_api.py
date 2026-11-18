"""liquid_glass/web_api.py -- PyWebView 前端與 Python 後端橋接介面。

Functionality:
1. 曝露公開 API 方法給前端 control UI（狀態讀取、即時預覽圖產出、參數儲存、批次套用／還原）。
2. 提供管理員提權重啟（WizTree 風格非強制橫幅）與公用位置提權重試。
3. 提供開機自啟、桌面捷徑、Watcher 監控與退出生命週期開關控制。
"""
from __future__ import annotations

import base64
import io
import json
import os
import threading
from typing import Any

from liquid_glass import runtime as RT
from liquid_glass import shortcut_manager as SM
from liquid_glass import specs as S
from liquid_glass import tray_controller as TC
from liquid_glass import windhawk_bridge as WB


class LiquidGlassApi:
    """PyWebView JS-Python 橋接 API。內部狀態一律以底線開頭以免被 PyWebView 遞迴掃描。"""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._pcache: dict[tuple[str, str, float], dict[str, Any]] = {}
        self._plock = threading.Lock()

    # Lines 30-75: 狀態讀取與引擎遙測
    @staticmethod
    def _engine_statuses() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        return (
            WB.get_status(base_dir=RT.BASE_DIR, scope="taskbar"),
            WB.get_status(base_dir=RT.BASE_DIR, scope="startmenu"),
            WB.get_status(base_dir=RT.BASE_DIR, scope="tray"),
        )

    def get_state(self) -> dict[str, Any]:
        """回傳前端啟動所需完整狀態（純輕量讀取，不執行影像運算）。"""
        wc = self.get_watcher()
        tb_st, sm_st, tr_st = self._engine_statuses()
        wh_installed = bool(
            tb_st.get("windhawk_installed")
            or sm_st.get("windhawk_installed")
            or tr_st.get("windhawk_installed")
        )
        available = [sc for sc in S.ACTIVE_SCOPES if (not S.SPECS[sc].get("requires_windhawk") or wh_installed)]
        optional = [sc for sc in S.ACTIVE_SCOPES if S.SPECS[sc].get("requires_windhawk")]
        experimental = [sc for sc in S.ACTIVE_SCOPES if S.SPECS[sc].get("experimental")]
        lc = self._app.get_lifecycle()
        return {
            "version": S.VERSION,
            "scopes": list(S.SCOPES),
            "active_scopes": list(S.ACTIVE_SCOPES),
            "available_scopes": available,
            "optional_scopes": optional,
            "experimental_scopes": experimental,
            "locked_scopes": list(S.LOCKED_SCOPES),
            "default_scope": S.ACTIVE_SCOPES[0] if wh_installed else "desktop",
            "windhawk_installed": wh_installed,
            "windhawk_dir": tb_st.get("windhawk_dir") or sm_st.get("windhawk_dir") or tr_st.get("windhawk_dir") or "",
            "specs": S.SPECS,
            "config": {sc: self._app.config[sc] for sc in S.SCOPES},
            "autostart": RT.is_autostart_enabled(),
            "shortcut": RT.desktop_shortcut_exists(),
            "watcher": bool(wc["enabled"]),
            "watcher_locked": bool(wc.get("locked", False)),
            "watcher_config": wc,
            "lifecycle": lc,
            "restore_on_exit": bool(lc["restore_live_on_exit"]),
            "exit_on_close": bool(lc["exit_when_window_closes"]),
            "backup_count": SM.count_backups(),
            "is_admin": RT.is_admin(),
            "tray_running": TC.is_tray_running(),
            "taskbar_engine": tb_st,
            "startmenu_engine": sm_st,
            "tray_engine": tr_st,
        }

    def get_taskbar_status(self) -> dict[str, Any]:
        """取得工作列 Windhawk 模組即時狀態。"""
        return WB.get_status(base_dir=RT.BASE_DIR, scope="taskbar")

    def get_engine_status(self, scope: str = "taskbar") -> dict[str, Any]:
        """依區域名稱取得對應 Windhawk 即時引擎遙測狀態。"""
        sc = scope if scope in WB.MOD_SPECS else "taskbar"
        return WB.get_status(base_dir=RT.BASE_DIR, scope=sc)

    # Lines 77-135: 預覽圖渲染與滑桿參數儲存
    def preview(self, scope: str, params: dict[str, Any], dpr: float = 1.0, seq: int = 0) -> dict[str, Any]:
        """計算指定區域與滑桿參數之預覽 PNG Data URI，附帶序號以丟棄過期回應。"""
        if scope not in S.SCOPES:
            return {"seq": seq, "error": "bad scope"}
        if S.is_scope_locked(scope):
            return {"seq": seq, "locked": True, "error": "scope locked"}
        clamped = S.clamp_params(scope, params)
        try:
            scale = min(3.0, max(1.0, float(dpr)))
        except (TypeError, ValueError):
            scale = 1.0
        return dict(self._render_preview(scope, clamped, scale), seq=seq)

    def _render_preview(self, scope: str, params: dict[str, float], dpr: float) -> dict[str, Any]:
        from liquid_glass import renderer as R

        ck = (scope, json.dumps(params, sort_keys=True), round(dpr, 2))
        with self._plock:
            hit = self._pcache.get(ck)
        if hit:
            return hit

        big_css, act_css = 96, S.SPECS[scope]["actual_px"]

        def _enc(im: Any) -> str:
            buf = io.BytesIO()
            im.save(buf, format="PNG", compress_level=1)
            return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

        samples = R.sample_icons()
        out = {
            "scope": scope,
            "big_css": big_css,
            "actual_css": act_css,
            "big": [_enc(R.render_preview(scope, im, params, int(round(big_css * dpr)))) for im in samples],
            "actual": [_enc(R.render_preview(scope, im, params, int(round(act_css * dpr)))) for im in samples],
        }
        with self._plock:
            if len(self._pcache) > 24:
                self._pcache.clear()
            self._pcache[ck] = out
        return out

    def save_params(self, scope: str, params: dict[str, Any]) -> bool:
        """儲存區域參數並於 Windhawk 即時模組啟用中同步熱更新設定。"""
        if scope in S.SCOPES and not S.is_scope_locked(scope):
            self._app.config[scope] = S.clamp_params(scope, params)
            self._app.save_config()
            if scope in WB.MOD_SPECS:
                WB.update_settings_if_active(self._app.config[scope], scope=scope)
        return True

    # Lines 137-205: 套用 / 還原與系統管理員提權控制
    def _batch_response(self, kind: str, res: list[dict[str, Any]] | None) -> dict[str, Any]:
        ok = res is not None and not any(r.get("failed") for r in res)
        needs_admin = sum(r.get("needs_admin", 0) for r in (res or []))
        wh_needs_admin = any(r.get("windhawk_needs_admin") for r in (res or []))
        tb_st, sm_st, tr_st = self._engine_statuses()
        return {
            "ok": ok,
            "kind": kind,
            "needs_admin": needs_admin,
            "windhawk_needs_admin": wh_needs_admin,
            "windhawk_installed": bool(
                tb_st.get("windhawk_installed")
                or sm_st.get("windhawk_installed")
                or tr_st.get("windhawk_installed")
            ),
            "results": res,
            "message": SM.summarize(res, kind),
            "taskbar_engine": tb_st,
            "startmenu_engine": sm_st,
            "tray_engine": tr_st,
        }

    def apply_scope(self, scope: str, params: dict[str, Any]) -> dict[str, Any]:
        """套用單一區域 Liquid Glass。"""
        if scope not in S.SCOPES:
            return {"ok": False, "message": "未知的區域"}
        if S.is_scope_locked(scope):
            return {"ok": False, "locked": True, "message": f"「{S.SPECS[scope]['label']}」目前已鎖定"}
        self.save_params(scope, params)
        return self._batch_response("apply", SM.run_batch("apply", [scope], self._app.config))

    def restore_scope(self, scope: str) -> dict[str, Any]:
        """還原單一區域原生圖示。"""
        if scope not in S.SCOPES:
            return {"ok": False, "message": "未知的區域"}
        if S.is_scope_locked(scope):
            return {"ok": False, "locked": True, "message": f"「{S.SPECS[scope]['label']}」目前已鎖定"}
        return self._batch_response("restore", SM.run_batch("restore", [scope], self._app.config))

    def apply_all(self, all_params: dict[str, Any]) -> dict[str, Any]:
        """套用目前系統所有可用核心區域（未裝 Windhawk 時自動僅套用桌面，不含獨立測試分頁）。"""
        scopes = SM.effective_active_scopes(include_experimental=False) or ["desktop"]
        for sc in scopes:
            if isinstance(all_params, dict) and sc in all_params:
                self._app.config[sc] = S.clamp_params(sc, all_params[sc])
        self._app.save_config()
        return self._batch_response("apply", SM.run_batch("apply", scopes, self._app.config))

    def restore_all(self) -> dict[str, Any]:
        """還原目前系統所有可用區域（含獨立測試擴充，確保安全回復原廠外觀）。"""
        scopes = SM.effective_active_scopes(include_experimental=True) or ["desktop"]
        return self._batch_response("restore", SM.run_batch("restore", scopes, self._app.config))

    def get_progress(self) -> dict[str, Any]:
        """回傳桌面批次套用／還原的即時進度字典。"""
        return dict(SM.PROGRESS)

    def get_admin_result(self) -> dict[str, Any]:
        """讀取前次提權無主控台批次作業結果 JSON。"""
        try:
            if os.path.exists(RT.ADMIN_RESULT_PATH):
                with open(RT.ADMIN_RESULT_PATH, "r", encoding="utf-8") as handle:
                    return json.load(handle)
        except Exception:
            pass
        return {}

    def get_startup_scope(self) -> str:
        """取得啟動視窗指定之初始切換分頁（如提權重開後自動切回對應分頁）。"""
        sc = getattr(self._app, "initial_scope", "")
        return sc if sc in S.ACTIVE_SCOPES else ""

    def relaunch_admin(self, scope: str = "taskbar") -> dict[str, Any]:
        """非強制由使用者主動點擊以系統管理員身分重新開啟設定視窗。"""
        if RT.is_admin():
            return {"ok": False, "already_admin": True, "message": "目前已是系統管理員身分"}
        sc = scope if scope in S.ACTIVE_SCOPES else "taskbar"
        self._app._relaunching_admin = True
        if not RT.relaunch_as_admin(["--settings", "--scope", sc], show=1):
            self._app._relaunching_admin = False
            return {"ok": False, "message": "未取得系統管理員權限（UAC 已取消或無法提權）"}
        if (not self._app.get_lifecycle()["exit_when_window_closes"]) and (not TC.is_tray_running()):
            self._app._ensure_tray_spawned()
        win = getattr(self._app, "window", None)
        if win is not None:

            def _close_old() -> None:
                try:
                    win.destroy()
                except Exception:
                    pass

            timer = threading.Timer(0.25, _close_old)
            timer.daemon = True
            timer.start()
        return {"ok": True, "scope": sc, "message": "已啟動系統管理員視窗，正在重啟…"}

    def apply_admin(self, scope: str = "all") -> dict[str, Any]:
        """針對公用桌面等權限不足項目以系統管理員權限重試套用。"""
        return self._admin("--apply-admin", scope)

    def restore_admin(self, scope: str = "all") -> dict[str, Any]:
        """針對公用桌面等權限不足項目以系統管理員權限重試還原。"""
        return self._admin("--restore-admin", scope)

    @staticmethod
    def _admin(flag: str, scope: str) -> dict[str, Any]:
        if scope != "all" and scope not in S.SCOPES:
            return {"ok": False, "message": "未知的區域"}
        if scope != "all" and S.is_scope_locked(scope):
            return {"ok": False, "locked": True, "message": f"「{S.SPECS[scope]['label']}」目前已鎖定"}
        if RT.is_admin():
            return {"ok": False, "message": "目前已是系統管理員身分，請直接套用"}
        ok = RT.relaunch_as_admin([flag, scope])
        return {"ok": ok, "message": "已送出提權請求，請在 UAC 視窗按「是」" if ok else "未取得管理員權限（UAC 被取消或無法提權）"}

    # Lines 207-265: 系統整合、監控與生命週期開關
    def set_autostart(self, val: Any) -> bool:
        """設定或移除登入 Windows 自動啟動。"""
        return RT.set_autostart(bool(val))

    def set_shortcut(self, val: Any) -> bool:
        """建立或移除桌面捷徑。"""
        return RT.create_desktop_shortcut() if val else RT.remove_desktop_shortcut()

    def get_watcher(self) -> dict[str, Any]:
        """取得桌面即時監控開關與去抖參數。"""
        wc = S.clamp_watcher(self._app.config.get("watcher"))
        locked = all(S.is_scope_locked(sc) for sc in S.WATCHER_SCOPES)
        if locked:
            wc["enabled"] = False
        wc["locked"] = locked
        return wc

    def set_watcher(self, val: Any) -> bool:
        """啟閉桌面自動監控並於必要時啟動背景系統匣行程。"""
        wc = S.clamp_watcher(self._app.config.get("watcher"))
        locked = all(S.is_scope_locked(sc) for sc in S.WATCHER_SCOPES)
        wc["enabled"] = False if locked else bool(val)
        self._app.config["watcher"] = wc
        self._app.save_config(watcher_changed=True)
        if wc["enabled"] and not TC.is_tray_running():
            TC.spawn_background_tray()
        return bool(wc["enabled"])

    def set_watcher_debounce(self, sec: Any) -> float:
        """調整桌面監控去抖秒數並存入設定檔。"""
        wc = S.clamp_watcher(self._app.config.get("watcher"))
        wc["debounce_sec"] = S.clamp_watcher({"debounce_sec": sec})["debounce_sec"]
        self._app.config["watcher"] = wc
        self._app.save_config(watcher_changed=True)
        return float(wc["debounce_sec"])

    def set_restore_on_exit(self, val: Any) -> bool:
        """設定退出程式時是否自動關閉工作列／開始功能表 Windhawk 即時引擎。"""
        lc = self._app.get_lifecycle()
        lc["restore_live_on_exit"] = bool(val)
        self._app.config["lifecycle"] = lc
        self._app.save_config(lifecycle_changed=True)
        return bool(lc["restore_live_on_exit"])

    def set_exit_on_close(self, val: Any) -> bool:
        """設定關閉設定視窗時是否同步退出程式（關閉則縮小常駐系統匣通知區）。"""
        lc = self._app.get_lifecycle()
        lc["exit_when_window_closes"] = bool(val)
        self._app.config["lifecycle"] = lc
        self._app.save_config(lifecycle_changed=True)
        if (not lc["exit_when_window_closes"]) and (not TC.is_tray_running()):
            self._app._ensure_tray_spawned()
        return bool(lc["exit_when_window_closes"])

    def open_folder(self, which: str) -> bool:
        """在檔案總管開啟 IconVault 或 Original_Shortcuts_Backup 資料夾。"""
        path = {"vault": RT.ICON_VAULT_DIR, "backup": RT.BACKUP_DIR}.get(which)
        try:
            if path and RT.IS_WIN:
                os.startfile(path)
                return True
        except Exception:
            pass
        return False
