# Liquid Glass for Windows

[English](README.md) | [中文 (繁體)](README_zh.md)

Windows 11 液態玻璃圖示轉換中樞與模組化渲染引擎（`v3.0.0`）。可獨立為桌面捷徑生成多解析度（`256px` 至 `16px`）毛玻璃 `.ico` 圖示並保留原始備份；亦支援串接 Windhawk，為工作列、開始功能表（含內部資料夾彈窗）與系統匣提供即時繪製擴充。

---

## 檔案清單與元件職責

| 路徑 | 角色定位 | 核心職責 |
|---|---|---|
| [`main.py`](main.py) | CLI 與程式入口 | 分派設定介面（`--settings`）、系統匣常駐（`--autostart`）、無頭 CLI（`--apply` / `--restore`）及提權重試（`--apply-admin` / `--restore-admin`） |
| [`liquid_glass/`](liquid_glass/README.md) | Python 核心套件 | 拆分為 `specs.py`、`runtime.py`、`renderer.py`、`windhawk_bridge.py`、`shortcut_manager.py`、`desktop_watcher.py`、`tray_controller.py` 與 `web_api.py` |
| [`ui/`](ui/README.md) | 控制中心介面 | 基於 PyWebView EdgeChromium 的控制台，提供多 DPI 預覽、引擎狀態輪詢與各區獨立開關 |
| [`windhawk/`](windhawk/README.md) | 選用 C++ 模組 | Windhawk 模組原始碼（`liquid_glass_taskbar.wh.cpp`、`liquid_glass_startmenu.wh.cpp`、`liquid_glass_tray.wh.cpp`），內附 64 位元預編譯 `.dll` / `.whl` 備援 |
| [`assets/`](assets/) | 應用資源檔 | 應用程式圖示（`app.ico`、`app.png`）與選用開始按鈕覆蓋圖（`start_button/windows11_start_liquid_glass.png`） |
| [`tests/`](tests/) | 自動化測試集 | 包含 `93` 項 `pytest` 測試，驗證光學數學、`.url` 往返、資料夾監控、Windhawk 登錄檔／UTF-16 INI／自動安裝、`--check` 與結束生命週期 |
| [`docs/`](docs/ARCHITECTURE.md) | 工程技術文件 | 系統架構說明、[`CRASH_REPORT_SystemTray_Overflow.md`](docs/CRASH_REPORT_SystemTray_Overflow.md) 與 [`NEXT_SESSION_HANDOFF.md`](docs/NEXT_SESSION_HANDOFF.md) |
| [`指引.md`](指引.md) / [`CHANGELOG.md`](CHANGELOG.md) | 詳細指南與更新紀錄 | 完整中文技術指引與版本變更紀錄 |

---

## 核心架構與功能

* **獨立桌面核心**：支援使用者與公用桌面捷徑（`.lnk`、`.url` 與 MSIX/AUMID 市集應用），於 `Original_Shortcuts_Backup/` 保存原始位元備份，並在版本升級時自動更新過期備份。
* **選用 Windhawk 即時模式**：自動偵測標準安裝（`C:\Program Files\Windhawk`）、可攜目錄、執行中的 `windhawk.exe` 或手動指定路徑（`windhawk_dir`）。於 GPU/WIC 記憶體中即時繪製圖示（`<= 12 MiB` LRU 快取、不寫入磁碟捷徑），涵蓋開始功能表展開資料夾（`StartFolderModal` / `PopupRoot`）、`1024px` 系統匣圖示，以及可設定的處理程序排除白名單（預設排除 `HWiNFO64.exe;HWiNFO32.exe;HWiNFO;memreduct.exe;Mem Reduct`）。
* **四區統一與分頁獨立控制**：支援四區一鍵套用／還原，同時保留各分頁參數與開關獨立設定。
* **光學著色器一致化**：Python float32 與 C++ 實作採用統一高光上限（`<= 0.90` Alpha）、左上方向 Fresnel 邊緣與色縫偵測。
* **背景桌面監控**：以 Win32 `ReadDirectoryChangesW` 監聽新增桌面捷徑並防抖（`5s` 自我寫入抑制），自動套用液態玻璃圖示。

---

## 安裝與使用

### 可攜免安裝版

1. 自 [GitHub Releases](https://github.com/bryanhuang0511/Liquid-Glass-for-Windows/releases) 下載 **`LiquidGlassForWindows_v3.0.0_Portable.zip`**。
2. 解壓縮至固定資料夾（例如 `Tools\Liquid_Glass_for_Windows`），確保 Windows 捷徑圖示路徑有效。
3. 執行 **`LiquidGlassForWindows.exe`**（或 **`啟動圖示中樞.bat`**）。
4. *（選用）* 安裝 [Windhawk](https://windhawk.net/)，於介面重新偵測並套用即時擴充。

執行期資料（`config.json`、`IconVault/`、`Original_Shortcuts_Backup/`）均存放於解壓目錄內。刪除資料夾前請先於程式內執行圖示還原。

### 原始碼建置與測試

```powershell
# 1. 安裝相依套件（建議 Python 3.12+，Windows 11 x64）
pip install -r requirements.txt pytest

# 2. 執行單元與光學測試
python -m pytest tests -q

# 3. 由原始碼啟動控制中心介面
python main.py

# 4. 打包單一執行檔目錄與可攜 ZIP（releases/）
.\build.ps1 -PackageZip
.\verify_build.ps1
```

---

## 授權條款

本專案採用 [MIT License](LICENSE) 授權條款釋出。
