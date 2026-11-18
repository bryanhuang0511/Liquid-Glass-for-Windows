# `liquid_glass/` — Core Modular Package

Single-responsibility Python package powering **Liquid Glass for Windows**. Designed as an acyclic dependency DAG so background tray startup (`--autostart`) avoids loading heavy modules (`numpy`, `PIL`, `webview`) until actually required.

## Module Dependency Hierarchy

1. [`specs.py`](specs.py) — Pure-Python optical specs, source/extension contracts, lifecycle defaults, and config migration (zero heavy imports).
2. [`runtime.py`](runtime.py) — OS path resolution, Win32 ctypes bindings, DPI/WebView2 setup, admin elevation helpers, Registry autostart, and self-shortcut management.
3. [`renderer.py`](renderer.py) — Float32 NumPy/PIL optical Liquid Glass shader, solid-backplate detection, multi-size ICO layering (`256px`–`16px`), and live preview rendering (lazy-imported).
4. [`windhawk_bridge.py`](windhawk_bridge.py) — Auto-discovery across Portable & Standard Windhawk installs, `clang++.exe` compilation, INI/Registry config sync, Win32 Toolhelp32 process checks, and stale DLL cleanup.
5. [`shortcut_manager.py`](shortcut_manager.py) — `.lnk` COM & lossless `.url` text read/write, HICON/HBITMAP/AUMID 256px extraction, backup/stale-refresh logic, and batch orchestration.
6. [`desktop_watcher.py`](desktop_watcher.py) — `ReadDirectoryChangesW` folder watcher with debouncing, max-wait ceiling, and self-write loop suppression.
7. [`tray_controller.py`](tray_controller.py) — Win32 single-instance Tray Mutex & Local Stop Event handling, `LiquidGlassApp` lifecycle coordinator, and exit auto-restore behavior.
8. [`web_api.py`](web_api.py) — Public PyWebView JS-Python bridge (`LiquidGlassApi`) with preview sequence debouncing and telemetry polling endpoints.
