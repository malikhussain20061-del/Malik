"""
backup.py — Disaster Recovery & Audit Archive Backup
Uses sqlite3.backup API (WAL-mode safe) and compresses raw_archive.
Prunes backups older than 30 days.
"""

import sys
import shutil
import sqlite3
import datetime as dt
import pathlib

# Ensure Windows terminal UTF-8 encoding
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

BACKUP_DIR = pathlib.Path("backups")
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
today = dt.date.today()
stamp = today.isoformat()

print(f"[*] Starting disaster recovery backup for {stamp}...")

# 1. Safe SQLite Backup API (WAL-mode safe, zero locking corruption)
if pathlib.Path("psx.db").exists():
    src = sqlite3.connect("psx.db")
    dst = sqlite3.connect(BACKUP_DIR / f"psx_{stamp}.db")
    src.backup(dst)
    dst.close()
    src.close()
    print(f" [+] SQLite database backed up: psx_{stamp}.db")

# 2. Archive raw data folder (GZIP raw archive preservation)
if pathlib.Path("raw_archive").exists():
    zip_target = BACKUP_DIR / f"raw_{stamp}"
    shutil.make_archive(str(zip_target), "zip", "raw_archive")
    print(f" [+] Raw archive compressed: raw_{stamp}.zip")

# 3. Retention policy: Remove daily copies older than 30 days
pruned = 0
for f in BACKUP_DIR.glob("psx_*.db"):
    try:
        f_date_str = f.stem.replace("psx_", "")
        if (today - dt.date.fromisoformat(f_date_str)).days > 30:
            f.unlink()
            pruned += 1
    except Exception:
        pass

print(f"[OK] Local backup complete -> {BACKUP_DIR} (Pruned {pruned} old files >30d)")

# 4. Offsite Disaster Recovery Mirror (Documents & OneDrive)
offsite_dirs = [
    pathlib.Path.home() / "Documents" / "PSX_Backups_Archive",
    pathlib.Path.home() / "OneDrive" / "PSX_Backups_Archive",
]
for off in offsite_dirs:
    try:
        off.mkdir(parents=True, exist_ok=True)
        shutil.copy2(BACKUP_DIR / f"psx_{stamp}.db", off / f"psx_{stamp}.db")
        if (BACKUP_DIR / f"raw_{stamp}.zip").exists():
            shutil.copy2(BACKUP_DIR / f"raw_{stamp}.zip", off / f"raw_{stamp}.zip")
        print(f" [+] Offsite disaster recovery mirror synced: {off}")
    except Exception as e:
        print(f" [!] Offsite mirror to {off} skipped: {e}")
