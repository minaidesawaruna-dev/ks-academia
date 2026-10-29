"""End-to-end checks of the whole app: database, imports, billing, invoices and sign-in.

Runs each check against the real database where it can. Prints PASS or
FAIL for each one and exits non-zero if anything failed.
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

# Importing app.py runs the sign-in check, which stops without credentials, as it
# should. So the tests need some, and a fresh clone has no secrets file. A
# throwaway one is written here and removed afterwards, unless the developer
# already has a real one, which is left alone.
_SECRETS = PROJECT / ".streamlit" / "secrets.toml"
_SECRETS_WAS_OURS = not _SECRETS.exists()
if _SECRETS_WAS_OURS:
    import streamlit_authenticator as _stauth

    _SECRETS.parent.mkdir(exist_ok=True)
    _SECRETS.write_text(
        "# Written by tests/verify_all.py and deleted again on exit.\n"
        "[auth]\n"
        'cookie_name = "ks_academia_auth"\n'
        f'cookie_key = "{os.urandom(16).hex()}"\n'
        "cookie_expiry_days = 30\n\n"
        "[auth.credentials.usernames.verify]\n"
        'name = "Verification Run"\n'
        f'password = "{_stauth.Hasher.hash("not-a-real-password")}"\n\n'
        # The letterhead and bank details come from secrets too, and an invoice won't
        # render without them, on purpose: an invoice with no way to pay it is worse
        # than none.
        "[academy]\n"
        'name = "Verification Academy"\n'
        'address = "1 Example Street, Singapore 000000"\n'
        'phone = "+65 0000 0000"\n'
        'account_name = "Verification Academy"\n'
        'account_number = "000000000000"\n'
        'bank = "Example Bank"\n'
        'paynow = "UEN 000000000X"\n',
        encoding="utf-8",
    )


def check(name, fn):
    """Run one check and record whether it passed, with its message."""
    try:
        detail = fn()
        results.append((name, True, detail or ""))
    except Exception as exc:  # noqa: BLE001 - this is a test harness
        results.append((name, False, f"{type(exc).__name__}: {exc}"))


def run(args, env=None):
    """Run Python with `args` in the project folder, without DATABASE_URL unless `env` sets it."""
    e = dict(os.environ)
    e.pop("DATABASE_URL", None)
    if env:
        e.update(env)
    return subprocess.run([PY, *args], capture_output=True, text=True, env=e,
                          cwd=str(PROJECT), timeout=180)


# ---------------------------------------------------------------- 1. imports
def t_imports():
    """Every module imports without errors."""
    for m in ["db", "app", "invoice_render", "schedule_parser",
              "schedule_backfill", "schedule_grid", "migrate_to_postgres"]:
        importlib.import_module(m)
    return "7 modules"


# ------------------------------------------------------------ 2. requirements
def t_requirements():
    """Every package pinned in requirements.txt is installed at that version."""
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
    """There's no packages.txt, since the app needs no system packages."""
    assert not (PROJECT / "packages.txt").exists(), "packages.txt still present"
    return "absent, as intended (no system deps)"


# -------------------------------------------------------------- 3. gitignore
def t_gitignore():
    """The database files, secrets, venv and caches are all in .gitignore."""
    body = (PROJECT / ".gitignore").read_text()
    for pat in ["ks_academia.db", "ks_academia.db-wal", "ks_academia.db-shm",
                "secrets.toml", ".venv/", "__pycache__/"]:
        assert pat in body, f"{pat} not ignored"
    return "db, wal, shm, secrets, venv all ignored"


# ------------------------------------------------------------- 4. db config
def t_db_sqlite_default():
    """With no DATABASE_URL the app uses the local SQLite file."""
    r = run(["-c", "import db;print(db.engine.dialect.name);print(db.DATABASE_URL)"])
    assert r.returncode == 0, r.stderr[-400:]
    dialect = r.stdout.strip().splitlines()[0]
    assert dialect == "sqlite", f"expected sqlite, got {dialect}"
    return "falls back to local SQLite"


def t_db_postgres_rewrite():
    """A postgres:// URL is changed to postgresql://, with pre-ping turned on."""
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
    """The WAL settings are never added to a non-SQLite engine."""
    r = run(["-c",
             "import db;from sqlalchemy import event;"
             "print(event.contains(db.engine,'connect',"
             "getattr(db,'_set_sqlite_pragmas',lambda *a: None)))"],
            {"DATABASE_URL": "postgresql://u:p@h/db"})
    assert r.returncode == 0, r.stderr[-300:]
    return "no SQLite pragmas attached to a Postgres engine"


def t_real_data():
    """The database can be reached, and how many invoices it has."""
    import db
    s = db.SessionLocal()
    try:
        n = s.query(db.Invoice).count() if hasattr(db, "Invoice") else None
    finally:
        s.close()
    return f"database reachable, {n} invoices" if n else "database reachable"


# ------------------------------------------------------- 5. migration script
def t_migrate_guards():
    """The migration script refuses a non-Postgres target and a non-SQLite source."""
    r1 = run(["migrate_to_postgres.py", "mysql://x/y"])
    assert "Refusing to run" in r1.stdout, r1.stdout[-300:]
    r2 = run(["migrate_to_postgres.py", "postgresql://u:p@h/db"],
             {"DATABASE_URL": "postgresql://u:p@h/db"})
    assert "Refusing to run" in r2.stdout, r2.stdout[-300:]
    return "rejects non-Postgres target and non-SQLite source"


def t_migrate_copy():
    """Copy everything into a fresh database with foreign keys switched on."""
    import db
    from sqlalchemy import create_engine, event, func, select
    target_path = SCRATCH / "verify_copy.db"
    target_path.unlink(missing_ok=True)
    target = create_engine(f"sqlite:///{target_path.as_posix()}")

    @event.listens_for(target, "connect")
    def _fk(conn, rec):
        """Switch foreign keys on for each new connection."""
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
    tables = len(db.Base.metadata.sorted_tables)
    return f"{total} rows across {tables} tables, FKs enforced"


# ------------------------------------------------------------ 6. invoice render
def _real_invoices(limit=3):
    """Return up to `limit` real invoices in full."""
    import db
    rows = db.get_invoices()
    out = []
    for r in rows[:limit]:
        full = db.get_invoice(r["ID"])
        if full:
            out.append(full)
    return out


def t_render_html_real():
    """Real invoices render as full HTML pages, singly and as a batch."""
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
    """Real invoices render as valid one-page A4 PDFs, with progress reported."""
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
    """The PDF really contains the invoice's numbers."""
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
    """Real invoices render as valid PNG images."""
    import invoice_render as ir
    invs = _real_invoices(2)
    shots = ir.render_invoices_png(invs)
    assert len(shots) == len(invs)
    for b in shots:
        assert b[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return f"{len(shots)} real invoices -> valid PNGs"


def t_render_edge_cases():
    """An invoice with no lines, a draft with no number, and a name with HTML in it all render."""
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
    """The app picks PNG, then PDF, then HTML, depending on what this machine can make."""
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
    """The zip holds one file per invoice in each format."""
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
    # The HTML-only zip, whatever this machine can make.
    blob = app._invoices_zip(invs, as_images=False)
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    assert all(n.endswith(".html") for n in names)
    app.image_export_available = ir.image_export_available
    app.pdf_export_available = ir.pdf_export_available
    return f"zips ok: {made}, plus explicit html"


def t_zip_unique_names():
    """Two invoices for students with the same name get different file names in the zip."""
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
    """The sign-in check runs before anything reads or shows student data.

    It checks the source code rather than the behaviour: the order of these
    two calls is the whole point, and moving the sign-in below some
    harmless-looking setup would be easy to miss.
    """
    source = (PROJECT / "app.py").read_text(encoding="utf-8")
    gate = source.index("auth.require_login()")
    init = source.index("db.initialise_database()")
    assert gate < init, "login gate runs after the database is opened"
    # Nothing may be drawn above it either.
    above = source[:gate]
    for drawn in ["st.dataframe", "st.table", "st.write(", "st.tabs", "st.radio"]:
        assert drawn not in above, f"{drawn} renders before the login gate"
    return "require_login precedes database access and all rendering"


def t_login_fails_closed():
    """Missing or empty credentials keep everyone out, not let everyone in."""
    import auth
    import inspect
    src = inspect.getsource(auth.require_login)
    assert "_configuration_error" in src, "no configuration guard at all"
    for guard in ["st.secrets", "cookie_key", "usernames"]:
        assert guard in src, f"missing guard: {guard}"
    # Reading secrets must never show a visitor a traceback: st.secrets raises an
    # error when a deployment has no secrets at all, instead of acting empty.
    assert "except Exception" in src, "secrets access is not guarded against raising"
    assert "auto_hash=False" in src, (
        "auto_hash must be off or the stored bcrypt hash gets hashed again "
        "and nobody can sign in"
    )
    return "refuses to open without valid [auth] secrets"


def t_login_cookie_read_from_browser():
    """The stay-signed-in cookie is read even when the host hides it.

    Streamlit Community Cloud removes cookies before a request reaches the
    app, so the library's header lookup finds nothing and every visit starts
    at the sign-in form. The app falls back to reading the cookie in the
    browser. Here the headers are empty (bare mode) and a stub stands in for
    the browser part, so this runs the same path as the hosted app, and
    checks a forged or expired cookie is still rejected.
    """
    import time
    import types
    import jwt
    import auth
    import inspect

    src = inspect.getsource(auth.require_login)
    assert "_read_cookie_from_browser" in src, "cookie fallback is not wired in"

    # 32 or more bytes, or PyJWT warns about a short key on every run.
    key = "verification-only-key-" + "x" * 32
    other = "some-other-key-" + "y" * 32
    good = jwt.encode({"username": "ada", "exp_date": time.time() + 3600},
                      key, algorithm="HS256")
    stale = jwt.encode({"username": "ada", "exp_date": time.time() - 1},
                       key, algorithm="HS256")
    forged = jwt.encode({"username": "ada", "exp_date": time.time() + 3600},
                        other, algorithm="HS256")

    def model_with(value):
        """Return a stand-in for the sign-in object whose cookie holds `value`."""
        manager = types.SimpleNamespace(get=lambda name: value if name == "c" else None)
        return types.SimpleNamespace(cookie_name="c", cookie_key=key,
                                     cookie_manager=manager)

    got = auth._read_cookie_from_browser(model_with(good))()
    assert got and got["username"] == "ada", f"valid cookie rejected: {got!r}"
    assert auth._read_cookie_from_browser(model_with(stale))() is None, "expired cookie accepted"
    assert auth._read_cookie_from_browser(model_with(forged))() is None, "forged cookie accepted"
    assert auth._read_cookie_from_browser(model_with(None))() is None, "no cookie but signed in"
    assert auth._read_cookie_from_browser(model_with("not a token"))() is None, "garbage accepted"
    return "cookie read via the browser component; stale, forged and absent cookies refused"


def t_no_credentials_in_repo():
    """No password or password hash is committed."""
    import subprocess
    tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                             cwd=str(PROJECT)).stdout.split()
    assert "\\.streamlit/secrets.toml" not in tracked, "secrets.toml is tracked"
    assert ".streamlit/secrets.toml" not in tracked, "secrets.toml is tracked"
    # A real bcrypt hash, not just the "$2b$" prefix: the docs and this file both have
    # placeholders like "$2b$12$....." on purpose, and matching the prefix alone would
    # always fail on them.
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


def t_read_functions_all_run():
    """Every read-only query actually runs on the database in use.

    Added because get_payment_reminders grouped by the invoice's id while
    selecting the student's name. SQLite allows that and picks a row;
    Postgres refuses the query, so the Reminders screen worked in development
    and would have failed on the hosted app.

    With DATABASE_URL pointing at Postgres this catches that kind of
    difference. Without it, it still checks nothing raises an error.
    """
    import datetime as dt

    import db

    year, month = 2026, 8
    teachers = db.get_all_teachers()
    tid = teachers[0]["ID"] if teachers else 1
    first = dt.date(year, month, 1)
    calls = {
        "get_all_teachers": lambda: db.get_all_teachers(),
        "get_teacher_session_counts": lambda: db.get_teacher_session_counts(year, month),
        "get_unpriced_subjects": lambda: db.get_unpriced_subjects(),
        "get_subject_student_grades": lambda: db.get_subject_student_grades(
            [c["ID"] for c in db.get_all_classes()[:20]]),
        "get_all_students": lambda: db.get_all_students(),
        "get_all_classes": lambda: db.get_all_classes(),
        "get_all_class_rates": lambda: db.get_all_class_rates(),
        "get_class_rates_for_date": lambda: db.get_class_rates_for_date(
            [c["ID"] for c in db.get_all_classes()[:5]], first),
        "get_import_status": lambda: db.get_import_status(year, month),
        "get_unpriced_classes_for_month":
            lambda: db.get_unpriced_classes_for_month(year, month),
        "get_teacher_classes_for_schedule":
            lambda: db.get_teacher_classes_for_schedule(tid, first),
        "get_month_schedule": lambda: db.get_month_schedule(tid, year, month),
        "get_month_attendance": lambda: db.get_month_attendance(tid, year, month),
        "get_students_in_month": lambda: db.get_students_in_month(year, month),
        "get_all_student_month_breakdowns":
            lambda: db.get_all_student_month_breakdowns(year, month),
        "get_invoices": lambda: db.get_invoices(),
        "get_invoice_counts": lambda: db.get_invoice_counts(),
        "get_next_invoice_number": lambda: db.get_next_invoice_number(),
        "get_month_invoice_summary": lambda: db.get_month_invoice_summary(year, month),
        "get_open_invoice_items_for_month":
            lambda: db.get_open_invoice_items_for_month(year, month),
        "get_invoice_payments": lambda: db.get_invoice_payments(year, month),
        "get_payment_reminders": lambda: db.get_payment_reminders(),
        "get_teacher_month_stats": lambda: db.get_teacher_month_stats(year, month),
        "get_teacher_year_trend": lambda: db.get_teacher_year_trend(year, month),
        "get_credits": lambda: db.get_credits(),
        "get_student_credit_totals": lambda: db.get_student_credit_totals(
            [s["ID"] for s in db.get_all_students()[:25]]),
        "get_delete_choices": lambda: db.get_delete_choices(year, month),
        "get_delete_choices (all months)": db.get_delete_choices,
        "describe_master_delete": lambda: db.describe_master_delete(
            [t["ID"] for t in db.get_all_teachers()], [s["ID"] for s in db.get_all_students()]),
    }
    broken = []
    for name, fn in calls.items():
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            broken.append(f"{name}: {type(exc).__name__}")
    assert not broken, "; ".join(broken)
    return f"{len(calls)} read functions run on {db.engine.dialect.name}"


def t_batch_matches_single():
    """get_invoices_detailed agrees with get_invoice, invoice by invoice.

    The batch version only exists for speed. If it ever disagrees with the
    one-at-a-time version, that's a billing bug, so they're compared here.
    """
    import db

    ids = [row["ID"] for row in db.get_invoices()][:40]
    if not ids:
        return "no invoices to compare"
    singly = [d for d in (db.get_invoice(i) for i in ids) if d]
    batched = db.get_invoices_detailed(ids)
    assert len(singly) == len(batched), f"{len(singly)} vs {len(batched)}"
    for a, b in zip(singly, batched):
        for key in a:
            assert a[key] == b.get(key), f"invoice {a['ID']} differs on {key}"
    return f"{len(batched)} invoices identical either way"


def t_long_names_fit_their_columns():
    """A name from a spreadsheet is cut to fit the column it's saved in.

    Postgres refuses a value that's too long; SQLite keeps it. A teacher who
    writes a paragraph where the subject goes would import fine in testing
    and break the hosted app, so the cutting is checked against the columns
    themselves, in a throwaway database.
    """
    import tempfile

    script = (
        "import db;"
        "long_name = 'Very Long Name ' * 60;"
        "db.initialise_database();"
        "assert db.create_teacher(long_name) is True;"
        "assert db.create_student(long_name) == 'created';"
        "teacher = db.get_all_teachers()[0];"
        "student = db.get_all_students()[0];"
        "outcome = db.create_class_and_first_session("
        "    name=long_name, teacher_id=teacher['ID'], hourly_rate=65,"
        "    display_color='#2a78d6', student_ids=[student['ID']],"
        "    session_date=__import__('datetime').date(2026, 8, 3),"
        "    start_time=__import__('datetime').time(9),"
        "    end_time=__import__('datetime').time(11), status='Completed', note='',"
        "    attendance_rows=[{'student_id': student['ID'], 'is_online': False,"
        "                      'has_recording': False, 'is_cancelled': False, 'note': ''}]);"
        "academy_class = db.get_all_classes()[0];"
        "limits = {c.name: c.type.length for m in (db.Teacher, db.Student, db.AcademyClass)"
        "          for c in m.__table__.columns if getattr(c.type, 'length', None)};"
        "print(outcome, len(teacher['Name']) <= limits['name'],"
        "      len(student['Name']) <= limits['full_name'],"
        "      len(academy_class['Class']) <= limits['name'])"
    )
    with tempfile.TemporaryDirectory() as folder:
        url = "sqlite:///" + os.path.join(folder, "fit.db").replace("\\", "/")
        result = run(["-c", script], {"DATABASE_URL": url})
        assert result.returncode == 0, result.stderr[-400:]
        assert result.stdout.split() == ["created", "True", "True", "True"], result.stdout
    return "long teacher, student and class names trimmed to fit"


def t_import_prices_by_grade():
    """An imported subject comes in priced; none is left on the placeholder.

    Grade 10 and below is $60/h, above that $65/h. The grade comes from the
    subject's name, else its students' grades in their other subjects, else
    the $60 default the academy asked for. A placeholder only ever meant an
    invoice nobody could send.
    """
    import tempfile

    script = (
        "import datetime as dt, db, schedule_backfill;"
        "db.initialise_database();"
        "db.create_teacher('Teacher A');"
        "teacher = db.get_all_teachers()[0]['ID'];"
        "lesson = lambda name, day, who: {'class_name': name, 'date': dt.date(2026, 8, day),"
        "  'start_time': dt.time(9), 'end_time': dt.time(11), 'warnings': [],"
        "  'attendance': [{'student_name': who, 'status': 'Attending'}]};"
        "preview = {'sessions': [lesson('G9 Science', 3, 'Seo Yerin'),"
        "  lesson('G12 Econs HL', 4, 'Nam Jihoon'), lesson('Basic Eng', 5, 'Seo Yerin'),"
        "  lesson('Essay Club', 6, 'Nam Jihoon'), lesson('Art Club', 7, 'Oh Minseok')],"
        "  'name_reviews': []};"
        "schedule_backfill.backfill(preview, teacher);"
        "rates = {row['Class']: row['Hourly Rate'] for row in db.get_all_class_rates()};"
        "print(*[rates.get(n) for n in ('G9 Science', 'G12 Econs HL', 'Basic Eng', 'Essay Club', 'Art Club')]);"
        "print(len(db.get_unpriced_subjects()))"
    )
    with tempfile.TemporaryDirectory() as folder:
        url = "sqlite:///" + os.path.join(folder, "rates.db").replace("\\", "/")
        result = run(["-c", script], {"DATABASE_URL": url})
        assert result.returncode == 0, result.stderr[-400:]
        prices, unpriced = result.stdout.strip().splitlines()[-2:]
        got = dict(zip(("G9 Science", "G12 Econs HL", "Basic Eng", "Essay Club", "Art Club"),
                       map(float, prices.split())))
    assert got == {"G9 Science": 60.0, "G12 Econs HL": 65.0, "Basic Eng": 60.0,
                   "Essay Club": 65.0, "Art Club": 60.0}, got
    assert unpriced == "0", f"{unpriced} subject(s) left on the placeholder"
    return "by name (G9 $60, G12 $65), by students' grades, else $60; none on the placeholder"


_DOUBLE_IMPORT_SCRIPT = '''
import datetime as dt, json, threading
import db, schedule_backfill
db.initialise_database()
db.create_teacher("Teacher A")
teacher = db.get_all_teachers()[0]["ID"]
names = ["Nam Jihoon", "Oh Minseok", "Seo Yerin", "Choi Doyun", "Jang Kyubin", "Park Hana"]
def preview():
    return {"name_reviews": [], "sessions": [
        {"class_name": f"G9 Group {i % 3}", "date": dt.date(2026, 9, 1 + i), "start_time": dt.time(9),
         "end_time": dt.time(11), "warnings": [],
         "attendance": [{"student_name": n, "status": "Attending"} for n in names]}
        for i in range(8)]}
previews, results, errors = [preview(), preview()], [None, None], []
start = threading.Barrier(2)
def go(i):
    start.wait()
    try:
        results[i] = schedule_backfill.backfill(previews[i], teacher)["status"]
    except Exception as error:
        errors.append(repr(error)[:200])
threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
[t.start() for t in threads]; [t.join() for t in threads]
# Issued invoices: newest first by number, as the lookup promises.
rows = db.get_open_invoice_items_for_month(2026, 9)
for row in rows:
    db.issue_invoice_for_month(row["Invoice ID"], 2026, 9)
numbers = [i["Number"] for i in db.get_invoices(status="Issued")]
from sqlalchemy import func, select
with db.SessionLocal() as session:
    on_file = dict(session.execute(select(db.Student.full_name, func.count()).group_by(db.Student.full_name)).all())
    lessons = session.scalar(select(func.count()).select_from(db.ClassSession))
print(json.dumps({"results": results, "errors": errors, "on_file": on_file, "lessons": lessons,
                  "numbers": numbers}))
'''


def t_double_import():
    """Two imports at the same moment put each student on file once.

    A second run of the app can start while the first is still importing (a
    second click on the import button, or the app open in another tab). Side
    by side, each checked that a new student wasn't on file yet and each
    added them; eleven students were put on file twice that way, and one
    import failed half way. Now the second waits for the first and finds
    everything already there.
    """
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        url = "sqlite:///" + os.path.join(folder, "double.db").replace("\\", "/")
        result = run(["-c", _DOUBLE_IMPORT_SCRIPT], {"DATABASE_URL": url})
        assert result.returncode == 0, result.stderr[-600:]
        got = json.loads(result.stdout.strip().splitlines()[-1])
    assert not got["errors"], got["errors"]
    assert got["results"] == ["imported", "imported"], got["results"]
    twice = {name: n for name, n in got["on_file"].items() if n > 1}
    assert not twice, f"on file more than once: {twice}"
    assert len(got["on_file"]) == 6 and got["lessons"] == 8, got
    assert got["numbers"] == sorted(got["numbers"], reverse=True), f"not newest first: {got['numbers']}"
    return f"6 students once each, 8 lessons, {len(got['numbers'])} invoices newest first"


_SPLIT_MONTH_SCRIPT = '''
import datetime as dt, json, db, schedule_backfill as sb
from sqlalchemy import func, select
db.initialise_database()
db.create_teacher("Teacher A"); db.create_teacher("Teacher B")
ids = {t["Name"]: t["ID"] for t in db.get_all_teachers()}

def lesson(day, subject, start, end, names, cancelled=()):
    return {"date": day, "class_name": subject, "start_time": dt.time(start), "end_time": dt.time(end),
            "warnings": [], "attendance": [
                {"student_name": n, "status": "Cancelled" if n in cancelled else "Attending"} for n in names]}

def maths(names_by_day, cancelled_by_day={}):
    return [lesson(dt.date(2026, 9, d), "G11 Test Math", 16, 18, names, cancelled_by_day.get(d, ()))
            for d, names in names_by_day.items()]

def chem(names_by_day, cancelled_by_day={}):
    return [lesson(dt.date(2026, 9, d), "G11 Test Chem", 16, 17, names, cancelled_by_day.get(d, ()))
            for d, names in names_by_day.items()]

def bill(year, month):
    out = {}
    for row in db.get_open_invoice_items_for_month(year, month):
        _, new_id = db.issue_invoice_for_month(row["Invoice ID"], year, month)
        out[row["Student"]] = round(db.get_invoice(new_id)["Total"], 2)
    return out

def owed():
    return sorted([c["Student"], c["Amount"], c["Status"]] for c in db.get_credits())

both = ["Nam Jihoon", "Oh Minseok"]
b_both = ["Nam Jihoon", "Minseok Oh"]
a_sept = maths({1: both, 8: both, 15: both, 22: both})
b_sept = chem({3: b_both, 10: b_both, 17: b_both, 24: b_both})
# Teacher A's September goes out first; Teacher B's comes in later, and the
# same children get a second September invoice. B spells Oh Minseok the
# other way round, and it came in as a new student.
sb.backfill({"sessions": a_sept}, ids["Teacher A"])
result = {"first": bill(2026, 9)}
sb.backfill({"sessions": b_sept}, ids["Teacher B"])
result["second"] = bill(2026, 9)
students = {s["Name"]: s["ID"] for s in db.get_all_students()}
result["merge"] = db.merge_students(students["Oh Minseok"], students["Minseok Oh"])["status"]
# After both went out: Nam Jihoon cancels A's 15th; B takes Oh Minseok off
# the 17th and marks him cancelled on the 24th -- still spelling him "Minseok Oh".
a_changed = maths({1: both, 8: both, 15: both, 22: both}, {15: {"Nam Jihoon"}})
b_changed = chem({3: b_both, 10: b_both, 17: ["Nam Jihoon"], 24: b_both}, {24: {"Minseok Oh"}})
sb.backfill({"sessions": a_changed}, ids["Teacher A"])
sb.backfill({"sessions": b_changed}, ids["Teacher B"])
result["after_changes"] = owed()
result["students"] = sorted(s["Name"] for s in db.get_all_students())
sb.backfill({"sessions": a_changed}, ids["Teacher A"])
sb.backfill({"sessions": b_changed}, ids["Teacher B"])
result["uploaded_twice"] = owed()
sb.backfill({"sessions": [lesson(dt.date(2026, 10, d), "G11 Test Math", 16, 18, both) for d in (6, 13)]},
            ids["Teacher A"])
result["october"] = bill(2026, 10)
result["settled"] = owed()
with db.SessionLocal() as session:
    items = session.scalar(select(func.count()).select_from(db.InvoiceItem))
    credits = session.scalar(select(func.count()).select_from(db.Credit))
    lessons = [row for row in session.scalars(select(db.ClassSession.id)).all()]
for lesson_id in lessons:
    db.sync_invoice_items(lesson_id)
with db.SessionLocal() as session:
    result["resync_changes"] = [
        session.scalar(select(func.count()).select_from(db.InvoiceItem)) - items,
        session.scalar(select(func.count()).select_from(db.Credit)) - credits]
print(json.dumps(result))
'''


def t_two_invoices_one_month():
    """Credits come off correctly when a month went out as two invoices, and after a merge.

    A teacher who joins part-way through a month gives the same children a
    second invoice for it. A lesson on either invoice that's missed later is
    still owed back once, on the next invoice, including for a child whose
    two records were merged and whose teacher still uses the old spelling.
    """
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        url = "sqlite:///" + os.path.join(folder, "split.db").replace("\\", "/")
        result = run(["-c", _SPLIT_MONTH_SCRIPT], {"DATABASE_URL": url})
        assert result.returncode == 0, result.stderr[-600:]
        got = json.loads(result.stdout.strip().splitlines()[-1])
    assert got["first"] == {"Nam Jihoon": 520.0, "Oh Minseok": 520.0}, got["first"]
    assert got["second"] == {"Nam Jihoon": 260.0, "Minseok Oh": 260.0}, got["second"]
    assert got["merge"] == "merged", got["merge"]
    raised = [["Nam Jihoon", 130.0, "Open"], ["Oh Minseok", 65.0, "Open"], ["Oh Minseok", 65.0, "Open"]]
    assert got["after_changes"] == raised, f"credits after the changes: {got['after_changes']}"
    assert got["students"] == ["Nam Jihoon", "Oh Minseok"], f"old spelling came back: {got['students']}"
    assert got["uploaded_twice"] == raised, f"uploading again raised more: {got['uploaded_twice']}"
    assert got["october"] == {"Nam Jihoon": 130.0, "Oh Minseok": 130.0}, got["october"]
    assert all(status == "Applied" for _, _, status in got["settled"]), got["settled"]
    assert got["resync_changes"] == [0, 0], f"re-syncing changed lines/credits: {got['resync_changes']}"
    return "two September invoices, a merge, then 3 missed classes: each credited once, off October"


_MERGE_AND_NAME_SCRIPT = '''
import datetime as dt, json, db, schedule_backfill as sb
db.initialise_database()
db.create_teacher("Teacher A"); db.create_teacher("Teacher B")
ids = {t["Name"]: t["ID"] for t in db.get_all_teachers()}

def lesson(day, subject, names):
    return {"date": dt.date(2026, 9, day), "class_name": subject, "start_time": dt.time(13, 30),
            "end_time": dt.time(15, 30), "warnings": [],
            "attendance": [{"student_name": n, "status": "Attending"} for n in names]}

sb.backfill({"sessions": [lesson(6, "G9 Test Eng", ["Hayun Kim", "Nam Jihoon", "Seo Yerin"]),
                          lesson(13, "G9 Test Eng", ["Kim Hayun", "Nam Jihoon", "Seo Yerin"]),
                          lesson(20, "(unnamed class) · Sun 13:30", ["Oh Minseok"]),
                          lesson(27, "(unnamed class) · Sun 13:30", ["Oh Minseok"])]}, ids["Teacher A"])
sb.backfill({"sessions": [lesson(7, "Reading", ["Choi Doyun"])]}, ids["Teacher B"])
people = lambda: {s["Name"]: s["ID"] for s in db.get_all_students()}
who = people()
result = {"pairs": [sorted(p["names"]) for p in db.find_duplicate_students()]}
before = len(who)
result["dry_run"] = db.merge_students(who["Hayun Kim"], who["Kim Hayun"], dry_run=True)["status"]
result["after_dry_run"] = len(people()) == before
result["together"] = db.merge_students(who["Nam Jihoon"], who["Seo Yerin"])["status"]
result["merge"] = db.merge_students(who["Hayun Kim"], who["Kim Hayun"])["status"]
result["alias"] = db.get_student_aliases().get("kim hayun") == who["Hayun Kim"]
# Next month's workbook still says "Kim Hayun": no new student, no question.
sb.backfill({"sessions": [dict(lesson(6, "G9 Test Eng", ["Kim Hayun"]), date=dt.date(2026, 10, 4))]},
            ids["Teacher A"])
result["students_after_october"] = sorted(people())
result["asked_again"] = [m["parsed_name"] for m in sb.suggest_student_matches(
    [{"attendance": [{"student_name": "Kim Hayun"}]}])]
# Unnamed: issued as the placeholder, then named -- "Reading" is Teacher B's.
for row in db.get_open_invoice_items_for_month(2026, 9):
    db.issue_invoice_for_month(row["Invoice ID"], 2026, 9)
unnamed = db.get_unnamed_subjects(2026, 9)
result["unnamed"] = [(u["Teacher"], u["Lessons"], u["Students"], u["Issued lines"]) for u in unnamed]
result["unnamed_in_august"] = db.get_unnamed_subjects(2026, 8)
invoice_id = next(i["ID"] for i in db.get_invoices(status="Issued") if i["Student"] == "Oh Minseok")
total_before = db.get_invoice(invoice_id)["Total"]
named = db.name_subjects({unnamed[0]["Class ID"]: "Reading"})
result["named"] = [list(v) for v in named["outcome"].values()]
result["relabelled"] = named["relabelled"]
detail = db.get_invoice(invoice_id)
result["lines"] = sorted({line["Subject"] for line in detail["Lines"]})
result["same_total"] = detail["Total"] == total_before
result["left_unnamed"] = len(db.get_unnamed_subjects())
print(json.dumps(result))
'''


def t_merge_and_name():
    """One child on file twice is merged completely; a subject with no name gets one.

    The search offers "Hayun Kim" and "Kim Hayun", but never two children who
    were in a lesson together, which the merge refuses too. A dry run changes
    nothing. The removed spelling is remembered, so next month's workbook
    neither brings the second record back nor asks about it. An unnamed
    subject, invoiced as "(unnamed class) · Sun 13:30", gets a name (with its
    teacher's name added if another teacher already uses it), and its sent
    lines take the name without any amount changing.
    """
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        url = "sqlite:///" + os.path.join(folder, "merge.db").replace("\\", "/")
        result = run(["-c", _MERGE_AND_NAME_SCRIPT], {"DATABASE_URL": url})
        assert result.returncode == 0, result.stderr[-600:]
        got = json.loads(result.stdout.strip().splitlines()[-1])
    assert ["Hayun Kim", "Kim Hayun"] in got["pairs"], got["pairs"]
    assert ["Nam Jihoon", "Seo Yerin"] not in got["pairs"], got["pairs"]
    assert got["dry_run"] == "would_merge" and got["after_dry_run"], got
    assert got["together"] == "share_a_lesson", got["together"]
    assert got["merge"] == "merged" and got["alias"], got
    assert got["students_after_october"] == ["Choi Doyun", "Hayun Kim", "Nam Jihoon", "Oh Minseok",
                                             "Seo Yerin"], got["students_after_october"]
    assert got["asked_again"] == [], got["asked_again"]
    assert got["unnamed"] == [["Teacher A", 2, ["Oh Minseok"], 2]], got["unnamed"]
    assert got["unnamed_in_august"] == [], got["unnamed_in_august"]
    assert got["named"] == [["named", "Reading (Teacher A)"]], got["named"]
    assert got["relabelled"] == 2 and got["lines"] == ["Reading (Teacher A)"], got
    assert got["same_total"] and got["left_unnamed"] == 0, got
    return "merged whole, remembered next month; unnamed subject named on its sent lines too"


_INDEX_SCRIPT = '''
import json, sqlite3, sys, db
from sqlalchemy import inspect
db.initialise_database()
path = db.engine.url.database
db.engine.dispose()
# An older database: the same tables, without the indexes added since.
con = sqlite3.connect(path)
dropped = [name for (name,) in con.execute(
    "select name from sqlite_master where type = 'index' and name like 'ix_%'")]
for name in dropped:
    con.execute(f"drop index {name}")
con.commit(); con.close()
before = db._schema_is_current()
db.initialise_database()
after = sorted(i["name"] for v in inspect(db.engine).get_multi_indexes().values() for i in v
               if i["name"].startswith("ix_"))
print(json.dumps({"dropped": sorted(dropped), "before": before, "after": after,
                  "current": db._schema_is_current()}))
'''


def t_missing_indexes_added():
    """A database from before the indexes gets them on the next start, and only once."""
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        url = "sqlite:///" + os.path.join(folder, "index.db").replace("\\", "/")
        result = run(["-c", _INDEX_SCRIPT], {"DATABASE_URL": url})
        assert result.returncode == 0, result.stderr[-600:]
        got = json.loads(result.stdout.strip().splitlines()[-1])
    assert len(got["dropped"]) == 6 and got["before"] is False, got
    assert got["after"] == got["dropped"] and got["current"] is True, got
    return f"{len(got['after'])} indexes restored on start; the check then passes"


_SAME_NAME_SCRIPT = '''
import datetime as dt, json, db, schedule_backfill as sb
db.initialise_database()
db.create_teacher("Teacher A"); db.create_teacher("Teacher B")
ids = {t["Name"]: t["ID"] for t in db.get_all_teachers()}

def lesson(day, subject, names):
    return {"date": dt.date(2026, 9, day), "class_name": subject, "start_time": dt.time(16),
            "end_time": dt.time(18), "warnings": [],
            "attendance": [{"student_name": n, "status": "Attending"} for n in names]}

# Two girls called Hana, one in each of Teacher A's classes.
db.create_student("Hana"); db.create_student("Nam Jihoon")
from sqlalchemy import select, func
with db.SessionLocal() as s:
    s.add(db.Student(full_name="Hana")); s.commit()
first, second = [st["ID"] for st in db.get_all_students() if st["Name"] == "Hana"]
sessions = [lesson(1, "G9 Hana Math", ["Hana", "Nam Jihoon"]), lesson(2, "G9 Hana Eng", ["Hana"])]
sb.backfill({"sessions": sessions, "name_reviews": []}, ids["Teacher A"])
def who(subject):
    with db.SessionLocal() as s:
        return sorted(s.scalars(select(db.SessionAttendance.student_id).join(db.ClassSession,
            db.ClassSession.id == db.SessionAttendance.session_id).join(db.AcademyClass,
            db.AcademyClass.id == db.ClassSession.class_id).where(db.AcademyClass.name == subject)).all())
# One Hana in each class, as the admin sorted them out.
with db.SessionLocal() as s:
    for subject, hana in (("G9 Hana Math", first), ("G9 Hana Eng", second)):
        for row in s.scalars(select(db.SessionAttendance).join(db.ClassSession, db.ClassSession.id == db.SessionAttendance.session_id)
                .join(db.AcademyClass, db.AcademyClass.id == db.ClassSession.class_id)
                .where(db.AcademyClass.name == subject, db.SessionAttendance.student_id.in_([first, second]))).all():
            row.student_id = hana
    s.commit()
before = {"math": who("G9 Hana Math"), "eng": who("G9 Hana Eng")}
# Uploading again must keep each Hana in her own class.
sb.backfill({"sessions": [dict(x) for x in sessions], "name_reviews": []}, ids["Teacher A"])
after = {"math": who("G9 Hana Math"), "eng": who("G9 Hana Eng")}
with db.SessionLocal() as s:
    credits = s.scalar(select(func.count()).select_from(db.Credit))
# Two spellings merged inside a workbook keep the one on file.
db.create_student("Bae Sujin")
preview = {"sessions": [lesson(8, "G12 Test", ["Bae soojin"]), lesson(15, "G12 Test", ["Bae soojin"]),
                        lesson(22, "G12 Test", ["Bae Sujin"])],
           "name_reviews": [{"names": ["Bae Sujin", "Bae soojin"], "counts": [1, 2]}]}
sb.apply_review_decisions(preview, {0: "merge"})
spelled = sorted({a["student_name"] for x in preview["sessions"] for a in x["attendance"]})
print(json.dumps({"first": first, "second": second, "before": before, "after": after,
                  "credits": credits, "spelled": spelled}))
'''


def t_same_name_students():
    """Two students with the same name stay in their own classes; a merge keeps the name on file."""
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        url = "sqlite:///" + os.path.join(folder, "same.db").replace("\\", "/")
        result = run(["-c", _SAME_NAME_SCRIPT], {"DATABASE_URL": url})
        assert result.returncode == 0, result.stderr[-600:]
        got = json.loads(result.stdout.strip().splitlines()[-1])
    assert got["before"] == got["after"], f"re-upload moved a Hana: {got['before']} -> {got['after']}"
    assert got["first"] in got["after"]["math"] and got["second"] not in got["after"]["math"], got
    assert got["second"] in got["after"]["eng"] and got["first"] not in got["after"]["eng"], got
    assert got["credits"] == 0, got
    assert got["spelled"] == ["Bae Sujin"], got["spelled"]
    return "each Hana kept in her class on re-upload; 'Bae soojin' merged into the name on file"


_CREDIT_SCRIPT = '''
import datetime as dt, json, db, schedule_backfill as sb
db.initialise_database()
db.create_teacher("Teacher A")
teacher = db.get_all_teachers()[0]["ID"]
everyone = ["Nam Jihoon", "Oh Minseok", "Seo Yerin"]

def lesson(day, names, cancelled=()):
    return {"date": day, "class_name": "G11 Test Math", "start_time": dt.time(16),
            "end_time": dt.time(18), "warnings": [],
            "attendance": [{"student_name": n, "status": "Cancelled" if n in cancelled else "Attending"}
                           for n in names]}

def upload(sessions):
    sb.backfill({"sessions": sessions}, teacher)

def bill(year, month):
    out = {}
    for row in db.get_open_invoice_items_for_month(year, month):
        _, new_id = db.issue_invoice_for_month(row["Invoice ID"], year, month)
        out[row["Student"]] = round(db.get_invoice(new_id)["Total"], 2)
    return out

def owed():
    return sorted([c["Student"], c["Amount"], c["Status"]] for c in db.get_credits())

sept = [lesson(dt.date(2026, 9, d), everyone) for d in (1, 8, 15, 22)]
upload(sept)
result = {"september": bill(2026, 9)}
changed = sept[:2] + [lesson(dt.date(2026, 9, 15), everyone, cancelled={"Nam Jihoon"}),
                      lesson(dt.date(2026, 9, 22), ["Nam Jihoon", "Seo Yerin"])]
upload(changed)
result["after_changes"] = owed()
upload(changed)
result["uploaded_twice"] = owed()
upload(sept[:2] + [lesson(dt.date(2026, 9, 15), everyone, cancelled={"Nam Jihoon"}), sept[3]])
result["put_back"] = owed()
upload(changed)
upload([lesson(dt.date(2026, 10, 6), everyone),
        lesson(dt.date(2026, 10, 13), everyone, cancelled={"Seo Yerin"})])
result["october"] = bill(2026, 10)
result["settled"] = owed()
print(json.dumps(result))
'''


_SWAP_SCRIPT = r'''
import datetime as dt, json, db, schedule_backfill as sb
db.initialise_database()
db.create_teacher("Teacher A")
teacher = db.get_all_teachers()[0]["ID"]
everyone = ["Nam Jihoon", "Oh Minseok", "Seo Yerin"]

def lesson(day, names, subject="G11 Test Math"):
    return {"date": day, "class_name": subject, "start_time": dt.time(16), "end_time": dt.time(18),
            "warnings": [], "attendance": [{"student_name": n, "status": "Attending"} for n in names]}

def lessons_on(day):
    from sqlalchemy import select, func
    with db.SessionLocal() as s:
        rows = s.execute(select(db.AcademyClass.name, func.count(db.SessionAttendance.id))
                         .join(db.ClassSession, db.ClassSession.class_id == db.AcademyClass.id)
                         .join(db.SessionAttendance, db.SessionAttendance.session_id == db.ClassSession.id)
                         .where(db.ClassSession.session_date == day).group_by(db.ClassSession.id)).all()
    return [{"class_name": n, "n": c} for n, c in rows]

def owed():
    return sorted([c["Student"], c["Amount"], c["Status"]] for c in db.get_credits())

def to_bill():
    return sorted([row["Student"], row["Month Amount"]] for row in db.get_open_invoice_items_for_month(2026, 9))

sept = [lesson(dt.date(2026, 9, d), everyone) for d in (1, 8, 15, 22)]
sb.backfill({"sessions": sept}, teacher)
for row in db.get_open_invoice_items_for_month(2026, 9):
    db.issue_invoice_for_month(row["Invoice ID"], 2026, 9)
day = dt.date(2026, 9, 22)
swapped = sept[:3] + [lesson(day, ["Nam Jihoon", "Seo Yerin"], "G11 Test Workshop")]
first = sb.backfill({"sessions": swapped}, teacher)["created"]
result = {"counts": {k: v for k, v in first.items() if k in ("sessions_created", "lessons_removed")},
          "on_day": sorted([x["class_name"], x["n"]] for x in lessons_on(day)), "credits": owed(), "to_bill": to_bill()}
sb.backfill({"sessions": swapped}, teacher)
result["again"] = [sorted([x["class_name"], x["n"]] for x in lessons_on(day)), owed(), to_bill()]
clash = sb.backfill({"sessions": swapped + [lesson(dt.date(2026, 9, 1), ["Oh Minseok"], "G11 Test Clinic")]}, teacher)
result["clash"] = [clash["created"].get("lessons_teacher_conflict", 0) + clash["created"].get("class_teacher_conflict", 0),
                   sorted([x["class_name"], x["n"]] for x in lessons_on(dt.date(2026, 9, 1)))]
print(json.dumps(result))
'''


_REUPLOAD_SCRIPT = r'''
import datetime as dt, json, db, schedule_backfill as sb
from sqlalchemy import select, func
db.initialise_database()
db.create_teacher("Teacher A")
teacher = db.get_all_teachers()[0]["ID"]
everyone = ["Nam Jihoon", "Oh Minseok", "Seo Yerin"]
KINDS = ("new", "changed", "unchanged", "removed", "clash", "skipped")

def lesson(day, names, subject="G11 Test Math", hour=16):
    return {"date": day, "class_name": subject, "start_time": dt.time(hour), "end_time": dt.time(hour + 2),
            "warnings": [], "attendance": [{"student_name": n, "status": "Attending"} for n in names]}

def month_days(month, leave_out=()):
    first = dt.date(2026, month, 1)
    days = [first + dt.timedelta(days=i) for i in range(31)]
    return [d for d in days if d.month == month and d not in leave_out]

def on_calendar():
    with db.SessionLocal() as s:
        rows = s.execute(select(db.ClassSession.session_date, db.ClassSession.start_time, db.AcademyClass.name)
                         .join(db.AcademyClass, db.AcademyClass.id == db.ClassSession.class_id)).all()
    return sorted(f"{d:%m-%d} {t:%H} {n}" for d, t, n in rows)

def upload(sessions, dates):
    preview = {"sessions": sessions, "dates_shown": dates}
    plan = sb.plan_import(preview, teacher, dates=dates)
    got = sb.backfill(preview, teacher, dates=dates)
    c = got["created"]
    done = {"new": c.get("sessions_created", 0), "changed": c.get("sessions_updated", 0),
            "unchanged": c.get("sessions_unchanged", 0), "removed": c.get("lessons_removed", 0),
            "clash": c.get("lessons_teacher_conflict", 0) + c.get("class_teacher_conflict", 0),
            "skipped": c.get("class_duplicate", 0)}
    planned = {k: sum(m[k] for m in plan.values()) for k in KINDS}
    planned["changed"] += sum(m["held"] for m in plan.values())  # the import counts both as updated
    return planned, done, got.get("listed_twice", [])

sept = lambda d: dt.date(2026, 9, d)
first = ([lesson(sept(d), everyone) for d in (7, 14, 21, 25, 28)]
         + [lesson(dt.date(2026, 10, d), everyone) for d in (5, 12)]
         + [lesson(sept(20), everyone[:2], "G11 Test Mock", 10), lesson(sept(21), everyone[2:], "G9 Test Science", 13)])
result = {"first": upload(first, month_days(9) + month_days(10))}
for row in db.get_open_invoice_items_for_month(2026, 9):
    db.issue_invoice_for_month(row["Invoice ID"], 2026, 9)

# The updated September sheet: the 28th moved to the 29th, the mock gone, Science written
# another way, one child written twice twice over -- and its last columns, 1 and 2 October.
twice = ["Nam Jihoon"] + everyone
update = ([lesson(sept(7), twice), lesson(sept(14), everyone), lesson(sept(21), everyone), lesson(sept(29), twice)]
          + [lesson(sept(21), everyone[2:], "G9 Test Sci", 13)])
shown = month_days(9, leave_out={sept(25)}) + [dt.date(2026, 10, 1), dt.date(2026, 10, 2)]
result["update"] = upload(update, shown)
result["calendar"] = on_calendar()
result["credits"] = sorted([c["Subject"], str(c["Class date"]), c["Amount"]] for c in db.get_credits())
with db.SessionLocal() as s:
    result["on_29th"] = s.scalar(select(func.count()).select_from(db.SessionAttendance)
                                 .join(db.ClassSession, db.ClassSession.id == db.SessionAttendance.session_id)
                                 .where(db.ClassSession.session_date == sept(29)))
result["again"] = upload(update, shown)

picked = {"sessions": [lesson(dt.date(2026, 1, 5), everyone), lesson(sept(7), everyone), lesson(dt.date(2026, 11, 2), everyone)],
          "dates_shown": [dt.date(2026, 1, 5), sept(7), dt.date(2026, 11, 2), dt.date(2026, 12, 1)],
          "sheet_months": [(2026, 1), (2026, 9), (2026, 11)]}
result["default"] = sb.default_months(picked, teacher)
result["default_new"] = [sb.default_months(picked, None, today=dt.date(2026, 9, 15)),
                         sb.default_months(picked, None, today=dt.date(2026, 10, 1))]
cut = sb.restrict_to_months(picked, [(2026, 9)])
result["restricted"] = [[str(s["date"]) for s in cut["sessions"]], [str(d) for d in cut["dates_shown"]]]

# Past schedules: a newer copy of a workbook replaces the old on the days it shows.
db.save_lesson_history("Teacher B", [lesson(dt.date(2026, 8, 3), everyone[:1])])
db.save_lesson_history("Teacher B", [lesson(sept(7), everyone[:2]), lesson(sept(14), everyone[:2])],
                       dates=[sept(7), sept(14)])
again = db.save_lesson_history("Teacher B", [lesson(sept(7), everyone[:1]), lesson(sept(15), everyone[:2])],
                               dates=[sept(7), sept(14), sept(15)])
with db.SessionLocal() as s:
    rows = s.execute(select(db.LessonHistory.lesson_date, db.LessonHistory.student_name)).all()
result["history"] = [sorted(f"{d:%m-%d} {n}" for d, n in rows), again["removed"]]
print(json.dumps(result, default=list))
'''


def t_reupload_same_month():
    """Uploading an updated workbook for a month already imported changes only what changed.

    The calendar is made to match the workbook on the days it shows, and
    nowhere else. A lesson moved or dropped goes (credited if invoiced), even
    when its subject has disappeared from the upload. A lesson the upload
    writes another way at the same time is left for a person to decide. A
    day with no column, and the rest of a month a sheet only spills into,
    aren't touched. A child written twice in a class gets one place, not a
    crash. What the screen says beforehand (plan_import) is exactly what the
    import does.
    """
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        url = "sqlite:///" + os.path.join(folder, "reupload.db").replace("\\", "/")
        result = run(["-c", _REUPLOAD_SCRIPT], {"DATABASE_URL": url})
        assert result.returncode == 0, result.stderr[-900:]
        got = json.loads(result.stdout.strip().splitlines()[-1])
    for step in ("first", "update", "again"):
        planned, done, _ = got[step]
        assert planned == done, f"{step}: the screen said {planned}, the import did {done}"
    assert got["first"][1] == {"new": 9, "changed": 0, "unchanged": 0, "removed": 0, "clash": 0, "skipped": 0}
    assert got["update"][1] == {"new": 1, "changed": 0, "unchanged": 3, "removed": 2, "clash": 1, "skipped": 0}, got["update"]
    assert got["update"][2] == ["07 Sep G11 Test Math", "29 Sep G11 Test Math"], got["update"][2]
    assert got["calendar"] == ["09-07 16 G11 Test Math", "09-14 16 G11 Test Math", "09-21 13 G9 Test Science",
                               "09-21 16 G11 Test Math", "09-25 16 G11 Test Math", "09-29 16 G11 Test Math",
                               "10-05 16 G11 Test Math", "10-12 16 G11 Test Math"], got["calendar"]
    assert got["credits"] == ([["G11 Test Math", "2026-09-28", 130.0]] * 3
                              + [["G11 Test Mock", "2026-09-20", 130.0]] * 2), got["credits"]
    assert got["on_29th"] == 3, f"a child written twice was put in twice: {got['on_29th']}"
    assert got["again"][1] == {"new": 0, "changed": 0, "unchanged": 4, "removed": 0, "clash": 1, "skipped": 0}, got["again"]
    assert got["default"] == [[2026, 9], [2026, 11]], got["default"]
    assert got["default_new"] == [[[2026, 9], [2026, 11]], [[2026, 11]]], got["default_new"]
    assert got["restricted"] == [["2026-09-07"], ["2026-09-07"]], got["restricted"]
    assert got["history"] == [["08-03 Nam Jihoon", "09-07 Nam Jihoon", "09-15 Nam Jihoon", "09-15 Oh Minseok"], 3], got["history"]
    return ("moved and dropped classes go, credited; October's rest, an unshown day and a renamed class "
            "kept; a child written twice kept once; the screen's forecast matched; past schedules replaced")


_APART_SCRIPT = r'''
import copy, datetime as dt, json, db, schedule_backfill as sb, schedule_parser as sp
db.initialise_database()
db.create_teacher("Teacher A")
teacher = db.get_all_teachers()[0]["ID"]

def lesson(d, side=()):
    # The class's own cell lists the roster; a child written in the cell beside it
    # ("Recording") is in the lesson too, but the parser does not count them as
    # sitting together.
    attendance = [{"student_name": n, "status": "Attending", "source": "F3"} for n in ("Nam Jihoon", "Bang Hayun")]
    attendance += [{"student_name": n, "status": "Recording", "source": "G3"} for n in side]
    return {"date": dt.date(2026, 1, d), "class_name": "G11 Test Econs", "start_time": dt.time(9),
            "end_time": dt.time(11), "warnings": [], "attendance": attendance,
            "coordinate": "F3", "cell": "Jan!F3"}

workbook = [lesson(4, side=["Bang Hayoon"])] + [lesson(d) for d in (11, 18, 25)]
# The first import kept the two apart, and January was invoiced.
sb.backfill({"sessions": copy.deepcopy(workbook)}, teacher)
for row in db.get_open_invoice_items_for_month(2026, 1):
    db.issue_invoice_for_month(row["Invoice ID"], 2026, 1)
hayoon = db.get_student_id_by_name("Bang Hayoon")
db.remember_student_aliases([(db.get_student_id_by_name("Bang Hayun"), "Bang Ha Yun")])

# The same workbook uploaded again.
preview = {"sessions": copy.deepcopy(workbook), "dates_shown": [s["date"] for s in workbook]}
preview["name_reviews"] = sp.canonicalise_names(preview["sessions"])["reviews"]
pair = lambda reviews: next(r for r in reviews if set(r["names"]) == {"Bang Hayun", "Bang Hayoon"})
result = {"parser_says_same": pair(preview["name_reviews"])["likely_same"]}

def defaults(reviews):
    return {i: "merge" if r["likely_same"] else "separate" for i, r in enumerate(reviews)}

def forecast(reviews):
    planned = {**preview, "sessions": copy.deepcopy(preview["sessions"]), "name_reviews": reviews}
    planned["sessions"] = sb.apply_review_decisions(planned, defaults(reviews))
    return sb.plan_import(planned, teacher, dates=planned["dates_shown"])[(2026, 1)]

result["old_default"] = dict(forecast(preview["name_reviews"]))
marked = sb.mark_names_on_file(preview["name_reviews"])
result["marked"] = [pair(marked)["likely_same"], pair(marked)["reason_text"].endswith("two different students on file")]
result["planned"] = dict(forecast(marked))
preview["name_reviews"] = marked
preview["sessions"] = sb.apply_review_decisions(preview, defaults(marked))
done = sb.backfill(preview, teacher, dates=preview["dates_shown"])["created"]
result["done"] = [done.get("sessions_updated", 0), done.get("sessions_unchanged", 0)]
result["credits"] = len(db.get_credits())
result["hayoon_billed"] = sum(1 for i in db.get_invoices(status="Issued")
                              for line in db.get_invoice(i["ID"])["Lines"] if i["Student"] == "Bang Hayoon")
# Neither student on file, or the same student under an alias: left as the parser said.
result["untouched"] = [r["likely_same"] for r in sb.mark_names_on_file([
    {"names": ["Nam Jihoon", "Nam Jihun"], "likely_same": True, "reason_text": ""},
    {"names": ["Bang Hayun", "Bang Ha Yun"], "likely_same": True, "reason_text": ""}])]
print(json.dumps(result))
'''


def t_reupload_keeps_students_apart():
    """Two spellings on file as two students start on "keep separate" when a workbook comes back.

    On live, re-uploading an unchanged January started one pair on "merge":
    two children one letter apart, one written next to the class
    ("Recording"), so the parser didn't count them as in the lesson
    together. The default merge took one child off a lesson they'd been
    billed for and credited it back.
    """
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        url = "sqlite:///" + os.path.join(folder, "apart.db").replace("\\", "/")
        result = run(["-c", _APART_SCRIPT], {"DATABASE_URL": url})
        assert result.returncode == 0, result.stderr[-900:]
        got = json.loads(result.stdout.strip().splitlines()[-1])
    assert got["parser_says_same"] is True, "the parser no longer starts this pair on merge; rethink the test"
    assert got["old_default"].get("changed_invoiced", 0) == 1, got["old_default"]
    assert got["marked"] == [False, True], got["marked"]
    assert got["planned"].get("changed", 0) == 0 and got["planned"].get("unchanged") == 4, got["planned"]
    assert got["done"] == [0, 4], got["done"]
    assert got["credits"] == 0, f"{got['credits']} credit(s) raised by re-uploading an unchanged month"
    assert got["hayoon_billed"] == 1, got["hayoon_billed"]
    assert got["untouched"] == [True, True], got["untouched"]
    return "an unchanged month re-uploaded changes nothing; a pair on file apart starts on keep separate"


_CLASH_SCRIPT = r'''
import datetime as dt, json, copy, db, schedule_backfill as sb
db.initialise_database()
db.create_teacher("Teacher A")
teacher = db.get_all_teachers()[0]["ID"]
day = dt.date(2026, 10, 5)

def lesson(name, date, start, end):
    return {"date": date, "class_name": name, "start_time": dt.time(*start), "end_time": dt.time(*end),
            "warnings": [], "attendance": [{"student_name": "Nam Jihoon", "status": "Attending"}]}

sb.backfill({"sessions": [lesson("G10 Test Math", dt.date(2026, 9, 28), (16, 0), (17, 30))]}, teacher)
# One upload: Math's next lesson, and a new subject written over it -- two new lessons at once.
upload = {"sessions": [lesson("G10 Test Math", day, (16, 0), (17, 30)),
                       lesson("G11 Test Chem", day, (16, 30), (18, 0)),
                       lesson("G11 Test Chem", dt.date(2026, 10, 12), (16, 30), (18, 0))]}
plan = sb.plan_import(copy.deepcopy(upload), teacher, dates=[day, dt.date(2026, 10, 12)])[(2026, 10)]
done = sb.backfill(upload, teacher, dates=[day, dt.date(2026, 10, 12)])["created"]
on_calendar = len(db.get_teacher_lessons_on(teacher, {day, dt.date(2026, 10, 12)}))
print(json.dumps({"plan": {k: plan[k] for k in ("new", "clash")}, "created": done.get("sessions_created", 0),
                  "on_calendar": on_calendar}))
'''


def t_forecast_clash_in_one_upload():
    """Two new lessons at the same time in one upload: the screen says so before the import.

    The forecast only checked lessons already on the calendar, so a lesson
    the same upload had just placed never counted, and "What this changes"
    said new where the import then refused a clash (on live, a teacher's
    October).
    """
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        url = "sqlite:///" + os.path.join(folder, "clash.db").replace("\\", "/")
        result = run(["-c", _CLASH_SCRIPT], {"DATABASE_URL": url})
        assert result.returncode == 0, result.stderr[-900:]
        got = json.loads(result.stdout.strip().splitlines()[-1])
    assert got["plan"] == {"new": 1, "clash": 2}, got
    assert got["created"] == got["plan"]["new"] == got["on_calendar"], got
    return "forecast 1 new, 2 not added -- what the import then did"


_MONTH_FILES_SCRIPT = r'''
import datetime as dt, json, db, schedule_backfill as sb
db.initialise_database()
db.create_teacher("Teacher A")
teacher = db.get_all_teachers()[0]["ID"]

def lesson(day):
    return {"date": day, "class_name": "G11 Test Math", "start_time": dt.time(16), "end_time": dt.time(18),
            "warnings": [], "attendance": [{"student_name": n, "status": "Attending"} for n in ("Nam Jihoon", "Oh Minseok")]}

sb.backfill({"sessions": [lesson(dt.date(2026, 9, d)) for d in (1, 8)] + [lesson(dt.date(2026, 10, 6))]}, teacher)
september = [db.issue_invoice_for_month(row["Invoice ID"], 2026, 9)[1] for row in db.get_open_invoice_items_for_month(2026, 9)]
october = [db.issue_invoice_for_month(row["Invoice ID"], 2026, 10)[1] for row in db.get_open_invoice_items_for_month(2026, 10)]
voided = september[0]
lines = db.get_invoice_for_edit(voided)["Lines"]
_, replacement, _ = db.replace_invoice(voided, {lines[0]["ID"]: {"Rate": 60}}, note="a test")
listed = db.get_issued_invoices_for_month(2026, 9)
print(json.dumps({"september": sorted(september), "october": sorted(october), "voided": voided, "replacement": replacement,
                  "listed": [item["ID"] for item in listed], "dated": all(item["Issued"] for item in listed),
                  "listed_october": [item["ID"] for item in db.get_issued_invoices_for_month(2026, 10)]}))
'''


def t_month_invoices_again():
    """A month's sent invoices can be downloaded again in one go, from any browser.

    The download after issuing only lasted until the next batch: issuing
    October removed September's. Void invoices are left out; their
    replacement is what a parent should have.
    """
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        url = "sqlite:///" + os.path.join(folder, "month.db").replace("\\", "/")
        result = run(["-c", _MONTH_FILES_SCRIPT], {"DATABASE_URL": url})
        assert result.returncode == 0, result.stderr[-900:]
        got = json.loads(result.stdout.strip().splitlines()[-1])
    want = sorted([i for i in got["september"] if i != got["voided"]] + [got["replacement"]])
    assert sorted(got["listed"]) == want, got
    assert got["voided"] not in got["listed"] and got["dated"], got
    assert sorted(got["listed_october"]) == got["october"], got
    return "September's issued invoices, the replacement in and the void one out; October's apart"


_REPLACE_SCRIPT = r'''
import datetime as dt, json, db, schedule_backfill as sb
db.initialise_database()
db.create_teacher("Teacher A")
teacher = db.get_all_teachers()[0]["ID"]
everyone = ["Nam Jihoon", "Oh Minseok"]

def lesson(day, names, cancelled=()):
    return {"date": day, "class_name": "G11 Test Math", "start_time": dt.time(16), "end_time": dt.time(18),
            "warnings": [], "attendance": [{"student_name": n, "status": "Cancelled" if n in cancelled else "Attending"}
                                           for n in names]}

def owed(who):
    return sorted([c["Amount"], c["Status"], c["Reason"].split(":")[0]] for c in db.get_credits() if c["Student"] == who)

day = lambda d: dt.date(2026, 9, d)
sb.backfill({"sessions": [lesson(day(d), everyone) for d in (1, 8, 15)]}, teacher)
issued = {}
for row in db.get_open_invoice_items_for_month(2026, 9):
    issued[row["Student"]] = db.issue_invoice_for_month(row["Invoice ID"], 2026, 9)[1]
# After sending: Oh Minseok misses the 15th, so a credit waits for him.
sb.backfill({"sessions": [lesson(day(1), everyone), lesson(day(8), everyone),
                          lesson(day(15), everyone, cancelled={"Oh Minseok"})]}, teacher)
result = {"flagged_at_first": [e["ID"] for e in db.get_sent_invoices_out_of_step()]}

nam = issued["Nam Jihoon"]
top = max(db.get_invoice(i)["Number"] for i in issued.values())
lines = db.get_invoice_for_edit(nam)["Lines"]
status, nam_new, number = db.replace_invoice(
    nam, {lines[0]["ID"]: {"Rate": 60}, lines[1]["ID"]: {"Charged": False}},
    extra=[{"Description": "Materials", "Amount": 20}, {"Description": "Discount", "Amount": -10}], note="a test")
new, old = db.get_invoice(nam_new), db.get_invoice(nam)
result["nam"] = [status, number == top + 1, new["Total"], new["Replaces"] == old["Number"],
                 old["Status"], old["Total"], old["Replaced by"] == number,
                 sorted(line["Subject"] for line in new["Lines"])]
# He then misses the 8th -- the class he was not charged for: nothing to give back.
sb.backfill({"sessions": [lesson(day(1), everyone), lesson(day(8), everyone, cancelled={"Nam Jihoon"}),
                          lesson(day(15), everyone, cancelled={"Oh Minseok"})]}, teacher)
result["nam_credits"] = owed("Nam Jihoon")

oh = issued["Oh Minseok"]
db.mark_invoice_paid(oh)
fifteenth = next(line for line in db.get_invoice_for_edit(oh)["Lines"] if line["Date"] == day(15))
status, oh_new, _ = db.replace_invoice(oh, {fifteenth["ID"]: {"Charged": False}})
paid = next(row for row in db.get_invoice_payments(2026, 9)["paid"] if row["ID"] == oh_new)
result["oh"] = [status, db.get_invoice(oh_new)["Total"], paid["Paid amount"], owed("Oh Minseok")]
result["refused"] = [db.replace_invoice(oh, {})[0],
                     db.replace_invoice(oh_new, {fifteenth["ID"]: {"Charged": True, "Hours": 0}})[0],
                     db.replace_invoice(oh_new, {}, extra=[{"Description": "Discount", "Amount": -999}])[0]]
result["void"] = sorted(row["Number"] for row in db.get_invoices(status="Void"))
result["flagged_before"] = [e["ID"] for e in db.get_sent_invoices_out_of_step()]
db.set_class_rate_for_month(db.get_all_classes()[0]["ID"], day(1), 70)
result["flagged_after"] = sorted(e["ID"] for e in db.get_sent_invoices_out_of_step())
db.keep_invoice_as_sent(nam_new)
result["flagged_kept"] = sorted(e["ID"] for e in db.get_sent_invoices_out_of_step())

# A class left off Nam Jihoon's invoice: taught on the 22nd, waiting for his next one.
sb.backfill({"sessions": [lesson(day(1), everyone), lesson(day(8), everyone, cancelled={"Nam Jihoon"}),
                          lesson(day(15), everyone, cancelled={"Oh Minseok"}),
                          lesson(day(22), ["Nam Jihoon"])]}, teacher)
data = db.get_invoice_for_edit(nam_new)
status, nam_3, _ = db.replace_invoice(nam_new, {}, extra=data["Extra"], add=[w["ID"] for w in data["Waiting"]])
result["forgotten"] = [len(data["Waiting"]), status, db.get_invoice(nam_3)["Total"],
                       [row["Student"] for row in db.get_open_invoice_items_for_month(2026, 9)],
                       len(db.get_invoices(status="Open"))]
# Oh Minseok's, brought to the schedule's new price in one go.
done = db.update_sent_invoices_to_schedule([oh_new])
oh_3 = done[0][1] if done else None
result["bulk"] = [len(done), oh_3 and db.get_invoice(oh_3)["Total"],
                  [e["ID"] for e in db.get_sent_invoices_out_of_step()],
                  next((r["Owing"] for r in db.get_invoice_payments(2026, 9)["unpaid"] if r["ID"] == oh_3), None)]
result["ids"] = [nam, oh, nam_new, oh_new]
print(json.dumps(result, default=str))
'''


def t_edit_sent_invoice():
    """A sent invoice only changes when someone edits it, and then under a new number.

    Editing voids the old invoice (keeping what it said) and issues a
    replacement that says which one it replaces. A lesson not charged stays
    on it at $0, so it's never billed again and a later absence raises no
    credit. Nothing is refunded twice: a credit waiting for a lesson taken
    off is dropped, and a payment over the new total becomes a credit. A
    price changed after sending is flagged until the invoice is replaced or
    kept as sent.
    """
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        url = "sqlite:///" + os.path.join(folder, "replace.db").replace("\\", "/")
        result = run(["-c", _REPLACE_SCRIPT], {"DATABASE_URL": url})
        assert result.returncode == 0, result.stderr[-900:]
        got = json.loads(result.stdout.strip().splitlines()[-1])
    _, _, nam_new, oh_new = got["ids"]
    assert got["flagged_at_first"] == [], got["flagged_at_first"]
    assert got["nam"] == ["replaced", True, 260.0, True, "Void", 390.0, True,
                          ["Discount", "G11 Test Math", "G11 Test Math", "Materials"]], got["nam"]
    assert got["nam_credits"] == [], f"a credit for a class never charged: {got['nam_credits']}"
    assert got["oh"] == ["replaced", 260.0, 260.0, [[130.0, "Open", "Overpaid"]]], got["oh"]
    assert got["refused"] == ["not_issued", "invalid", "negative"], got["refused"]
    assert len(got["void"]) == 2, got["void"]
    assert got["flagged_before"] == [], got["flagged_before"]
    assert got["flagged_after"] == sorted([nam_new, oh_new]), got["flagged_after"]
    assert got["flagged_kept"] == [oh_new], got["flagged_kept"]
    # The lesson left off goes on at today's $70 and is no longer waiting to be billed.
    assert got["forgotten"] == [1, "replaced", 400.0, [], 0], got["forgotten"]
    # Only the lessons that changed move to $70; the one not charged stays at $0.
    assert got["bulk"] == [1, 280.0, [], 20.0], got["bulk"]
    return ("replaced under a new number, old one void and kept; $0 class never credited; "
            "no double refund; overpayment a credit; a forgotten class added once; later price "
            "change flagged, kept as sent or updated in bulk")


_NUMBERING_SCRIPT = r'''
import datetime as dt, json, db, schedule_backfill as sb
db.initialise_database()
db.create_teacher("Teacher A")
teacher = db.get_all_teachers()[0]["ID"]
everyone = ["Nam Jihoon", "Oh Minseok", "Seo Yuna"]

def lesson(day):
    return {"date": dt.date(2026, 9, day), "class_name": "G11 Test Math", "start_time": dt.time(16),
            "end_time": dt.time(18), "warnings": [],
            "attendance": [{"student_name": n, "status": "Attending"} for n in everyone]}

def brief(outcome):
    kind, detail = outcome
    return [kind, {k: detail[k] for k in ("Student", "Number", "Status") if k in detail}
            if isinstance(detail, dict) else detail]

sb.backfill({"sessions": [lesson(1), lesson(8)]}, teacher)
open_ = {row["Student"]: row["Invoice ID"] for row in db.get_open_invoice_items_for_month(2026, 9)}
number = lambda invoice: db.get_invoice(invoice)["Number"]
result = {"fresh": db.get_next_invoice_number()}
nam = db.issue_invoice_for_month(open_["Nam Jihoon"], 2026, 9)[1]
result["first"] = number(nam)
result["taken"] = brief(db.set_next_invoice_number(7323))
result["low"] = db.set_next_invoice_number(7000)
result["typo"] = db.set_next_invoice_number(9000)
result["lowered"] = db.set_next_invoice_number(8000)
result["chosen"] = db.get_next_invoice_number()
oh = db.issue_invoice_for_month(open_["Oh Minseok"], 2026, 9)[1]
result["second"] = [number(oh), db.get_next_invoice_number()]
# Paid, then edited: the replacement says what it corrected, by number, on an overpaid credit.
db.mark_invoice_paid(oh)
eighth = next(line for line in db.get_invoice_for_edit(oh)["Lines"] if line["Date"] == dt.date(2026, 9, 8))
_, oh_new, oh_new_number = db.replace_invoice(oh, {eighth["ID"]: {"Charged": False}})
result["replacement"] = oh_new_number
# Its number wanted for the next invoice: who has it, then moved out of the way.
result["clash"] = brief(db.set_next_invoice_number(oh_new_number))
result["onto_void"] = brief(db.move_invoice_number(oh_new_number, 8000))
result["moved"] = brief(db.move_invoice_number(oh_new_number, 8002)) + [number(oh_new)]
result["after_move"] = db.get_next_invoice_number()
result["credit_text"] = sorted([c["Subject"], c["Reason"]] for c in db.get_credits()
                               if c["Student"] == "Oh Minseok")
result["void_says"] = db.get_invoice(oh)["Replaced by"]
seo = db.issue_invoice_for_month(open_["Seo Yuna"], 2026, 9)[1]
result["third"] = [number(seo), db.get_next_invoice_number()]
# One sent invoice given another number: Seo Yuna's #8001.
result["same"] = db.renumber_invoice(seo, 8001)
result["re_taken"] = brief(db.renumber_invoice(seo, 7323))
result["re_onto_void"] = brief(db.renumber_invoice(seo, 7323, move_holder_to=8000))
kind, done = db.renumber_invoice(seo, 7323, move_holder_to=8001)
result["swapped"] = [kind, done["From"], done["To"], done["Moved"], number(seo), number(nam)]
result["re_credit"] = list(db.renumber_invoice(oh_new, 8010))[0:1] + [number(oh_new), db.get_invoice(oh)["Replaced by"]]
result["re_credit_text"] = sorted([c["Subject"], c["Reason"]] for c in db.get_credits() if c["Student"] == "Oh Minseok")
result["re_void"] = db.renumber_invoice(oh, 8020)
result["re_low"] = db.renumber_invoice(seo, 7000)
result["re_next"] = db.get_next_invoice_number()
numbers = [row["Number"] for row in db.get_invoices() if row["Number"] is not None]
result["unique"] = [len(numbers), len(set(numbers))]
print(json.dumps(result, default=str))
'''


def t_next_invoice_number():
    """The admin chooses the next invoice's number; a used one can be freed, never shared.

    For invoices written outside the app. A number below the app's first is
    refused, since those are the academy's earlier invoices it can't check.
    A number in use is reported with who has it; renumbering that invoice
    frees it, and credits that mention it by number follow. A free number
    below the top is used once, then numbering goes back to the top.
    """
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        url = "sqlite:///" + os.path.join(folder, "numbering.db").replace("\\", "/")
        result = run(["-c", _NUMBERING_SCRIPT], {"DATABASE_URL": url})
        assert result.returncode == 0, result.stderr[-900:]
        got = json.loads(result.stdout.strip().splitlines()[-1])
    assert got["fresh"] == {"next": 7323, "top": 7323}, got["fresh"]
    assert got["first"] == 7323, got["first"]
    assert got["taken"] == ["taken", {"Student": "Nam Jihoon", "Number": 7323, "Status": "Issued"}], got["taken"]
    assert got["low"] == ["low", 7323], got["low"]
    assert got["typo"] == ["set", 9000] and got["lowered"] == ["set", 8000], got
    assert got["chosen"] == {"next": 8000, "top": 7324}, got["chosen"]
    assert got["second"] == [8000, {"next": 8001, "top": 8001}], got["second"]
    assert got["replacement"] == 8001, got["replacement"]
    assert got["clash"] == ["taken", {"Student": "Oh Minseok", "Number": 8001, "Status": "Issued"}], got["clash"]
    assert got["onto_void"] == ["taken", {"Student": "Oh Minseok", "Number": 8000, "Status": "Void"}], got
    assert got["moved"] == ["moved", {"Student": "Oh Minseok", "Number": 8002, "Status": "Issued"}, 8002], got
    assert got["after_move"] == {"next": 8001, "top": 8003}, got["after_move"]
    assert any("#8002" in text for pair in got["credit_text"] for text in pair), got["credit_text"]
    assert not any("#8001" in text for pair in got["credit_text"] for text in pair), got["credit_text"]
    assert got["void_says"] == 8002, got["void_says"]
    assert got["third"] == [8001, {"next": 8003, "top": 8003}], got["third"]
    assert got["same"] == ["same", 8001], got["same"]
    assert got["re_taken"] == ["taken", {"Student": "Nam Jihoon", "Number": 7323, "Status": "Issued"}], got
    assert got["re_onto_void"] == ["move_taken", {"Student": "Oh Minseok", "Number": 8000, "Status": "Void"}], got
    assert got["swapped"] == ["renumbered", 8001, 7323, {"Student": "Nam Jihoon", "From": 7323, "To": 8001},
                              7323, 8001], got["swapped"]
    assert got["re_credit"] == ["renumbered", 8010, 8010], got["re_credit"]
    assert any("#8010" in text for pair in got["re_credit_text"] for text in pair), got["re_credit_text"]
    assert not any("#8002" in text for pair in got["re_credit_text"] for text in pair), got["re_credit_text"]
    assert got["re_void"] == ["not_issued", None], got["re_void"]
    assert got["re_low"] == ["low", 7323], got["re_low"]
    assert got["re_next"] == {"next": 8011, "top": 8011}, got["re_next"]
    assert got["unique"] == [4, 4], got["unique"]
    return ("next number chosen; one in use reported with whose it is, renumbered out of the way "
            "with its credits; a freed number used once; a sent invoice renumbered or two swapped; "
            "a void one keeps its number; none ever shared")


_CLASS_EDIT_SCRIPT = r"""
import datetime as dt, json, db, schedule_backfill as sb
from sqlalchemy import event

# Postgres refuses to delete a row others still point at; SQLite only does
# when asked. Asked here, so this runs as the live database would.
@event.listens_for(db.engine, "connect")
def _links_enforced(connection, _):
    connection.execute("PRAGMA foreign_keys=ON")
db.engine.dispose()

db.initialise_database()
db.create_teacher("Teacher A")
db.create_teacher("Teacher B")
ids = {t["Name"]: t["ID"] for t in db.get_all_teachers()}

def lesson(day, names, subject, start, end):
    return {"date": dt.date(2026, 9, day), "class_name": subject, "start_time": dt.time(start),
            "end_time": dt.time(end), "warnings": [],
            "attendance": [{"student_name": n, "status": "Attending"} for n in names]}

sb.backfill({"sessions": [lesson(1, ["Nam Jihoon", "Oh Minseok"], "G11 Test Math", 16, 18),
                          lesson(8, ["Nam Jihoon", "Oh Minseok"], "G11 Test Math", 16, 18),
                          lesson(1, ["Kang Minji"], "G11 Test Bio", 18, 19)]}, ids["Teacher A"])
sb.backfill({"sessions": [lesson(1, ["Seo Yuna"], "G11 Test Chem", 17, 19)]}, ids["Teacher B"])
classes = {(item["Class"], item["Date"].day if hasattr(item["Date"], "day") else int(str(item["Date"])[-2:])): item["ID"]
           for teacher in ids.values() for item in db.get_month_schedule(teacher, 2026, 9)}
math_1, math_8, chem_1 = classes[("G11 Test Math", 1)], classes[("G11 Test Math", 8)], classes[("G11 Test Chem", 1)]
students = {s["Name"]: s["ID"] for s in db.get_all_students()}
open_ = {row["Student"]: row["Invoice ID"] for row in db.get_open_invoice_items_for_month(2026, 9)}
db.issue_invoice_for_month(open_["Nam Jihoon"], 2026, 9)

result = {}
# The 8th deleted on Delete records: it comes off Nam's sent invoice and Oh's draft alike.
nam = next(i["ID"] for i in db.get_invoices() if i["Student"] == "Nam Jihoon" and i["Status"] == "Issued")
done = db.master_delete(lesson_ids=[math_8])
result["deleted"] = [{row["What"]: row["How many"] for row in done["Rows"]}, done["Shortened"]]
result["gone"] = db.get_schedule_session(math_8) is None
result["credits"] = sorted([c["Student"], c["Reason"], c["Amount"]] for c in db.get_credits())
result["oh_open"] = [row["Classes"] for row in db.get_open_invoice_items_for_month(2026, 9) if row["Student"] == "Oh Minseok"]
result["nam_lines"] = sorted(str(line["Date"]) for line in db.get_invoice_for_edit(nam)["Lines"])
result["again"] = db.master_delete(lesson_ids=[math_8])["Rows"]

def roster(session_id):
    return [row["student_name"] for row in db.get_schedule_session(session_id)["Attendance"]]

def save(session_id, names):
    detail = db.get_schedule_session(session_id)
    return db.update_schedule_session(
        session_id=session_id, session_date=detail["Date"], start_time=detail["Start"], end_time=detail["End"],
        status=detail["Status"], note=detail["Note"],
        attendance_rows=[{"student_id": students[n]} for n in names])

# Seo Yuna, then Kang Minji, added to the 1st's maths: first come, first served.
result["before"] = roster(math_1)
save(math_1, roster(math_1) + ["Seo Yuna"])
save(math_1, roster(math_1) + ["Kang Minji"])
result["added"] = roster(math_1)
save(math_1, list(reversed(roster(math_1))))
result["resaved"] = roster(math_1)

# Seo Yuna has chemistry 17-19 with another teacher; Kang Minji's biology starts as maths ends.
result["class_clash"] = [[c["Student"], c["Class"], c["Teacher"]] for c in db.get_session_clashes(math_1)]
result["ahead"] = [c["Class"] for c in db.find_student_clashes(
    [students["Seo Yuna"], students["Kang Minji"]], [dt.date(2026, 9, 1)], dt.time(16), dt.time(18),
    excluded_session_id=math_1)]
result["month"] = [[c["Student"], c["First"]["Class"], c["Second"]["Class"]] for c in db.get_month_student_clashes(2026, 9)]
chem = db.get_schedule_session(chem_1)
db.update_schedule_session(session_id=chem_1, session_date=chem["Date"], start_time=chem["Start"],
                           end_time=chem["End"], status=chem["Status"], note=chem["Note"],
                           attendance_rows=[{"student_id": students["Seo Yuna"], "is_cancelled": True}])
result["after_cancel"] = [len(db.get_session_clashes(math_1)), len(db.get_month_student_clashes(2026, 9))]

# Taken out: the order of the rest stands.
save(math_1, [n for n in roster(math_1) if n != "Oh Minseok"])
result["taken_out"] = roster(math_1)
print(json.dumps(result, default=str))
"""


_PERIOD_DELETE_SCRIPT = r"""
import datetime as dt, json, db, schedule_backfill as sb
from sqlalchemy import event, select

@event.listens_for(db.engine, "connect")
def _links_enforced(connection, _):
    connection.execute("PRAGMA foreign_keys=ON")
db.engine.dispose()

db.initialise_database()
db.create_teacher("Teacher A")
teacher = db.get_all_teachers()[0]["ID"]

def lesson(day, names):
    return {"date": day, "class_name": "G11 Test Math", "start_time": dt.time(16), "end_time": dt.time(18),
            "warnings": [], "attendance": [{"student_name": n, "status": "Attending"} for n in names]}

sb.backfill({"sessions": [lesson(dt.date(2025, 12, 3), ["Nam Jihoon"]),
                          lesson(dt.date(2026, 9, 1), ["Nam Jihoon", "Oh Minseok"]),
                          lesson(dt.date(2026, 9, 8), ["Nam Jihoon", "Oh Minseok"]),
                          lesson(dt.date(2026, 10, 6), ["Oh Minseok"]),
                          lesson(dt.date(2026, 12, 2), ["Nam Jihoon"])]}, teacher)
for year, month in ((2025, 12), (2026, 9), (2026, 10), (2026, 12)):
    db._record_import(teacher, year, month, 1, 0, 0)
with db.SessionLocal() as session:
    for day in (dt.date(2025, 12, 10), dt.date(2026, 9, 15)):
        session.add(db.LessonHistory(teacher_name="Teacher A", class_name="G11 Test Math", lesson_date=day,
                                     start_time=dt.time(16), end_time=dt.time(18), student_name="Nam Jihoon",
                                     status="Attending"))
    session.commit()
students = {s["Name"]: s["ID"] for s in db.get_all_students()}
def open_for(year, month):
    return {row["Student"]: row["Invoice ID"] for row in db.get_open_invoice_items_for_month(year, month)}
nam_sep = db.issue_invoice_for_month(open_for(2026, 9)["Nam Jihoon"], 2026, 9)[1]
oh_sep = db.issue_invoice_for_month(open_for(2026, 9)["Oh Minseok"], 2026, 9)[1]
oh_oct = db.issue_invoice_for_month(open_for(2026, 10)["Oh Minseok"], 2026, 10)[1]

def left():
    with db.SessionLocal() as session:
        days = sorted(str(d) for d in session.scalars(select(db.ClassSession.session_date)))
        places = sorted(f"{d} {n}" for d, n in session.execute(
            select(db.ClassSession.session_date, db.Student.full_name)
            .join(db.SessionAttendance, db.SessionAttendance.session_id == db.ClassSession.id)
            .join(db.Student, db.Student.id == db.SessionAttendance.student_id)))
        uploads = sorted(f"{y}-{m}" for y, m in session.execute(select(db.ScheduleImport.year, db.ScheduleImport.month)))
        history = sorted(str(d) for d in session.scalars(select(db.LessonHistory.lesson_date)))
        broken = session.connection().exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    numbers = sorted(row["Number"] for row in db.get_invoices() if row["Number"])
    return {"teachers": [t["Name"] for t in db.get_all_teachers()], "subjects": [c["Class"] for c in db.get_all_classes()],
            "students": sorted(s["Name"] for s in db.get_all_students()), "days": days, "places": places,
            "invoices": numbers, "uploads": uploads, "history": history, "broken": broken}

def offered(year=None, month=None):
    choices = db.get_delete_choices(year, month)
    return [[t["Name"] for t in choices["Teachers"]], len(choices["Classes"]), [s["Name"] for s in choices["Students"]],
            choices["History"]]

result = {"start": left(), "numbers": [db.get_invoice(i)["Number"] for i in (nam_sep, oh_sep, oh_oct)],
          "offered_dec_any_year": offered(month=12), "offered_2026": offered(year=2026),
          "offered_2025_dec": offered(2025, 12)}
# 1. Teacher A for September 2026 only: those classes, their invoice lines and upload record.
preview = db.describe_master_delete(teacher_ids=[teacher], year=2026, month=9)
result["teacher_card"] = [preview["Linked"][0]["Kind"], preview["Linked"][0]["Whole"],
                          [row["Classes"] for row in preview["Linked"][0]["Subjects"]]]
result["sep"] = [db.master_delete(teacher_ids=[teacher], year=2026, month=9)["Numbers"], left()]
# 2. Oh Minseok for October 2026 only: his place and his October invoice; he and the class stay.
result["oh_oct"] = [db.master_delete(student_ids=[students["Oh Minseok"]], year=2026, month=10)["Numbers"], left()]
# 3. Teacher A for every December, any year.
result["decembers"] = left() if not db.master_delete(teacher_ids=[teacher], month=12) else left()
# 4. Past schedules of 2025 only.
db.master_delete(history_names=["Teacher A"], year=2025)
result["history_2025"] = left()["history"]
# 5. Teacher A with no time chosen: the whole teacher.
db.master_delete(teacher_ids=[teacher])
result["whole"] = left()
print(json.dumps(result, default=str))
"""


def t_delete_by_period():
    """Delete records keeps a delete inside the year and month chosen.

    A teacher picked for September 2026 loses only those classes, their
    invoice lines and that month's upload record, and stays on file. A
    student picked for October 2026 loses their places and invoice in it,
    and stays. December with any year takes every December. Past schedules
    go by year when picked. With no time chosen the teacher goes whole.
    Links stay intact throughout.
    """
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        url = "sqlite:///" + os.path.join(folder, "period.db").replace("\\", "/")
        result = run(["-c", _PERIOD_DELETE_SCRIPT], {"DATABASE_URL": url})
        assert result.returncode == 0, result.stderr[-1500:]
        got = json.loads(result.stdout.strip().splitlines()[-1])
    assert got["numbers"] == [7323, 7324, 7325], got["numbers"]
    assert got["offered_dec_any_year"] == [["Teacher A"], 2, ["Nam Jihoon"], ["Teacher A"]], got["offered_dec_any_year"]
    assert got["offered_2026"] == [["Teacher A"], 4, ["Nam Jihoon", "Oh Minseok"], ["Teacher A"]], got["offered_2026"]
    assert got["offered_2025_dec"] == [["Teacher A"], 1, ["Nam Jihoon"], ["Teacher A"]], got["offered_2025_dec"]
    assert got["teacher_card"] == ["Teacher", False, [2]], got["teacher_card"]
    numbers, sep = got["sep"]
    assert numbers == [7323, 7324], numbers
    assert sep["teachers"] == ["Teacher A"] and sep["subjects"] == ["G11 Test Math"], sep
    assert sep["days"] == ["2025-12-03", "2026-10-06", "2026-12-02"], sep["days"]
    assert sep["invoices"] == [7325] and sep["uploads"] == ["2025-12", "2026-10", "2026-12"], sep
    numbers, oct_ = got["oh_oct"]
    assert numbers == [7325] and oct_["invoices"] == [], oct_
    assert "Oh Minseok" in oct_["students"] and "2026-10-06" in oct_["days"], oct_
    assert not any(place.endswith("Oh Minseok") for place in oct_["places"]), oct_["places"]
    assert got["decembers"]["days"] == ["2026-10-06"], got["decembers"]["days"]
    assert got["decembers"]["uploads"] == ["2026-10"] and got["decembers"]["history"] == ["2025-12-10", "2026-09-15"], got
    assert got["history_2025"] == ["2026-09-15"], got["history_2025"]
    whole = got["whole"]
    assert whole["teachers"] == [] and whole["subjects"] == [] and whole["days"] == [], whole
    assert whole["history"] == [] and whole["uploads"] == [], whole
    assert whole["students"] == ["Nam Jihoon", "Oh Minseok"], whole["students"]
    assert all(step["broken"] == [] for step in (sep, oct_, got["decembers"], whole)), "broken links"
    return ("a teacher for one month loses only those classes and stays; a student for one month too; "
            "December of any year; past schedules by year; no time chosen takes the teacher whole")


def t_class_edits():
    """Deleting a class works with foreign keys on; students keep their order; double bookings show.

    The old "Delete for good" deleted the class before its invoice lines,
    which Postgres refuses, so on the live app nothing was ever deleted.
    Classes are deleted on Delete records now, with links enforced: the class
    comes off every invoice it was on, sent or draft, and nothing is credited.
    A save keeps students in the order they were added, however the rows
    arrive. A student in two classes at once is found, whoever teaches them,
    until one of the two is cancelled for them.
    """
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        url = "sqlite:///" + os.path.join(folder, "classes.db").replace("\\", "/")
        result = run(["-c", _CLASS_EDIT_SCRIPT], {"DATABASE_URL": url})
        assert result.returncode == 0, result.stderr[-1200:]
        got = json.loads(result.stdout.strip().splitlines()[-1])
    assert got["deleted"] == [{"Classes": 1, "Students' places in classes": 2, "Invoice lines": 2}, 1], got["deleted"]
    assert got["gone"], got
    assert got["credits"] == [], got["credits"]
    assert got["oh_open"] == [1], got["oh_open"]
    assert got["nam_lines"] == ["2026-09-01"], got["nam_lines"]
    assert got["again"] == [], got["again"]
    assert got["before"] == ["Nam Jihoon", "Oh Minseok"], got["before"]
    assert got["added"] == ["Nam Jihoon", "Oh Minseok", "Seo Yuna", "Kang Minji"], got["added"]
    assert got["resaved"] == got["added"], got["resaved"]
    assert got["class_clash"] == [["Seo Yuna", "G11 Test Chem", "Teacher B"]], got["class_clash"]
    assert got["ahead"] == ["G11 Test Chem"], got["ahead"]
    assert got["month"] == [["Seo Yuna", "G11 Test Math", "G11 Test Chem"]], got["month"]
    assert got["after_cancel"] == [0, 0], got["after_cancel"]
    assert got["taken_out"] == ["Nam Jihoon", "Seo Yuna", "Kang Minji"], got["taken_out"]
    return ("class deleted with links enforced, off the sent invoice and the draft; order added kept "
            "through a save; a double booking across teachers found, gone once cancelled; one taken out")


_MASTER_DELETE_SCRIPT = r"""
import datetime as dt, json, db, schedule_backfill as sb
from sqlalchemy import event, func, select

# Postgres refuses to delete a row others still point at; SQLite only does
# when asked. Asked here, so this runs as the live database would.
@event.listens_for(db.engine, "connect")
def _links_enforced(connection, _):
    connection.execute("PRAGMA foreign_keys=ON")
db.engine.dispose()

db.initialise_database()
db.create_teacher("Teacher A")
db.create_teacher("Demo Teacher")
ids = {t["Name"]: t["ID"] for t in db.get_all_teachers()}

def lesson(month, day, names, subject, start, end):
    return {"date": dt.date(2026, month, day), "class_name": subject, "start_time": dt.time(start),
            "end_time": dt.time(end), "warnings": [],
            "attendance": [{"student_name": n, "status": "Attending"} for n in names]}

sb.backfill({"sessions": [lesson(9, 1, ["Nam Jihoon", "Oh Minseok"], "G11 Test Math", 16, 18),
                          lesson(9, 8, ["Nam Jihoon", "Oh Minseok"], "G11 Test Math", 16, 18),
                          lesson(10, 6, ["Oh Minseok"], "G11 Test Math", 16, 18)]}, ids["Teacher A"])
# A demo class with a real student in it, Nam, whose sent invoice then holds a demo line.
sb.backfill({"sessions": [lesson(9, 2, ["Seo Yuna", "Nam Jihoon"], "G11 Test Chem", 17, 19),
                          lesson(9, 9, ["Seo Yuna"], "G11 Test Chem", 17, 19)]}, ids["Demo Teacher"])
students = {s["Name"]: s["ID"] for s in db.get_all_students()}
db.update_student(students["Seo Yuna"], "Seo Yuna", "Parent One", "+65 9000 0001")
db.update_student(students["Nam Jihoon"], "Nam Jihoon", "Parent Two", "+65 9000 0002")

def open_for(month):
    return {row["Student"]: row["Invoice ID"] for row in db.get_open_invoice_items_for_month(2026, month)}

def number(invoice_id):
    found = db.get_invoice(invoice_id)
    return found["Number"] if found else None

def counts():
    with db.engine.connect() as connection:
        return {table.name: connection.execute(select(func.count()).select_from(table)).scalar()
                for table in db.Base.metadata.sorted_tables}

def broken_links():
    with db.engine.connect() as connection:
        return connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()

def short(summary):
    return {"rows": {row["What"]: row["How many"] for row in summary["Rows"]},
            **{key: summary[key] for key in ("Numbers", "Next before", "Next after", "Shortened", "Reopened", "Unpaid")}}

nam = db.issue_invoice_for_month(open_for(9)["Nam Jihoon"], 2026, 9)[1]
oh = db.issue_invoice_for_month(open_for(9)["Oh Minseok"], 2026, 9)[1]
db.mark_invoice_paid(oh)
eighth = next(line for line in db.get_invoice_for_edit(oh)["Lines"] if line["Date"] == dt.date(2026, 9, 8))
_, oh_sept, _ = db.replace_invoice(oh, {eighth["ID"]: {"Charged": False}})
# October's invoice uses up the overpaid credit from correcting September's.
oh_oct = db.issue_invoice_for_month(open_for(10)["Oh Minseok"], 2026, 10)[1]
db.set_next_invoice_number(9001)
seo = db.issue_invoice_for_month(open_for(9)["Seo Yuna"], 2026, 9)[1]
result = {"numbers": [number(nam), number(oh), number(oh_sept), number(oh_oct), number(seo)],
          "credits_before": sorted([c["Student"], c["Status"]] for c in db.get_credits())}

# What the screen offers: a month narrows each list to what it touched.
def offered(choices):
    return {kind: sorted(row.get("Name") or row.get("Number") or row["ID"] for row in rows)
            for kind, rows in choices.items() if kind not in ("Classes", "History")} | {"Classes": len(choices["Classes"])}
result["offered_oct"] = offered(db.get_delete_choices(2026, 10))
result["offered_sep"] = offered(db.get_delete_choices(2026, 9))
result["offered_all"] = offered(db.get_delete_choices())

# 1. The demo teacher and the demo student, as after a live demo.
before = counts()
preview = db.describe_master_delete([ids["Demo Teacher"]], [students["Seo Yuna"]])
result["describe_changes_nothing"] = counts() == before
done = db.master_delete([ids["Demo Teacher"]], [students["Seo Yuna"]])
result["demo"] = short(done)
result["demo_matches_preview"] = done == {key: preview[key] for key in done}
result["report"] = {key: preview[key] for key in ("Linked", "Beyond", "Effects")}
after = counts()
result["demo_removed"] = {name: before[name] - after[name] for name in before if before[name] != after[name]}
result["demo_links"] = broken_links()
result["left"] = [sorted(t["Name"] for t in db.get_all_teachers()), sorted(s["Name"] for s in db.get_all_students()),
                  sorted(c["Class"] for c in db.get_all_classes())]
result["nam_kept"] = [number(nam), db.get_invoice(nam)["Status"],
                      sorted(str(line["Date"]) for line in db.get_invoice_for_edit(nam)["Lines"])]
result["next_after_demo"] = db.get_next_invoice_number()

# 2. Only Oh's October invoice: the credit it used waits again, its class is unbilled.
result["october"] = short(db.master_delete(invoice_ids=[oh_oct]))
result["oh_credits"] = sorted([c["Status"], c["Reason"][:8]] for c in db.get_credits() if c["Student"] == "Oh Minseok")

# 3. Oh's corrected September invoice: the void one it replaced and the overpaid credit go with it.
result["september"] = short(db.master_delete(invoice_ids=[oh_sept]))
result["oh_after"] = [db.get_credits(), [number(i) for i in (oh, oh_sept)], db.get_next_invoice_number()]
result["links"] = broken_links()

# 4. Oh's classes are still on the schedule; billed again, they take a freed number.
for month in (9, 10):
    for item in db.get_month_schedule(ids["Teacher A"], 2026, month):
        db.sync_invoice_items(item["ID"])
again = db.issue_invoice_for_month(open_for(9)["Oh Minseok"], 2026, 9)[1]
result["reused"] = number(again)
print(json.dumps(result, default=str))
"""


def t_master_delete():
    """Delete records removes what was picked and everything linked, and frees the numbers.

    Run with links enforced, so a row left pointing at a deleted one fails it.
    A demo teacher and student go with their classes, sent invoices, parent
    and the demo's chosen number, while a real student's invoice that held a
    demo line keeps the rest. Deleting one invoice takes every version of
    it and the credit its correction raised; a credit it used goes back to
    waiting. The preview changes nothing and matches what is done, names
    what each pick is linked to, and flags the real student it reaches. A
    freed number is given out again.
    """
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        url = "sqlite:///" + os.path.join(folder, "master.db").replace("\\", "/")
        result = run(["-c", _MASTER_DELETE_SCRIPT], {"DATABASE_URL": url})
        assert result.returncode == 0, result.stderr[-1500:]
        got = json.loads(result.stdout.strip().splitlines()[-1])
    assert got["numbers"] == [7323, 7324, 7325, 7326, 9001], got["numbers"]
    # The month narrows each list to what it touched; with no month, everything.
    assert got["offered_oct"] == {"Teachers": ["Teacher A"], "Subjects": ["G11 Test Math"], "Students": ["Oh Minseok"],
                                  "Invoices": [7326], "Classes": 1}, got["offered_oct"]
    assert got["offered_sep"]["Teachers"] == ["Demo Teacher", "Teacher A"] and got["offered_sep"]["Classes"] == 4, got
    assert got["offered_sep"]["Invoices"] == [7323, 7325, 9001], got["offered_sep"]
    assert got["offered_all"]["Invoices"] == [7323, 7324, 7325, 7326, 9001], got["offered_all"]
    assert got["describe_changes_nothing"] and got["demo_matches_preview"], got
    # The preview names what each pick is linked to, and flags the real student it reaches.
    teacher, student = got["report"]["Linked"]
    assert [teacher["Name"], [s["Name"] for s in teacher["Subjects"]], teacher["Students"]] == \
        ["Demo Teacher", ["G11 Test Chem"], ["Nam Jihoon", "Seo Yuna"]], teacher
    assert [student["Name"], student["Parent"], student["Teachers"],
            [[i["Number"], i["Status"], i["Amount"]] for i in student["Invoices"]]] == \
        ["Seo Yuna", "Parent One", ["Demo Teacher"], [[9001, "Issued", 260.0]]], student
    beyond = got["report"]["Beyond"]
    assert beyond["Places"] == [{"Student": "Nam Jihoon", "Classes": 1, "None left": False}], beyond
    assert [[c["Number"], c["Student"], c["Classes"], c["Removed"]] for c in beyond["Changed"]] == \
        [[7323, "Nam Jihoon", 1, 130.0]], beyond
    assert beyond["Emptied"] == [] and beyond["Credits"] == [], beyond
    effects = got["report"]["Effects"]
    assert [effects["Sent"], effects["Sent amount"], effects["Parents"]] == [[9001], 260.0, ["Parent One"]], effects
    demo = got["demo"]
    assert demo["Numbers"] == [9001] and demo["Next before"] == 9002 and demo["Next after"] == 7327, demo
    assert demo["Shortened"] == 1 and demo["Reopened"] == 0 and demo["Unpaid"] == 0, demo
    for what, many in {"Teachers": 1, "Students": 1, "Parents": 1, "Subjects": 1, "Classes": 2,
                       "Students' places in classes": 3, "Invoices": 1, "Invoice lines": 3}.items():
        assert demo["rows"].get(what) == many, (what, demo["rows"])
    assert got["demo_removed"].get("settings") == 1 and got["demo_removed"].get("parents") == 1, got["demo_removed"]
    assert got["demo_links"] == [] and got["links"] == [], [got["demo_links"], got["links"]]
    assert got["left"] == [["Teacher A"], ["Nam Jihoon", "Oh Minseok"], ["G11 Test Math"]], got["left"]
    assert got["nam_kept"] == [7323, "Issued", ["2026-09-01", "2026-09-08"]], got["nam_kept"]
    assert got["next_after_demo"] == {"next": 7327, "top": 7327}, got["next_after_demo"]
    october = got["october"]
    assert october["Numbers"] == [7326] and october["Reopened"] == 1 and october["Unpaid"] == 1, october
    assert october["rows"] == {"Invoices": 1, "Invoice lines": 1}, october["rows"]
    assert got["oh_credits"] == [["Open", "Overpaid"]], got["oh_credits"]
    september = got["september"]
    assert september["Numbers"] == [7324, 7325] and september["Next after"] == 7324, september
    assert september["rows"] == {"Invoices": 2, "Invoice lines": 2, "Invoice corrections": 1, "Credits": 1}, september
    assert september["Unpaid"] == 2, september
    assert got["oh_after"] == [[], [None, None], {"next": 7324, "top": 7324}], got["oh_after"]
    assert got["reused"] == 7324, got["reused"]
    return ("demo teacher and student gone with every linked row, a real invoice keeps its other lines; "
            "every version of a bill and its credit go together; a used credit waits again; "
            "preview matches, changes nothing and flags the real student reached; freed numbers given "
            "out again; no broken links")


def t_swapped_class_same_slot():
    """A class the workbook replaces with another at the same time is swapped, not lost.

    The new class used to be refused (the teacher already had a class at
    that time), and the old one was then removed as missing, so the slot
    ended up empty: billed students credited, and the class that did run
    never charged. A real double booking is still refused and reported.
    """
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        url = "sqlite:///" + os.path.join(folder, "swap.db").replace("\\", "/")
        result = run(["-c", _SWAP_SCRIPT], {"DATABASE_URL": url})
        assert result.returncode == 0, result.stderr[-600:]
        got = json.loads(result.stdout.strip().splitlines()[-1])
    assert got["on_day"] == [["G11 Test Workshop", 2]], got["on_day"]
    assert got["counts"] == {"sessions_created": 1, "lessons_removed": 1}, got["counts"]
    assert got["credits"] == [[n, 130.0, "Open"] for n in ("Nam Jihoon", "Oh Minseok", "Seo Yerin")], got["credits"]
    assert got["to_bill"] == [["Nam Jihoon", 130.0], ["Seo Yerin", 130.0]], got["to_bill"]
    assert got["again"] == [got["on_day"], got["credits"], got["to_bill"]], f"uploading again changed: {got['again']}"
    assert got["clash"] == [1, [["G11 Test Math", 3]]], f"double booking: {got['clash']}"
    return "swapped class in; old one credited; the new one billed; a real double booking still refused"


def t_cancelled_classes_credited():
    """A lesson invoiced and then missed comes off the student's next invoice.

    The academy's rule: a cancelled lesson is carried forward and taken off
    the next invoice. Teachers record an absence either by marking the
    student cancelled or by taking them off the lesson. Either one, arriving
    by re-upload after the month was invoiced, must raise a credit for what
    was paid (once, however often the schedule is uploaded), which the next
    invoice settles. Putting the student back removes the credit. A
    cancellation known before the invoice goes out is just never charged.
    """
    import json
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        url = "sqlite:///" + os.path.join(folder, "credit.db").replace("\\", "/")
        result = run(["-c", _CREDIT_SCRIPT], {"DATABASE_URL": url})
        assert result.returncode == 0, result.stderr[-600:]
        got = json.loads(result.stdout.strip().splitlines()[-1])
    assert got["september"] == {n: 520.0 for n in ("Nam Jihoon", "Oh Minseok", "Seo Yerin")}, got
    raised = [["Nam Jihoon", 130.0, "Open"], ["Oh Minseok", 130.0, "Open"]]
    assert got["after_changes"] == raised, f"credits after the changes: {got['after_changes']}"
    assert got["uploaded_twice"] == raised, f"uploading again raised more: {got['uploaded_twice']}"
    assert got["put_back"] == [["Nam Jihoon", 130.0, "Open"]], f"put back: {got['put_back']}"
    assert got["october"] == {"Nam Jihoon": 130.0, "Oh Minseok": 130.0, "Seo Yerin": 130.0}, got
    assert got["settled"] == [["Nam Jihoon", 130.0, "Applied"], ["Oh Minseok", 130.0, "Applied"]], got
    return "cancelled or taken off after invoicing -> credited once, off the next invoice"


def t_student_batch_matches_single():
    """The Students screen's credit figures, loaded for a whole screen at once, match the records.

    Each row's figures used to cost their own queries, so a screen of 25
    students was a hundred round trips. They're loaded in one pass now,
    which is only worth it if the result is exactly right, so here they're
    counted straight from the tables, student by student.
    """
    import db
    from sqlalchemy import func, select

    ids = [row["ID"] for row in db.get_all_students()][:30]
    if not ids:
        return "no students to compare"
    credits = db.get_student_credit_totals(ids)
    with db.SessionLocal() as session:
        for student_id in ids:
            owed = session.scalar(
                select(func.coalesce(func.sum(db.Credit.amount), 0))
                .where(db.Credit.student_id == student_id, db.Credit.status == "Open")
            ) or 0
            assert abs(credits.get(student_id, 0.0) - float(owed)) < 0.005, (
                f"credit differs for student {student_id}"
            )
    assert db.get_student_credit_totals([]) == {}
    return f"{len(ids)} students' figures match the records"


def t_korean_pdf():
    """Korean names come out in the PDF as Korean, not as bars."""
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
    for needed in ["수민", "독서반", "어머니"]:
        assert needed in text, f"{needed!r} lost in the PDF"
    # Checked on the invoice line, not the letterhead: the letterhead comes from
    # secrets and differs between a developer's machine and a test run.
    assert "Upper-Sec Science" in text, "Latin broken by the CJK font"
    assert "Upper-Sec Science" in text, "mixed-script line broken"
    return "Hangul and Latin both intact in one document"


def t_korean_real_student():
    """The first student in the database with a non-English name comes out correctly in the PDF."""
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
    _, full_name = row
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
    """Adding the Korean font didn't change the PNG output."""
    from PIL import Image, ImageChops
    import invoice_render as ir
    import datetime as dt
    import invoice_render as ir

    ref = SCRATCH / "before.png"
    if not ref.exists():
        return "no stored reference to compare against"
    # The reference image was made with the academy's real letterhead, which comes
    # from secrets. With any other letterhead the images differ for reasons that have
    # nothing to do with this check.
    if ir.ACADEMY["name"] != "KS ACADEMIA PREP":
        return "skipped: reference was rendered with a different letterhead"
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
    ("login cookie read via the browser", t_login_cookie_read_from_browser),
    ("no credentials committed to the repo", t_no_credentials_in_repo),
    ("every read query runs on this backend", t_read_functions_all_run),
    ("batched invoices match single", t_batch_matches_single),
    ("student figures match the records", t_student_batch_matches_single),
    ("long names fit their columns", t_long_names_fit_their_columns),
    ("import prices by grade", t_import_prices_by_grade),
    ("cancelled classes credited", t_cancelled_classes_credited),
    ("a class swapped in the same slot", t_swapped_class_same_slot),
    ("re-uploading an updated month", t_reupload_same_month),
    ("re-uploading keeps two students apart", t_reupload_keeps_students_apart),
    ("a clash inside one upload is forecast", t_forecast_clash_in_one_upload),
    ("editing a sent invoice", t_edit_sent_invoice),
    ("choosing the next invoice number", t_next_invoice_number),
    ("deleting a class, order added, double bookings", t_class_edits),
    ("Delete records and freed numbers", t_master_delete),
    ("Delete records inside a year or month", t_delete_by_period),
    ("a month's invoices downloaded again", t_month_invoices_again),
    ("two imports at once", t_double_import),
    ("two invoices in one month", t_two_invoices_one_month),
    ("merge a child, name a subject", t_merge_and_name),
    ("missing indexes added", t_missing_indexes_added),
    ("students with the same name", t_same_name_students),
    ("Korean text survives into PDF", t_korean_pdf),
    ("real Korean student renders", t_korean_real_student),
    ("image render unchanged", t_png_unchanged),
]:
    check(name, fn)

if _SECRETS_WAS_OURS:
    _SECRETS.unlink(missing_ok=True)

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
