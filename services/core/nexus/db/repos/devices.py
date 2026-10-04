"""Device registry (task B3; spec §24-§29, §46 Devices screen).

Honest by construction: the only device that exists at first run is this
computer, registered by ``register_local``. Nothing else appears until a real
pairing happens, so the Devices screen shows one online device and an empty
state -- not a mock phone and tablet (§78, §100).

``state`` is derived from ``last_seen``, never asserted. A device that has not
checked in within ``STALE_SECONDS`` is reported offline even if its stored state
says otherwise, because the stored value is only as fresh as the last heartbeat.
"""

from __future__ import annotations

import platform
import socket
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import conn as db
from .base import new_id, now, to_dict, to_list, tombstone

ENTITY = "device"

STALE_SECONDS = 90

DEVICE_TYPES = ("desktop", "laptop", "phone", "tablet", "server", "unknown")

# Capabilities a device may expose to the rest of the system (§25).
CAPABILITIES = (
    "voice_input",
    "voice_output",
    "screen_capture",
    "file_access",
    "terminal",
    "notifications",
    "camera",
    "location",
)


def register_local() -> dict[str, Any]:
    """Ensure this machine has a device row. Idempotent across restarts.

    Identity is the hostname: stable on a personal machine, and it avoids
    inventing a second device row every time the process starts.
    """
    hostname = socket.gethostname()
    existing = to_dict(
        db.query_one("SELECT * FROM devices WHERE name = ?", (hostname,))
    )
    stamp = now()
    if existing:
        with db.transaction() as c:
            c.execute(
                "UPDATE devices SET state = 'online', last_seen = ?, updated_at = ?, rev = ? WHERE id = ?",
                (stamp, stamp, db.next_rev(c), existing["id"]),
            )
        existing.update(state="online", last_seen=stamp)
        return with_capabilities(existing)

    did = new_id()
    kind = "laptop" if _is_laptop() else "desktop"
    with db.transaction() as c:
        rev = db.next_rev(c)
        c.execute(
            """
            INSERT INTO devices (id, name, type, state, last_seen, created_at, updated_at, rev)
            VALUES (?, ?, ?, 'online', ?, ?, ?, ?)
            """,
            (did, hostname, kind, stamp, stamp, stamp, rev),
        )
        # Only what this platform can actually do is enabled; the rest are
        # recorded as present-but-disabled so the UI can explain why (§25, §78).
        for cap in CAPABILITIES:
            c.execute(
                """
                INSERT INTO device_capabilities (device_id, capability, enabled)
                VALUES (?, ?, ?)
                ON CONFLICT(device_id, capability) DO UPDATE SET enabled = excluded.enabled
                """,
                (did, cap, 1 if cap in _local_capabilities() else 0),
            )
    return with_capabilities(
        {
            "id": did,
            "name": hostname,
            "type": kind,
            "state": "online",
            "last_seen": stamp,
            "created_at": stamp,
            "updated_at": stamp,
            "rev": rev,
            "is_local": True,
        }
    )


def _is_laptop() -> bool:
    """Best-effort chassis hint; falls back to 'desktop' when unknown."""
    try:
        from pathlib import Path

        for battery in Path("/sys/class/power_supply").glob("BAT*"):
            if battery.exists():
                return True
    except OSError:
        pass
    return False


def _local_capabilities() -> set[str]:
    caps = {"file_access", "notifications"}
    if platform.system() == "Linux":
        caps.update({"screen_capture", "terminal"})
    # Voice and camera are only claimed once the H-phase probes confirm a working
    # device; registering them as enabled here would be a promise we cannot keep.
    return caps


def local_id() -> str | None:
    row = db.query_one(
        "SELECT id FROM devices WHERE name = ?", (socket.gethostname(),)
    )
    return row["id"] if row else None


def heartbeat(device_id: str) -> bool:
    stamp = now()
    with db.transaction() as c:
        cur = c.execute(
            "UPDATE devices SET state = 'online', last_seen = ?, updated_at = ?, rev = ? WHERE id = ?",
            (stamp, stamp, db.next_rev(c), device_id),
        )
        return (cur.rowcount or 0) > 0


def get(device_id: str) -> dict[str, Any] | None:
    row = to_dict(db.query_one("SELECT * FROM devices WHERE id = ?", (device_id,)))
    return with_capabilities(row) if row else None


def list_all() -> list[dict[str, Any]]:
    rows = to_list(db.query("SELECT * FROM devices ORDER BY created_at"))
    return [with_capabilities(row) for row in rows]


def with_capabilities(row: dict[str, Any]) -> dict[str, Any]:
    caps = db.query(
        "SELECT capability, enabled FROM device_capabilities WHERE device_id = ?",
        (row["id"],),
    )
    row["capabilities"] = {r["capability"]: bool(r["enabled"]) for r in caps}
    row["state"] = _effective_state(row)
    row["is_local"] = row.get("name") == socket.gethostname()
    return row


def _effective_state(row: dict[str, Any]) -> str:
    if row.get("state") == "unavailable":
        return "unavailable"
    raw = row.get("last_seen")
    if not raw:
        return "offline"
    try:
        seen = datetime.fromisoformat(raw)
    except ValueError:
        return "offline"
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    fresh = datetime.now(timezone.utc) - seen <= timedelta(seconds=STALE_SECONDS)
    return "online" if fresh else "offline"


def set_capability(device_id: str, capability: str, enabled: bool) -> bool:
    if capability not in CAPABILITIES:
        raise ValueError(f"unknown capability {capability!r}")
    with db.transaction() as c:
        c.execute(
            """
            INSERT INTO device_capabilities (device_id, capability, enabled)
            VALUES (?, ?, ?)
            ON CONFLICT(device_id, capability) DO UPDATE SET enabled = excluded.enabled
            """,
            (device_id, capability, 1 if enabled else 0),
        )
        c.execute(
            "UPDATE devices SET updated_at = ?, rev = ? WHERE id = ?",
            (now(), db.next_rev(c), device_id),
        )
    return True


def mark_offline_stale() -> int:
    """Flip stale rows so a stored 'online' cannot outlive its heartbeat."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=STALE_SECONDS)).isoformat()
    with db.transaction() as c:
        cur = c.execute(
            """
            UPDATE devices SET state = 'offline', updated_at = ?, rev = ?
            WHERE state = 'online' AND (last_seen IS NULL OR last_seen < ?)
            """,
            (now(), db.next_rev(c), cutoff),
        )
        return cur.rowcount or 0


def forget(device_id: str) -> bool:
    """Unpair. Capabilities cascade."""
    with db.transaction() as c:
        rev = db.next_rev(c)
        cur = c.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        removed = (cur.rowcount or 0) > 0
        if removed:
            tombstone(c, ENTITY, device_id, rev)
        return removed
