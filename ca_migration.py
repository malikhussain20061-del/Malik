"""
ca_migration.py — Single Source of Truth for corporate_actions Table Rebuild & Triggers.
Used by unit tests and live migration for Amendment #11.
"""
import sqlite3


def rebuild_corporate_actions(conn: sqlite3.Connection) -> None:
    """
    Executes atomic transactional rebuild of corporate_actions table:
    1. Adds voids_action_id column referencing corporate_actions(action_id).
    2. Drops rigid table-level UNIQUE constraint so voided events can be cleanly replaced.
    3. Reinstalls ca_no_update, ca_no_delete, and comprehensive ca_no_replace triggers.
    """
    conn.execute("PRAGMA foreign_keys=OFF;")
    conn.execute("BEGIN TRANSACTION;")

    # Drop existing triggers first
    conn.execute("DROP TRIGGER IF EXISTS ca_no_update;")
    conn.execute("DROP TRIGGER IF EXISTS ca_no_delete;")
    conn.execute("DROP TRIGGER IF EXISTS ca_no_replace;")

    # Create new table structure
    conn.execute("""
    CREATE TABLE corporate_actions_new (
        action_id INTEGER PRIMARY KEY AUTOINCREMENT,
        ex_date TEXT NOT NULL,
        symbol TEXT NOT NULL,
        base_symbol TEXT NOT NULL,
        action_type TEXT NOT NULL,
        amount REAL,
        ratio REAL,
        annc_date TEXT,
        ingest_ts TEXT NOT NULL,
        source TEXT NOT NULL,
        voids_action_id INTEGER REFERENCES corporate_actions(action_id)
    );
    """)

    # Carry voids_action_id through when the source table already has it. Hard-coding NULL
    # resurrects every voided row on a re-run and double-counts its replacement; NULL is
    # correct only for the first migration, before the column existed.
    has_voids_col = any(r[1] == "voids_action_id" for r in conn.execute(
        "PRAGMA table_info(corporate_actions)").fetchall())
    voids_sel = "voids_action_id" if has_voids_col else "NULL"
    conn.execute(f"""
    INSERT INTO corporate_actions_new (
        action_id, ex_date, symbol, base_symbol, action_type,
        amount, ratio, annc_date, ingest_ts, source, voids_action_id
    )
    SELECT 
        action_id, ex_date, symbol, base_symbol, action_type,
        amount, ratio, annc_date, ingest_ts, source, {voids_sel}
    FROM corporate_actions;
    """)

    conn.execute("DROP TABLE corporate_actions;")
    conn.execute("ALTER TABLE corporate_actions_new RENAME TO corporate_actions;")

    # Reinstall triggers
    conn.execute("""
    CREATE TRIGGER ca_no_update BEFORE UPDATE ON corporate_actions
    BEGIN
        SELECT RAISE(ABORT, 'corporate_actions is append-only (update blocked)');
    END;
    """)

    conn.execute("""
    CREATE TRIGGER ca_no_delete BEFORE DELETE ON corporate_actions
    BEGIN
        SELECT RAISE(ABORT, 'corporate_actions is append-only (delete blocked)');
    END;
    """)

    conn.execute("""
    CREATE TRIGGER ca_no_replace BEFORE INSERT ON corporate_actions
    WHEN (NEW.action_id IS NOT NULL AND EXISTS (SELECT 1 FROM corporate_actions WHERE action_id = NEW.action_id))
      OR (NEW.action_type != 'VOID' AND EXISTS (
          SELECT 1 FROM corporate_actions
          WHERE base_symbol = NEW.base_symbol 
            AND ex_date = NEW.ex_date 
            AND action_type = NEW.action_type
            AND action_id NOT IN (
                SELECT voids_action_id FROM corporate_actions 
                WHERE action_type = 'VOID' AND voids_action_id IS NOT NULL
            )
      ))
      OR (NEW.action_type = 'VOID' AND (
          NEW.voids_action_id IS NULL
          OR NOT EXISTS (SELECT 1 FROM corporate_actions WHERE action_id = NEW.voids_action_id AND action_type <> 'VOID')
          OR EXISTS (SELECT 1 FROM corporate_actions WHERE action_type = 'VOID' AND voids_action_id = NEW.voids_action_id)
          OR EXISTS (
              SELECT 1 FROM corporate_actions target 
              WHERE target.action_id = NEW.voids_action_id 
                AND (target.ex_date <> NEW.ex_date OR target.base_symbol <> NEW.base_symbol)
          )
      ))
    BEGIN
        SELECT RAISE(ABORT, 'corporate_actions is append-only (replace/conflict/invalid_void blocked)');
    END;
    """)

    conn.commit()


if __name__ == "__main__":
    conn = sqlite3.connect("psx.db")
    rebuild_corporate_actions(conn)
    conn.close()
    print("rebuild_corporate_actions completed successfully on psx.db.")
