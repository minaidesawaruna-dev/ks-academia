"""Copy the local SQLite database into a new, empty Postgres database (one time only).

    python migrate_to_postgres.py "postgresql://user:pass@host/db?sslmode=require"

Rows are copied through the app's own SQLAlchemy tables, so dates, times
and true/false values (stored as text and numbers in SQLite) come out as
the right types in Postgres. Tables are copied parents first, so every
row a record points to already exists.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, func, select, text

import db


CHUNK = 1000


def normalise(url: str) -> str:
    """Change a postgres:// address (still given out by some hosts) to postgresql://."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def reset_sequences(connection) -> None:
    """Move each id counter past the ids just copied in.

    Inserting rows with their ids doesn't move Postgres' counters, so without
    this the first new record after the move would reuse id 1 and fail.
    """
    for table in db.Base.metadata.sorted_tables:
        for column in table.primary_key.columns:
            sequence = connection.execute(
                text("SELECT pg_get_serial_sequence(:t, :c)"),
                {"t": table.name, "c": column.name},
            ).scalar()
            if sequence is None:
                continue
            # For an empty table the next id is 1; otherwise it is the highest id + 1.
            connection.execute(
                text(
                    f"SELECT setval('{sequence}', "
                    f"COALESCE(MAX({column.name}), 1), "
                    f"MAX({column.name}) IS NOT NULL) FROM {table.name}"
                )
            )
            print(f"    sequence reset: {table.name}.{column.name}")


def main() -> int:
    """Copy every table from the local SQLite file into the given Postgres database. Returns the exit code.
    """
    if len(sys.argv) > 1:
        target_url = sys.argv[1]
    else:
        target_url = os.environ.get("TARGET_DATABASE_URL", "")
    if not target_url:
        print(__doc__)
        return 2

    target_url = normalise(target_url)
    if not target_url.startswith("postgresql://"):
        print(f"Refusing to run: target is not Postgres ({target_url[:30]}...)")
        return 2

    # The source must be the local SQLite file. Run against Postgres, this would copy
    # the live data over itself.
    if db.engine.dialect.name != "sqlite":
        print("Refusing to run: DATABASE_URL is set, so the source is not SQLite.")
        print("Unset it so this reads the local file.")
        return 2

    source = db.engine
    target = create_engine(target_url)

    print(f"source: {source.url}")
    print(f"target: {target.url.render_as_string(hide_password=True)}\n")

    print("creating schema on target...")
    db.Base.metadata.create_all(target)

    with target.begin() as tconn:
        # Refuse a target that already has rows: it means this has run before, and copying
        # again would duplicate everything.
        for table in db.Base.metadata.sorted_tables:
            existing = tconn.execute(select(func.count()).select_from(table)).scalar()
            if existing:
                print(f"\nRefusing to run: {table.name} already holds {existing} rows.")
                print("The target is not empty. Drop and recreate it to start over.")
                return 1

        print("\ncopying rows...")
        with source.connect() as sconn:
            for table in db.Base.metadata.sorted_tables:
                rows = [dict(r) for r in sconn.execute(select(table)).mappings()]
                if not rows:
                    print(f"  {table.name:24} empty")
                    continue
                for start in range(0, len(rows), CHUNK):
                    tconn.execute(table.insert(), rows[start : start + CHUNK])
                print(f"  {table.name:24} {len(rows)} rows")

        print("\nresetting sequences...")
        reset_sequences(tconn)

    print("\nverifying...")
    ok = True
    with source.connect() as sconn, target.connect() as tconn:
        for table in db.Base.metadata.sorted_tables:
            want = sconn.execute(select(func.count()).select_from(table)).scalar()
            got = tconn.execute(select(func.count()).select_from(table)).scalar()
            flag = "ok" if want == got else "MISMATCH"
            if want != got:
                ok = False
            print(f"  {table.name:24} sqlite={want:<6} postgres={got:<6} {flag}")

    print("\ndone." if ok else "\nFINISHED WITH MISMATCHES - do not switch over.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
