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
    eval_str = SPEC.get("evaluation_start")
    EVAL_START = dt.date.fromisoformat(eval_str) if eval_str else None
except Exception:
    EVAL_START = None   # unreadable spec must never trigger live evaluation

def send_alert(status: str, detail: str) -> None:
    # 1. Write local status file
    status_file = Path("daily_job_status.txt")
    status_file.write_text(
        f"STATUS: {status}\nTIMESTAMP_PKT: {dt.datetime.now().isoformat()}\nDETAIL:\n{detail}\n",
        encoding="utf-8"
    )
    # 2. Telegram / Webhook notification. Credentials live in alerts_config.json (gitignored),
    #    because a Task Scheduler job running without an interactive logon never loads user
    #    environment variables, so an env-var-only design silently disables every alert.
    import os, urllib.request, json
    cfg = {}
    cfg_path = Path("alerts_config.json")
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("alerts_config.json unreadable: %s", e)
    tg_token = cfg.get("telegram_bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = cfg.get("telegram_chat_id") or os.environ.get("TELEGRAM_CHAT_ID")
    if not (tg_token and tg_chat):
        log.warning("Telegram not configured (alerts_config.json missing keys); local status file only.")
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
        # stdout is where the feed errors actually land (psx_data_v2 prints them), so a
        # rejection like HTTP 403 is invisible if only stderr is carried into the message.
        raise RuntimeError(f"Step {step_name} failed: {res.stderr}\nSTDOUT: {res.stdout}"[:4000])
    log.info("Step %s SUCCEEDED:\n%s", step_name, res.stdout.strip())

BLOCKED_MARKERS = ("403", "404", "Forbidden", "Not Found", "unreachable", "timed out")


def classify_capture_failure(text: str) -> str:
    """Distinguish 'the exchange is refusing us' from 'our parser broke'.

    They need opposite responses - one is a permission/email question, the other a code fix -
    and one generic FAILURE status hides which of the two happened.
    """
    return "FEED_BLOCKED" if any(m in text for m in BLOCKED_MARKERS) else "FEED_PARSE_FAILURE"


def main() -> None:
    today = dt.date.today()
    log.info("=== DAILY RUN INITIATED FOR %s ===", today.isoformat())

    # Weekend guard: PSX is closed on Saturday (5) and Sunday (6)
    if today.weekday() >= 5:
        log.info("Today is %s (Weekend - PSX closed). Routine completed with no actions.", today.strftime("%A"))
        return

    capture_failed = False
    capture_status = None
    # Step 1: Ingest perishable feeds (Quotes, FIPI/LIPI, NCCPL MTS report)
    log.info("Step 1: Ingesting daily perishable exchange feeds...")
    try:
        run_step(["psx_data_v2.py"], "CAPTURE_AND_INGEST_FEEDS")
    except Exception as e:
        capture_failed = True
        capture_status = classify_capture_failure(str(e))
        log.error("Live feed ingestion failed (%s): %s", capture_status, e)
        send_alert(capture_status, f"psx_data_v2.py capture failed as {capture_status}: {e}")

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

    # Step 2: Pipeline Execution (stays shadow until a pre-data amendment commits evaluation_start)
    mode = "shadow" if EVAL_START is None or today < EVAL_START else "evaluate"
    log.info("Step 2: Executing pipeline in mode: %s (evaluation start: %s)",
             mode, EVAL_START.isoformat() if EVAL_START else "not committed")
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

    # Step 4: Guard drill. shadow mode returns from run_mts_h1 before verify_registration,
    # so this nightly drill is the only thing that detects a drifted CODE_FILES hash. It runs
    # on an isolated DB copy, so the real ledger is not at risk.
    log.info("Step 4: Running guard drill (tamper + code-hash verification)...")
    try:
        run_step(["guard_drill.py"], "GUARD_DRILL")
    except Exception as e:
        log.critical("Guard drill failed - integrity of the registered code/data is unverified: %s", e)
        send_alert("GUARD_DRILL_FAILED",
                   f"Guard drill FAILED on {today.isoformat()}.\n{str(e)[:3000]}")
        sys.exit(1)

    if capture_failed:
        warn_msg = (f"Routine completed with {capture_status}.\nDate: {today.isoformat()}\n"
                    f"Mode: {mode}\nTime: {dt.datetime.now().isoformat()}\n"
                    f"Next action: "
                    + ("exchange refused our requests - do NOT retry; settle access with PSX, "
                       "and backfill this day from a manually downloaded closing-rate file."
                       if capture_status == "FEED_BLOCKED" else
                       "feed answered but the data did not ingest - this is a parser or schema "
                       "problem, check CRITICAL_JOB_FAILURE.log."))
        send_alert(f"COMPLETED_WITH_{capture_status}", warn_msg)
        log.warning("=== DAILY RUN COMPLETED WITH %s FOR %s ===", capture_status, today.isoformat())
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
