# 系統匣隱藏圖示溢位區（`^`）閃退診斷與修復紀錄報告

- **建立日期**：2026-11-18
- **影響範圍**：僅限先前實驗性唯讀探針 `local@liquid-glass-tray-discovery`（目前已完整停用並移除；**不影響**正式版「工作列」與「開始功能表」兩顆模組）。

---

## 1. 故障現象與事件檢視器實測證據

在掛載 `local@liquid-glass-tray-discovery`（`explorer.exe`）後點擊工作列隱藏圖示箭頭 `^` 展開溢位小方塊時，Windows 檔案總管會立刻發生存取違規（`0xc0000005`）閃退並自動重啟：

| 時間 | 受影響程序 | 錯誤模組與偏移 | 例外代碼 | 關聯說明 |
| :--- | :--- | :--- | :--- | :--- |
| `16:05:12` | `Explorer.EXE` (`10.0.26100.9974`) | `Windows.UI.Xaml.dll` `+0x10b6f0` | `0xc0000005` | 第一次 `v0.1.0` 探針於展開 `^` 後閃退 |
| `16:05:20` | `StartMenuExperienceHost.exe` | `taskbar-on-top_1.3.1_321078.dll` `+0xd5cb` | `0xc0000005` | Explorer 重啟期間連動異常 |
| `16:13:15` | `Explorer.EXE` | `Windows.UI.Xaml.dll` `+0x10b6f0` | `0xc0000005` | `v0.2.0` 探針於點擊 `^` 時閃退 |
| `16:13:35` | `Explorer.EXE` | `Windows.UI.Xaml.dll` `+0x10b6f0` | `0xc0000005` | 再次點擊 `^` 時於同一位置閃退 |
| `16:15:50` | `Explorer.EXE` | `Windows.UI.Xaml.dll` `+0x10b6f0` | `0xc0000005` | 再次點擊 `^` 時於同一位置閃退 |
| `16:15:55` | `StartMenuExperienceHost.exe` | `taskbar-on-top_1.3.1_321078.dll` `+0xd5cb` | `0xc0000005` | Explorer 重啟期間連動異常 |

同時已驗證本機 **`LiquidGlassIconCenter.exe` 本身並未當機**（設定與關閉還原開關可正常寫入並執行）。

---

## 2. 探針成功與失敗的分界點

探針最後一輪在系統環境 `SystemTray.dll` 版本 **`2609.11000.100.0`** 內實際記錄到：

1. **已確認正常運作的部分**：
   - `LoadLibraryExW` 延遲載入攔截與 `HookSystemTray` 符號對位均成功。
   - `HookTaskbarDll=1` 取得工作列 `XamlRoot` 成功，並成功找到 `SystemTray.NotificationAreaIcons`（主列寬度顯示為 `0.0x48.0`，代表目前設定將托盤圖示全數摺疊於 `^` 溢位方塊內）。
2. **實際引爆點（造成 `Windows.UI.Xaml.dll+0x10b6f0` `0xc0000005` 的原因）**：
   - 探針在兩個地方假設了舊版 `SystemTray.dll` 記憶體配置或執行時機：
     1. **`OverflowXamlIslandManager_InitializeIfNeeded_Hook` 硬編碼成員偏移 `[5]`**：
        `reinterpret_cast<IUnknown**>(pThis)[5]->QueryInterface(...)` 未先做 `IsReadableMemory` 與物件有效指標檢查；在 Windows 11 26100/26300（`SystemTray.dll 2609.11000.100.0`）裡該偏移量或其生命週期狀態可能已改變或尚未就緒。
     2. **`IconView_IconView_Hook` 於建構子階段直接取用 `pThis[1]` 並掛載 `Loaded`**：
        在 `IconView::IconView(void)` 剛返回時，衍生類別 `SystemTray::NotifyIconView` 的 WinRT 控制項模板與 XAML Island `VisualTreeHelper` 尚未完全就緒；直接讀指標 `[1]` 或於後續 `Loaded` 內觸發 `RenderTargetBitmap::RenderAsync`／未防護的視覺樹遞迴，於 XAML Island 上引發 `Windows.UI.Xaml.dll` 空指標／無效介面解參考。

---

## 3. 未來接手修復的安全路線（供後續開發參考）

1. **禁止硬編碼 `pThis[1]` / `pThis[5]`**：
   - 改從 `taskbar.dll` / `XamlRoot` 或安全的 `OverflowRootGrid` 公開屬性／子樹搜尋著手，且任何原始指標解參考一律先經 `IsReadableMemory()` 驗證再 `QueryInterface`。
2. **避開建構子 `IconView::IconView` 與同步 `RenderTargetBitmap`**：
   - 先用 `NotifyIconView::ApplyLayoutMode`（物件已完成建構且樣板通常已存在）或以 `OverflowXamlIslandManager` 穩定開啟後的 `Dispatcher::TryRunAsync(Low)` 非同步走訪一次 `WrapGrid` 子元素，確認 `SystemTray.ImageIconContent` 底下 `Image.Source()` 是 `SoftwareBitmapSource` 還是 `WriteableBitmap`/`HICON` 投影後再設計渲染鉤子。
3. **評估畫質取捨**：
   - 溢位區托盤圖示原始輸入多為 `16×16` ～ `32×32` 動態 `HICON`（且底框目前為深灰不透明），正式實作前需留意低解析度圖示折光霧化問題與動態更新頻率限制。
