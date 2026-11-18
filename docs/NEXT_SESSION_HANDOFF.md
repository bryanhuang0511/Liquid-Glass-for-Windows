# 🧭 Liquid Glass for Windows — 交接紀錄暨下一版待辦報告（v2.1.0 ➔ v2.2.0）

> **建立日期**：2026-11-18  
> **目前版本**：`v2.1.0`（已完成測試並同步發布至 GitHub）  
> **用途**：當夥伴日後重新開啟本專案時，立即提醒下一步架構調整目標與目前穩定基準。

---

## 📌 1. 下次啟動優先提醒（Partner 指定待辦）

夥伴於 `v2.1.0` 驗收完成後明確指示，**下次重啟專案開發時，將「通知列（`tray`）」正式結束實驗隔離狀態，與工作列等區塊整併為「一次一鍵套用四區 ＋ 各區可個別獨立套用／還原」**：

1. **將「一鍵套用核心三區」升級為「一鍵套用四區」**：
   - 在 [`liquid_glass/specs.py`](../liquid_glass/specs.py) 移除或預設關閉 `SPECS["tray"]["experimental"] = True` 的批次排除限制（可保留各分頁獨立微調權限，並將 `VERSION` 升至 `2.2.0`）。
   - 在 [`liquid_glass/shortcut_manager.py`](../liquid_glass/shortcut_manager.py)（`effective_active_scopes()`）、[`liquid_glass/web_api.py`](../liquid_glass/web_api.py)（`apply_all`）與 [`main.py`](../main.py)（`--apply`）將 `tray` 納入預設全域批次套用清單（同時維持未安裝 Windhawk 時僅安全套用 `desktop` 的防呆）。
2. **評估「工作列（`taskbar`）」與「通知列（`tray`）」操作整合度**：
   - 依夥伴實際使用需求選擇：
     - **方案 A（推薦・邏輯分層清晰）**：介面仍保留四個設定分頁（讓兩套 C++ Windhawk 模組 `local@liquid-glass-taskbar-icons` 與 `local@liquid-glass-tray-icons` 的參數與通知列白名單可以各自獨立調整與單獨還原），同時把全區批次控制列（`#btnApplyAll` / `#btnRestoreAll` / CLI `--apply`）正式統一為 **「✨ 一鍵套用全部四區」**（工作列、開始功能表、桌面、通知列）與 **「🛡️ 一鍵還原全部四區」**。
     - **方案 B（單頁合併控制）**：若希望操作更精簡，也可在「工作列」分頁與全域控制加上連動開關，底層仍維持兩個獨立 Windhawk Mod 以避免單一模組失效波及另一區，或進一步評估合併單一 `explorer.exe` Mod 的編譯與除錯風險。
3. **同步更新提示文案與單元測試**：
   - 更新 [`ui/index.html`](../ui/index.html) 的 `#btnApplyAll`、`#batchNote`、頁籤副標題（移除「獨立測試（選用擴充）」試驗標記）。
   - 更新 [`tests/test_lifecycle_api.py`](../tests/test_lifecycle_api.py) 與 [`tests/test_shortcuts.py`](../tests/test_shortcuts.py) 原本斷言 `apply_all` 略過 `tray` 的測試案例。

---

## ✅ 2. `v2.1.0` 已完成驗證的穩定基準

- **Windhawk 同款關閉縮小常駐通知區**：關閉設定視窗移除工作列圖示並保留背景常駐與即時效果（標準／系統管理員權限、跨 UIPI 喚醒與防 WebView2 殘留 `os._exit(0)` 已驗證）。
- **通知列暨 `^` 溢位面板即時引擎（`windhawk/liquid_glass_tray.wh.cpp`）**：
  - `GetTaskbarXamlRoot` + `RunFromWindowThread` 主動冷啟動掃描 + `OverflowXamlIslandManager::Show` / `NotificationAreaOverflow` 延遲安全佇列。
  - **1024px 高解析點陣圖快速路徑**：解決 Antigravity（`512x512` PNG）原本超出 `256px` 上限落入 RTB 失敗的問題，先以 `PremultipliedResizeFloat` 降採樣至 `<= 256px` 再算圖。
  - **可編輯通知列排除白名單（`WhitelistApps` / `tray_whitelist`）**：預設保護 `HWiNFO64.exe;HWiNFO32.exe;HWiNFO`，透過 `INotificationAreaIcon6::AppFriendlyName` COM 介面與 `HKCU\Control Panel\NotifyIconSettings` 交叉對照，自動保留所有 HWiNFO64 動態感測器數值原色並略過每秒重繪。

---

## 🧹 3. 資料夾清理清單備忘（尚未擅自刪除之項目）

以下為本機資料夾中可視情況清理的產物（需由夥伴確認後執行，**絕對不可誤刪**使用者執行期檔案）：
- **可安全清理（視需要）**：`releases/LiquidGlassForWindows_v2.0.0_Portable.zip`（舊版封裝包）、`.pytest_cache/` 與各資料夾下 `__pycache__/`、標準權限舊快取 `.webview_profile/`，以及已合併的舊 Git 分支 `feat/v2.0-modular-release`。
- **務必保留（不可刪除）**：`LiquidGlassForWindows.exe`、`_internal/`、`config.json`、`IconVault/`、`Original_Shortcuts_Backup/`。
