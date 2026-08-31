"""Copy the local SQLite database into a fresh Postgres one, once.

Run this after creating the Postgres database but before pointing the app at
it:

    python migrate_to_postgres.py "postgresql://user:pass@host/db?sslmode=require"

The copy goes through SQLAlchemy's table metadata rather than raw SQL, which
matters more than it looks: SQLite keeps dates, times and booleans as text
and integers, and only the typed metadata knows that ``session_attendance.
present`` is a boolean rather than the number 1. Reading through the same
Table objects the app uses gets every column converted on the way out and
adapted again on the way in.

Tables are copied in ``sorted_tables`` order, which SQLAlchemy sorts by
foreign key dependency, so parents land before the rows that reference them.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, func, select, text

import db


CHUNK = 1000


def normalise(url: str) -> str:
    """Accept the ``postgres://`` scheme the hosts still hand out."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


def reset_sequences(connection) -> None:
    """Point each identity sequence past the ids we just inserted.

    Copying rows with explicit primary keys does not advance Postgres'
    sequences -- they still sit at 1. Without this the first invoice an admin
    creates after the move collides with invoice number 1 and the insert
    fails on a duplicate key, which is a baffling thing to hit a week later
    with no idea it traces back to the migration.
    """
    for table in db.Base.metadata.sorted_tables:
        for column in table.primary_key.columns:
            sequence = connection.execute(
                text("SELECT pg_get_serial_sequence(:t, :c)"),
                {"t": table.name, "c": column.name},
            ).scalar()
            if sequence is None:
                continue
            # is_called=false on an empty table, so the next id is 1 rather
            # than 2; true otherwise, so the next id is max+1.
            connection.execute(
                text(
                    f"SELECT setval('{sequence}', "
                    f"COALESCE(MAX({column.name}), 1), "
                    f"MAX({column.name}) IS NOT NULL) FROM {table.name}"
                )
            )
            print(f"    sequence reset: {table.name}.{column.name}")


def main() -> int:
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

    # db was imported with DATABASE_URL unset, so db.engine is the local
    # SQLite file. Guard rather than assume -- running this against a
    # Postgres source would copy the deployed data over itself.
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
        # A non-empty target almost certainly means this ran already. Copying
        # again would double every row rather than fail loudly.
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
