# 🧊 Liquid Glass for Windows

> **Windows 11 Real-Time Optical Liquid Glass Icon Hub & Modular Engine (`v2.2.1`)**  
> Standalone Zero-Dependency Desktop Icon Transformation ・ Optional Windhawk Real-Time Taskbar, Start Menu & System Tray (`^`) Engines ・ Unified 4-Scope Apply-All + Per-Scope Controls ・ Minimize-to-Tray Lifecycle ・ Byte-Exact Shortcut Backup & One-Click Restore

[📘 正體中文完整架構與操作指引 (指引.md)](指引.md) ｜ [📜 更新紀錄 (CHANGELOG.md)](CHANGELOG.md)

---

## ✨ Highlights (`v2.2.1`)

- **Independent Desktop Core (Zero External Dependencies)**:  
  Generates multi-resolution (`256px` down to `16px`) frosted translucent Liquid Glass `.ico` icons for user & public Desktop shortcuts (`.lnk`, `.url`, and MSIX/AUMID Store apps) with automatic byte-exact backups and stale-backup refresh on app upgrades.
- **Optional Windhawk Real-Time Extensions (Taskbar, Start Menu & System Tray `^`)**:  
  Automatically detects **Windhawk** (Portable `C:\Windhawk` INI mode or Standard `Portable=0` HKLM Registry installation). `taskbar`, `startmenu`, and `tray` (系統匣與 `^` 溢位區) render icons directly in GPU/WIC memory (`<= 12 MiB` LRU cache, `0` shortcut files mutated on disk), with `GetTaskbarXamlRoot` cold-start sweeps, `1024px` high-res Electron/PNG icon downsampling (`<= 256px`), and a configurable process exclusion whitelist (`HWiNFO64.exe;HWiNFO32.exe;HWiNFO` by default) to preserve colored telemetry sensors.
- **Unified Four-Scope Apply & Per-Tab Independence**:  
  One-click batch controls (`一鍵套用全部四區` / `一鍵還原全部區域`) manage all four scopes seamlessly together while keeping per-tab parameters, enable/restore switches, and the tray whitelist independently configurable.
- **Optical Liquid Glass Shader Parity**:  
  Unified float32/C++ optical model featuring zero opaque black/gray RGB, frosted ice-white highlights capped safely below solid white (`<= 0.90` alpha), directional top-left Fresnel bevel rims, and color-seam glint detection.
- **Windhawk-Style Minimize-to-Tray & Exit Safety Controls**:  
  Closing the Control Center window hides to the system tray notification area cleanly across both standard and elevated sessions (zero lingering taskbar button, live effects preserved), with quick tray actions to reopen Settings or restore & exit (`退出並還原即時效果` / `restore_live_on_exit`).
- **Background Desktop Watcher**:  
  Win32 `ReadDirectoryChangesW` folder monitor with debounce, max-wait ceiling, and self-write loop suppression (`5s`) to skin newly installed desktop apps automatically.

---

## 📂 Module Architecture

| Path | Role | Description |
| :--- | :--- | :--- |
| [`main.py`](main.py) | CLI & App Entrypoint | Minimal dispatcher for UI (`--settings`), Tray (`--autostart`), Headless CLI (`--apply` / `--restore`), and Admin retry (`--apply-admin` / `--restore-admin`). |
| [`liquid_glass/`](liquid_glass/README.md) | Core Python Package | Modular engine split cleanly into `specs.py`, `runtime.py`, `renderer.py`, `windhawk_bridge.py`, `shortcut_manager.py`, `desktop_watcher.py`, `tray_controller.py`, and `web_api.py`. |
| [`ui/`](ui/README.md) | Control Center UI | Modern PyWebView EdgeChromium dark-glass dashboard with live multi-DPI sample previews, engine telemetry polling, and lifecycle switches. |
| [`windhawk/`](windhawk/README.md) | Optional C++ Mods | Windhawk mod sources (`liquid_glass_taskbar.wh.cpp`, `liquid_glass_startmenu.wh.cpp`, `liquid_glass_tray.wh.cpp`) auto-compiled via local `clang++.exe`. |
| [`assets/`](assets/) | Application & Start Assets | App icons (`app.ico`, `app.png`) and optional Windows Start button overlay asset (`start_button/windows11_start_liquid_glass.png`). |
| [`tests/`](tests/) | Automated Verification | Domain-split `pytest` suite (`90` test cases covering optical math, specs, `.url` round-trip, watcher, Windhawk Registry/INI/metadata, `--check`, and exit lifecycle). |
| [`docs/`](docs/ARCHITECTURE.md) | Engineering Docs | System architecture notes, technical root-cause analyses ([`CRASH_REPORT_SystemTray_Overflow.md`](docs/CRASH_REPORT_SystemTray_Overflow.md)), and next-session roadmap ([`NEXT_SESSION_HANDOFF.md`](docs/NEXT_SESSION_HANDOFF.md)). |

---

## 🚀 Quick Start (Portable ZIP Release)

1. Download **`LiquidGlassForWindows_v2.2.1_Portable.zip`** from [GitHub Releases](https://github.com/bryanhuang0511/Liquid-Glass-for-Windows/releases).
2. Extract to a permanent folder (e.g., `D:\Tools\Liquid_Glass_for_Windows`) so Windows shortcut icon paths remain valid.
3. Double-click **`LiquidGlassForWindows.exe`** (or **`啟動圖示中樞.bat`**).
4. **Optional Taskbar & Start Menu Real-Time Mode**: Install [Windhawk](https://windhawk.net/), click **「重新偵測 Windhawk」** in the UI, and apply. For best visual contrast, enable Windows Dark Mode or pair with a transparent Windhawk taskbar/start-menu styler.

> [!NOTE]
> First launch operates cleanly with zero registry pollution and creates local runtime data (`config.json`, `IconVault/`, `Original_Shortcuts_Backup/`) exclusively inside your extracted folder. Always use **「還原原生圖示」** inside the app before deleting the folder.

---

## 🛠️ Development & Packaging

```powershell
# 1. Install dependencies (Python 3.12+ recommended, Windows 11 x64)
pip install -r requirements.txt pytest

# 2. Run modular unit & optical test suite
python -m pytest tests -q

# 3. Run UI control center from source
python main.py

# 4. Build standalone onedir EXE & clean Portable ZIP (releases/)
.\build.ps1 -PackageZip
.\verify_build.ps1
```

---

## 📄 License

Released under the [MIT License](LICENSE).
