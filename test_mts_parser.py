"""
================================================================================
  MTS PDF PARSER REGRESSION TEST SUITE  (golden file: NCCPL report 2026-09-14)
================================================================================
Protects the Amendment #12 coordinate-based cell reader against the 3 verified
defects of the old positional/regex reader:
  1. Footnote glyphs bleeding into numeric cells ("a 13.05", "12.80\\nM")
  2. Amounts rendered across several spans ("36,961,696.9" + "2")
  3. Silent acceptance of an unreadable row (SLGL printed "-" for volume)
The [5, 35] weighted_rate band is asserted to still fire. It was NOT loosened.
================================================================================
"""

import os
import sqlite3
import tempfile

import numpy as np

import mts_engine as M

DB_PATH = "psx.db"
GOLDEN_DATE = "2026-09-14"
GOLDEN_ROWS = 66          # 67 printed rows minus SLGL, whose volume cell prints "-"

# Rates the old reader destroyed by column bleed, recovered by coordinate reading.
RECOVERED_RATES = {
    "FATIMA": 12.80, "FCCL": 13.05, "FFC": 12.96, "FFL": 14.75,
    "GCIL": 15.02, "HBL": 13.13, "PPL": 12.74, "PSO": 12.90, "SNBL": 15.00,
}


def _golden_pdf() -> str:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    blob = conn.execute(
        "SELECT blob FROM mts_raw_reports WHERE report_date=?", (GOLDEN_DATE,)).fetchone()[0]
    conn.close()
    path = os.path.join(tempfile.gettempdir(), "mts_golden_20260914.pdf")
    with open(path, "wb") as fh:
        fh.write(blob)
    return path


def test_golden_parse():
    print(f"\n[TEST 1] Golden {GOLDEN_DATE} report parses to exactly {GOLDEN_ROWS} rows...")
    df, diag = M.parse_mts_pdf(_golden_pdf())

    assert diag["report_date"] == GOLDEN_DATE, f"report_date drifted: {diag['report_date']}"
    assert len(df) == GOLDEN_ROWS, f"Expected {GOLDEN_ROWS} rows, got {len(df)}"
    assert diag["n_rejected"] == 1, f"Expected 1 rejected row, got {diag['n_rejected']}"
    assert diag["rejected_symbols"] == ["SLGL"], f"Unexpected rejects: {diag['rejected_symbols']}"

    # Independent proof the numeric columns are aligned: totals must reconcile.
    assert diag["grand_total"] is not None, "grand total row missing"
    assert abs(diag["parsed_total"] - diag["grand_total"]) <= max(1.0, 1e-7 * diag["grand_total"]), \
        f"Reconciliation failed: {diag}"

    got = df.set_index("raw_symbol")["weighted_rate"]
    for sym, want in RECOVERED_RATES.items():
        assert sym in got.index, f"{sym} missing from parse"
        assert abs(got[sym] - want) < 0.005, f"{sym} rate {got[sym]} != printed {want}"
    print(f"  [PASS] {len(df)} rows, {len(RECOVERED_RATES)} bled rates recovered, "
          f"total {diag['parsed_total']:,.2f} reconciles to {diag['grand_total']:,.2f}")


def test_boundary_glyphs_excluded():
    print("\n[TEST 2] Glyphs spilling outside the cell rect are excluded, digits never trimmed...")
    rect = (720.9, 371.7, 768.9, 389.2)
    words = [
        (719.2, 378.1, 727.0, 393.7, "a"),    # footnote, x0 left of rect -> dropped
        (719.2, 361.3, 730.9, 376.9, "M"),    # footnote, y0 above rect  -> dropped
        (733.6, 376.5, 756.4, 386.5, "13.05"),
    ]
    assert M._contained_num(words, rect) == 13.05, "boundary glyph still contaminating"
    print("  [PASS] 'a 13.05' reads as 13.05")


def test_fragmented_number_joined():
    print("\n[TEST 3] An amount printed across two spans is re-joined, not truncated...")
    rect = (642.8, 178.7, 720.9, 196.2)
    words = [
        (650.0, 182.0, 704.0, 192.0, "36,961,696.9"),
        (704.2, 182.0, 712.0, 192.0, "2"),
    ]
    assert M._contained_num(words, rect) == 36961696.92, "fragmented amount mis-read"
    print("  [PASS] '36,961,696.9' + '2' reads as 36,961,696.92")


def test_ambiguous_cell_raises():
    print("\n[TEST 4] Two different numbers in one cell abort instead of guessing...")
    rect = (642.8, 178.7, 720.9, 196.2)
    words = [(650.0, 182.0, 690.0, 192.0, "111,016.00"), (700.0, 182.0, 715.0, 192.0, "99.00")]
    try:
        M._contained_num(words, rect)
    except M.ParseIntegrityError as e:
        print(f"  [PASS] Ambiguity caught: {e}")
        return
    raise AssertionError("Ambiguous cell was silently accepted")


def test_rate_band_still_enforced():
    print("\n[TEST 5] weighted_rate [5, 35] band is still enforced on observed values...")
    orig = M._contained_num

    # Spike only the printed rate range (12.66-18.5 in the golden report) so open_pct
    # values that also fall inside [5, 35] are not disturbed by the probe.
    def spiked(words, rect):
        v = orig(words, rect)
        return 99.0 if isinstance(v, float) and np.isfinite(v) and 12.0 <= v <= 19.0 else v

    M._contained_num = spiked
    try:
        M.parse_mts_pdf(_golden_pdf())
        raise AssertionError("Out-of-range rate passed the band check")
    except M.ParseIntegrityError as e:
        assert "weighted_rate out of range [5, 35]" in str(e), f"Wrong error: {e}"
        print(f"  [PASS] Band guard fired: {e}")
    finally:
        M._contained_num = orig


def test_missing_rate_tolerated_but_counted():
    print("\n[TEST 6] A printed '-' rate is counted, not silently absorbed...")
    df, diag = M.parse_mts_pdf(_golden_pdf())
    assert diag["n_rate_missing"] == int(df["weighted_rate"].isna().sum()) > 0, \
        f"missing-rate count wrong: {diag['n_rate_missing']}"
    assert df["weighted_rate"].dropna().between(5.0, 35.0).all()
    print(f"  [PASS] {diag['n_rate_missing']} rows legitimately print no rate")


def test_layout_drift_aborts():
    print("\n[TEST 7] Upstream column reorder aborts rather than mislabelling...")
    good = ["S.No", "Report Date", "Symbol Code", "Open MTS Volume Before Release",
            "Open MTS Amount Before Release", "Adjustment", "Current Day Release Volume",
            "Current Day Release Amount", "Current Day MTS Volume", "Current Day MTS Amount",
            "Weighted Average", "Net Open MTS Volume", "Net Open MTS Amount",
            "MTS Open Percentage", "Symbol Category"]
    M._assert_layout(good)
    swapped = list(good)
    swapped[11], swapped[12] = swapped[12], swapped[11]
    try:
        M._assert_layout(swapped)
        raise AssertionError("Column swap passed the layout guard")
    except M.LayoutDriftError as e:
        print(f"  [PASS] Drift caught: {e}")


def test_formation_key_is_publication_date():
    print("\n[TEST 8] Formation key is the cover date, never the per-row as-of date...")
    df, diag = M.parse_mts_pdf(_golden_pdf())
    assert diag["report_date"] == GOLDEN_DATE, \
        f"formation key drifted: {diag['report_date']}"
    assert diag["data_as_of"] == "2026-09-11", \
        f"as-of date not captured, staleness is invisible: {diag['data_as_of']}"
    assert diag["data_as_of"] < diag["report_date"], \
        "positions dated on/after publication would mean the signal knows the future"
    # The row-level date must never become the snapshot key: that would let the engine
    # trade on a session before the report existed publicly.
    stored = set(df["raw_symbol"])
    assert stored and GOLDEN_DATE == diag["report_date"]
    print(f"  [PASS] forms on {diag['report_date']}, positions as of {diag['data_as_of']} "
          f"(staleness recorded, not used as key)")


def test_suite():
    print("=" * 72)
    print("  MTS PARSER REGRESSION SUITE (golden 2026-09-14 report)")
    print("=" * 72)
    for fn in (test_golden_parse, test_boundary_glyphs_excluded, test_fragmented_number_joined,
               test_ambiguous_cell_raises, test_rate_band_still_enforced,
               test_missing_rate_tolerated_but_counted, test_layout_drift_aborts,
               test_formation_key_is_publication_date):
        fn()
    print("\n[ALL TESTS PASSED] Coordinate-based MTS reader verified on all 8 cases.")


if __name__ == "__main__":
    test_suite()
