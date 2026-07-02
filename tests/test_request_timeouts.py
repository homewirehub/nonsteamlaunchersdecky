"""Tests for the timeout hardening (NEXT-STEPS Commit 3).

Static: every requests.<verb>() call in main.py and py_modules/lib (vendored
externals excluded) carries an explicit timeout=, and no bare requests.get
reference is handed to run_in_executor (which would bypass any timeout).

Behavioral (via ast extraction, as in the K1 tests):
  - download_artwork degrades to None/(None, None) when the API call or the
    image download times out - it must never raise into the scan cycle.
  - The "icons" fallback path returns a 2-tuple even when downloads fail
    (callers unpack `icon, icon_path = ...`; the old code returned a single
    value from the icons_ico recursion and crashed the scanner with a
    TypeError precisely when downloads failed).
  - add_launchers still calls track_game when create_new_entry blows up -
    a network/artwork error must not count as "launcher missing" toward
    removal detection (AUDIT M25). A ShortcutsVdfError still propagates
    (AUDIT K1: the scan must abort).
"""

import ast
import logging
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCANNER = os.path.join(REPO, "py_modules", "lib", "scanner.py")

HTTP_VERBS = {"get", "post", "put", "head", "delete", "patch"}

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def make_decky_stub():
    logger = logging.getLogger("timeouttest")
    logger.addHandler(logging.NullHandler())
    return types.SimpleNamespace(logger=logger)


def source_files():
    yield os.path.join(REPO, "main.py")
    lib = os.path.join(REPO, "py_modules", "lib")
    for root, dirs, files in os.walk(lib):
        dirs[:] = [d for d in dirs if d != "externals"]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


def is_requests_attr(node, verbs=HTTP_VERBS):
    return (
        isinstance(node, ast.Attribute)
        and node.attr in verbs
        and isinstance(node.value, ast.Name)
        and node.value.id == "requests"
    )


def test_static():
    missing = []
    bare_refs = []
    for path in source_files():
        with open(path) as f:
            tree = ast.parse(f.read())
        rel = os.path.relpath(path, REPO)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if is_requests_attr(node.func):
                if not any(kw.arg == "timeout" for kw in node.keywords):
                    missing.append(f"{rel}:{node.lineno}")
            for arg in node.args:
                if is_requests_attr(arg):
                    bare_refs.append(f"{rel}:{node.lineno}")
    check("every requests call has an explicit timeout", missing == [], str(missing))
    check("no bare requests.<verb> passed as callback", bare_refs == [], str(bare_refs))


def extract(path, names):
    with open(path) as f:
        tree = ast.parse(f.read())
    nodes = [
        n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names
    ]
    assert len(nodes) == len(names), f"expected {names}, found {[n.name for n in nodes]}"
    return compile(ast.Module(body=nodes, type_ignores=[]), path, "exec")


class FakeRequestException(Exception):
    pass


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


def make_fake_requests(get_impl):
    return types.SimpleNamespace(
        get=get_impl,
        exceptions=types.SimpleNamespace(
            RequestException=FakeRequestException,
            Timeout=type("Timeout", (FakeRequestException,), {}),
        ),
    )


def load_download_artwork(get_impl):
    from base64 import b64encode
    g = {
        "os": os,
        "b64encode": b64encode,
        "decky_plugin": make_decky_stub(),
        "requests": make_fake_requests(get_impl),
        "proxy_url": "https://proxy.invalid/api",
        "HTTP_TIMEOUT": (5, 30),
        "HTTP_TIMEOUT_SLOW": (5, 90),
        "logged_in_home": "/tmp/nonexistent-home",
        "steamid3": "12345",
    }
    exec(extract(SCANNER, {"download_artwork"}), g)
    return g["download_artwork"]


def test_download_artwork():
    def timeout_everything(*a, **kw):
        raise FakeRequestException("simulated timeout")

    da = load_download_artwork(timeout_everything)
    check("API timeout (logos) -> None, no raise", da(42, "logos") is None)
    check("API timeout (icons) -> (None, None), no raise", da(42, "icons") == (None, None))

    def api_ok_image_get(url, **kw):
        if url.startswith("https://proxy.invalid/"):
            return FakeResponse({"data": [{"thumb": "https://cdn.invalid/thumb.png"}]})
        raise FakeRequestException("simulated timeout")

    da = load_download_artwork(api_ok_image_get)
    check("image timeout (logos) -> None, no raise", da(42, "logos") is None)
    result = da(42, "icons")
    check(
        "image timeout (icons) -> 2-tuple for unpacking",
        isinstance(result, tuple) and len(result) == 2,
        repr(result),
    )
    icon, icon_path = result
    check("image timeout (icons) -> (None, None)", icon is None and icon_path is None)

    da = load_download_artwork(
        lambda url, **kw: FakeResponse({"data": [{"no_thumb_key": True}]})
    )
    check("artwork entry without thumb -> skipped, None", da(42, "logos") is None)


def test_add_launchers_tracking():
    tracked = []

    def load_add_launchers(create_impl, vdf_error_cls):
        g = {
            "decky_plugin": make_decky_stub(),
            "env_vars": {
                "epicshortcutdirectory": "/fake/dir",
                "epiclaunchoptions": "opts",
                "epicstartingdir": "/fake",
            },
            "create_new_entry": create_impl,
            "track_game": lambda name, launcher: tracked.append((name, launcher)),
            "ShortcutsVdfError": vdf_error_cls,
        }
        exec(extract(SCANNER, {"add_launchers"}), g)
        return g["add_launchers"]

    class VdfError(Exception):
        pass

    def boom(*a, **kw):
        raise RuntimeError("simulated artwork timeout")

    add_launchers = load_add_launchers(boom, VdfError)
    tracked.clear()
    try:
        add_launchers()
        raised = False
    except Exception:
        raised = True
    check("create_new_entry crash does not propagate", not raised)
    check(
        "launcher still tracked despite crash",
        ("Epic Games", "Launcher") in tracked,
        str(tracked),
    )

    def boom_vdf(*a, **kw):
        raise VdfError("unreadable shortcuts.vdf")

    add_launchers = load_add_launchers(boom_vdf, VdfError)
    tracked.clear()
    try:
        add_launchers()
        raised = False
    except VdfError:
        raised = True
    check("ShortcutsVdfError still aborts (K1)", raised)


if __name__ == "__main__":
    test_static()
    test_download_artwork()
    test_add_launchers_tracking()
    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("All timeout tests passed.")
    sys.exit(0)
