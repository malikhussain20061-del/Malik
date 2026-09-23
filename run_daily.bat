@echo off
cd /d "%~dp0"
C:\Python312\python.exe daily_job.py >> daily_job_cron.log 2>&1
