"""Runs a real Anki (offscreen, in a throwaway base folder) with Incremento and this add-on, and checks that switching
the add-on off puts Incremento back exactly as it was and switching it on again restores sync.

    python tools/anki_smoke.py <path to an Incremento checkout>

Needs ``aqt`` and ``PyQt6`` installed (see requirements-dev.txt). Never touches your own Anki profile.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# First start asks for a language in a dialog; skip it (the profile is a throwaway).
BOOT = (
    "import faulthandler, os; faulthandler.dump_traceback_later(int(os.environ.get('SMOKE_DUMP', '0')) or 100000, exit=True);"
    " import sys, unittest.mock as m; sys.modules['PyQt6.QtPdf'] = m.MagicMock(); sys.modules['PyQt6.QtPdfWidgets'] = m.MagicMock(); import aqt.profiles as p; p.ProfileManager.setDefaultLang = lambda self, idx: self.setLang('en_US');"
    " import aqt; aqt.run()"
)

CHECK = r'''
import json, os, sys
from aqt import gui_hooks, mw
from aqt.qt import QTimer

RESULT = os.environ["SMOKE_RESULT"]
checks = {}


def menu_texts():
    texts = []
    for action in mw.menuBar().actions():
        if action.objectName() == "incremento_menu":
            texts = [a.text() for a in action.menu().actions()]
    return texts


def sync_actions():
    return [t for t in menu_texts() if "Sync" in t]


def run():
    try:
        import incremento_sync
        from incremento_sync import desktop, loader
        import importlib
        try:
            inc = importlib.import_module("incremento_host")
        except Exception:
            import traceback
            checks["incremento_import_error"] = traceback.format_exc()[-1500:]
            raise
        eb = sys.modules[inc.__name__ + ".backend.export_bundle"]
        unpatched = getattr(eb.should_skip_user_file, "__wrapped__", None)
        checks["patch_keeps_the_original"] = unpatched is not None
        hooks_before = (len(gui_hooks.profile_will_close._hooks), len(gui_hooks.sync_did_finish._hooks))

        checks["enabled_at_start"] = loader.is_enabled()
        checks["menu_has_sync_when_on"] = len(sync_actions()) >= 1
        checks["backup_skips_sync_when_on"] = bool(eb.should_skip_user_file("sync/blobs/x"))
        checks["engine_importable_when_on"] = (inc.__name__ + ".backend.sync_service") in sys.modules

        mw.addonManager.writeConfig("incremento_sync", {"enabled": False})
        desktop.apply_config()
        checks["disabled"] = not loader.is_enabled()
        checks["menu_clean_when_off"] = sync_actions() == []
        checks["engine_gone_when_off"] = not any(n.startswith(inc.__name__ + ".backend.sync_") for n in sys.modules)
        checks["backup_function_restored"] = sys.modules[inc.__name__ + ".backend.export_bundle"].should_skip_user_file is unpatched
        checks["backup_rule_restored"] = not sys.modules[inc.__name__ + ".backend.export_bundle"].should_skip_user_file("sync/blobs/x")
        checks["hooks_removed"] = (len(gui_hooks.profile_will_close._hooks), len(gui_hooks.sync_did_finish._hooks)) == hooks_before

        mw.addonManager.writeConfig("incremento_sync", {"enabled": True})
        desktop.apply_config()
        checks["enabled_again"] = loader.is_enabled()
        checks["menu_back_when_on_again"] = len(sync_actions()) >= 1
        checks["no_duplicate_actions"] = len(sync_actions()) == len(set(sync_actions()))
    except Exception as error:  # reported, not raised: the window must still close
        import traceback
        checks["error"] = traceback.format_exc()
    with open(RESULT, "w", encoding="utf8") as handle:
        json.dump(checks, handle, indent=1)
    os._exit(0)


gui_hooks.main_window_did_init.append(lambda: QTimer.singleShot(4000, run))
'''


def main() -> int:
    incremento = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory() as base_dir:
        base = Path(base_dir)
        addons = base / "addons21"
        shutil.copytree(incremento, addons / "incremento_host", ignore=shutil.ignore_patterns(".git", "__pycache__", "tests", "user_files"))
        shutil.copytree(ROOT / "incremento_sync", addons / "incremento_sync", ignore=shutil.ignore_patterns("__pycache__"))
        (addons / "zz_smoke").mkdir()
        (addons / "zz_smoke" / "__init__.py").write_text(CHECK, encoding="utf8")
        result = base / "result.json"
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "SMOKE_RESULT": str(result), "ANKI_BASE": str(base)}
        try:
            proc = subprocess.run(
                [sys.executable, "-c", BOOT, "-b", str(base), "-l", "en"],
                env=env, capture_output=True, text=True, timeout=int(os.environ.get("SMOKE_TIMEOUT", "180")),
            )
        except subprocess.TimeoutExpired as expired:
            print("Anki did not finish in time.")
            print((expired.stdout or b"")[-2000:], (expired.stderr or b"")[-4000:])
            return 1
        if not result.is_file():
            print(proc.stdout[-2000:], proc.stderr[-4000:])
            print("Anki did not report.")
            return 1
        checks = json.loads(result.read_text(encoding="utf8"))
    print(json.dumps(checks, indent=1))
    failed = [name for name, value in checks.items() if value is not True]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
