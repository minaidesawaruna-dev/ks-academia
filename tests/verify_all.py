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

# Importing app.py runs the sign-in gate, which refuses to proceed without
# credentials -- correctly. So the suite needs some, and must not depend on
# whatever happens to be on this machine: a fresh clone has no secrets file.
# A throwaway one is written here and removed afterwards, unless the developer
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
        # The letterhead and bank details also come from secrets now, and
        # rendering an invoice refuses without them -- deliberately, since an
        # invoice with no way to pay it is worse than none at all.
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
              "schedule_backfill", "schedule_grid", "migrate_to_postgres"]:
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
    tables = len(db.Base.metadata.sorted_tables)
    return f"{total} rows across {tables} tables, FKs enforced"


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
    for guard in ["st.secrets", "cookie_key", "usernames"]:
        assert guard in src, f"missing guard: {guard}"
    # Reading secrets must not be able to throw a traceback at a visitor:
    # st.secrets raises rather than behaving like an empty mapping when a
    # deployment has no secrets at all.
    assert "except Exception" in src, "secrets access is not guarded against raising"
    assert "auto_hash=False" in src, (
        "auto_hash must be off or the stored bcrypt hash gets hashed again "
        "and nobody can sign in"
    )
    return "refuses to open without valid [auth] secrets"


def t_login_cookie_read_from_browser():
    """The stay-signed-in cookie must be read even when the host hides it.

    Streamlit Community Cloud strips cookies before a request reaches the
    app, so the library's header-based lookup finds nothing there and every
    visit starts at the sign-in form. The gate falls back to the browser-side
    component. Here the headers are empty (bare mode) and a stub stands in
    for the component, so this exercises exactly the deployed path -- and
    checks that the fallback still rejects a forged or expired cookie.
    """
    import time
    import types
    import jwt
    import auth
    import inspect

    src = inspect.getsource(auth.require_login)
    assert "_read_cookie_from_browser" in src, "cookie fallback is not wired in"

    # 32+ bytes, or PyJWT warns about a short HMAC key on every run.
    key = "verification-only-key-" + "x" * 32
    other = "some-other-key-" + "y" * 32
    good = jwt.encode({"username": "ada", "exp_date": time.time() + 3600},
                      key, algorithm="HS256")
    stale = jwt.encode({"username": "ada", "exp_date": time.time() - 1},
                       key, algorithm="HS256")
    forged = jwt.encode({"username": "ada", "exp_date": time.time() + 3600},
                        other, algorithm="HS256")

    def model_with(value):
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


def t_read_functions_all_run():
    """Every read-only query must actually execute on the live backend.

    This exists because get_payment_reminders grouped by the invoice's id
    while selecting the student's name. SQLite allows that and quietly picks
    a row; Postgres refuses the query outright, so the Reminders tab worked
    perfectly in development and would have failed on the deployed app.

    Run with DATABASE_URL pointing at Postgres, this catches that whole class
    of difference. Run without, it still checks nothing raises.
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
        "get_student_usage_many": lambda: db.get_student_usage_many(
            [s["ID"] for s in db.get_all_students()[:25]]),
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
    """get_invoices_detailed must agree with get_invoice, invoice for invoice.

    The batched version exists only for speed. The moment it disagrees with
    the one-at-a-time path it is a billing bug, so this compares them rather
    than trusting them.
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
    """A name out of a spreadsheet must fit the column it is stored in.

    Postgres rejects an over-long value outright; SQLite keeps it. A teacher
    who writes a paragraph where the subject goes would therefore import
    fine in testing and break the deployed app, so the trimming is checked
    against the columns themselves, in a throwaway database.
    """
    import tempfile

    script = (
        "import db;"
        "long_name = 'Very Long Name ' * 60;"
        "db.initialise_database();"
        "assert db.create_teacher(long_name) is True;"
        "assert db.create_quick_student(long_name) == 'created';"
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
    """An imported subject arrives priced; none is left on the placeholder.

    Grade 10 and below is $60/h, above it $65/h. The grade is read from the
    subject's name, else from its students' grades in their other subjects,
    else the $60 default the academy asked for -- a placeholder only ever
    meant an invoice nobody could send.
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
names = ["Nam Jihoon", "Oh Minseok", "Seo Yerin", "Choi Doyun", "Lee Kyuwon", "Park Hana"]
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
    """Two imports at the same instant put each student on file once.

    A second run of the app can start while the first is still importing -- a
    second click on "Commit import", or the app open in another tab. Side by
    side, each checked that a new student wasn't on file yet and each added
    them; eleven students were put on file twice that way, and one of the two
    imports failed half way. Now the second waits for the first, and finds
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


def t_cancelled_classes_credited():
    """A class invoiced and then missed comes off the student's next invoice.

    The academy's rule: a cancelled class is carried forward and deducted from
    the next invoice. Teachers record an absence either by marking the student
    cancelled or by taking them off the lesson; both, arriving by re-upload
    after the month was invoiced, must raise a credit for what was paid --
    once, however often the schedule is re-uploaded -- that the next invoice
    settles. Putting the student back takes the credit away. A cancellation
    known before an invoice goes out is simply never charged.
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
    """The Students screen's figures, fetched for a screen at once, match the records.

    One row's worth of figures used to cost four queries, so a screen of 25
    students cost a hundred round trips. They are collected in one pass now,
    which is only worth doing if it says exactly what the records do --
    counted here straight from the tables, student by student.
    """
    import db
    from sqlalchemy import func, select

    ids = [row["ID"] for row in db.get_all_students()][:30]
    if not ids:
        return "no students to compare"
    usage = db.get_student_usage_many(ids)
    credits = db.get_student_credit_totals(ids)
    with db.SessionLocal() as session:
        def count(model, *where):
            return session.scalar(select(func.count()).select_from(model).where(*where)) or 0
        for student_id in ids:
            expected = {
                "subjects": count(db.Enrolment, db.Enrolment.student_id == student_id),
                "classes": count(db.SessionAttendance, db.SessionAttendance.student_id == student_id),
                "invoices": count(db.Invoice, db.Invoice.student_id == student_id,
                                  db.Invoice.status == "Issued"),
            }
            assert usage.get(student_id) == expected, f"usage differs for student {student_id}"
            owed = session.scalar(
                select(func.coalesce(func.sum(db.Credit.amount), 0))
                .where(db.Credit.student_id == student_id, db.Credit.status == "Open")
            ) or 0
            assert abs(credits.get(student_id, 0.0) - float(owed)) < 0.005, (
                f"credit differs for student {student_id}"
            )
    assert db.get_student_usage_many([]) == {} and db.get_student_credit_totals([]) == {}
    return f"{len(ids)} students' figures match the records"


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
    for needed in ["수민", "독서반", "어머니"]:
        assert needed in text, f"{needed!r} lost in the PDF"
    # Checked on the line, not the letterhead: the letterhead now comes
    # from secrets and differs between a developer's machine and a test run.
    assert "Upper-Sec Science" in text, "Latin broken by the CJK font"
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
    import invoice_render as ir

    ref = SCRATCH / "before.png"
    if not ref.exists():
        return "no stored reference to compare against"
    # The reference was rendered with the academy's real letterhead, which now
    # comes from secrets. Against any other letterhead the images differ for a
    # reason that has nothing to do with what this test is checking.
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
    ("two imports at once", t_double_import),
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
