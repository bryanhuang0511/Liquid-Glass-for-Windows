# Liquid Glass for Windows

[English](README.md) | [中文 (繁體)](README_zh.md)

Windows 11 icon transformation utility and modular rendering engine (`v3.0.0`). Generates multi-resolution frosted glass `.ico` files (`256px` down to `16px`) for desktop shortcuts independently, with optional Windhawk real-time extensions for the Taskbar, Start Menu (including inner folder modals), and System Tray.

---

## File List and Component Roles

| Path | Role | Core Responsibility |
|---|---|---|
| [`main.py`](main.py) | CLI and App Entrypoint | Dispatches UI (`--settings`), Tray (`--autostart`), headless CLI (`--apply` / `--restore`), and elevated retries (`--apply-admin` / `--restore-admin`) |
| [`liquid_glass/`](liquid_glass/README.md) | Core Python Package | Contains `specs.py`, `runtime.py`, `renderer.py`, `windhawk_bridge.py`, `shortcut_manager.py`, `desktop_watcher.py`, `tray_controller.py`, and `web_api.py` |
| [`ui/`](ui/README.md) | Control Center UI | PyWebView EdgeChromium interface for multi-DPI previews, engine telemetry, and per-tab configuration |
| [`windhawk/`](windhawk/README.md) | Optional C++ Mods | Windhawk mod sources (`liquid_glass_taskbar.wh.cpp`, `liquid_glass_startmenu.wh.cpp`, `liquid_glass_tray.wh.cpp`) with bundled 64-bit `.dll` / `.whl` fallback |
| [`assets/`](assets/) | Application Assets | App icons (`app.ico`, `app.png`) and optional Windows Start button overlay (`start_button/windows11_start_liquid_glass.png`) |
| [`tests/`](tests/) | Test Suite | `pytest` coverage (`93` tests) for optical math, `.url` round-trip, watcher, Windhawk Registry/UTF-16 INI/auto-setup, `--check`, and exit lifecycle |
| [`docs/`](docs/ARCHITECTURE.md) | Engineering Docs | Architecture notes, [`CRASH_REPORT_SystemTray_Overflow.md`](docs/CRASH_REPORT_SystemTray_Overflow.md), and [`NEXT_SESSION_HANDOFF.md`](docs/NEXT_SESSION_HANDOFF.md) |
| [`指引.md`](指引.md) / [`CHANGELOG.md`](CHANGELOG.md) | Detailed Manual & Changelog | Extended Chinese architecture manual and version history |

---

## Core Architecture and Capabilities

* **Standalone Desktop Core**: Transforms user and public desktop shortcuts (`.lnk`, `.url`, and MSIX/AUMID Store apps) with byte-exact backups in `Original_Shortcuts_Backup/` and automatic stale-backup refresh across upgrades.
* **Optional Windhawk Real-Time Mode**: Detects Windhawk across standard installations (`C:\Program Files\Windhawk`), portable installs, running `windhawk.exe` processes, or user-selected paths (`windhawk_dir`). Renders icons in GPU/WIC memory (`<= 12 MiB` LRU cache, `0` shortcut files modified on disk), including Start Menu folder modals (`StartFolderModal` / `PopupRoot`), `1024px` tray icons, and a process exclusion list (`HWiNFO64.exe;HWiNFO32.exe;HWiNFO;memreduct.exe;Mem Reduct` by default).
* **Four-Scope Management**: Supports unified batch apply/restore alongside independent per-tab parameters and enable/restore toggles.
* **Optical Shader Parity**: Float32 and C++ shader implementations align on translucent ice-white highlights (`<= 0.90` alpha), directional Fresnel bevels, and seam glint detection.
* **Background Desktop Watcher**: Uses Win32 `ReadDirectoryChangesW` with debouncing and `5s` self-write loop suppression to apply styles to newly added desktop shortcuts.

---

## Quick Start and Development

### Portable Release

1. Download **`LiquidGlassForWindows_v3.0.0_Portable.zip`** from [GitHub Releases](https://github.com/bryanhuang0511/Liquid-Glass-for-Windows/releases).
2. Extract to a permanent folder (for example, `Tools\Liquid_Glass_for_Windows`) so Windows shortcut icon references remain valid.
3. Run **`LiquidGlassForWindows.exe`** (or **`啟動圖示中樞.bat`**).
4. *(Optional)* Install [Windhawk](https://windhawk.net/), detect it in the UI, and apply real-time taskbar/start-menu/tray hooks.

Runtime data (`config.json`, `IconVault/`, `Original_Shortcuts_Backup/`) is stored inside the extracted folder. Run icon restoration inside the app before deleting the folder.

### Build and Test from Source

```powershell
# 1. Install dependencies (Python 3.12+ recommended, Windows 11 x64)
pip install -r requirements.txt pytest

# 2. Run unit and optical test suite
python -m pytest tests -q

# 3. Launch UI from source
python main.py

# 4. Package standalone onedir EXE and Portable ZIP (releases/)
.\build.ps1 -PackageZip
.\verify_build.ps1
```

---

## License

Released under the [MIT License](LICENSE).
