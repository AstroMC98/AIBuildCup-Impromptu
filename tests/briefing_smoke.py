"""End-to-end check of personal briefings against tests/fake_gemini.py.

    python -m tests.briefing_smoke

Starts the stub and the app like tests/smoke_test.py does (its own ports, so both
can run at once), with the briefing loop polling every second and a 24-hour lead
time, so any scheduled briefing is due at once. It checks:
  - the briefings page: premade templates, on demand by default, 1 credit per 5 minutes
  - the watchlist refuses private addresses; templates are validated and saved
  - "Make my briefing" goes live, keeps the template's run of show, stays private
    (no sharing, no albums, not in pod lists, not visible to anyone else), and
    refunds unused 5-minute blocks
  - a scheduled briefing is made with nobody listening, is saved ready to play, and
    the schedule moves on to the next day
"""
import asyncio
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

os.environ.setdefault("SMOKE_PORT", "8875")   # the stub on it, the app on the next port
from tests.smoke_test import APP_PORT, BASE, FAKE_PORT, WS, Listener, _start, _wait_up, sign_in  # noqa: E402


async def scenario(checks: dict) -> None:
    clients = [httpx.AsyncClient(base_url=BASE, timeout=60) for _ in range(3)]
    alice_c, dave_c, erin_c = clients
    try:
        alice = await sign_in(alice_c, "alice@example.com", "Alice")
        dave = await sign_in(dave_c, "dave@example.com", "Dave Kim")
        await sign_in(erin_c, "erin@example.com", "Erin")
        await alice_c.post(f"/api/admin/users/{dave['uid']}/credits", json={"amount": 10, "note": "briefings"})
        assert alice["admin"]

        page = (await dave_c.get("/api/briefings")).json()
        checks["briefings: premade templates, on demand by default, 1 credit per 5 minutes"] = (
            len(page["premade"]) >= 4 and not page["schedule"]["enabled"] and page["minutes_per_credit"] == 5
            and page["briefings"] == [] and page["lengths"] == [5, 10, 15])

        private = await dave_c.post("/api/watches", json={"url": "http://127.0.0.1:9/feed"})
        metadata = await dave_c.post("/api/watches", json={"url": "http://169.254.169.254/latest/meta-data/"})
        checks["watchlist refuses private addresses"] = (
            private.status_code == 422 and metadata.status_code == 422 and "private" in private.json()["detail"]
            and (await dave_c.get("/api/watches")).json()["watches"] == [])

        bad = await dave_c.post("/api/briefing-templates", json={
            "name": "Oops", "style": "Calm.", "sections": ["watchlist"],
            "hosts": [{"name": "Al", "voice": "Kore", "gender": "male"}]})
        made = await dave_c.post("/api/briefing-templates", json={
            "name": "My commute", "style": "Brisk and friendly.", "minutes": 5, "topics": "running, Spanish",
            "hosts": [{"name": "Ana", "voice": "Kore", "persona": "A running coach who teaches Spanish."}],
            "sections": ["watchlist", "custom"], "custom_title": "Spanish word of the day",
            "custom_notes": "One useful Spanish word, used in a sentence about running."})
        tid = made.json().get("id")
        mine = (await dave_c.get("/api/briefing-templates")).json()["items"]
        checks["briefing templates: validated (voice matches host) and saved"] = (
            bad.status_code == 422 and made.status_code == 200 and [t["id"] for t in mine] == [tid]
            and mine[0]["hosts"][0]["gender"] == "female")

        # on demand: it goes live
        r = await dave_c.post("/api/briefings", json={"template_id": tid, "minutes": 10, "tz": "America/New_York"})
        checks["make one now: 10 minutes costs 2 credits"] = r.status_code == 200 and r.json()["credits"] == 8
        bid = r.json()["id"]
        got = (await dave_c.get(f"/api/shows/{bid}")).json()
        show = got["show"]
        share = await dave_c.patch(f"/api/shows/{bid}", json={"visibility": "public"})
        organize = await dave_c.patch(f"/api/shows/{bid}/organize", json={"tags": ["x"]})
        listed = [s["id"] for s in (await dave_c.get("/api/shows")).json()["shows"]]
        on_page = [b["id"] for b in (await dave_c.get("/api/briefings")).json()["briefings"]]
        erin_sees = (await erin_c.get(f"/api/shows/{bid}")).status_code
        checks["a briefing is private: no sharing or albums, not in pod lists, nobody else sees it"] = (
            got["live"] and show["kind"] == "briefing" and show["visibility"] == "private"
            and abs(show["expires_at"] - (time.time() + 48 * 3600)) < 120
            and share.status_code == 422 and organize.status_code == 422
            and bid not in listed and on_page == [bid] and erin_sees == 404)

        async def net_credits(show_id: str) -> int:   # the ledger, not the 15-second account cache
            entries = (await dave_c.get("/api/me/ledger")).json()["entries"]
            return sum(e["amount"] for e in entries if e.get("show_id") == show_id)

        cookie = dave_c.cookies.get("gp_session")
        lst = await Listener("dave-briefing", f"{WS}/ws/show/{bid}", cookie).connect()
        await lst.until(lambda: lst.of("plan"), what="the plan")
        titles = [b["title"] for b in lst.of("plan")[0]["beats"]]
        checks["the plan keeps the template's run of show"] = titles == [
            "Cold open", "Nothing new on your watchlist", "Spanish word of the day", "Sign-off"]
        await lst.until(lambda: len(lst.of("segment_end")) >= 2, what="two segments")
        await lst.send({"type": "end", "mode": "now"})          # stop well inside the first 5 minutes
        await lst.until(lambda: lst.of("saved"), timeout=120, what="the briefing to be saved")
        await lst.close()
        await asyncio.sleep(0.5)
        heard = (await dave_c.get(f"/api/shows/{bid}")).json()["show"]["duration_s"]
        checks["stopped early: charged 2, the unused 5-minute block refunded"] = (
            0 < heard < 300 and await net_credits(bid) == -1)

        # a search to follow: words, not a link
        s = await dave_c.post("/api/watches", json={"url": "climate tech funding"})
        sw = s.json().get("watch", {})
        checks["words become a news search: its latest stories wait for the next briefing"] = (
            s.status_code == 200 and sw.get("kind") == "search" and sw.get("query") == "climate tech funding"
            and sw.get("waiting") == 3 and "FAKE SUGGESTIONS" in (sw.get("suggestions") or ""))

        # scheduled: made headless ahead of time
        soon = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%H:%M")
        r = await dave_c.put("/api/briefings/schedule", json={
            "enabled": True, "time": soon, "days": list(range(7)), "tz": "UTC", "template_id": "focus", "minutes": 5})
        first_ready = r.json().get("next_ready_by") or 0
        checks["a schedule is saved with its next time"] = (
            r.status_code == 200 and 2.9 * 3600 < first_ready - time.time() < 3.1 * 3600)
        deadline = time.time() + 120
        ready = None
        while time.time() < deadline and ready is None:
            await asyncio.sleep(1)
            data = (await dave_c.get("/api/briefings")).json()
            ready = next((b for b in data["briefings"] if b["briefing"].get("scheduled")
                          and b["status"] == "finished" and b["has_audio"]), None)
        assert ready, "the scheduled briefing never finished"
        audio = await dave_c.get(f"/api/shows/{ready['id']}/audio")
        checks["a scheduled briefing is made with nobody listening, ready to play"] = (
            audio.status_code == 200 and audio.headers["content-type"] == "audio/mpeg" and not ready["rendering"]
            and data["last"]["show_id"] == ready["id"] and ready["briefing"]["template"] == "Five-minute focus")
        checks["...and the schedule moves on to the next day"] = abs(data["next_ready_by"] - first_ready - 86400) < 5
        used = ready["briefing"]["updates"]
        checks["...it covered the search's stories, and keeps Google's suggestions to show"] = (
            len(used) == 3 and all(u["via"] == "search" for u in used)
            and any("FAKE SUGGESTIONS" in m for m in ready["briefing"]["search_suggestions"])
            and (await dave_c.get("/api/watches")).json()["watches"][0]["waiting"] == 0)
        checks["...and a 5-minute one costs 1 credit"] = (
            await net_credits(ready["id"]) == -1 and 0 < ready["duration_s"] <= 300)

        off = await dave_c.put("/api/briefings/schedule", json={
            "enabled": False, "time": soon, "days": list(range(7)), "tz": "UTC", "template_id": "focus"})
        checks["turning the schedule off clears the next time"] = off.json()["next_ready_by"] is None
    finally:
        for c in clients:
            await c.aclose()


def main() -> int:
    data_dir = tempfile.mkdtemp(prefix="podcasts-briefing-smoke-")
    env = {
        **os.environ, "GEMINI_API_KEY": "fake", "GEMINI_BASE_URL": f"http://127.0.0.1:{FAKE_PORT}",
        "STORAGE_BACKEND": "local", "DATA_DIR": data_dir, "AUTH_MODE": "dev", "SESSION_SECRET": "smoke",
        "ADMIN_EMAILS": "alice@example.com", "SIGNUP_CREDITS": "0",
        "CLOCK_SPEED": "20", "LOW_WATER_SECONDS": "3", "LINES_PER_SEGMENT": "4", "IDLE_END_SECONDS": "5",
        "STINGER_DELAY_SECONDS": "0", "BRIEFING_POLL_SECONDS": "1", "BRIEFING_LEAD_MINUTES": "1440",
    }
    py = sys.executable
    app_log = Path(data_dir, "app.log")
    fake = _start([py, "-m", "uvicorn", "tests.fake_gemini:app", "--port", str(FAKE_PORT)], env, Path(data_dir, "fake.log"))
    srv = _start([py, "-m", "uvicorn", "app.main:app", "--port", str(APP_PORT)], env, app_log)
    checks: dict[str, bool] = {}
    error = None
    try:
        _wait_up(f"http://127.0.0.1:{FAKE_PORT}/docs")
        _wait_up(f"{BASE}/healthz")
        asyncio.run(scenario(checks))
    except Exception as exc:
        error = exc
    finally:
        for p in (srv, fake):
            p.terminate()
            p.wait(timeout=10)
        out = app_log.read_text(encoding="utf-8", errors="replace")
    for name, ok in checks.items():
        print(("PASS " if ok else "FAIL ") + name)
    if error or not all(checks.values()):
        if error:
            print(f"ERROR {type(error).__name__}: {error}")
        print("---- app log (tail) ----")
        print(out[-4000:])
        return 1
    print(f"all {len(checks)} briefing checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
