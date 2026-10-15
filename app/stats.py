"""Listening stats for a pod's owner: who tuned in, for how long, and when they left.

A session is one listen. Live sessions come from the room, one per socket; replay
sessions come from the player's progress reports. The owner's own listening never
counts. Signed-in listeners are named; anonymous ones are counted by a random id
their browser keeps (`lid`), never identified.

Session fields
  id, kind (live | replay), uid and name (signed in) or None, lid, role
  joined_at, left_at      epoch seconds
  join_pos, leave_pos     seconds into the show (replays: the furthest point reached)
  listened_s              seconds actually playing
  to_end                  live: still listening when the show ended
  completed               replay: played to the end
"""
from __future__ import annotations

TIMELINE_STEP_SECONDS = 10    # how often the room samples "listening now"


def timeline_doc(timeline: list[list[int]]) -> list[dict]:
    """The listener timeline as saved: Firestore rejects arrays of arrays, so each
    [seconds, listening] sample is stored as {s, n}."""
    return [{"s": s, "n": n} for s, n in timeline]


def timeline_pairs(saved: list | None) -> list[list[int]]:
    """A saved timeline back as [seconds, listening] pairs (older local saves are pairs)."""
    return [[p["s"], p["n"]] if isinstance(p, dict) else [p[0], p[1]] for p in saved or []]


def person(s: dict) -> str:
    return f"u:{s['uid']}" if s.get("uid") else f"a:{s.get('lid') or s.get('id')}"


def _merge(sessions: list[dict]) -> dict[str, dict]:
    """Everyone's sessions folded into one record per person."""
    people: dict[str, dict] = {}
    for s in sessions:
        p = people.setdefault(person(s), {
            "uid": s.get("uid"), "name": s.get("name") if s.get("uid") else None,
            "sessions": 0, "listened_s": 0.0, "joined_at": None, "left_at": None,
            "join_pos": None, "leave_pos": None, "to_end": False, "completed": False,
        })
        p["sessions"] += 1
        p["listened_s"] += float(s.get("listened_s") or 0)
        if s.get("joined_at") and (p["joined_at"] is None or s["joined_at"] < p["joined_at"]):
            p["joined_at"], p["join_pos"] = s["joined_at"], s.get("join_pos")
        if s.get("left_at") and (p["left_at"] is None or s["left_at"] > p["left_at"]):
            p["left_at"], p["leave_pos"] = s["left_at"], s.get("leave_pos")
        p["to_end"] = p["to_end"] or bool(s.get("to_end"))
        p["completed"] = p["completed"] or bool(s.get("completed"))
    return people


def summarize(doc: dict, sessions: list[dict], *, comments: int = 0, live_now: int | None = None,
              timeline: list | None = None, subscribers: int = 0) -> dict:
    owner = doc.get("owner_uid")
    duration = float(doc.get("duration_s") or 0)
    not_owner = [s for s in sessions if not (s.get("uid") and s.get("uid") == owner)]
    live = _merge([s for s in not_owner if s.get("kind") == "live"])
    replay = _merge([s for s in not_owner if s.get("kind") == "replay"])
    timeline = timeline if timeline is not None else timeline_pairs(doc.get("listener_timeline"))

    # When people left: the last time each live listener dropped off, by minute of
    # the show. Anyone still there at the sign-off stayed to the end.
    dropoff: dict[int, int] = {}
    for p in live.values():
        if not p["to_end"] and p["leave_pos"] is not None:
            minute = int(p["leave_pos"] // 60)
            dropoff[minute] = dropoff.get(minute, 0) + 1

    live_total = sum(p["listened_s"] for p in live.values())
    replay_total = sum(p["listened_s"] for p in replay.values())
    completion = [min(1.0, p["leave_pos"] / duration) for p in replay.values()
                  if duration and p["leave_pos"] is not None]

    # Named rows for signed-in listeners only, live and replay side by side.
    named: dict[str, dict] = {}
    for kind, group in (("live", live), ("replay", replay)):
        for key, p in group.items():
            if not p["uid"]:
                continue
            row = named.setdefault(key, {"uid": p["uid"], "name": p["name"] or "Listener", "live": None, "replay": None})
            row[kind] = {k: p[k] for k in ("joined_at", "left_at", "join_pos", "leave_pos", "listened_s",
                                           "to_end", "completed", "sessions")}

    def first_seen(row: dict) -> float:
        times = [r["joined_at"] for r in (row["live"], row["replay"]) if r and r["joined_at"]]
        return min(times) if times else 0

    return {
        "duration_s": duration,
        "live": {
            "now": live_now,
            "peak": max((int(n) for _, n in timeline), default=0),
            "unique": len(live),
            "signed_in": sum(1 for p in live.values() if p["uid"]),
            "anonymous": sum(1 for p in live.values() if not p["uid"]),
            "listened_s": round(live_total, 1),
            "avg_listen_s": round(live_total / len(live), 1) if live else 0,
            "stayed_to_end": sum(1 for p in live.values() if p["to_end"]),
            "timeline": timeline,
            "dropoff": [{"minute": m, "left": n} for m, n in sorted(dropoff.items())],
        },
        "replays": {
            "plays": sum(p["sessions"] for p in replay.values()),
            "unique": len(replay),
            "listened_s": round(replay_total, 1),
            "avg_completion": round(sum(completion) / len(completion), 3) if completion else 0,
            "completed": sum(1 for p in replay.values() if p["completed"]),
        },
        "engagement": {
            "comments": comments,
            "hearts": int((doc.get("counts") or {}).get("hearts", 0)),
            "subscribers": subscribers,
        },
        "people": sorted(named.values(), key=first_seen),
        "anonymous": sum(1 for p in [*live.values(), *replay.values()] if not p["uid"]),
    }


def overview_row(doc: dict, summary: dict) -> dict:
    """One line per pod on the owner's stats page."""
    return {
        "id": doc["id"], "title": doc.get("title", ""), "accent": doc.get("accent"),
        "created_at": doc.get("created_at"), "status": doc.get("status"), "duration_s": summary["duration_s"],
        "listeners": summary["live"]["unique"], "peak": summary["live"]["peak"],
        "avg_listen_s": summary["live"]["avg_listen_s"], "replays": summary["replays"]["plays"],
        "listened_s": summary["live"]["listened_s"] + summary["replays"]["listened_s"],
    }
