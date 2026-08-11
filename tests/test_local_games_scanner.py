"""Tests for the Local Games scanner (scanners/local_games_scanner.py).

Same shape as test_m25_removal_cycles.py: decky_plugin is stubbed via
sys.modules, the scanner is imported directly and driven against a temp
HOME with fabricated game folders. create_new_entry and track_game are
replaced with recorders, so nothing touches shortcuts.vdf or the tracking
files.

The safety properties this pins down are the reason the scanner exists in
this shape - a folder scanner feeds game_tracker, and game_tracker removes
shortcuts for anything it stops seeing:

  1. Disabled by default: no setting -> nothing scanned, nothing tracked.
  2. Enabled: folders with a usable .exe become entries, and every entry is
     also tracked (an untracked entry would be "removed" three cycles later).
  3. A folder without a usable .exe is not a game and is skipped.
  4. Missing scan folder -> NOTHING tracked. finalize_game_tracking only
     skips removal detection for a launcher that reported nothing at all;
     tracking a partial result here would delete shortcuts when an SD card
     is not mounted.
  5. Unreadable scan folder -> same, nothing tracked.
  6. Folders starting with "." or "_" are skipped (~/Games/_tools).
  7. pick_main_exe prefers the real game over uninstallers, setups and
     launchers, and picks the shallowest/largest candidate.
  8. A custom localGamesPath is honoured.
"""

import json
import logging
import os
import shutil
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = tempfile.mkdtemp(prefix="localgames-home.")
SETTINGS_DIR = os.path.join(HOME, "settings")
os.makedirs(SETTINGS_DIR, exist_ok=True)

decky_stub = types.ModuleType("decky_plugin")
decky_stub.DECKY_USER_HOME = HOME
decky_stub.DECKY_PLUGIN_DIR = HOME
decky_stub.DECKY_PLUGIN_SETTINGS_DIR = SETTINGS_DIR
_logger = logging.getLogger("localgamestest")
_logger.addHandler(logging.NullHandler())
decky_stub.logger = _logger
sys.modules["decky_plugin"] = decky_stub

# game_tracker pulls in vdf; the scanner only needs track_game from it.
sys.path.insert(0, os.path.join(REPO, "py_modules"))
import externals.vdf as real_vdf  # noqa: E402
sys.modules["vdf"] = real_vdf

sys.path.insert(0, os.path.join(REPO, "py_modules", "lib"))
import scanners.local_games_scanner as lgs  # noqa: E402

tracked = []
lgs.track_game = lambda appname, launcher: tracked.append((appname, launcher))

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def write_settings(**kw):
    with open(os.path.join(SETTINGS_DIR, "config.json"), "w") as f:
        json.dump({"settings": kw}, f)


def make_file(path, size=1024):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\0" * size)


def run_scan(home=HOME):
    """Returns the list of create_new_entry calls."""
    entries = []
    del tracked[:]

    def create_new_entry(exe, appname, launchoptions, startingdir, launcher):
        entries.append({"exe": exe, "appname": appname, "startdir": startingdir,
                        "launcher": launcher})

    lgs.local_games_scanner(home, create_new_entry)
    return entries


def run_tests():
    games = os.path.join(HOME, "Games")

    # A normal Ren'Py-ish game
    make_file(os.path.join(games, "Quiet Harbour", "QuietHarbour.exe"), 5000)
    make_file(os.path.join(games, "Quiet Harbour", "game", "script.rpa"), 100)
    make_file(os.path.join(games, "Quiet Harbour", "lib", "python.exe"), 900)
    # A game shipping an uninstaller and a setup next to the real exe
    make_file(os.path.join(games, "Noisy", "unins000.exe"), 9000)
    make_file(os.path.join(games, "Noisy", "setup.exe"), 8000)
    make_file(os.path.join(games, "Noisy", "Launcher.exe"), 7000)
    make_file(os.path.join(games, "Noisy", "TheGame.exe"), 100)
    # Not a game: no exe at all
    make_file(os.path.join(games, "save-backup", "notes.txt"), 10)
    # Not a game: only an uninstaller
    make_file(os.path.join(games, "leftovers", "unins000.exe"), 500)
    # Must be ignored by name
    make_file(os.path.join(games, "_tools", "autoadd.exe"), 500)
    make_file(os.path.join(games, ".cache", "thing.exe"), 500)

    # --- 1. disabled by default -------------------------------------
    write_settings()
    entries = run_scan()
    check("disabled by default: no entries", entries == [], str(entries))
    check("disabled by default: nothing tracked", tracked == [], str(tracked))

    # --- 2./3./6. enabled -------------------------------------------
    write_settings(localGamesEnabled=True)
    entries = run_scan()
    names = sorted(e["appname"] for e in entries)
    check("enabled: finds the two real games", names == ["Noisy", "Quiet Harbour"], str(names))
    check("every entry is also tracked",
          sorted(n for n, _ in tracked) == names, str(tracked))
    check("tracked under the Local Games launcher",
          all(l == lgs.LAUNCHER for _, l in tracked), str(tracked))
    check("folder without exe is skipped", "save-backup" not in names)
    check("folder with only an uninstaller is skipped", "leftovers" not in names)
    check("_tools is skipped", "_tools" not in names)
    check(".cache is skipped", ".cache" not in names)

    quiet = [e for e in entries if e["appname"] == "Quiet Harbour"][0]
    check("exe is quoted and absolute",
          quiet["exe"] == '"%s"' % os.path.join(games, "Quiet Harbour", "QuietHarbour.exe"),
          quiet["exe"])
    check("startdir is quoted and ends with a separator",
          quiet["startdir"] == '"%s/"' % os.path.join(games, "Quiet Harbour"),
          quiet["startdir"])
    check("launcher label is passed through",
          quiet["launcher"] == lgs.LAUNCHER, quiet["launcher"])
    check("python.exe in lib/ is not mistaken for the game",
          "python" not in quiet["exe"].lower(), quiet["exe"])

    # --- 7. exe preference ------------------------------------------
    noisy = [e for e in entries if e["appname"] == "Noisy"][0]
    check("real game beats uninstaller/setup/launcher, even when smaller",
          noisy["exe"].endswith('TheGame.exe"'), noisy["exe"])

    # --- 4. missing folder ------------------------------------------
    missing_home = tempfile.mkdtemp(prefix="localgames-empty.")
    try:
        entries = run_scan(missing_home)
        check("missing folder: no entries", entries == [], str(entries))
        check("missing folder: NOTHING tracked (removal guard)",
              tracked == [], str(tracked))
    finally:
        shutil.rmtree(missing_home, ignore_errors=True)

    # --- 5. unreadable folder ---------------------------------------
    blocked_home = tempfile.mkdtemp(prefix="localgames-blocked.")
    blocked = os.path.join(blocked_home, "Games")
    os.makedirs(blocked)
    os.chmod(blocked, 0o000)
    try:
        if os.access(blocked, os.R_OK):
            # running as root - the permission bit cannot be tested
            print("[SKIP] unreadable folder (running with override rights)")
        else:
            entries = run_scan(blocked_home)
            check("unreadable folder: no entries", entries == [], str(entries))
            check("unreadable folder: NOTHING tracked (removal guard)",
                  tracked == [], str(tracked))
    finally:
        os.chmod(blocked, 0o755)
        shutil.rmtree(blocked_home, ignore_errors=True)

    # --- 8. custom path ---------------------------------------------
    custom = os.path.join(HOME, "Elsewhere")
    make_file(os.path.join(custom, "Other Game", "Other.exe"), 4000)
    write_settings(localGamesEnabled=True, localGamesPath=custom)
    entries = run_scan()
    check("custom localGamesPath is honoured",
          [e["appname"] for e in entries] == ["Other Game"], str(entries))

    # --- corrupt settings are treated as disabled --------------------
    with open(os.path.join(SETTINGS_DIR, "config.json"), "w") as f:
        f.write("{not json")
    entries = run_scan()
    check("corrupt settings: treated as disabled", entries == [], str(entries))
    check("corrupt settings: nothing tracked", tracked == [], str(tracked))


if __name__ == "__main__":
    try:
        run_tests()
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("All Local Games scanner tests passed.")
    sys.exit(0)
