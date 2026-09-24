"""news_job.py - the 5-minute runner and the weekly report for psx_news.

Scheduled separately from the 18:30 MTS pipeline. A lock file keeps the two from running at
the same time: both write to psx.db, and an overlapping run would make news_runs lie about
which process recorded what.
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import psx_news as N

PKT = ZoneInfo("Asia/Karachi")
LOCK = Path("news_job.lock")
STALE_SECONDS = 900          # a run that died should not block the schedule forever
CONFIG = Path("alerts_config.json")


def acquire_lock():
    """Exclusive-create lock, portable to Windows where fcntl does not exist.

    O_CREAT|O_EXCL fails when the file is already there, which is the atomic test-and-set
    we need. A lock older than STALE_SECONDS is treated as a crashed run and taken over.
    """
    for attempt in (1, 2):
        try:
            fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - LOCK.stat().st_mtime
            except OSError:
                continue
            if age < STALE_SECONDS:
                return None
            try:
                LOCK.unlink()
            except OSError:
                return None
            continue
        os.write(fd, datetime.now(PKT).isoformat().encode())
        return fd
    return None


def release_lock(fd):
    try:
        os.close(fd)
        LOCK.unlink()
    except OSError:
        pass


def notifier_factory():
    """Returns (notify, delivered) so a run can tell 'formatted' from 'actually sent'."""
    cfg = {}
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[!] alerts_config.json unreadable: {e}")
    token = cfg.get("telegram_bot_token")
    chat = cfg.get("telegram_chat_id")
    if not (token and chat):
        print("[!] Telegram NOT configured. Messages are printed, not delivered. "
              "Fill alerts_config.json (gitignored).")
        return (lambda text: print("\n----- would send -----\n" + text)), False
    import urllib.request

    def send(text):
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = json.dumps({"chat_id": chat, "text": text[:3900]}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print(f"[!] telegram delivery failed: {type(e).__name__}")

    return send, True


def run_once(conn) -> dict:
    notify, delivered = notifier_factory()
    seen = new = parsed = 0
    for feed in N.FEEDS:
        s, n = N.fetch_feed(conn, feed, count=25)
        seen += s
        new += n
    meetings = N.update_board_meetings(conn, limit=4)
    msgs = N.new_alerts(conn, limit=25) + N.due_reminders(conn)
    ok, health = N.check_health(conn)
    if not ok:
        msgs.append("[NEWS MONITOR HEALTH] " + health)
    mf = N.market_filter(conn)
    if msgs:
        for m in msgs:
            notify(m)
    return {"feeds_rows": seen, "new": new, "meetings": len(meetings),
            "alerts": len(msgs), "delivered": delivered,
            "healthy": ok, "health": health,
            "regime": mf.get("regime"), "regime_label": mf.get("label")}


def weekly_report(conn, days: int = 7) -> str:
    since = (datetime.now(PKT) - timedelta(days=days)).isoformat()
    runs = conn.execute("SELECT status, COUNT(*), COALESCE(SUM(parsed),0), COALESCE(SUM(failed),0) "
                        "FROM news_runs WHERE started_at_pkt>=? GROUP BY status", (since,)).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM announcements WHERE captured_at_pkt>=?", (since,)).fetchone()[0]
    dup_guard = conn.execute("SELECT COUNT(DISTINCT doc_id), COUNT(*) FROM announcements").fetchone()
    mt = conn.execute("SELECT date_status, COUNT(*) FROM board_meetings WHERE captured_at_pkt>=? "
                      "GROUP BY 1 ORDER BY 2 DESC", (since,)).fetchall()
    ocr_dates = conn.execute("SELECT COUNT(*) FROM board_meetings WHERE date_source='OCR' "
                             "AND meeting_date IS NOT NULL").fetchone()[0]
    lines = [f"[WEEKLY NEWS REPORT] last {days} days, generated {datetime.now(PKT).isoformat()}"]
    for status, n, parsed, failed in runs:
        lines.append(f"  runs {status:8}: {n:4}  parsed={parsed}  failed={failed}")
    lines.append(f"  announcements stored: {total}   distinct doc_ids: {dup_guard[0]} "
                 f"(equal means no duplicates slipped in: {dup_guard[0] == dup_guard[1]})")
    lines.append("  board-meeting date outcomes:")
    for st, n in mt:
        lines.append(f"     {st:28} {n}")
    lines.append(f"  dates recovered only by OCR (need manual verify): {ocr_dates}")
    mf = N.market_filter(conn)
    lines.append(f"  market proxy: {mf.get('regime')} ({mf.get('label')}), "
                 f"{mf.get('pct_above_ma')}% vs its 50-session mean, "
                 f"adv {mf.get('advancers_last_window')} / dec {mf.get('decliners_last_window')}")
    lines.append("confidence: counts are measured; nothing here is a tested signal")
    return "\n".join(lines)


def main():
    # Task Scheduler starts a job with an unpredictable working directory, and every path in
    # this module (psx.db, raw_archive, alerts_config.json) is relative.
    os.chdir(str(Path(__file__).resolve().parent))
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    # The 18:30 MTS pipeline writes the same database. WAL lets readers and one writer
    # coexist, but a 5-minute poll hitting a write lock would otherwise raise immediately.
    conn = sqlite3.connect("psx.db", timeout=60)
    N.init_schema(conn)
    if mode == "report":
        print(weekly_report(conn))
        return
    if mode == "report30":
        print(weekly_report(conn, days=30))
        return
    fd = acquire_lock()
    if fd is None:
        print("[skip] another news_job holds the lock")
        return
    try:
        out = run_once(conn)
        print(json.dumps(out, indent=1))
    finally:
        release_lock(fd)
        conn.close()


if __name__ == "__main__":
    main()
