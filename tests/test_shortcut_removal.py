"""Tests for the deliberate-uninstall shortcut removal support:

game_tracker.get_shortcut_entries_for_names
  1. Exact name match + shared NSL prefix marker in Exe -> returned,
     appid converted to unsigned.
  2. Exact name match + separate-app-id prefix marker in StartDir -> returned.
  3. Exact name match but Exe/StartDir/LaunchOptions outside NSL-managed
     locations (user's own shortcut) -> NOT returned.
  4. Shortcuts that were not asked for are never returned.
  5. Name matching is case-insensitive on both sides.
  6. Flatpak marker (GFN) in LaunchOptions qualifies.
  7. Missing shortcuts.vdf -> empty result, no crash.

game_tracker.purge_tracking_for_launcher
  8. Removes the launcher's item under the shared "Launcher" bucket and
     its miss counter; unrelated entries survive.
  9. Removes a launcher's own per-game bucket entirely.
 10. Purges the in-memory master list state too.
 11. Missing files -> no crash, returns False.

main.py wiring (source-level)
 12. to_nice_name maps frontend option names to script display names.
 13. install() calls purge_tracking_for_launcher inside the scan-lock block.
"""

import ast
import json
import logging
import os
import struct
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = tempfile.mkdtemp(prefix="shortcut-removal-test-home.")
STEAMID3 = 4711
STEAMID64 = STEAMID3 + 76561197960265728

# Stub decky_plugin and vdf before importing game_tracker
decky_stub = types.ModuleType("decky_plugin")
decky_stub.DECKY_USER_HOME = HOME
decky_stub.DECKY_PLUGIN_DIR = HOME
_logger = logging.getLogger("shortcut-removal-test")
_logger.addHandler(logging.NullHandler())
decky_stub.logger = _logger
sys.modules["decky_plugin"] = decky_stub

sys.path.insert(0, os.path.join(REPO, "py_modules"))
import externals.vdf as real_vdf  # noqa: E402
sys.modules["vdf"] = real_vdf

sys.path.insert(0, os.path.join(REPO, "py_modules", "lib"))
import scanners.game_tracker as gt  # noqa: E402

INSTALLED = os.path.join(HOME, ".config/systemd/user/installedapps.json")
COUNTERS = os.path.join(HOME, ".config/systemd/user/nsl_removal_counters.json")
SHORTCUTS = os.path.join(HOME, f".steam/root/userdata/{STEAMID3}/config/shortcuts.vdf")

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def write_loginusers():
    path = os.path.join(HOME, ".steam/root/config/loginusers.vdf")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(
            '"users"\n{\n'
            f'\t"{STEAMID64}"\n'
            '\t{\n'
            '\t\t"AccountName"\t\t"tester"\n'
            '\t\t"Timestamp"\t\t"1700000000"\n'
            '\t}\n'
            '}\n'
        )
    os.makedirs(os.path.join(HOME, f".steam/root/userdata/{STEAMID3}"), exist_ok=True)


def signed(appid_unsigned):
    return struct.unpack("<i", struct.pack("<I", appid_unsigned))[0]


def write_shortcuts(entries):
    os.makedirs(os.path.dirname(SHORTCUTS), exist_ok=True)
    payload = {"shortcuts": {str(i): e for i, e in enumerate(entries)}}
    with open(SHORTCUTS, "wb") as f:
        f.write(real_vdf.binary_dumps(payload))


def reset_files():
    for path in (INSTALLED, COUNTERS, SHORTCUTS):
        if os.path.exists(path):
            os.remove(path)
    gt._master_list.clear()
    gt._previous_master_list.clear()


write_loginusers()

GFN_APPID = 2341031622  # unsigned form of the real -1953935674 case

# --- get_shortcut_entries_for_names ---------------------------------------

reset_files()
write_shortcuts([
    {
        "appid": signed(GFN_APPID),
        "AppName": "NVIDIA GeForce NOW",
        "Exe": '"/usr/bin/flatpak"',
        "StartDir": '"/usr/bin"',
        "LaunchOptions": "run --branch=master com.nvidia.geforcenow",
    },
    {
        "appid": 1111111111,
        "AppName": "GOG Galaxy",
        "Exe": '"/home/tester/.local/share/Steam/steamapps/compatdata/GogGalaxyLauncher/pfx/drive_c/Program Files (x86)/GOG Galaxy/GalaxyClient.exe"',
        "StartDir": '"/home/tester/.local/share/Steam/steamapps/compatdata/GogGalaxyLauncher/pfx/drive_c/Program Files (x86)/GOG Galaxy"',
        "LaunchOptions": "",
    },
    {
        "appid": 1234567890,
        "AppName": "ITCH.IO",
        "Exe": '"/home/tester/.local/share/Steam/steamapps/compatdata/NonSteamLaunchers/pfx/drive_c/users/steamuser/AppData/Local/itch/app-26.13.0/itch.exe"',
        "StartDir": '"/home/tester/.local/share/Steam/steamapps/compatdata/NonSteamLaunchers/pfx/drive_c/users/steamuser/AppData/Local/itch"',
        "LaunchOptions": "STEAM_COMPAT_DATA_PATH=... %command%",
    },
    {
        # User's own shortcut that merely shares the launcher name
        "appid": 2222222222 - 2**32,
        "AppName": "Ubisoft Connect",
        "Exe": '"/home/tester/my-own-tools/ubisoft-thing.sh"',
        "StartDir": '"/home/tester/my-own-tools"',
        "LaunchOptions": "",
    },
    {
        "appid": 3333333333 - 2**32,
        "AppName": "Netflix",
        "Exe": '"/app/bin/chrome"',
        "StartDir": '"/usr/bin"',
        "LaunchOptions": "--kiosk https://netflix.com",
    },
])

res = gt.get_shortcut_entries_for_names(["NVIDIA GeForce NOW", "GOG Galaxy", "itch.io", "Ubisoft Connect"])

check("1. flatpak marker in LaunchOptions qualifies (GFN)",
      "NVIDIA GeForce NOW" in res, f"got {res}")
check("1b. appid returned unsigned",
      res.get("NVIDIA GeForce NOW", {}).get("appid") == GFN_APPID,
      f"got {res.get('NVIDIA GeForce NOW')}")
check("2. separate-app-id prefix marker qualifies (GOG)",
      res.get("GOG Galaxy", {}).get("appid") == 1111111111, f"got {res.get('GOG Galaxy')}")
check("5. case-insensitive name match, shared prefix (itch)",
      res.get("itch.io", {}).get("appid") == 1234567890, f"got {res.get('itch.io')}")
check("5b. original shortcut AppName reported",
      res.get("itch.io", {}).get("appname") == "ITCH.IO", f"got {res.get('itch.io')}")
check("3. same-named user shortcut outside NSL paths is NOT returned",
      "Ubisoft Connect" not in res, f"got {res.get('Ubisoft Connect')}")
check("4. unrelated shortcuts never returned",
      all(k in ("NVIDIA GeForce NOW", "GOG Galaxy", "itch.io") for k in res), f"got {list(res)}")

os.remove(SHORTCUTS)
res_missing = gt.get_shortcut_entries_for_names(["GOG Galaxy"])
check("7. missing shortcuts.vdf -> empty result", res_missing == {}, f"got {res_missing}")

# --- purge_tracking_for_launcher -------------------------------------------

reset_files()
os.makedirs(os.path.dirname(INSTALLED), exist_ok=True)
master_fixture = {
    "Launcher": {
        "NVIDIA GeForce NOW": {"first_seen": "x", "last_seen": "y", "still_installed": True},
        "Ubisoft Connect": {"first_seen": "x", "last_seen": "y", "still_installed": True},
    },
    "GOG Galaxy": {
        "Some GOG Game": {"first_seen": "x", "last_seen": "y", "still_installed": True},
    },
}
with open(INSTALLED, "w") as f:
    json.dump(master_fixture, f)
with open(COUNTERS, "w") as f:
    json.dump({"Launcher": {"Ubisoft Connect": 1}}, f)
gt._master_list.update(json.loads(json.dumps(master_fixture)))
gt._previous_master_list.update(json.loads(json.dumps(master_fixture)))

purged = gt.purge_tracking_for_launcher("Ubisoft Connect")
with open(INSTALLED) as f:
    master_after = json.load(f)
with open(COUNTERS) as f:
    counters_after = json.load(f)

check("8. purge returns True when something was removed", purged is True)
check("8a. UC gone from shared Launcher bucket",
      "Ubisoft Connect" not in master_after.get("Launcher", {}), f"got {master_after}")
check("8b. GFN survives in shared Launcher bucket",
      "NVIDIA GeForce NOW" in master_after.get("Launcher", {}), f"got {master_after}")
check("8c. unrelated per-game bucket survives",
      "GOG Galaxy" in master_after, f"got {master_after}")
check("8d. UC miss counter gone (empty bucket dropped)",
      counters_after == {}, f"got {counters_after}")
check("10. in-memory master list purged",
      "Ubisoft Connect" not in gt._master_list.get("Launcher", {})
      and "Ubisoft Connect" not in gt._previous_master_list.get("Launcher", {}),
      f"got {gt._master_list}")

purged_gog = gt.purge_tracking_for_launcher("GOG Galaxy")
with open(INSTALLED) as f:
    master_after_gog = json.load(f)
check("9. launcher's own per-game bucket removed entirely",
      purged_gog is True and "GOG Galaxy" not in master_after_gog, f"got {master_after_gog}")

reset_files()
purged_none = gt.purge_tracking_for_launcher("GOG Galaxy")
check("11. missing files -> no crash, returns False", purged_none is False)

# --- main.py wiring ---------------------------------------------------------

main_src = open(os.path.join(REPO, "main.py")).read()
main_ast = ast.parse(main_src)

# Extract camel_to_title + to_nice_name and exercise the mapping standalone.
extracted = [n for n in main_ast.body if isinstance(n, ast.FunctionDef)
             and n.name in ("camel_to_title", "to_nice_name")]
check("12. to_nice_name exists at module level", len(extracted) == 2)
ns = {"re": __import__("re")}
exec(compile(ast.Module(body=extracted, type_ignores=[]), "main.py", "exec"), ns)
cases = {
    "Uplay": "Ubisoft Connect",
    "GogGalaxy": "GOG Galaxy",
    "ItchIo": "itch.io",
    "NvidiaGeForcenow": "NVIDIA GeForce NOW",
    "EpicGames": "Epic Games",
    "separateAppIds": "",
}
for arg, want in cases.items():
    got = ns["to_nice_name"](arg)
    check(f"12. to_nice_name({arg!r}) == {want!r}", got == want, f"got {got!r}")

install_fn = None
for node in ast.walk(main_ast):
    if isinstance(node, ast.AsyncFunctionDef) and node.name == "install":
        install_fn = node
        break
check("13. install() found", install_fn is not None)
if install_fn:
    src_install = ast.get_source_segment(main_src, install_fn)
    lock_blocks = [n for n in ast.walk(install_fn) if isinstance(n, ast.AsyncWith)]
    purge_in_lock = any("purge_tracking_for_launcher" in ast.get_source_segment(main_src, b)
                        for b in lock_blocks)
    check("13. purge_tracking_for_launcher called inside the scan-lock block",
          purge_in_lock, "not referenced under any async with block in install()")

getsc = [n for n in ast.walk(main_ast)
         if isinstance(n, ast.AsyncFunctionDef) and n.name == "get_shortcut_appids"]
check("13b. get_shortcut_appids plugin method exists", len(getsc) == 1)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL TESTS PASSED")
