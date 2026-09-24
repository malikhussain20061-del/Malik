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
from datetime import date, datetime, time as dtime, timedelta
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


PLACEHOLDER = ("PASTE_", "YOUR ", "EXAMPLE.COM", "XXXX", "CHANGEME")


def telegram_status(path=CONFIG) -> tuple[bool, str]:
    """Why Telegram is or is not usable, without ever echoing a credential."""
    if not path.exists():
        return False, (f"{path.resolve()} does not exist. It is NOT the same file as "
                       f"alerts_config.example.json - the example is a template and is never read.")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"{path.resolve()} is not valid JSON ({type(e).__name__}: {str(e)[:90]})"
    problems = []
    for key, label in (("telegram_bot_token", "bot token"), ("telegram_chat_id", "chat id")):
        v = str(cfg.get(key) or "").strip()
        if not v:
            problems.append(f"{label} is empty in {path}")
        elif any(v.upper().startswith(p) or p in v.upper() for p in PLACEHOLDER):
            problems.append(f"{label} still says {v.split()[0]!r} - that is template text, "
                            f"not a real value")
    return (False, "Telegram not usable: " + "; ".join(problems)) if problems else (True, "ready")


def notifier_factory():
    """Returns (notify, delivered) so a run can tell 'formatted' from 'actually sent'."""
    ready, why = telegram_status()
    if not ready:
        print(f"[!] {why}")
        return (lambda text: print("\n----- would send -----\n" + text)), False
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    token, chat = cfg["telegram_bot_token"], cfg["telegram_chat_id"]
    import urllib.request

    def send(text):
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = json.dumps({"chat_id": chat, "text": text[:3900]}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            # The reason usually says whether the token or the chat id is wrong, and never
            # needs the token echoed to say so.
            print(f"[!] telegram delivery failed: {type(e).__name__}: {str(e)[:160]}")

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
    # Date extraction quality, because "OCR works" is only a claim until it is counted.
    tot_mt = conn.execute("SELECT COUNT(*) FROM board_meetings WHERE captured_at_pkt>=?",
                          (since,)).fetchone()[0]
    parsed_mt = conn.execute("SELECT COUNT(*) FROM board_meetings WHERE captured_at_pkt>=? "
                             "AND meeting_date IS NOT NULL", (since,)).fetchone()[0]
    checked = conn.execute("SELECT manual_check, COUNT(*) FROM board_meetings WHERE date_source='OCR' "
                           "AND manual_check IS NOT NULL GROUP BY 1").fetchall()
    lines = [f"[WEEKLY NEWS REPORT] last {days} days, generated {datetime.now(PKT).isoformat()}"]
    for status, n, parsed, failed in runs:
        lines.append(f"  runs {status:8}: {n:4}  parsed={parsed}  failed={failed}")
    lines.append(f"  announcements stored: {total}   distinct doc_ids: {dup_guard[0]} "
                 f"(equal means no duplicates slipped in: {dup_guard[0] == dup_guard[1]})")
    rate = f"{parsed_mt / tot_mt * 100:.0f}%" if tot_mt else "n/a"
    lines.append(f"  meeting notices: {tot_mt}, date extracted from {parsed_mt} ({rate})")
    lines.append("  board-meeting date outcomes:")
    for st, n in mt:
        lines.append(f"     {st:28} {n}")
    lines.append(f"  dates recovered only by OCR (need manual verify): {ocr_dates}")
    if checked:
        good = sum(n for k, n in checked if k == "CORRECT")
        bad = sum(n for k, n in checked if k and k.startswith("WRONG"))
        lines.append(f"  OCR dates a human checked: {good + bad} of {ocr_dates} -> "
                     f"{good} correct, {bad} WRONG"
                     + (f"  => OCR error rate {bad / (good + bad) * 100:.0f}%" if good + bad else ""))
    else:
        lines.append(f"  OCR dates a human checked: 0 of {ocr_dates} -> accuracy is UNKNOWN. "
                     f"Use psx_news.record_manual_check(conn, doc_id, 'CORRECT'|'WRONG:YYYY-MM-DD')")
    mf = N.market_filter(conn)
    lines.append(f"  market proxy: {mf.get('regime')} ({mf.get('label')}), "
                 f"{mf.get('pct_above_ma')}% vs its 50-session mean, "
                 f"adv {mf.get('advancers_last_window')} / dec {mf.get('decliners_last_window')}")
    lines.append("confidence: counts are measured; nothing here is a tested signal")
    return "\n".join(lines)


ACTIVE = (dtime(9, 0), dtime(18, 0))     # session + immediate after-hours, every 5 min
LATE = (dtime(18, 0), dtime(21, 0))      # results are often filed in the evening, every 30 min


def cadence_seconds(now: datetime, active=300, late=1800) -> int | None:
    """None means sleep through it: weekends and the overnight gap are not polling time.

    Announcements keep arriving after the close - the notices seen today were timestamped
    15:04 and 16:06 PKT - so stopping at 15:30 would miss the busiest filing window.
    """
    if now.weekday() >= 5:
        return None
    t = now.time()
    if ACTIVE[0] <= t < ACTIVE[1]:
        return active
    if LATE[0] <= t < LATE[1]:
        return late
    return None


def loop(active: int = 300, late: int = 1800) -> None:
    """Poll on the cadence above until 21:00 PKT, then exit for tomorrow's daily trigger.

    Task Scheduler's /sc minute repetition is bounded by a Duration measured from the start
    boundary on the start date; a task built that way reported "Next Run: N/A" and would
    probably never fire again. The MTS job that has run for days uses a plain daily trigger, so
    the cadence lives here and the task only has to start us once at 09:00.
    """
    while True:
        now = datetime.now(PKT)
        if now.time() >= LATE[1]:
            print(f"[done] past {LATE[1]} PKT, exiting until tomorrow's trigger", flush=True)
            return
        wait = cadence_seconds(now, active, late)
        if wait is None:
            nxt = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if now >= nxt:
                nxt += timedelta(days=1)
            while now.weekday() >= 5:
                nxt += timedelta(days=1)
                now = nxt
            print(f"[sleep] off-hours, next poll {nxt.isoformat()}", flush=True)
            time.sleep(max(60, (nxt - datetime.now(PKT)).total_seconds()))
            continue
        fd = acquire_lock()
        if fd is None:
            print("[skip] another news_job holds the lock", flush=True)
        else:
            conn = sqlite3.connect("psx.db", timeout=60)
            try:
                N.init_schema(conn)
                print(json.dumps(run_once(conn)), flush=True)
            finally:
                release_lock(fd)
                conn.close()
        time.sleep(wait)


def digest() -> str:
    """The three lines that answer 'is anything broken today', in one screen.

    Order is deliberate: the MTS feed date is the one that gates the real experiment, the
    tamper drill is the only thing that checks the locked code hash during shadow mode, and
    news health is the collector's own pulse.
    """
    import subprocess
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect("psx.db", timeout=60)
    N.init_schema(conn)
    row = conn.execute("SELECT MAX(report_date) FROM mts_snapshots").fetchone()
    mts = row[0] or "NONE"
    if row[0]:
        days = (datetime.now(PKT).date() - date.fromisoformat(row[0])).days
        mts += f"  ({days} days old)"
    try:
        out = subprocess.run([sys.executable, "guard_drill.py"], capture_output=True,
                             text=True, encoding="utf-8", timeout=900)
        passed = (out.stdout or "").count("[PASS]")
        failed = (out.stdout or "").count("[FAIL]")
        drill = (f"{passed} PASS / {failed} FAIL" if passed or failed
                 else f"no result (exit {out.returncode})")
    except Exception as e:
        drill = f"DRILL DID NOT RUN: {type(e).__name__}"
    ok, health = N.check_health(conn)
    conn.close()
    return (f"{datetime.now(PKT).strftime('%Y-%m-%d %H:%M PKT')}\n"
            f"  MTS feed      : {mts}\n"
            f"  guard_drill   : {drill}\n"
            f"  news monitor  : {health}")


def main():
    # Task Scheduler starts a job with an unpredictable working directory, and every path in
    # this module (psx.db, raw_archive, alerts_config.json) is relative.
    os.chdir(str(Path(__file__).resolve().parent))
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "digest":
        msg = digest()
        print(msg)
        ready, _ = telegram_status()
        if ready:
            notify, _ = notifier_factory()
            notify(msg)
        return
    if mode == "loop":
        loop(int(sys.argv[2]) if len(sys.argv) > 2 else 300,
             int(sys.argv[3]) if len(sys.argv) > 3 else 1800)
        return
    # The 18:30 MTS pipeline writes the same database. WAL lets readers and one writer
    # coexist, but a 5-minute poll hitting a write lock would otherwise raise immediately.
    conn = sqlite3.connect("psx.db", timeout=60)
    N.init_schema(conn)
    if mode == "check-telegram":
        ready, why = telegram_status()
        print(("READY: " if ready else "NOT READY: ") + why)
        print(f"expected file: {CONFIG.resolve()}")
        return
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
