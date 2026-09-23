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

def run_step(cmd: list[str], step_name: str) -> None:
    log.info("Starting step: %s (%s)", step_name, " ".join(cmd))
    res = subprocess.run([sys.executable] + cmd, capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        log.error("Step %s FAILED (exit code %d):\nSTDOUT:\n%s\nSTDERR:\n%s",
                  step_name, res.returncode, res.stdout, res.stderr)
        # Write emergency alert file
        alert_file = Path("CRITICAL_JOB_FAILURE.log")
        alert_file.write_text(f"FAILED {step_name} at {dt.datetime.now().isoformat()}\n{res.stderr}\n{res.stdout}", encoding="utf-8")
        raise RuntimeError(f"Step {step_name} failed: {res.stderr}")
    log.info("Step %s SUCCEEDED:\n%s", step_name, res.stdout.strip())

def main() -> None:
    today = dt.date.today()
    log.info("=== DAILY RUN INITIATED FOR %s ===", today.isoformat())

    # Step 1: Online WAL backup & offsite mirror
    try:
        run_step(["backup.py"], "DISASTER_RECOVERY_BACKUP")
    except Exception as e:
        log.critical("Backup failed: %s", e)
        sys.exit(1)

    # Step 2: Shadow Run (pre-Oct 1) or Evaluate (post-Oct 1)
    mode = "shadow" if today < FREEZE_DATE else "evaluate"
    log.info("Executing pipeline in mode: %s (Freeze date: %s)", mode, FREEZE_DATE.isoformat())
    try:
        run_step(["run_mts_h1.py", mode], f"PIPELINE_{mode.upper()}")
    except Exception as e:
        log.critical("Pipeline %s failed: %s", mode, e)
        sys.exit(1)

    log.info("=== DAILY RUN COMPLETED SUCCESSFULLY FOR %s ===", today.isoformat())

if __name__ == "__main__":
    main()
