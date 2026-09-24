# PSX system walkthrough

Three separate things run from this folder. They do not share data, and only one of them is a
registered research hypothesis.

---

## DATA ACCESS: PERMISSION PENDING

**Read this before adding any new poller or scraper.**

PSX Terms of Use — `https://www.psx.com.pk/psx/terms-of-use`, "Proprietary Rights" — prohibit,
without **written** permission:
- running robots, spiders or similar software against the site,
- "systematic retrieval" of content in order to create a database,
- redistributing or transmitting the data to anyone else.

Only a download for **personal, non-commercial** use is granted by default.

This covers every feed this project uses, not just intraday:
`psx_news.py` (announcements), `psx_data_v2.py` (end-of-day quotes, FIPI/LIPI), and the MTS
report fetch. `robots.txt` was checked first and is not a defence: `dps.psx.com.pk` serves no
robots.txt at all (404) and `www.psx.com.pk` allows everything except `/cgi-bin/`, but a
robots file speaks for crawlers, not for the contract in the Terms of Use.

**A permission request was emailed to `marketdatarequest@psx.com.pk`.** Until that is answered:

| | Status |
|---|---|
| Announcements monitor | running, 5-minute cadence, trading days only — do not make it faster |
| End-of-day quotes | running, once per day after the close |
| Intraday market-watch snapshots | **DISABLED** — `intraday_archive.py` refuses to run |
| Redistribution / publishing of any of it | **not done, not to be done** |

If PSX declines or points to a licensed vendor, that vendor becomes the source and the polling
code should be pointed at it rather than at the website.

---

## 1. MTS crowding hypothesis — `MTS_CROWDING_XS_H10_V3`

The only registered research test. Pre-registered, hashed, and currently **postponed**.

- `run_mts_h1.py` holds `SPEC`, the registered spec. Its SHA-256 plus the SHA-256 of every file
  in `CODE_FILES` are recorded in the `hypothesis_amendments` ledger. Editing any of those eight
  files makes `verify_registration()` return `code_ok: False`, so a change cannot be made
  quietly — it has to be registered as an amendment first.
- `daily_job.py` runs at 18:30 PKT: ingest quotes, FIPI/LIPI and the MTS report → run the
  pipeline → back up to three mirrors → run `guard_drill.py`.
- Shadow mode only until `SPEC["evaluation_start"]` is committed by a pre-data amendment. It is
  `None` today, which is the postponement: the 240-session clock starts only after a verified
  NCCPL primary source delivers 5 consecutive clean sessions.

**Amendments #12-#18** (all pre-data, all with zero live sessions observed) fixed, in order: a
PDF reader that could not parse the MTS report at all, 8 real tickers filed under phantom
sector/quote keys, a rebuild that erased VOID links, a phantom member of the eligible
universe, 27 sectors being counted as 39, and the power figures being re-derived on the
corrected data. Each is described in the ledger with its diff.

`guard_drill.py` is the daily proof: 1 positive run plus 6 negative tampering drills, on an
isolated copy of the database. It is also wired into `daily_job.py`, because shadow mode
returns before `verify_registration` and would otherwise never check the code hash.

## 2. News and board-meeting monitor — `psx_news.py`, `news_job.py`

Information only. Places no orders, and every message is labelled
`sirf khabar - buy/sell advice nahi`.

- Five DPS announcement feeds: A=CDC, B=SECP, C=Companies, D=NCCPL, E=PSX.
- Every raw page and every notice PDF is archived gzip + SHA-256, so a parser fix can replay
  history instead of depending on a live page that has since changed.
- Board meeting dates come from the notice text, never from the announcement date. Notices that
  are image-only scans go through OCR, and **every OCR date is labelled for manual
  verification** — OCR once produced `2026-09-00`, a day that does not exist. Impossible dates
  are now rejected outright.
- `check_health()` is the point of the whole module: a feed that fetched rows and stored zero,
  or that has not run at all, is an outage and alerts. It caught a real bug on its first run —
  four of the five feeds use `/download/attachment/<id>-1.pdf` while company announcements use
  `/download/document/<id>.pdf`, so those four were storing nothing.
- Meeting revisions keep the superseded row (`is_current=0`, `superseded_by`), so what was
  announced when stays auditable.
- Liquidity is judged on 20-session average **traded value**, not share count.
- The market regime figure is an equal-weight **PROXY** and says so. It is not KSE-100. The real
  index level is published on the PSX homepage and `dps.psx.com.pk` root, not on market-watch;
  reading it is pending the same permission.

**PSX Regulations Chapter 5, clause 5.9.2** (regulations dated 09-Feb-2026) requires a listed
company to notify the exchange **at least one week in advance** of a board meeting called for
quarterly or annual accounts, or to declare any entitlement to security holders. The calendar
splits meetings on that line: the 7-day rule applies to ACCOUNTS/ENTITLEMENT meetings, not to
every board meeting.

What is knowable is the **date and the agenda**. The dividend **amount** is not knowable in
advance, and anything claiming to know it before the board resolution is insider information.
This system does not use, store, or forward such claims.

## 3. Intraday collection — `intraday_archive.py`

Built, measured, and then **disabled** when the Terms of Use were read. It exists so that the
work already done is not lost, not so that it can run.

The measurement that made it look worthwhile: existing raw captures show market-watch changing
during the session (13:30 / 14:17 / 14:40 on 2026-09-21 are three different SHA-256s) and static
after the close (18:30 and 18:57 on 2026-09-23 are identical). So a 5-minute cadence would have
captured real movement, and the module skips writing a page whose hash it already holds.

It now refuses to run unless `alerts_config.json` carries an explicit
`psx_written_permission` value recording that PSX granted it.

## Scheduled tasks

| Task | When | Runs |
|---|---|---|
| `PSX_MTS_Daily_Pipeline` | daily 18:30 | `daily_job.py` |
| `PSX_News_Monitor` | daily 09:00 | `news_job.py loop` |

Both use a plain daily trigger, not `/sc minute`. A minute schedule is bounded by a Duration
measured from the start boundary on the start date, which reported `Next Run Time: N/A` and
would quietly never fire again. The cadence lives inside the script instead.

Both are `InteractiveToken` until switched to "run whether user is logged on or not" in the
GUI, which needs the owner's own Windows password. Alert credentials are in
`alerts_config.json` (gitignored) rather than environment variables precisely because a
non-interactive task does not load user environment variables.

## Weekly report

`python news_job.py report` — runs, rows fetched, rows parsed, failures, duplicate check,
board-meeting date outcomes, how many dates came only from OCR, and how many of those a human
later confirmed or corrected. That last number is the one that decides whether OCR is usable.

After 1-2 weeks of real logs: no new modules until the report says the existing ones behave.
