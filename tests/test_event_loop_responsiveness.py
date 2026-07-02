"""Tests for the event-loop decoupling (NEXT-STEPS Commit 4).

Part A pins the real main.py to the pattern via ast:
  - handleScan / handleAutoScan / handleCustomSite never call scan() or
    addCustomSite() directly on the event loop; they go through
    run_in_executor, and every such call sits inside `async with
    self.scan_lock`.
  - handleAutoScan takes the lock per iteration (the AsyncWith is inside
    the while loop, not wrapped around it), so a manual scan cannot be
    starved while autoscan is enabled.

Part B drives the exact pattern (asyncio.Lock + run_in_executor around a
blocking scan) and proves the two properties the rebuild is for:
  - a status endpoint keeps answering while a scan blocks its worker thread
    (the /launcher_status hang this fixes), and
  - concurrent scan handlers never run scan() in parallel (max_active == 1).

The live WebSocket wiring on the Deck is verified manually after deploy.
"""

import ast
import asyncio
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(REPO, "main.py")

HANDLERS = {"handleScan", "handleAutoScan", "handleCustomSite"}
SCAN_FUNCS = {"scan", "addCustomSite"}

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


# ---------- Part A: ast wiring on the real main.py ----------

def is_scan_lock_with(node):
    return isinstance(node, ast.AsyncWith) and any(
        isinstance(item.context_expr, ast.Attribute)
        and item.context_expr.attr == "scan_lock"
        for item in node.items
    )


def is_executor_scan_call(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run_in_executor"
        and any(isinstance(a, ast.Name) and a.id in SCAN_FUNCS for a in node.args)
    )


def walk_with_ancestors(node, ancestors=()):
    yield node, ancestors
    for child in ast.iter_child_nodes(node):
        yield from walk_with_ancestors(child, ancestors + (node,))


def test_wiring():
    with open(MAIN) as f:
        tree = ast.parse(f.read())

    direct_calls = [
        f"main.py:{n.lineno}"
        for n, _ in walk_with_ancestors(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id in SCAN_FUNCS
    ]
    check("no direct scan()/addCustomSite() call on the loop", direct_calls == [], str(direct_calls))

    handlers = {
        n.name: n
        for n, _ in walk_with_ancestors(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name in HANDLERS
    }
    check("all three handlers found", set(handlers) == HANDLERS, str(set(handlers)))

    for name, fn in sorted(handlers.items()):
        executor_calls = [
            (n, anc) for n, anc in walk_with_ancestors(fn) if is_executor_scan_call(n)
        ]
        check(f"{name} uses run_in_executor for scan work", len(executor_calls) >= 1)
        check(
            f"{name} holds scan_lock around every executor scan call",
            all(any(is_scan_lock_with(a) for a in anc) for _, anc in executor_calls),
        )

    auto = handlers.get("handleAutoScan")
    if auto is not None:
        whiles = [(n, anc) for n, anc in walk_with_ancestors(auto) if isinstance(n, ast.While)]
        check("handleAutoScan has a while loop", len(whiles) == 1, str(len(whiles)))
        if len(whiles) == 1:
            loop_node, loop_ancestors = whiles[0]
            check(
                "autoscan while loop is NOT wrapped in scan_lock",
                not any(is_scan_lock_with(a) for a in loop_ancestors),
            )
            check(
                "autoscan takes scan_lock inside the loop (per iteration)",
                any(is_scan_lock_with(n) for n, _ in walk_with_ancestors(loop_node)),
            )


# ---------- Part B: behavioral pattern test ----------

SCAN_DURATION = 1.0


async def scenario():
    lock = asyncio.Lock()
    loop = asyncio.get_event_loop()
    state = {"active": 0, "max_active": 0}

    def fake_scan():
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(SCAN_DURATION)
        state["active"] -= 1
        return {}, {}

    async def scan_handler():
        async with lock:
            return await loop.run_in_executor(None, fake_scan)

    async def status_handler():
        return {"installedLaunchers": []}

    # responsiveness: the status endpoint answers while a scan is in flight
    scan_task = asyncio.ensure_future(scan_handler())
    await asyncio.sleep(0.2)
    latencies = []
    for _ in range(5):
        t0 = loop.time()
        await status_handler()
        latencies.append(loop.time() - t0)
        await asyncio.sleep(0.05)
    still_running = not scan_task.done()
    await scan_task
    check("status endpoint answered while scan was in flight", still_running)
    check(
        "status latency well below scan duration",
        max(latencies) < 0.5,
        f"max={max(latencies):.3f}s",
    )

    # seriality: concurrent handlers never run the scan in parallel
    state["max_active"] = 0
    t0 = loop.time()
    await asyncio.gather(scan_handler(), scan_handler(), scan_handler())
    elapsed = loop.time() - t0
    check("scans never overlap (max_active == 1)", state["max_active"] == 1, str(state["max_active"]))
    check(
        "three scans ran back to back, not in parallel",
        elapsed >= 2.8 * SCAN_DURATION,
        f"{elapsed:.2f}s",
    )


if __name__ == "__main__":
    test_wiring()
    asyncio.run(scenario())
    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("All event-loop tests passed.")
    sys.exit(0)
