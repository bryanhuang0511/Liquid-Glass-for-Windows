# Changelog

All notable changes to **Liquid Glass for Windows** will be documented in this file.

## [v2.0.0] - 2026-11-18
### Added
- **Modular Core Package (`liquid_glass/`)**: Refactored the legacy single-file application (`2,421` lines) into single-responsibility modules (`specs.py`, `runtime.py`, `renderer.py`, `windhawk_bridge.py`, `shortcut_manager.py`, `desktop_watcher.py`, `tray_controller.py`, `web_api.py`) plus clean entrypoint [`main.py`](main.py).
- **Optional Windhawk Extension Mode**: Standalone desktop optical Liquid Glass works out of the box with zero external dependencies; `taskbar` and `startmenu` real-time engines automatically activate when Windhawk (Portable INI or Standard `Portable=0` HKLM Registry) is detected.
- **Lifecycle Controls**: Added UI switches (`exit_when_window_closes`, `restore_live_on_exit`) and system tray action (`退出並還原即時效果`) to automatically disable live Windhawk mods on exit without altering desktop shortcuts.
- **Portable Release Packaging**: Added deterministic PyInstaller spec (`LiquidGlassForWindows.spec`), isolated staging `.zip` builder (`build.ps1 -PackageZip`), and automated smoke verification (`verify_build.ps1`).
- **Crash Investigation & Cleanup**: Investigated Explorer crash under temporary System Tray overflow probe and archived technical root-cause report at [`docs/CRASH_REPORT_SystemTray_Overflow.md`](docs/CRASH_REPORT_SystemTray_Overflow.md).

### Changed
- **Renamed Application Identity**: Standardized executable and public repository naming to `LiquidGlassForWindows.exe` and `bryanhuang0511/Liquid-Glass-for-Windows`.
- **Performance Optimization**: Replaced `tasklist.exe` subprocess polling with Win32 `CreateToolhelp32Snapshot`, tightened Windhawk bitmap LRU cache limit (`12 MiB`) with stale-generation purging, throttled telemetry flushes (`1000ms`), removed redundant `OnPointerMoved` hook, and added visibility-aware `4000ms` UI status polling.
- **Test Suite Modularization**: Split monolithic test suite into domain test modules under [`tests/`](tests/).

## [v1.3.1] - 2026-11-13
### Added
- Merged user and public desktop shortcut enumeration with `needs_admin` reporting and clean `--apply-admin` / `--restore-admin` flows.
- Added lossless `.url` shortcut rewriting preserving original encoding, BOM, line endings, and UTF-7 `[InternetShortcut.W]` support.
- Added stale backup refresh on app upgrades, MSIX / AUMID Shell Item fallback icon extraction, and debounced desktop folder watcher.
