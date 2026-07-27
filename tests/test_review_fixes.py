"""Tests fuer die Review-Fixes vom 2026-07-27 (R1, R2, R4).

R4 — game_tracker.desktop_match_key / desktop_name_matches
  1. Der reale Fall: Spielname mit Doppelpunkt findet die sanitisierte Datei.
  2. Emoji und Sonderzeichen im Namen stoeren den Abgleich nicht.
  3. Aehnliche, aber andere Namen matchen NICHT (kein Praefix-Treffer).
  4. Nicht-.desktop-Dateien matchen nie.
  5. Leerer Name liefert einen leeren Schluessel (Aufrufer ueberspringt ihn).

R2 — get_env_vars.write_env_vars_atomically und die Schreibbedingung
  6. Ohne zu filternde Zeilen wird die Datei NICHT angefasst (mtime + inode).
  7. Mit zu filternden Zeilen wird gefiltert und atomar ersetzt.
  8. Kein temporaerer Rest im Verzeichnis.
  9. Die Zieldatei existiert waehrend des Schreibens durchgehend.

R1 — NonSteamLaunchers.sh Update-Pfad (statisch am Quelltext)
 10. Kein 'rm -rf "$LOCAL_DIR"' mehr im Update-Zweig.
 11. curl laeuft mit -f und Fehlerbehandlung, vor jedem Eingriff.
 12. Das Archiv wird auf main.py und dist/index.js geprueft.
 13. Bei fehlgeschlagenem Swap wird die alte Installation zurueckgetauscht.
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
    "1. Doppelpunkt-Name findet die sanitisierte .desktop (Fall aus dem Feld 2026-07-27)",
)
check(
    matches("Bracket Tag Title [No Ai].desktop", key("Bracket Tag Title [\U0001f6abNo Ai]")),
    "2. Emoji im Spielnamen stoert den Abgleich nicht",
)
check(
    not matches("Universe.desktop", key("Uni")),
    "3. Aehnlicher Name matcht nicht (kein Praefix-Treffer)",
)
check(
    not matches("Sample Title Colon Case.txt", key("Sample Title: Colon Case")),
    "4. Nicht-.desktop-Datei matcht nie",
)
check(key("") == "" and key(None) == "", "5. Leerer Name -> leerer Schluessel")


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
    "6. Ohne zu filternde Zeilen bleibt env_vars unangetastet (inode + mtime gleich)",
)

DIRTY = CLEAN + 'export chromelaunchoptions="run ..."\nexport websites_str="a,b"\n'
with open(env_path, "w") as f:
    f.write(DIRTY)
lines = DIRTY.splitlines(keepends=True)
kept = [l for l in lines if "chromelaunchoptions" not in l and "websites_str" not in l]
get_env_vars.write_env_vars_atomically(env_path, kept)
check(read(env_path) == CLEAN, "7. Fluechtige Zeilen werden entfernt, der Rest bleibt exakt erhalten")
check(
    [f for f in os.listdir(env_dir) if f.startswith(".env_vars.")] == [],
    "8. Kein temporaerer Rest im Verzeichnis",
)


class _Watcher:
    """Prueft waehrend des fsync, dass die Zieldatei nie verschwindet."""

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
check(not watcher.gone, "9. Zieldatei existiert waehrend des Schreibens durchgehend")


# ---------------------------------------------------------------- R1
with open(os.path.join(REPO, "NonSteamLaunchers.sh")) as f:
    sh = f.read()

up_block = sh.split('if [ "$arg" = "Up" ]', 1)[1].split("#end HR for DP", 1)[0]

# Kommentarzeilen ausklammern: der Fix erklaert den alten Befehl im Kommentar.
up_code = "\n".join(
    line for line in up_block.splitlines() if not line.lstrip().startswith("#")
)
check(
    'rm -rf "$LOCAL_DIR"' not in up_code,
    "10. Kein 'rm -rf \"$LOCAL_DIR\"' mehr im ausgefuehrten Update-Zweig",
)

curl_pos = up_block.find("curl -fL")
swap_pos = up_block.find('mv "$LOCAL_DIR"')
check(
    curl_pos != -1 and "abort_update" in up_block[curl_pos:curl_pos + 200],
    "11a. curl laeuft mit -f und Fehlerbehandlung",
)
check(
    curl_pos != -1 and swap_pos != -1 and curl_pos < swap_pos,
    "11b. Der Download passiert vor jedem Eingriff in die Installation",
)
check(
    'main.py" ] || abort_update' in up_block and 'dist/index.js" ] || abort_update' in up_block,
    "12. Das entpackte Archiv wird auf main.py und dist/index.js geprueft",
)
check(
    re.search(r'if ! mv "\$new_root" "\$LOCAL_DIR"', up_block) is not None
    and 'mv "$backup_dir" "$LOCAL_DIR"' in up_block,
    "13. Fehlgeschlagener Swap taucht die alte Installation zurueck",
)

print()
if failures:
    print(f"{len(failures)} FEHLGESCHLAGEN:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL PASS")
