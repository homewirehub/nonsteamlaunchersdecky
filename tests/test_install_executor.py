"""AST regression for the install() executor pattern (Install-UI Step 2).

Pins main.py to the shape introduced with "fix: run installer subprocess
in executor" - the install() counterpart to the scan wiring pinned by
test_event_loop_responsiveness.py:

  - install() never runs blocking subprocess work (Popen, process.wait,
    subprocess.run / run, the xhost calls) directly on the event loop;
    all of it lives in a nested sync worker function,
  - that worker is handed to run_in_executor by name,
  - the executor call is awaited under `async with self.scan_lock`, so
    an install can never overlap a scan or another install (the
    installer rewrites env_vars and the launcher prefixes that scan()
    re-reads on every run), and
  - the xhost + / xhost - pair sits in try/finally inside the worker,
    so X access control is restored even if the script cannot start.

The behavioral half of the pattern (loop responsiveness + strict
serialization under an asyncio.Lock held across run_in_executor) is
already proven generically by test_event_loop_responsiveness.py Part B.
"""

import ast
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(REPO, "main.py")

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def is_scan_lock_with(node):
    return isinstance(node, ast.AsyncWith) and any(
        isinstance(item.context_expr, ast.Attribute)
        and item.context_expr.attr == "scan_lock"
        for item in node.items
    )


def is_blocking_call(node):
    """Popen(...), run(...), subprocess.run(...), <x>.Popen(...), <x>.wait()."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Name) and f.id in {"Popen", "run"}:
        return True
    if isinstance(f, ast.Attribute) and f.attr in {"Popen", "run", "wait"}:
        return True
    return False


def is_run_in_executor_call(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run_in_executor"
    )


def walk_with_ancestors(node, ancestors=()):
    yield node, ancestors
    for child in ast.iter_child_nodes(node):
        yield from walk_with_ancestors(child, ancestors + (node,))


def calls_xhost(stmts):
    for stmt in stmts:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Call) and any(
                isinstance(a, ast.List)
                and any(
                    isinstance(e, ast.Constant) and e.value == "xhost"
                    for e in a.elts
                )
                for a in n.args
            ):
                return True
    return False


def test_install_wiring():
    with open(MAIN) as f:
        tree = ast.parse(f.read())

    installs = [
        n
        for n, _ in walk_with_ancestors(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "install"
    ]
    check("exactly one async install() found", len(installs) == 1, str(len(installs)))
    if len(installs) != 1:
        return
    install = installs[0]

    # Every blocking subprocess call inside install() must live in a nested
    # sync function (which only ever runs on an executor thread), never in
    # install()'s own async body.
    loop_level_blocking = [
        f"main.py:{n.lineno}"
        for n, anc in walk_with_ancestors(install)
        if is_blocking_call(n)
        and not any(isinstance(a, ast.FunctionDef) for a in anc if a is not install)
    ]
    check(
        "no blocking subprocess call on the event loop in install()",
        loop_level_blocking == [],
        str(loop_level_blocking),
    )

    # The worker: the nested sync function that waits on the subprocess.
    workers = [
        n
        for n, anc in walk_with_ancestors(install)
        if isinstance(n, ast.FunctionDef)
        and any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "wait"
            for c in ast.walk(n)
        )
    ]
    check("install() has a nested worker that waits on the process", len(workers) == 1, str(len(workers)))
    if len(workers) != 1:
        return
    worker = workers[0]

    executor_calls = [
        (n, anc) for n, anc in walk_with_ancestors(install) if is_run_in_executor_call(n)
    ]
    check("install() uses run_in_executor", len(executor_calls) >= 1)
    check(
        "run_in_executor is passed the worker by name",
        any(
            any(isinstance(a, ast.Name) and a.id == worker.name for a in n.args)
            for n, _ in executor_calls
        ),
    )
    check(
        "install() holds scan_lock around every executor call",
        all(any(is_scan_lock_with(a) for a in anc) for _, anc in executor_calls),
    )

    xhost_finally = [
        n
        for n in ast.walk(worker)
        if isinstance(n, ast.Try) and n.finalbody and calls_xhost(n.finalbody)
    ]
    check("worker re-enables xhost in a finally block", len(xhost_finally) >= 1)


if __name__ == "__main__":
    test_install_wiring()
    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("All install-executor tests passed.")
    sys.exit(0)
