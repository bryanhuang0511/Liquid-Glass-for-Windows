# 🧊 Liquid Glass for Windows

> **Windows 11 Real-Time Optical Liquid Glass Icon Hub & Modular Engine (`v2.0.0`)**  
> Standalone Zero-Dependency Desktop Icon Transformation ・ Optional Windhawk Real-Time Taskbar & Start Menu Engines ・ Byte-Exact Shortcut Backup & One-Click Restore ・ Lossless `.url` & MSIX/AUMID Support ・ Portable ZIP Distribution

[📘 正體中文完整架構與操作指引 (指引.md)](指引.md) ｜ [📜 更新紀錄 (CHANGELOG.md)](CHANGELOG.md)

---

## ✨ Highlights (`v2.0.0`)

- **Independent Desktop Core (Zero External Dependencies)**:  
  Generates multi-resolution (`256px` down to `16px`) frosted translucent Liquid Glass `.ico` icons for user & public Desktop shortcuts (`.lnk`, `.url`, and MSIX/AUMID Store apps) with automatic byte-exact backups and stale-backup refresh on app upgrades.
- **Optional Windhawk Real-Time Extensions (Taskbar & Start Menu)**:  
  Automatically detects **Windhawk** (Portable `C:\Windhawk` INI mode or Standard `Portable=0` HKLM Registry installation). Renders taskbar and Start Menu icons directly in GPU/WIC memory (`<= 12 MiB` LRU cache, `0` shortcut files mutated on disk, instant toggle/restore). If Windhawk is not installed, the Desktop module continues working standalone with friendly guidance.
- **Optical Liquid Glass Shader Parity**:  
  Unified float32/C++ optical model featuring zero opaque black/gray RGB, frosted ice-white highlights capped safely below solid white (`<= 0.90` alpha), directional top-left Fresnel bevel rims, and color-seam glint detection.
- **Exit Lifecycle Safety Controls**:  
  Built-in UI switches for **Close Window to Exit** and **Auto-Restore Live Engines on Exit** (plus System Tray `退出並還原即時效果`) so Taskbar/Start Menu appearance immediately reverts on exit without touching Desktop `.lnk` backups.
- **Background Desktop Watcher**:  
  Win32 `ReadDirectoryChangesW` folder monitor with debounce, max-wait ceiling, and self-write loop suppression (`5s`) to skin newly installed desktop apps automatically.

---

## 📂 Module Architecture

| Path | Role | Description |
| :--- | :--- | :--- |
| [`main.py`](main.py) | CLI & App Entrypoint | Minimal dispatcher for UI (`--settings`), Tray (`--autostart`), Headless CLI (`--apply` / `--restore`), and Admin retry (`--apply-admin` / `--restore-admin`). |
| [`liquid_glass/`](liquid_glass/README.md) | Core Python Package | Modular engine split cleanly into `specs.py`, `runtime.py`, `renderer.py`, `windhawk_bridge.py`, `shortcut_manager.py`, `desktop_watcher.py`, `tray_controller.py`, and `web_api.py`. |
| [`ui/`](ui/README.md) | Control Center UI | Modern PyWebView EdgeChromium dark-glass dashboard with live multi-DPI sample previews, engine telemetry polling, and lifecycle switches. |
| [`windhawk/`](windhawk/README.md) | Optional C++ Mods | Windhawk mod sources (`liquid_glass_taskbar.wh.cpp`, `liquid_glass_startmenu.wh.cpp`) auto-compiled via local `clang++.exe`. |
| [`assets/`](assets/) | Application & Start Assets | App icons (`app.ico`, `app.png`) and optional Windows Start button overlay asset (`start_button/windows11_start_liquid_glass.png`). |
| [`tests/`](tests/) | Automated Verification | Domain-split `pytest` suite (`88` test cases covering optical math, specs, `.url` round-trip, watcher, Windhawk Registry/INI, `--check`, and exit lifecycle). |
| [`docs/`](docs/ARCHITECTURE.md) | Engineering Docs | System architecture notes and technical root-cause analyses (including [`CRASH_REPORT_SystemTray_Overflow.md`](docs/CRASH_REPORT_SystemTray_Overflow.md)). |

---

## 🚀 Quick Start (Portable ZIP Release)

1. Download **`LiquidGlassForWindows_v2.0.0_Portable.zip`** from [GitHub Releases](https://github.com/bryanhuang0511/Liquid-Glass-for-Windows/releases).
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
