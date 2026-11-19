# `windhawk/` — Optional Real-Time Windhawk C++ Mods

Contains optional native Windhawk mod sources compiled locally via Windhawk's bundled `clang++.exe`:

| Mod File | Mod ID | Target Process | Function |
| :--- | :--- | :--- | :--- |
| [`liquid_glass_taskbar.wh.cpp`](liquid_glass_taskbar.wh.cpp) | `local@liquid-glass-taskbar-icons` | `explorer.exe` | Hooks taskbar app button icon controls and applies real-time WIC/float32 Liquid Glass (`12 MiB` LRU cache, stale-generation purge, `1000ms` throttled telemetry). |
| [`liquid_glass_startmenu.wh.cpp`](liquid_glass_startmenu.wh.cpp) | `local@liquid-glass-startmenu-icons` | `StartMenuExperienceHost.exe` | Applies matching optical Liquid Glass across Start Menu Pinned, All Apps, Category, and expanded inner folder modal (`StartFolderModal` / `ExpandedFolderList` / `PopupRoot`) icons without altering `.lnk` files on disk (`12 MiB` LRU cache, `1000ms` throttled telemetry). |
| [`liquid_glass_tray.wh.cpp`](liquid_glass_tray.wh.cpp) | `local@liquid-glass-tray-icons` | `explorer.exe` | **System Tray & Overflow (`tray`)**: Combines `GetTaskbarXamlRoot` cold-start sweeps, `OverflowXamlIslandManager::Show` / `NotificationAreaOverflow` hooks, `1024px` `WriteableBitmap` downsampling (`<= 256px`), and `WhitelistApps` process exclusions (`INotificationAreaIcon6::AppFriendlyName` + `NotifyIconSettings`, default HWiNFO & Mem Reduct protection) with `12 MiB` LRU cache. |

> [!NOTE]
> As of `v3.0.0`, `windhawk_bridge.py` supports both UTF-16 LE BOM (`Portable=0` standard installs) and UTF-8 `windhawk.ini` files, stages precompiled 64-bit `.dll` + runtime shims under `windhawk/prebuilt/`, and keeps `SystemTray.dll` symbol hooks optional with `Taskbar.View` / `GetTaskbarXamlRoot` fallbacks. Historical crash investigation notes are preserved at [`docs/CRASH_REPORT_SystemTray_Overflow.md`](../docs/CRASH_REPORT_SystemTray_Overflow.md).
