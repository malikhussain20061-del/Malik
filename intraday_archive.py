"""intraday_archive.py - DISABLED. Built and measured, then gated behind written permission.

Why it is switched off: PSX Terms of Use (psx.com.pk/psx/terms-of-use, "Proprietary Rights")
forbid, without written permission, running robots/spiders against the site and doing
"systematic retrieval" of content to build a database. Checking robots.txt was not enough -
dps.psx.com.pk serves no robots.txt at all, and a robots file speaks for crawlers, not for the
contract. A permission request has been emailed to marketdatarequest@psx.com.pk.

The measurement that made this worth building is kept so the work is not lost: existing
raw_archive captures show the market-watch page genuinely changing during the session
(13:30, 14:17 and 14:40 on 2026-09-21 are three different SHA-256s) and static after the close
(18:30 and 18:57 on 2026-09-23 are identical bytes). So the cadence and the same-hash skip were
both correct; only the permission is missing.

To enable, put a real value in alerts_config.json under "psx_written_permission" recording what
PSX granted. Without it every entry point refuses and says why.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

PKT = ZoneInfo("Asia/Karachi")
URL = "https://dps.psx.com.pk/market-watch"
CONFIG = Path("alerts_config.json")
OUT = Path("raw_archive/intraday")
# PSX regular session is 09:00-15:30 PKT; the margins catch the pre-open and the close auction.
WINDOW = (dtime(8, 55), dtime(15, 40))
MAX_FETCHES_PER_DAY = 120     # a 5-minute cadence needs ~81; this is the runaway guard


def permission() -> str | None:
    """Written PSX permission, or None. Absence disables the module."""
    if not CONFIG.exists():
        return None
    try:
        v = json.loads(CONFIG.read_text(encoding="utf-8")).get("psx_written_permission")
    except Exception:
        return None
    return v if isinstance(v, str) and v.strip() else None


def require_permission(action: str) -> int:
    grant = permission()
    if grant:
        return 0
    print(f"[REFUSED] {action}: PSX Terms of Use forbid systematic retrieval without written\n"
          f"          permission. Record what PSX granted under \"psx_written_permission\" in\n"
          f"          {CONFIG} (gitignored). See walkthrough.md.")
    return 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS intraday_snapshots(
  captured_at_pkt TEXT PRIMARY KEY,
  sha256          TEXT NOT NULL,
  bytes           INTEGER NOT NULL,
  changed         INTEGER NOT NULL,
  gz_path         TEXT,
  rows_parsed     INTEGER,
  note            TEXT
);
"""


def in_session(now: datetime | None = None) -> bool:
    now = now or datetime.now(PKT)
    return now.weekday() < 5 and WINDOW[0] <= now.time() <= WINDOW[1]


def fetch() -> bytes:
    import psx_news
    req = urllib.request.Request(URL, headers=psx_news.user_agent())
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def count_today(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM intraday_snapshots WHERE captured_at_pkt LIKE ?",
                        (datetime.now(PKT).date().isoformat() + "%",)).fetchone()[0]


def snapshot(conn, now: datetime | None = None) -> dict:
    now = now or datetime.now(PKT)
    stamp = now.isoformat(timespec="seconds")
    if count_today(conn) >= MAX_FETCHES_PER_DAY:
        return {"skipped": f"daily fetch cap {MAX_FETCHES_PER_DAY} reached"}
    try:
        body = fetch()
    except Exception as e:
        # Recorded, not swallowed: a dead collector must be visible in the table itself.
        conn.execute("INSERT OR REPLACE INTO intraday_snapshots VALUES (?,?,?,?,?,?,?)",
                     (stamp, "ERROR", 0, 0, None, 0, f"{type(e).__name__}: {e}"[:300]))
        conn.commit()
        return {"error": f"{type(e).__name__}: {e}"}

    sha = hashlib.sha256(body).hexdigest()
    prev = conn.execute("SELECT sha256 FROM intraday_snapshots WHERE sha256 <> 'ERROR' "
                        "ORDER BY captured_at_pkt DESC LIMIT 1").fetchone()
    changed = 1 if prev is None or prev[0] != sha else 0
    gz_path = None
    if changed:
        OUT.mkdir(parents=True, exist_ok=True)
        gz_path = str(OUT / f"market_watch_{now.strftime('%Y%m%d_%H%M%S')}.gz")
        Path(gz_path).write_bytes(gzip.compress(body))
    rows = body.count(b"<tr")
    conn.execute("INSERT OR REPLACE INTO intraday_snapshots VALUES (?,?,?,?,?,?,?)",
                 (stamp, sha, len(body), changed, gz_path, rows,
                  None if changed else "identical to previous capture, not rewritten"))
    conn.commit()
    return {"captured": stamp, "sha256": sha[:12], "bytes": len(body), "changed": bool(changed),
            "tr_tags": rows, "stored": gz_path}


def status(conn, days: int = 5) -> str:
    since = datetime.now(PKT).strftime("%Y-%m-%d")
    rows = conn.execute("SELECT substr(captured_at_pkt,1,10) d, COUNT(*), SUM(changed), "
                        "SUM(CASE WHEN sha256='ERROR' THEN 1 ELSE 0 END), SUM(bytes) "
                        "FROM intraday_snapshots GROUP BY d ORDER BY d DESC LIMIT ?", (days,)).fetchall()
    lines = [f"[INTRADAY ARCHIVE STATUS] (window {WINDOW[0]}-{WINDOW[1]} PKT, cap {MAX_FETCHES_PER_DAY}/day)"]
    if not rows:
        return "\n".join(lines + ["  no snapshots yet - the scheduler has not run inside a session"])
    for d, n, ch, err, by in rows:
        uniq = ch or 0
        lines.append(f"  {d}: {n:3} fetches, {uniq:3} distinct pages, {err} errors, {by/1e6:.1f} MB raw")
    lines.append("  note: 'distinct pages' is what actually carries new information; identical")
    lines.append("  captures are timed but not written to disk.")
    return "\n".join(lines)


def loop(interval: int = 300) -> None:
    """Poll for the rest of the session, then exit.

    Task Scheduler's /sc minute repetition is bounded by a Duration measured from the start
    boundary on the start date, which is why a task built that way showed "Next Run: N/A" and
    would very likely never fire again tomorrow. The MTS job that has run reliably for days
    uses a plain daily trigger instead, so this loop owns the cadence and the task only has to
    start us once at 09:00.
    """
    while True:
        conn = sqlite3.connect("psx.db", timeout=60)
        conn.executescript(SCHEMA)
        try:
            if not in_session():
                if (datetime.now(PKT).time() > WINDOW[1]):
                    print(f"[done] session window closed at {WINDOW[1]}", flush=True)
                    return
            else:
                print(snapshot(conn), flush=True)
        finally:
            conn.close()
        time.sleep(interval)


def main():
    os.chdir(str(Path(__file__).resolve().parent))
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    if mode == "status":
        conn = sqlite3.connect("psx.db", timeout=60)
        conn.executescript(SCHEMA)
        print(status(conn))
        conn.close()
        return
    if require_permission(f"intraday snapshot ({mode})") != 0:
        return
    conn = sqlite3.connect("psx.db", timeout=60)
    conn.executescript(SCHEMA)
    if mode == "force":
        print(snapshot(conn))
    elif mode == "loop":
        conn.close()
        loop(int(sys.argv[2]) if len(sys.argv) > 2 else 300)
        return
    else:
        if not in_session():
            print(f"[skip] outside market window {WINDOW[0]}-{WINDOW[1]} PKT "
                  f"(now {datetime.now(PKT).strftime('%a %H:%M')})")
        else:
            print(snapshot(conn))
    conn.close()


if __name__ == "__main__":
    main()
