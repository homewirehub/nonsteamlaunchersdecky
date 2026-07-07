"""Tests for the shortcut self-heal after in-place launcher updates
(versioned executables, e.g. itch renaming Game-v1.exe to Game-v2.exe):

scanner.find_stale_shortcut_for_update (extracted via ast, run for real)
  1. Name matches, old exe gone, new exe present, same prefix -> returned,
     appid converted to unsigned int32.
  2. Old exe still exists on disk -> None (never redirect a working
     shortcut).
  3. New exe does not exist -> None.
  4. Old exe outside the new exe's compatdata prefix (user's own
     same-named shortcut) -> None.
  5. No shortcut with that name -> None.
  6. Shortcut already points at the new exe -> None.
  7. Missing shortcuts.vdf -> None, no crash.
  8. Unreadable shortcuts.vdf -> raises ShortcutsVdfError (K1 semantics).

scanner.create_new_entry wiring (source-level)
  9. create_new_entry calls find_stale_shortcut_for_update.
 10. The update record carries 'Update' and 'appid' and is assigned to
     decky_shortcuts.
 11. Re-emission guard _emitted_shortcut_updates exists at module level
     and is consulted in create_new_entry.

frontend wiring (source-level)
 12. scan.tsx routes messages with Update to updateShortcut for both
     /scan and /autoscan.
 13. createShortcut.tsx exports updateShortcut which sets exe and start
     dir but never touches launch options, compat tool or artwork.
"""

import ast
import os
import re
import struct
import sys
import tempfile
import types
import logging

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCANNER_PY = os.path.join(REPO, "py_modules", "lib", "scanner.py")
HOME = tempfile.mkdtemp(prefix="shortcut-update-test-home.")
STEAMID3 = 4711

sys.path.insert(0, os.path.join(REPO, "py_modules"))
import externals.vdf as real_vdf  # noqa: E402

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" - {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# --- Extract find_stale_shortcut_for_update + ShortcutsVdfError ----------

with open(SCANNER_PY) as f:
    scanner_src = f.read()
scanner_tree = ast.parse(scanner_src)

def extract(name, kind):
    for node in scanner_tree.body:
        if isinstance(node, kind) and node.name == name:
            return ast.get_source_segment(scanner_src, node)
    raise AssertionError(f"{name} not found in scanner.py")

func_src = extract("find_stale_shortcut_for_update", ast.FunctionDef)
class_src = extract("ShortcutsVdfError", ast.ClassDef)

_logger = logging.getLogger("shortcut-update-test")
_logger.addHandler(logging.NullHandler())
decky_stub = types.ModuleType("decky_plugin")
decky_stub.logger = _logger

ns = {
    "os": os,
    "re": re,
    "vdf": real_vdf,
    "decky_plugin": decky_stub,
    "logged_in_home": HOME,
    "steamid3": STEAMID3,
    "_shortcuts_vdf_error": False,
}
exec(class_src, ns)
exec(func_src, ns)
find_stale = ns["find_stale_shortcut_for_update"]
ShortcutsVdfError = ns["ShortcutsVdfError"]


# --- Test fixture ---------------------------------------------------------

PREFIX = f"{HOME}/.local/share/Steam/steamapps/compatdata/NonSteamLaunchers/"
GAMEDIR = f"{PREFIX}pfx/drive_c/users/steamuser/AppData/Roaming/itch/apps/testgame"
OTHER_PREFIX = f"{HOME}/.local/share/Steam/steamapps/compatdata/otherprefix/"
OLD_EXE = f"{GAMEDIR}/TestGame-v1.0-pc/TestGame.exe"
NEW_EXE = f"{GAMEDIR}/TestGame-v2.0-pc/TestGame.exe"
SHORTCUTS = f"{HOME}/.steam/root/userdata/{STEAMID3}/config/shortcuts.vdf"

# appid chosen so the signed int32 representation is negative
APPID_UNSIGNED = 3490586613
APPID_SIGNED = struct.unpack("i", struct.pack("I", APPID_UNSIGNED))[0]


def make_exe(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"MZ")


def remove_exe(path):
    if os.path.isfile(path):
        os.remove(path)


def write_shortcuts(entries):
    os.makedirs(os.path.dirname(SHORTCUTS), exist_ok=True)
    data = {"shortcuts": {str(i): e for i, e in enumerate(entries)}}
    with open(SHORTCUTS, "wb") as f:
        f.write(real_vdf.binary_dumps(data))


def entry(name, exe, appid=APPID_SIGNED):
    return {
        "appid": appid,
        "AppName": name,
        "Exe": f'"{exe}"',
        "StartDir": f'"{os.path.dirname(exe)}"',
        "LaunchOptions": f'STEAM_COMPAT_DATA_PATH="{PREFIX}" %command%',
    }


# 1. The heal case
make_exe(NEW_EXE)
remove_exe(OLD_EXE)
write_shortcuts([entry("TestGame", OLD_EXE)])
res = find_stale("TestGame", f'"{NEW_EXE}"', f'"{os.path.dirname(NEW_EXE)}"')
check("1. stale shortcut found, appid unsigned",
      res is not None and res["appid"] == APPID_UNSIGNED and res["old_exe"] == OLD_EXE,
      f"got {res}")

# 2. Old exe still exists -> never redirect
make_exe(OLD_EXE)
res = find_stale("TestGame", f'"{NEW_EXE}"', f'"{os.path.dirname(NEW_EXE)}"')
check("2. working shortcut untouched", res is None, f"got {res}")
remove_exe(OLD_EXE)

# 3. New exe missing -> no heal
res = find_stale("TestGame", f'"{GAMEDIR}/nope/TestGame.exe"', '""')
check("3. missing new exe -> None", res is None, f"got {res}")

# 4. Old exe outside the new exe's prefix -> user's shortcut, hands off
foreign = f"{OTHER_PREFIX}pfx/drive_c/TestGame.exe"
write_shortcuts([entry("TestGame", foreign)])
res = find_stale("TestGame", f'"{NEW_EXE}"', f'"{os.path.dirname(NEW_EXE)}"')
check("4. foreign prefix -> None", res is None, f"got {res}")

# 5. No name match
write_shortcuts([entry("SomethingElse", OLD_EXE)])
res = find_stale("TestGame", f'"{NEW_EXE}"', f'"{os.path.dirname(NEW_EXE)}"')
check("5. no name match -> None", res is None, f"got {res}")

# 6. Shortcut already points at the new exe
write_shortcuts([entry("TestGame", NEW_EXE)])
res = find_stale("TestGame", f'"{NEW_EXE}"', f'"{os.path.dirname(NEW_EXE)}"')
check("6. already healed -> None", res is None, f"got {res}")

# 7. Missing shortcuts.vdf
os.remove(SHORTCUTS)
res = find_stale("TestGame", f'"{NEW_EXE}"', f'"{os.path.dirname(NEW_EXE)}"')
check("7. missing shortcuts.vdf -> None", res is None, f"got {res}")

# 8. Unreadable shortcuts.vdf -> K1 semantics
os.makedirs(os.path.dirname(SHORTCUTS), exist_ok=True)
with open(SHORTCUTS, "wb") as f:
    f.write(b"\x00garbage")
try:
    find_stale("TestGame", f'"{NEW_EXE}"', f'"{os.path.dirname(NEW_EXE)}"')
    check("8. unreadable vdf raises ShortcutsVdfError", False, "no exception")
except ShortcutsVdfError:
    check("8. unreadable vdf raises ShortcutsVdfError", True)
except Exception as e:  # noqa: BLE001
    check("8. unreadable vdf raises ShortcutsVdfError", False, f"raised {type(e).__name__}")


# --- create_new_entry wiring (source-level) --------------------------------

cne = next(n for n in scanner_tree.body
           if isinstance(n, ast.FunctionDef) and n.name == "create_new_entry")
cne_src = ast.get_source_segment(scanner_src, cne)

calls = [n for n in ast.walk(cne) if isinstance(n, ast.Call)
         and isinstance(n.func, ast.Name) and n.func.id == "find_stale_shortcut_for_update"]
check("9. create_new_entry calls find_stale_shortcut_for_update", len(calls) == 1)

update_dicts = [
    n for n in ast.walk(cne) if isinstance(n, ast.Assign)
    and isinstance(n.value, ast.Dict)
    and {"Update", "appid"} <= {k.value for k in n.value.keys if isinstance(k, ast.Constant)}
    and any(isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
            and t.value.id == "decky_shortcuts" for t in n.targets)
]
check("10. update record with Update+appid assigned to decky_shortcuts",
      len(update_dicts) == 1)

module_has_guard = any(
    isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == "_emitted_shortcut_updates" for t in n.targets)
    for n in scanner_tree.body
)
check("11. re-emission guard exists and is consulted",
      module_has_guard and "_emitted_shortcut_updates" in cne_src)


# --- frontend wiring (source-level) ----------------------------------------

with open(os.path.join(REPO, "src", "hooks", "scan.tsx")) as f:
    scan_tsx = f.read()
check("12. scan.tsx routes Update messages for /scan and /autoscan",
      "message.Update" in scan_tsx
      and "updateShortcut(message)" in scan_tsx
      and scan_tsx.count("handleGameMessage(message)") == 2)

with open(os.path.join(REPO, "src", "hooks", "createShortcut.tsx")) as f:
    cs_tsx = f.read()
m = re.search(r"export async function updateShortcut.*", cs_tsx, re.DOTALL)
body = m.group(0) if m else ""
check("13. updateShortcut sets exe+startdir, leaves the rest alone",
      bool(m)
      and "SetShortcutExe" in body and "SetShortcutStartDir" in body
      and "SetAppLaunchOptions" not in body
      and "SpecifyCompatTool" not in body
      and "SetCustomArtworkForApp" not in body
      and "AddShortcut" not in body)


print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("ALL PASS")
