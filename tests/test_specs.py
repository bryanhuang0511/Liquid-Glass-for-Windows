"""tests/test_specs.py -- 規格表、參數範圍、命名規則與設定檔遷移測試。"""
from __future__ import annotations

import pytest

from liquid_glass import specs as S


def test_config_migration_does_not_pollute_taskbar():
    assert S.VERSION == "2.2.2"
    assert S.SCOPES == ("desktop", "startmenu", "taskbar", "tray")
    assert S.SPECS["tray"]["actual_px"] == 20
    assert all(not S.SPECS[sc]["experimental"] for sc in S.SCOPES)
    cfg = S.migrate_config({"line_strength": 1.9, "glow_amount": 0.35, "film_opacity": 0.88})
    assert cfg["desktop"] == S.default_params("desktop") == {"opacity": 1.0, "frost": 1.0, "glow": 1.0, "dark_protect": 1.0}
    assert cfg["taskbar"] == S.default_params("taskbar") == {"opacity": 1.0, "frost": 1.0, "glow": 1.0, "dark_protect": 1.0}
    assert cfg["startmenu"] == S.default_params("startmenu") == {"opacity": 1.0, "frost": 1.0, "glow": 1.0, "dark_protect": 1.0}
    assert cfg["tray"] == S.default_params("tray") == {"opacity": 1.0, "frost": 1.0, "glow": 1.0, "dark_protect": 1.0}
    v13 = S.migrate_config(
        {
            "desktop": {"line": 1.9, "glow": 0.35, "film": 0.88, "ratio": 0.68},
            "startmenu": {"line": 1.8, "glow": 0.40, "film": 0.45, "emboss": 0.50, "ratio": 0.80},
            "taskbar": {"line": 1.5, "glow": 0.35, "film": 0.06, "ratio": 0.8},
        }
    )
    assert v13["desktop"] == S.default_params("desktop")
    assert v13["startmenu"] == S.default_params("startmenu")
    assert v13["taskbar"] == S.default_params("taskbar")


def test_clamp_rejects_garbage():
    p = S.clamp_params("taskbar", {"opacity": "abc", "frost": float("nan"), "glow": -5, "dark_protect": None, "x": 1})
    assert p == {"opacity": 1.0, "frost": 1.0, "glow": 0.3, "dark_protect": 1.0}
    assert S.migrate_config("not a dict") == S.default_config()


def test_specs_paths_and_tags():
    assert S.backup_parts("desktop", "user") == ()
    assert S.backup_parts("desktop", "common") == ("PublicDesktop",)
    assert S.backup_parts("taskbar", "user") == ("Taskbar",)
    assert S.backup_parts("tray", "user") == ("Tray",)
    assert S.backup_parts("startmenu", "common") == ("StartMenu", "common")
    assert S.vault_tag("desktop", "user", ".lnk") == ""
    assert S.vault_tag("desktop", "common", ".lnk") == "_pub"
    assert S.vault_tag("desktop", "user", ".URL") == "_url"
    assert S.vault_tag("desktop", "common", ".url") == "_pub_url"
    assert S.vault_tag("startmenu", "common", ".lnk") == ""
    with pytest.raises(ValueError):
        S.backup_parts("desktop", "nobody")


def test_specs_ext_and_result_fields_and_watcher_clamp():
    assert S.shortcut_ext("desktop", "A.URL") == ".url" and S.shortcut_ext("desktop", "a.LNK") == ".lnk"
    assert S.shortcut_ext("startmenu", "a.url") is None and S.shortcut_ext("taskbar", "a.url") is None and S.shortcut_ext("tray", "a.url") is None
    assert S.new_result("desktop", 3)["needs_admin"] == 0
    assert S.clamp_watcher(True) == {"enabled": True, "debounce_sec": 3.0}
    assert S.clamp_watcher({"enabled": 1, "debounce_sec": 999})["debounce_sec"] == 30.0
    assert S.clamp_watcher({"debounce_sec": 0.1})["debounce_sec"] == 1.0
    assert S.clamp_watcher({"debounce_sec": "x"}) == S.default_watcher()
    assert S.clamp_watcher("junk") == S.default_watcher()
    cfg = S.migrate_config({"desktop": {"line": 2.0}, "watcher": {"enabled": True, "debounce_sec": 7}})
    assert cfg["watcher"] == {"enabled": True, "debounce_sec": 7.0}
    assert S.migrate_config({})["watcher"] == S.default_watcher()
    assert S.default_config()["tray_whitelist"] == S.DEFAULT_TRAY_WHITELIST
    assert S.normalize_tray_whitelist(None) == S.DEFAULT_TRAY_WHITELIST
    assert S.normalize_tray_whitelist("") == ""
    assert S.normalize_tray_whitelist("HWiNFO64.exe, hwinfo64.exe\naida64.exe ; Steam") == "HWiNFO64.exe;aida64.exe;Steam"
    assert S.migrate_config({"tray_whitelist": "aida64.exe, HWiNFO"})["tray_whitelist"] == "aida64.exe;HWiNFO"
