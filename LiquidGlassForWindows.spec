# -*- mode: python ; coding: utf-8 -*-
# PyInstaller --onedir spec for Liquid Glass for Windows
from PyInstaller.utils.hooks import collect_all

datas = [
    ("ui", "ui"),
    ("windhawk", "windhawk"),
    ("assets/app.ico", "assets"),
    ("assets/app.png", "assets"),
    ("assets/start_button", "assets/start_button"),
]
binaries = []
hiddenimports = [
    "liquid_glass",
    "liquid_glass.specs",
    "liquid_glass.runtime",
    "liquid_glass.renderer",
    "liquid_glass.windhawk_bridge",
    "liquid_glass.shortcut_manager",
    "liquid_glass.desktop_watcher",
    "liquid_glass.tray_controller",
    "liquid_glass.web_api",
    "pystray",
    "PIL",
    "numpy",
    "pythoncom",
    "win32com.client",
    "win32com.shell",
    "win32com.propsys",
    "win32com.propsys.pscon",
]

for pkg in ("webview", "clr_loader", "pythonnet"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:
        print(f"[spec] WARNING collect_all({pkg}) failed: {exc}")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["psutil", "tkinter", "matplotlib", "scipy"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LiquidGlassForWindows",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["assets\\app.ico"],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LiquidGlassForWindows",
)
