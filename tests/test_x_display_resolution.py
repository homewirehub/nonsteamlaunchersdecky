"""Tests for resolving the X display an install has to draw on.

The bug this pins: install() hardcoded

    XAUTHORITY = ~/.Xauthority

That file does not exist in a gamescope session, so `xhost` and
`xterm -e <script>` both failed with "Can't open display: :0" and the
install died with exit code 1 about thirty milliseconds after the click -
before the installer script had run a single line. Nothing was downloaded,
nothing was installed, and the only trace was an exit code.

Static (ast): install() no longer names an auth file, and the terminal is
gated on a display that actually answered.

Behavioral (ast extraction, as in the K1 tests): the candidate order and
the probe, against a stubbed subprocess - no X server is contacted here.
"""

import ast
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(REPO, "main.py")

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


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


class DeckyStub:
    class logger:
        @staticmethod
        def info(*a, **k):
            pass

        @staticmethod
        def warning(*a, **k):
            pass

        @staticmethod
        def error(*a, **k):
            pass


def load(answers):
    """Load the helpers with a subprocess whose xhost answers per auth file.

    answers maps an XAUTHORITY value to the return code xhost would give.
    Anything not named answers 1, the way a wrong cookie does.
    """
    calls = []

    class FakeCompleted:
        def __init__(self, returncode):
            self.returncode = returncode

    class FakeSubprocess:
        DEVNULL = subprocess.DEVNULL
        SubprocessError = subprocess.SubprocessError

        @staticmethod
        def run(argv, env=None, **kwargs):
            calls.append((tuple(argv), (env or {}).get("XAUTHORITY")))
            if answers == "raise":
                raise OSError("xhost is not installed")
            return FakeCompleted(answers.get((env or {}).get("XAUTHORITY"), 1))

    g = {
        "os": os,
        "glob": __import__("glob"),
        "subprocess": FakeSubprocess,
        "decky_plugin": DeckyStub,
    }
    exec(extract(MAIN, {"x_authority_candidates", "resolve_x_display"}), g)
    return g, calls


def test_candidate_order():
    with tempfile.TemporaryDirectory() as tmp:
        runtime = os.path.join(tmp, "run")
        home = os.path.join(tmp, "home")
        os.makedirs(runtime)
        os.makedirs(home)
        session = os.path.join(runtime, "xauth_abc123")
        stale = os.path.join(runtime, "xauth_old000")
        home_auth = os.path.join(home, ".Xauthority")
        for path in (stale, session, home_auth):
            open(path, "w").close()
        os.utime(stale, (1, 1))
        os.utime(session, (2, 2))

        g, _ = load({})
        env = {"XDG_RUNTIME_DIR": runtime, "HOME": home}
        found = g["x_authority_candidates"](env)
        check("the session's own auth file comes before ~/.Xauthority",
              found.index(session) < found.index(home_auth), str(found))
        check("the newer runtime cookie is tried before the stale one",
              found.index(session) < found.index(stale), str(found))
        check("~/.Xauthority is kept as a last resort, not dropped",
              home_auth in found, str(found))

        env_named = dict(env, XAUTHORITY=home_auth)
        check("an XAUTHORITY already in the environment is tried first",
              g["x_authority_candidates"](env_named)[0] == home_auth)

        missing = os.path.join(tmp, "gone")
        check("a path that does not exist is never offered",
              missing not in g["x_authority_candidates"](dict(env, XAUTHORITY=missing)))


def test_probe_picks_the_file_that_answers():
    with tempfile.TemporaryDirectory() as tmp:
        runtime = os.path.join(tmp, "run")
        home = os.path.join(tmp, "home")
        os.makedirs(runtime)
        os.makedirs(home)
        session = os.path.join(runtime, "xauth_abc123")
        home_auth = os.path.join(home, ".Xauthority")
        for path in (session, home_auth):
            open(path, "w").close()

        g, calls = load({session: 0})
        env = {"XDG_RUNTIME_DIR": runtime, "HOME": home, "DISPLAY": ":0"}
        check("a display that answers is reported as ready", g["resolve_x_display"](env) is True)
        check("the environment carries the file that answered",
              env.get("XAUTHORITY") == session, env.get("XAUTHORITY"))
        check("the probe is xhost, not the installer",
              all(c[0][0] == "xhost" for c in calls), str(calls))

        g, calls = load({})
        env = {"XDG_RUNTIME_DIR": runtime, "HOME": home, "DISPLAY": ":0"}
        check("no answer anywhere is reported, not guessed",
              g["resolve_x_display"](env) is False)
        check("every candidate was tried before giving up", len(calls) >= 2, str(calls))
        check("a failed probe leaves no auth file behind in the environment",
              "XAUTHORITY" not in env)

        g, _ = load("raise")
        env = {"XDG_RUNTIME_DIR": runtime, "HOME": home, "DISPLAY": ":0"}
        check("a missing xhost is a no, not a crash", g["resolve_x_display"](env) is False)


def test_install_no_longer_guesses():
    with open(MAIN) as f:
        source = f.read()
    tree = ast.parse(source)
    install = [n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "install"][0]
    body = ast.dump(install)

    check("install() no longer names an auth file",
          ".Xauthority" not in body)
    display_reads = [n for n in ast.walk(install)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
                     and any(isinstance(a, ast.Constant) and a.value == "DISPLAY"
                             for a in n.args)]
    check("install() takes DISPLAY from the session before falling back to :0",
          len(display_reads) == 1, str(len(display_reads)))
    check("install() resolves the display through the helper",
          any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "resolve_x_display" for n in ast.walk(install)))

    xterm_calls = [n for n in ast.walk(install)
                   if isinstance(n, ast.JoinedStr)
                   and any(isinstance(v, ast.Constant) and "xterm" in str(v.value)
                           for v in n.values)]
    check("the xterm command still exists", len(xterm_calls) == 1, str(len(xterm_calls)))

    guards = [n for n in ast.walk(install)
              if isinstance(n, ast.If)
              and any(isinstance(x, ast.Name) and x.id == "display_ready"
                      for x in ast.walk(n.test))]
    check("xterm and the xhost pair are gated on a display that answered",
          len(guards) >= 3, str(len(guards)))


if __name__ == "__main__":
    test_candidate_order()
    test_probe_picks_the_file_that_answers()
    test_install_no_longer_guesses()
    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("All X display tests passed.")
    sys.exit(0)
