"""
add_corporate_action.py — Researcher tool for safe, audited forward corporate action ingestion.
Enforces plain INSERT, provenance requirement, sanity checks, and catches append-only trigger aborts.
"""
import argparse
import datetime as dt
import sqlite3
import sys


def add_action(
    db_path: str,
    symbol: str,
    ex_date: str,
    action_type: str,
    source: str,
    amount: float | None = None,
    ratio: float | None = None,
    voids_action_id: int | None = None,
    annc_date: str | None = None,
) -> int:
    conn = sqlite3.connect(db_path)
    base_symbol = symbol.split("_")[0].upper()
    action_type = action_type.strip().upper()
    now_utc = dt.datetime.now(dt.timezone.utc).isoformat()

    # 1. Validation checks
    if action_type not in ("CASH", "BONUS", "SPLIT", "VOID"):
        raise ValueError(f"Invalid action_type: {action_type}. Must be CASH, BONUS, SPLIT, or VOID.")

    if not source or not source.strip():
        raise ValueError("Mandatory source provenance missing (e.g. official PSX notice URL or reference).")

    if action_type == "CASH":
        if amount is None or amount <= 0:
            raise ValueError("CASH action requires positive amount.")
        # Yield sanity check against latest close/ldcp
        q = conn.execute(
            "SELECT ldcp, close FROM quotes WHERE symbol=? ORDER BY date DESC LIMIT 1",
            (symbol,)
        ).fetchone()
        if q and q[0] and q[0] > 0:
            ref_px = q[0]
            yield_pct = amount / ref_px
            if yield_pct > 0.25:
                print(f"[!] WARNING: High payout yield detected: Rs {amount:.2f} / Rs {ref_px:.2f} = {yield_pct*100:.1f}%. Please verify PSX announcement.")

    elif action_type in ("BONUS", "SPLIT"):
        if ratio is None or ratio <= 0:
            raise ValueError(f"{action_type} action requires positive ratio.")
        if action_type == "BONUS" and ratio >= 1.0:
            raise ValueError(f"Bonus ratio {ratio} >= 1.0 (>= 100%). Check if this is a stock split.")

    elif action_type == "VOID":
        if voids_action_id is None:
            raise ValueError("VOID action requires --voids-action-id.")

    # 2. Execute plain INSERT (trigger ca_no_replace will guard against duplicate active / replace)
    try:
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO corporate_actions (
            ex_date, symbol, base_symbol, action_type, amount, ratio, annc_date, ingest_ts, source, voids_action_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ex_date, symbol, base_symbol, action_type, amount, ratio, annc_date, now_utc, source.strip(), voids_action_id))
        new_id = cur.lastrowid
        conn.commit()
        print(f"[SUCCESS] Action {new_id} inserted: {symbol} {action_type} on {ex_date} (source: {source})")
        return new_id
    except sqlite3.OperationalError as e:
        if "corporate_actions is append-only" in str(e):
            print(f"[ERROR] Rejected by database append-only trigger:\n  {e}")
            sys.exit(1)
        raise
    except sqlite3.IntegrityError as e:
        print(f"[ERROR] Database integrity violation:\n  {e}")
        sys.exit(1)
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Insert a forward corporate action safely into psx.db")
    parser.add_argument("--db", default="psx.db", help="Path to database (default: psx.db)")
    parser.add_argument("--symbol", required=True, help="Stock symbol (e.g. OGDC)")
    parser.add_argument("--ex-date", required=True, help="Ex-date in YYYY-MM-DD format")
    parser.add_argument("--type", required=True, dest="action_type", choices=["CASH", "BONUS", "SPLIT", "VOID"], help="Action type")
    parser.add_argument("--amount", type=float, default=None, help="Cash dividend amount in PKR per share")
    parser.add_argument("--ratio", type=float, default=None, help="Bonus ratio (e.g. 0.10 for 10%%) or Split ratio")
    parser.add_argument("--voids-action-id", type=int, default=None, help="Target action_id to void (if type=VOID)")
    parser.add_argument("--source", required=True, help="Official PSX notice reference or URL")
    parser.add_argument("--annc-date", default=None, help="Announcement date in YYYY-MM-DD format")

    args = parser.parse_args()
    add_action(
        db_path=args.db,
        symbol=args.symbol,
        ex_date=args.ex_date,
        action_type=args.action_type,
        amount=args.amount,
        ratio=args.ratio,
        voids_action_id=args.voids_action_id,
        source=args.source,
        annc_date=args.annc_date,
    )


if __name__ == "__main__":
    main()
