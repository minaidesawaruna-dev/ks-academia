from pathlib import Path
from collections import defaultdict
from datetime import date, datetime, timedelta
from calendar import month_abbr, monthrange
from typing import Any
import os
import re

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Time,
    UniqueConstraint,
    create_engine,
    distinct,
    event,
    exists,
    func,
    inspect,
    or_,
    select,
    text,
)
from sqlalchemy.orm import declarative_base, sessionmaker


PROJECT_FOLDER = Path(__file__).resolve().parent
DATABASE_FILE = PROJECT_FOLDER / "ks_academia.db"

# The rate a freshly imported subject gets until somebody prices it. A rate
# of zero is rejected outright, so an import cannot simply leave the price
# blank -- it seeds this instead, and anything still on it counts as
# unpriced. Nothing in a tuition academy genuinely costs a dollar an hour.
UNSET_RATE = 1.0

# Where the data actually lives. Left alone this is the SQLite file sitting
# beside the code, which is what a developer running the app locally wants.
# Deployed, the host supplies a Postgres URL through the environment so that
# every admin works against one shared database instead of each carrying
# their own diverging copy. Nothing else in the app has to know which of the
# two it is talking to -- that is the whole reason for going through
# SQLAlchemy rather than the sqlite3 module directly.
#
# Neon and Heroku still hand out URLs on the older "postgres://" scheme,
# which SQLAlchemy 2.x refuses to load a dialect for; rewriting the prefix
# here saves every future deployment from the same puzzling crash on boot.
def _database_url() -> str:
    """The environment's database if it named one, else the local file.

    Streamlit Community Cloud takes its configuration through a secrets
    store. Root-level secrets are documented as also reaching the process
    environment when running locally, but the Cloud-side documentation stops
    short of promising the same, and the failure mode if that assumption is
    wrong is quiet and nasty: the app finds no DATABASE_URL, falls back to
    SQLite, creates an empty file on the host's disposable disk, and every
    admin gets their own blank academy instead of an error. Reading both
    sources costs nothing and removes the guess.

    The Streamlit import is deliberately local and guarded, because this
    module is also imported by migrate_to_postgres.py, which runs from a
    plain terminal with no Streamlit runtime and no secrets file.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        try:
            import streamlit as st

            url = str(st.secrets.get("DATABASE_URL", "")).strip()
        except Exception:
            url = ""
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url or f"sqlite:///{DATABASE_FILE.as_posix()}"


DATABASE_URL = _database_url()

def apply_sqlite_pragmas(target_engine):
    """Put an engine on WAL journalling instead of SQLite's default.

    The default mode fsyncs the whole database file on every single commit,
    which is fine for occasional writes but turns an Excel import -- hundreds
    of small commits, one per class written through the same functions the
    Timetable tab uses -- into a multi-second wait dominated almost entirely
    by disk sync rather than actual work. Measured on a full year's import:
    4.1s with these pragmas, 10.7s without. WAL lets readers and writers
    proceed without that per-commit fsync, and is the standard, safe setting
    for this access pattern -- a single local desktop app, not multiple
    processes writing at once.

    Exposed rather than applied inline so that anything building its own
    engine against a copy of the database (a test, a one-off script) can
    match how the app actually runs, instead of quietly measuring or
    behaving differently from production.
    """

    @event.listens_for(target_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return target_engine


if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL)
    apply_sqlite_pragmas(engine)
else:
    # A serverless Postgres (Neon, and others like it) parks its compute
    # after a few minutes of quiet, which silently kills every connection
    # sitting in the pool. Without pre-ping the first query after a lunch
    # break comes back as a stale-connection error rather than data, so
    # check each connection out with a cheap round trip and recycle well
    # inside the idle window. WAL pragmas are meaningless here -- they are
    # SQLite's journalling, not a portable setting.
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=280)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class Teacher(Base):
    __tablename__ = "teachers"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
    )


class Parent(Base):
    __tablename__ = "parents"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    phone = Column(String(30), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
    )


class Student(Base):
    __tablename__ = "students"

    id = Column(Integer, primary_key=True)
    full_name = Column(String(100), nullable=False)
    parent_id = Column(Integer, ForeignKey("parents.id"), nullable=True)
    note = Column(String(1000), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
    )


class AcademyClass(Base):
    __tablename__ = "classes"

    id = Column(Integer, primary_key=True)
    name = Column(String(150), nullable=False, unique=True)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    status = Column(String(20), nullable=False, default="Ongoing")
    display_color = Column(String(7), nullable=False, default="#4F81BD")
    note = Column(String(1000), nullable=True)
    # Retained internally for compatibility with databases created in Stage 3.
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
    )


class ClassSchedule(Base):
    __tablename__ = "class_schedules"

    id = Column(Integer, primary_key=True)
    class_id = Column(Integer, ForeignKey("classes.id"), nullable=False)
    day_of_week = Column(String(10), nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)


class Enrolment(Base):
    __tablename__ = "enrolments"
    __table_args__ = (
        UniqueConstraint("class_id", "student_id", name="uq_class_student"),
    )

    id = Column(Integer, primary_key=True)
    class_id = Column(Integer, ForeignKey("classes.id"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    created_at = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
    )


class ClassSession(Base):
    __tablename__ = "class_sessions"
    __table_args__ = (
        UniqueConstraint(
            "class_id",
            "session_date",
            "start_time",
            name="uq_class_session_time",
        ),
    )

    id = Column(Integer, primary_key=True)
    class_id = Column(Integer, ForeignKey("classes.id"), nullable=False)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    session_date = Column(Date, nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    status = Column(String(20), nullable=False, default="Scheduled")
    note = Column(String(500), nullable=True)
    created_at = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
    )


class SessionAttendance(Base):
    """One student's conditions for one dated class."""

    __tablename__ = "session_attendance"
    __table_args__ = (
        UniqueConstraint("session_id", "student_id", name="uq_session_student"),
    )

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("class_sessions.id"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    # Retained for compatibility with schedule imports made before Scheduling.
    status = Column(String(40), nullable=False, default="Attending")
    is_online = Column(Boolean, nullable=False, default=False)
    has_recording = Column(Boolean, nullable=False, default=False)
    is_cancelled = Column(Boolean, nullable=False, default=False)
    is_paid = Column(Boolean, nullable=False, default=False)
    on_roster = Column(Integer, nullable=False, default=1)
    source_cell = Column(String(40), nullable=True)
    note = Column(String(200), nullable=True)
    created_at = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
    )


class ClassRate(Base):
    __tablename__ = "class_rates"

    id = Column(Integer, primary_key=True)
    class_id = Column(Integer, ForeignKey("classes.id"), nullable=False)
    hourly_rate = Column(Numeric(10, 2), nullable=False)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=True)
    created_at = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
    )


class Invoice(Base):
    """A bill for one student.

    An invoice stays ``Open`` and collects a line for every class the student
    is added to.  Issuing it fixes the number, the date and the contents; the
    next class they join opens a new one.
    """

    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    status = Column(String(10), nullable=False, default="Open")   # Open | Issued
    invoice_number = Column(Integer, nullable=True)
    issued_on = Column(Date, nullable=True)
    note = Column(String(500), nullable=True)
    # Payment lives here rather than in `status` so that "issued" keeps
    # meaning "has been billed" everywhere it is already relied on -- the
    # earnings trend and the month summaries all filter on Issued, and a paid
    # invoice must not drop out of them. Paid is simply `paid_on is not None`.
    paid_on = Column(Date, nullable=True)
    paid_amount = Column(Numeric(10, 2), nullable=True)
    payment_note = Column(String(500), nullable=True)
    created_at = Column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class InvoiceItem(Base):
    """One class on one invoice.

    The link is to the class rather than to a copied amount, so an invoice
    still being drafted follows any correction to the class.  Issuing copies
    the figures across, so an issued invoice never changes afterwards.
    """

    __tablename__ = "invoice_items"
    __table_args__ = (
        UniqueConstraint("invoice_id", "session_id", name="uq_invoice_session"),
    )

    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False)
    # Nullable so an issued line can outlive the class it came from. Once an
    # invoice goes out its figures are frozen on the line itself, and the
    # class may later be deleted -- a subject removed, a class dropped from a
    # re-uploaded workbook. Detaching keeps the sent invoice printable and
    # leaves nothing pointing at a row that no longer exists. An open
    # invoice's line is deleted outright instead, so it never sees this.
    session_id = Column(Integer, ForeignKey("class_sessions.id"), nullable=True)
    # Filled in when the invoice is issued, so the figures stop moving.
    class_name = Column(String(120), nullable=True)
    teacher_name = Column(String(120), nullable=True)
    # Frozen beside the name, not instead of it. The name on a sent invoice
    # must never change, but reporting still has to find the teacher after a
    # rename -- matching the frozen text against today's names silently lost
    # every earlier month the moment somebody was renamed.
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=True)
    session_date = Column(Date, nullable=True)
    hours = Column(Numeric(5, 2), nullable=True)
    hourly_rate = Column(Numeric(8, 2), nullable=True)
    amount = Column(Numeric(10, 2), nullable=True)


class Credit(Base):
    """Money owed back to a student, waiting to come off a future invoice.

    A class the student cancelled costs them nothing. If that is known
    before their invoice goes out, the class is simply left off it and no
    credit is needed. Once an invoice has been issued the figures are
    frozen, so the correction has to travel forward instead: a credit is
    raised here and deducted from the next invoice, which is how the
    academy already settles these in practice. A refund is the same record,
    settled a different way, so the money is accounted for either way.

    ``class_name`` and ``session_date`` are copied in rather than read back
    through ``session_id`` so the credit still reads correctly on an invoice
    after the class it came from has been removed from the timetable.
    """

    __tablename__ = "credits"
    __table_args__ = (
        # One cancelled class can only ever owe a student once, however
        # many times the workbook is re-imported. SQLite treats NULLs as
        # distinct, so hand-written credits (no session) are unaffected.
        UniqueConstraint("student_id", "session_id", name="uq_credit_student_session"),
    )

    id = Column(Integer, primary_key=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    session_id = Column(Integer, ForeignKey("class_sessions.id"), nullable=True)
    amount = Column(Numeric(10, 2), nullable=False)
    class_name = Column(String(150), nullable=True)
    session_date = Column(Date, nullable=True)
    reason = Column(String(200), nullable=False, default="Cancelled class")
    status = Column(String(10), nullable=False, default="Open")  # Open|Applied|Refunded
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=True)
    created_on = Column(Date, nullable=False, default=date.today)
    settled_on = Column(Date, nullable=True)
    note = Column(String(500), nullable=True)


class ScheduleImport(Base):
    """One row per (teacher, year, month) an Excel schedule has been imported for.

    Upserted every time an import runs, so the Students tab can show which
    teachers have that month's workbook in the system yet.
    """

    __tablename__ = "schedule_imports"
    __table_args__ = (
        UniqueConstraint("teacher_id", "year", "month", name="uq_import_period"),
    )

    id = Column(Integer, primary_key=True)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    sessions_created = Column(Integer, nullable=False, default=0)
    sessions_updated = Column(Integer, nullable=False, default=0)
    warning_count = Column(Integer, nullable=False, default=0)
    imported_at = Column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


def _schema_is_current() -> bool:
    """Whether every table and column the app expects is already in place.

    ``initialise_database`` is cheap against a local SQLite file and ruinous
    against a remote Postgres: ``create_all``'s reflection plus the column
    inspections guarding each migration come to over fifteen hundred round
    trips. Beside the database that is six seconds; across an ocean -- the
    app is hosted in the United States and the database is in Singapore -- it
    is minutes. And it runs on *every* rerun, because Streamlit re-executes
    the script from the top on every interaction, so the app appeared to hang
    on every click.

    ``get_multi_columns`` reflects the whole schema in three queries. If
    nothing is missing there is, by definition, no table to create and no
    column to add, and all of that work can be skipped.

    Nullability is checked too, not just presence: one of the migrations
    below rebuilds ``invoice_items`` when ``session_id`` is still NOT NULL,
    and a check that only counted column names would skip it.
    """
    try:
        reflected = inspect(engine).get_multi_columns()
    except Exception:
        # Reflection is an optimisation, not the source of truth. If it
        # cannot be done, fall through to the slow, careful path.
        return False

    actual: dict[str, dict[str, Any]] = {
        key[1]: {column["name"]: column for column in columns}
        for key, columns in reflected.items()
    }
    for table in Base.metadata.sorted_tables:
        present = actual.get(table.name)
        if present is None:
            return False
        for column in table.columns:
            found = present.get(column.name)
            if found is None:
                return False
            if column.nullable and not found.get("nullable", True):
                return False
    return True


def initialise_database():
    """Create missing tables and apply the prototype's small SQLite upgrade.

    Returns immediately when the schema already matches, which on a remote
    database is the difference between a page that loads and one that looks
    like it has hung. See ``_schema_is_current``.
    """
    if _schema_is_current():
        return

    Base.metadata.create_all(engine)

    # create_all() creates new tables but does not add columns to old tables.
    # This preserves existing students while adding their parent link.
    student_columns = {
        column["name"] for column in inspect(engine).get_columns("students")
    }

    if "parent_id" not in student_columns:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE students "
                    "ADD COLUMN parent_id INTEGER REFERENCES parents(id)"
                )
            )

    class_tables = inspect(engine).get_table_names()
    if "classes" in class_tables:
        class_columns = {
            column["name"] for column in inspect(engine).get_columns("classes")
        }
        if "status" not in class_columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE classes "
                        "ADD COLUMN status VARCHAR(20) "
                        "NOT NULL DEFAULT 'Ongoing'"
                    )
                )
        if "display_color" not in class_columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE classes "
                        "ADD COLUMN display_color VARCHAR(7) "
                        "NOT NULL DEFAULT '#4F81BD'"
                    )
                )

    if "class_sessions" in class_tables:
        session_columns = {
            column["name"]
            for column in inspect(engine).get_columns("class_sessions")
        }
        if "note" not in session_columns:
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE class_sessions ADD COLUMN note VARCHAR(500)")
                )

    # Notes on students and subjects arrived after the first databases.
    for table_name in ("students", "classes"):
        if table_name in inspect(engine).get_table_names():
            existing = {
                column["name"] for column in inspect(engine).get_columns(table_name)
            }
            if "note" not in existing:
                with engine.begin() as connection:
                    connection.execute(
                        text(f"ALTER TABLE {table_name} ADD COLUMN note VARCHAR(1000)")
                    )

    # The import prototype created an earlier version of this table. Add the
    # independent condition fields needed by the timetable without data loss.
    if "session_attendance" in inspect(engine).get_table_names():
        attendance_columns = {
            column["name"]
            for column in inspect(engine).get_columns("session_attendance")
        }
        attendance_upgrades = {
            "is_online": "BOOLEAN NOT NULL DEFAULT 0",
            "has_recording": "BOOLEAN NOT NULL DEFAULT 0",
            "is_cancelled": "BOOLEAN NOT NULL DEFAULT 0",
            "is_paid": "BOOLEAN NOT NULL DEFAULT 0",
        }
        for column_name, definition in attendance_upgrades.items():
            if column_name not in attendance_columns:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            f"ALTER TABLE session_attendance "
                            f"ADD COLUMN {column_name} {definition}"
                        )
                    )

        # Part-billing was replaced by a paid flag, and part notes.  The old
        # column is NOT NULL, so leaving it in place makes every new insert
        # fail once the model stops supplying it.
        if "billing_fraction" in attendance_columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE session_attendance "
                        "DROP COLUMN billing_fraction"
                    )
                )

    # invoice_items.session_id used to be NOT NULL, which meant a class could
    # not be deleted while an issued invoice still referenced it without
    # leaving a dangling row behind. SQLite cannot relax NOT NULL in place, so
    # the table is rebuilt from the model and the rows copied across.
    if "invoice_items" in inspect(engine).get_table_names():
        item_columns = {
            column["name"]: column
            for column in inspect(engine).get_columns("invoice_items")
        }
        session_column = item_columns.get("session_id")
        if session_column is not None and not session_column["nullable"]:
            names = ", ".join(item_columns)
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE invoice_items RENAME TO invoice_items_old")
                )
            InvoiceItem.__table__.create(engine)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        f"INSERT INTO invoice_items ({names}) "
                        f"SELECT {names} FROM invoice_items_old"
                    )
                )
                connection.execute(text("DROP TABLE invoice_items_old"))

    # Invoice lines used to identify their teacher by the frozen name alone,
    # which reporting then matched against today's names -- so renaming a
    # teacher dropped every month they had already been paid for. The id is
    # added beside the name and filled in from the class each line came
    # from, falling back to a name match for lines whose class has since
    # been deleted.
    if "invoice_items" in inspect(engine).get_table_names():
        if "teacher_id" not in {
            column["name"] for column in inspect(engine).get_columns("invoice_items")
        }:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE invoice_items "
                        "ADD COLUMN teacher_id INTEGER REFERENCES teachers(id)"
                    )
                )
                connection.execute(
                    text(
                        "UPDATE invoice_items SET teacher_id = ("
                        "  SELECT cs.teacher_id FROM class_sessions cs"
                        "  WHERE cs.id = invoice_items.session_id"
                        ") WHERE session_id IS NOT NULL"
                    )
                )
                connection.execute(
                    text(
                        "UPDATE invoice_items SET teacher_id = ("
                        "  SELECT t.id FROM teachers t"
                        "  WHERE lower(t.name) = lower(invoice_items.teacher_name)"
                        ") WHERE teacher_id IS NULL AND teacher_name IS NOT NULL"
                    )
                )

    # Payment used to be tracked one class at a time; it now belongs to the
    # invoice the parent actually pays. Existing databases get the columns
    # added in place, no data lost.
    if "invoices" in inspect(engine).get_table_names():
        invoice_columns = {
            column["name"] for column in inspect(engine).get_columns("invoices")
        }
        for column_name, definition in {
            "paid_on": "DATE",
            "paid_amount": "NUMERIC(10, 2)",
            "payment_note": "VARCHAR(500)",
        }.items():
            if column_name not in invoice_columns:
                with engine.begin() as connection:
                    connection.execute(
                        text(f"ALTER TABLE invoices ADD COLUMN {column_name} {definition}")
                    )

    # A cancelled class is never paid for. Attendance written while that was
    # not yet true left charges behind, and the same is true of any row that
    # somehow bypasses sync_invoice_items, so the books are squared here on
    # every start rather than waiting to be asked. One counting query when
    # there is nothing to do, and running it twice changes nothing.
    if count_billed_cancellations():
        backfill_cancellation_credits()

    # Payment moved from per-class ticks onto the invoice. Any invoice whose
    # classes were all ticked under the old scheme was fully paid, so it is
    # recorded as such once rather than reappearing as a debt. A no-op after
    # the first run, and running it again changes nothing.
    backfill_invoice_payments()


def create_teacher(name):
    cleaned_name = name.strip()
    if not cleaned_name:
        return False

    with SessionLocal() as session:
        duplicate = session.scalar(
            select(Teacher).where(
                func.lower(Teacher.name) == cleaned_name.lower()
            )
        )
        if duplicate:
            return False

        session.add(Teacher(name=cleaned_name))
        session.commit()
        return True


def get_all_teachers():
    with SessionLocal() as session:
        teachers = session.scalars(
            select(Teacher).order_by(Teacher.name)
        ).all()
        return [
            {
                "ID": teacher.id,
                "Name": teacher.name,
                "Active": teacher.is_active,
                "Created": teacher.created_at,
            }
            for teacher in teachers
        ]


def update_teacher_status(teacher_id, is_active):
    with SessionLocal() as session:
        teacher = session.get(Teacher, teacher_id)
        if teacher is None:
            return False
        teacher.is_active = is_active
        session.commit()
        return True


def update_teacher_name(teacher_id, new_name):
    cleaned_name = new_name.strip()
    if not cleaned_name:
        return "invalid"

    with SessionLocal() as session:
        teacher = session.get(Teacher, teacher_id)
        if teacher is None:
            return "not_found"

        duplicate = session.scalar(
            select(Teacher).where(
                func.lower(Teacher.name) == cleaned_name.lower(),
                Teacher.id != teacher_id,
            )
        )
        if duplicate:
            return "duplicate"

        teacher.name = cleaned_name
        session.commit()
        return "updated"


def _get_or_create_parent(session, name, phone):
    """Return a matching parent, or create one inside the current transaction."""

    cleaned_name = name.strip()
    cleaned_phone = phone.strip()
    if not cleaned_name or not cleaned_phone:
        return None

    parent = session.scalar(
        select(Parent).where(
            func.lower(Parent.name) == cleaned_name.lower(),
            Parent.phone == cleaned_phone,
        )
    )

    if parent:
        parent.is_active = True
        return parent

    parent = Parent(name=cleaned_name, phone=cleaned_phone)
    session.add(parent)
    session.flush()
    return parent


def create_student(full_name, parent_name, parent_phone):
    cleaned_name = full_name.strip()
    if not cleaned_name:
        return "invalid"

    with SessionLocal() as session:
        duplicate = session.scalar(
            select(Student).where(
                func.lower(Student.full_name) == cleaned_name.lower()
            )
        )
        if duplicate:
            return "duplicate"

        parent = _get_or_create_parent(session, parent_name, parent_phone)
        if parent is None:
            return "parent_unavailable"

        session.add(Student(full_name=cleaned_name, parent_id=parent.id))
        session.commit()
        return "created"


def create_quick_student(full_name):
    """Create a student name from Scheduling; family details can be added later."""

    cleaned_name = full_name.strip()
    if not cleaned_name:
        return "invalid"

    with SessionLocal() as session:
        duplicate = session.scalar(
            select(Student).where(
                func.lower(Student.full_name) == cleaned_name.lower()
            )
        )
        if duplicate:
            return "duplicate"
        session.add(Student(full_name=cleaned_name, parent_id=None))
        session.commit()
        return "created"


def get_student_id_by_name(full_name):
    """A single indexed lookup for one student's id, case-insensitive.

    Used right after creating a student (e.g. during an Excel import) so the
    caller doesn't have to re-fetch and rebuild the whole students table just
    to learn one new id -- that refetch-per-row pattern is what made
    importing slower as the *total* number of students grew, independent of
    how big any one import was.
    """
    with SessionLocal() as session:
        return session.scalar(
            select(Student.id).where(
                func.lower(Student.full_name) == full_name.strip().lower()
            )
        )


def get_all_students():
    with SessionLocal() as session:
        rows = session.execute(
            select(Student, Parent)
            .outerjoin(Parent, Student.parent_id == Parent.id)
            .order_by(Student.full_name)
        ).all()

        return [
            {
                "ID": student.id,
                "Name": student.full_name,
                "Note": student.note or "",
                # Empty, not a "Not assigned" placeholder: this feeds edit
                # boxes and printed invoices as well as lists, and a screen
                # cannot tell a stand-in apart from a parent really called
                # that. Each caller words "nobody on file" its own way.
                "Parent": parent.name if parent else "",
                "Phone": parent.phone if parent else "",
                "Active": student.is_active,
                "Created": student.created_at,
            }
            for student, parent in rows
        ]


def update_student(student_id, new_name, parent_name, parent_phone):
    cleaned_name = new_name.strip()
    if not cleaned_name:
        return "invalid"

    with SessionLocal() as session:
        student = session.get(Student, student_id)
        if student is None:
            return "not_found"

        duplicate = session.scalar(
            select(Student).where(
                func.lower(Student.full_name) == cleaned_name.lower(),
                Student.id != student_id,
            )
        )
        if duplicate:
            return "duplicate"

        # Parent details are optional -- every student an import creates has
        # none at all -- so leaving both boxes empty means "no parent on
        # file", not "refuse to save". Refusing used to swallow the rename
        # of any student without a parent, which is most of them.
        # Half a parent is still an error: a name with no number, or a
        # number with no name, is a slip worth showing rather than storing.
        wants_parent = bool(parent_name.strip()) or bool(parent_phone.strip())
        parent = _get_or_create_parent(session, parent_name, parent_phone)
        if wants_parent and parent is None:
            return "parent_unavailable"

        student.full_name = cleaned_name
        student.parent_id = parent.id if parent else None
        session.commit()
        return "updated"


def get_class_id_by_name(name):
    """A single indexed lookup for one class's id, case-insensitive.

    Same reasoning as ``get_student_id_by_name``: lets a caller learn a
    just-created class's id without re-fetching (and, for classes,
    re-computing -- ``get_all_classes`` runs a per-class enrolment count)
    every class in the academy just to find one new row.
    """
    with SessionLocal() as session:
        return session.scalar(
            select(AcademyClass.id).where(
                func.lower(AcademyClass.name) == name.strip().lower()
            )
        )


def get_all_classes():
    """Every subject, with the teacher who owns it.

    Only what callers actually read. It used to outer-join the weekly
    schedule and count enrolments for columns nothing displayed, which meant
    an extra join, an extra grouped query and a de-duplication pass on a
    function several screens call on every render.
    """

    with SessionLocal() as session:
        return [
            {
                "ID": academy_class.id,
                "Class": academy_class.name,
                "Teacher": teacher.name,
                "Teacher ID": teacher.id,
            }
            for academy_class, teacher in session.execute(
                select(AcademyClass, Teacher)
                .join(Teacher, AcademyClass.teacher_id == Teacher.id)
                .order_by(AcademyClass.name)
            ).all()
        ]


def get_class_student_ids(class_id):
    with SessionLocal() as session:
        return list(
            session.scalars(
                select(Enrolment.student_id).where(
                    Enrolment.class_id == class_id
                )
            ).all()
        )


def describe_class_deletion(class_ids):
    """What deleting these subjects would take with it.

    Shown before anything happens, because the answer changes the decision:
    wiping an import you made five minutes ago is nothing, and wiping a
    subject a parent has already been invoiced for is not.
    """
    class_ids = list(class_ids)
    if not class_ids:
        return []
    with SessionLocal() as session:
        rows = []
        for academy_class in session.scalars(
            select(AcademyClass).where(AcademyClass.id.in_(class_ids))
        ).all():
            lessons = session.scalars(
                select(ClassSession).where(ClassSession.class_id == academy_class.id)
            ).all()
            lesson_ids = [lesson.id for lesson in lessons]
            students = billed = unbilled = 0
            if lesson_ids:
                students = session.scalar(
                    select(func.count(distinct(SessionAttendance.student_id))).where(
                        SessionAttendance.session_id.in_(lesson_ids)
                    )
                ) or 0
                for status, count in session.execute(
                    select(Invoice.status, func.count())
                    .select_from(InvoiceItem)
                    .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
                    .where(InvoiceItem.session_id.in_(lesson_ids))
                    .group_by(Invoice.status)
                ).all():
                    if status == "Issued":
                        billed += count
                    else:
                        unbilled += count
            teacher = session.get(Teacher, academy_class.teacher_id)
            rows.append(
                {
                    "Class ID": academy_class.id,
                    "Class": academy_class.name,
                    "Teacher": teacher.name if teacher else "",
                    "Lessons": len(lessons),
                    "Students": students,
                    "On issued invoices": billed,
                    "On open invoices": unbilled,
                    "First": min((l.session_date for l in lessons), default=None),
                    "Last": max((l.session_date for l in lessons), default=None),
                }
            )
        rows.sort(key=lambda item: item["Class"])
        return rows


def delete_class(class_id):
    """Delete a subject and everything hanging off it.

    Billing is handled exactly as it is when a rescheduled class disappears
    from a re-uploaded workbook: a charge on an invoice still open is simply
    dropped, and one on an invoice already sent is credited back rather than
    rewriting a bill a parent has in their hand. Credits that pointed at a
    deleted class keep their money and lose only the link.

    Returns a summary rather than a bare string so the screen can say what
    actually happened.
    """
    with SessionLocal() as session:
        academy_class = session.get(AcademyClass, class_id)
        if academy_class is None:
            return {"status": "not_found"}

        lessons = session.scalars(
            select(ClassSession).where(ClassSession.class_id == class_id)
        ).all()
        lesson_ids = [lesson.id for lesson in lessons]
        credited = dropped = 0

        if lesson_ids:
            rate_index = _rate_index(session, {class_id})
            for lesson in lessons:
                for item in session.scalars(
                    select(InvoiceItem).where(InvoiceItem.session_id == lesson.id)
                ).all():
                    invoice = session.get(Invoice, item.invoice_id)
                    if invoice is None:
                        session.delete(item)
                        continue
                    if invoice.status == "Issued":
                        credit, raised = _raise_credit(
                            session, invoice.student_id, lesson,
                            "Subject removed", rate_index,
                        )
                        credit.session_id = None      # the class is about to go
                        item.session_id = None        # ... and so is its line's link
                        credited += int(raised)
                    else:
                        session.delete(item)
                        dropped += 1
                for attendance in session.scalars(
                    select(SessionAttendance).where(
                        SessionAttendance.session_id == lesson.id
                    )
                ).all():
                    session.delete(attendance)
                for credit in session.scalars(
                    select(Credit).where(Credit.session_id == lesson.id)
                ).all():
                    credit.session_id = None
                session.delete(lesson)

        for enrolment in session.scalars(
            select(Enrolment).where(Enrolment.class_id == class_id)
        ).all():
            session.delete(enrolment)
        for schedule in session.scalars(
            select(ClassSchedule).where(ClassSchedule.class_id == class_id)
        ).all():
            session.delete(schedule)
        for class_rate in session.scalars(
            select(ClassRate).where(ClassRate.class_id == class_id)
        ).all():
            session.delete(class_rate)

        name = academy_class.name
        session.delete(academy_class)
        session.commit()
        return {
            "status": "deleted",
            "class": name,
            "lessons": len(lesson_ids),
            "credits_raised": credited,
            "charges_dropped": dropped,
        }


def get_all_class_rates():
    with SessionLocal() as session:
        rows = session.execute(
            select(ClassRate, AcademyClass)
            .join(AcademyClass, ClassRate.class_id == AcademyClass.id)
            .order_by(AcademyClass.name, ClassRate.effective_from.desc())
        ).all()

        return [
            {
                "ID": rate.id,
                "Class ID": academy_class.id,
                "Class": academy_class.name,
                "Hourly Rate": float(rate.hourly_rate),
                "Effective From": rate.effective_from,
                "Effective To": rate.effective_to,
            }
            for rate, academy_class in rows
        ]


def set_class_rate_for_month(class_id, month_start, hourly_rate):
    """Price a subject from one whole calendar month onward, in one transaction.

    A naive "find whatever period covers this month and overwrite its price"
    corrupts history: a class is usually on one open-ended period from
    whenever it started, so overwriting it in place silently reprices every
    past month too. This instead:

    * caps any period starting earlier, the day before this month, so its old
      price keeps every earlier month exactly as billed;
    * absorbs any period starting *inside* this month -- the placeholder an
      import seeds is dated to the class's first real class, which can land
      on any day -- so the new price covers the whole month;
    * and stops the new period before any later price change, so the two
      never overlap.

    All of it in one transaction. The screen used to orchestrate this as a
    sequence of separate calls, which deleted the periods it was replacing
    before creating their replacement: anything that made the create fail
    left the subject with no price at all, reported only as "could not be
    updated". A subject that cannot be priced silently bills at nothing.
    """
    if hourly_rate is None or float(hourly_rate) <= 0:
        return "invalid"
    with SessionLocal() as session:
        if session.get(AcademyClass, class_id) is None:
            return "class_unavailable"
        outcome = _apply_month_rate(session, class_id, month_start, hourly_rate)
        session.commit()
        return outcome


def _apply_month_rate(session, class_id, month_start, hourly_rate):
    """The month-rate rule itself, inside a caller's transaction.

    Shared so the Teachers tab and the Timetable's subject editor price a
    subject the same way -- by the calendar month. The editor used to date a
    new period to the class being edited, which both split a month in half
    and, when that date fell before the existing period, left two open-ended
    periods overlapping: the later one then shadowed the price just typed.

    The caller commits.
    """
    month_start = date(month_start.year, month_start.month, 1)
    month_end = date(
        month_start.year,
        month_start.month,
        monthrange(month_start.year, month_start.month)[1],
    )
    periods = sorted(
        session.scalars(
            select(ClassRate).where(ClassRate.class_id == class_id)
        ).all(),
        key=lambda rate: rate.effective_from,
    )

    exact = next(
        (rate for rate in periods if rate.effective_from == month_start), None
    )
    if exact is not None:
        exact.hourly_rate = hourly_rate
        # Anything else starting inside the month is a leftover that would
        # shadow this one from part-way through it.
        for stray in periods:
            if stray is not exact and month_start < stray.effective_from <= month_end:
                session.delete(stray)
        return "updated"

    later = next(
        (rate for rate in periods if rate.effective_from > month_end), None
    )
    for rate in periods:
        if rate.effective_from < month_start:
            # Covers this month or runs past it: stop it the day before, so
            # every earlier month keeps the price it was billed at.
            if rate.effective_to is None or rate.effective_to >= month_start:
                rate.effective_to = month_start - timedelta(days=1)
        elif rate.effective_from <= month_end:
            session.delete(rate)

    session.add(
        ClassRate(
            class_id=class_id,
            hourly_rate=hourly_rate,
            effective_from=month_start,
            effective_to=(
                later.effective_from - timedelta(days=1) if later else None
            ),
        )
    )
    return "created"


def get_class_rates_for_date(class_ids, on_date):
    """The rate effective on one date for many classes, keyed by class id.

    One query for the whole list -- for screens that show every subject with
    its current price. A class with no rate covering that date is simply
    absent from the result.
    """
    class_ids = list(class_ids)
    if not class_ids:
        return {}
    with SessionLocal() as session:
        index = _rate_index(session, class_ids)
        found = {}
        for class_id in class_ids:
            rate = _rate_lookup(index, class_id, on_date)
            # _rate_lookup reports "no rate on file" as 0.0; this API keeps
            # that distinct from a real zero by leaving the key out.
            if rate:
                found[class_id] = rate
        return found


# ---------------------------------------------------------------------------
# Scheduling workspace
# ---------------------------------------------------------------------------


def _valid_colour(value):
    return bool(re.fullmatch(r"#[0-9A-Fa-f]{6}", (value or "").strip()))


def _attendance_status(row):
    statuses = []
    if row.get("is_online"):
        statuses.append("Online")
    if row.get("has_recording"):
        statuses.append("Recording")
    if row.get("is_cancelled"):
        statuses.append("Cancelled")
    return " + ".join(statuses) if statuses else "Attending"


def get_teacher_classes_for_schedule(teacher_id, on_date):
    """Return a teacher's reusable classes, current price, and roster."""

    with SessionLocal() as session:
        classes = session.scalars(
            select(AcademyClass)
            .where(
                AcademyClass.teacher_id == teacher_id,
                AcademyClass.status == "Ongoing",
            )
            .order_by(AcademyClass.name)
        ).all()
        results = []
        for academy_class in classes:
            rate = session.scalar(
                select(ClassRate)
                .where(
                    ClassRate.class_id == academy_class.id,
                    ClassRate.effective_from <= on_date,
                    (
                        ClassRate.effective_to.is_(None)
                        | (ClassRate.effective_to >= on_date)
                    ),
                )
                .order_by(ClassRate.effective_from.desc())
            )
            students = session.execute(
                select(Student)
                .join(Enrolment, Enrolment.student_id == Student.id)
                .where(
                    Enrolment.class_id == academy_class.id,
                    Student.is_active.is_(True),
                )
                .order_by(Student.full_name)
            ).scalars().all()
            results.append(
                {
                    "ID": academy_class.id,
                    "Class": academy_class.name,
                    "Teacher ID": academy_class.teacher_id,
                    "Colour": academy_class.display_color or "#4F81BD",
                    "Note": academy_class.note or "",
                    "Hourly Rate": float(rate.hourly_rate) if rate else None,
                    "Student IDs": [student.id for student in students],
                    "Students": [student.full_name for student in students],
                }
            )
        return results


def update_scheduling_class(
    class_id,
    name,
    hourly_rate,
    student_ids,
    display_color,
    effective_from,
):
    """Update a reusable class and start a new price period when required."""

    cleaned_name = name.strip()
    if (
        not cleaned_name
        or hourly_rate <= 0
        or not _valid_colour(display_color)
    ):
        return "invalid"

    with SessionLocal() as session:
        academy_class = session.get(AcademyClass, class_id)
        if academy_class is None:
            return "not_found"
        duplicate = session.scalar(
            select(AcademyClass).where(
                func.lower(AcademyClass.name) == cleaned_name.lower(),
                AcademyClass.id != class_id,
            )
        )
        if duplicate:
            return "duplicate"

        academy_class.name = cleaned_name
        academy_class.display_color = display_color.upper()

        old_enrolments = session.scalars(
            select(Enrolment).where(Enrolment.class_id == class_id)
        ).all()
        for enrolment in old_enrolments:
            session.delete(enrolment)
        valid_students = session.scalars(
            select(Student).where(
                Student.id.in_(student_ids),
                Student.is_active.is_(True),
            )
        ).all()
        session.add_all(
            Enrolment(class_id=class_id, student_id=student.id)
            for student in valid_students
        )

        # Priced by the calendar month, the same as the Teachers tab: a
        # subject costs what it costs for a month, and dating a change to the
        # class being edited split the month and could leave two overlapping
        # periods, the later of which then shadowed the price just typed.
        current = _rate_lookup(
            _rate_index(session, [class_id]), class_id, effective_from
        )
        if float(current or 0) != float(hourly_rate):
            _apply_month_rate(session, class_id, effective_from, hourly_rate)

        session.commit()
        return "updated"


def _teacher_has_conflict(
    session,
    teacher_id,
    session_date,
    start_time,
    end_time,
    excluded_session_id=None,
):
    query = select(ClassSession).where(
        ClassSession.teacher_id == teacher_id,
        ClassSession.session_date == session_date,
        ClassSession.start_time < end_time,
        ClassSession.end_time > start_time,
    )
    if excluded_session_id is not None:
        query = query.where(ClassSession.id != excluded_session_id)
    return session.scalar(query.limit(1)) is not None


def _replace_session_attendance(session, session_id, attendance_rows):
    for old_row in session.scalars(
        select(SessionAttendance).where(
            SessionAttendance.session_id == session_id
        )
    ).all():
        session.delete(old_row)

    student_ids = [row["student_id"] for row in attendance_rows]
    valid_ids = set(
        session.scalars(
            select(Student.id).where(
                Student.id.in_(student_ids),
                Student.is_active.is_(True),
            )
        ).all()
    )
    session.add_all(
        SessionAttendance(
            session_id=session_id,
            student_id=row["student_id"],
            status=_attendance_status(row),
            is_online=bool(row.get("is_online")),
            has_recording=bool(row.get("has_recording")),
            is_cancelled=bool(row.get("is_cancelled")),
            is_paid=bool(row.get("is_paid")),
            on_roster=1,
            note=(row.get("note") or "").strip() or None,
        )
        for row in attendance_rows
        if row["student_id"] in valid_ids
    )


def create_class_and_first_session(
    name,
    teacher_id,
    hourly_rate,
    display_color,
    student_ids,
    session_date,
    start_time,
    end_time,
    status,
    note,
    attendance_rows,
):
    """Create a reusable class and its first timetable slot in one transaction."""

    cleaned_name = name.strip()
    if (
        not cleaned_name
        or hourly_rate <= 0
        or not _valid_colour(display_color)
        or start_time >= end_time
        or status not in {"Scheduled", "Completed", "Cancelled"}
    ):
        return "invalid"

    with SessionLocal() as session:
        duplicate = session.scalar(
            select(AcademyClass).where(
                func.lower(AcademyClass.name) == cleaned_name.lower()
            )
        )
        if duplicate:
            return "duplicate"
        teacher = session.get(Teacher, teacher_id)
        if teacher is None or not teacher.is_active:
            return "teacher_unavailable"
        if _teacher_has_conflict(
            session,
            teacher_id,
            session_date,
            start_time,
            end_time,
        ):
            return "teacher_conflict"

        academy_class = AcademyClass(
            name=cleaned_name,
            teacher_id=teacher_id,
            display_color=display_color.upper(),
        )
        session.add(academy_class)
        session.flush()

        valid_students = session.scalars(
            select(Student).where(
                Student.id.in_(student_ids),
                Student.is_active.is_(True),
            )
        ).all()
        session.add_all(
            Enrolment(class_id=academy_class.id, student_id=student.id)
            for student in valid_students
        )
        session.add(
            ClassRate(
                class_id=academy_class.id,
                hourly_rate=hourly_rate,
                effective_from=session_date,
            )
        )

        class_session = ClassSession(
            class_id=academy_class.id,
            teacher_id=teacher_id,
            session_date=session_date,
            start_time=start_time,
            end_time=end_time,
            status=status,
            note=(note or "").strip() or None,
        )
        session.add(class_session)
        session.flush()
        _replace_session_attendance(
            session,
            class_session.id,
            attendance_rows,
        )
        session.commit()
        new_id = class_session.id
    # Outside the transaction: every student in the class goes onto their
    # open invoice.
    sync_invoice_items(new_id)
    return "created"


def create_timetable_session(
    teacher_id,
    class_id,
    session_date,
    start_time,
    end_time,
    status,
    note,
    attendance_rows,
):
    """Create a class and its complete per-student conditions atomically."""

    if start_time >= end_time or status not in {
        "Scheduled",
        "Completed",
        "Cancelled",
    }:
        return "invalid"

    with SessionLocal() as session:
        academy_class = session.get(AcademyClass, class_id)
        if academy_class is None or academy_class.teacher_id != teacher_id:
            return "class_unavailable"
        if _teacher_has_conflict(
            session,
            teacher_id,
            session_date,
            start_time,
            end_time,
        ):
            return "teacher_conflict"

        class_session = ClassSession(
            class_id=class_id,
            teacher_id=teacher_id,
            session_date=session_date,
            start_time=start_time,
            end_time=end_time,
            status=status,
            note=(note or "").strip() or None,
        )
        session.add(class_session)
        session.flush()
        _replace_session_attendance(
            session,
            class_session.id,
            attendance_rows,
        )
        session.commit()
        new_id = class_session.id
    sync_invoice_items(new_id)
    return "created"


def get_month_timetable(teacher_id, year, month):
    """Return timetable cards for one teacher and calendar month."""

    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])
    with SessionLocal() as session:
        rows = session.execute(
            select(ClassSession, AcademyClass)
            .join(AcademyClass, ClassSession.class_id == AcademyClass.id)
            .where(
                ClassSession.teacher_id == teacher_id,
                ClassSession.session_date >= first_day,
                ClassSession.session_date <= last_day,
            )
            .order_by(ClassSession.session_date, ClassSession.start_time)
        ).all()

        # Every class's attendance in one query, keyed by class, rather
        # than a query per class -- this drives the month grid, so its cost
        # would otherwise grow with how busy the month is.
        attendance_by_session: dict[int, list[SessionAttendance]] = defaultdict(list)
        session_ids = [class_session.id for class_session, _ in rows]
        if session_ids:
            for row in session.scalars(
                select(SessionAttendance).where(
                    SessionAttendance.session_id.in_(session_ids)
                )
            ).all():
                attendance_by_session[row.session_id].append(row)

        results = []
        for class_session, academy_class in rows:
            attendance = attendance_by_session.get(class_session.id, [])
            results.append(
                {
                    "ID": class_session.id,
                    "Date": class_session.session_date,
                    "Start": class_session.start_time,
                    "End": class_session.end_time,
                    "Class ID": academy_class.id,
                    "Class": academy_class.name,
                    "Colour": academy_class.display_color or "#4F81BD",
                    "Status": class_session.status,
                    "Note": class_session.note or "",
                    "Students": len(attendance),
                    "Online": sum(bool(row.is_online) for row in attendance),
                    "Recording": sum(
                        bool(row.has_recording) for row in attendance
                    ),
                    "Cancelled students": sum(
                        bool(row.is_cancelled) for row in attendance
                    ),
                    "Unpaid": sum(
                        1
                        for row in attendance
                        if not row.is_paid and not row.is_cancelled
                    ),
                    "Subject note": academy_class.note or "",
                }
            )
        return results


def get_month_attendance(teacher_id, year, month):
    """Every class's roster for one teacher-month, keyed by class id.

    The month grid names each student on the card along with their
    condition, which otherwise means calling ``get_timetable_session`` once
    per class -- three queries apiece, on every render of the Timetable
    tab. This is the same information in one query.
    """
    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])
    with SessionLocal() as session:
        rows = session.execute(
            select(SessionAttendance, Student, ClassSession.id)
            .join(ClassSession, SessionAttendance.session_id == ClassSession.id)
            .join(Student, SessionAttendance.student_id == Student.id)
            .where(
                ClassSession.teacher_id == teacher_id,
                ClassSession.session_date >= first_day,
                ClassSession.session_date <= last_day,
            )
            .order_by(Student.full_name)
        ).all()

        by_session: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for attendance, student, session_id in rows:
            by_session[session_id].append(
                {
                    "student_id": student.id,
                    "student_name": student.full_name,
                    "is_online": bool(attendance.is_online),
                    "has_recording": bool(attendance.has_recording),
                    "is_cancelled": bool(attendance.is_cancelled),
                    "is_paid": bool(attendance.is_paid),
                    "note": attendance.note or "",
                }
            )
        return dict(by_session)


def get_timetable_session(session_id):
    """Return one class with its editable roster and conditions."""

    with SessionLocal() as session:
        row = session.execute(
            select(ClassSession, AcademyClass)
            .join(AcademyClass, ClassSession.class_id == AcademyClass.id)
            .where(ClassSession.id == session_id)
        ).first()
        if row is None:
            return None
        class_session, academy_class = row
        attendance_rows = session.execute(
            select(SessionAttendance, Student)
            .join(Student, SessionAttendance.student_id == Student.id)
            .where(SessionAttendance.session_id == session_id)
            .order_by(Student.full_name)
        ).all()
        return {
            "ID": class_session.id,
            "Teacher ID": class_session.teacher_id,
            "Class ID": class_session.class_id,
            "Class": academy_class.name,
            "Date": class_session.session_date,
            "Start": class_session.start_time,
            "End": class_session.end_time,
            "Status": class_session.status,
            "Note": class_session.note or "",
            "Attendance": [
                {
                    "student_id": student.id,
                    "student_name": student.full_name,
                    "is_online": bool(attendance.is_online),
                    "has_recording": bool(attendance.has_recording),
                    "is_cancelled": bool(attendance.is_cancelled),
                    "is_paid": bool(attendance.is_paid),
                    "note": attendance.note or "",
                }
                for attendance, student in attendance_rows
            ],
        }


def update_timetable_session(
    session_id,
    session_date,
    start_time,
    end_time,
    status,
    note,
    attendance_rows,
):
    if start_time >= end_time or status not in {
        "Scheduled",
        "Completed",
        "Cancelled",
    }:
        return "invalid"

    with SessionLocal() as session:
        class_session = session.get(ClassSession, session_id)
        if class_session is None:
            return "not_found"
        if _teacher_has_conflict(
            session,
            class_session.teacher_id,
            session_date,
            start_time,
            end_time,
            excluded_session_id=session_id,
        ):
            return "teacher_conflict"

        class_session.session_date = session_date
        class_session.start_time = start_time
        class_session.end_time = end_time
        class_session.status = status
        class_session.note = (note or "").strip() or None
        _replace_session_attendance(session, session_id, attendance_rows)
        session.commit()
    sync_invoice_items(session_id)
    return "updated"


def delete_timetable_session(session_id):
    with SessionLocal() as session:
        class_session = session.get(ClassSession, session_id)
        if class_session is None:
            return False
        for attendance in session.scalars(
            select(SessionAttendance).where(
                SessionAttendance.session_id == session_id
            )
        ).all():
            session.delete(attendance)
        session.delete(class_session)
        session.commit()
    remove_invoice_items(session_id)
    return True


def set_student_note(student_id, note):
    """Store a free-text note against a student."""

    with SessionLocal() as session:
        student = session.get(Student, student_id)
        if student is None:
            return "missing"
        student.note = (note or "").strip()[:1000] or None
        session.commit()
        return "updated"


def set_subject_note(class_id, note):
    """Store a free-text note against a subject."""

    with SessionLocal() as session:
        academy_class = session.get(AcademyClass, class_id)
        if academy_class is None:
            return "missing"
        academy_class.note = (note or "").strip()[:1000] or None
        session.commit()
        return "updated"


def get_student_usage(student_id):
    """How much a student is tied into: subjects, classes, invoices sent."""

    with SessionLocal() as session:
        subjects = session.scalar(
            select(func.count())
            .select_from(Enrolment)
            .where(Enrolment.student_id == student_id)
        )
        classes = session.scalar(
            select(func.count())
            .select_from(SessionAttendance)
            .where(SessionAttendance.student_id == student_id)
        )
        invoices = session.scalar(
            select(func.count())
            .select_from(Invoice)
            .where(Invoice.student_id == student_id, Invoice.status == "Issued")
        )
        return {
            "subjects": int(subjects or 0),
            "classes": int(classes or 0),
            "invoices": int(invoices or 0),
        }


def remove_student(student_id):
    """Delete a student, their enrolments, attendance and any draft billing.

    Refused once they have been sent an invoice. Every screen that lists
    invoices reaches them through their student, so deleting the person an
    issued invoice was addressed to does not remove the invoice -- it hides
    it: the money drops off the Invoices list, off Payments and off the
    overdue reminders, while still counting as earned on the Data tab. An
    invoice that has gone out keeps its student, the same way it keeps its
    figures.

    Without any invoice sent there is nothing to preserve, so the draft
    invoice and any unsettled credit go too and nothing is left pointing at
    a student who no longer exists -- which is what makes this the safe way
    to undo a mistyped name.
    """

    with SessionLocal() as session:
        student = session.get(Student, student_id)
        if student is None:
            return "not_found"

        issued = session.scalar(
            select(func.count())
            .select_from(Invoice)
            .where(Invoice.student_id == student_id, Invoice.status == "Issued")
        ) or 0
        if issued:
            return "has_invoices"

        for invoice in session.scalars(
            select(Invoice).where(Invoice.student_id == student_id)
        ).all():
            for item in session.scalars(
                select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)
            ).all():
                session.delete(item)
            session.delete(invoice)

        for credit in session.scalars(
            select(Credit).where(Credit.student_id == student_id)
        ).all():
            session.delete(credit)

        for row in session.scalars(
            select(SessionAttendance).where(
                SessionAttendance.student_id == student_id
            )
        ).all():
            session.delete(row)

        for row in session.scalars(
            select(Enrolment).where(Enrolment.student_id == student_id)
        ).all():
            session.delete(row)

        session.delete(student)
        session.commit()
        return "deleted"


# ---------------------------------------------------------------------------
# Invoicing
# ---------------------------------------------------------------------------

# The academy's existing invoices run to #7322, so new ones continue from there.
INVOICE_NUMBER_START = 7323


def _as_day(value):
    """A date, whatever SQLite handed back.

    ``min()``/``max()`` over a Date column come back as text rather than a
    date object, so anything comparing an aggregate against a real date has
    to go through here first.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _span_hours(start_time, end_time):
    minutes = (
        end_time.hour * 60 + end_time.minute
        - start_time.hour * 60 - start_time.minute
    )
    return round(max(minutes, 0) / 60, 2)


def _class_hours(session):
    return _span_hours(session.start_time, session.end_time)


def _rate_index(session, class_ids) -> dict[int, list[ClassRate]]:
    """Preload every rate period for a set of classes, newest first.

    Used wherever a rate would otherwise be looked up per class inside a
    loop -- one query up front instead of one per class.
    """
    class_ids = set(class_ids)
    if not class_ids:
        return {}
    index: dict[int, list[ClassRate]] = defaultdict(list)
    for rate in session.scalars(
        select(ClassRate).where(ClassRate.class_id.in_(class_ids))
    ).all():
        index[rate.class_id].append(rate)
    for rates in index.values():
        rates.sort(key=lambda item: item.effective_from, reverse=True)
    return index


def _rate_lookup(index: dict[int, list[ClassRate]], class_id: int, when: date) -> float:
    """The rate effective for a class on a date, from a preloaded ``_rate_index``."""
    for rate in index.get(class_id, []):
        if rate.effective_from <= when and (
            rate.effective_to is None or rate.effective_to >= when
        ):
            return float(rate.hourly_rate)
    return 0.0


def _apply_credits(session, invoice, items_total, issued_on):
    """Settle a student's outstanding credits against an invoice being issued.

    Oldest first, and never past zero: an invoice asking for a negative
    amount would be nonsense to send. A credit larger than what is left to
    bill is split, so the used part settles here and the remainder stays
    open for the month after -- which is what "carry the balance forward"
    has to mean when one cancelled month outweighs the next.
    """
    remaining = round(float(items_total), 2)
    credits = session.scalars(
        select(Credit)
        .where(Credit.student_id == invoice.student_id, Credit.status == "Open")
        .order_by(Credit.session_date, Credit.id)
    ).all()

    applied = 0.0
    for credit in credits:
        if remaining <= 0:
            break
        amount = round(float(credit.amount), 2)
        if amount <= remaining:
            credit.status = "Applied"
            credit.invoice_id = invoice.id
            credit.settled_on = issued_on
            remaining = round(remaining - amount, 2)
            applied = round(applied + amount, 2)
        else:
            # Only part of it fits; the rest waits for the next invoice.
            session.add(
                Credit(
                    student_id=credit.student_id,
                    session_id=None,
                    amount=round(amount - remaining, 2),
                    class_name=credit.class_name,
                    session_date=credit.session_date,
                    reason=credit.reason,
                    status="Open",
                    created_on=credit.created_on,
                    note="Balance carried forward",
                )
            )
            credit.amount = remaining
            credit.status = "Applied"
            credit.invoice_id = invoice.id
            credit.settled_on = issued_on
            applied = round(applied + remaining, 2)
            remaining = 0.0
    return applied


def _group_credits(entries):
    """Turn credit rows into invoice lines: one per subject, dates listed,
    amount negative -- so a parent sees which classes came off and by how
    much, in the same shape as the billing lines above them.

    Takes ``(credit, amount)`` pairs rather than bare credits, because a
    draft can only show as much of a credit as its charges absorb.
    """
    grouped = {}
    for credit, amount in entries:
        entry = grouped.setdefault(
            credit.class_name or "Cancelled class", {"dates": [], "amount": 0.0}
        )
        if credit.session_date:
            entry["dates"].append(credit.session_date)
        entry["amount"] += float(amount or 0)

    lines = []
    for name, entry in grouped.items():
        dates = sorted(entry["dates"])
        lines.append(
            {
                "Subject": f"Cancelled — {name}",
                "Teacher": "",
                "Dates": dates,
                "Quantity": len(dates),
                "Hours": 0,
                "Rate": 0,
                "Amount": -round(entry["amount"], 2),
                "Credit": True,
            }
        )
    lines.sort(key=lambda line: (line["Dates"][0] if line["Dates"] else date.max))
    return lines


def _credit_lines(session, invoice):
    """The credits an issued invoice actually settled."""
    return _group_credits(
        (credit, float(credit.amount or 0))
        for credit in session.scalars(
            select(Credit).where(Credit.invoice_id == invoice.id)
        ).all()
    )


def _open_credit_lines(session, student_id, charges):
    """Credits still waiting, trimmed to what this draft can actually absorb.

    Issuing never asks for a negative amount: ``_apply_credits`` settles
    oldest first, stops at zero and carries any remainder to the next
    invoice.  A draft has to show the same arithmetic, or the total on
    screen -- and on the printable copy, which can be handed to a parent --
    is not the figure that will really be asked for.  Same ordering as
    ``_apply_credits``, so the two pick the same credits.
    """
    remaining = round(float(charges), 2)
    if remaining <= 0:
        return []

    entries = []
    for credit in session.scalars(
        select(Credit)
        .where(Credit.student_id == student_id, Credit.status == "Open")
        .order_by(Credit.session_date, Credit.id)
    ).all():
        if remaining <= 0:
            break
        amount = min(round(float(credit.amount or 0), 2), remaining)
        entries.append((credit, amount))
        remaining = round(remaining - amount, 2)
    return _group_credits(entries)


def _lesson_charge(session, lesson, rate_index=None):
    """What one student pays for one class: hours x the rate in force."""
    index = rate_index if rate_index is not None else _rate_index(session, [lesson.class_id])
    hours = _class_hours(lesson)
    return round(hours * _rate_lookup(index, lesson.class_id, lesson.session_date), 2)


def _billed_amount(session, student_id, lesson_id):
    """What a student was actually charged for a class, or ``None``.

    Issuing an invoice copies the hours, the rate and the amount onto the
    line itself so the figures stop moving.  Crediting has to give back that
    frozen amount and not today's price: re-pricing a month after its
    invoices went out would otherwise refund a number the student was never
    asked for.
    """
    return session.scalar(
        select(InvoiceItem.amount)
        .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
        .where(
            InvoiceItem.session_id == lesson_id,
            Invoice.student_id == student_id,
            Invoice.status == "Issued",
        )
    )


def _raise_credit(session, student_id, lesson, reason, rate_index=None):
    """Owe a student back for a class they were already billed for.

    Idempotent: re-importing a workbook re-writes attendance again and
    again, and every pass would otherwise raise the same credit afresh.
    Returns ``(credit, created)`` -- ``created`` is False when the credit
    was already there, so callers reporting "N credits raised" can count
    what actually happened rather than how many times they asked.
    """
    existing = session.scalar(
        select(Credit).where(
            Credit.student_id == student_id, Credit.session_id == lesson.id
        )
    )
    if existing is not None:
        return existing, False

    billed = _billed_amount(session, student_id, lesson.id)
    academy_class = session.get(AcademyClass, lesson.class_id)
    credit = Credit(
        student_id=student_id,
        session_id=lesson.id,
        amount=(
            round(float(billed), 2)
            if billed is not None
            else _lesson_charge(session, lesson, rate_index)
        ),
        class_name=academy_class.name if academy_class else "",
        session_date=lesson.session_date,
        reason=reason,
        status="Open",
        created_on=date.today(),
    )
    session.add(credit)
    return credit, True


def _drop_credit(session, student_id, lesson_id):
    """Undo a credit that has not been settled yet.

    A cancellation put back to attending -- a corrected spreadsheet, a
    student who turned up after all -- should take its credit with it, so
    long as no invoice has consumed it yet. One already applied or refunded
    is history and stays put.
    """
    credit = session.scalar(
        select(Credit).where(
            Credit.student_id == student_id,
            Credit.session_id == lesson_id,
            Credit.status == "Open",
        )
    )
    if credit is not None:
        session.delete(credit)


def _open_invoice_for(session, student_id):
    """The student's open invoice, created if they do not have one."""
    invoice = session.scalar(
        select(Invoice).where(
            Invoice.student_id == student_id, Invoice.status == "Open"
        )
    )
    if invoice is None:
        invoice = Invoice(student_id=student_id, status="Open")
        session.add(invoice)
        session.flush()
    return invoice


def sync_invoice_items(session_id):
    """Put every student who owes for a class onto their open invoice.

    Called whenever a class or its roster changes. Three rules:

    * A student who cancelled owes nothing for that class. If their invoice
      is still open the charge is simply left off it.
    * If their invoice has already been issued the figures are frozen, so
      the correction becomes a credit against their next one instead.
    * A student taken off the class entirely is dropped from any invoice
      still open; issued invoices are never touched.
    """

    with SessionLocal() as session:
        lesson = session.get(ClassSession, session_id)
        if lesson is None:
            return "missing"

        rows = session.scalars(
            select(SessionAttendance).where(
                SessionAttendance.session_id == session_id
            )
        ).all()
        billable = {row.student_id for row in rows if not row.is_cancelled}
        cancelled = {row.student_id for row in rows if row.is_cancelled}

        existing = session.scalars(
            select(InvoiceItem).where(InvoiceItem.session_id == session_id)
        ).all()

        billed = set()
        for item in existing:
            invoice = session.get(Invoice, item.invoice_id)
            if invoice is None:
                continue
            if invoice.status == "Issued":
                billed.add(invoice.student_id)
                if invoice.student_id in cancelled:
                    _raise_credit(
                        session, invoice.student_id, lesson, "Cancelled class"
                    )
                else:
                    _drop_credit(session, invoice.student_id, lesson.id)
                continue
            if invoice.student_id in billable:
                billed.add(invoice.student_id)
            else:
                session.delete(item)

        # Nothing was ever billed for these, so there is nothing to credit
        # back -- the charge just never appears.
        for student_id in cancelled - billed:
            _drop_credit(session, student_id, lesson.id)

        for student_id in billable - billed:
            invoice = _open_invoice_for(session, student_id)
            session.add(
                InvoiceItem(invoice_id=invoice.id, session_id=session_id)
            )

        session.commit()
        return "synced"


def remove_invoice_items(session_id):
    """Drop a deleted class from any invoice that has not been issued."""

    with SessionLocal() as session:
        for item in session.scalars(
            select(InvoiceItem).where(InvoiceItem.session_id == session_id)
        ).all():
            invoice = session.get(Invoice, item.invoice_id)
            if invoice is not None and invoice.status == "Open":
                session.delete(item)
        session.commit()
        return "removed"


def _group_invoice_items(invoice, items, lessons, classes, teachers, rate_index):
    """Group one invoice's items into billing lines, one per subject and rate.

    Pure grouping, no queries -- callers preload ``lessons``/``classes``/
    ``teachers``/``rate_index`` (dicts keyed by id, ``rate_index`` from
    ``_rate_index``) at whatever scope suits them: one invoice at a time, or
    every invoice on the page in a handful of queries total. An Issued
    invoice needs none of that -- its items already carry their own frozen
    figures -- so an empty dict is fine there.
    """
    grouped: dict[tuple, dict] = {}
    if invoice.status == "Issued":
        for item in items:
            if not item.session_date:
                continue
            key = (
                item.class_name, item.teacher_name,
                float(item.hours or 0), float(item.hourly_rate or 0),
            )
            entry = grouped.setdefault(key, {"dates": [], "amount": 0.0})
            entry["dates"].append(item.session_date)
            entry["amount"] += float(item.amount or 0)
    else:
        for item in items:
            lesson = lessons.get(item.session_id)
            if lesson is None:
                continue
            academy_class = classes.get(lesson.class_id)
            teacher = teachers.get(lesson.teacher_id)
            hourly = _rate_lookup(rate_index, lesson.class_id, lesson.session_date)
            hours = _class_hours(lesson)
            key = (
                academy_class.name if academy_class else "",
                teacher.name if teacher else "",
                hours,
                hourly,
            )
            entry = grouped.setdefault(key, {"dates": [], "amount": 0.0})
            entry["dates"].append(lesson.session_date)
            entry["amount"] += round(hours * hourly, 2)

    lines = []
    for (name, teacher, hours, rate), entry in grouped.items():
        dates = sorted(d for d in entry["dates"] if d)
        lines.append(
            {
                "Subject": name,
                "Teacher": teacher,
                "Dates": dates,
                "Quantity": len(dates),
                "Hours": hours,
                "Rate": rate,
                "Amount": round(entry["amount"], 2),
            }
        )
    lines.sort(key=lambda line: (line["Dates"][0] if line["Dates"] else date.max))
    return lines


def _invoice_lines(session, invoice):
    """Group one invoice's classes into billing lines, one per subject and rate."""
    items = session.scalars(
        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)
    ).all()
    if invoice.status == "Issued":
        return _group_invoice_items(invoice, items, {}, {}, {}, {})

    session_ids = {item.session_id for item in items}
    lessons = {
        lesson.id: lesson
        for lesson in (
            session.scalars(
                select(ClassSession).where(ClassSession.id.in_(session_ids))
            ).all()
            if session_ids else []
        )
    }
    class_ids = {lesson.class_id for lesson in lessons.values()}
    teacher_ids = {lesson.teacher_id for lesson in lessons.values()}
    classes = {
        item.id: item
        for item in (
            session.scalars(
                select(AcademyClass).where(AcademyClass.id.in_(class_ids))
            ).all()
            if class_ids else []
        )
    }
    teachers = {
        item.id: item
        for item in (
            session.scalars(select(Teacher).where(Teacher.id.in_(teacher_ids))).all()
            if teacher_ids else []
        )
    }
    rate_index = _rate_index(session, class_ids)
    return _group_invoice_items(invoice, items, lessons, classes, teachers, rate_index)


def _credit_totals(session, invoice_ids):
    """How much credit each invoice settled, keyed by invoice id.

    Only ``_apply_credits`` ever attaches a credit to an invoice, so this is
    exactly what came off each one when it was issued.
    """
    invoice_ids = list(invoice_ids)
    if not invoice_ids:
        return {}
    return {
        invoice_id: float(total or 0.0)
        for invoice_id, total in session.execute(
            select(Credit.invoice_id, func.sum(Credit.amount))
            .where(Credit.invoice_id.in_(invoice_ids))
            .group_by(Credit.invoice_id)
        ).all()
    }


def _net_totals(session, invoice_ids):
    """What each invoice actually asks for: its lines, less the credit it settled.

    The single definition of "the figure on the invoice".  The printable copy
    arrives at it by listing the credit as a negative line; everything working
    from the stored figures instead -- the invoice list, the Payments screen,
    recording a payment -- has to subtract it the same way, or a parent is
    chased for money their invoice never asked for.
    """
    invoice_ids = list(invoice_ids)
    if not invoice_ids:
        return {}
    gross = {
        invoice_id: float(total or 0.0)
        for invoice_id, total in session.execute(
            select(InvoiceItem.invoice_id, func.sum(InvoiceItem.amount))
            .where(InvoiceItem.invoice_id.in_(invoice_ids))
            .group_by(InvoiceItem.invoice_id)
        ).all()
    }
    credited = _credit_totals(session, invoice_ids)
    # _apply_credits never settles more than the invoice charges, so this
    # cannot really go negative; the floor keeps it honest if one ever does.
    return {
        invoice_id: round(
            max(gross.get(invoice_id, 0.0) - credited.get(invoice_id, 0.0), 0.0), 2
        )
        for invoice_id in invoice_ids
    }


def get_invoices(status=None, student_id=None):
    """Invoices with their totals, newest first.

    An Issued invoice's items are frozen -- once issued, those figures never
    change again -- so its Total and Classes count come from one SQL
    aggregate (a plain SUM/COUNT of numbers already sitting in the table),
    not from re-fetching every item and re-grouping them in Python on every
    render. That grouping is what "Lines" (the itemized per-subject
    breakdown) needs, and nothing in the list view reads Lines for an
    Issued invoice -- it stays empty here; get_invoice() (singular) still
    builds the full breakdown, for the printable copy, where it matters.
    An Open invoice's figures aren't frozen yet, so it keeps the fuller
    item/class/class/teacher/rate lookup, same as before.
    """

    with SessionLocal() as session:
        query = select(Invoice, Student).join(Student, Invoice.student_id == Student.id)
        if status:
            query = query.where(Invoice.status == status)
        if student_id:
            query = query.where(Invoice.student_id == student_id)
        rows = session.execute(query).all()
        if not rows:
            return []

        issued_ids = [invoice.id for invoice, _ in rows if invoice.status == "Issued"]
        open_ids = [invoice.id for invoice, _ in rows if invoice.status != "Issued"]

        # The money comes from _net_totals so this agrees with the printable
        # copy and the Payments screen; only the line count is counted here.
        issued_nets = _net_totals(session, issued_ids)
        issued_counts: dict[int, int] = {}
        if issued_ids:
            for invoice_id, count in session.execute(
                select(InvoiceItem.invoice_id, func.count())
                .where(InvoiceItem.invoice_id.in_(issued_ids))
                .group_by(InvoiceItem.invoice_id)
            ).all():
                issued_counts[invoice_id] = count

        items_by_invoice: dict[int, list[InvoiceItem]] = defaultdict(list)
        if open_ids:
            for item in session.scalars(
                select(InvoiceItem).where(InvoiceItem.invoice_id.in_(open_ids))
            ).all():
                items_by_invoice[item.invoice_id].append(item)

        open_session_ids = {
            item.session_id
            for items in items_by_invoice.values()
            for item in items
        }
        lessons = {
            lesson.id: lesson
            for lesson in (
                session.scalars(
                    select(ClassSession).where(ClassSession.id.in_(open_session_ids))
                ).all()
                if open_session_ids else []
            )
        }
        class_ids = {lesson.class_id for lesson in lessons.values()}
        teacher_ids = {lesson.teacher_id for lesson in lessons.values()}
        classes = {
            item.id: item
            for item in (
                session.scalars(
                    select(AcademyClass).where(AcademyClass.id.in_(class_ids))
                ).all()
                if class_ids else []
            )
        }
        teachers = {
            item.id: item
            for item in (
                session.scalars(select(Teacher).where(Teacher.id.in_(teacher_ids))).all()
                if teacher_ids else []
            )
        }
        rate_index = _rate_index(session, class_ids)

        # An issued invoice's credits are already inside _net_totals above.
        # An open one has none attached yet, so its draft figure has to look
        # at whatever its student still has waiting.
        open_credit = defaultdict(float)
        if open_ids:
            student_ids = {invoice.student_id for invoice, _ in rows}
            for student_id, total in session.execute(
                select(Credit.student_id, func.sum(Credit.amount))
                .where(Credit.student_id.in_(student_ids), Credit.status == "Open")
                .group_by(Credit.student_id)
            ).all():
                open_credit[student_id] = float(total or 0)

        results = []
        for invoice, student in rows:
            if invoice.status == "Issued":
                total = issued_nets.get(invoice.id, 0.0)
                count = issued_counts.get(invoice.id, 0)
                lines: list[dict] = []
            else:
                lines = _group_invoice_items(
                    invoice, items_by_invoice.get(invoice.id, []),
                    lessons, classes, teachers, rate_index,
                )
                total = sum(line["Amount"] for line in lines)
                count = sum(line["Quantity"] for line in lines)
                total = max(0.0, total - open_credit.get(student.id, 0.0))
            results.append(
                {
                    "ID": invoice.id,
                    "Number": invoice.invoice_number,
                    "Student": student.full_name,
                    "Student ID": student.id,
                    "Status": invoice.status,
                    "Issued": invoice.issued_on,
                    "Classes": count,
                    "Total": round(total, 2),
                    "Lines": lines,
                    "Created": invoice.created_at,
                }
            )
        results.sort(
            key=lambda row: (row["Status"] != "Open", -(row["ID"] or 0))
        )
        return results


def get_invoice_counts():
    """How many invoices are open and how many have been issued.

    Only the two numbers the browse-everything sections put in their
    headings, so the Invoices tab does not have to load every invoice to
    label a collapsed list -- and Streamlit runs every tab's body on every
    interaction anywhere in the app.
    """
    with SessionLocal() as session:
        open_count = session.scalar(
            select(func.count()).select_from(Invoice).where(Invoice.status == "Open")
        ) or 0
        issued_count = session.scalar(
            select(func.count()).select_from(Invoice).where(Invoice.status == "Issued")
        ) or 0
        return {"open_count": int(open_count), "issued_count": int(issued_count)}


def get_month_invoice_summary(year, month):
    """The billing position for one month of classes.

    A month is the unit the academy actually works in: August's schedule is
    typed in July, invoiced at the start of August, and chased from late
    September. So these are all scoped to *classes taught in that month* --
    what still needs invoicing, and what already has been -- rather than to
    whatever today's date happens to be.

    ``credit_outstanding`` is the exception and is deliberately not scoped:
    money owed to a student sits against the student, not a month, and is
    waiting to come off whichever invoice they are sent next.
    """
    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])

    with SessionLocal() as session:
        # Still to bill: open invoice items whose class falls in the month.
        rows = session.execute(
            select(Invoice.student_id, ClassSession)
            .select_from(InvoiceItem)
            .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
            .join(ClassSession, InvoiceItem.session_id == ClassSession.id)
            .where(
                Invoice.status == "Open",
                ClassSession.session_date >= first_day,
                ClassSession.session_date <= last_day,
            )
        ).all()
        rate_index = _rate_index(session, {lesson.class_id for _, lesson in rows})
        to_bill_students = {student_id for student_id, _ in rows}
        to_bill_value = sum(
            _class_hours(lesson)
            * _rate_lookup(rate_index, lesson.class_id, lesson.session_date)
            for _, lesson in rows
        )

        # Already billed: issued items carry their own frozen figures, and
        # their session_date was copied across at the time.
        billed_count, billed_value = session.execute(
            select(
                func.count(func.distinct(InvoiceItem.invoice_id)),
                func.coalesce(func.sum(InvoiceItem.amount), 0),
            )
            .select_from(InvoiceItem)
            .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
            .where(
                Invoice.status == "Issued",
                InvoiceItem.session_date >= first_day,
                InvoiceItem.session_date <= last_day,
            )
        ).one()

        credit_outstanding = session.scalar(
            select(func.coalesce(func.sum(Credit.amount), 0.0))
            .where(Credit.status == "Open")
        ) or 0.0

        return {
            "to_bill_count": len(to_bill_students),
            "to_bill_value": round(float(to_bill_value), 2),
            "billed_count": int(billed_count or 0),
            "billed_value": round(float(billed_value or 0), 2),
            "credit_outstanding": round(float(credit_outstanding), 2),
        }


def get_invoice(invoice_id):
    with SessionLocal() as session:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None:
            return None
        student = session.get(Student, invoice.student_id)
        parent = session.get(Parent, student.parent_id) if student and student.parent_id else None
        lines = _invoice_lines(session, invoice)
        charges = round(sum(line["Amount"] for line in lines), 2)
        # An issued invoice shows the credits it actually settled; one still
        # open shows only as much as its own charges can absorb, so the draft
        # total is the figure issuing will really ask for. Anything past that
        # carries forward instead of turning the invoice negative.
        lines += (
            _credit_lines(session, invoice)
            if invoice.status == "Issued"
            else _open_credit_lines(session, invoice.student_id, charges)
        )
        return {
            "ID": invoice.id,
            "Number": invoice.invoice_number,
            "Student": student.full_name if student else "",
            "Parent": parent.name if parent else "",
            "Phone": parent.phone if parent else "",
            "Status": invoice.status,
            "Issued": invoice.issued_on,
            "Lines": lines,
            "Total": round(sum(line["Amount"] for line in lines), 2),
        }


def get_invoices_detailed(invoice_ids):
    """Several invoices in full, in a fixed number of queries rather than per invoice.

    ``get_invoice`` costs four round trips, and the bulk paths -- rendering a
    month's invoices to files, or listing what was just issued -- ask for one
    invoice at a time. Thirty of them is a hundred and twenty round trips:
    under a second beside the database, and the better part of half a minute
    across an ocean, which is what the deployment actually is.

    Everything an *issued* invoice needs is fetched up front and grouped in
    Python, so the cost stops growing with the number of invoices. Issued is
    the case that matters here: its items carry their own frozen figures, so
    no rates or lessons have to be looked up at all.

    An invoice that is still open falls back to ``get_invoice``'s own path,
    one at a time. Drafts are read singly in the interface, the arithmetic
    for them is considerably more delicate -- credits are apportioned against
    charges -- and there is nothing to be gained by duplicating it here.

    Returns the same dictionaries as ``[get_invoice(i) for i in ids]``, in the
    order asked for, skipping ids that do not exist.
    """
    ids = [int(i) for i in invoice_ids]
    if not ids:
        return []

    with SessionLocal() as session:
        invoices = {
            invoice.id: invoice
            for invoice in session.scalars(
                select(Invoice).where(Invoice.id.in_(ids))
            ).all()
        }
        if not invoices:
            return []

        students = {
            student.id: student
            for student in session.scalars(
                select(Student).where(
                    Student.id.in_({inv.student_id for inv in invoices.values()})
                )
            ).all()
        }
        parent_ids = {
            student.parent_id for student in students.values() if student.parent_id
        }
        parents = {
            parent.id: parent
            for parent in (
                session.scalars(select(Parent).where(Parent.id.in_(parent_ids))).all()
                if parent_ids else []
            )
        }

        items_by_invoice: dict[int, list] = {}
        for item in session.scalars(
            select(InvoiceItem).where(InvoiceItem.invoice_id.in_(invoices))
        ).all():
            items_by_invoice.setdefault(item.invoice_id, []).append(item)

        credits_by_invoice: dict[int, list] = {}
        for credit in session.scalars(
            select(Credit).where(Credit.invoice_id.in_(invoices))
        ).all():
            credits_by_invoice.setdefault(credit.invoice_id, []).append(credit)

        detailed = []
        for invoice_id in ids:
            invoice = invoices.get(invoice_id)
            if invoice is None:
                continue
            student = students.get(invoice.student_id)
            parent = (
                parents.get(student.parent_id)
                if student and student.parent_id
                else None
            )

            if invoice.status == "Issued":
                lines = _group_invoice_items(
                    invoice, items_by_invoice.get(invoice_id, []), {}, {}, {}, {}
                )
                lines += _group_credits(
                    (credit, float(credit.amount or 0))
                    for credit in credits_by_invoice.get(invoice_id, [])
                )
            else:
                lines = _invoice_lines(session, invoice)
                charges = round(sum(line["Amount"] for line in lines), 2)
                lines += _open_credit_lines(session, invoice.student_id, charges)

            detailed.append(
                {
                    "ID": invoice.id,
                    "Number": invoice.invoice_number,
                    "Student": student.full_name if student else "",
                    "Parent": parent.name if parent else "",
                    "Phone": parent.phone if parent else "",
                    "Status": invoice.status,
                    "Issued": invoice.issued_on,
                    "Lines": lines,
                    "Total": round(sum(line["Amount"] for line in lines), 2),
                }
            )
        return detailed


def issue_invoice_for_month(invoice_id, year, month, issued_on=None):
    """Freeze only the classes on an open invoice that fall in one month.

    A student's invoice stays open and collects every class until someone
    issues it -- but billing should only ever cover the month asked for. If
    the invoice also holds classes from outside that month, this splits it:
    the month's classes get a new, numbered, Issued invoice, and everything
    else stays right where it was, still open, for whenever that month gets
    billed.
    """
    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])

    with SessionLocal() as session:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None:
            return "missing", None
        if invoice.status == "Issued":
            return "already_issued", None

        items = session.scalars(
            select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)
        ).all()

        in_month: list[tuple[InvoiceItem, ClassSession]] = []
        other: list[InvoiceItem] = []
        for item in items:
            lesson = session.get(ClassSession, item.session_id)
            if lesson is None:
                session.delete(item)
                continue
            if first_day <= lesson.session_date <= last_day:
                in_month.append((item, lesson))
            else:
                other.append(item)

        if not in_month:
            session.commit()
            return "empty", None

        if other:
            target = Invoice(student_id=invoice.student_id, status="Open")
            session.add(target)
            session.flush()
        else:
            target = invoice

        rate_index = _rate_index(session, {lesson.class_id for _, lesson in in_month})
        # A warning was not enough: 44 lines have already gone out at the
        # placeholder. Nothing priced at or below it can be billed -- an
        # invoice asking a parent for a dollar an hour is worse than one that
        # was never sent.
        if any(
            _rate_lookup(rate_index, lesson.class_id, lesson.session_date)
            <= UNSET_RATE
            for _, lesson in in_month
        ):
            session.rollback()
            return "unpriced", None
        for item, lesson in in_month:
            academy_class = session.get(AcademyClass, lesson.class_id)
            teacher = session.get(Teacher, lesson.teacher_id)
            hours = _class_hours(lesson)
            hourly = _rate_lookup(rate_index, lesson.class_id, lesson.session_date)
            item.invoice_id = target.id
            item.class_name = academy_class.name if academy_class else ""
            item.teacher_name = teacher.name if teacher else ""
            item.teacher_id = lesson.teacher_id
            item.session_date = lesson.session_date
            item.hours = hours
            item.hourly_rate = hourly
            item.amount = round(hours * hourly, 2)

        when = issued_on or date.today()
        # The amounts were just frozen onto the items above; sum those rather
        # than working them out a second time from hours and rates.
        items_total = sum(float(item.amount or 0) for item, _ in in_month)
        _apply_credits(session, target, items_total, when)

        highest = session.scalar(select(func.max(Invoice.invoice_number)))
        target.invoice_number = max(INVOICE_NUMBER_START, (highest or 0) + 1)
        target.issued_on = when
        target.status = "Issued"
        session.commit()
        return "issued", target.id


def delete_invoice(invoice_id):
    """Remove an invoice and its lines.  Only an open invoice can go."""

    with SessionLocal() as session:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None:
            return "missing"
        if invoice.status == "Issued":
            return "issued"
        for item in session.scalars(
            select(InvoiceItem).where(InvoiceItem.invoice_id == invoice_id)
        ).all():
            session.delete(item)
        session.delete(invoice)
        session.commit()
        return "deleted"


# ---------------------------------------------------------------------------
# Credits for cancelled classes
# ---------------------------------------------------------------------------


def get_credits(status=None, student_id=None):
    """Credits with their student, newest first."""
    with SessionLocal() as session:
        query = select(Credit, Student).join(Student, Credit.student_id == Student.id)
        if status:
            query = query.where(Credit.status == status)
        if student_id:
            query = query.where(Credit.student_id == student_id)
        rows = session.execute(query).all()
        results = [
            {
                "ID": credit.id,
                "Student": student.full_name,
                "Student ID": student.id,
                "Amount": float(credit.amount or 0),
                "Subject": credit.class_name or "",
                "Class date": credit.session_date,
                "Reason": credit.reason,
                "Status": credit.status,
                "Invoice ID": credit.invoice_id,
                "Created": credit.created_on,
                "Settled": credit.settled_on,
                "Note": credit.note or "",
            }
            for credit, student in rows
        ]
        results.sort(
            key=lambda row: (row["Status"] != "Open", row["Class date"] or date.min),
        )
        return results


def refund_credit(credit_id, refunded_on=None):
    """Settle a credit with money back instead of carrying it forward."""
    with SessionLocal() as session:
        credit = session.get(Credit, credit_id)
        if credit is None:
            return "missing"
        if credit.status != "Open":
            return "already_settled"
        credit.status = "Refunded"
        credit.settled_on = refunded_on or date.today()
        session.commit()
        return "refunded"


def get_student_credit_total(student_id):
    """What a student is owed and has not yet had deducted."""
    with SessionLocal() as session:
        return float(
            session.scalar(
                select(func.coalesce(func.sum(Credit.amount), 0.0)).where(
                    Credit.student_id == student_id, Credit.status == "Open"
                )
            )
            or 0.0
        )


def count_billed_cancellations():
    """Cancelled classes whose charge has not been put right yet.

    A charge needs action when it sits on an invoice not yet sent (it can
    simply come off), or on one already sent that has not been credited
    back. Once credited, an issued invoice keeps its item deliberately --
    a sent invoice is never rewritten -- so the item's continued existence
    must not be read as outstanding work, or the correction pass would
    re-run on every start for the rest of the database's life.
    """
    with SessionLocal() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(SessionAttendance)
                .join(ClassSession, SessionAttendance.session_id == ClassSession.id)
                .join(InvoiceItem, InvoiceItem.session_id == ClassSession.id)
                .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
                .where(
                    SessionAttendance.is_cancelled.is_(True),
                    Invoice.student_id == SessionAttendance.student_id,
                    or_(
                        Invoice.status == "Open",
                        ~exists().where(
                            Credit.student_id == SessionAttendance.student_id,
                            Credit.session_id == ClassSession.id,
                        ),
                    ),
                )
            )
            or 0
        )


def backfill_cancellation_credits():
    """Bring already-recorded cancellations in line with the rule that a
    cancelled class is never paid for.

    Written for the one-off correction of data captured before the rule
    existed, but safe to run at any time: a cancelled class on an invoice
    still open is simply taken off it, one on an issued invoice raises a
    credit, and neither step repeats itself on a second run.
    """
    removed = credited = 0
    with SessionLocal() as session:
        rows = session.execute(
            select(SessionAttendance, ClassSession)
            .join(ClassSession, SessionAttendance.session_id == ClassSession.id)
            .where(SessionAttendance.is_cancelled.is_(True))
        ).all()
        rate_index = _rate_index(session, {lesson.class_id for _, lesson in rows})
        for attendance, lesson in rows:
            item = session.scalar(
                select(InvoiceItem)
                .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
                .where(
                    InvoiceItem.session_id == lesson.id,
                    Invoice.student_id == attendance.student_id,
                )
            )
            if item is None:
                continue
            invoice = session.get(Invoice, item.invoice_id)
            if invoice is None:
                continue
            if invoice.status == "Issued":
                _, raised = _raise_credit(
                    session, attendance.student_id, lesson,
                    "Cancelled class", rate_index,
                )
                credited += int(raised)
            else:
                session.delete(item)
                removed += 1
        session.commit()
    return {"removed_from_open": removed, "credits_raised": credited}


def remove_lessons_not_in(teacher_id, periods, keep_slots, class_ids):
    """Delete a teacher's classes that a re-imported workbook no longer lists.

    Scoped deliberately tightly: only the given (year, month) periods, only
    that teacher, so uploading one month can never disturb another month or
    another teacher's timetable.

    A class that moved to a new day leaves its old slot behind, and without
    this the student would be billed for both. Removing it is treated the
    same as a cancellation: dropped from an invoice still open, credited
    back if the invoice has already gone out.

    ``keep_slots`` is the set of (class_id, date, start_time) the workbook
    still describes; ``class_ids`` limits the sweep to the classes it
    actually contains, so a class whose import was skipped keeps its
    classes rather than looking like one that was dropped.
    """
    removed = credited = 0
    with SessionLocal() as session:
        for year, month in periods:
            first_day = date(year, month, 1)
            last_day = date(year, month, monthrange(year, month)[1])
            lessons = session.scalars(
                select(ClassSession).where(
                    ClassSession.teacher_id == teacher_id,
                    ClassSession.session_date >= first_day,
                    ClassSession.session_date <= last_day,
                )
            ).all()
            stale = [
                lesson for lesson in lessons
                if lesson.class_id in class_ids
                and (lesson.class_id, lesson.session_date, lesson.start_time)
                not in keep_slots
            ]
            if not stale:
                continue
            rate_index = _rate_index(session, {lesson.class_id for lesson in stale})
            for lesson in stale:
                items = session.scalars(
                    select(InvoiceItem).where(InvoiceItem.session_id == lesson.id)
                ).all()
                for item in items:
                    invoice = session.get(Invoice, item.invoice_id)
                    if invoice is None:
                        continue
                    if invoice.status == "Issued":
                        # Already asked for; owe it back rather than
                        # rewriting an invoice that has gone out.
                        credit, raised = _raise_credit(
                            session, invoice.student_id, lesson,
                            "Class removed from the schedule", rate_index,
                        )
                        credit.session_id = None      # the class is about to go
                        item.session_id = None        # ... and so is its line's link
                        credited += int(raised)
                    else:
                        session.delete(item)
                for attendance in session.scalars(
                    select(SessionAttendance).where(
                        SessionAttendance.session_id == lesson.id
                    )
                ).all():
                    session.delete(attendance)
                for credit in session.scalars(
                    select(Credit).where(Credit.session_id == lesson.id)
                ).all():
                    credit.session_id = None
                session.delete(lesson)
                removed += 1
        session.commit()
    return {"lessons_removed": removed, "credits_raised": credited}


def payment_due_date(year, month):
    """When an invoice covering that month of classes becomes overdue.

    The 20th of the month after the classes, matching how the academy has
    always chased payment.
    """
    due_year, due_month = (year, month + 1)
    if due_month == 13:
        due_year, due_month = due_year + 1, 1
    return date(due_year, due_month, 20)


def _sync_lesson_paid_flags(session, invoice, paid):
    """Keep the per-class flags in step with the invoice.

    The timetable grid marks classes with money still owing, and it reads
    those flags. They are no longer edited by hand -- marking the invoice is
    the single action -- so they are kept correct from here.
    """
    session_ids = [
        row[0]
        for row in session.execute(
            select(InvoiceItem.session_id).where(
                InvoiceItem.invoice_id == invoice.id,
                InvoiceItem.session_id.is_not(None),
            )
        ).all()
    ]
    if not session_ids:
        return
    for attendance in session.scalars(
        select(SessionAttendance).where(
            SessionAttendance.student_id == invoice.student_id,
            SessionAttendance.session_id.in_(session_ids),
        )
    ).all():
        attendance.is_paid = bool(paid)


def mark_invoice_paid(invoice_id, paid_on=None, amount=None, note=None):
    """Record money received against an invoice.

    ``amount`` defaults to whatever is still owing, which is what almost every
    payment is. A smaller figure records a part payment: it adds to what has
    already come in rather than replacing it, so a parent who pays twice ends
    up settled, and the invoice keeps being chased until the balance is
    actually covered.
    """
    with SessionLocal() as session:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None:
            return "missing"
        if invoice.status != "Issued":
            return "not issued"

        # What the invoice asked for, not what its lines add up to: a credit
        # settled when it was issued already came off the figure the parent
        # was sent, so chasing the gross would leave every such invoice
        # permanently short.
        total = _net_totals(session, [invoice_id]).get(invoice_id, 0.0)
        already = float(invoice.paid_amount or 0.0)
        if already >= round(total, 2) and invoice.paid_on is not None:
            return "already paid"

        received = round(total - already, 2) if amount is None else round(float(amount), 2)
        invoice.paid_on = paid_on or date.today()
        invoice.paid_amount = round(already + received, 2)
        note = (note or "").strip()
        if note:
            invoice.payment_note = (
                f"{invoice.payment_note}; {note}" if invoice.payment_note else note
            )
        # The timetable's "owing" markers only clear once the whole invoice is
        # covered -- a part payment leaves the classes owing.
        _sync_lesson_paid_flags(
            session, invoice, invoice.paid_amount >= round(total, 2)
        )
        session.commit()
        return "paid" if invoice.paid_amount >= round(total, 2) else "part paid"


def unmark_invoice_paid(invoice_id):
    """Undo a payment recorded by mistake."""
    with SessionLocal() as session:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None:
            return "missing"
        if invoice.paid_on is None:
            return "not paid"
        invoice.paid_on = None
        invoice.paid_amount = None
        invoice.payment_note = None
        _sync_lesson_paid_flags(session, invoice, False)
        session.commit()
        return "reopened"


def get_invoice_payments(year, month, today=None):
    """Issued invoices covering a month, split into unpaid and paid.

    One query for the invoices and one for their totals, rather than a total
    per invoice -- a busy month is well over a hundred of these and the
    Payments screen draws them all.
    """
    today = today or date.today()
    year, month = int(year), int(month)
    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])
    due = payment_due_date(year, month)

    with SessionLocal() as session:
        # An invoice belongs to one month's screen: the month of its latest
        # class, which is also the month its due date is worked out from (see
        # payment_due_date and get_payment_reminders). Listing it under every
        # month it happened to touch put the same invoice, at its full value,
        # on several screens at once and counted it as outstanding on each --
        # so a single invoice spanning nine months overstated eight of them.
        spans = {
            invoice_id: (earliest, latest)
            for invoice_id, earliest, latest in session.execute(
                select(
                    InvoiceItem.invoice_id,
                    func.min(InvoiceItem.session_date),
                    func.max(InvoiceItem.session_date),
                )
                .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
                .where(Invoice.status == "Issued")
                .group_by(InvoiceItem.invoice_id)
            ).all()
        }
        invoice_ids = [
            invoice_id
            for invoice_id, (_, latest) in spans.items()
            if latest is not None and first_day <= _as_day(latest) <= last_day
        ]
        if not invoice_ids:
            return {"unpaid": [], "paid": [], "due": due}

        totals = _net_totals(session, invoice_ids)

        unpaid, paid = [], []
        for invoice, student in session.execute(
            select(Invoice, Student)
            .join(Student, Student.id == Invoice.student_id)
            .where(Invoice.id.in_(invoice_ids))
            .order_by(Student.full_name)
        ).all():
            row = {
                "ID": invoice.id,
                "Number": invoice.invoice_number,
                "Student": student.full_name,
                "Student ID": student.id,
                "Total": round(totals.get(invoice.id, 0.0), 2),
                "Issued": invoice.issued_on,
                "Paid on": invoice.paid_on,
                "Paid amount": (
                    round(float(invoice.paid_amount), 2)
                    if invoice.paid_amount is not None
                    else None
                ),
                "Note": invoice.payment_note or "",
            }
            # An invoice issued a month at a time covers exactly one; one
            # billed whole ("Bill it now") can cover several, and the screen
            # has to say so rather than let its total read as this month's.
            earliest, latest = spans.get(invoice.id, (None, None))
            row["Covers"] = ""
            if earliest is not None and latest is not None:
                earliest, latest = _as_day(earliest), _as_day(latest)
                if (earliest.year, earliest.month) != (latest.year, latest.month):
                    row["Covers"] = (
                        f"{month_abbr[earliest.month]} {earliest.year} – "
                        f"{month_abbr[latest.month]} {latest.year}"
                    )
            row["Owing"] = round(row["Total"] - float(invoice.paid_amount or 0.0), 2)
            if row["Owing"] > 0.005:
                # Includes part-paid invoices: money is still owed, so they
                # belong with the work, not with the done pile.
                row["Overdue"] = max((today - due).days, 0) if today > due else 0
                unpaid.append(row)
            else:
                paid.append(row)
        paid.sort(key=lambda item: (item["Paid on"], item["Student"]), reverse=True)
        return {"unpaid": unpaid, "paid": paid, "due": due}


def backfill_invoice_payments():
    """Carry the old per-class ticks up onto the invoices they belong to.

    Before payment moved to the invoice, an admin ticked each class. Any
    issued invoice whose classes were all ticked was, in the old scheme, fully
    paid -- so it is recorded as paid here rather than reappearing as a debt.
    """
    with SessionLocal() as session:
        candidates = session.scalars(
            select(Invoice).where(Invoice.status == "Issued", Invoice.paid_on.is_(None))
        ).all()
        marked = 0
        for invoice in candidates:
            session_ids = [
                row[0]
                for row in session.execute(
                    select(InvoiceItem.session_id).where(
                        InvoiceItem.invoice_id == invoice.id,
                        InvoiceItem.session_id.is_not(None),
                    )
                ).all()
            ]
            if not session_ids:
                continue
            flags = [
                row[0]
                for row in session.execute(
                    select(SessionAttendance.is_paid).where(
                        SessionAttendance.student_id == invoice.student_id,
                        SessionAttendance.session_id.in_(session_ids),
                    )
                ).all()
            ]
            if flags and all(flags):
                total = session.scalar(
                    select(func.coalesce(func.sum(InvoiceItem.amount), 0.0)).where(
                        InvoiceItem.invoice_id == invoice.id
                    )
                ) or 0.0
                invoice.paid_on = invoice.issued_on or date.today()
                invoice.paid_amount = round(float(total), 2)
                invoice.payment_note = "Carried over from per-class ticks"
                marked += 1
        if marked:
            session.commit()
        return marked


def get_payment_reminders(today=None):
    """Issued invoices past their due date that nobody has recorded payment for.

    An invoice covering January classes is due on 20 February, so it appears
    here from that date. One row per unpaid invoice -- which is one row per
    thing you would actually chase a parent about.

    Bounded by date in SQL rather than filtered in Python afterwards: an
    academy that lets a year go unpaid still only shows the overdue ones, and
    there is no reason to drag the rest across the boundary.
    """
    today = today or date.today()

    # An invoice can only be overdue once the 20th of the month after its
    # classes has passed, so the newest month that can qualify is last month
    # (from the 20th onward) or the month before that.
    newest_year, newest_month = today.year, today.month - (1 if today.day >= 20 else 2)
    while newest_month < 1:
        newest_month += 12
        newest_year -= 1
    cutoff = date(newest_year, newest_month, monthrange(newest_year, newest_month)[1])

    with SessionLocal() as session:
        # Latest class on each invoice decides when it falls due, and the
        # totals come back in the same grouped pass.
        rows = session.execute(
            select(
                Invoice.id,
                Invoice.invoice_number,
                Student.id,
                Student.full_name,
                func.max(InvoiceItem.session_date),
                func.min(InvoiceItem.session_date),
                func.sum(InvoiceItem.amount),
                func.count(),
            )
            .select_from(Invoice)
            .join(InvoiceItem, InvoiceItem.invoice_id == Invoice.id)
            .join(Student, Student.id == Invoice.student_id)
            .where(
                Invoice.status == "Issued",
                InvoiceItem.session_date <= cutoff,
            )
            # Both primary keys, not just the invoice's. Postgres will let
            # the other columns of a table ride along on that table's grouped
            # primary key, but the student's name is not covered by grouping
            # on the invoice, and it rejects the query outright. SQLite waves
            # it through and picks a row, which is why this only failed once
            # the app was on a real database.
            .group_by(Invoice.id, Student.id)
            # Anything still owing, which takes in part payments as well as
            # invoices nobody has paid a cent of.
            .having(
                func.sum(InvoiceItem.amount)
                > func.coalesce(Invoice.paid_amount, 0.0) + 0.005
            )
        ).all()
        if not rows:
            return []

        # Subjects in a second grouped query rather than a group_concat: at
        # least one real class is named "S3 REVISION (PHYS, MATH)", and splitting
        # a concatenated string on its separator would tear that in half.
        chased = [row[0] for row in rows]
        paid_so_far = {
            invoice_id: float(amount or 0.0)
            for invoice_id, amount in session.execute(
                select(Invoice.id, Invoice.paid_amount).where(Invoice.id.in_(chased))
            ).all()
        }
        # The HAVING above compares against the lines alone, because the
        # credit an invoice settled cannot be summed in the same grouped
        # pass. Subtract it here: an invoice covered by credit asks for
        # nothing, and chasing a parent for it would never stop, since no
        # payment could ever reach the gross figure.
        credited = _credit_totals(session, chased)
        subjects_by_invoice = defaultdict(set)
        for invoice_id, class_name in session.execute(
            select(InvoiceItem.invoice_id, InvoiceItem.class_name)
            .where(InvoiceItem.invoice_id.in_(chased))
            .distinct()
        ).all():
            subjects_by_invoice[invoice_id].add(class_name)

        results = []
        for (
            invoice_id, number, student_id, name, latest, earliest, amount, classes
        ) in rows:
            latest, earliest = _as_day(latest), _as_day(earliest)
            due = payment_due_date(latest.year, latest.month)
            if today < due:
                continue
            owing = round(
                float(amount or 0.0)
                - credited.get(invoice_id, 0.0)
                - paid_so_far.get(invoice_id, 0.0),
                2,
            )
            if owing <= 0.005:
                continue
            results.append(
                {
                    "Invoice ID": invoice_id,
                    "Number": number,
                    "Student": name,
                    "Student ID": student_id,
                    "Started": earliest,
                    "Month": date(latest.year, latest.month, 1),
                    "Due": due,
                    "Classes": classes,
                    "Amount": owing,
                    "Part paid": paid_so_far.get(invoice_id, 0.0) > 0.005,
                    "Subjects": ", ".join(sorted(subjects_by_invoice.get(invoice_id, ()))),
                    "Days overdue": (today - due).days,
                }
            )
        results.sort(key=lambda row: (row["Started"], row["Student"]))
        return results


# ---------------------------------------------------------------------------
# Excel schedule import
# ---------------------------------------------------------------------------


def find_timetable_session(class_id, session_date, start_time):
    """Return the id of the class already at this class/date/time, if any.

    Used by the Excel importer to decide whether a parsed class is new or
    should update one already on the calendar.
    """
    with SessionLocal() as session:
        return session.scalar(
            select(ClassSession.id).where(
                ClassSession.class_id == class_id,
                ClassSession.session_date == session_date,
                ClassSession.start_time == start_time,
            )
        )


def _record_import(teacher_id, year, month, sessions_created, sessions_updated, warning_count):
    """Upsert the (teacher, year, month) marker the Students tab reads."""
    with SessionLocal() as session:
        row = session.scalar(
            select(ScheduleImport).where(
                ScheduleImport.teacher_id == teacher_id,
                ScheduleImport.year == year,
                ScheduleImport.month == month,
            )
        )
        if row is None:
            row = ScheduleImport(teacher_id=teacher_id, year=year, month=month)
            session.add(row)
        row.sessions_created = sessions_created
        row.sessions_updated = sessions_updated
        row.warning_count = warning_count
        row.imported_at = datetime.utcnow()
        session.commit()


def get_import_status(year, month):
    """One row per active teacher: whether/when they have this month imported."""
    with SessionLocal() as session:
        teachers = session.scalars(
            select(Teacher).where(Teacher.is_active.is_(True)).order_by(Teacher.name)
        ).all()
        imports = {
            row.teacher_id: row
            for row in session.scalars(
                select(ScheduleImport).where(
                    ScheduleImport.year == year, ScheduleImport.month == month
                )
            ).all()
        }
        results = []
        for teacher in teachers:
            row = imports.get(teacher.id)
            results.append(
                {
                    "Teacher ID": teacher.id,
                    "Teacher": teacher.name,
                    "Imported": row is not None,
                    "Imported At": row.imported_at if row else None,
                    "Sessions": (row.sessions_created + row.sessions_updated) if row else 0,
                    "Warnings": row.warning_count if row else 0,
                }
            )
        return results


# ---------------------------------------------------------------------------
# Month reporting -- Students / Invoices / Data tabs
# ---------------------------------------------------------------------------


def get_students_in_month(year, month):
    """Students with at least one class in this calendar month."""
    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])
    with SessionLocal() as session:
        students = session.scalars(
            select(Student)
            .join(SessionAttendance, SessionAttendance.student_id == Student.id)
            .join(ClassSession, SessionAttendance.session_id == ClassSession.id)
            .where(
                ClassSession.session_date >= first_day,
                ClassSession.session_date <= last_day,
            )
            .distinct()
            .order_by(Student.full_name)
        ).all()
        return [{"ID": student.id, "Name": student.full_name} for student in students]


def get_all_student_month_breakdowns(year, month):
    """Every student's month breakdown at once, keyed by student id.

    What each student's classes in a month add up to, grouped the same way as
    an invoice (`_invoice_lines`), by subject, teacher and rate. One query for
    everyone rather than one call (and one query) per student -- the Students
    tab lists up to 200 of these on a single render, so batching this is what
    keeps that page from crawling.

    A class already on an issued invoice is priced from that invoice's own
    frozen line, not from today's rate table. Pricing everything live made
    this screen disagree with the bill the parent actually holds -- and, for a
    subject whose rate has since been removed, show $0.00 for a month that was
    really invoiced in full. Anything not yet billed is still priced live, so
    the figure stays a useful "what will this come to" for the current month.

    Cancelled attendance is left out either way, exactly as
    ``sync_invoice_items`` leaves it off the invoice.
    """
    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])
    with SessionLocal() as session:
        rows = session.execute(
            select(SessionAttendance.student_id, ClassSession, AcademyClass, Teacher)
            .join(ClassSession, SessionAttendance.session_id == ClassSession.id)
            .join(AcademyClass, ClassSession.class_id == AcademyClass.id)
            .join(Teacher, ClassSession.teacher_id == Teacher.id)
            .where(
                ClassSession.session_date >= first_day,
                ClassSession.session_date <= last_day,
                SessionAttendance.is_cancelled.is_(False),
            )
        ).all()
        if not rows:
            return {}

        # What each of these classes was actually billed at, where it has
        # been. Keyed by (student, class) because one class is billed
        # separately to each student on it.
        frozen: dict[tuple[int, int], tuple[str, str, float, float, float]] = {}
        for item, student_id in session.execute(
            select(InvoiceItem, Invoice.student_id)
            .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
            .where(
                Invoice.status == "Issued",
                InvoiceItem.session_id.is_not(None),
                InvoiceItem.session_date >= first_day,
                InvoiceItem.session_date <= last_day,
            )
        ).all():
            frozen[(student_id, item.session_id)] = (
                item.class_name or "",
                item.teacher_name or "",
                float(item.hours or 0.0),
                float(item.hourly_rate or 0.0),
                float(item.amount or 0.0),
            )

        rate_index = _rate_index(session, {lesson.class_id for _, lesson, _, _ in rows})
        grouped: dict[int, dict[tuple, dict[str, Any]]] = defaultdict(dict)
        billed_students: set[int] = set()
        for student_id, lesson, academy_class, teacher in rows:
            was_billed = frozen.get((student_id, lesson.id))
            if was_billed:
                class_name, teacher_name, hours, hourly, amount = was_billed
                billed_students.add(student_id)
            else:
                class_name, teacher_name = academy_class.name, teacher.name
                hourly = _rate_lookup(rate_index, lesson.class_id, lesson.session_date)
                hours = _class_hours(lesson)
                amount = round(hours * hourly, 2)
            key = (class_name, teacher_name, hourly)
            bucket = grouped[student_id].setdefault(
                key,
                {
                    "Subject": class_name,
                    "Teacher": teacher_name,
                    "Rate": hourly,
                    "Sessions": 0,
                    "Hours": 0.0,
                    "Amount": 0.0,
                },
            )
            bucket["Sessions"] += 1
            bucket["Hours"] += hours
            bucket["Amount"] += amount

        results: dict[int, dict[str, Any]] = {}
        for student_id, lines_by_key in grouped.items():
            lines = sorted(lines_by_key.values(), key=lambda line: line["Subject"])
            for line in lines:
                line["Hours"] = round(line["Hours"], 2)
                line["Amount"] = round(line["Amount"], 2)
            results[student_id] = {
                "lines": lines,
                "total": round(sum(line["Amount"] for line in lines), 2),
                # True when any of it was read off an invoice already sent, so
                # the screen can say the figure is a bill and not an estimate.
                "billed": student_id in billed_students,
            }
        return results


def get_open_invoice_items_for_month(year, month):
    """Students with an open (unbilled) invoice holding a class in this month.

    "Month Amount" is only the slice of that open invoice which falls in the
    selected month -- ``issue_invoice_for_month`` bills exactly that slice
    and leaves anything else on the invoice open.

    It is the charges alone. A student holding a credit has it deducted when
    the invoice is issued, so what they are finally asked for can be less;
    the Invoices tab says so beside the total rather than folding it in here,
    because which invoice a credit lands on is decided at issue time.
    """
    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])
    with SessionLocal() as session:
        rows = session.execute(
            select(Invoice, Student, ClassSession)
            .join(Student, Invoice.student_id == Student.id)
            .join(InvoiceItem, InvoiceItem.invoice_id == Invoice.id)
            .join(ClassSession, InvoiceItem.session_id == ClassSession.id)
            .where(
                Invoice.status == "Open",
                ClassSession.session_date >= first_day,
                ClassSession.session_date <= last_day,
            )
        ).all()

        rate_index = _rate_index(session, {lesson.class_id for _, _, lesson in rows})
        by_invoice: dict[int, dict[str, Any]] = {}
        for invoice, student, lesson in rows:
            entry = by_invoice.setdefault(
                invoice.id,
                {
                    "Invoice ID": invoice.id,
                    "Student ID": student.id,
                    "Student": student.full_name,
                    "Classes": 0,
                    "Month Amount": 0.0,
                },
            )
            hourly = _rate_lookup(rate_index, lesson.class_id, lesson.session_date)
            entry["Classes"] += 1
            entry["Month Amount"] += round(_class_hours(lesson) * hourly, 2)

        results = list(by_invoice.values())
        for entry in results:
            entry["Month Amount"] = round(entry["Month Amount"], 2)
        results.sort(key=lambda row: row["Student"])
        return results


def _lesson_window(first_day, last_day, teacher_ids):
    """The date+teacher filter every Data-tab query shares."""
    return (
        ClassSession.teacher_id.in_(teacher_ids),
        ClassSession.session_date >= first_day,
        ClassSession.session_date <= last_day,
    )


def _teacher_month_money(session, first_day, last_day, teacher_ids):
    """What each teacher's classes were actually invoiced for, per month.

    Read off the issued invoices rather than recomputed from the rate table.
    An invoice line freezes its hours and price when it goes out, so this is
    the money the academy really asked for -- it does not move when a rate is
    later changed, corrected or cleared, and it already excludes the cancelled
    students who were left off the bill.

    Teacher and date come from the line itself for the same reason: both are
    frozen on it, so a line still reports correctly even if its class was
    later edited or removed. The teacher is matched by the frozen *id*, not
    the frozen name -- a rename must not make an already-paid month vanish.
    """
    if not teacher_ids:
        return {}

    rows = session.execute(
        select(
            InvoiceItem.teacher_id,
            InvoiceItem.session_date,
            func.sum(InvoiceItem.amount),
            func.sum(InvoiceItem.hours),
        )
        .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
        .where(
            Invoice.status == "Issued",
            InvoiceItem.teacher_id.in_(list(teacher_ids)),
            InvoiceItem.session_date >= first_day,
            InvoiceItem.session_date <= last_day,
        )
        .group_by(InvoiceItem.teacher_id, InvoiceItem.session_date)
    ).all()

    buckets: dict[tuple[int, int, int], dict] = {}
    for teacher_id, when, amount, hours in rows:
        if teacher_id is None:
            continue
        when = _as_day(when)
        key = (teacher_id, when.year, when.month)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = buckets[key] = {"Earnings": 0.0, "Hours": 0.0}
        bucket["Earnings"] += float(amount or 0.0)
        bucket["Hours"] += float(hours or 0.0)
    return buckets


def _teacher_month_scheduled(session, first_day, last_day, teacher_ids):
    """Hours actually taught in a span, invoiced or not.

    Kept separate from the money: a class is taught whether or not its
    invoice has gone out, so "hours taught" must not drop to zero just
    because a month has not been billed yet.
    """
    if not teacher_ids:
        return {}
    rows = session.execute(
        select(
            ClassSession.teacher_id,
            ClassSession.session_date,
            ClassSession.start_time,
            ClassSession.end_time,
        ).where(*_lesson_window(first_day, last_day, list(teacher_ids)))
    ).all()
    buckets: dict[tuple[int, int, int], float] = {}
    for teacher_id, when, start, end in rows:
        key = (teacher_id, when.year, when.month)
        buckets[key] = buckets.get(key, 0.0) + _span_hours(start, end)
    return buckets


def _teacher_student_counts(session, first_day, last_day, teacher_ids):
    """Unique students per teacher across a span, counted by the database.

    ``COUNT(DISTINCT ...)`` in SQL rather than a set union in Python: the
    difference is a few hundred rows crossing the boundary instead of tens of
    thousands.
    """
    if not teacher_ids:
        return {}
    return {
        teacher_id: count
        for teacher_id, count in session.execute(
            select(
                ClassSession.teacher_id,
                func.count(distinct(SessionAttendance.student_id)),
            )
            .join(SessionAttendance, ClassSession.id == SessionAttendance.session_id)
            .where(*_lesson_window(first_day, last_day, list(teacher_ids)))
            .group_by(ClassSession.teacher_id)
        ).all()
    }


def _active_teachers(session, teacher_ids=None):
    query = select(Teacher).where(Teacher.is_active.is_(True))
    if teacher_ids is not None:
        query = query.where(Teacher.id.in_(list(teacher_ids)))
    return session.scalars(query.order_by(Teacher.name)).all()


def get_teacher_month_stats(year, month, teacher_ids=None):
    """Per active teacher with a class that month: earnings, hours, unique students."""
    year, month = int(year), int(month)
    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])
    with SessionLocal() as session:
        if teacher_ids is not None and not list(teacher_ids):
            return []
        teachers = _active_teachers(session, teacher_ids)
        if not teachers:
            return []

        ids = [teacher.id for teacher in teachers]
        money = _teacher_month_money(session, first_day, last_day, ids)
        taught = _teacher_month_scheduled(session, first_day, last_day, ids)
        students = _teacher_student_counts(session, first_day, last_day, ids)

        results = []
        for teacher in teachers:
            key = (teacher.id, year, month)
            bucket = money.get(key)
            hours = taught.get(key, 0.0)
            count = students.get(teacher.id, 0)
            if not bucket and not hours and not count:
                continue
            results.append(
                {
                    "Teacher ID": teacher.id,
                    "Teacher": teacher.name,
                    "Invoiced": round(bucket["Earnings"], 2) if bucket else 0.0,
                    "Billed hours": round(bucket["Hours"], 2) if bucket else 0.0,
                    "Hours": round(hours, 2),
                    "Students": count,
                }
            )
        return results


def get_teacher_year_trend(year, up_to_month, teacher_ids=None):
    """Month-by-month earnings for one calendar year, up to and including a month.

    Deliberately capped at the single year: putting December beside the
    following January on one axis reads as a cliff rather than as a year
    ending, so each year gets its own trend.

    Rows are dense -- every teacher who taught at any point in the span gets a
    row for every month in it, zero-filled -- so a line stays continuous
    through a month somebody happened not to teach.  Earnings are read off the
    classes themselves, so a month counts here whether or not it has been
    invoiced.
    """
    year = int(year)
    up_to_month = max(1, min(12, int(up_to_month)))
    first_day = date(year, 1, 1)
    last_day = date(year, up_to_month, monthrange(year, up_to_month)[1])

    with SessionLocal() as session:
        if teacher_ids is not None and not list(teacher_ids):
            return []
        teachers = _active_teachers(session, teacher_ids)
        if not teachers:
            return []

        ids = [teacher.id for teacher in teachers]
        money = _teacher_month_money(session, first_day, last_day, ids)
        scheduled = _teacher_month_scheduled(session, first_day, last_day, ids)
        if not money and not scheduled:
            return []

        # A teacher belongs on the chart if they either billed or taught in
        # the span -- so a month that has been taught but not yet invoiced
        # still shows its line, sitting at zero, rather than the teacher
        # vanishing from the comparison entirely.
        active = {tid for tid, _, _ in money} | {tid for tid, _, _ in scheduled}
        rows = []
        for teacher in teachers:
            if teacher.id not in active:
                continue
            for month in range(1, up_to_month + 1):
                key = (teacher.id, year, month)
                bucket = money.get(key)
                rows.append(
                    {
                        "Teacher ID": teacher.id,
                        "Teacher": teacher.name,
                        "Year": year,
                        "Month": month,
                        "Month name": month_abbr[month],
                        "Invoiced": round(bucket["Earnings"], 2) if bucket else 0.0,
                        "Billed hours": round(bucket["Hours"], 2) if bucket else 0.0,
                        "Hours": round(scheduled.get(key, 0.0), 2),
                    }
                )
        return rows


def get_unpriced_classes_for_month(year, month):
    """Subjects taught in a month that have no rate covering their classes.

    "No rate" covers both a subject nobody has priced at all and one still
    sitting on the ``UNSET_RATE`` placeholder an import seeds -- the common
    case, and the one that quietly turns a $40,000 month into a $531 one.
    Worth catching on the way to sending the invoices rather than after a
    parent has received one.

    A subject counts as unpriced for whichever of its classes fall outside
    every real rate period, so a mid-month price change shows only the part
    still missing a price.
    """
    year, month = int(year), int(month)
    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])
    with SessionLocal() as session:
        rows = session.execute(
            select(
                ClassSession.class_id,
                ClassSession.session_date,
                ClassSession.start_time,
                ClassSession.end_time,
                AcademyClass.name,
                Teacher.id,
                Teacher.name,
            )
            .join(AcademyClass, AcademyClass.id == ClassSession.class_id)
            .join(Teacher, Teacher.id == ClassSession.teacher_id)
            .where(
                ClassSession.session_date >= first_day,
                ClassSession.session_date <= last_day,
            )
        ).all()
        if not rows:
            return []

        rate_index = _rate_index(session, {row[0] for row in rows})
        unpriced: dict[int, dict] = {}
        for class_id, when, start, end, class_name, teacher_id, teacher_name in rows:
            if _rate_lookup(rate_index, class_id, when) > UNSET_RATE:
                continue
            bucket = unpriced.get(class_id)
            if bucket is None:
                bucket = unpriced[class_id] = {
                    "Class ID": class_id,
                    "Class": class_name,
                    "Teacher ID": teacher_id,
                    "Teacher": teacher_name,
                    "Sessions": 0,
                    "Hours": 0.0,
                }
            bucket["Sessions"] += 1
            bucket["Hours"] += _span_hours(start, end)

        for bucket in unpriced.values():
            bucket["Hours"] = round(bucket["Hours"], 2)
        return sorted(unpriced.values(), key=lambda item: (item["Teacher"], item["Class"]))
