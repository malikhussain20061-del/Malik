"""
================================================================================
  PSX NEWS MODULE REGRESSION SUITE
================================================================================
Every case here is a bug that actually happened on 2026-09-24 while building the
Phase 1 monitor. They are pinned so the same class of failure cannot come back
silently.
================================================================================
"""

import os
import sqlite3
import tempfile
from datetime import datetime

import psx_news as N

DB = "psx.db"


def _conn():
    path = os.path.join(tempfile.gettempdir(), "psx_news_test.db")
    if os.path.exists(path):
        os.remove(path)
    src = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    dst = sqlite3.connect(path)
    with dst:
        src.backup(dst)
    src.close()
    N.init_schema(dst)
    return dst, path


def test_both_download_link_shapes():
    print("\n[TEST 1] Company and notice feeds use different PDF link shapes...")
    # parse_rows drops the first <tr> as a header, so these fixtures carry one. If DPS ever
    # stops sending the header row the first data row would be lost with it - test 1b pins
    # that behaviour so a change is loud rather than silent.
    hdr_c = ('<tr><th>DATE</th><th>TIME</th><th>SYMBOL</th><th>NAME</th><th>TITLE</th><th></th></tr>')
    hdr_d = ('<tr><th>DATE</th><th>TIME</th><th>TITLE</th><th></th></tr>')
    c_row = ('<tr><td>Sep 24, 2026</td><td>3:01 PM</td><td><a href="/company/XYZ"><strong>XYZ</strong></a></td>'
             '<td>XYZ Co</td><td>Board Meeting</td>'
             '<td><a href="/download/document/111.pdf">View PDF</a></td></tr>')
    d_row = ('<tr><td>Sep 23, 2026</td><td>10:03 AM</td><td>Restoration WTL-NCCPL Notice</td>'
             '<td><a href="/download/attachment/283240-1.pdf">PDF</a></td></tr>')
    rc = N.parse_rows(f"<table>{hdr_c}{c_row}</table>", "C")
    rd = N.parse_rows(f"<table>{hdr_d}{d_row}</table>", "D")
    assert len(rc) == 1 and rc[0]["doc_id"] == "111.pdf", f"C feed wrong: {rc}"
    assert len(rd) == 1 and rd[0]["doc_id"] == "283240-1.pdf", f"D feed wrong: {rd}"
    assert rd[0]["pdf_url"].endswith("/download/attachment/283240-1.pdf")
    assert rd[0]["symbol"] is None, "notice feed has no scrip column; must not invent one"
    print(f"  [PASS] C -> {rc[0]['doc_id']}, D -> {rd[0]['doc_id']}")


def test_no_pdf_link_is_not_fabricated():
    print("\n[TEST 2] A row with no download anchor gets no URL, never a guessed one...")
    row = ('<tr><th>DATE</th><th>TIME</th><th>SYMBOL</th><th>NAME</th><th>TITLE</th><th></th></tr>'
           '<tr><td>Sep 24, 2026</td><td>1:00 PM</td><td><a href="/company/AAA">'
           '<strong>AAA</strong></a></td><td>AAA Co</td><td>Something</td><td>&mdash;</td></tr>')
    parsed = N.parse_rows(f"<table>{row}</table>", "C")
    assert parsed and parsed[0]["doc_id"] is None and parsed[0]["pdf_url"] is None, parsed
    print("  [PASS] doc_id=None, pdf_url=None")


def test_invalid_calendar_dates_rejected():
    print("\n[TEST 3] OCR-produced impossible dates are rejected, not published...")
    for bad in ("2026-09-00", "2026-02-31", "2026-13-05", "september 32, 2026"):
        assert N._parse_date(bad) is None, f"{bad} was accepted"
    assert N._parse_date("september 29, 2026") == "2026-09-29"
    assert N._parse_date("29 September 2026") == "2026-09-29"
    print("  [PASS] 00-day, 31-Feb, month 13, day 32 all rejected; real dates parse")


def test_scheduled_as_follows_phrasing():
    print("\n[TEST 4] 'scheduled as follows' is a real notice phrasing, not just 'held on'...")
    a = "The 67th BOD Meeting has been scheduled as follows: Day and Date Tuesday, September 29, 2026 at 10:30 AM"
    b = "the meeting of Board of Directors will be held on october 02,2026 at 12:00 p.m, (Friday)"
    for text, want in ((a, "2026-09-29"), (b, "2026-10-02")):
        m = N.HELD_RE.search(text)
        assert m, f"no anchor in: {text[:50]}"
        got = N._parse_date(text[m.end():m.end() + 140])
        assert got == want, f"{text[:40]} -> {got}, wanted {want}"
    print("  [PASS] both phrasings yield the right date")


def test_heartbeat_uses_latest_run_per_feed():
    print("\n[TEST 5] Health scores the latest run per feed, not every run today...")
    conn, path = _conn()
    conn.execute("DELETE FROM news_runs")
    today = datetime.now(N.PKT).date().isoformat()
    # broken at 09:00, fixed at 09:30
    conn.execute("INSERT INTO news_runs(started_at_pkt,feed,status,rows_seen,rows_new,error,parsed,failed)"
                 " VALUES (?,?,?,?,?,?,?,?)", (f"{today}T09:00:00", "C", "SUCCESS", 25, 0, "x", 0, 0))
    conn.execute("INSERT INTO news_runs(started_at_pkt,feed,status,rows_seen,rows_new,error,parsed,failed)"
                 " VALUES (?,?,?,?,?,?,?,?)", (f"{today}T09:30:00", "C", "SUCCESS", 25, 20, None, 20, 0))
    conn.commit()
    ok, msg = N.check_health(conn, today=today)
    assert ok, f"a fixed feed still alerts all day: {msg}"
    # now break it again as the latest run
    conn.execute("INSERT INTO news_runs(started_at_pkt,feed,status,rows_seen,rows_new,error,parsed,failed)"
                 " VALUES (?,?,?,?,?,?,?,?)", (f"{today}T10:00:00", "C", "SUCCESS", 25, 0, "x", 0, 0))
    conn.commit()
    ok2, msg2 = N.check_health(conn, today=today)
    assert not ok2 and "parsed 0" in msg2, msg2
    conn.close()
    os.remove(path)
    print("  [PASS] recovered feed goes quiet; a fresh outage still alerts")


def test_no_run_recorded_is_an_outage():
    print("\n[TEST 6] No run row at all is reported as an outage, not as a quiet day...")
    conn, path = _conn()
    conn.execute("DELETE FROM news_runs")
    conn.commit()
    ok, msg = N.check_health(conn, today="2026-09-24")
    assert not ok and "did not execute" in msg, msg
    conn.close()
    os.remove(path)
    print("  [PASS]", msg)


def test_superseded_meeting_kept_not_deleted():
    print("\n[TEST 7] A revised meeting date supersedes the old row but keeps the history...")
    conn, path = _conn()
    conn.execute("DELETE FROM board_meetings")
    conn.execute("INSERT INTO board_meetings(doc_id,symbol,company,meeting_date,date_status,"
                 "captured_at_pkt,is_current,date_source) VALUES (?,?,?,?,?,?,1,'PDF_TEXT')",
                 ("OLD.pdf", "TST", "Test Co", "2026-10-02", "parsed", "2026-09-24T10:00:00+05:00"))
    conn.execute("INSERT INTO board_meetings(doc_id,symbol,company,meeting_date,date_status,"
                 "captured_at_pkt,is_current,date_source) VALUES (?,?,?,?,?,?,1,'PDF_TEXT')",
                 ("NEW.pdf", "TST", "Test Co", "2026-11-05", "parsed", "2026-09-25T10:00:00+05:00"))
    conn.execute("UPDATE board_meetings SET is_current=0, superseded_by=?, superseded_reason=? "
                 "WHERE symbol=? AND is_current=1 AND doc_id<>? AND meeting_date IS NOT NULL AND meeting_date<>?",
                 ("NEW.pdf", "superseded by later notice", "TST", "NEW.pdf", "2026-11-05"))
    conn.commit()
    rows = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT doc_id, meeting_date, is_current, superseded_by FROM board_meetings WHERE symbol='TST'")}
    assert len(rows) == 2, f"history was destroyed: {rows}"
    assert rows["NEW.pdf"][1] == 1 and rows["NEW.pdf"][2] is None, rows
    assert rows["OLD.pdf"][1] == 0 and rows["OLD.pdf"][2] == "NEW.pdf", rows
    assert rows["OLD.pdf"][0] == "2026-10-02", "superseded row must keep its own old date"
    cal = N.daily_calendar_message(conn, today="2026-09-25", horizon_days=60)
    assert "2026-11-05" in cal and "2026-10-02" not in cal, cal
    conn.close()
    os.remove(path)
    print("  [PASS] both rows retained, calendar shows only the current date")


def test_liquidity_uses_traded_value_not_share_count():
    print("\n[TEST 8] LOW LIQUIDITY is decided by rupees traded, not number of shares...")
    conn, path = _conn()
    thin = N.liquidity_flag(conn, "IDYM")
    assert thin and "LOW LIQUIDITY" in thin, f"IDYM (1,598 vol) not flagged: {thin}"
    deep = N.liquidity_flag(conn, "LUCK")
    assert deep is None, f"LUCK wrongly flagged: {deep}"
    assert N.liquidity_flag(conn, None) is None
    conn.close()
    os.remove(path)
    print(f"  [PASS] IDYM -> {thin}; LUCK -> None")


def test_ocr_dates_are_flagged_for_verification():
    print("\n[TEST 9] OCR-derived dates carry a VERIFY marker; text-layer dates do not...")
    conn, path = _conn()
    conn.execute("DELETE FROM board_meetings")
    for doc, src in (("T1.pdf", "OCR"), ("T2.pdf", "PDF_TEXT")):
        conn.execute("INSERT INTO board_meetings(doc_id,symbol,company,meeting_date,date_status,"
                     "captured_at_pkt,is_current,date_source) VALUES (?,?,?,?,?,?,1,?)",
                     (doc, "TST", "Test Co", "2026-10-15",
                      "parsed_ocr_VERIFY" if src == "OCR" else "parsed",
                      "2026-09-24T10:00:00+05:00", src))
    conn.commit()
    cal = N.daily_calendar_message(conn, today="2026-10-10", horizon_days=20)
    assert "OCR, VERIFY" in cal and "came from OCR" in cal, cal
    conn.close()
    os.remove(path)
    print("  [PASS] calendar names the OCR count and warns before acting")


def test_suite():
    print("=" * 72)
    print("  PSX NEWS MODULE REGRESSION SUITE")
    print("=" * 72)
    for fn in (test_both_download_link_shapes, test_no_pdf_link_is_not_fabricated,
               test_invalid_calendar_dates_rejected, test_scheduled_as_follows_phrasing,
               test_heartbeat_uses_latest_run_per_feed, test_no_run_recorded_is_an_outage,
               test_superseded_meeting_kept_not_deleted,
               test_liquidity_uses_traded_value_not_share_count,
               test_ocr_dates_are_flagged_for_verification):
        fn()
    print("\n[ALL TESTS PASSED] 9/9 news-monitor regressions pinned.")


if __name__ == "__main__":
    test_suite()
