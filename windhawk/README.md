# `windhawk/` — Optional Real-Time Windhawk C++ Mods

Contains optional native Windhawk mod sources compiled locally via Windhawk's bundled `clang++.exe`:

| Mod File | Mod ID | Target Process | Function |
| :--- | :--- | :--- | :--- |
| [`liquid_glass_taskbar.wh.cpp`](liquid_glass_taskbar.wh.cpp) | `local@liquid-glass-taskbar-icons` | `explorer.exe` | Hooks taskbar app button icon controls and applies real-time WIC/float32 Liquid Glass (`12 MiB` LRU cache, stale-generation purge, `1000ms` throttled telemetry). |
| [`liquid_glass_startmenu.wh.cpp`](liquid_glass_startmenu.wh.cpp) | `local@liquid-glass-startmenu-icons` | `StartMenuExperienceHost.exe` | Applies matching optical Liquid Glass across Start Menu Pinned, All Apps, and Category icons without altering `.lnk` files on disk (`12 MiB` LRU cache, `1000ms` throttled telemetry). |

> [!NOTE]
> The Windows **System Tray overflow flyout (`^`)** is intentionally excluded due to Explorer memory instability documented in [`docs/CRASH_REPORT_SystemTray_Overflow.md`](../docs/CRASH_REPORT_SystemTray_Overflow.md).
