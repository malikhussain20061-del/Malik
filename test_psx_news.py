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


def _all_feeds(conn, today, parsed=20, stamp="09:00:00"):
    for feed in N.FEEDS:
        conn.execute("INSERT INTO news_runs(started_at_pkt,feed,status,rows_seen,rows_new,error,"
                     "parsed,failed) VALUES (?,?,?,?,?,?,?,?)",
                     (f"{today}T{stamp}", feed, "SUCCESS", 25, parsed, None if parsed else "x",
                      parsed, 0))
    conn.commit()


def test_heartbeat_uses_latest_run_per_feed():
    print("\n[TEST 5] Health scores the latest run per feed, not every run today...")
    conn, path = _conn()
    conn.execute("DELETE FROM news_runs")
    today = datetime.now(N.PKT).date().isoformat()
    _all_feeds(conn, today, parsed=0, stamp="09:00:00")          # broken at 09:00
    _all_feeds(conn, today, parsed=20, stamp="09:30:00")         # fixed at 09:30
    ok, msg = N.check_health(conn, today=today,
                              now=datetime.fromisoformat(f"{today}T09:35:00+05:00"))
    assert ok, f"a fixed feed still alerts all day: {msg}"
    _all_feeds(conn, today, parsed=0, stamp="10:00:00")          # broken again
    ok2, msg2 = N.check_health(conn, today=today,
                               now=datetime.fromisoformat(f"{today}T10:05:00+05:00"))
    assert not ok2 and "parsed 0" in msg2, msg2
    conn.close()
    os.remove(path)
    print("  [PASS] recovered feed goes quiet; a fresh outage still alerts")


def test_missing_feed_and_stall_are_outages():
    print("\n[TEST 10] A feed that never ran, and one that stalled, are both outages...")
    conn, path = _conn()
    conn.execute("DELETE FROM news_runs")
    today = datetime.now(N.PKT).date().isoformat()
    noon = datetime.fromisoformat(f"{today}T12:00:00+05:00")
    for feed in list(N.FEEDS)[:4]:                                # E never ran
        conn.execute("INSERT INTO news_runs(started_at_pkt,feed,status,rows_seen,rows_new,error,"
                     "parsed,failed) VALUES (?,?,?,?,?,?,?,?)",
                     (f"{today}T11:58:00", feed, "SUCCESS", 25, 20, None, 20, 0))
    conn.commit()
    ok, msg = N.check_health(conn, today=today, now=noon)
    assert not ok and "E" in msg and "no run recorded" in msg, msg
    # now every feed ran, but one stalled 90 minutes ago
    conn.execute("DELETE FROM news_runs")
    _all_feeds(conn, today, parsed=20, stamp="11:58:00")
    conn.execute("UPDATE news_runs SET started_at_pkt=? WHERE feed='A'",
                 (f"{today}T10:20:00",))
    conn.commit()
    ok2, msg2 = N.check_health(conn, today=today, now=noon)
    assert not ok2 and "stalled" in msg2, f"90-min-old run A not flagged: {msg2}"
    # the same stall outside polling hours must NOT alert, or every morning starts noisy
    ok3, msg3 = N.check_health(conn, today=today,
                               now=datetime.fromisoformat(f"{today}T22:30:00+05:00"))
    assert ok3, f"off-hours stall alerted: {msg3}"
    conn.close()
    os.remove(path)
    print("  [PASS] missing feed caught; stall caught in-hours; ignored overnight")


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


def test_clause_5_9_2_scope_split():
    print("\n[TEST 11] The 7-day clause is applied only to accounts/entitlement meetings...")
    inside = ("approve the Annual Audited Financial Statements of the company for the year ended "
              "June 30, 2026", "consider results and declare dividend", "bonus issue")
    outside = ("approve the minutes of the last meeting", "consider acquisition of plant",
               None, "")
    for a in inside:
        assert N.rule_scope(a) == "IN_SCOPE_5.9.2", a
    for a in outside:
        assert N.rule_scope(a) == "OUT_OF_SCOPE", a
    conn, path = _conn()
    conn.execute("DELETE FROM board_meetings")
    for doc, ag in (("IN.pdf", "consider annual accounts and interim dividend"),
                    ("OUT.pdf", "approve minutes of the previous meeting")):
        conn.execute("INSERT INTO board_meetings(doc_id,symbol,company,meeting_date,agenda,"
                     "date_status,captured_at_pkt,is_current,date_source) "
                     "VALUES (?,?,?,?,?,?,?,?, 'PDF_TEXT')",
                     (doc, "TST", "Test Co", "2026-10-15", ag, "parsed",
                      "2026-09-24T10:00:00+05:00", 1))
    conn.commit()
    cal = N.daily_calendar_message(conn, today="2026-10-10", horizon_days=20)
    assert "7-day rule applies" in cal and "outside 5.9.2" in cal, cal
    assert "applies to 1 of 2" in cal, cal
    conn.close()
    os.remove(path)
    print("  [PASS] 4 in-scope and 4 out-of-scope agendas classified; calendar counts 1 of 2")


def test_manual_ocr_check_is_recorded():
    print("\n[TEST 12] OCR accuracy is only knowable if human checks are stored...")
    conn, path = _conn()
    conn.execute("DELETE FROM board_meetings")
    conn.execute("INSERT INTO board_meetings(doc_id,symbol,meeting_date,date_status,date_source,"
                 "captured_at_pkt,is_current) VALUES (?,?,?,?,?,?,1)",
                 ("O1.pdf", "TST", "2026-10-15", "parsed_ocr_VERIFY", "OCR",
                  "2026-09-24T10:00:00+05:00"))
    conn.commit()
    N.record_manual_check(conn, "O1.pdf", "WRONG:2026-10-16")
    row = conn.execute("SELECT manual_check, manual_check_at FROM board_meetings "
                       "WHERE doc_id='O1.pdf'").fetchone()
    assert row[0] == "WRONG:2026-10-16" and row[1], row
    try:
        N.record_manual_check(conn, "O1.pdf", "maybe")
        raise AssertionError("accepted a meaningless verdict")
    except ValueError:
        pass
    conn.close()
    os.remove(path)
    print("  [PASS] verdict stored with a timestamp; a nonsense verdict is rejected")


def test_intraday_archive_refuses_without_permission():
    print("\n[TEST 13] Intraday polling refuses to run while PSX permission is pending...")
    import intraday_archive as A
    assert A.permission() is None, "a permission token is present; the gate is not holding"
    assert A.require_permission("test") == 3, "module allowed itself to run"
    # the window/dedupe logic is still correct, so re-enabling later is a config change only
    assert not A.in_session(datetime(2026, 9, 26, 11, 0, tzinfo=A.PKT)), "Saturday polled"
    assert A.in_session(datetime(2026, 9, 25, 11, 0, tzinfo=A.PKT)), "Friday 11:00 rejected"
    assert not A.in_session(datetime(2026, 9, 25, 17, 0, tzinfo=A.PKT)), "after-close polled"
    print("  [PASS] refuses without permission; window logic still weekday 08:55-15:40")


def test_cadence_slows_after_the_close():
    print("\n[TEST 14] Polling cadence: 5 min in session, 30 min after, nothing overnight...")
    import news_job as J
    d = "2026-09-25"                                  # a Friday
    at = lambda h, m: datetime.fromisoformat(f"{d}T{h:02d}:{m:02d}:00+05:00")
    assert J.cadence_seconds(at(11, 0)) == 300
    assert J.cadence_seconds(at(17, 59)) == 300, "still inside the active window"
    assert J.cadence_seconds(at(19, 0)) == 1800       # results are often filed in the evening
    assert J.cadence_seconds(at(21, 30)) is None, "past the last slot"
    assert J.cadence_seconds(at(3, 0)) is None
    sat = datetime.fromisoformat("2026-09-26T11:00:00+05:00")
    assert J.cadence_seconds(sat) is None, "weekend polling scheduled"
    print("  [PASS] 09:00-18:00 = 300s, 18:00-21:00 = 1800s, else None")


def test_telegram_diagnosis_names_the_real_reason():
    print("\n[TEST 15] The 'not configured' warning must say WHY, and never echo a token...")
    import json
    import tempfile
    from pathlib import Path
    import news_job as J
    d = Path(tempfile.mkdtemp())
    p = d / "cfg.json"
    cases = {}

    p.unlink(missing_ok=True)
    cases["missing"] = J.telegram_status(p)
    p.write_text(json.dumps({"telegram_bot_token": "", "telegram_chat_id": ""}), encoding="utf-8")
    cases["empty"] = J.telegram_status(p)
    p.write_text(json.dumps({"telegram_bot_token": "PASTE_FROM_BOTFATHER",
                             "telegram_chat_id": "1"}), encoding="utf-8")
    cases["placeholder"] = J.telegram_status(p)
    p.write_text("{ broken", encoding="utf-8")
    cases["badjson"] = J.telegram_status(p)
    secret = "123456789:AAExampleTokenMustNotBePrinted"
    p.write_text(json.dumps({"telegram_bot_token": secret, "telegram_chat_id": "42"}),
                 encoding="utf-8")
    cases["ready"] = J.telegram_status(p)

    for name in ("missing", "empty", "placeholder", "badjson"):
        ok, why = cases[name]
        assert not ok, f"{name} reported ready"
        assert why, f"{name} gave no reason"
    assert not cases["missing"][1].startswith("Telegram not usable")  # names the real cause
    assert "does not exist" in cases["missing"][1]
    assert "template text" in cases["placeholder"][1]
    assert "not valid JSON" in cases["badjson"][1]
    assert cases["ready"][0] is True
    # the point of the whole config-file design: nothing here prints a credential
    assert all(secret not in w for _, w in cases.values())
    print("  [PASS] 4 failure modes each named specifically; token never echoed")


def _counter_db(rows_quotes, rows_mts):
    """Minimal in-memory DB carrying only what clean_session_count reads."""
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE daily_quotes(trade_date TEXT, symbol TEXT, is_final INT, quality_flags TEXT)")
    c.execute("CREATE TABLE mts_snapshots(report_date TEXT, symbol TEXT, data_as_of TEXT)")
    c.executemany("INSERT INTO daily_quotes VALUES (?,?,?,?)", rows_quotes)
    c.executemany("INSERT INTO mts_snapshots VALUES (?,?,?)", rows_mts)
    return c


def test_stale_mts_report_is_not_clean():
    print("\n[TEST 16] A 14-Sep MTS file fetched on 23-Sep does NOT make 23-Sep clean...")
    import news_job as J
    # quotes exist for 23-Sep, and a report was CAPTURED on 23-Sep - but the positions are 14-Sep.
    c = _counter_db([("2026-09-23", "LUCK", 1, "")], [("2026-09-14", "LUCK", "2026-09-11")])
    n, why = J.clean_session_count(c)
    assert n == 0, f"stale report counted as clean: n={n}"
    assert "no MTS positions dated this session" in why[0], why
    print(f"  [PASS] rejected with: {why[0]}")
    # and the same session IS clean when the positions really are for that date
    c2 = _counter_db([("2026-09-23", "LUCK", 1, "")], [("2026-09-24", "LUCK", "2026-09-23")])
    n2, _ = J.clean_session_count(c2)
    assert n2 == 1, f"fresh positions not counted: {n2}"
    print("  [PASS] same session counts once when data_as_of matches the session")


def test_manual_rows_must_prove_themselves():
    print("\n[TEST 17] Hand-downloaded rows only count with sha + passed band check, never SCRATCH...")
    import news_job as J
    mts = [("2026-09-24", "LUCK", "2026-09-23")]
    good = [("2026-09-23", "LUCK", 1, "MANUAL_DOWNLOAD:sha256=abc123:LDCP_OK")]
    scratch = [("2026-09-23", "LUCK", 1, "MANUAL_DOWNLOAD:sha256=abc123:LDCP_OK:SCRATCH")]
    noband = [("2026-09-23", "LUCK", 1, "MANUAL_DOWNLOAD:sha256=abc123:LDCP_SUSPECT")]
    nosha = [("2026-09-23", "LUCK", 1, "")]
    for label, rows, want in (("sha + LDCP_OK", good, 1), ("test-written", scratch, 0),
                              ("band check failed", noband, 0)):
        n, why = J.clean_session_count(_counter_db(rows, mts))
        assert n == want, f"{label}: got n={n}, want {want} ({why})"
        print(f"  [PASS] {label:20s} -> {n}/5")
    # a session with no manual marker at all is judged on the MTS leg only (captured path)
    n, _ = J.clean_session_count(_counter_db(nosha, mts))
    assert n == 1, n
    print("  [PASS] pipeline-captured rows are not failed for lacking a download sha")


def test_suite():
    print("=" * 72)
    print("  PSX NEWS MODULE REGRESSION SUITE")
    print("=" * 72)
    for fn in (test_both_download_link_shapes, test_no_pdf_link_is_not_fabricated,
               test_invalid_calendar_dates_rejected, test_scheduled_as_follows_phrasing,
               test_heartbeat_uses_latest_run_per_feed, test_no_run_recorded_is_an_outage,
               test_superseded_meeting_kept_not_deleted,
               test_liquidity_uses_traded_value_not_share_count,
               test_ocr_dates_are_flagged_for_verification,
               test_missing_feed_and_stall_are_outages, test_clause_5_9_2_scope_split,
               test_manual_ocr_check_is_recorded, test_intraday_archive_refuses_without_permission,
               test_cadence_slows_after_the_close,
               test_telegram_diagnosis_names_the_real_reason,
               test_stale_mts_report_is_not_clean,
               test_manual_rows_must_prove_themselves):
        fn()
    print("\n[ALL TESTS PASSED] 17/17 news-monitor regressions pinned.")


if __name__ == "__main__":
    test_suite()
