# 🧭 Liquid Glass for Windows — 交接紀錄與完成歸檔（v2.1.0 ➔ v3.0.0）

> **建立日期**：2026-11-18（`v3.0.0` 更新於 2026-11-20）  
> **目前版本**：`v3.0.0`（已完成零警告自動引導流程、UTF-16 LE `windhawk.ini` 修復、跨 Win11 通知列 Hook 強化與 `ModTasksDialogDelay` 控制）  
> **狀態**：**✅ 已完成建置與測試驗證，待夥伴實機驗收後發布 `v3.0.0`**。

---

## ✅ 1. `v3.0.0` 跨機相容與零警告自動配置升級（2026-11-20）

- **UTF-16 LE BOM `windhawk.ini` 與錯置檔清理**：修正標準版（`Portable=0`）`windhawk.ini` 讀取編碼問題並自動清除 `<WindhawkRoot>\AppData\Engine\Mods\local@liquid-glass-*`。
- **通知列跨 Win11 版本 Hook 相容**：`liquid_glass_tray.wh.cpp` 將 `SystemTrayFrame::ArrangeOverride` 轉為選用並加入 `MeasureOverride` 與 `GetTaskbarXamlRoot` 保底。
- **零警告引導與自動配置**：移除警告橫幅，未裝 Windhawk 時僅開桌面區，並提供 `auto_setup_windhawk()`、5 秒背景偵測、`initializing_symbols` 狀態顯示及 `swHideWhTasks`（`ModTasksDialogDelay`）可選開關。

## ✅ 2. `v2.2.2` 開始選單內層資料夾、跨機預編譯備援與引導套用升級（2026-11-19）

- **開始功能表內層展開資料夾圖示轉換（`windhawk/liquid_glass_startmenu.wh.cpp`）**：
  - 向上追溯至 `RootVisual`（含 `PopupRoot`）、走訪 `Popup.Child()` 與 `GetOpenPopupsForXamlRoot`，並於 `SetBitmapAsync_Hook` 針對 `16..256px` 圖示直接行內轉換（搭配 `IsDisallowedStartMenuUiImage` 自動排除並保護 `UserTile`、`PersonPicture`、`SearchBox` 等非應用圖示），解決點開「所有應用程式」或釘選區內層資料夾（如 `Chrome 應用程式`）時內部圖示未轉換的問題。
- **Windhawk 跨機器免編譯器預編譯備援與引導套用流程**：
  - `build.ps1` 自動將編譯好的三區 `.dll` 與 `libc++.whl`、`libunwind.whl`、`windhawk-mod-shim.dll` 封入 `windhawk/prebuilt/`；對方電腦若為 Windhawk 2.0 預設無 `clang++.exe` 環境，自動退回部署隨包預編譯模組。
  - 新增執行中 `windhawk.exe` 行程路徑反查（`QueryFullProcessImageNameW`）、多磁碟／Scoop 掃描、UI 手動瀏覽選取 `windhawk.exe` 並持久化至 `config.json["windhawk_dir"]`，以及標準安裝版（`Portable=0`）一鍵 UAC 管理員授權套用按鈕。
- **預設通知列白名單擴充**：`DEFAULT_TRAY_WHITELIST` 預設納入 `memreduct.exe;Mem Reduct`。

## ✅ 3. `v2.2.1` 介面表頭相容性小修補（2026-11-19）

- 修正 `windhawk/liquid_glass_taskbar.wh.cpp`、`windhawk/liquid_glass_startmenu.wh.cpp`、`windhawk/liquid_glass_tray.wh.cpp` 第 2 行表頭 `// @id`，移除多餘 `local@` 前綴並維持儲存檔名使用 `local@*.wh.cpp`／`local@*.ini`，徹底消除 Windhawk 主程式視窗右下角紅字 `Mod id must only contain the following characters: 0-9, a-z, and a hyphen (-)` 並恢復模組名稱／說明／版本顯示。
- 於 `tests/test_windhawk_bridge.py` 新增 `test_windhawk_source_metadata_matches_windhawk_validator` 單元防呆測試。

## ✅ 4. 已於 `v2.2.0` 完成整併之項目（原待辦已清空）

1. **將「一鍵套用核心三區」升級為「一鍵套用全部四區 ＋ 各區獨立控制」**（已完成）：
   - [`liquid_glass/specs.py`](../liquid_glass/specs.py) 已移除 `tray` 實驗隔離（`experimental: False`），`VERSION` 升至 `2.2.0`。
   - [`liquid_glass/shortcut_manager.py`](../liquid_glass/shortcut_manager.py)（`effective_active_scopes()`）、[`liquid_glass/web_api.py`](../liquid_glass/web_api.py)（`apply_all`）與 [`main.py`](../main.py)（`--apply`）已正式將 `tray` 納入全區一鍵套用，未安裝 Windhawk 時仍安全僅套用 `desktop`。
   - 保留四區分頁獨立調整與啟閉權限，並於 [`liquid_glass/tray_controller.py`](../liquid_glass/tray_controller.py) 系統匣右鍵選單頂端新增「✨ 一鍵套用全部四區」／「🛡️ 一鍵還原全部區域」。
   - [`ui/index.html`](../ui/index.html) 已移除通知列虛線試驗標籤並全面更新文案與版本號；[`tests/test_specs.py`](../tests/test_specs.py) 與 [`tests/test_lifecycle_api.py`](../tests/test_lifecycle_api.py) 亦同步通過。

---

## ✅ 3. `v2.1.0` 已完成驗證的穩定基準

- **Windhawk 同款關閉縮小常駐通知區**：關閉設定視窗移除工作列圖示並保留背景常駐與即時效果（標準／系統管理員權限、跨 UIPI 喚醒與防 WebView2 殘留 `os._exit(0)` 已驗證）。
- **通知列暨 `^` 溢位面板即時引擎（`windhawk/liquid_glass_tray.wh.cpp`）**：
  - `GetTaskbarXamlRoot` + `RunFromWindowThread` 主動冷啟動掃描 + `OverflowXamlIslandManager::Show` / `NotificationAreaOverflow` 延遲安全佇列。
  - **1024px 高解析點陣圖快速路徑**：解決 Antigravity（`512x512` PNG）原本超出 `256px` 上限落入 RTB 失敗的問題，先以 `PremultipliedResizeFloat` 降採樣至 `<= 256px` 再算圖。
  - **可編輯通知列排除白名單（`WhitelistApps` / `tray_whitelist`）**：預設保護 `HWiNFO64.exe;HWiNFO32.exe;HWiNFO`，透過 `INotificationAreaIcon6::AppFriendlyName` COM 介面與 `HKCU\Control Panel\NotifyIconSettings` 交叉對照，自動保留所有 HWiNFO64 動態感測器數值原色並略過每秒重繪。

---

## 🧹 4. 收尾保留項目說明

- **本機運行必要檔案（務必持續保留）**：`LiquidGlassForWindows.exe`、`_internal/`、`config.json`、`IconVault/`、`Original_Shortcuts_Backup/`。
- **後續若重返擴充時建議評估方向**：目前所有區域功能均已完整就緒；未來如有需要，僅剩純視覺進階自訂項目（例如外框強度微調、特定捷徑圖示黑／白名單或自動開機延遲策略）。
