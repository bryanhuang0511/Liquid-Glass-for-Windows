# `windhawk/` — Optional Real-Time Windhawk C++ Mods

Contains optional native Windhawk mod sources compiled locally via Windhawk's bundled `clang++.exe`:

| Mod File | Mod ID | Target Process | Function |
| :--- | :--- | :--- | :--- |
| [`liquid_glass_taskbar.wh.cpp`](liquid_glass_taskbar.wh.cpp) | `local@liquid-glass-taskbar-icons` | `explorer.exe` | Hooks taskbar app button icon controls and applies real-time WIC/float32 Liquid Glass (`12 MiB` LRU cache, stale-generation purge, `1000ms` throttled telemetry). |
| [`liquid_glass_startmenu.wh.cpp`](liquid_glass_startmenu.wh.cpp) | `local@liquid-glass-startmenu-icons` | `StartMenuExperienceHost.exe` | Applies matching optical Liquid Glass across Start Menu Pinned, All Apps, and Category icons without altering `.lnk` files on disk (`12 MiB` LRU cache, `1000ms` throttled telemetry). |
| [`liquid_glass_tray.wh.cpp`](liquid_glass_tray.wh.cpp) | `local@liquid-glass-tray-icons` | `explorer.exe` | **Experimental 4th Scope (`tray`)**: Hooks `SystemTray.dll` `ImageIconContent::MeasureOverride` & `put_IconImageSource` with zero spacing/on-top constructor collisions (`WriteableBitmap` sync read + `IsLoaded()`-gated `RenderTargetBitmap`, `12 MiB` LRU cache). |

> [!NOTE]
> The System Tray module is isolated on its own 4th opt-in UI tab (`🔔 通知列`) and excluded from global "Apply Core 3" batch actions; historical crash investigation notes are preserved at [`docs/CRASH_REPORT_SystemTray_Overflow.md`](../docs/CRASH_REPORT_SystemTray_Overflow.md).
