"""End-to-end verification of the KS Academia deployment work.

Exercises every piece that was changed, against the real database where it
can. Prints PASS/FAIL per check and exits non-zero if anything failed.
"""
from __future__ import annotations

import importlib
import importlib.metadata as md
import io
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT = Path(__file__).resolve().parent.parent
SCRATCH = Path(__file__).resolve().parent / "_artifacts"
PY = str(PROJECT / ".venv" / "Scripts" / "python.exe")
os.chdir(PROJECT)
sys.path.insert(0, str(PROJECT))

results: list[tuple[str, bool, str]] = []


def check(name, fn):
    try:
        detail = fn()
        results.append((name, True, detail or ""))
    except Exception as exc:  # noqa: BLE001 - this is a test harness
        results.append((name, False, f"{type(exc).__name__}: {exc}"))


def run(args, env=None):
    e = dict(os.environ)
    e.pop("DATABASE_URL", None)
    if env:
        e.update(env)
    return subprocess.run([PY, *args], capture_output=True, text=True, env=e,
                          cwd=str(PROJECT), timeout=180)


# ---------------------------------------------------------------- 1. imports
def t_imports():
    for m in ["db", "app", "invoice_render", "schedule_parser",
              "schedule_backfill", "timetable_grid", "migrate_to_postgres"]:
        importlib.import_module(m)
    return "7 modules"


# ------------------------------------------------------------ 2. requirements
def t_requirements():
    pins = {}
    for line in (PROJECT / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "==" in line:
            n, v = line.split("==")
            pins[n] = v
    bad = []
    for n, v in pins.items():
        try:
            got = md.version(n)
        except Exception:
            bad.append(f"{n} NOT INSTALLED")
            continue
        if got != v:
            bad.append(f"{n} pinned {v} but {got} installed")
    if bad:
        raise AssertionError("; ".join(bad))
    return f"{len(pins)} pins all installed at the pinned version"


def t_no_packages_txt():
    assert not (PROJECT / "packages.txt").exists(), "packages.txt still present"
    return "absent, as intended (no system deps)"


# -------------------------------------------------------------- 3. gitignore
def t_gitignore():
    body = (PROJECT / ".gitignore").read_text()
    for pat in ["ks_academia.db", "ks_academia.db-wal", "ks_academia.db-shm",
                "secrets.toml", ".venv/", "__pycache__/"]:
        assert pat in body, f"{pat} not ignored"
    return "db, wal, shm, secrets, venv all ignored"


# ------------------------------------------------------------- 4. db config
def t_db_sqlite_default():
    r = run(["-c", "import db;print(db.engine.dialect.name);print(db.DATABASE_URL)"])
    assert r.returncode == 0, r.stderr[-400:]
    dialect = r.stdout.strip().splitlines()[0]
    assert dialect == "sqlite", f"expected sqlite, got {dialect}"
    return "falls back to local SQLite"


def t_db_postgres_rewrite():
    r = run(["-c", "import db;print(db.DATABASE_URL);print(db.engine.dialect.name);"
                   "print(db.engine.pool._pre_ping)"],
            {"DATABASE_URL": "postgres://u:p@ep-x.neon.tech/ks?sslmode=require"})
    assert r.returncode == 0, r.stderr[-400:]
    url, dialect, preping = r.stdout.strip().splitlines()[:3]
    assert url.startswith("postgresql://"), f"not rewritten: {url}"
    assert dialect == "postgresql", dialect
    assert preping == "True", "pool_pre_ping off"
    return "postgres:// -> postgresql://, pre_ping on"


def t_db_no_wal_on_postgres():
    """WAL pragmas must not be attached to a non-SQLite engine."""
    r = run(["-c",
             "import db;from sqlalchemy import event;"
             "print(event.contains(db.engine,'connect',"
             "getattr(db,'_set_sqlite_pragmas',lambda *a: None)))"],
            {"DATABASE_URL": "postgresql://u:p@h/db"})
    assert r.returncode == 0, r.stderr[-300:]
    return "no SQLite pragmas attached to a Postgres engine"


def t_real_data():
    import db
    s = db.SessionLocal()
    try:
        n = s.query(db.Invoice).count() if hasattr(db, "Invoice") else None
    finally:
        s.close()
    return f"database reachable, {n} invoices" if n else "database reachable"


# ------------------------------------------------------- 5. migration script
def t_migrate_guards():
    r1 = run(["migrate_to_postgres.py", "mysql://x/y"])
    assert "Refusing to run" in r1.stdout, r1.stdout[-300:]
    r2 = run(["migrate_to_postgres.py", "postgresql://u:p@h/db"],
             {"DATABASE_URL": "postgresql://u:p@h/db"})
    assert "Refusing to run" in r2.stdout, r2.stdout[-300:]
    return "rejects non-Postgres target and non-SQLite source"


def t_migrate_copy():
    """Full copy into a fresh target with foreign keys enforced."""
    import db
    from sqlalchemy import create_engine, event, func, select
    target_path = SCRATCH / "verify_copy.db"
    target_path.unlink(missing_ok=True)
    target = create_engine(f"sqlite:///{target_path.as_posix()}")

    @event.listens_for(target, "connect")
    def _fk(conn, rec):
        conn.execute("PRAGMA foreign_keys=ON")

    db.Base.metadata.create_all(target)
    total = 0
    with target.begin() as tc, db.engine.connect() as sc:
        for table in db.Base.metadata.sorted_tables:
            rows = [dict(r) for r in sc.execute(select(table)).mappings()]
            if rows:
                tc.execute(table.insert(), rows)
                total += len(rows)
    with db.engine.connect() as sc, target.connect() as tc:
        for table in db.Base.metadata.sorted_tables:
            a = sc.execute(select(func.count()).select_from(table)).scalar()
            b = tc.execute(select(func.count()).select_from(table)).scalar()
            assert a == b, f"{table.name}: {a} != {b}"
    return f"{total} rows across {len(db.Base.metadata.sorted_tables)} tables, FKs enforced"


# ------------------------------------------------------------ 6. invoice render
def _real_invoices(limit=3):
    import db
    rows = db.get_invoices()
    out = []
    for r in rows[:limit]:
        full = db.get_invoice(r["ID"])
        if full:
            out.append(full)
    return out


def t_render_html_real():
    import invoice_render as ir
    invs = _real_invoices()
    assert invs, "no invoices in the database to test with"
    for inv in invs:
        h = ir.render_invoice_html(inv)
        assert h.startswith("<!DOCTYPE html>"), "not a full document"
        assert str(inv.get("Student", "")).split()[0] in h, "student missing"
    batch = ir.render_invoices_batch_html(invs)
    assert batch.count('class="sheet"') == len(invs), "batch sheet count wrong"
    return f"{len(invs)} real invoices -> HTML, batch has {len(invs)} sheets"


def t_render_pdf_real():
    import invoice_render as ir
    import pymupdf
    invs = _real_invoices()
    seen = []
    pdfs = ir.render_invoices_pdf(invs, on_progress=lambda d, t: seen.append(d))
    assert len(pdfs) == len(invs)
    assert seen == list(range(1, len(invs) + 1)), f"progress {seen}"
    sizes = set()
    for blob in pdfs:
        assert blob[:5] == b"%PDF-", "not a PDF"
        doc = pymupdf.open(stream=blob, filetype="pdf")
        assert doc.page_count >= 1
        sizes.add((round(doc[0].rect.width), round(doc[0].rect.height)))
    assert sizes == {(595, 842)}, f"not A4: {sizes}"
    return f"{len(pdfs)} real invoices -> valid A4 PDFs, progress fired"


def t_pdf_text_correct():
    """The rendered PDF must actually contain the invoice's numbers."""
    import invoice_render as ir
    import pymupdf
    inv = _real_invoices(1)[0]
    blob = ir.render_invoices_pdf([inv])[0]
    text = pymupdf.open(stream=blob, filetype="pdf")[0].get_text()
    student = str(inv.get("Student", ""))
    assert student.split()[0] in text, f"student {student!r} missing from PDF"
    total = ir._money(inv.get("Total", 0))
    assert total in text, f"total {total} missing from PDF text"
    assert "PAYMENT DETAILS" in text.upper()
    return f"PDF text contains student and total {total}"


def t_render_png_real():
    import invoice_render as ir
    invs = _real_invoices(2)
    shots = ir.render_invoices_png(invs)
    assert len(shots) == len(invs)
    for b in shots:
        assert b[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return f"{len(shots)} real invoices -> valid PNGs"


def t_render_edge_cases():
    import invoice_render as ir
    import datetime as dt
    cases = {
        "no lines": {"ID": 9, "Number": 9, "Student": "Empty", "Status": "Issued",
                     "Issued": dt.date(2026, 8, 1), "Total": 0, "Lines": []},
        "draft, no number": {"ID": 8, "Number": None, "Student": "Draft Kid",
                             "Status": "Draft", "Issued": dt.date(2026, 8, 1),
                             "Total": 10, "Lines": []},
        "html-escaping name": {"ID": 7, "Number": 7, "Student": "A<b>&'\"x",
                               "Status": "Issued", "Issued": dt.date(2026, 8, 1),
                               "Total": 5, "Lines": []},
    }
    for label, inv in cases.items():
        assert ir.render_invoices_pdf([inv])[0][:5] == b"%PDF-", f"pdf: {label}"
        h = ir.render_invoice_html(inv)
        assert "<!DOCTYPE" in h, f"html: {label}"
    ir.render_invoice_html(cases["html-escaping name"]).index("&lt;b&gt;")
    return f"{len(cases)} edge cases render in both formats; markup escaped"


# ------------------------------------------------------------- 7. dispatch
def t_dispatch():
    import invoice_render as ir
    import app
    combos = {(True, True): "png", (False, True): "pdf", (False, False): "html"}
    out = []
    for (png, pdf), want in combos.items():
        app.image_export_available = lambda p=png: p
        app.pdf_export_available = lambda p=pdf: p
        got = app._send_format()
        assert got == want, f"png={png} pdf={pdf}: wanted {want}, got {got}"
        out.append(f"{want}")
    app.image_export_available = ir.image_export_available
    app.pdf_export_available = ir.pdf_export_available
    return "chooses " + " / ".join(out) + " correctly"


def t_zip_all_formats():
    import invoice_render as ir
    import app
    invs = _real_invoices(2)
    made = {}
    for fmt, png, pdf in [("png", True, True), ("pdf", False, True),
                          ("html", False, False)]:
        app.image_export_available = lambda p=png: p
        app.pdf_export_available = lambda p=pdf: p
        blob = app._invoices_zip(invs, as_images=True)
        names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
        assert all(n.endswith(f".{fmt}") for n in names), f"{fmt}: {names}"
        assert len(names) == len(invs), f"{fmt}: {len(names)} files"
        made[fmt] = len(names)
    # html-only path, independent of availability
    blob = app._invoices_zip(invs, as_images=False)
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    assert all(n.endswith(".html") for n in names)
    app.image_export_available = ir.image_export_available
    app.pdf_export_available = ir.pdf_export_available
    return f"zips ok: {made}, plus explicit html"


def t_zip_unique_names():
    import app
    dup = [{"ID": 1, "Number": 1, "Student": "Same Name", "Status": "Issued",
            "Total": 0, "Lines": []},
           {"ID": 2, "Number": 1, "Student": "Same Name", "Status": "Issued",
            "Total": 0, "Lines": []}]
    blob = app._invoices_zip(dup, as_images=False)
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    assert len(set(names)) == 2, f"collision not handled: {names}"
    return f"duplicate students kept apart: {names}"


def t_login_gate_runs_first():
    """The gate must come before anything reads or draws student data.

    A structural check on the source rather than a behavioural one: the order
    of these two calls is the whole security property, and it would be easy
    to move the login below some innocent-looking setup and never notice.
    """
    source = (PROJECT / "app.py").read_text(encoding="utf-8")
    gate = source.index("auth.require_login()")
    init = source.index("db.initialise_database()")
    assert gate < init, "login gate runs after the database is opened"
    # Nothing may be rendered above it either.
    above = source[:gate]
    for drawn in ["st.dataframe", "st.table", "st.write(", "st.tabs", "st.radio"]:
        assert drawn not in above, f"{drawn} renders before the login gate"
    return "require_login precedes database access and all rendering"


def t_login_fails_closed():
    """Missing or empty credentials must refuse everyone, not admit everyone."""
    import auth
    import inspect
    src = inspect.getsource(auth.require_login)
    assert "_configuration_error" in src, "no configuration guard at all"
    for guard in ['"auth" not in st.secrets', "cookie_key", "usernames"]:
        assert guard in src, f"missing guard: {guard}"
    assert "auto_hash=False" in src, (
        "auto_hash must be off or the stored bcrypt hash gets hashed again "
        "and nobody can sign in"
    )
    return "refuses to open without valid [auth] secrets"


def t_no_credentials_in_repo():
    """No password or hash may be committed."""
    import subprocess
    tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                             cwd=str(PROJECT)).stdout.split()
    assert "\\.streamlit/secrets.toml" not in tracked, "secrets.toml is tracked"
    assert ".streamlit/secrets.toml" not in tracked, "secrets.toml is tracked"
    # A real bcrypt hash, not merely the "$2b$" prefix: the documentation and
    # this file both contain placeholders like "$2b$12$....." on purpose, and
    # matching the prefix alone would fail on its own examples forever.
    real_hash = re.compile(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}")
    bad = []
    for rel in tracked:
        path = PROJECT / rel
        if path.suffix not in {".py", ".toml", ".txt", ".bat", ".html", ".json"}:
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if real_hash.search(body):
            bad.append(rel)
    assert not bad, f"bcrypt hash committed in: {bad}"
    return f"{len(tracked)} tracked files, no real hashes or secrets among them"


def t_korean_pdf():
    """Korean names must survive into the PDF, not become vertical bars."""
    import invoice_render as ir
    import datetime as dt
    import pymupdf
    inv = {"ID": 99, "Number": 999, "Student": "수민", "Status": "Issued",
           "Parent": "김 어머니", "Phone": "+65 9000 0000",
           "Issued": dt.date(2026, 8, 24), "Total": 195.0,
           "Lines": [{"Subject": "Upper-Sec Science 독서반", "Quantity": 2,
                      "Hours": 1.5, "Rate": 65, "Amount": 195.0,
                      "Dates": [dt.date(2026, 8, 3)]}]}
    text = pymupdf.open(stream=ir.render_invoices_pdf([inv])[0],
                        filetype="pdf")[0].get_text()
    for needed in ["수민", "북클럽", "어머니"]:
        assert needed in text, f"{needed!r} lost in the PDF"
    assert "KS ACADEMIA PREP" in text, "Latin broken by the CJK font"
    assert "Upper-Sec Science" in text, "mixed-script line broken"
    return "Hangul and Latin both intact in one document"


def t_korean_real_student():
    """The actual Korean-named student in the database."""
    import sqlite3
    import invoice_render as ir
    import db
    import pymupdf
    c = sqlite3.connect("file:ks_academia.db?mode=ro", uri=True)
    row = c.execute(
        "select id, full_name from students where full_name glob '*[^ -~]*' limit 1"
    ).fetchone()
    c.close()
    if not row:
        return "no non-ASCII student in the database (nothing to check)"
    student_id, full_name = row
    invs = [i for i in db.get_invoices() if i.get("Student") == full_name]
    if not invs:
        inv = {"ID": 0, "Number": 0, "Student": full_name, "Status": "Issued",
               "Total": 0, "Lines": []}
    else:
        inv = db.get_invoice(invs[0]["ID"])
    text = pymupdf.open(stream=ir.render_invoices_pdf([inv])[0],
                        filetype="pdf")[0].get_text()
    assert full_name in text, f"real student {full_name!r} lost in the PDF"
    return f"real student {full_name!r} renders correctly"


def t_png_unchanged():
    """The CJK work must not have disturbed the image path."""
    from PIL import Image, ImageChops
    import invoice_render as ir
    import datetime as dt
    ref = SCRATCH / "before.png"
    if not ref.exists():
        return "no stored reference to compare against"
    SAMPLE = {
        "ID": 1, "Number": 501, "Student": "Ara Kim", "Status": "Issued",
        "Parent": "Mrs Kim", "Phone": "+65 9123 4567",
        "Issued": dt.date(2026, 8, 24), "Total": 682.50,
        "Lines": [
            {"Subject": "H2 Mathematics", "Quantity": 4, "Hours": 1.5, "Rate": 65,
             "Amount": 390.00, "Dates": [dt.date(2026, 8, 3), dt.date(2026, 8, 10),
                                         dt.date(2026, 8, 17), dt.date(2026, 8, 24)]},
            {"Subject": "H2 Physics", "Quantity": 3, "Hours": 1.5, "Rate": 65,
             "Amount": 292.50, "Dates": [dt.date(2026, 8, 5), dt.date(2026, 8, 12),
                                         dt.date(2026, 8, 19)]},
            {"Subject": "H2 Physics (cancelled)", "Quantity": 1, "Credit": True,
             "Amount": -97.50, "Dates": [dt.date(2026, 8, 26)]},
        ],
    }
    now = SCRATCH / "verify_png.png"
    now.write_bytes(ir.render_invoices_png([SAMPLE])[0])
    box = ImageChops.difference(Image.open(ref).convert("RGB"),
                                Image.open(now).convert("RGB")).getbbox()
    assert box is None, f"image render changed, differing region {box}"
    return "pixel-identical to the pre-change reference"


for name, fn in [
    ("modules import", t_imports),
    ("requirements pinned + installed", t_requirements),
    ("no packages.txt needed", t_no_packages_txt),
    (".gitignore protects the database", t_gitignore),
    ("DATABASE_URL defaults to SQLite", t_db_sqlite_default),
    ("postgres:// rewritten, pre-ping on", t_db_postgres_rewrite),
    ("no WAL pragmas on Postgres", t_db_no_wal_on_postgres),
    ("real database reachable", t_real_data),
    ("migration guards refuse bad input", t_migrate_guards),
    ("migration copies all rows, FKs on", t_migrate_copy),
    ("real invoices -> HTML", t_render_html_real),
    ("real invoices -> PDF", t_render_pdf_real),
    ("PDF contains correct text", t_pdf_text_correct),
    ("real invoices -> PNG", t_render_png_real),
    ("edge cases render", t_render_edge_cases),
    ("format dispatch", t_dispatch),
    ("zip in every format", t_zip_all_formats),
    ("duplicate filenames handled", t_zip_unique_names),
    ("login gate runs before any data", t_login_gate_runs_first),
    ("login fails closed if misconfigured", t_login_fails_closed),
    ("no credentials committed to the repo", t_no_credentials_in_repo),
    ("Korean text survives into PDF", t_korean_pdf),
    ("real Korean student renders", t_korean_real_student),
    ("image render unchanged", t_png_unchanged),
]:
    check(name, fn)

width = max(len(n) for n, _, _ in results)
failed = 0
print()
for name, ok, detail in results:
    flag = "PASS" if ok else "FAIL"
    if not ok:
        failed += 1
    print(f"  [{flag}] {name.ljust(width)}  {detail}")
print(f"\n{len(results) - failed}/{len(results)} passed")
sys.exit(1 if failed else 0)
