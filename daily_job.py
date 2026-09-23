"""
daily_job.py — Automated Daily Pipeline Runner for Windows Task Scheduler
Executes daily disaster backup and shadow / live evaluation.
Runs at 18:30 PKT (after NCCPL report publication, well before next T+2 open).
"""
import datetime as dt
import logging
import subprocess
import sys
from pathlib import Path

LOG_FILE = Path("daily_job.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger("daily_job")

try:
    from run_mts_h1 import SPEC
    freeze_str = SPEC.get("freeze_date", "2026-10-01")
    FREEZE_DATE = dt.date.fromisoformat(freeze_str)
except Exception:
    FREEZE_DATE = dt.date(2026, 10, 1)

def send_alert(status: str, detail: str) -> None:
    # 1. Write local status file
    status_file = Path("daily_job_status.txt")
    status_file.write_text(
        f"STATUS: {status}\nTIMESTAMP_PKT: {dt.datetime.now().isoformat()}\nDETAIL:\n{detail}\n",
        encoding="utf-8"
    )
    # 2. Telegram / Webhook notification if environment variables configured
    import os, urllib.request, json
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        try:
            tg_url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
            payload = json.dumps({"chat_id": tg_chat, "text": f"[PSX PIPELINE] {status}\n{detail[:3500]}"}).encode("utf-8")
            req = urllib.request.Request(tg_url, data=payload, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
            log.info("Telegram notification successfully dispatched.")
        except Exception as e:
            log.warning("Telegram alert failed: %s", e)

def run_step(cmd: list[str], step_name: str) -> None:
    log.info("Starting step: %s (%s)", step_name, " ".join(cmd))
    res = subprocess.run([sys.executable] + cmd, capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        log.error("Step %s FAILED (exit code %d):\nSTDOUT:\n%s\nSTDERR:\n%s",
                  step_name, res.returncode, res.stdout, res.stderr)
        # Write emergency alert file and trigger notification
        alert_file = Path("CRITICAL_JOB_FAILURE.log")
        err_msg = f"FAILED {step_name} at {dt.datetime.now().isoformat()}\nSTDERR:\n{res.stderr}\nSTDOUT:\n{res.stdout}"
        alert_file.write_text(err_msg, encoding="utf-8")
        send_alert(f"CRITICAL_FAILURE: {step_name}", err_msg)
        raise RuntimeError(f"Step {step_name} failed: {res.stderr}")
    log.info("Step %s SUCCEEDED:\n%s", step_name, res.stdout.strip())

def main() -> None:
    today = dt.date.today()
    log.info("=== DAILY RUN INITIATED FOR %s ===", today.isoformat())

    # Weekend guard: PSX is closed on Saturday (5) and Sunday (6)
    if today.weekday() >= 5:
        log.info("Today is %s (Weekend - PSX closed). Routine completed with no actions.", today.strftime("%A"))
        return

    capture_failed = False
    # Step 1: Ingest perishable feeds (Quotes, FIPI/LIPI, NCCPL MTS report)
    log.info("Step 1: Ingesting daily perishable exchange feeds...")
    try:
        run_step(["psx_data_v2.py"], "CAPTURE_AND_INGEST_FEEDS")
    except Exception as e:
        capture_failed = True
        log.error("Live feed ingestion failed: %s", e)
        send_alert("WARNING_FEED_CAPTURE_FAILED", f"psx_data_v2.py capture encountered failure: {e}")

    # Check feed freshness in database
    import sqlite3
    try:
        conn = sqlite3.connect("psx.db")
        latest_rd = conn.execute("SELECT max(report_date) FROM mts_snapshots").fetchone()[0]
        if latest_rd:
            rd_date = dt.date.fromisoformat(latest_rd)
            days_old = (today - rd_date).days
            if days_old > 4:  # >4 calendar days means >2 trading sessions stagnant
                stale_msg = f"STALE_FEED: Latest report_date is {latest_rd} ({days_old} days old)."
                log.warning(stale_msg)
                send_alert("STALE_FEED_ALERT", stale_msg)
    except Exception as e:
        log.warning("Could not check feed freshness: %s", e)

    had_gap = False
    # Check if GAP was detected in today's ingest run
    try:
        conn = sqlite3.connect("psx.db")
        gap_count = conn.execute(
            "SELECT COUNT(*) FROM ingest_runs WHERE status='GAP_DETECTED' AND substr(started_ts, 1, 10)=?",
            (today.isoformat(),)
        ).fetchone()[0]
        if gap_count:
            had_gap = True
            gap_msg = f"GAP_DETECTED on {today.isoformat()}: Missed trading session detected in DPS feed. Intermediate day must be backfilled from official PSX closing sheet before next T+2 entry."
            log.warning(gap_msg)
            send_alert("GAP_DETECTED", gap_msg)
    except Exception as e:
        log.warning("Could not check GAP_DETECTED in ingest_runs: %s", e)

    # Check if hypothesis has already completed and evaluated
    try:
        conn = sqlite3.connect("psx.db")
        row = conn.execute("SELECT status FROM hypothesis_ledger WHERE hypothesis_id=?", (SPEC["hypothesis_id"],)).fetchone()
        if row and str(row[0]).startswith("EVALUATED"):
            log.info("Hypothesis %s status is %s. 240 sessions complete. Pipeline standing down.", SPEC["hypothesis_id"], row[0])
            send_alert("EXPERIMENT_COMPLETED", f"Hypothesis {SPEC['hypothesis_id']} has reached final state ({row[0]}). Standing down.")
            return
    except Exception as e:
        log.warning("Could not check hypothesis_ledger status: %s", e)

    # Step 2: Pipeline Execution (Shadow mode pre-freeze, Live Evaluate post-freeze)
    mode = "shadow" if today < FREEZE_DATE else "evaluate"
    log.info("Step 2: Executing pipeline in mode: %s (Freeze date: %s)", mode, FREEZE_DATE.isoformat())
    try:
        run_step(["run_mts_h1.py", mode], f"PIPELINE_{mode.upper()}")
    except Exception as e:
        log.critical("Pipeline %s failed: %s", mode, e)
        sys.exit(1)

    # Step 3: Disaster Recovery Backup AT THE END (ensures today's fresh data is mirrored)
    log.info("Step 3: Running disaster recovery backup and multi-mirror sync...")
    try:
        run_step(["backup.py"], "DISASTER_RECOVERY_BACKUP")
    except Exception as e:
        log.critical("Disaster recovery backup failed: %s", e)
        sys.exit(1)

    if capture_failed:
        warn_msg = f"Routine completed with CAPTURE WARNINGS.\nDate: {today.isoformat()}\nMode: {mode}\nTime: {dt.datetime.now().isoformat()}"
        send_alert("COMPLETED_WITH_CAPTURE_FAILURE", warn_msg)
        log.warning("=== DAILY RUN COMPLETED WITH WARNINGS FOR %s ===", today.isoformat())
    elif had_gap:
        gap_summary = f"Routine completed with GAP WARNING.\nDate: {today.isoformat()}\nMode: {mode}\nTime: {dt.datetime.now().isoformat()}\nMissed session must be backfilled from PSX closing sheet."
        send_alert("COMPLETED_WITH_GAP", gap_summary)
        log.warning("=== DAILY RUN COMPLETED WITH GAP DETECTED FOR %s ===", today.isoformat())
    else:
        ok_msg = f"Routine completed successfully.\nDate: {today.isoformat()}\nMode: {mode}\nTime: {dt.datetime.now().isoformat()}"
        send_alert("OK", ok_msg)
        log.info("=== DAILY RUN COMPLETED SUCCESSFULLY FOR %s ===", today.isoformat())

if __name__ == "__main__":
    main()
