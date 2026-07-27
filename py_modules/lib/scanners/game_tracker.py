import os
import sys
import json
import tempfile
from datetime import datetime
import subprocess
import time
import decky_plugin
from decky_plugin import DECKY_PLUGIN_DIR, DECKY_USER_HOME
import vdf
import re


def normalize_appname(name):
    if not name:
        return ""
    return name.strip().lower()


def desktop_match_key(name):
    """Comparison key for .desktop file names.

    Characters that are invalid in file names are stripped on creation: a
    game name containing a colon turns into a file without it, so
    'Title: Subtitle' becomes 'Title Subtitle.desktop'. Until 2026-07-27 the
    lookup compared the raw name with a plain lower(), therefore never
    matched and left the dead entry behind (logged twice as
    'No .desktop file found').
    Stripping everything except a-z0-9 on both sides makes the comparison
    independent of that sanitisation.
    """
    return re.sub(r'[^a-z0-9]+', '', (name or "").lower())


def desktop_name_matches(filename, wanted_key):
    """True if the .desktop file belongs to the requested game name."""
    if not filename.lower().endswith(".desktop"):
        return False
    return desktop_match_key(filename[:-len(".desktop")]) == wanted_key


def get_steamid3(DECKY_USER_HOME, decky_plugin):
    paths = [
        f"{DECKY_USER_HOME}/.steam/root/config/loginusers.vdf",
        f"{DECKY_USER_HOME}/.local/share/Steam/config/loginusers.vdf"
    ]

    # Find the first existing loginusers.vdf file
    file_path = next((p for p in paths if os.path.isfile(p)), None)
    if not file_path:
        decky_plugin.logger.error("loginusers.vdf not found in expected locations.")
        return None

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Regex to find steamid blocks (steamID 17-digit followed by {...})
        users = re.findall(r'"(\d{17})"\s*{([^}]+)}', content, re.DOTALL)

        max_timestamp = 0
        current_steamid = None

        for steamid, block in users:
            account_match = re.search(r'"AccountName"\s+"([^"]+)"', block)
            timestamp_match = re.search(r'"Timestamp"\s+"(\d+)"', block)

            if account_match and timestamp_match:
                timestamp = int(timestamp_match.group(1))

                if timestamp > max_timestamp:
                    max_timestamp = timestamp
                    current_steamid = steamid

        if current_steamid:
            decky_plugin.logger.info(f"SteamID64 found: {current_steamid}")
            # Convert SteamID64 to SteamID3 (as int)
            steamid3 = int(current_steamid) - 76561197960265728
            userdata_path = f"{DECKY_USER_HOME}/.steam/root/userdata/{steamid3}"

            if os.path.isdir(userdata_path):
                decky_plugin.logger.info(f"Found userdata folder for SteamID3 {steamid3}: {userdata_path}")
            else:
                decky_plugin.logger.warning(f"Userdata folder does not exist for SteamID3 {steamid3}: {userdata_path}")

            return steamid3

        decky_plugin.logger.error("No valid SteamID found in loginusers.vdf.")
        return None

    except Exception as e:
        decky_plugin.logger.error(f"Failed to process SteamID: {e}")
        return None


def get_shortcuts_path():
    steamid3 = get_steamid3(DECKY_USER_HOME, decky_plugin)
    if steamid3 is None:
        decky_plugin.logger.error("steamid3 is not initialized yet!")
        return None
    return f"{DECKY_USER_HOME}/.steam/root/userdata/{steamid3}/config/shortcuts.vdf"


def get_installed_apps_path():
    return f"{DECKY_USER_HOME}/.config/systemd/user/installedapps.json"


def get_removal_counters_path():
    return f"{DECKY_USER_HOME}/.config/systemd/user/nsl_removal_counters.json"


# AUDIT M25 (ported): a game must be missing for this many consecutive
# countable scan cycles before it is treated as removed.
REMOVAL_MISS_THRESHOLD = 3


def load_removal_counters():
    # AUDIT M25 (ported): a corrupt or missing counter file must never count
    # as "threshold reached" - reset to 0 and recount. A missed removal is
    # harmless, a wrong removal is not.
    try:
        with open(get_removal_counters_path(), "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("Expected dictionary.")
        return {
            launcher: {app: int(count) for app, count in apps.items()}
            for launcher, apps in data.items() if isinstance(apps, dict)
        }
    except FileNotFoundError:
        return {}
    except Exception as e:
        decky_plugin.logger.warning(f"Removal counter file unreadable ({e}); resetting all counters.")
        return {}


def save_removal_counters(counters):
    # Same atomic write pattern as the K1 fix (tempfile + fsync + os.replace).
    counters_path = get_removal_counters_path()
    os.makedirs(os.path.dirname(counters_path), exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix='.nsl_removal_counters.', dir=os.path.dirname(counters_path))
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(counters, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, counters_path)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


_current_scan = {}
_master_list = {}
_previous_master_list = {}  # To track previous scan state


def clear_current_scan():
    _current_scan.clear()


def load_master_list():
    global _master_list, _previous_master_list
    installed_apps_path = get_installed_apps_path()

    if os.path.exists(installed_apps_path):
        try:
            with open(installed_apps_path, "r") as f:
                master_list_raw = json.load(f)
                if not isinstance(master_list_raw, dict):
                    raise ValueError("Master list JSON format is incorrect! Expected dictionary.")
                _master_list = master_list_raw
                _previous_master_list = json.loads(json.dumps(_master_list))  # Deep copy
        except Exception as e:
            decky_plugin.logger.error(f"Failed to load master list: {e}")
            _master_list = {}
            _previous_master_list = {}
    else:
        _master_list = {}
        _previous_master_list = {}



def track_game(appname, launcher):
    now = datetime.utcnow().isoformat() + "Z"

    if launcher not in _current_scan:
        _current_scan[launcher] = {}

    _current_scan[launcher][appname] = {  # Keeping original name as key
        "first_seen": _master_list.get(launcher, {}).get(appname, {}).get("first_seen", now),
        "last_seen": now,
        "still_installed": True
    }




def load_shortcuts_appid_map():
    shortcuts_path = get_shortcuts_path()
    if not shortcuts_path or not os.path.isfile(shortcuts_path):
        decky_plugin.logger.warning("shortcuts.vdf not found!")
        return {}

    try:
        with open(shortcuts_path, "rb") as f:
            data = vdf.binary_load(f)

        shortcuts = data.get("shortcuts", data)
        appid_map = {}

        for key, entry in shortcuts.items():
            appname = entry.get("AppName") or entry.get("appname")
            appid = entry.get("appid") or entry.get("AppID")
            if appname and appid:
                norm_name = normalize_appname(appname)
                appid_map[norm_name] = appid

        return appid_map

    except Exception as e:
        decky_plugin.logger.error(f"Failed to load shortcuts.vdf: {e}")
        return {}



def uninstall_removed_apps(removed_appnames, appid_map):
    for appname in removed_appnames:
        norm_name = normalize_appname(appname)
        appid = appid_map.get(norm_name)

        if not appid:
            decky_plugin.logger.warning(f"AppID not found for removed app '{appname}'")
            continue

        decky_plugin.logger.info(f"Attempting to uninstall '{appname}' with AppID {appid} ...")

        uninstall_uri = f"steam://uninstall/{appid}"

        env = os.environ.copy()
        env.update({
            'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
            'LD_LIBRARY_PATH': '/usr/lib:/lib:/usr/lib32:/lib32',
        })

        commands_to_try = [
            ["steam", uninstall_uri]
        ]

        success = False
        for cmd in commands_to_try:
            try:
                subprocess.run(cmd, check=True, env=env)
                decky_plugin.logger.info(f"Successfully ran uninstall command: {' '.join(cmd)}")
                success = True
                time.sleep(2)
                break
            except subprocess.CalledProcessError as e:
                decky_plugin.logger.warning(f"Failed command: {' '.join(cmd)} | Error: {e}")
            except FileNotFoundError:
                decky_plugin.logger.warning(f"Command not found: {cmd[0]}")

        if not success:
            decky_plugin.logger.error(
                f"All uninstall attempts failed for '{appname}' (AppID {appid}). "
                "Manual removal may be needed."
            )


    desktop_dir = os.path.join(DECKY_USER_HOME, "Desktop")
    applications_dir = os.path.join(DECKY_USER_HOME, '.local', 'share', 'applications')

    try:
        desktop_files = os.listdir(desktop_dir)
        applications_files = os.listdir(applications_dir)
    except Exception as e:
        decky_plugin.logger.error(f"Failed to list Desktop or Applications directory: {e}")
        return

    for game_name in removed_appnames:
        base_game_name = game_name.split(' (')[0].strip()
        wanted_key = desktop_match_key(base_game_name)
        if not wanted_key:
            decky_plugin.logger.warning(f"No usable .desktop name for removed game: {game_name}")
            continue

        found_path = None
        for f in desktop_files:
            if desktop_name_matches(f, wanted_key):
                found_path = os.path.join(desktop_dir, f)
                break

        if found_path:
            try:
                os.remove(found_path)
                decky_plugin.logger.info(f"Deleted the .desktop file from Desktop for removed game: {game_name}")
            except Exception as e:
                decky_plugin.logger.error(f"Failed to delete .desktop file from Desktop for {game_name}: {e}")
        else:
            decky_plugin.logger.warning(f"No .desktop file found on Desktop for removed game: {game_name}")

        found_path = None
        for f in applications_files:
            if desktop_name_matches(f, wanted_key):
                found_path = os.path.join(applications_dir, f)
                break

        if found_path:
            try:
                os.remove(found_path)
                decky_plugin.logger.info(f"Deleted the .desktop file from Applications for removed game: {game_name}")
            except Exception as e:
                decky_plugin.logger.error(f"Failed to delete .desktop file from Applications for {game_name}: {e}")
        else:
            decky_plugin.logger.warning(f"No .desktop file found in Applications for removed game: {game_name}")


def finalize_game_tracking():
    now = datetime.utcnow().isoformat() + "Z"
    installed_apps_path = get_installed_apps_path()

    removed_apps = {}
    counters = load_removal_counters()
    new_counters = {}

    for launcher in list(_master_list.keys()):
        # AUDIT M25 (ported): a launcher without any scan data this cycle
        # means its data source was unavailable (or the scan came up empty),
        # not that every one of its games was uninstalled at once. Since all
        # games are tracked under the single "Launcher" key, this branch is
        # also what keeps an empty scan from removing everything at once.
        # Do not count this cycle; carry existing counters over unchanged.
        if launcher not in _current_scan or not _current_scan[launcher]:
            if launcher in counters:
                new_counters[launcher] = counters[launcher]
            decky_plugin.logger.info(f"Launcher '{launcher}' yielded no scan data this cycle; skipping removal detection for it.")
            continue

        # Check missing games within the launcher
        for appname in list(_master_list[launcher].keys()):
            if appname in _current_scan[launcher]:
                continue  # still installed; any previous miss counter resets
            if not _master_list[launcher][appname].get("still_installed", True):
                continue  # already handled as removed in an earlier cycle
            miss_count = counters.get(launcher, {}).get(appname, 0) + 1
            if miss_count >= REMOVAL_MISS_THRESHOLD:
                was_installed_before = _previous_master_list.get(launcher, {}).get(appname, {}).get("still_installed", True)
                if was_installed_before:
                    if launcher not in removed_apps:
                        removed_apps[launcher] = []
                    removed_apps[launcher].append(appname)

                _master_list[launcher][appname]["still_installed"] = False
                _master_list[launcher][appname]["last_seen"] = now
            else:
                new_counters.setdefault(launcher, {})[appname] = miss_count
                decky_plugin.logger.info(f"'{appname}' ({launcher}) missing for {miss_count}/{REMOVAL_MISS_THRESHOLD} cycles; not removing yet.")

    # Merge updated scan data
    for launcher, games in _current_scan.items():
        if launcher not in _master_list:
            _master_list[launcher] = {}
        _master_list[launcher].update(games)

    # Only write when a counter actually changed - with autoscan enabled
    # this would otherwise be one flash write every scan cycle.
    if new_counters != counters:
        save_removal_counters(new_counters)

    # Helper to strip volatile fields for comparison
    def cleaned(data):
        if isinstance(data, dict):
            return {k: cleaned(v) for k, v in data.items() if k != "last_seen"}
        elif isinstance(data, list):
            return [cleaned(i) for i in data]
        else:
            return data

    # Only write to file if there are meaningful changes
    if cleaned(_master_list) != cleaned(_previous_master_list):
        os.makedirs(os.path.dirname(installed_apps_path), exist_ok=True)
        with open(installed_apps_path, "w") as f:
            json.dump(_master_list, f, indent=4)
        decky_plugin.logger.info("Master list updated and saved.")
    else:
        decky_plugin.logger.info("No meaningful changes to master list. Skipping write.")

    # Handle removed apps
    if removed_apps:
        decky_plugin.logger.info(f"Newly removed apps detected: {removed_apps}")
        appid_map = load_shortcuts_appid_map()
        for launcher, apps in removed_apps.items():
            uninstall_removed_apps(apps, appid_map)
    else:
        decky_plugin.logger.info("No newly removed apps detected.")

    return removed_apps


# Optional helper for debugging shortcut contents:
def debug_print_shortcuts():
    shortcuts_path = get_shortcuts_path()
    if not shortcuts_path or not os.path.isfile(shortcuts_path):
        decky_plugin.logger.warning("shortcuts.vdf not found!")
        return

    with open(shortcuts_path, "rb") as f:
        data = vdf.binary_load(f)

    shortcuts = data.get("shortcuts", data)
    for key, entry in shortcuts.items():
        appname = entry.get("AppName") or entry.get("appname") or "<No AppName>"
        appid = entry.get("appid") or entry.get("AppID") or "<No AppID>"
        decky_plugin.logger.info(f"Shortcut index: {key} | AppName: {appname} | AppID: {appid}")


# --- Deliberate launcher uninstall support --------------------------------
#
# Resolving and purging is driven by the plugin's install(operation=
# "Uninstall") path. Shortcut removal itself happens in the frontend via
# SteamClient.Apps.RemoveShortcut (Steam persists that change itself, so
# shortcuts.vdf is never written from here).

# Every NSL-managed launcher lives either in the shared prefix or in its
# own separate-app-id prefix; flatpak-based entries carry the flatpak id
# in their shortcut instead. A shortcut only qualifies for removal if its
# Exe/StartDir/LaunchOptions contain one of these markers - a same-named
# shortcut created by the user is never touched.
NSL_SHARED_PREFIX_MARKER = "/steamapps/compatdata/nonsteamlaunchers"

LAUNCHER_SHORTCUT_MARKERS = {
    "epic games": ["/steamapps/compatdata/epicgameslauncher"],
    "gog galaxy": ["/steamapps/compatdata/goggalaxylauncher"],
    "ubisoft connect": ["/steamapps/compatdata/uplaylauncher"],
    "battle.net": ["/steamapps/compatdata/battle.netlauncher"],
    "amazon games": ["/steamapps/compatdata/amazongameslauncher"],
    "ea app": ["/steamapps/compatdata/theeaapplauncher"],
    "legacy games": ["/steamapps/compatdata/legacygameslauncher"],
    "itch.io": ["/steamapps/compatdata/itchiolauncher"],
    "humble games collection": ["/steamapps/compatdata/humblegameslauncher"],
    "indiegala": ["/steamapps/compatdata/indiegalalauncher"],
    "rockstar games launcher": ["/steamapps/compatdata/rockstargameslauncher"],
    "glyph launcher": ["/steamapps/compatdata/glyphlauncher"],
    "minecraft launcher": ["/steamapps/compatdata/minecraftlauncher"],
    "playstation plus": ["/steamapps/compatdata/playstationpluslauncher"],
    "vk play": ["/steamapps/compatdata/vkplaylauncher"],
    "hoyoplay": ["/steamapps/compatdata/hoyoplaylauncher"],
    "nexon launcher": ["/steamapps/compatdata/nexonlauncher"],
    "game jolt client": ["/steamapps/compatdata/gamejoltlauncher"],
    "artix game launcher": ["/steamapps/compatdata/artixgamelauncher"],
    "arc launcher": ["/steamapps/compatdata/arclauncher"],
    "pokémon trading card game live": ["/steamapps/compatdata/poketcglauncher"],
    "plarium play": ["/steamapps/compatdata/plariumlauncher"],
    "vfun launcher": ["/steamapps/compatdata/vfunlauncher"],
    "tempo launcher": ["/steamapps/compatdata/tempolauncher"],
    "antstream arcade": ["/steamapps/compatdata/antstreamlauncher"],
    "gryphlink": ["/steamapps/compatdata/gryphlinklauncher", "gryphlink"],
    "nvidia geforce now": ["com.nvidia.geforcenow"],
    "moonlight": ["com.moonlight_stream.moonlight"],
}


def get_shortcut_entries_for_names(names):
    """Resolve the NSL-managed Steam shortcuts for the given launcher
    display names from shortcuts.vdf. Exact (normalized) AppName match
    plus an NSL path marker in Exe/StartDir/LaunchOptions are both
    required. Read-only. Returns {requested_name: {"appid": <unsigned>,
    "appname": <shortcut AppName>}}.
    """
    result = {}
    shortcuts_path = get_shortcuts_path()
    if not shortcuts_path or not os.path.isfile(shortcuts_path):
        decky_plugin.logger.warning("get_shortcut_entries_for_names: shortcuts.vdf not found")
        return result

    try:
        with open(shortcuts_path, "rb") as f:
            data = vdf.binary_load(f)
    except Exception as e:
        decky_plugin.logger.error(f"get_shortcut_entries_for_names: cannot read shortcuts.vdf: {e}")
        return result

    shortcuts = data.get("shortcuts", data)
    wanted = {normalize_appname(n): n for n in names if n}

    for entry in shortcuts.values():
        if not isinstance(entry, dict):
            continue
        appname = entry.get("AppName") or entry.get("appname") or ""
        norm = normalize_appname(appname)
        if norm not in wanted:
            continue
        appid = entry.get("appid") or entry.get("AppID")
        if not isinstance(appid, int):
            decky_plugin.logger.warning(f"Shortcut '{appname}' has no usable appid; skipping.")
            continue

        haystack = " ".join(
            str(entry.get(k) or "")
            for k in ("Exe", "exe", "StartDir", "startdir", "LaunchOptions", "launchoptions")
        ).lower()
        markers = [NSL_SHARED_PREFIX_MARKER] + LAUNCHER_SHORTCUT_MARKERS.get(norm, [])
        if not any(m in haystack for m in markers):
            decky_plugin.logger.warning(
                f"Shortcut '{appname}' matches the name but no NSL-managed path; leaving it alone."
            )
            continue

        # vdf stores the appid as a signed int32; SteamClient expects the
        # unsigned value.
        result[wanted[norm]] = {"appid": appid & 0xFFFFFFFF, "appname": appname}

    return result


def _purge_launcher_from_master(master, normalized_names):
    """Drop a launcher's own per-game bucket and its entry under the
    shared "Launcher" bucket. Returns True if anything was removed."""
    changed = False
    for key in list(master.keys()):
        if normalize_appname(key) in normalized_names:
            del master[key]
            changed = True
    launcher_bucket = master.get("Launcher")
    if isinstance(launcher_bucket, dict):
        for item in list(launcher_bucket.keys()):
            if normalize_appname(item) in normalized_names:
                del launcher_bucket[item]
                changed = True
    return changed


def purge_tracking_for_launcher(launcher_name):
    """After a deliberate launcher uninstall, drop the launcher from the
    tracking state. The absent-launcher skip branch in
    finalize_game_tracking would otherwise carry a stale still_installed
    entry and a frozen miss counter forever. Cleans the on-disk files and
    the in-memory state of this scanner process; call with the scan lock
    held so no scan cycle runs concurrently.
    """
    normalized_names = {normalize_appname(launcher_name)}
    purged = False

    installed_apps_path = get_installed_apps_path()
    master = None
    try:
        with open(installed_apps_path, "r") as f:
            master = json.load(f)
        if not isinstance(master, dict):
            raise ValueError("Expected dictionary.")
    except FileNotFoundError:
        pass
    except Exception as e:
        decky_plugin.logger.warning(
            f"purge_tracking_for_launcher: installedapps.json unreadable ({e}); skipping file purge."
        )
        master = None

    if master is not None and _purge_launcher_from_master(master, normalized_names):
        # Same atomic write pattern as the K1 fix (tempfile + fsync + os.replace).
        dirname = os.path.dirname(installed_apps_path)
        fd, temp_path = tempfile.mkstemp(prefix='.installedapps.', dir=dirname)
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(master, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, installed_apps_path)
            purged = True
        except Exception:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise

    counters = load_removal_counters()
    counters_changed = False
    for launcher_key in list(counters.keys()):
        if normalize_appname(launcher_key) in normalized_names:
            del counters[launcher_key]
            counters_changed = True
            continue
        bucket = counters[launcher_key]
        for item in list(bucket.keys()):
            if normalize_appname(item) in normalized_names:
                del bucket[item]
                counters_changed = True
        if not bucket:
            del counters[launcher_key]
            counters_changed = True
    if counters_changed:
        save_removal_counters(counters)
        purged = True

    # The scanner holds the master list in module state between cycles;
    # purge it too so the next finalize cannot resurrect the entry.
    for state in (_master_list, _previous_master_list):
        if _purge_launcher_from_master(state, normalized_names):
            purged = True

    if purged:
        decky_plugin.logger.info(
            f"Purged tracking state for uninstalled launcher '{launcher_name}'."
        )
    return purged
