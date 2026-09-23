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

    # Step 1: Ingest perishable feeds (Quotes, FIPI/LIPI, NCCPL MTS report)
    log.info("Step 1: Ingesting daily perishable exchange feeds...")
    try:
        run_step(["psx_data_v2.py"], "CAPTURE_AND_INGEST_FEEDS")
    except Exception as e:
        log.warning("Live feed ingestion had warnings/issues: %s", e)

    # Step 2: Pipeline Execution (Shadow mode pre-Oct 1, Live Evaluate post-Oct 1)
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

    ok_msg = f"Routine completed successfully.\nDate: {today.isoformat()}\nMode: {mode}\nTime: {dt.datetime.now().isoformat()}"
    send_alert("OK", ok_msg)
    log.info("=== DAILY RUN COMPLETED SUCCESSFULLY FOR %s ===", today.isoformat())

if __name__ == "__main__":
    main()
