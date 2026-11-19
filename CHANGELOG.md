# Changelog

All notable changes to **Liquid Glass for Windows** will be documented in this file.

## [v2.2.2] - 2026-11-19
### Fixed
- **Start Menu Inner Folder & Modal Icon Transformation (`windhawk/liquid_glass_startmenu.wh.cpp`)**: Fixed icons inside expanded Start Menu folders (`StartMenu.FolderModal#StartFolderModal`, `StartMenu.ExpandedFolderList`, All Apps subfolders such as `Chrome 應用程式`) remaining untransformed. Now walks up to the top XAML `RootVisual` (covering `PopupRoot`), traverses `Popup.Child()` and `GetOpenPopupsForXamlRoot`, transforms icon-sized (`16..256px`) `SoftwareBitmapSource` objects inline inside `SetBitmapAsync_Hook` with automatic UI-element disallow/restore guards (`UserTile`, `AccountControl`, `PersonPicture`, `SearchBox`), resets `captureAttemptedGen` when capturing mid-animation frames, and runs trailing/visible-window scans.
- **Standard Windhawk (`Portable=0`) Permission Detection**: Compiles temporary `.dll` output in `%TEMP%` before atomically copying to `Engine\Mods\64`, ensuring non-elevated runs on standard `C:\Program Files\Windhawk` installations accurately report `needs_admin` instead of a generic compiler error.

### Added
- **Bundled Precompiled Windhawk Mods & Runtime Shims (`windhawk/prebuilt/`)**: Automatically stages precompiled 64-bit `.dll` binaries (`taskbar`, `startmenu`, `tray`) and MinGW runtime dependencies (`libc++.whl`, `libunwind.whl`, `windhawk-mod-shim.dll`) during `build.ps1`, enabling zero-compiler fallback on Windhawk 2.0 default installations where local `clang++.exe` is not installed.
- **Guided Windhawk Detection, Manual Path Binding & One-Click UAC Apply Flow**: Added running `windhawk.exe` path discovery via Win32 `QueryFullProcessImageNameW`, multi-drive/Scoop candidate scanning, persistent custom path binding (`windhawk_dir` in `config.json`, `set_windhawk_dir`, `browse_windhawk_dir`), and a 3-step onboarding card + inline one-click UAC elevation retry button (`#btnAdminRetry`) for Standard Windhawk installations.
- **Default Notification Area Whitelist Expansion**: Added `memreduct.exe;Mem Reduct` to `DEFAULT_TRAY_WHITELIST` (alongside `HWiNFO64.exe;HWiNFO32.exe;HWiNFO`) and added a `+ Mem Reduct` quick-add button in the UI (`91` total automated tests).

## [v2.2.1] - 2026-11-19
### Fixed
- **Windhawk Mod Metadata Compatibility (`// @id`)**: Removed the redundant `local@` prefix from `// @id` inside `windhawk/liquid_glass_taskbar.wh.cpp`, `windhawk/liquid_glass_startmenu.wh.cpp`, and `windhawk/liquid_glass_tray.wh.cpp` while keeping `local@` storage filenames (`ModsSource/local@*.wh.cpp`, `Engine/Mods/local@*.ini`, and `local@*.dll`). Resolves Windhawk UI's `"Mod id must only contain the following characters: 0-9, a-z, and a hyphen (-)"` error toast and restores Name/Description/Author/Version display inside Windhawk's Installed Mods list.

### Added
- **Automated Windhawk Metadata Regression Test**: Added `test_windhawk_source_metadata_matches_windhawk_validator` in `tests/test_windhawk_bridge.py` (`90` total test cases) to enforce Windhawk's `validateMetadata` regex and local mod slug consistency across all three `.wh.cpp` sources.

## [v2.2.0] - 2026-11-18
### Changed
- **Unified Four-Scope One-Click Apply + Independent Per-Scope Controls**: Promoted System Tray (`tray`) out of experimental opt-in isolation (`experimental = false` in `liquid_glass/specs.py`). Global batch controls (`apply_all`, `restore_all`, CLI `--apply`, UI `#btnApplyAll`, and Tray quick actions) now apply all four areas together (`taskbar`, `startmenu`, `desktop`, `tray`) when Windhawk is detected while preserving individual per-tab apply/restore/tuning and the notification-area process whitelist.

## [v2.1.0] - 2026-11-18
### Added
- **Experimental 4th Scope — System Tray & Overflow (`tray`)**: Added an isolated 4th UI tab (`🔔 通知列`) and dedicated Windhawk mod (`windhawk/liquid_glass_tray.wh.cpp`, `local@liquid-glass-tray-icons`) using `GetTaskbarXamlRoot` cold-start sweeps (`Shell_TrayWnd` / `Shell_SecondaryTrayWnd` via `RunFromWindowThread` + `IsReadableMemory` `VirtualQuery` guards), `NotificationAreaOverflow::OnApplyTemplate`/`ArrangeOverride`, and guarded `OverflowXamlIslandManager::Show` sweeps (`CoreDispatcherPriority::Low`) with zero constructor collision against spacing/on-top mods and `12 MiB` LRU cache.
- **Configurable Notification Area Whitelist (`WhitelistApps` / `tray_whitelist`)**: Added UI whitelist editor card (`#trayWhitelistCard`) in Tab 4 (`🔔 通知列`) with default protection for `HWiNFO64.exe;HWiNFO32.exe;HWiNFO`. Combines live COM `INotificationAreaIcon6::AppFriendlyName` resolution (`IIconView` -> `IIconConfiguration` -> `INotificationAreaIconsDataModel`) with `HKCU\Control Panel\NotifyIconSettings` executable/tooltip indexing and XAML automation/tooltip checks so rapidly updating sensor icons keep original colors with zero per-second re-render overhead.
- **Opt-In Isolation & Safe Batch Cleanup**: Global batch "Apply Core 3" (`apply_all` / CLI `--apply`) keeps `tray` strictly opt-in from its dedicated tab, while `restore_all`, CLI `--restore`, and `restore_live_on_exit` cleanly restore it when enabled.

### Fixed
- **High-Resolution Electron / PNG Tray Icons (<= 1024px, e.g., Antigravity `512x512`)**: Raised input bitmap limit in synchronous `WriteableBitmap` Fast Path 1 to `1024px` with `PremultipliedResizeFloat` downsampling (`<= 256px`), and updated `RenderTargetBitmap` fallback to `CoreDispatcherPriority::Normal` (`RenderAsync(img, 128, 0)`).
- **Windhawk-Style Minimize-to-Tray Lifecycle**: Fixed elevated / quick-relaunch sessions failing to spawn the background tray (`--autostart`), resolved cross-integrity-level per-user SDDL mutex/stop-event visibility, verified `PostMessageW` before assuming cross-UIPI window focus succeeded, added spawn readiness synchronization on window close, logged unexpected UI lifecycle errors to `%TEMP%\lgfw_app.log`, and exited closed WebView2 processes cleanly via `os._exit(0)` so closing the UI window cleanly removes the taskbar button and preserves live effects until exiting from the notification area tray menu.

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
