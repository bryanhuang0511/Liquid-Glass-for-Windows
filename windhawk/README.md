# `windhawk/` — Optional Real-Time Windhawk C++ Mods

Contains optional native Windhawk mod sources compiled locally via Windhawk's bundled `clang++.exe`:

| Mod File | Mod ID | Target Process | Function |
| :--- | :--- | :--- | :--- |
| [`liquid_glass_taskbar.wh.cpp`](liquid_glass_taskbar.wh.cpp) | `local@liquid-glass-taskbar-icons` | `explorer.exe` | Hooks taskbar app button icon controls and applies real-time WIC/float32 Liquid Glass (`12 MiB` LRU cache, stale-generation purge, `1000ms` throttled telemetry). |
| [`liquid_glass_startmenu.wh.cpp`](liquid_glass_startmenu.wh.cpp) | `local@liquid-glass-startmenu-icons` | `StartMenuExperienceHost.exe` | Applies matching optical Liquid Glass across Start Menu Pinned, All Apps, and Category icons without altering `.lnk` files on disk (`12 MiB` LRU cache, `1000ms` throttled telemetry). |
| [`liquid_glass_tray.wh.cpp`](liquid_glass_tray.wh.cpp) | `local@liquid-glass-tray-icons` | `explorer.exe` | **System Tray & Overflow (`tray`)**: Combines `GetTaskbarXamlRoot` cold-start sweeps, `OverflowXamlIslandManager::Show` / `NotificationAreaOverflow` hooks, `1024px` `WriteableBitmap` downsampling (`<= 256px`), and `WhitelistApps` process exclusions (`INotificationAreaIcon6::AppFriendlyName` + `NotifyIconSettings`, default HWiNFO protection) with `12 MiB` LRU cache. |

> [!NOTE]
> As of `v2.2.0`, the System Tray module participates in global "Apply All 4 Scopes" (`apply_all`) while preserving independent per-tab controls and whitelist editing; `v2.2.1` aligns `// @id` metadata inside all three `.wh.cpp` headers with Windhawk's `^[0-9a-z-]+$` rule while keeping `local@` storage filenames. Historical crash investigation notes are preserved at [`docs/CRASH_REPORT_SystemTray_Overflow.md`](../docs/CRASH_REPORT_SystemTray_Overflow.md).
