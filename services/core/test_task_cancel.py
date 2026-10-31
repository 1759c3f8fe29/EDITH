#!/usr/bin/env python3
"""
Part (c) of task #70: start a multi-step task, watch the HUD track it,
press Stop, then confirm the run really halted (GET /api/tasks/{id}
shows cancelled and no further tool.started events arrive).
"""
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8787"
WS_URL = "ws://127.0.0.1:8787/ws/events?after_seq=0"

# Event types we care about
TOOL_STARTED = "tool.started"
TASK_STARTED = "task.started"
TASK_CANCELLED = "task.cancelled"
TASK_FAILED = "task.failed"
TASK_COMPLETED = "task.completed"

async def main():
    # 1. Create a multi-step task
    objective = (
        "Open a browser session and navigate to example.com. Then read the full page "
        "content and extract all text. Then query the page for all links. Then take a "
        "screenshot of the page. Then create a PDF document summarizing everything you "
        "found. Then close the browser. Do each of these as separate steps using the "
        "browser tools and document tools."
    )

    print(f"[+] Creating task: {objective[:80]}...")
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(f"{BASE}/api/tasks", json={"objective": objective, "run": False})
        r.raise_for_status()
        resp = r.json()
        task = resp.get("task", resp)
        task_id = task["id"]
        print(f"[+] Created task {task_id}, status: {task['status']}")

        # 2. Start the task
        print(f"[+] Starting task {task_id}...")
        r = await client.post(f"{BASE}/api/tasks/{task_id}/run")
        r.raise_for_status()
        print(f"[+] Task started")

    # 3. Watch WebSocket for events
    tool_started_count = 0
    tool_started_after_cancel = 0
    cancelled_received = False
    task_status_after_cancel = None
    cancel_sent_at = None
    ws_connected = False

    # We'll use websockets library if available, otherwise fall back to manual
    try:
        import websockets
        has_websockets = True
    except ImportError:
        has_websockets = False
        print("[!] websockets not installed, will poll REST instead")

    if has_websockets:
        async with websockets.connect(WS_URL) as ws:
            ws_connected = True
            print(f"[+] WebSocket connected")

            cancel_acked_at = None

            async def recv_loop():
                nonlocal tool_started_count, tool_started_after_cancel, cancelled_received, cancel_acked_at
                try:
                    async for msg in ws:
                        evt = json.loads(msg)
                        etype = evt.get("type", "")
                        if etype == TOOL_STARTED:
                            tool_started_count += 1
                            tool = evt.get("tool", "?")
                            print(f"    [ws] tool.started #{tool_started_count}: {tool}")
                            if cancel_acked_at and time.time() > cancel_acked_at:
                                tool_started_after_cancel += 1
                        elif etype == TASK_CANCELLED:
                            cancelled_received = True
                            print(f"    [ws] task.cancelled received")
                        elif etype in (TASK_STARTED, TASK_COMPLETED, TASK_FAILED):
                            print(f"    [ws] task.{etype.split('.')[-1]}")
                except websockets.exceptions.ConnectionClosed:
                    pass

            recv_task = asyncio.create_task(recv_loop())

            # Wait for at least one tool.started
            print("[+] Waiting for first tool.started event...")
            deadline = time.time() + 60
            while time.time() < deadline and tool_started_count == 0:
                await asyncio.sleep(0.2)

            if tool_started_count == 0:
                print("[!] No tool.started event within 60s, cancelling anyway")
            else:
                print(f"[+] Got {tool_started_count} tool.started event(s), now cancelling...")

            # Wait a bit for more steps to start
            print("[+] Waiting 2s for more tool.started events before cancelling...")
            await asyncio.sleep(2)

            # 4. Send cancel
            cancel_sent_at = time.time()
            cancel_acked_at = None
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(f"{BASE}/api/tasks/{task_id}/cancel")
                r.raise_for_status()
                cancel_acked_at = time.time()
                print(f"[+] Cancel sent, response: {r.json()}")

            # 5. Wait 6 seconds and count any tool.started after cancel
            print("[+] Waiting 6 seconds to observe post-cancel tool.started events...")
            await asyncio.sleep(6)

            # 6. Fetch final task status
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{BASE}/api/tasks/{task_id}")
                r.raise_for_status()
                resp = r.json()
                task_status_after_cancel = resp.get("task", resp)
                print(f"[+] Final task status: {task_status_after_cancel.get('status')}")

            recv_task.cancel()
            try:
                await recv_task
            except asyncio.CancelledError:
                pass

    else:
        # Fallback: poll REST endpoint for task status and events via SSE or repeated GET
        print("[!] Using REST polling fallback (no websockets)")
        print("[+] Waiting for first tool.started via polling...")
        # This is less reliable without WS, but we can try
        deadline = time.time() + 60
        last_seq = 0
        while time.time() < deadline and tool_started_count == 0:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{BASE}/api/tasks/{task_id}")
                if r.status_code == 200:
                    t = r.json()
                    # Can't easily get tool.started count from REST alone
                    pass
            await asyncio.sleep(1)

        print("[!] Cannot reliably track tool.started without WebSocket")
        return 1

    # 7. Report results
    print("\n=== RESULTS ===")
    print(f"Task ID: {task_id}")
    print(f"Total tool.started events: {tool_started_count}")
    print(f"tool.started AFTER cancel: {tool_started_after_cancel}")
    print(f"task.cancelled event received: {cancelled_received}")
    print(f"Final task status: {task_status_after_cancel.get('status') if task_status_after_cancel else 'unknown'}")

    success = (
        cancelled_received and
        task_status_after_cancel and task_status_after_cancel.get("status") == "cancelled" and
        tool_started_after_cancel == 0
    )

    if success:
        print("\n✓ PART (c) PASSED: Task cancelled cleanly, no tool.started after cancel")
        return 0
    else:
        print("\n✗ PART (c) FAILED")
        if not cancelled_received:
            print("  - task.cancelled event not received")
        if not task_status_after_cancel or task_status_after_cancel.get("status") != "cancelled":
            print(f"  - Final status not 'cancelled': {task_status_after_cancel.get('status') if task_status_after_cancel else 'unknown'}")
        if tool_started_after_cancel > 0:
            print(f"  - {tool_started_after_cancel} tool.started event(s) arrived AFTER cancel")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))