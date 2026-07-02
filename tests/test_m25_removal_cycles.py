"""Multi-cycle tests for AUDIT M25 (ported): removal counters in game_tracker.py.

Ported from the standalone repo's tests/test_m25_removal_cycles.py. Unlike
there, game_tracker.py is an importable module: decky_plugin and vdf are
stubbed via sys.modules, then scanners.game_tracker is imported directly and
driven against a temp HOME. Each "cycle" mirrors what scan() does:
load_master_list + clear_current_scan + track_game(...) + finalize_game_tracking.

uninstall_removed_apps and load_shortcuts_appid_map are replaced with
recorders - the real ones would run `steam steam://uninstall/...` on the
machine running the tests.

Covered:
  1. Game missing 1x/2x -> NOT removed (counter 1, 2); missing 3x -> removed,
     uninstall handler invoked, counter dropped.
  2. Game reappearing after misses -> counter resets (dropped from file).
  3. Empty scan while master list is non-empty (the single-"Launcher"-key
     case) -> skip: no counting, no removal, counters carried over unchanged.
  4. Corrupt counter file (invalid JSON / wrong type) -> reset to 0, no removal.
  5. Already-removed games (still_installed=False) are not re-reported.
  6. Partial scan (some games found) -> misses for absent games DO count.
"""

import json
import logging
import os
import shutil
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = tempfile.mkdtemp(prefix="m25test-home.")
LAUNCHER = "Launcher"

# Stub decky_plugin and vdf before importing game_tracker
decky_stub = types.ModuleType("decky_plugin")
decky_stub.DECKY_USER_HOME = HOME
decky_stub.DECKY_PLUGIN_DIR = HOME
_logger = logging.getLogger("m25test")
_logger.addHandler(logging.NullHandler())
decky_stub.logger = _logger
sys.modules["decky_plugin"] = decky_stub

sys.path.insert(0, os.path.join(REPO, "py_modules"))
import externals.vdf as real_vdf  # noqa: E402
sys.modules["vdf"] = real_vdf

sys.path.insert(0, os.path.join(REPO, "py_modules", "lib"))
import scanners.game_tracker as gt  # noqa: E402

# Never let tests trigger real `steam steam://uninstall/...` calls
uninstall_calls = []
gt.uninstall_removed_apps = lambda apps, appid_map: uninstall_calls.append(list(apps))
gt.load_shortcuts_appid_map = lambda: {}

INSTALLED = os.path.join(HOME, ".config/systemd/user/installedapps.json")
COUNTERS = os.path.join(HOME, ".config/systemd/user/nsl_removal_counters.json")

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def cycle(present_games):
    gt.load_master_list()
    gt.clear_current_scan()
    for name in present_games:
        gt.track_game(name, LAUNCHER)
    return gt.finalize_game_tracking()


def read_counters():
    if not os.path.exists(COUNTERS):
        return None
    with open(COUNTERS) as f:
        return json.load(f)


def read_master():
    with open(INSTALLED) as f:
        return json.load(f)


def fresh():
    for path in (INSTALLED, COUNTERS):
        if os.path.exists(path):
            os.remove(path)
    uninstall_calls.clear()


def run_tests():
    # --- 1. three consecutive misses required ---
    fresh()
    cycle(["GameA", "GameB"])
    removed = cycle(["GameA"])
    check("miss 1/3 -> not removed", removed == {})
    check("miss 1/3 -> counter is 1", read_counters() == {LAUNCHER: {"GameB": 1}})
    check("miss 1/3 -> still_installed stays True", read_master()[LAUNCHER]["GameB"]["still_installed"] is True)
    removed = cycle(["GameA"])
    check("miss 2/3 -> not removed", removed == {})
    check("miss 2/3 -> counter is 2", read_counters() == {LAUNCHER: {"GameB": 2}})
    check("no uninstall before threshold", uninstall_calls == [])
    removed = cycle(["GameA"])
    check("miss 3/3 -> removed", removed == {LAUNCHER: ["GameB"]})
    check("uninstall handler invoked once", uninstall_calls == [["GameB"]])
    check("counter dropped after removal", read_counters() == {})
    check("still_installed False after removal", read_master()[LAUNCHER]["GameB"]["still_installed"] is False)

    # --- 2. reappearing resets the counter ---
    fresh()
    cycle(["GameA", "GameB"])
    cycle(["GameA"])
    cycle(["GameA"])
    check("setup: counter at 2", read_counters() == {LAUNCHER: {"GameB": 2}})
    removed = cycle(["GameA", "GameB"])
    check("reappear -> not removed", removed == {})
    check("reappear -> counter dropped", read_counters() == {})
    removed = cycle(["GameA"])
    check("miss after reappear counts from 1", read_counters() == {LAUNCHER: {"GameB": 1}} and removed == {})

    # --- 3. empty scan, non-empty master (single-key protection) ---
    fresh()
    cycle(["GameA", "GameB"])
    cycle(["GameA"])  # GameB at 1
    removed = cycle([])
    check("empty scan -> nothing removed", removed == {})
    check("empty scan -> counters carried unchanged", read_counters() == {LAUNCHER: {"GameB": 1}})
    master = read_master()
    check(
        "empty scan -> master untouched",
        master[LAUNCHER]["GameA"]["still_installed"] is True
        and master[LAUNCHER]["GameB"]["still_installed"] is True,
    )
    check("empty scan -> no uninstall", uninstall_calls == [])
    # even three empty scans in a row must not remove anything
    cycle([])
    removed = cycle([])
    check("repeated empty scans -> still nothing removed", removed == {} and uninstall_calls == [])

    # --- 4. corrupt counter file resets to 0 ---
    fresh()
    cycle(["GameA", "GameB"])
    with open(COUNTERS, "w") as f:
        f.write("{not valid json")
    removed = cycle(["GameA"])
    check("corrupt counters -> not removed", removed == {})
    check("corrupt counters -> recount from 1", read_counters() == {LAUNCHER: {"GameB": 1}})
    with open(COUNTERS, "w") as f:
        json.dump([1, 2, 3], f)
    removed = cycle(["GameA"])
    check("wrong-type counters -> not removed", removed == {})
    check("wrong-type counters -> recount from 1", read_counters() == {LAUNCHER: {"GameB": 1}})

    # --- 5. already-removed games are not re-reported ---
    fresh()
    cycle(["GameA", "GameB"])
    cycle(["GameA"])
    cycle(["GameA"])
    cycle(["GameA"])  # GameB removed here
    uninstall_calls.clear()
    removed = cycle(["GameA"])
    check("removed game not re-reported", removed == {})
    check("removed game not re-uninstalled", uninstall_calls == [])
    check("no counter for removed game", read_counters() == {})

    # --- 6. partial scan counts misses for absent games ---
    fresh()
    cycle(["GameA", "GameB", "GameC"])
    removed = cycle(["GameA"])
    check(
        "partial scan -> misses count for absent games",
        removed == {} and read_counters() == {LAUNCHER: {"GameB": 1, "GameC": 1}},
    )

    # --- atomic write leaves no temp files ---
    leftovers = [
        f for f in os.listdir(os.path.dirname(COUNTERS))
        if f.startswith(".nsl_removal_counters.")
    ]
    check("no temp leftovers from counter writes", leftovers == [], str(leftovers))


if __name__ == "__main__":
    try:
        run_tests()
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("All M25 tests passed.")
    sys.exit(0)
