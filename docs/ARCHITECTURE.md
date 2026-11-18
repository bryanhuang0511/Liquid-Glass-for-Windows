# System Architecture

## Two-Process Design (`Fast Boot + Low Idle Footprint`)

```text
+-------------------------------------------------------------------+
|                        User / Windows OS                          |
+-----------------------+--------------------------+----------------+
                        |                          |
                        v (Desktop .lnk/.url/AUMID)| (Optional Live Render)
        +---------------+---------------+     +----+---------------------+
        | LiquidGlassForWindows.exe     |     | Windhawk Runtime         |
        | (--settings UI Process)       |     |  - explorer.exe (Taskbar)|
        |  * PyWebView EdgeChromium     |     |  - explorer.exe (Tray ^) |
        |  * WebApi (preview / config)  |     |  - StartMenuExperience   |
        |  * Warm preview worker        |     |  * Config via INI or     |
        +---------------+---------------+     |    HKLM Registry         |
                        |                     +----+---------------------+
                        | IPC: config.json + Same-User-SID Mutex/Stop Event
                        v
        +---------------+---------------+
        | LiquidGlassForWindows.exe     |
        | (--autostart Tray Process)    |
        |  * pystray + PIL ONLY         |
        |  * IconWatcher (ReadDirChgW)  |
        |  * Exits cleanly on Stop Event|
        +-------------------------------+
```

## Key Invariants

1. **Zero Stale Backup Loss**: Original `.lnk` / `.url` shortcuts are copied byte-for-byte into `Original_Shortcuts_Backup/` before first icon replacement. Restoration detects ownership via `IconVault/` path signatures regardless of folder relocation.
2. **Dual-Process Config Isolation**: Settings UI (`--settings`) and background Tray (`--autostart`) isolate `watcher` and `lifecycle` sections on `save_config()` so concurrent saves never clobber the other process's state.
3. **Zero Forced UAC**: Normal desktop shortcuts, Portable Windhawk INI, and live UI previews run under standard privileges. Admin elevation (`--apply-admin` / `relaunch_admin`) is only requested interactively for `PublicDesktop` or Standard `HKLM` Registry writes.
