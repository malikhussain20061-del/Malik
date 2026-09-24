"""psx_news.py - Phase 1 public announcement capture, board-meeting calendar and alerts.

INFORMATION ONLY. Nothing here places an order, and no message it produces is buy or sell
advice. Every alert is labelled with its confidence class.

Data legitimacy (checked, not assumed):
  * feeds are the same public DPS endpoint this repo already used in psx_engine.py
  * dps.psx.com.pk serves no robots.txt (404); www.psx.com.pk allows all but /cgi-bin/
  * a robots file is not a licence: PSX's terms of use were NOT reviewed here, so this module
    stays polite - one request per feed per run, serial, with a delay, public data only,
    no authentication and no login-gated content
  * the PDF link is taken from the row's own /download/document/<id>.pdf anchor. The visible
    "View PDF" anchor is href="javascript:" and is NOT a link; using it would fabricate a URL.

Failure policy: exceptions propagate and are recorded in news_runs. A silent empty list would
make a dead feed look like a quiet market, which is the exact mistake the MTS feed made.
"""

from __future__ import annotations

import gzip
import hashlib
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

PKT = ZoneInfo("Asia/Karachi")
BASE = "https://dps.psx.com.pk"
ANNOUNCEMENTS_URL = f"{BASE}/announcements"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "X-Requested-With": "XMLHttpRequest"}
RAW_DIR = Path("raw_archive")

# Values read off the page's own <select name="type">, not invented.
FEEDS = {"A": "CDC Notices", "B": "SECP Notices", "C": "Companies Announcements",
         "D": "NCCPL Notices", "E": "PSX Notices"}

CONFIDENCE = "sirf khabar - buy/sell advice nahi"

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements(
  doc_id        TEXT PRIMARY KEY,
  feed          TEXT NOT NULL,
  feed_label    TEXT NOT NULL,
  symbol        TEXT,
  company       TEXT,
  title         TEXT NOT NULL,
  announced_at_pkt TEXT,
  pdf_url       TEXT,
  pdf_sha256    TEXT,
  raw_sha256    TEXT,
  captured_at_pkt TEXT NOT NULL,
  alerted       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS board_meetings(
  doc_id        TEXT PRIMARY KEY REFERENCES announcements(doc_id),
  symbol        TEXT,
  company       TEXT,
  meeting_date  TEXT,
  meeting_time  TEXT,
  agenda        TEXT,
  close_period_from TEXT,
  close_period_to   TEXT,
  date_status   TEXT NOT NULL,
  captured_at_pkt TEXT NOT NULL,
  reminded_1d   INTEGER NOT NULL DEFAULT 0,
  reminded_day  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS news_runs(
  started_at_pkt TEXT NOT NULL,
  feed           TEXT NOT NULL,
  status         TEXT NOT NULL,
  rows_seen      INTEGER NOT NULL DEFAULT 0,
  rows_new       INTEGER NOT NULL DEFAULT 0,
  error          TEXT
);
"""

MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december"
          "|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec")
DATE_RE = re.compile(rf"([0-2]?\d)(?:st|nd|rd|th)?[\s/.,-]*({MONTHS})\.?[\s,]*((?:19|20)\d{{2}})",
                     re.I)
DATE_RE2 = re.compile(rf"({MONTHS})\.?[\s,]*([0-2]?\d)(?:st|nd|rd|th)?[\s,]*((?:19|20)\d{{2}})", re.I)
HELD_RE = re.compile(r"(?:will be held on|is being held on|has been held on|held on)\b", re.I)
TIME_RE = re.compile(r"(\d{1,2}\s*:\s*\d{2}\s*[ap]\.?\s*m\.?)", re.I)
AGENDA_RE = re.compile(r"(?:to consider and|following agenda[:\-]?|agenda)\s*([^.;{]{10,260})", re.I)
CLOSE_RE = re.compile(r"close\s*period", re.I)
DATE_SCAN_RE = re.compile(
    rf"\d{{1,2}}(?:st|nd|rd|th)?\s*[-/.,]*\s*(?:{MONTHS})\.?\s*[,]?\s*(?:19|20)\d{{2}}"
    rf"|(?:{MONTHS})\.?\s*\d{{1,2}}(?:st|nd|rd|th)?\s*[,]?\s*(?:19|20)\d{{2}}", re.I)


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> str:
    return datetime.now(PKT).isoformat(timespec="seconds")


def archive_raw(tag: str, content: bytes) -> str:
    """Same discipline as psx_data_v2: keep every raw payload so a parser fix can replay it."""
    RAW_DIR.mkdir(exist_ok=True)
    sha = hashlib.sha256(content).hexdigest()
    stamp = datetime.now(PKT).strftime("%Y%m%d_%H%M%S")
    (RAW_DIR / f"{tag}_{stamp}.gz").write_bytes(gzip.compress(content))
    return sha


def _get(url: str, payload: dict | None = None, timeout: int = 25) -> bytes:
    data = urllib.parse.urlencode(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _parse_date(text: str) -> str | None:
    m = DATE_RE.search(text)
    if m:
        day, mon, year = m.group(1), m.group(2), m.group(3)
    else:
        m = DATE_RE2.search(text)
        if not m:
            return None
        mon, day, year = m.group(1), m.group(2), m.group(3)
    mon = mon.rstrip(".").lower()
    names = {n: i + 1 for i, n in enumerate(
        "january february march april may june july august september october november december".split())}
    num = names.get(mon) or names.get(next((k for k in names if k.startswith(mon[:3])), ""), None)
    if not num:
        return None
    try:
        return f"{int(year):04d}-{num:02d}-{int(day):02d}"
    except ValueError:
        return None


def parse_rows(html: str, feed: str) -> list[dict]:
    """Type C rows carry symbol+company; A/B/D/E rows do not. Both shapes are handled."""
    label = FEEDS[feed]
    out = []
    for r in BeautifulSoup(html, "html.parser").find_all("tr")[1:]:
        cells = [c.get_text(" ", strip=True) for c in r.find_all("td")]
        if len(cells) < 4:
            continue
        anchors = [a for a in r.find_all("a") if str(a.get("href", "")).startswith("/download/document/")]
        pdf = f"{BASE}{anchors[0]['href']}" if anchors else None
        doc_id = anchors[0]["href"].rsplit("/", 1)[-1] if anchors else None
        sym_anchors = [a for a in r.find_all("a") if str(a.get("href", "")).startswith("/company/")]
        if len(cells) >= 5 and (cells[2] or cells[3]):
            when = f"{cells[0]} {cells[1]}"
            out.append({"doc_id": doc_id, "feed": feed, "feed_label": label, "symbol": cells[2],
                        "company": cells[3], "title": cells[4], "when": when,
                        "pdf_url": pdf, "scrip_href": sym_anchors[0]["href"] if sym_anchors else None})
        else:
            out.append({"doc_id": doc_id, "feed": feed, "feed_label": label, "symbol": None,
                        "company": None, "title": cells[2], "when": f"{cells[0]} {cells[1]}",
                        "pdf_url": pdf, "scrip_href": None})
    return out


def fetch_feed(conn, feed: str, count: int = 40) -> tuple[int, int]:
    payload = {"type": feed, "symbol": "", "query": "", "count": count, "offset": 0,
               "date_from": "", "date_to": "", "page": "all"}
    started = _now()
    try:
        raw = _get(ANNOUNCEMENTS_URL, payload)
    except Exception as e:
        conn.execute("INSERT INTO news_runs VALUES (?,?,?,?,?,?)",
                     (started, feed, "FAILED", 0, 0, f"{type(e).__name__}: {e}"[:500]))
        conn.commit()
        raise
    raw_sha = archive_raw(f"announcements_{feed}", raw)
    rows = parse_rows(raw.decode("utf-8", "ignore"), feed)
    new = skipped = 0
    for r in rows:
        if not r["doc_id"]:
            skipped += 1
            continue
        cur = conn.execute("INSERT OR IGNORE INTO announcements(doc_id, feed, feed_label, symbol, "
                           "company, title, announced_at_pkt, pdf_url, raw_sha256, captured_at_pkt) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (r["doc_id"], r["feed"], r["feed_label"], r["symbol"] or None,
                            r["company"] or None, r["title"], _normalise_when(r["when"]),
                            r["pdf_url"], raw_sha, _now()))
        new += cur.rowcount
    conn.execute("INSERT INTO news_runs VALUES (?,?,?,?,?,?)",
                 (started, feed, "SUCCESS", len(rows), new, None))
    conn.commit()
    return len(rows), new


def _normalise_when(when: str) -> str | None:
    d = _parse_date(when)
    if not d:
        return None
    t = TIME_RE.search(when)
    return f"{d} {t.group(1)}" if t else d


def parse_board_meeting(pdf_bytes: bytes) -> dict:
    """Extract what the notice actually says. Unparseable fields stay None - never guessed.

    OCR on these scanned notices is poor ("Genera! Manager", "october 02,2026"), so each
    field is read from a bounded window after its own anchor phrase rather than from a
    sentence regex: the window has to survive periods inside "12:00 p.m." and the garbled
    "2026tooctober" run-together in close-period lines.
    """
    import pymupdf
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    text = re.sub(r"\s+", " ", " ".join(p.get_text() for p in doc))
    if not text.strip():
        # Several PSX notices are image-only scans with no text layer. Saying "no held
        # phrase" would blame the wording when the real cause is that there is no text to
        # read at all, and would hide that OCR is the missing capability.
        return {"meeting_date": None, "meeting_time": None, "agenda": None,
                "close_from": None, "close_to": None,
                "date_status": "no_text_layer_needs_ocr"}

    held = HELD_RE.search(text)
    window = text[held.end():held.end() + 90] if held else ""
    meeting_date = _parse_date(window) if held else None
    tmatch = TIME_RE.search(window)

    agenda = AGENDA_RE.search(text)
    close_from = close_to = None
    close = CLOSE_RE.search(text)
    if close:
        # "from <date> to <date>", where OCR glues the two together ("2026tooctober").
        tail = text[close.end():close.end() + 220]
        # finditer, not findall: the pattern has capture groups, so findall would return the
        # month name alone and _parse_date would see an incomplete date.
        dates = [d for d in (_parse_date(m.group(0)) for m in DATE_SCAN_RE.finditer(tail)) if d]
        if len(dates) >= 2:
            close_from, close_to = dates[0], dates[-1]
        elif dates:
            close_from = dates[0]

    return {"meeting_date": meeting_date,
            "meeting_time": tmatch.group(1) if tmatch else None,
            "agenda": agenda.group(1).strip() if agenda else None,
            "close_from": close_from, "close_to": close_to,
            "date_status": "parsed" if meeting_date else ("no_held_phrase" if not held
                                                          else "date_unparseable")}


BOARD_TITLE_RE = re.compile(r"board\s*meeting|BOD\s*meeting|meeting\s*of\s*the\s*board", re.I)


def update_board_meetings(conn, limit: int = 6) -> list[dict]:
    """Notices whose own title announces a board meeting.

    Matching only the exact string 'Board Meeting' silently missed real notices titled
    "Notice of the 67th BOD meeting" and "Board Meeting Intimation", so the title is kept
    in the row and matched case-insensitively instead.
    """
    todo = []
    for doc_id, sym, comp, url, title in conn.execute(
            "SELECT a.doc_id, a.symbol, a.company, a.pdf_url, a.title FROM announcements a "
            "LEFT JOIN board_meetings b ON b.doc_id = a.doc_id "
            "WHERE b.doc_id IS NULL AND a.title IS NOT NULL "
            "ORDER BY a.captured_at_pkt DESC LIMIT 400"):
        if BOARD_TITLE_RE.search(title):
            todo.append((doc_id, sym, comp, url))
        if len(todo) >= limit:
            break
    made = []
    for doc_id, sym, comp, url in todo:
        if not url:
            conn.execute("INSERT OR REPLACE INTO board_meetings VALUES (?,?,?,?,?,?,?,?,?,?,0,0)",
                         (doc_id, sym, comp, None, None, None, None, None, "no_pdf_link", _now()))
            continue
        try:
            b = _get(url, timeout=30)
            sha = archive_raw(f"board_meeting_{doc_id}", b)
            p = parse_board_meeting(b)
        except Exception as e:
            conn.execute("INSERT OR REPLACE INTO board_meetings VALUES (?,?,?,?,?,?,?,?,?,?,0,0)",
                         (doc_id, sym, comp, None, None, None, None, None,
                          f"failed:{type(e).__name__}:{str(e)[:120]}"[:60], _now()))
            conn.commit()
            time.sleep(1.0)
            continue
        conn.execute("UPDATE announcements SET pdf_sha256=? WHERE doc_id=?", (sha, doc_id))
        conn.execute("INSERT OR REPLACE INTO board_meetings VALUES (?,?,?,?,?,?,?,?,?,?,0,0)",
                     (doc_id, sym, comp, p["meeting_date"], p["meeting_time"], p["agenda"],
                      p["close_from"], p["close_to"], p["date_status"], _now()))
        made.append({"doc_id": doc_id, "symbol": sym, "company": comp, **p})
        conn.commit()
        time.sleep(1.0)
    return made


def price_context(conn, symbol: str | None) -> str:
    if not symbol:
        return "n/a (feed has no scrip)"
    row = conn.execute("SELECT trade_date, close, volume FROM daily_quotes WHERE base_symbol=? "
                       "AND is_final=1 ORDER BY trade_date DESC LIMIT 1", (symbol,)).fetchone()
    if not row:
        return f"{symbol}: no quote history found"
    return f"{symbol} last final quote {row[0]}: close Rs {row[1]:,.2f}, vol {row[2]:,.0f}"


def new_alerts(conn, limit: int = 25) -> list[str]:
    out = []
    for (doc_id, label, sym, comp, title, when, pdf, cap) in conn.execute(
            "SELECT doc_id, feed_label, symbol, company, title, announced_at_pkt, pdf_url, "
            "captured_at_pkt FROM announcements WHERE alerted=0 "
            "ORDER BY captured_at_pkt DESC LIMIT ?", (limit,)):
        body = (f"[PSX NEWS] {label}\n"
                f"{comp or sym or '-'} ({sym or 'no scrip'})\n"
                f"{title}\n"
                f"announced: {when or 'not stated in row'}\n"
                f"captured : {cap} PKT\n"
                f"context  : {price_context(conn, sym)}\n"
                f"source   : {pdf or 'no PDF link published in row'}\n"
                f"confidence: untested observation | {CONFIDENCE}")
        out.append(body)
        conn.execute("UPDATE announcements SET alerted=1 WHERE doc_id=?", (doc_id,))
    conn.commit()
    return out


def due_reminders(conn, today: str | None = None) -> list[str]:
    today = today or datetime.now(PKT).date().isoformat()
    tomorrow = (datetime.fromisoformat(today) + timedelta(days=1)).date().isoformat()
    out = []
    for kind, day, flag in (("T-1", tomorrow, "reminded_1d"), ("TODAY", today, "reminded_day")):
        for row in conn.execute(
                f"SELECT doc_id, symbol, company, meeting_date, meeting_time, agenda, "
                f"close_period_from, close_period_to FROM board_meetings "
                f"WHERE meeting_date=? AND {flag}=0", (day,)):
            out.append(f"[BOARD MEETING {kind}] {row[2]} ({row[1]})\n"
                       f"meeting: {row[3]} {row[4] or ''}\n"
                       f"agenda : {row[5] or 'not extracted'}\n"
                       f"close period: {row[6] or '?'} .. {row[7] or '?'}\n"
                       f"confidence: tested fact from the notice PDF | {CONFIDENCE}")
            conn.execute(f"UPDATE board_meetings SET {flag}=1 WHERE doc_id=?", (row[0],))
    conn.commit()
    return out


def send_all(messages: list[str], notifier=None) -> int:
    """notifier(text) is injected so this module never needs Telegram credentials to be testable."""
    if notifier is None:
        from daily_job import send_alert
        notifier = lambda t: send_alert("PSX_NEWS", t)
    for m in messages:
        notifier(m)
    return len(messages)


def run(conn: sqlite3.Connection | None = None, count: int = 40, delay: float = 2.0,
        notifier=None) -> dict:
    own = conn is None
    conn = conn or sqlite3.connect("psx.db")
    init_schema(conn)
    seen = new = 0
    for feed in FEEDS:
        s, n = fetch_feed(conn, feed, count=count)
        seen += s
        new += n
        time.sleep(delay)
    meetings = update_board_meetings(conn)
    msgs = new_alerts(conn) + [m for m in due_reminders(conn)]
    sent = send_all(msgs, notifier) if msgs else 0
    if own:
        conn.close()
    return {"rows_seen": seen, "rows_new": new, "meetings_parsed": len(meetings),
            "alerts": len(msgs), "sent": sent}


def session_returns(conn, symbol: str, on_date: str, back: int = 5, fwd: int = 5):
    """Cumulative scrip return vs the same window for every final quote in the market.

    Returns (scrip_pct, market_median_pct) or (None, None) when the window is incomplete.
    Quoting a raw return without the market move would credit a good day to the notice.
    """
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM daily_quotes WHERE is_final=1 ORDER BY trade_date")]
    if on_date not in days:
        return None, None
    i = days.index(on_date)
    lo, hi = max(0, i - back), min(len(days) - 1, i + fwd)
    if i - lo < 1 or hi - i < 1:
        return None, None

    def window(sym: str | None, a: int, b: int):
        sql = ("SELECT trade_date, close FROM daily_quotes WHERE is_final=1 AND close>0 "
               + ("AND base_symbol=?" if sym else "") + " AND trade_date BETWEEN ? AND ? "
               "ORDER BY trade_date")
        rows = conn.execute(sql, (sym, days[a], days[b]) if sym else (days[a], days[b])).fetchall()
        return rows

    scrip = window(symbol, i - back, i)
    if len(scrip) < 2:
        return None, None
    s_ret = (scrip[-1][1] / scrip[0][1] - 1.0) * 100
    per_sym = {}
    for sym in {r[0] for r in conn.execute(
            "SELECT DISTINCT base_symbol FROM daily_quotes WHERE is_final=1 "
            "AND trade_date BETWEEN ? AND ?", (days[i - back], days[i]))}:
        w = window(sym, i - back, i)
        if len(w) >= 2 and w[0][1]:
            per_sym[sym] = (w[-1][1] / w[0][1] - 1.0) * 100
    if len(per_sym) < 20:
        return round(s_ret, 2), None
    vals = sorted(per_sym.values())
    return round(s_ret, 2), round(vals[len(vals) // 2], 2)


def meeting_history(conn, symbol: str, limit: int = 8) -> list[dict]:
    """Past board meetings for this scrip and how it actually moved around them.

    Only meetings whose date we parsed from the notice itself are used; anything that needs
    OCR is reported as such rather than guessed, so the sample is smaller but real.
    """
    out = []
    for doc_id, mdate, agenda in conn.execute(
            "SELECT doc_id, meeting_date, agenda FROM board_meetings "
            "WHERE symbol=? AND meeting_date IS NOT NULL ORDER BY meeting_date DESC LIMIT ?",
            (symbol, limit)):
        prior = conn.execute(
            "SELECT amount, ex_date FROM corporate_actions WHERE base_symbol=? "
            "AND action_type='CASH' AND ex_date<? ORDER BY ex_date DESC LIMIT 1",
            (symbol, mdate)).fetchone()
        s_ret, m_ret = session_returns(conn, symbol, mdate)
        out.append({"doc_id": doc_id, "meeting_date": mdate, "agenda": (agenda or "")[:90],
                    "prev_cash": prior[0] if prior else None, "prev_ex_date": prior[0:2][1] if prior else None,
                    "ret_5d_before_pct": s_ret, "market_median_same_window_pct": m_ret})
    return out


def dividend_context(conn, symbol: str | None) -> str:
    """The company's own declared cash history, from corporate_actions. No forecast."""
    if not symbol:
        return "no scrip on this notice"
    rows = conn.execute("SELECT ex_date, amount FROM corporate_actions WHERE base_symbol=? "
                        "AND action_type='CASH' AND amount IS NOT NULL ORDER BY ex_date DESC LIMIT 4",
                        (symbol,)).fetchall()
    if not rows:
        return f"{symbol}: no cash dividend recorded in our corporate_actions table"
    return f"{symbol} declared cash: " + ", ".join(f"Rs {a:,.2f} ex {d}" for d, a in rows)


def daily_calendar_message(conn, horizon_days: int = 10, today: str | None = None) -> str:
    from datetime import date, timedelta
    today = today or datetime.now(PKT).date().isoformat()
    until = (date.fromisoformat(today) + timedelta(days=horizon_days)).isoformat()
    rows = conn.execute(
        "SELECT meeting_date, symbol, company, meeting_time, agenda FROM board_meetings "
        "WHERE meeting_date BETWEEN ? AND ? ORDER BY meeting_date, symbol", (today, until)).fetchall()
    unparsed = conn.execute(
        "SELECT COUNT(*) FROM board_meetings WHERE meeting_date IS NULL").fetchone()[0]
    lines = [f"[BOARD CALENDAR] {today} .. {until}  ({len(rows)} meetings with a parsed date)"]
    for d, sym, comp, t, ag in rows:
        lines.append(f"  {d}  {sym or '-':10} {t or 'time n/a'}  {(ag or 'agenda not extracted')[:70]}")
    if unparsed:
        lines.append(f"  note: {unparsed} notice(s) have no machine-readable date yet "
                     f"(image-only scans, need OCR). Not guessed.")
    lines.append(f"confidence: tested facts from notice PDFs | {CONFIDENCE}")
    return "\n".join(lines)
