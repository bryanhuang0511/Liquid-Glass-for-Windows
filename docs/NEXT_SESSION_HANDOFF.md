# 🧭 Liquid Glass for Windows — 交接紀錄與完成歸檔（v2.1.0 ➔ v2.2.0）

> **建立日期**：2026-11-18  
> **目前版本**：`v2.2.0`（已完成四區一鍵套用整併、實機驗收並發布至 GitHub）  
> **狀態**：**✅ 原先排入之 `v2.2.0` 整併項目已全部提前完成並驗收收工**。

---

## ✅ 1. 已於 `v2.2.0` 完成整併之項目（原待辦已清空）

1. **將「一鍵套用核心三區」升級為「一鍵套用全部四區 ＋ 各區獨立控制」**（已完成）：
   - [`liquid_glass/specs.py`](../liquid_glass/specs.py) 已移除 `tray` 實驗隔離（`experimental: False`），`VERSION` 升至 `2.2.0`。
   - [`liquid_glass/shortcut_manager.py`](../liquid_glass/shortcut_manager.py)（`effective_active_scopes()`）、[`liquid_glass/web_api.py`](../liquid_glass/web_api.py)（`apply_all`）與 [`main.py`](../main.py)（`--apply`）已正式將 `tray` 納入全區一鍵套用，未安裝 Windhawk 時仍安全僅套用 `desktop`。
   - 保留四區分頁獨立調整與啟閉權限，並於 [`liquid_glass/tray_controller.py`](../liquid_glass/tray_controller.py) 系統匣右鍵選單頂端新增「✨ 一鍵套用全部四區」／「🛡️ 一鍵還原全部區域」。
   - [`ui/index.html`](../ui/index.html) 已移除通知列虛線試驗標籤並全面更新文案與版本號；[`tests/test_specs.py`](../tests/test_specs.py) 與 [`tests/test_lifecycle_api.py`](../tests/test_lifecycle_api.py) 亦同步通過。

---

## ✅ 2. `v2.1.0` 已完成驗證的穩定基準

- **Windhawk 同款關閉縮小常駐通知區**：關閉設定視窗移除工作列圖示並保留背景常駐與即時效果（標準／系統管理員權限、跨 UIPI 喚醒與防 WebView2 殘留 `os._exit(0)` 已驗證）。
- **通知列暨 `^` 溢位面板即時引擎（`windhawk/liquid_glass_tray.wh.cpp`）**：
  - `GetTaskbarXamlRoot` + `RunFromWindowThread` 主動冷啟動掃描 + `OverflowXamlIslandManager::Show` / `NotificationAreaOverflow` 延遲安全佇列。
  - **1024px 高解析點陣圖快速路徑**：解決 Antigravity（`512x512` PNG）原本超出 `256px` 上限落入 RTB 失敗的問題，先以 `PremultipliedResizeFloat` 降採樣至 `<= 256px` 再算圖。
  - **可編輯通知列排除白名單（`WhitelistApps` / `tray_whitelist`）**：預設保護 `HWiNFO64.exe;HWiNFO32.exe;HWiNFO`，透過 `INotificationAreaIcon6::AppFriendlyName` COM 介面與 `HKCU\Control Panel\NotifyIconSettings` 交叉對照，自動保留所有 HWiNFO64 動態感測器數值原色並略過每秒重繪。

---

## 🧹 3. 收尾保留項目說明

- **本機運行必要檔案（務必持續保留）**：`LiquidGlassForWindows.exe`、`_internal/`、`config.json`、`IconVault/`、`Original_Shortcuts_Backup/`。
- **後續若重返擴充時建議評估方向**：目前所有區域功能均已完整就緒；未來如有需要，僅剩純視覺進階自訂項目（例如外框強度微調、特定捷徑圖示黑／白名單或自動開機延遲策略）。
