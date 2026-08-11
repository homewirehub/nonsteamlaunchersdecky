"""Scanner for locally unpacked Windows games.

Unlike every other scanner in this folder there is no launcher database to
read here: the games are plain folders that some other tool (or the user)
unpacked. So the scanner has to decide for itself whether a folder is a
game, and it errs on the side of skipping.

Deliberate design decisions, please keep them:

* It scans the INSTALL folder (default ~/Games), never a download folder.
  The tracking in game_tracker treats "not seen this cycle" as "uninstalled"
  after REMOVAL_MISS_THRESHOLD cycles and then removes the shortcut. Pointed
  at a download folder - where archives legitimately come and go, and where
  an unpacker deletes them after use - that would delete the shortcuts it
  had just created. Pointed at the install folder the same mechanism is
  exactly right: the folder is gone, so the game is gone.

* A folder without a usable .exe is not a game. That keeps backup folders,
  save copies and half finished downloads out of the library.

* If the scan folder is missing or unreadable, nothing at all is tracked.
  finalize_game_tracking skips removal detection for a launcher that
  reported nothing this cycle, so an unmounted SD card cannot wipe the
  entries. Never "track" a partial result here.

* Disabled by default. The setting lives under "localGamesEnabled" in the
  plugin's config.json.
"""

import json
import os
import re

import decky_plugin
from scanners.game_tracker import track_game

LAUNCHER = "Local Games"

# Same layout markers the unpacker uses; purely informational, a folder is
# not rejected for having an unknown engine.
def detect_engine(rel_paths):
    low = [p.lower() for p in rel_paths]
    if any(p == "game" or p.startswith("game/") for p in low) and \
       any(p == "lib" or p.startswith("lib/") for p in low):
        return "Ren'Py"
    if any(re.match(r"^[^/]+_data(/|$)", p) for p in low) and \
       any(p.endswith("unityplayer.dll") for p in low):
        return "Unity"
    if any(p == "www" or p.startswith("www/") for p in low) or \
       any(p.endswith("nw.dll") for p in low):
        return "RPG Maker"
    return "unknown"


# Executables that are never the game itself.
EXCLUDE_PATTERNS = (
    re.compile(r"unitycrashhandler", re.I),
    re.compile(r"-32\.exe$", re.I),
    re.compile(r"(^|/)unins[^/]*\.exe$", re.I),
    re.compile(r"(^|/)(python|pythonw)\.exe$", re.I),
)
INSTALLER_RE = re.compile(r"(^|/)(setup|install|installer)[^/]*\.exe$", re.I)
LAUNCHER_RE = re.compile(r"launcher[^/]*\.exe$", re.I)

# A folder deeper than this is not walked - game folders are shallow, and a
# runaway walk on a big library costs a scan cycle.
MAX_DEPTH = 3
MAX_FILES = 4000


def _settings():
    try:
        path = os.path.join(decky_plugin.DECKY_PLUGIN_SETTINGS_DIR, "config.json")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        settings = data.get("settings")
        return settings if isinstance(settings, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        decky_plugin.logger.warning(f"Local Games: settings unreadable ({e}); treating as disabled.")
        return {}


def _collect(folder):
    """(relative path, size) for every file below folder, depth limited."""
    files = []
    base_depth = folder.rstrip("/").count("/")
    for root, dirs, names in os.walk(folder):
        if root.count("/") - base_depth >= MAX_DEPTH:
            dirs[:] = []
        for n in names:
            full = os.path.join(root, n)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            files.append((os.path.relpath(full, folder).replace("\\", "/"), size))
            if len(files) >= MAX_FILES:
                return files
    return files


def pick_main_exe(files):
    """Pick the game's executable. Returns (relative path, reason) or (None, reason).

    Ordering matches the unpacker: throw out uninstallers and helpers, prefer
    a real game executable over a launcher or installer, and among equals take
    the largest file - that is reliably the engine binary.
    """
    exes = [(p, s) for p, s in files if p.lower().endswith(".exe")]
    if not exes:
        return None, "no .exe in the folder"

    kept = [(p, s) for p, s in exes if not any(rx.search(p) for rx in EXCLUDE_PATTERNS)]
    if not kept:
        return None, "only helper executables (uninstaller and the like)"

    plain = [(p, s) for p, s in kept
             if not INSTALLER_RE.search(p) and not LAUNCHER_RE.search(p)]
    pool = plain or kept
    # Shallow beats deep, then larger beats smaller.
    pool.sort(key=lambda ps: (ps[0].count("/"), -ps[1]))
    return pool[0][0], "chosen from %d candidate(s)" % len(pool)


def local_games_scanner(logged_in_home, create_new_entry):
    settings = _settings()
    if not settings.get("localGamesEnabled"):
        decky_plugin.logger.info("Local Games scanner disabled; skipping.")
        return

    folder = (settings.get("localGamesPath") or "").strip() \
        or os.path.join(logged_in_home, "Games")
    folder = os.path.abspath(os.path.expanduser(folder))

    if not os.path.isdir(folder):
        # Nothing tracked on purpose: an absent folder must not be read as
        # "every game uninstalled".
        decky_plugin.logger.info(f"Local Games: folder {folder} does not exist; skipping.")
        return
    try:
        entries = sorted(os.listdir(folder))
    except OSError as e:
        decky_plugin.logger.warning(f"Local Games: cannot read {folder} ({e}); skipping.")
        return

    decky_plugin.logger.info(f"Local Games: scanning {folder}")
    found = 0
    for name in entries:
        if name.startswith(".") or name.startswith("_"):
            continue                      # _tools and friends are not games
        game_dir = os.path.join(folder, name)
        if not os.path.isdir(game_dir):
            continue

        try:
            files = _collect(game_dir)
        except OSError as e:
            decky_plugin.logger.warning(f"Local Games: cannot read {game_dir} ({e}); skipping.")
            continue

        rel_exe, reason = pick_main_exe(files)
        if not rel_exe:
            decky_plugin.logger.info(f"Local Games: '{name}' is not a game ({reason}); skipping.")
            continue

        exe_path = os.path.join(game_dir, rel_exe)
        start_dir = os.path.dirname(exe_path)
        engine = detect_engine([p for p, _ in files])
        decky_plugin.logger.info(
            f"Local Games: '{name}' -> {rel_exe} ({engine}, {reason})"
        )

        # Quoting matches the other scanners; create_new_entry wraps the
        # executable for umu/Proton itself.
        create_new_entry(f'"{exe_path}"', name, "", f'"{start_dir}/"', LAUNCHER)
        # Tracked under the folder name, which is what the shortcut is called.
        track_game(name, LAUNCHER)
        found += 1

    decky_plugin.logger.info(f"Local Games: {found} game(s) found in {folder}.")
