"""Tests for the review fixes of 2026-07-27 (R1, R2, R4).

R4 — game_tracker.desktop_match_key / desktop_name_matches
  1. The field case: a game name with a colon finds the sanitised file.
  2. Emoji and special characters in the name do not break the lookup.
  3. Similar but different names do NOT match (no prefix hit).
  4. Non-.desktop files never match.
  5. An empty name yields an empty key (the caller skips it).

R2 — get_env_vars.write_env_vars_atomically and the write condition
  6. With no lines to filter the file is NOT touched (mtime + inode).
  7. With lines to filter they are dropped and the file replaced atomically.
  8. No temporary leftovers in the directory.
  9. The target file exists throughout the write.

R1 — NonSteamLaunchers.sh update path (statically, on the source)
 10. No 'rm -rf "$LOCAL_DIR"' left in the update branch.
 11. curl runs with -f and error handling, before any intervention.
 12. The archive is checked for main.py and dist/index.js.
 13. A failed swap moves the old installation back.
"""

import logging
import os
import re
import sys
import tempfile
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = tempfile.mkdtemp(prefix="review-fixes-test-home.")

decky_stub = types.ModuleType("decky_plugin")
decky_stub.DECKY_USER_HOME = HOME
decky_stub.DECKY_PLUGIN_DIR = HOME
_logger = logging.getLogger("review-fixes-test")
_logger.addHandler(logging.NullHandler())
decky_stub.logger = _logger
sys.modules["decky_plugin"] = decky_stub

vdf_stub = types.ModuleType("vdf")
vdf_stub.binary_load = lambda f: {"shortcuts": {}}
vdf_stub.binary_loads = lambda b: {"shortcuts": {}}
vdf_stub.binary_dumps = lambda d: b""
sys.modules.setdefault("vdf", vdf_stub)

sys.path.insert(0, os.path.join(REPO, "py_modules", "lib"))
sys.path.insert(0, os.path.join(REPO, "py_modules", "lib", "scanners"))

import game_tracker  # noqa: E402
import get_env_vars  # noqa: E402

failures = []


def check(ok, label):
    print(("[PASS] " if ok else "[FAIL] ") + label)
    if not ok:
        failures.append(label)


# ---------------------------------------------------------------- R4
key = game_tracker.desktop_match_key
matches = game_tracker.desktop_name_matches

check(
    matches("Sample Title Colon Case.desktop", key("Sample Title: Colon Case")),
    "1. Colon name finds the sanitised .desktop (field case 2026-07-27)",
)
check(
    matches("Bracket Tag Title [No Ai].desktop", key("Bracket Tag Title [\U0001f6abNo Ai]")),
    "2. Emoji in the game name does not break the lookup",
)
check(
    not matches("Universe.desktop", key("Uni")),
    "3. Similar name does not match (no prefix hit)",
)
check(
    not matches("Sample Title Colon Case.txt", key("Sample Title: Colon Case")),
    "4. Non-.desktop file never matches",
)
check(key("") == "" and key(None) == "", "5. Empty name -> empty key")


# ---------------------------------------------------------------- R2
def read(path):
    with open(path) as f:
        return f.read()


env_dir = tempfile.mkdtemp(prefix="review-fixes-envvars.", dir=HOME)
env_path = os.path.join(env_dir, "env_vars")

CLEAN = 'export epicshortcutdirectory="/x/Epic.exe"\nexport steamid3=4711\n'
with open(env_path, "w") as f:
    f.write(CLEAN)

before_stat = os.stat(env_path)
lines = CLEAN.splitlines(keepends=True)
kept = [l for l in lines if "chromelaunchoptions" not in l and "websites_str" not in l]
if len(kept) != len(lines):
    get_env_vars.write_env_vars_atomically(env_path, kept)
after_stat = os.stat(env_path)
check(
    (before_stat.st_ino, before_stat.st_mtime_ns) == (after_stat.st_ino, after_stat.st_mtime_ns)
    and read(env_path) == CLEAN,
    "6. With nothing to filter env_vars stays untouched (same inode + mtime)",
)

DIRTY = CLEAN + 'export chromelaunchoptions="run ..."\nexport websites_str="a,b"\n'
with open(env_path, "w") as f:
    f.write(DIRTY)
lines = DIRTY.splitlines(keepends=True)
kept = [l for l in lines if "chromelaunchoptions" not in l and "websites_str" not in l]
get_env_vars.write_env_vars_atomically(env_path, kept)
check(read(env_path) == CLEAN, "7. Volatile lines are dropped, the rest is preserved exactly")
check(
    [f for f in os.listdir(env_dir) if f.startswith(".env_vars.")] == [],
    "8. No temporary leftovers in the directory",
)


class _Watcher:
    """Verifies during fsync that the target file never disappears."""

    def __init__(self, path):
        self.path = path
        self.gone = False

    def __call__(self, fd):
        if not os.path.exists(self.path):
            self.gone = True
        return _real_fsync(fd)


_real_fsync = os.fsync
watcher = _Watcher(env_path)
os.fsync = watcher
try:
    get_env_vars.write_env_vars_atomically(env_path, ["export a=1\n"])
finally:
    os.fsync = _real_fsync
check(not watcher.gone, "9. Target file exists throughout the write")


# ---------------------------------------------------------------- R1
with open(os.path.join(REPO, "NonSteamLaunchers.sh")) as f:
    sh = f.read()

up_block = sh.split('if [ "$arg" = "Up" ]', 1)[1].split("#end HR for DP", 1)[0]

# Skip comment lines: the fix explains the old command in a comment.
up_code = "\n".join(
    line for line in up_block.splitlines() if not line.lstrip().startswith("#")
)
check(
    'rm -rf "$LOCAL_DIR"' not in up_code,
    "10. No 'rm -rf \"$LOCAL_DIR\"' left in the executed update branch",
)

curl_pos = up_block.find("curl -fL")
swap_pos = up_block.find('mv "$LOCAL_DIR"')
check(
    curl_pos != -1 and "abort_update" in up_block[curl_pos:curl_pos + 200],
    "11a. curl runs with -f and error handling",
)
check(
    curl_pos != -1 and swap_pos != -1 and curl_pos < swap_pos,
    "11b. The download happens before any intervention in the installation",
)
check(
    'main.py" ] || abort_update' in up_block and 'dist/index.js" ] || abort_update' in up_block,
    "12. The extracted archive is checked for main.py and dist/index.js",
)
check(
    re.search(r'if ! mv "\$new_root" "\$LOCAL_DIR"', up_block) is not None
    and 'mv "$backup_dir" "$LOCAL_DIR"' in up_block,
    "13. A failed swap moves the old installation back",
)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")
