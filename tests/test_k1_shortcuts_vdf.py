"""Tests for AUDIT K1 (ported): shortcuts.vdf must never be wiped or rewritten.

Extracts check_if_shortcut_exists()/ShortcutsVdfError from py_modules/lib/scanner.py
and create_empty_shortcuts_vdf() from py_modules/lib/get_env_vars.py via ast
(importing the modules would pull in decky_plugin and every scanner) and drives
them against a temp HOME.

Covered:
  1. Valid file, matching entry -> True; file byte-identical.
  2. Valid file, no match -> False; file byte-identical.
  3. Valid file WITHOUT exec bit -> parsed normally (old code wiped it here);
     file bytes and mode unchanged.
  4. Corrupt file -> ShortcutsVdfError raised, abort flag set, file untouched.
  5. Parseable file without 'shortcuts' key -> ShortcutsVdfError, file untouched.
  6. Missing file -> False, file not created.
  7. create_empty_shortcuts_vdf: missing -> valid empty vdf created (exec bit,
     no temp leftovers).
  8. create_empty_shortcuts_vdf on an existing file -> byte-identical (race
     between existence check and write must not overwrite).
"""

import ast
import logging
import os
import platform
import shutil
import stat
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "py_modules"))
import externals.vdf as vdf  # noqa: E402

SCANNER = os.path.join(REPO, "py_modules", "lib", "scanner.py")
GET_ENV_VARS = os.path.join(REPO, "py_modules", "lib", "get_env_vars.py")
STEAMID3 = "12345"

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def make_decky_stub():
    logger = logging.getLogger("k1test")
    logger.addHandler(logging.NullHandler())
    return types.SimpleNamespace(logger=logger)


def extract(path, names):
    with open(path) as f:
        tree = ast.parse(f.read())
    nodes = [
        n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names
    ]
    assert len(nodes) == len(names), f"expected {names} in {path}, found {[n.name for n in nodes]}"
    module = ast.Module(body=nodes, type_ignores=[])
    return compile(module, path, "exec")


def load_scanner_env(home):
    g = {
        "os": os,
        "platform": platform,
        "vdf": vdf,
        "decky_plugin": make_decky_stub(),
        "env_vars": {},
        "logged_in_home": home,
        "steamid3": STEAMID3,
        "_shortcuts_vdf_error": False,
    }
    exec(extract(SCANNER, {"ShortcutsVdfError", "check_if_shortcut_exists"}), g)
    return g


def load_env_vars_env():
    g = {
        "os": os,
        "tempfile": tempfile,
        "vdf": vdf,
        "decky_plugin": make_decky_stub(),
    }
    exec(extract(GET_ENV_VARS, {"create_empty_shortcuts_vdf"}), g)
    return g


ENTRY = {
    "appname": "TestGame",
    "exe": '"/usr/bin/testgame"',
    "StartDir": '"/usr/bin"',
    "LaunchOptions": "",
}


def write_vdf(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(vdf.binary_dumps(payload))


def run_tests():
    home = tempfile.mkdtemp(prefix="k1test-home.")
    try:
        g = load_scanner_env(home)
        vdf_path = f"{home}/.steam/root/userdata/{STEAMID3}/config/shortcuts.vdf"
        config_dir = os.path.dirname(vdf_path)
        call = lambda: g["check_if_shortcut_exists"](
            "TestGame", '"/usr/bin/testgame"', '"/usr/bin"', ""
        )

        # 1. valid file, matching entry
        write_vdf(vdf_path, {"shortcuts": {"0": ENTRY}})
        before = open(vdf_path, "rb").read()
        result = call()
        check("match found in valid file", result is True)
        check("valid file untouched", open(vdf_path, "rb").read() == before)

        # 2. valid file, no match
        g["_shortcuts_vdf_error"] = False
        result = g["check_if_shortcut_exists"]("OtherGame", '"/x"', '"/y"', "")
        check("no match -> False", result is False)
        check("file untouched after no-match", open(vdf_path, "rb").read() == before)
        check("no abort flag on valid file", g["_shortcuts_vdf_error"] is False)

        # 3. valid file WITHOUT exec bit (the K1 wipe trigger)
        os.chmod(vdf_path, 0o644)
        mode_before = stat.S_IMODE(os.stat(vdf_path).st_mode)
        result = call()
        check("non-executable file still parsed (no wipe)", result is True)
        check("non-executable file byte-identical", open(vdf_path, "rb").read() == before)
        check(
            "mode unchanged (no chmod)",
            stat.S_IMODE(os.stat(vdf_path).st_mode) == mode_before,
        )

        # 4. corrupt file -> ShortcutsVdfError, no modification
        corrupt = b"\x07this is not a binary vdf"
        with open(vdf_path, "wb") as f:
            f.write(corrupt)
        g["_shortcuts_vdf_error"] = False
        raised = False
        try:
            call()
        except Exception as e:
            raised = type(e).__name__ == "ShortcutsVdfError"
        check("corrupt file raises ShortcutsVdfError", raised)
        check("corrupt file untouched", open(vdf_path, "rb").read() == corrupt)
        check("abort flag set on corrupt file", g["_shortcuts_vdf_error"] is True)

        # 5. parseable file without 'shortcuts' key
        write_vdf(vdf_path, {"notshortcuts": {}})
        wrong = open(vdf_path, "rb").read()
        g["_shortcuts_vdf_error"] = False
        raised = False
        try:
            call()
        except Exception as e:
            raised = type(e).__name__ == "ShortcutsVdfError"
        check("missing 'shortcuts' key raises ShortcutsVdfError", raised)
        check("wrong-structure file untouched", open(vdf_path, "rb").read() == wrong)

        # 6. missing file -> False, not created
        os.remove(vdf_path)
        g["_shortcuts_vdf_error"] = False
        result = call()
        check("missing file -> False", result is False)
        check("missing file not created", not os.path.exists(vdf_path))
        check("no abort flag on missing file", g["_shortcuts_vdf_error"] is False)

        # 7. create_empty_shortcuts_vdf creates a valid empty vdf
        ge = load_env_vars_env()
        ge["create_empty_shortcuts_vdf"](vdf_path)
        check("empty vdf created", os.path.exists(vdf_path))
        with open(vdf_path, "rb") as f:
            check("created vdf parses to empty dict", vdf.binary_loads(f.read()) == {"shortcuts": {}})
        check(
            "created vdf has exec bit",
            stat.S_IMODE(os.stat(vdf_path).st_mode) & stat.S_IXUSR != 0,
        )
        leftovers = [f for f in os.listdir(config_dir) if f.startswith(".shortcuts.vdf.")]
        check("no temp leftovers after create", leftovers == [], str(leftovers))

        # 8. create_empty_shortcuts_vdf never replaces an existing file
        write_vdf(vdf_path, {"shortcuts": {"0": ENTRY}})
        before = open(vdf_path, "rb").read()
        ge["create_empty_shortcuts_vdf"](vdf_path)
        check("existing file not replaced", open(vdf_path, "rb").read() == before)
        leftovers = [f for f in os.listdir(config_dir) if f.startswith(".shortcuts.vdf.")]
        check("no temp leftovers after race-create", leftovers == [], str(leftovers))
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    run_tests()
    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("All K1 tests passed.")
    sys.exit(0)
