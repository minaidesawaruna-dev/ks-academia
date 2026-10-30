from pathlib import Path
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from calendar import month_abbr, monthrange
from typing import Any
import json
import os
import re
import threading

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    create_engine,
    delete,
    distinct,
    event,
    exists,
    extract,
    func,
    inspect,
    and_,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.orm import declarative_base, sessionmaker


PROJECT_FOLDER = Path(__file__).resolve().parent
DATABASE_FILE = PROJECT_FOLDER / "ks_academia.db"

# The price an imported subject gets until someone sets one. A price of $0 is
# refused, so an import can't leave it blank; it uses this instead, and anything
# still on it counts as unpriced. No class costs a dollar an hour.
UNSET_RATE = 1.0

# Where the data is kept. Locally it's the SQLite file next to the code. On the
# hosted app, DATABASE_URL points to Postgres, so every admin uses the same
# database. SQLAlchemy lets the rest of the app work the same on both.
#
# Some hosts give URLs starting "postgres://", which SQLAlchemy 2 won't load, so the
# prefix is changed to "postgresql://" here.
def _database_url() -> str:
    """Return the database URL from the environment or Streamlit secrets, else the local file.

    Streamlit Cloud keeps settings in its secrets store, and it isn't certain
    those reach the environment there. If they didn't, the app would quietly
    start on an empty SQLite file instead of the real database, so both places
    are checked.

    Streamlit is imported inside the function because migrate_to_postgres.py
    also imports this module, and it runs from a plain terminal without
    Streamlit or a secrets file.
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
    """Switch a SQLite engine to WAL mode.

    SQLite's default mode syncs the whole file to disk on every commit. An
    Excel import makes hundreds of small commits, so most of its time went
    on disk syncs: a full year's import took 10.7s before and 4.1s with this.
    WAL is the normal safe choice for one app writing at a time.

    It's a separate function so tests and scripts that build their own
    engine on a copy of the database run the same way as the app.
    """

    @event.listens_for(target_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        """Turn on WAL for each new SQLite connection."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return target_engine


if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL)
    apply_sqlite_pragmas(engine)
else:
    # Neon pauses the database after a few minutes with no queries, which breaks the
    # connections waiting in the pool. pool_pre_ping tests each connection before use,
    # and pool_recycle replaces it before that timeout, so the first click after a
    # break doesn't fail. The WAL settings are SQLite only, so they aren't used here.
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=280)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class Teacher(Base):
    """A teacher. Names are unique."""
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
    """A parent, with the phone number used to reach them."""
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
    """A student, linked to a parent if one is on file."""
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
    """A subject (stored as a class): its name, its teacher and its colour on the schedule.

    Names are unique across the whole academy, not just per teacher.
    """
    __tablename__ = "classes"

    id = Column(Integer, primary_key=True)
    name = Column(String(150), nullable=False, unique=True)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    status = Column(String(20), nullable=False, default="Ongoing")
    display_color = Column(String(7), nullable=False, default="#4F81BD")
    note = Column(String(1000), nullable=True)
    # Not used by the app any more; kept so older databases still load.
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
    )


class ClassSchedule(Base):
    """A weekly time slot for a subject, from an early version of the app.

    Nothing adds these now; deleting a subject removes any it has.
    """
    __tablename__ = "class_schedules"

    id = Column(Integer, primary_key=True)
    class_id = Column(Integer, ForeignKey("classes.id"), nullable=False)
    day_of_week = Column(String(10), nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)


class Enrolment(Base):
    """A student taking a subject. Each pair is stored once."""
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
    """One dated lesson of a subject, with its teacher, times and status."""
    __tablename__ = "class_sessions"
    __table_args__ = (
        UniqueConstraint(
            "class_id",
            "session_date",
            "start_time",
            name="uq_class_session_time",
        ),
        # The Schedule, Teachers and Data screens look lessons up by teacher and month.
        Index("ix_class_sessions_teacher_date", "teacher_id", "session_date"),
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
    """One student's place in one lesson: online, recording, cancelled, paid and a note."""

    __tablename__ = "session_attendance"
    __table_args__ = (
        UniqueConstraint("session_id", "student_id", name="uq_session_student"),
    )

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("class_sessions.id"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False, index=True)
    # One label for the flags below. Still written, for databases from before the flags.
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
    """A subject's price per hour from effective_from until effective_to (None: no end)."""
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

    An invoice stays Open and collects a line for every lesson the student is
    on. Issuing it fixes its number, date and contents; the student's next
    lesson then starts a new open invoice.
    """

    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False, index=True)
    status = Column(String(10), nullable=False, default="Open")   # Open | Issued | Void
    invoice_number = Column(Integer, nullable=True)
    issued_on = Column(Date, nullable=True)
    note = Column(String(500), nullable=True)
    # Payment is kept here, not in `status`, so "Issued" still means "billed". The
    # earnings figures and month summaries count Issued invoices, and a paid one must
    # still be counted. An invoice is paid when paid_on is set.
    paid_on = Column(Date, nullable=True)
    paid_amount = Column(Numeric(10, 2), nullable=True)
    payment_note = Column(String(500), nullable=True)
    created_at = Column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class InvoiceItem(Base):
    """One lesson on one invoice.

    While the invoice is open, the line points at the lesson, so a change to
    the lesson shows up on the draft. Issuing copies the hours, price and
    amount onto the line, so a sent invoice never changes afterwards.
    """

    __tablename__ = "invoice_items"
    __table_args__ = (
        UniqueConstraint("invoice_id", "session_id", name="uq_invoice_session"),
    )

    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False)
    # Can be empty so a sent line outlives its lesson. Its figures are already copied
    # onto it, and the lesson may be deleted later (a subject removed, or a lesson
    # dropped from a re-uploaded workbook). The link is cleared, so the sent invoice
    # still prints. A line on an open invoice is deleted with its lesson instead.
    session_id = Column(Integer, ForeignKey("class_sessions.id"), nullable=True, index=True)
    # Filled in when the invoice is issued, so the figures stop changing.
    class_name = Column(String(120), nullable=True)
    teacher_name = Column(String(120), nullable=True)
    # Saved next to the name, not instead of it. The name on a sent invoice must not
    # change, but reports still need to find the teacher after a rename. Matching by
    # name alone used to lose all of a teacher's past months when they were renamed.
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=True)
    session_date = Column(Date, nullable=True)
    hours = Column(Numeric(5, 2), nullable=True)
    hourly_rate = Column(Numeric(8, 2), nullable=True)
    amount = Column(Numeric(10, 2), nullable=True)


class InvoiceRevision(Base):
    """A sent invoice that someone went back to: replaced, or kept as sent.

    A sent invoice is never edited in place. Correcting one voids it (its
    number retired, what it said saved in `details`) and issues a
    replacement under a new number (`replaced_by_id`). Keeping one as sent,
    after the schedule changed, is saved the same way without a replacement,
    so it isn't flagged again.

    `details` is JSON: the old invoice's lines, totals and payment, plus
    `seen`, each lesson's hours, price and name on the calendar at the time.
    Later changes are measured against `seen`.
    """

    __tablename__ = "invoice_revisions"

    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False, index=True)
    replaced_by_id = Column(Integer, ForeignKey("invoices.id"), nullable=True, index=True)
    revised_on = Column(Date, nullable=False)
    details = Column(Text, nullable=False)
    note = Column(String(500), nullable=True)
    created_at = Column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class Credit(Base):
    """Money owed back to a student, to come off a future invoice.

    A lesson the student cancelled costs them nothing. If their invoice
    hasn't gone out yet, the lesson is just left off it. Once it has gone
    out, its figures are fixed, so a credit is saved here and taken off the
    next invoice, which is how the academy already handles this. A refund
    uses the same record, settled in cash instead.

    `class_name` and `session_date` are copied in, not looked up through
    `session_id`, so the credit still reads correctly after its lesson is
    removed from the schedule.
    """

    __tablename__ = "credits"
    __table_args__ = (
        # A cancelled lesson can only owe a student once, however many times the workbook
        # is imported. A credit added by hand has no lesson, and empty values don't count
        # as duplicates, so those aren't affected.
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
    """One row per teacher and month an Excel schedule has been imported for.

    Updated on every import, so the Students screen can show which teachers'
    workbooks for a month are in.
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


class LessonHistory(Base):
    """One student at one past lesson, kept only for the Teaching figures.

    A separate table, with names instead of links, on purpose. Importing a
    year that was billed outside the app into the schedule would put all
    those lessons on students' open invoices, one click from being billed
    again. Nothing that bills, schedules or sends reminders reads this table;
    only the Data screen's Teaching view does.
    """

    __tablename__ = "lesson_history"
    __table_args__ = (
        UniqueConstraint(
            "teacher_name", "lesson_date", "start_time", "student_name",
            name="uq_history_lesson_student",
        ),
    )

    id = Column(Integer, primary_key=True)
    teacher_name = Column(String(100), nullable=False)
    class_name = Column(String(150), nullable=False)
    lesson_date = Column(Date, nullable=False)
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)
    student_name = Column(String(100), nullable=False)
    status = Column(String(20), nullable=False)
    source = Column(String(200), nullable=True)
    created_at = Column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class StudentAlias(Base):
    """Another spelling a teacher uses for a student already on file.

    Each teacher writes a child's name their own way, every month. Once
    "Kim Hayun" has been merged into "Hayun Kim", or confirmed as the same
    child at an import, the next workbook saying "Kim Hayun" means that
    child, not a new student and not another question for the admin.
    """

    __tablename__ = "student_aliases"

    id = Column(Integer, primary_key=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    alias = Column(String(100), nullable=False, unique=True)
    created_at = Column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class SubjectSpelling(Base):
    """An old name of a subject, kept when the subject is renamed.

    The teacher's sheet keeps the name it always had. Once "G9 Englsh" is
    renamed "G9 English", the next upload saying "G9 Englsh" means that
    subject, not a new one.
    """

    __tablename__ = "subject_spellings"

    id = Column(Integer, primary_key=True)
    class_id = Column(Integer, ForeignKey("classes.id"), nullable=False)
    spelling = Column(String(150), nullable=False)
    created_at = Column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class DistinctStudents(Base):
    """Two students the admin said are different children, however alike their names.

    Stops the "on file twice?" list offering the pair again. Two Yunas in one
    teacher's classes can be two girls, and asking about them every week
    would hide the pairs that really are one child.
    """

    __tablename__ = "distinct_students"
    __table_args__ = (UniqueConstraint("first_id", "second_id", name="uq_distinct_pair"),)

    id = Column(Integer, primary_key=True)
    first_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    second_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    created_at = Column(
        DateTime, nullable=False, server_default=func.current_timestamp()
    )


class Setting(Base):
    """A setting the admin chose, saved by name.

    For now only the next invoice number, for when invoices were written
    outside the app and the app's numbers must follow on from them.
    """

    __tablename__ = "settings"

    key = Column(String(50), primary_key=True)
    value = Column(String(200), nullable=False)


def mark_students_distinct(first_id, second_id):
    """Save that two students are different children."""
    first_id, second_id = sorted((int(first_id), int(second_id)))
    with SessionLocal() as session:
        if not session.scalar(select(func.count()).select_from(DistinctStudents).where(
                DistinctStudents.first_id == first_id, DistinctStudents.second_id == second_id)):
            session.add(DistinctStudents(first_id=first_id, second_id=second_id))
            session.commit()
    return "marked"


def _alias_key(name: str) -> str:
    """Return a spelling the way aliases are stored: single spaces, case ignored."""
    return " ".join(str(name).split()).casefold()


def get_teacher_rosters(teacher_id) -> dict[int, set[int]]:
    """Return who has been in each of a teacher's subjects: {class_id: {student ids}}."""
    with SessionLocal() as session:
        rows = session.execute(
            select(ClassSession.class_id, SessionAttendance.student_id).distinct()
            .join(SessionAttendance, SessionAttendance.session_id == ClassSession.id)
            .where(ClassSession.teacher_id == int(teacher_id))
        ).all()
    rosters: dict[int, set[int]] = defaultdict(set)
    for class_id, student_id in rows:
        rosters[class_id].add(student_id)
    return dict(rosters)


def get_student_aliases() -> dict[str, int]:
    """Return every other spelling on file: {spelling in lower case: student id}."""
    with SessionLocal() as session:
        return {
            _alias_key(alias): student_id
            for alias, student_id in session.execute(
                select(StudentAlias.alias, StudentAlias.student_id)
            ).all()
        }


def _remember_alias(session, student_id, name, ignoring=()) -> bool:
    """Save `name` as another spelling of `student_id`, if it's worth saving.

    Not if it's the student's own name, or another student on file has
    exactly that name: then the spelling doesn't point to one child, and
    guessing would put lessons on the wrong one. `ignoring` is a student
    about to be removed, such as the one being merged away.
    """
    key = _alias_key(name)
    student = session.get(Student, student_id)
    if not key or student is None or _alias_key(student.full_name) == key:
        return False
    if session.scalar(
        select(func.count()).select_from(Student).where(
            Student.id != student_id, Student.id.not_in(list(ignoring)),
            func.lower(func.trim(Student.full_name)) == key,
        )
    ):
        return False
    existing = session.scalars(select(StudentAlias)).all()
    for row in existing:
        if _alias_key(row.alias) == key:
            row.student_id = student_id
            return True
    session.add(StudentAlias(student_id=student_id, alias=_fit(" ".join(str(name).split()),
                                                                StudentAlias.alias)))
    return True


def remember_student_aliases(pairs) -> int:
    """Save several (student_id, spelling) pairs at once. Returns how many were saved."""
    with SessionLocal() as session:
        added = sum(_remember_alias(session, student_id, name) for student_id, name in pairs)
        session.commit()
        return added


def _schema_is_current() -> bool:
    """Return True if every table and column the app needs is already there.

    initialise_database is quick on a local SQLite file but slow on remote
    Postgres: create_all and the column checks for each upgrade add up to
    over 1,500 queries. Next to the database that was six seconds; with the
    database and app in different regions it was minutes. Streamlit reruns
    the script on every click, so the hosted app looked frozen on every click.

    get_multi_columns reads the whole schema in three queries. If nothing is
    missing, there's nothing to create or add, and the slow path is skipped.

    It also checks which columns allow empty values, because one upgrade
    below rebuilds invoice_items while session_id still can't be empty.
    """
    try:
        inspector = inspect(engine)
        reflected = inspector.get_multi_columns()
        indexed = {
            index["name"] for indexes in inspector.get_multi_indexes().values() for index in indexes
        }
    except Exception:
        # This is only a shortcut. If the schema can't be read, take the slow, full path.
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
        if any(index.name not in indexed for index in table.indexes):
            return False
    return True


def initialise_database():
    """Create any missing tables and apply the upgrades older databases need.

    Returns straight away when the schema is already up to date, which on a
    remote database is the difference between a page that loads and one
    that seems frozen. See _schema_is_current.
    """
    if _schema_is_current():
        return

    Base.metadata.create_all(engine)

    # create_all() adds new tables but not new columns to old ones. Add the parent
    # link to students without losing any existing students.
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

    # Notes on students and subjects were added after the first databases.
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

    # An early version of the importer made an older form of this table. Add the
    # separate flags the schedule needs without losing any data.
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

        # Part-billing was replaced by a paid flag and notes. The old column can't be
        # empty, so leaving it would make every new row fail once the model stops filling it.
        if "billing_fraction" in attendance_columns:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE session_attendance "
                        "DROP COLUMN billing_fraction"
                    )
                )

    # invoice_items.session_id used to be required, so a lesson on a sent invoice
    # couldn't be deleted without leaving a broken link. SQLite can't change that in
    # place, so the table is rebuilt from the model and the rows copied over.
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

    # Invoice lines used to name their teacher only by the saved name, and reports
    # matched that against current names, so renaming a teacher lost their past
    # months. Add the teacher's id, filled from each line's lesson, or by name for
    # lines whose lesson has since been deleted.
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

    # Payment used to be tracked per lesson; now it's per invoice, which is what a
    # parent pays. Older databases get the new columns added, with no data lost.
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

    # A cancelled lesson is never charged. A database from before that rule can still
    # have charges for cancelled lessons, so they're fixed during the upgrade. (Only
    # then: an up-to-date schema returns at the top, and day to day sync_invoice_items
    # keeps cancellations off invoices.) It's one count query when there's nothing to
    # do, and running it twice changes nothing.
    if count_billed_cancellations():
        backfill_cancellation_credits()

    # create_all() makes indexes for new tables only. Without the index on
    # invoice_items.session_id, finding lessons still to bill scans every invoice line
    # for every student-lesson: eight minutes at five times this academy's size, and
    # a second and a half with the index.
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            index.create(engine, checkfirst=True)


def _fit(value, column):
    """Return the value cut to the length the column allows.

    Postgres refuses a value that's too long, where SQLite just stores it, so
    a 700-character class name from a spreadsheet would fail the whole import
    on the hosted app even though every local test passed. It's cut before
    the duplicate checks, so a name and its cut copy can't both be saved.
    """
    text = str(value or "").strip()
    limit = getattr(column.type, "length", None)
    return text[:limit] if limit else text


def create_teacher(name):
    """Add a teacher. Returns False if the name is empty or already taken."""
    cleaned_name = _fit(name, Teacher.name)
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
    """Return every teacher, sorted by name."""
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


def get_unpriced_subjects(class_ids=None):
    """Return each subject with a lesson that has no real price, and the months missing one.

    All months are checked, not just one: a price applies from a month
    onward, so pricing each subject from its first unpriced month covers the
    months after. Only lessons someone still has to be billed for count. A
    lesson already on every student's sent invoice kept the price it went out
    at; otherwise years of lessons billed before the app kept prices would
    all show as unpriced.
    """
    # The database checks which lessons are already on a sent invoice, so a month
    # that's fully billed returns nothing here, instead of fetching every lesson and
    # every invoice line just to throw them away.
    billed = (
        select(InvoiceItem.id)
        .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
        .where(
            Invoice.status == "Issued",
            InvoiceItem.session_id == ClassSession.id,
            Invoice.student_id == SessionAttendance.student_id,
        )
        .exists()
    )
    with SessionLocal() as session:
        rows = session.execute(
            select(
                ClassSession.id,
                ClassSession.class_id,
                ClassSession.session_date,
                AcademyClass.name,
                Teacher.id,
                Teacher.name,
            )
            .join(AcademyClass, ClassSession.class_id == AcademyClass.id)
            .join(Teacher, ClassSession.teacher_id == Teacher.id)
            .join(SessionAttendance, SessionAttendance.session_id == ClassSession.id)
            .where(
                ClassSession.status != "Cancelled",
                SessionAttendance.is_cancelled.is_(False),
                ~billed,
                *([ClassSession.class_id.in_([int(c) for c in class_ids])]
                  if class_ids is not None else []),
            )
            .distinct()
        ).all()
        rates = _rate_index(session, {row[1] for row in rows})
        subjects: dict[int, dict[str, Any]] = {}
        counted: set[int] = set()
        for session_id, class_id, when, name, teacher_id, teacher in rows:
            when = as_date(when)
            if session_id in counted:
                continue
            if _rate_lookup(rates, class_id, when) > UNSET_RATE:
                continue
            counted.add(session_id)
            item = subjects.setdefault(class_id, {
                "Class ID": class_id, "Class": name, "Teacher ID": teacher_id,
                "Teacher": teacher, "Sessions": 0, "Months": set(),
            })
            item["Sessions"] += 1
            item["Months"].add((when.year, when.month))
    for item in subjects.values():
        item["Months"] = sorted(item["Months"])
    return sorted(subjects.values(), key=lambda item: (item["Teacher"].casefold(), item["Class"].casefold()))


def get_subject_student_grades(class_ids):
    """Return each student's grade on each subject, taken from their other lessons.

    "LC Advanced A" doesn't say who it's for, but its students also take
    "G9 Add math" and "G10 English", and the price depends on the grade. Each
    student gets the grade of their most recent lesson that names one, since
    grades go up each school year. Students with no graded lesson are left out.
    """
    ids = [int(value) for value in class_ids]
    if not ids:
        return {}
    with SessionLocal() as session:
        on_subject = session.execute(
            select(ClassSession.class_id, SessionAttendance.student_id)
            .join(SessionAttendance, SessionAttendance.session_id == ClassSession.id)
            .where(ClassSession.class_id.in_(ids))
            .distinct()
        ).all()
        students = {student_id for _, student_id in on_subject}
        if not students:
            return {class_id: [] for class_id in ids}
        lessons = session.execute(
            select(SessionAttendance.student_id, AcademyClass.name, ClassSession.session_date)
            .join(ClassSession, SessionAttendance.session_id == ClassSession.id)
            .join(AcademyClass, ClassSession.class_id == AcademyClass.id)
            .where(SessionAttendance.student_id.in_(students))
        ).all()
    latest = _latest_grades(lessons)
    grades: dict[int, list[int]] = {class_id: [] for class_id in ids}
    for class_id, student_id in on_subject:
        if student_id in latest:
            grades[class_id].append(latest[student_id])
    return grades


def _latest_grades(lessons) -> dict[int, int]:
    """Return each student's grade, from their latest lesson whose name gives one.

    `lessons` are (student id, class name, date) rows.
    """
    from schedule_parser import grade_of

    latest: dict[int, tuple[date, int]] = {}
    for student_id, class_name, when in lessons:
        grade = grade_of(class_name)
        if grade and (student_id not in latest or as_date(when) > latest[student_id][0]):
            latest[student_id] = (as_date(when), grade)
    return {student_id: grade for student_id, (_, grade) in latest.items()}


def get_teacher_session_counts(year, month):
    """Return how many lessons each teacher has in a month, by teacher id."""
    first_day = date(int(year), int(month), 1)
    last_day = date(int(year), int(month), monthrange(int(year), int(month))[1])
    with SessionLocal() as session:
        return {
            teacher_id: int(count)
            for teacher_id, count in session.execute(
                select(ClassSession.teacher_id, func.count(ClassSession.id))
                .where(ClassSession.session_date >= first_day,
                       ClassSession.session_date <= last_day)
                .group_by(ClassSession.teacher_id)
            ).all()
        }


def update_teacher_status(teacher_id, is_active):
    """Mark a teacher active or inactive. Returns False if the teacher isn't found."""
    with SessionLocal() as session:
        teacher = session.get(Teacher, teacher_id)
        if teacher is None:
            return False
        teacher.is_active = is_active
        session.commit()
        return True


def update_teacher_name(teacher_id, new_name):
    """Rename a teacher. Returns "updated", "invalid", "not_found" or "duplicate"."""
    cleaned_name = _fit(new_name, Teacher.name)
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
    """Return the matching parent, or add one in the current transaction."""

    cleaned_name = _fit(name, Parent.name)
    cleaned_phone = _fit(phone, Parent.phone)
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


def create_student(full_name, parent_name="", parent_phone=""):
    """Add a student, with a parent if both a name and a phone number are given.

    The parent is optional (an import only knows the child's name, and it can
    be filled in later), but half a parent is refused, since nobody could
    call them.
    """
    cleaned_name = _fit(full_name, Student.full_name)
    if not cleaned_name:
        return "invalid"
    if bool((parent_name or "").strip()) != bool((parent_phone or "").strip()):
        return "parent_unavailable"

    with SessionLocal() as session:
        duplicate = session.scalar(
            select(Student).where(
                func.lower(Student.full_name) == cleaned_name.lower()
            )
        )
        if duplicate:
            return "duplicate"

        parent = None
        if (parent_name or "").strip():
            parent = _get_or_create_parent(session, parent_name, parent_phone)
            if parent is None:
                return "parent_unavailable"

        session.add(Student(full_name=cleaned_name, parent_id=parent.id if parent else None))
        session.commit()
        return "created"


def get_student_id_by_name(full_name):
    """Return one student's id by name, ignoring capitals, in a single query.

    Used just after adding a student (e.g. during an import), so the caller
    doesn't reload the whole students table to find one new id. Reloading per
    row made imports slower as the total number of students grew.
    """
    with SessionLocal() as session:
        return session.scalar(
            select(Student.id).where(
                func.lower(Student.full_name) == full_name.strip().lower()
            )
        )


def get_all_students():
    """Return every student with their parent's name and phone, sorted by name."""
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
                # Empty, not a "Not assigned" placeholder: this also fills edit boxes and printed
                # invoices, where a placeholder could look like a real parent's name. Each screen
                # says "no parent on file" in its own way.
                "Parent": parent.name if parent else "",
                "Phone": parent.phone if parent else "",
                "Active": student.is_active,
                "Created": student.created_at,
            }
            for student, parent in rows
        ]


def update_student(student_id, new_name, parent_name, parent_phone):
    """Rename a student and set their parent.

    Returns "updated", "invalid", "not_found", "duplicate" or "parent_unavailable".
    """
    cleaned_name = _fit(new_name, Student.full_name)
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

        # The parent is optional (students added by an import have none), so leaving
        # both boxes empty means "no parent on file", not an error. Refusing used to block
        # renaming any student without a parent, which is most of them. Half a parent
        # (a name without a number, or the other way round) is still refused.
        wants_parent = bool(parent_name.strip()) or bool(parent_phone.strip())
        parent = _get_or_create_parent(session, parent_name, parent_phone)
        if wants_parent and parent is None:
            return "parent_unavailable"

        student.full_name = cleaned_name
        student.parent_id = parent.id if parent else None
        session.commit()
        return "updated"


def get_class_id_by_name(name):
    """Return one subject's id by name, ignoring capitals, in a single query.

    Same reason as get_student_id_by_name: finds a new subject's id without
    reloading every subject in the academy.
    """
    with SessionLocal() as session:
        return session.scalar(
            select(AcademyClass.id).where(
                func.lower(AcademyClass.name) == name.strip().lower()
            )
        )


def get_all_classes():
    """Return every subject with the teacher it belongs to.

    Only the columns callers use. It used to join the weekly slots and count
    enrolments for columns nothing showed, on a function several screens
    call on every page load.
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
    """Return the ids of the students enrolled in a subject."""
    with SessionLocal() as session:
        return list(
            session.scalars(
                select(Enrolment.student_id).where(
                    Enrolment.class_id == class_id
                )
            ).all()
        )


def _remove_lesson(session, lesson, reason, rate_index):
    """Take one lesson off the schedule and sort out what it was billed.

    A charge on an open invoice is dropped. A charge on a sent invoice is
    owed back as a credit, instead of changing a bill the parent already has.
    The sent line keeps its figures and loses only its link to the lesson,
    and so do credits that pointed at it. Returns (credits raised, open
    charges dropped).
    """
    credited = dropped = 0
    for item in session.scalars(
        select(InvoiceItem).where(InvoiceItem.session_id == lesson.id)
    ).all():
        invoice = session.get(Invoice, item.invoice_id)
        if invoice is not None and invoice.status == "Issued":
            credit, raised = _raise_credit(session, invoice.student_id, lesson, reason, rate_index)
            if credit is not None:
                credit.session_id = None  # the lesson is about to go
            item.session_id = None  # and so is the line's link to it
            credited += int(raised)
        else:
            session.delete(item)
            dropped += invoice is not None
    for attendance in session.scalars(
        select(SessionAttendance).where(SessionAttendance.session_id == lesson.id)
    ).all():
        session.delete(attendance)
    for credit in session.scalars(select(Credit).where(Credit.session_id == lesson.id)).all():
        credit.session_id = None
    session.delete(lesson)
    return credited, dropped


def get_all_class_rates():
    """Return every price period, sorted by subject name, newest first."""
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
    """Set a subject's price from one calendar month onward, in one transaction.

    Simply overwriting the period that covers the month would change past
    months too, since a subject usually has one open-ended period from when
    it started. Instead this:

    * ends any earlier period the day before this month, so earlier months
      keep the price they were billed at;
    * removes any period starting inside this month (an import dates its
      placeholder to the first lesson, which can be any day), so the new
      price covers the whole month;
    * ends the new period before any later price change, so they don't overlap.

    It's all one transaction. The screen used to do these steps as separate
    calls, deleting the old periods before adding the new one, so if adding
    failed the subject was left with no price at all and billed at nothing.
    """
    if hourly_rate is None or float(hourly_rate) <= 0:
        return "invalid"
    with SessionLocal() as session:
        if session.get(AcademyClass, class_id) is None:
            return "class_unavailable"
        outcome = _apply_month_rate(session, class_id, month_start, hourly_rate)
        session.commit()
        return outcome


def _apply_month_rate(session, class_id, month_start, hourly_rate, periods=None):
    """Apply the month-price rule inside the caller's transaction.

    Shared so the Teachers screen and the Schedule's subject editor price a
    subject the same way, by calendar month. The editor used to start the
    new price on the lesson being edited, which split a month and could
    leave two open-ended periods overlapping, so the new price was ignored.

    The caller commits. `periods` is the subject's price periods if the
    caller already loaded them; it's kept up to date as they change.
    """
    month_start = date(month_start.year, month_start.month, 1)
    month_end = date(
        month_start.year,
        month_start.month,
        monthrange(month_start.year, month_start.month)[1],
    )
    known = periods
    periods = sorted(
        known if known is not None else session.scalars(
            select(ClassRate).where(ClassRate.class_id == class_id)
        ).all(),
        key=lambda rate: rate.effective_from,
    )

    exact = next(
        (rate for rate in periods if rate.effective_from == month_start), None
    )
    if exact is not None:
        exact.hourly_rate = hourly_rate
        # Any other period starting inside the month is a leftover that would override
        # this price part-way through the month.
        for stray in periods:
            if stray is not exact and month_start < stray.effective_from <= month_end:
                session.delete(stray)
                if known is not None:
                    known.remove(stray)
        return "updated"

    later = next(
        (rate for rate in periods if rate.effective_from > month_end), None
    )
    for rate in periods:
        if rate.effective_from < month_start:
            # Covers this month or runs past it: end it the day before, so every earlier
            # month keeps the price it was billed at.
            if rate.effective_to is None or rate.effective_to >= month_start:
                rate.effective_to = month_start - timedelta(days=1)
        elif rate.effective_from <= month_end:
            session.delete(rate)
            if known is not None:
                known.remove(rate)

    added = ClassRate(
        class_id=class_id,
        hourly_rate=hourly_rate,
        effective_from=month_start,
        effective_to=(
            later.effective_from - timedelta(days=1) if later else None
        ),
    )
    session.add(added)
    if known is not None:
        known.append(added)
    return "created"


def set_class_rates_for_months(assignments):
    """Set prices for many subjects at once, [(class_id, month_start, rate)], all or nothing.

    Done one by one, each price took several database round trips, so
    pricing forty subjects took most of a minute, and clicking anything in
    the meantime stopped it half-way: some subjects priced, the rest not.
    Here all the periods are read in one query and saved in one commit.
    Returns how many were priced.
    """
    items = [(int(class_id), month_start, float(rate))
             for class_id, month_start, rate in assignments
             if rate is not None and float(rate) > 0]
    if not items:
        return 0
    with SessionLocal() as session:
        periods: dict[int, list[ClassRate]] = defaultdict(list)
        for rate in session.scalars(
            select(ClassRate).where(ClassRate.class_id.in_({item[0] for item in items}))
        ).all():
            periods[rate.class_id].append(rate)
        for class_id, month_start, rate in items:
            _apply_month_rate(session, class_id, month_start, rate, periods=periods[class_id])
        session.commit()
    return len({item[0] for item in items})


def get_class_rates_for_date(class_ids, on_date):
    """Return the price in force on one date for many subjects, by class id.

    One query for the whole list, for screens that show every subject with
    its current price. A subject with no price on that date is left out.
    """
    class_ids = list(class_ids)
    if not class_ids:
        return {}
    with SessionLocal() as session:
        index = _rate_index(session, class_ids)
        found = {}
        for class_id in class_ids:
            rate = _rate_lookup(index, class_id, on_date)
            # _rate_lookup gives 0.0 for "no price"; leaving the key out keeps that apart
            # from a real $0.
            if rate:
                found[class_id] = rate
        return found


# ---------------------------------------------------------------------------
# Scheduling workspace
# ---------------------------------------------------------------------------


def _valid_colour(value):
    """Return True for a colour written as #RRGGBB."""
    return bool(re.fullmatch(r"#[0-9A-Fa-f]{6}", (value or "").strip()))


def _attendance_status(row):
    """Return a student's flags as one label, e.g. "Online + Recording", or "Attending"."""
    statuses = []
    if row.get("is_online"):
        statuses.append("Online")
    if row.get("has_recording"):
        statuses.append("Recording")
    if row.get("is_cancelled"):
        statuses.append("Cancelled")
    return " + ".join(statuses) if statuses else "Attending"


def get_teacher_classes_for_schedule(teacher_id, on_date):
    """Return a teacher's subjects with their current price and students.

    Three queries however many subjects (the subjects, their price periods,
    their enrolments) instead of two per subject. The Schedule screen calls
    this on every page load, and a teacher with a dozen subjects used to
    cost five seconds on the live app.
    """

    with SessionLocal() as session:
        classes = session.scalars(
            select(AcademyClass)
            .where(
                AcademyClass.teacher_id == teacher_id,
                AcademyClass.status == "Ongoing",
            )
            .order_by(AcademyClass.name)
        ).all()
        ids = [academy_class.id for academy_class in classes]
        # The period in force on the day; if two overlap, the one that started latest.
        rate_on: dict[int, ClassRate] = {}
        for rate in session.scalars(
            select(ClassRate).where(
                ClassRate.class_id.in_(ids),
                ClassRate.effective_from <= on_date,
                ClassRate.effective_to.is_(None) | (ClassRate.effective_to >= on_date),
            )
        ).all() if ids else []:
            current = rate_on.get(rate.class_id)
            if current is None or rate.effective_from > current.effective_from:
                rate_on[rate.class_id] = rate
        roster: dict[int, list[Student]] = defaultdict(list)
        for class_id, student in session.execute(
            select(Enrolment.class_id, Student)
            .join(Student, Enrolment.student_id == Student.id)
            .where(Enrolment.class_id.in_(ids), Student.is_active.is_(True))
            .order_by(Student.full_name)
        ).all() if ids else []:
            roster[class_id].append(student)
        results = []
        for academy_class in classes:
            rate = rate_on.get(academy_class.id)
            students = roster[academy_class.id]
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
    """Update a subject and start a new price period if the price changed."""

    cleaned_name = _fit(name, AcademyClass.name)
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

        _apply_subject_name(session, academy_class, cleaned_name)
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

        # Priced by calendar month, the same as the Teachers screen. Starting the change
        # on the lesson being edited split the month and could leave two periods
        # overlapping, with the later one overriding the price just typed.
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
    """Return True if the teacher already has a lesson overlapping this time on that date."""
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
    # Every save rewrites the roster, so each student's place in the order they were
    # added is carried over. Places in a class are sometimes first come, first served.
    """Replace a lesson's roster with `attendance_rows`, inside the caller's transaction.

    Students keep their place in the order; new ones go at the end. Inactive
    students are skipped.
    """
    added, place = {}, {}
    for position, old_row in enumerate(session.scalars(
        select(SessionAttendance)
        .where(SessionAttendance.session_id == session_id)
        .order_by(SessionAttendance.created_at, SessionAttendance.id)
    ).all()):
        added[old_row.student_id] = old_row.created_at
        place[old_row.student_id] = position
        session.delete(old_row)

    # One row per student, the first one given: two rows for the same child would
    # break the table's unique rule and fail the whole save.
    first_rows, seen = [], set()
    for row in attendance_rows:
        if row["student_id"] not in seen:
            seen.add(row["student_id"])
            first_rows.append(row)
    # Write them back in that order, newcomers last in the order given. Two added in
    # the same second are only told apart by the order they're written.
    attendance_rows = sorted(first_rows, key=lambda row: place.get(row["student_id"], len(place)))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
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
            # A newcomer's time is set here, not by the database default, which SQLite writes
            # in a format that sorts before times carried over from the same second.
            created_at=added.get(row["student_id"]) or now,
        )
        for row in attendance_rows
        if row["student_id"] in valid_ids
    )


def _valid_lesson(start_time, end_time, status):
    """Return True if the lesson ends after it starts and has one of the three statuses."""
    return start_time < end_time and status in {"Scheduled", "Completed", "Cancelled"}


def _insert_lesson(session, class_id, teacher_id, session_date, start_time, end_time, status, note,
                   attendance_rows):
    """Save one lesson and its roster inside the caller's transaction. Returns its id."""
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
    _replace_session_attendance(session, class_session.id, attendance_rows)
    return class_session.id


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
    """Create a subject and its first lesson in one transaction."""

    cleaned_name = _fit(name, AcademyClass.name)
    if (
        not cleaned_name
        or hourly_rate <= 0
        or not _valid_colour(display_color)
        or not _valid_lesson(start_time, end_time, status)
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
        # From the 1st of the month, like every other price. A price starting on the
        # first lesson's date read as "no price" to anything checking the month's price.
        session.add(
            ClassRate(
                class_id=academy_class.id,
                hourly_rate=hourly_rate,
                effective_from=session_date.replace(day=1),
            )
        )

        new_id = _insert_lesson(session, academy_class.id, teacher_id, session_date, start_time,
                                end_time, status, note, attendance_rows)
        session.commit()
    # After the transaction: put every student on the lesson onto their open invoice.
    sync_invoice_items(new_id)
    return "created"


def create_schedule_session(
    teacher_id,
    class_id,
    session_date,
    start_time,
    end_time,
    status,
    note,
    attendance_rows,
):
    """Create a lesson and each student's flags in one transaction."""

    if not _valid_lesson(start_time, end_time, status):
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

        new_id = _insert_lesson(session, class_id, teacher_id, session_date, start_time, end_time,
                                status, note, attendance_rows)
        session.commit()
    sync_invoice_items(new_id)
    return "created"


def get_month_schedule(teacher_id, year, month):
    """Return the schedule cards for one teacher and month."""

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

        # Every lesson's students in one query, grouped by lesson, instead of one query
        # per lesson. This feeds the month grid, so it would get slower in busy months.
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
    """Return every lesson's roster for one teacher and month, by lesson id.

    The month grid shows each student and their flags on the card. Calling
    get_schedule_session per lesson would be three queries each on every
    load of the Schedule screen; this is one query.
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
            .order_by(SessionAttendance.created_at, SessionAttendance.id)
        ).all()

        by_session: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for attendance, student, session_id in rows:
            by_session[session_id].append(_attendance_entry(attendance, student))
        return dict(by_session)


def _attendance_entry(attendance, student):
    """Return one student's place in a lesson, as the schedule screens edit it."""
    return {
        "student_id": student.id,
        "student_name": student.full_name,
        "is_online": bool(attendance.is_online),
        "has_recording": bool(attendance.has_recording),
        "is_cancelled": bool(attendance.is_cancelled),
        "is_paid": bool(attendance.is_paid),
        "note": attendance.note or "",
    }


def get_schedule_session(session_id):
    """Return one lesson with its students and their flags, for editing."""

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
            # In the order they were added: places are sometimes first come, first served.
            .order_by(SessionAttendance.created_at, SessionAttendance.id)
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
                _attendance_entry(attendance, student) for attendance, student in attendance_rows
            ],
        }


def update_schedule_session(
    session_id,
    session_date,
    start_time,
    end_time,
    status,
    note,
    attendance_rows,
):
    """Save changes to one lesson: date, times, status, note and roster.

    Returns "updated", "invalid", "not_found" or "teacher_conflict". Afterwards
    the students' open invoices are brought up to date.
    """
    if not _valid_lesson(start_time, end_time, status):
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


def _clash_entry(lesson, academy_class, teacher, student) -> dict:
    """Return one side of a clash as a row for the clash table."""
    return {
        "Student": student.full_name, "Student ID": student.id, "Date": lesson.session_date,
        "Class": academy_class.name, "Teacher": teacher.name if teacher else "",
        "Start": lesson.start_time, "End": lesson.end_time, "Session ID": lesson.id,
    }


def _bookings(session, student_ids=None, dates=None, start_time=None, end_time=None,
              excluded_session_id=None, between=None):
    """Return the lessons students are really booked into.

    Leaves out cancelled lessons and students marked cancelled: they won't
    be there, so it can't be a clash.
    """
    query = (
        select(ClassSession, AcademyClass, Teacher, Student)
        .select_from(SessionAttendance)
        .join(ClassSession, SessionAttendance.session_id == ClassSession.id)
        .join(AcademyClass, ClassSession.class_id == AcademyClass.id)
        .outerjoin(Teacher, ClassSession.teacher_id == Teacher.id)
        .join(Student, SessionAttendance.student_id == Student.id)
        .where(SessionAttendance.is_cancelled.is_(False), ClassSession.status != "Cancelled")
    )
    if student_ids is not None:
        query = query.where(SessionAttendance.student_id.in_(student_ids))
    if dates is not None:
        query = query.where(ClassSession.session_date.in_(dates))
    if start_time is not None and end_time is not None:
        query = query.where(ClassSession.start_time < end_time, ClassSession.end_time > start_time)
    if excluded_session_id is not None:
        query = query.where(ClassSession.id != excluded_session_id)
    if between is not None:
        query = query.where(ClassSession.session_date.between(*between))
    return session.execute(
        query.order_by(ClassSession.session_date, Student.full_name, ClassSession.start_time)
    ).all()


def find_student_clashes(student_ids, dates, start_time, end_time, excluded_session_id=None) -> list[dict]:
    """Return the other lessons these students have at an overlapping time on these dates.

    Any teacher's lessons count: a student taking three or four subjects
    would otherwise be checked by hand, one class at a time.
    `excluded_session_id` is the lesson being edited, which can't clash with
    itself.
    """
    if not student_ids or not dates or start_time is None or end_time is None:
        return []
    with SessionLocal() as session:
        return [_clash_entry(*row) for row in _bookings(
            session, list(student_ids), list(dates), start_time, end_time, excluded_session_id)]


def get_student_bookings(student_ids, first_day, last_day) -> list[dict]:
    """Return every lesson these students are really booked into between two dates, in one query.

    For checking a whole planned month at once; asking lesson by lesson
    would be a database round trip each.
    """
    if not student_ids:
        return []
    with SessionLocal() as session:
        return [_clash_entry(*row) for row in _bookings(
            session, list(student_ids), between=(first_day, last_day))]


def get_session_clashes(session_id) -> list[dict]:
    """Return the other lessons this lesson's students have at the same time."""
    with SessionLocal() as session:
        lesson = session.get(ClassSession, session_id)
        if lesson is None or lesson.status == "Cancelled":
            return []
        here = session.scalars(select(SessionAttendance.student_id).where(
            SessionAttendance.session_id == session_id, SessionAttendance.is_cancelled.is_(False))).all()
        if not here:
            return []
        return [_clash_entry(*row) for row in _bookings(
            session, here, [lesson.session_date], lesson.start_time, lesson.end_time, session_id)]


def get_month_student_clashes(year, month) -> list[dict]:
    """Return every student booked into two lessons at once this month, across all teachers.

    [{"Student", "Date", "First": clash entry, "Second": clash entry}].
    """
    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])
    with SessionLocal() as session:
        by_day = defaultdict(list)
        for row in _bookings(session, between=(first_day, last_day)):
            entry = _clash_entry(*row)
            by_day[(entry["Student ID"], entry["Date"])].append(entry)
    found = []
    for (_, day), entries in sorted(by_day.items(), key=lambda item: (item[0][1], item[1][0]["Student"])):
        entries.sort(key=lambda entry: entry["Start"])
        for index, first in enumerate(entries):
            for second in entries[index + 1:]:
                if second["Start"] < first["End"] and second["End"] > first["Start"]:
                    found.append({"Student": first["Student"], "Date": day, "First": first, "Second": second})
    return found


def set_student_note(student_id, note):
    """Save a free-text note on a student."""

    with SessionLocal() as session:
        student = session.get(Student, student_id)
        if student is None:
            return "missing"
        student.note = (note or "").strip()[:1000] or None
        session.commit()
        return "updated"


def set_subject_note(class_id, note):
    """Save a free-text note on a subject."""

    with SessionLocal() as session:
        academy_class = session.get(AcademyClass, class_id)
        if academy_class is None:
            return "missing"
        academy_class.note = (note or "").strip()[:1000] or None
        session.commit()
        return "updated"


def merge_students(keep_id, drop_id, dry_run=False):
    """Merge a second record of the same child into the first.

    Teachers spell a child two ways ("Kim Hayun" and "Hayun Kim", "Kwak" and
    "Gwak"), and a workbook imported before anyone noticed leaves the child on
    file twice: their months split over two records, two invoices instead of
    one, and two students in the Data screen's counts.

    Everything on the removed record moves at once: lessons, subjects,
    invoices (sent ones keep their numbers and figures, only the student
    changes), credits, and the past schedules under the removed spelling.
    Lessons and invoices must move together, because credits are worked out
    by comparing who was in a lesson with who was invoiced for it. The child
    ends up with one open invoice, where every lesson still to bill goes.
    A parent or note is copied over if the kept record has none.

    Refused if the two were ever in the same lesson: one child can't sit a
    lesson twice, so they're two children. `dry_run` works it all out
    without changing anything, to show what a merge would do first.
    """
    if keep_id == drop_id:
        return {"status": "same"}
    with SessionLocal() as session:
        keep, drop = session.get(Student, keep_id), session.get(Student, drop_id)
        if keep is None or drop is None:
            return {"status": "missing"}

        kept_lessons = set(session.scalars(
            select(SessionAttendance.session_id).where(SessionAttendance.student_id == keep_id)
        ).all())
        moving = session.scalars(
            select(SessionAttendance).where(SessionAttendance.student_id == drop_id)
        ).all()
        shared = sorted({row.session_id for row in moving} & kept_lessons)
        if shared:
            dates = session.scalars(
                select(ClassSession.session_date).where(ClassSession.id.in_(shared))
            ).all()
            return {"status": "share_a_lesson", "dates": sorted(as_date(d) for d in dates)}
        for row in moving:
            row.student_id = keep_id

        kept_subjects = set(session.scalars(
            select(Enrolment.class_id).where(Enrolment.student_id == keep_id)
        ).all())
        subjects = 0
        for row in session.scalars(select(Enrolment).where(Enrolment.student_id == drop_id)).all():
            if row.class_id in kept_subjects:
                session.delete(row)
            else:
                row.student_id = keep_id
                subjects += 1

        invoices = session.scalars(
            select(Invoice).where(Invoice.student_id.in_([keep_id, drop_id]))
            .order_by(Invoice.student_id != keep_id, Invoice.id)
        ).all()
        issued = [invoice for invoice in invoices
                  if invoice.status in ("Issued", "Void") and invoice.student_id == drop_id]
        for invoice in issued:
            invoice.student_id = keep_id
        # One open invoice: the kept record's if it has one, with every lesson still to
        # bill on it.
        open_ones = [invoice for invoice in invoices if invoice.status == "Open"]
        open_items = 0
        if open_ones:
            target, others = open_ones[0], open_ones[1:]
            target.student_id = keep_id
            for other in others:
                for item in session.scalars(
                    select(InvoiceItem).where(InvoiceItem.invoice_id == other.id)
                ).all():
                    item.invoice_id = target.id
                    open_items += 1
            session.flush()
            for other in others:
                session.delete(other)

        credits = session.scalars(select(Credit).where(Credit.student_id == drop_id)).all()
        for credit in credits:
            credit.student_id = keep_id

        # The removed spelling, and any spellings the removed record already had, now
        # mean the kept child, so next month's workbook that still says "Kim Hayun" finds
        # them, and so do the past schedules.
        spellings = [drop.full_name] + [
            row.alias for row in session.scalars(
                select(StudentAlias).where(StudentAlias.student_id == drop_id)
            ).all()
        ]
        for row in session.scalars(select(StudentAlias).where(StudentAlias.student_id == drop_id)).all():
            session.delete(row)
        session.flush()
        remembered = [name for name in spellings
                      if _remember_alias(session, keep_id, name, ignoring=(drop_id,))]

        if keep.parent_id is None and drop.parent_id is not None:
            keep.parent_id = drop.parent_id
        notes = [note.strip() for note in (keep.note, drop.note) if note and note.strip()]
        if len(notes) == 2 and notes[0] != notes[1]:
            keep.note = _fit(" · ".join(notes), Student.note)
        elif notes:
            keep.note = notes[0]
        keep.is_active = bool(keep.is_active or drop.is_active)

        for row in session.scalars(select(DistinctStudents).where(
                (DistinctStudents.first_id == drop_id) | (DistinctStudents.second_id == drop_id))).all():
            session.delete(row)

        summary = {
            "status": "merged", "kept": keep.full_name, "dropped": drop.full_name,
            "lessons": len(moving), "subjects": subjects, "invoices": len(issued),
            "open_items": open_items, "credits": len(credits), "spellings": remembered,
        }
        session.flush()
        session.delete(drop)
        if dry_run:
            session.rollback()
            summary["status"] = "would_merge"
        else:
            session.commit()
        return summary


def get_unnamed_subjects(year=None, month=None):
    """Return the subjects a workbook gave no name, with enough detail to tell them apart.

    A class cell with students and a time but no subject line comes in as
    "(unnamed class) · Sun 13:30", and that's what the invoices say. Its
    teacher, times and students let the admin name it. With `year` and
    `month`, only those with a lesson that month.
    """
    from schedule_parser import UNNAMED_CLASS

    with SessionLocal() as session:
        classes = session.execute(
            select(AcademyClass.id, AcademyClass.name, Teacher.name)
            .join(Teacher, Teacher.id == AcademyClass.teacher_id)
            .where(AcademyClass.name.like(UNNAMED_CLASS + "%"))
            .order_by(Teacher.name, AcademyClass.name)
        ).all()
        if not classes:
            return []
        ids = [class_id for class_id, _, _ in classes]
        lessons = session.execute(
            select(ClassSession.class_id, ClassSession.session_date)
            .where(ClassSession.class_id.in_(ids))
        ).all()
        roster = session.execute(
            select(ClassSession.class_id, Student.full_name).distinct()
            .join(SessionAttendance, SessionAttendance.session_id == ClassSession.id)
            .join(Student, Student.id == SessionAttendance.student_id)
            .where(ClassSession.class_id.in_(ids))
        ).all()
        issued = dict(session.execute(
            select(ClassSession.class_id, func.count(InvoiceItem.id))
            .join(ClassSession, ClassSession.id == InvoiceItem.session_id)
            .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
            .where(Invoice.status == "Issued", ClassSession.class_id.in_(ids))
            .group_by(ClassSession.class_id)
        ).all())
    dates: dict[int, list] = defaultdict(list)
    for class_id, when in lessons:
        dates[class_id].append(as_date(when))
    students: dict[int, set] = defaultdict(set)
    for class_id, name in roster:
        students[class_id].add(name)
    found = []
    for class_id, name, teacher in classes:
        days = sorted(dates[class_id])
        if year and month and not any((d.year, d.month) == (year, month) for d in days):
            continue
        found.append({
            "Class ID": class_id, "Class": name, "Teacher": teacher,
            "Lessons": len(days), "First": days[0] if days else None,
            "Last": days[-1] if days else None,
            "Students": sorted(students[class_id], key=str.casefold),
            "Issued lines": int(issued.get(class_id, 0)),
        })
    return found


def name_subjects(names):
    """Name unnamed subjects from {class_id: name}. Returns (result, name) for each.

    Sent invoice lines for them said "(unnamed class) · Sun 13:30"; they get
    the new name too, so the next copy a parent gets reads properly. Only
    that label changes, never an amount, date or price. A name another
    teacher's subject already has gets this teacher's name added, the same
    way imports tell such subjects apart.
    """
    from schedule_parser import UNNAMED_CLASS

    outcome: dict[int, tuple[str, str | None]] = {}
    relabelled = 0
    with SessionLocal() as session:
        taken = {name.casefold(): class_id for class_id, name in
                 session.execute(select(AcademyClass.id, AcademyClass.name)).all()}
        for class_id, typed in names.items():
            academy_class = session.get(AcademyClass, int(class_id))
            cleaned = " ".join(str(typed or "").split())
            if academy_class is None:
                outcome[class_id] = ("not_found", None)
                continue
            if not cleaned or cleaned.startswith(UNNAMED_CLASS):
                outcome[class_id] = ("invalid", None)
                continue
            if taken.get(cleaned.casefold(), academy_class.id) != academy_class.id:
                teacher = session.get(Teacher, academy_class.teacher_id)
                cleaned = f"{cleaned} ({teacher.name})" if teacher else cleaned
            cleaned = _fit(cleaned, AcademyClass.name)
            if taken.get(cleaned.casefold(), academy_class.id) != academy_class.id:
                outcome[class_id] = ("duplicate", None)
                continue
            taken.pop(academy_class.name.casefold(), None)
            taken[cleaned.casefold()] = academy_class.id
            academy_class.name = cleaned
            lessons = select(ClassSession.id).where(ClassSession.class_id == academy_class.id)
            for item in session.scalars(
                select(InvoiceItem).where(InvoiceItem.session_id.in_(lessons),
                                          InvoiceItem.class_name.like(UNNAMED_CLASS + "%"))
            ).all():
                item.class_name = _fit(cleaned, InvoiceItem.class_name)
                relabelled += 1
            for credit in session.scalars(
                select(Credit).where(Credit.session_id.in_(lessons),
                                     Credit.class_name.like(UNNAMED_CLASS + "%"))
            ).all():
                credit.class_name = _fit(cleaned, Credit.class_name)
            outcome[class_id] = ("named", cleaned)
        session.commit()
    return {"outcome": outcome, "relabelled": relabelled}


def _apply_subject_name(session, academy_class, new_name) -> int:
    """Give a subject a new name, keeping the old one for uploads. Returns invoice lines relabelled.

    Invoice lines and credits that carry the old name take the new one, so a
    copy sent again reads properly and the invoice isn't listed as changed
    since it went out. Only that label changes, never an amount, date or
    price. The caller checks the name is free and commits.
    """
    from schedule_parser import UNNAMED_CLASS

    old = academy_class.name
    if old == new_name:
        return 0
    spellings = session.scalars(
        select(SubjectSpelling).where(SubjectSpelling.class_id == academy_class.id)).all()
    # Going back to an old name makes it the subject's own again, not an old spelling.
    for row in spellings:
        if _alias_key(row.spelling) == _alias_key(new_name):
            session.delete(row)
    # A placeholder for a class the sheet never named isn't worth keeping: the upload
    # asks for those names each time. Nor is a change of capitals only, since uploads
    # ignore capitals anyway.
    if (not old.startswith(UNNAMED_CLASS) and _alias_key(old) != _alias_key(new_name)
            and not any(_alias_key(row.spelling) == _alias_key(old) for row in spellings)):
        session.add(SubjectSpelling(class_id=academy_class.id, spelling=_fit(old, SubjectSpelling.spelling)))
    academy_class.name = new_name

    lessons = select(ClassSession.id).where(ClassSession.class_id == academy_class.id)
    items = session.scalars(select(InvoiceItem).where(
        InvoiceItem.session_id.in_(lessons), InvoiceItem.class_name == _fit(old, InvoiceItem.class_name))).all()
    for item in items:
        item.class_name = _fit(new_name, InvoiceItem.class_name)
    for credit in session.scalars(select(Credit).where(
            Credit.session_id.in_(lessons), Credit.class_name == _fit(old, Credit.class_name))).all():
        credit.class_name = _fit(new_name, Credit.class_name)
    return len(items)


def rename_subject(class_id, new_name) -> dict:
    """Rename a subject everywhere it shows. Returns {"result", "name", "relabelled"}.

    Its classes, prices and students stay as they are. Sent invoices show
    the new name, amounts unchanged. The old name is kept, so the teacher's
    sheet that still says it lands on this subject at the next upload. Names
    are unique across the academy, ignoring capitals. The result is
    "renamed", "unchanged", "duplicate", "invalid" or "not_found".
    """
    from schedule_parser import UNNAMED_CLASS

    cleaned = _fit(" ".join(str(new_name or "").split()), AcademyClass.name)
    with SessionLocal() as session:
        academy_class = session.get(AcademyClass, int(class_id))
        if academy_class is None:
            return {"result": "not_found"}
        if not cleaned or cleaned.startswith(UNNAMED_CLASS):
            return {"result": "invalid"}
        if cleaned == academy_class.name:
            return {"result": "unchanged", "name": cleaned, "relabelled": 0}
        if session.scalar(select(AcademyClass.id).where(
                func.lower(AcademyClass.name) == cleaned.lower(), AcademyClass.id != academy_class.id)):
            return {"result": "duplicate"}
        relabelled = _apply_subject_name(session, academy_class, cleaned)
        session.commit()
        return {"result": "renamed", "name": cleaned, "relabelled": relabelled}


def get_subject_spellings(teacher_id) -> dict[str, str]:
    """Return a teacher's subjects' old names: {old name in lower case: the subject's name now}."""
    if not teacher_id:
        return {}
    with SessionLocal() as session:
        return {
            _alias_key(spelling): name
            for spelling, name in session.execute(
                select(SubjectSpelling.spelling, AcademyClass.name)
                .join(AcademyClass, AcademyClass.id == SubjectSpelling.class_id)
                .where(AcademyClass.teacher_id == int(teacher_id))
            ).all()
        }


def find_duplicate_students():
    """Return pairs of students who may be one child on file twice, most likely first.

    Pairs are names that sound the same (other word order, another
    romanisation, a nickname in brackets) or are spelled within a letter or
    two. Two records that were ever in the same lesson are left out, since
    they're two children and merging them is refused anyway. So are two
    records with different tags ("Park Hana(A)" and "(B)"), which is how
    teachers tell two children with one name apart.
    """
    import difflib

    from schedule_parser import _HANGUL_NAME, MERGE_THRESHOLD, _bare, _suffix, hangul_fit, name_sound

    with SessionLocal() as session:
        students = session.execute(select(Student.id, Student.full_name).order_by(Student.id)).all()
        apart = set(session.execute(select(DistinctStudents.first_id, DistinctStudents.second_id)).all())
        lessons = session.execute(
            select(SessionAttendance.student_id, SessionAttendance.session_id,
                   Teacher.name, AcademyClass.name, ClassSession.session_date)
            .join(ClassSession, ClassSession.id == SessionAttendance.session_id)
            .join(Teacher, Teacher.id == ClassSession.teacher_id)
            .join(AcademyClass, AcademyClass.id == ClassSession.class_id)
        ).all()
    sat: dict[int, set] = defaultdict(set)
    teachers: dict[int, set] = defaultdict(set)
    for student_id, session_id, teacher, _, _ in lessons:
        sat[student_id].add(session_id)
        teachers[student_id].add(teacher)
    latest_grade = _latest_grades((row[0], row[3], row[4]) for row in lessons)

    said = {student_id: name_sound(name) for student_id, name in students}
    bare = {student_id: _bare(name) for student_id, name in students}
    tags = {student_id: _suffix(name) for student_id, name in students}
    names = dict(students)
    # Comparing every student with every other gets slow as students pile up: 0.4s at
    # 300, 5s at 1,500. Two names can only be alike if they share something (how they
    # sound, a word like "Lee", or how they start or end without spaces, like
    # "Kimhana" and "Kim Hana"), so only those pairs are compared. The result is the same.
    buckets: dict[str, list[int]] = defaultdict(list)
    for student_id, name in students:
        compact = bare[student_id].replace(" ", "")
        keys = {"sound:" + said[student_id]} if said[student_id] else set()
        keys |= {"word:" + word for word in bare[student_id].split() if len(word) > 1}
        keys |= {"start:" + compact[:3], "end:" + compact[-3:]}
        for key in keys:
            buckets[key].append(student_id)
    candidates = {
        (bucket[i], bucket[j])
        for bucket in buckets.values()
        for i in range(len(bucket)) for j in range(i + 1, len(bucket))
    }
    # A child written once in Hangul ("다혜" for "Dahye") shares no letters with their
    # English name, so none of the groups above would pair them.
    in_hangul = {sid for sid, name in students if _HANGUL_NAME.fullmatch(name.strip())}
    written_in = {sid: [other for other, other_name in students if hangul_fit(names[sid], other_name)]
                  for sid in in_hangul}
    candidates |= {tuple(sorted((sid, other))) for sid, others in written_in.items() for other in others}
    candidates = sorted(candidates)
    pairs = []
    for left_id, right_id in candidates:
        left, right = names[left_id], names[right_id]
        if (left_id, right_id) in apart:
            continue
        if tags[left_id] and tags[right_id] and tags[left_id] != tags[right_id]:
            continue
        sound = (bool(said[left_id]) and said[left_id] == said[right_id]) or (
            right_id in written_in.get(left_id, ()) or left_id in written_in.get(right_id, ()))
        if sound and sat[left_id] & sat[right_id]:
            continue
        matcher = difflib.SequenceMatcher(None, bare[left_id], bare[right_id])
        # Quick upper-limit checks first; most pairs stop here.
        if not sound and (matcher.real_quick_ratio() < MERGE_THRESHOLD
                          or matcher.quick_ratio() < MERGE_THRESHOLD):
            continue
        ratio = matcher.ratio()
        if not sound and ratio < MERGE_THRESHOLD:
            continue
        if sat[left_id] & sat[right_id]:
            continue
        grades = [latest_grade.get(left_id), latest_grade.get(right_id)]
        # Two or more grades apart means two children, however alike the names.
        if all(grades) and abs(grades[0] - grades[1]) >= 2:
            continue
        pairs.append({
            "ids": [left_id, right_id],
            "names": [left, right],
            "reason": "sound" if sound else "spelling",
            "similarity": round(ratio, 3),
            "lessons": [len(sat[left_id]), len(sat[right_id])],
            "teachers": [sorted(teachers[left_id]), sorted(teachers[right_id])],
            "grades": grades,
        })
    pairs.sort(key=lambda pair: (pair["reason"] != "sound", -pair["similarity"]))
    return pairs


# ---------------------------------------------------------------------------
# Invoicing
# ---------------------------------------------------------------------------

# Invoices from before the app go up to #7322, so the app's numbers start at 7323.
INVOICE_NUMBER_START = 7323
NEXT_NUMBER_SETTING = "next_invoice_number"


def _top_free_number(session) -> int:
    """Return one more than the highest invoice number ever used, void invoices included."""
    highest = session.scalar(select(func.max(Invoice.invoice_number)))
    return max(INVOICE_NUMBER_START, (highest or 0) + 1)


def _number_holder(session, number):
    """Return the invoice that has `number`, void ones included, or None."""
    return session.scalar(select(Invoice).where(Invoice.invoice_number == number))


def _next_invoice_number(session) -> int:
    """Return the number the next invoice will get.

    The admin's chosen number, if it's still free; otherwise one more than
    the highest used. A chosen number above the highest carries numbering on
    from there. A free one below it (freed by renumbering the invoice that
    had it) is used once, then numbering goes back to the top. It's checked
    when taken, so two invoices never share a number.
    """
    chosen = session.get(Setting, NEXT_NUMBER_SETTING)
    if chosen is not None:
        number = int(chosen.value)
        if number >= INVOICE_NUMBER_START and _number_holder(session, number) is None:
            return number
    return _top_free_number(session)


def _holder_summary(session, invoice) -> dict:
    """Return the details of the invoice holding a number, for the screen."""
    student = session.get(Student, invoice.student_id)
    return {"ID": invoice.id, "Number": invoice.invoice_number, "Status": invoice.status,
            "Student": student.full_name if student else "", "Issued": invoice.issued_on}


def _lock_numbers(session) -> None:
    """On Postgres, lock invoice numbering until the transaction ends.

    Stops two issues running at once from taking the same number.
    """
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": INVOICE_NUMBER_START})


def _choose_next(session, number) -> None:
    """Save `number` as the next invoice number."""
    chosen = session.get(Setting, NEXT_NUMBER_SETTING)
    if chosen is None:
        session.add(Setting(key=NEXT_NUMBER_SETTING, value=str(number)))
    else:
        chosen.value = str(number)


def get_next_invoice_number() -> dict:
    """Return {"next": the next invoice's number, "top": one more than the highest used}."""
    with SessionLocal() as session:
        return {"next": _next_invoice_number(session), "top": _top_free_number(session)}


def set_next_invoice_number(number) -> tuple[str, Any]:
    """Make `number` the next invoice's number.

    Returns ("set", number); ("taken", the invoice that has it), which
    move_invoice_number can free; or ("low", INVOICE_NUMBER_START) for a
    number in the range used before the app, which it can't check.
    """
    number = int(number)
    if number < INVOICE_NUMBER_START:
        return "low", INVOICE_NUMBER_START
    with _ONE_ISSUE_AT_A_TIME, SessionLocal() as session:
        _lock_numbers(session)
        holder = _number_holder(session, number)
        if holder is not None:
            return "taken", _holder_summary(session, holder)
        _choose_next(session, number)
        session.commit()
        return "set", number


def _rename_numbers_in_credits(session, renamed: dict) -> None:
    """Update credits that mention an invoice by number, e.g. "Overpaid: invoice #8590
    corrected to #8601", to the invoice's new number.

    All are changed at once, so two swapped numbers swap here too instead of
    both ending up the same.
    """
    if not renamed:
        return
    def follow(match):
        """Replace one "#number" with its new number, if it was renamed."""
        number = int(match.group(1))
        return f"#{renamed[number]}" if number in renamed else match.group(0)
    naming = [column.contains(f"#{number}") for number in renamed
              for column in (Credit.class_name, Credit.reason)]
    for credit in session.scalars(select(Credit).where(or_(*naming))):
        if credit.class_name:
            credit.class_name = _fit(re.sub(r"#(\d+)", follow, credit.class_name), Credit.class_name)
        credit.reason = _fit(re.sub(r"#(\d+)", follow, credit.reason or ""), Credit.reason)


def move_invoice_number(wanted, move_to) -> tuple[str, Any]:
    """Renumber the invoice that has `wanted` to `move_to`; `wanted` goes to the next invoice.

    For a number the admin needs that an invoice in the app already has.
    Credits that mention that invoice by number are updated too. Returns
    ("moved", {"Student", "From", "To"}); ("taken", the invoice that has it)
    or ("low", INVOICE_NUMBER_START) when `move_to` can't be used; or
    ("set", wanted) when nothing has `wanted` any more.
    """
    wanted, move_to = int(wanted), int(move_to)
    if min(wanted, move_to) < INVOICE_NUMBER_START:
        return "low", INVOICE_NUMBER_START
    with _ONE_ISSUE_AT_A_TIME, SessionLocal() as session:
        _lock_numbers(session)
        holder, moved = _number_holder(session, wanted), None
        if holder is not None:
            other = _number_holder(session, move_to)
            if other is not None:
                return "taken", _holder_summary(session, other)
            holder.invoice_number = move_to
            _rename_numbers_in_credits(session, {wanted: move_to})
            moved = {**_holder_summary(session, holder), "From": wanted, "To": move_to}
        _choose_next(session, wanted)
        session.commit()
        return ("moved", moved) if moved else ("set", wanted)


def renumber_invoice(invoice_id, number, move_holder_to=None) -> tuple[str, Any]:
    """Give a sent invoice the number `number`.

    If another invoice has it, that one first gets `move_holder_to` (this
    invoice's old number swaps the two). Credits that mention either by
    number are updated. Returns:

    * ("renumbered", {"Student", "From", "To", "Moved"}), where "Moved" is
      {"Student", "From", "To"} for the other invoice, or None;
    * ("taken", the invoice that has the number) if `move_holder_to` isn't given;
    * ("move_taken", the invoice that has it) if `move_holder_to` is taken too;
    * ("low", INVOICE_NUMBER_START); ("same", number);
    * ("not_issued", None) for an invoice that isn't sent, or is void. A void
      invoice is the record of what a parent was sent, so it keeps its number.
    """
    number = int(number)
    move_to = int(move_holder_to) if move_holder_to is not None else None
    if number < INVOICE_NUMBER_START or (move_to is not None and move_to < INVOICE_NUMBER_START):
        return "low", INVOICE_NUMBER_START
    with _ONE_ISSUE_AT_A_TIME, SessionLocal() as session:
        _lock_numbers(session)
        invoice = session.get(Invoice, invoice_id)
        if invoice is None or invoice.status != "Issued":
            return "not_issued", None
        if invoice.invoice_number == number:
            return "same", number
        renamed, moved = {}, None
        holder = _number_holder(session, number)
        if holder is not None:
            if move_to is None:
                return "taken", _holder_summary(session, holder)
            other = _number_holder(session, move_to)
            if other is not None and other.id != invoice.id:
                return "move_taken", _holder_summary(session, other)
            holder.invoice_number = move_to
            renamed[number] = move_to
            moved = {"Student": _holder_summary(session, holder)["Student"], "From": number, "To": move_to}
        old = invoice.invoice_number
        invoice.invoice_number = number
        renamed[old] = number
        _rename_numbers_in_credits(session, renamed)
        result = {"Student": _holder_summary(session, invoice)["Student"], "From": old, "To": number,
                  "Moved": moved}
        session.commit()
        return "renumbered", result


def as_date(value) -> date:
    """Return a date, whatever form SQLite gave it in.

    min() and max() over a date column come back as text, not a date, so
    anything comparing them with a real date goes through here first. The
    screens use it on dates from this module for the same reason.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def as_time(value) -> time:
    """Return a time of day from a time, a datetime, or "HH:MM[:SS]" text."""
    if isinstance(value, time):
        return value
    if isinstance(value, datetime):
        return value.time()
    parts = [int(part) for part in str(value).split(":")[:2]]
    return time(parts[0], parts[1] if len(parts) > 1 else 0)


def _span_hours(start_time, end_time):
    """Return the hours between two times, to 2 decimals and never below 0."""
    minutes = (
        end_time.hour * 60 + end_time.minute
        - start_time.hour * 60 - start_time.minute
    )
    return round(max(minutes, 0) / 60, 2)


def _class_hours(session):
    """Return how many hours a lesson lasts."""
    return _span_hours(session.start_time, session.end_time)


def _rate_index(session, class_ids) -> dict[int, list[ClassRate]]:
    """Load every price period for a set of subjects, newest first.

    Used wherever a price would otherwise be looked up per subject inside a
    loop: one query up front instead of one per subject.
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
    """Return the price in force for a subject on a date, from _rate_index."""
    for rate in index.get(class_id, []):
        if rate.effective_from <= when and (
            rate.effective_to is None or rate.effective_to >= when
        ):
            return float(rate.hourly_rate)
    return 0.0


def _apply_credits(session, invoice, items_total, issued_on):
    """Take a student's open credits off an invoice being issued.

    Oldest first, and never below zero: an invoice can't ask for a negative
    amount. A credit bigger than what's left is split, so the used part is
    settled here and the rest waits for the next month. That's what
    "carry the balance forward" means when a month of cancellations is worth
    more than the next month's bill.
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
    """Turn credits into invoice lines: one per subject, dates listed, amount negative.

    So a parent sees which lessons came off and by how much, laid out like
    the charges above them. Takes (credit, amount) pairs, because a draft
    can only show as much of a credit as its charges cover.
    """
    grouped = {}
    for credit, amount in entries:
        # Most credits are for a missed lesson. A price corrected later isn't, and a parent
        # reading "Cancelled" would look for a lesson that wasn't.
        reason = credit.reason or ""
        label = ("Price corrected" if reason.startswith("Price corrected")
                 else "Overpaid" if reason.startswith("Overpaid") else "Cancelled")
        entry = grouped.setdefault(
            (label, credit.class_name or "Cancelled class"), {"dates": [], "amount": 0.0}
        )
        if credit.session_date:
            entry["dates"].append(credit.session_date)
        entry["amount"] += float(amount or 0)

    lines = []
    for (label, name), entry in grouped.items():
        dates = sorted(entry["dates"])
        lines.append(
            {
                "Subject": f"{label} — {name}",
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
    """Return the credit lines a sent invoice actually settled."""
    return _group_credits(
        (credit, float(credit.amount or 0))
        for credit in session.scalars(
            select(Credit).where(Credit.invoice_id == invoice.id)
        ).all()
    )


def _open_credit_lines(session, student_id, charges):
    """Return the credits still open, cut down to what this draft can cover.

    Issuing never asks for a negative amount: _apply_credits settles oldest
    first, stops at zero and carries the rest to the next invoice. A draft
    must show the same sums, or the total on screen (and on the printable
    copy a parent might get) isn't what will really be asked for. Uses the
    same order as _apply_credits, so both pick the same credits.
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
    """Return what one student pays for one lesson: hours times the price in force."""
    index = rate_index if rate_index is not None else _rate_index(session, [lesson.class_id])
    hours = _class_hours(lesson)
    return round(hours * _rate_lookup(index, lesson.class_id, lesson.session_date), 2)


def _billed_amount(session, student_id, lesson_id):
    """Return what a student was actually charged for a lesson, or None.

    Issuing copies the hours, price and amount onto the line so they stop
    changing. A credit must give back that amount, not today's price:
    re-pricing a month after its invoices went out would otherwise refund
    an amount the student was never charged.
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
    """Save a credit for a lesson the student was already billed for.

    Safe to repeat: a re-import rewrites the roster again and again, and
    each pass would otherwise add the same credit again. Returns
    (credit, created); `created` is False when the credit was already there,
    so "N credits raised" counts real new credits.
    """
    existing = session.scalar(
        select(Credit).where(
            Credit.student_id == student_id, Credit.session_id == lesson.id
        )
    )
    if existing is not None:
        return existing, False

    billed = _billed_amount(session, student_id, lesson.id)
    if billed is not None and round(float(billed), 2) <= 0:
        return None, False  # on a sent invoice, but not charged: nothing to give back
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
    """Remove a credit that hasn't been used yet.

    A cancellation changed back to attending (a corrected spreadsheet, or a
    student who came after all) should take its credit with it, as long as
    no invoice has used it. One already applied or refunded stays.
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
    """Return the student's open invoice, creating one if they don't have one."""
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
    """Put every student who owes for a lesson onto their open invoice.

    Called whenever a lesson or its roster changes. Three rules:

    * A student who cancelled owes nothing for that lesson. If their invoice
      is still open, the charge is just left off.
    * If their invoice has already been sent, its figures are fixed, so the
      correction becomes a credit on their next invoice instead.
    * A student taken off the lesson is removed from any open invoice. One
      already invoiced for it gets a credit, the same as a cancellation.
      Teachers usually record an absence by taking the student off and
      writing why underneath, and a lesson invoiced in advance that the
      student then missed must come off their next invoice. The sent invoice
      itself is never changed.
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
                elif invoice.student_id not in billable:
                    _raise_credit(
                        session, invoice.student_id, lesson, "Taken off the class"
                    )
                else:
                    _drop_credit(session, invoice.student_id, lesson.id)
                continue
            if invoice.student_id in billable:
                billed.add(invoice.student_id)
            else:
                session.delete(item)

        # Never billed, so there's nothing to credit back; the charge just never appears.
        for student_id in cancelled - billed:
            _drop_credit(session, student_id, lesson.id)

        for student_id in billable - billed:
            invoice = _open_invoice_for(session, student_id)
            session.add(
                InvoiceItem(invoice_id=invoice.id, session_id=session_id)
            )

        session.commit()
        return "synced"


def _group_invoice_items(invoice, items, lessons, classes, teachers, rate_index):
    """Group one invoice's items into billing lines, one per subject and price.

    Only groups; it runs no queries. Callers load `lessons`, `classes`,
    `teachers` and `rate_index` (dicts by id; `rate_index` from _rate_index)
    for one invoice or a whole page of them. A sent invoice's items already
    hold their own figures, so empty dicts are fine for it.
    """
    grouped: dict[tuple, dict] = {}
    own_lines = []
    if invoice.status == "Issued":
        for item in items:
            if not item.session_date:
                continue
            if item.hours is None and item.session_id is None:
                # A line the admin added (materials, a discount), not a lesson.
                own_lines.append({
                    "Subject": item.class_name or "", "Teacher": "", "Dates": [], "Quantity": 1,
                    "Hours": 0, "Rate": 0, "Amount": round(float(item.amount or 0), 2), "Extra": True,
                })
                continue
            if not float(item.amount or 0) and not float(item.hours or 0):
                continue  # kept on the invoice so it's never billed again, but not charged
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
    return lines + own_lines


def _invoice_lines(session, invoice):
    """Return one invoice's lessons grouped into billing lines, one per subject and price."""
    items = session.scalars(
        select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)
    ).all()
    if invoice.status == "Issued":
        return _group_invoice_items(invoice, items, {}, {}, {}, {})
    lessons, classes, teachers, rate_index = _lessons_with_context(
        session, {item.session_id for item in items}
    )
    return _group_invoice_items(invoice, items, lessons, classes, teachers, rate_index)


def _lessons_with_context(session, session_ids):
    """Return what an open invoice's lines are priced and named from.

    (lessons, classes, teachers, rate_index): the lessons by id, their
    subjects and teachers by id, and the price index for those subjects.
    Four queries however many lines.
    """
    session_ids = {session_id for session_id in session_ids if session_id is not None}

    def by_id(model, ids):
        """Load rows of `model` by id, as {id: row}."""
        if not ids:
            return {}
        return {row.id: row for row in session.scalars(select(model).where(model.id.in_(ids))).all()}

    lessons = by_id(ClassSession, session_ids)
    class_ids = {lesson.class_id for lesson in lessons.values()}
    classes = by_id(AcademyClass, class_ids)
    teachers = by_id(Teacher, {lesson.teacher_id for lesson in lessons.values()})
    return lessons, classes, teachers, _rate_index(session, class_ids)


def _credit_totals(session, invoice_ids):
    """Return how much credit each invoice settled, by invoice id.

    Only _apply_credits attaches credits to invoices, so this is exactly what
    came off each one when it was issued.
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
    """Return what each invoice actually asks for: its lines, minus the credit it settled.

    This is the one definition of "the amount on the invoice". The printable
    copy shows the credit as a negative line. Everything that uses the saved
    figures instead (the invoice list, Payments, recording a payment) must
    subtract it the same way, or a parent is chased for money their invoice
    never asked for.
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
    # _apply_credits never settles more than the invoice charges, so this shouldn't go
    # below zero; the floor is there in case it ever does.
    return {
        invoice_id: round(
            max(gross.get(invoice_id, 0.0) - credited.get(invoice_id, 0.0), 0.0), 2
        )
        for invoice_id in invoice_ids
    }


def get_invoices(status=None, student_id=None):
    """Return invoices with their totals, newest first.

    A sent invoice's items are fixed, so its total and lesson count come
    from one SQL sum and count, instead of loading and grouping every item
    on every page load. "Lines" (the breakdown by subject) stays empty for
    sent invoices here, since the list doesn't show it; get_invoice still
    builds it for the printable copy. An open invoice's figures can still
    change, so it's worked out in full from its lessons, subjects, teachers
    and prices.
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
        open_ids = [invoice.id for invoice, _ in rows if invoice.status == "Open"]
        replaced_by, replaces = _revision_links(session, [invoice.id for invoice, _ in rows])

        # The money comes from _net_totals so it matches the printable copy and the
        # Payments screen; only the line count is worked out here.
        issued_nets = _net_totals(session, issued_ids)
        issued_counts: dict[int, int] = {}
        if issued_ids:
            for invoice_id, count in session.execute(
                select(InvoiceItem.invoice_id, func.count())
                .where(InvoiceItem.invoice_id.in_(issued_ids), InvoiceItem.hours > 0)
                .group_by(InvoiceItem.invoice_id)
            ).all():
                issued_counts[invoice_id] = count

        items_by_invoice: dict[int, list[InvoiceItem]] = defaultdict(list)
        if open_ids:
            for item in session.scalars(
                select(InvoiceItem).where(InvoiceItem.invoice_id.in_(open_ids))
            ).all():
                items_by_invoice[item.invoice_id].append(item)

        lessons, classes, teachers, rate_index = _lessons_with_context(
            session, {item.session_id for items in items_by_invoice.values() for item in items}
        )

        # A sent invoice's credits are already in _net_totals above. An open one has none
        # attached yet, so its draft figure uses whatever credit its student has waiting.
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
            elif invoice.status == "Void":
                said = replaced_by.get(invoice.id, {})
                total, count, lines = said.get("total", 0.0), said.get("classes", 0), []
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
                    "Replaced by": replaced_by.get(invoice.id, {}).get("number"),
                    "Replaces": replaces.get(invoice.id),
                }
            )
        # Newest by what people read (issue date, then invoice number), not by row id.
        # Issuing a backlog oldest month first splits new rows off each student's open
        # invoice, so ids and numbers run in different orders. Sorted by id, the newest
        # invoices ended up hundreds down the list, past the 100 the search shows.
        results.sort(
            key=lambda row: (
                row["Status"] != "Open",
                -(as_date(row["Issued"]).toordinal() if row["Issued"] else 0),
                -(row["Number"] or 0),
                -(row["ID"] or 0),
            )
        )
        return results


def get_invoice_counts():
    """Return how many invoices are open and how many have been issued.

    Only the two numbers the browse sections show in their headings, so the
    Invoices screen doesn't load every invoice to label a closed list.
    Streamlit runs every tab's code on every click anywhere in the app.
    """
    with SessionLocal() as session:
        open_count = session.scalar(
            select(func.count()).select_from(Invoice).where(Invoice.status == "Open")
        ) or 0
        issued_count = session.scalar(
            select(func.count()).select_from(Invoice).where(Invoice.status == "Issued")
        ) or 0
        return {"open_count": int(open_count), "issued_count": int(issued_count)}


def get_issued_invoices_for_month(year, month):
    """Return every invoice issued for a month's lessons, lowest number first, with its issue date.

    Uses the same rule as the month's "already sent" figure: a sent invoice
    with a lesson in that month. Void ones are left out; the replacement is
    the one the parent should have.
    """
    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])
    with SessionLocal() as session:
        rows = session.execute(
            select(Invoice.id, Invoice.invoice_number, Invoice.issued_on)
            .join(InvoiceItem, InvoiceItem.invoice_id == Invoice.id)
            .where(
                Invoice.status == "Issued",
                InvoiceItem.session_date >= first_day,
                InvoiceItem.session_date <= last_day,
            )
            .distinct()
            .order_by(Invoice.invoice_number)
        ).all()
    return [{"ID": invoice_id, "Number": number, "Issued": as_date(issued) if issued else None}
            for invoice_id, number, issued in rows]


def get_month_invoice_summary(year, month):
    """Return the billing position for one month of lessons.

    The academy works by month: August's schedule is typed in July,
    invoiced at the start of August and chased from late September. So the
    figures are for lessons taught in that month (what still needs
    invoicing, and what already has been), not for today's date.

    `credit_outstanding` is the exception and isn't limited to the month:
    credit belongs to the student, not a month, and comes off whichever
    invoice they get next.
    """
    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])

    with SessionLocal() as session:
        # Still to bill: items on open invoices whose lesson is in the month.
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

        # Already billed: sent items hold their own figures, and their lesson date was
        # copied onto them when they were issued.
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
    """Return one invoice with its lines, as the screens and printable copy show it, or None."""
    with SessionLocal() as session:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None:
            return None
        student = session.get(Student, invoice.student_id)
        parent = session.get(Parent, student.parent_id) if student and student.parent_id else None
        if invoice.status == "Void":
            return _void_record(session, invoice, student, parent)
        lines = _invoice_lines(session, invoice)
        charges = round(sum(line["Amount"] for line in lines), 2)
        # A sent invoice shows the credits it actually settled. An open one shows only as
        # much as its own charges can cover, so the draft total is what issuing will
        # really ask for; the rest carries forward instead of making the total negative.
        lines += (
            _credit_lines(session, invoice)
            if invoice.status == "Issued"
            else _open_credit_lines(session, invoice.student_id, charges)
        )
        record = _invoice_record(invoice, student, parent, lines)
        record["Replaces"] = _revision_links(session, [invoice.id])[1].get(invoice.id)
        return record


def _invoice_record(invoice, student, parent, lines):
    """Return one invoice the way the screens and the printable copy read it."""
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
    """Return several invoices in full, using a fixed number of queries.

    get_invoice takes four round trips, and the bulk tasks (making a month's
    invoice files, or listing what was just issued) asked for one invoice at
    a time. Thirty invoices was 120 round trips: under a second next to the
    database, but most of half a minute when the app and database were in
    different regions. They're both in Oregon now, but every round trip is
    still paid on every click.

    Everything a sent invoice needs is loaded up front and grouped in
    Python, so the cost doesn't grow with the number of invoices. Sent
    invoices are the case that matters: their items hold their own figures,
    so no prices or lessons need looking up.

    An open invoice goes through get_invoice, one at a time. Drafts are only
    opened one by one on screen, and their sums (credits shared against
    charges) are fiddly enough not to repeat here.

    Returns the same as [get_invoice(i) for i in ids], in the same order,
    skipping ids that don't exist.
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

        replaces = _revision_links(session, ids)[1]
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

            if invoice.status == "Void":
                detailed.append(_void_record(session, invoice, student, parent))
                continue
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

            record = _invoice_record(invoice, student, parent, lines)
            record["Replaces"] = replaces.get(invoice_id)
            detailed.append(record)
        return detailed


# Two issues at once (a double click, two admins, or a script running while the app
# is in use) would each read the same highest number and use it twice, and both move
# the same lessons. The thread lock covers runs inside the app; on Postgres an
# advisory lock, held until the commit, covers anything else using the database.
_ONE_ISSUE_AT_A_TIME = threading.Lock()


def issue_invoice_for_month(invoice_id, year, month, issued_on=None):
    """Issue only the lessons on a student's open invoice that fall in one month.

    A student's invoice stays open and collects every lesson until it's
    issued, but billing should only cover the month asked for. If the
    invoice also has lessons from other months, this splits it: the month's
    lessons go on a new, numbered, issued invoice, and the rest stay on the
    open one until their month is billed.
    """
    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])

    with _ONE_ISSUE_AT_A_TIME, SessionLocal() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                            {"key": INVOICE_NUMBER_START})
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
        # A warning wasn't enough: lines have gone out at the placeholder price before.
        # Nothing priced at or below it can be billed. An invoice asking a parent for a
        # dollar an hour is worse than one not sent.
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
            # A class name can be longer than the line's column allows.
            item.class_name = _fit(academy_class.name if academy_class else "",
                                   InvoiceItem.class_name)
            item.teacher_name = _fit(teacher.name if teacher else "",
                                     InvoiceItem.teacher_name)
            item.teacher_id = lesson.teacher_id
            item.session_date = lesson.session_date
            item.hours = hours
            item.hourly_rate = hourly
            item.amount = round(hours * hourly, 2)

        when = issued_on or date.today()
        # The amounts were just saved onto the items above; add those up instead of
        # working them out again from hours and prices.
        items_total = sum(float(item.amount or 0) for item, _ in in_month)
        _apply_credits(session, target, items_total, when)

        target.invoice_number = _next_invoice_number(session)
        target.issued_on = when
        target.status = "Issued"
        session.commit()
        return "issued", target.id


# ---------------------------------------------------------------------------
# Correcting a sent invoice
# ---------------------------------------------------------------------------
#
# A sent invoice only changes when someone edits it. Editing voids it (its number
# retired, what it said kept) and issues a replacement under a new number that
# says which one it replaces. Its lessons, the credit it settled and any payment
# recorded against it move to the replacement.


def _is_own_line(item):
    """Return True for a line the admin typed (materials, a discount), not a lesson."""
    return item.hours is None and item.session_id is None


def _calendar_now(lessons, classes, rate_index, session_id):
    """Return a lesson as the calendar has it now: (hours, price, subject), or None."""
    lesson = lessons.get(session_id)
    if lesson is None:
        return None
    academy_class = classes.get(lesson.class_id)
    return (round(_class_hours(lesson), 2),
            round(_rate_lookup(rate_index, lesson.class_id, lesson.session_date), 2),
            academy_class.name if academy_class else "")


def _serial(lines):
    """Return the lines with dates as text, so they can be saved as JSON."""
    return [{**line, "Dates": [str(day) for day in line.get("Dates") or []]} for line in lines]


def _revision_links(session, invoice_ids):
    """Return which of these invoices were replaced, and which replace another.

    (replaced_by, replaces): for a void invoice, what replaced it
    ({"number", "on", "total", "classes"}); for a replacement, the number of
    the invoice it replaced.
    """
    ids = list(invoice_ids)
    if not ids:
        return {}, {}
    replaced_by, replaces = {}, {}
    rows = session.execute(
        select(InvoiceRevision, Invoice.invoice_number)
        .join(Invoice, Invoice.id == InvoiceRevision.replaced_by_id)
        .where(or_(InvoiceRevision.invoice_id.in_(ids), InvoiceRevision.replaced_by_id.in_(ids)))
    ).all()
    old_numbers = dict(session.execute(
        select(Invoice.id, Invoice.invoice_number)
        .where(Invoice.id.in_({revision.invoice_id for revision, _ in rows}))
    ).all()) if rows else {}
    for revision, new_number in rows:
        said = json.loads(revision.details)
        replaced_by[revision.invoice_id] = {
            "number": new_number, "on": revision.revised_on, "total": said.get("total", 0.0),
            "classes": said.get("classes", 0), "details": said, "note": revision.note or "",
        }
        replaces[revision.replaced_by_id] = old_numbers.get(revision.invoice_id)
    return replaced_by, replaces


def _void_record(session, invoice, student, parent):
    """Return a void invoice exactly as it was sent, and what replaced it."""
    link = _revision_links(session, [invoice.id])[0].get(invoice.id, {})
    said = link.get("details", {})
    lines = [{**line, "Dates": [as_date(day) for day in line.get("Dates") or []]}
             for line in said.get("lines", [])]
    return {
        "ID": invoice.id,
        "Number": invoice.invoice_number,
        "Student": student.full_name if student else "",
        "Parent": parent.name if parent else "",
        "Phone": parent.phone if parent else "",
        "Status": "Void",
        "Issued": as_date(said["issued_on"]) if said.get("issued_on") else invoice.issued_on,
        "Lines": lines,
        "Total": round(float(said.get("total", 0.0)), 2),
        "Replaced by": link.get("number"),
        "Replaced on": link.get("on"),
        "Note": link.get("note", ""),
    }


def get_invoice_for_edit(invoice_id):
    """Return a sent invoice ready for editing, next to the calendar as it is now.

    "Lines" are its lessons: the line id, date, subject, teacher, hours,
    price and amount as sent, whether it's charged, the lesson on the
    calendar now ("Now": hours, price, subject, or None if it's gone) and
    any credit waiting for it. "Extra" are lines the admin added. "Waiting"
    are this student's lessons in the same months not billed yet. A lesson
    left off the invoice is one of these, and should be added from here
    rather than typed in, which would bill it twice. Also the credit it
    settled and what's been paid. None unless it's a sent invoice.
    """
    with SessionLocal() as session:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None or invoice.status != "Issued":
            return None
        student = session.get(Student, invoice.student_id)
        items = session.scalars(
            select(InvoiceItem).where(InvoiceItem.invoice_id == invoice.id)
            .order_by(InvoiceItem.session_date, InvoiceItem.id)
        ).all()
        lessons, classes, _, rate_index = _lessons_with_context(session, {i.session_id for i in items})
        lesson_ids = [item.session_id for item in items if item.session_id is not None]
        waiting = dict(session.execute(
            select(Credit.session_id, Credit.amount).where(
                Credit.student_id == invoice.student_id, Credit.status == "Open",
                Credit.session_id.in_(lesson_ids))
        ).all()) if lesson_ids else {}
        lines, extra = [], []
        for item in items:
            if _is_own_line(item):
                extra.append({"ID": item.id, "Description": item.class_name or "",
                              "Amount": round(float(item.amount or 0), 2)})
                continue
            lines.append({
                "ID": item.id,
                "Session ID": item.session_id,
                "Date": item.session_date,
                "Subject": item.class_name or "",
                "Teacher": item.teacher_name or "",
                "Hours": round(float(item.hours or 0), 2),
                "Rate": round(float(item.hourly_rate or 0), 2),
                "Amount": round(float(item.amount or 0), 2),
                "Charged": bool(float(item.amount or 0) or float(item.hours or 0)),
                "Now": _calendar_now(lessons, classes, rate_index, item.session_id),
                "Credit waiting": round(float(waiting.get(item.session_id) or 0), 2),
            })
        return {
            "ID": invoice.id,
            "Number": invoice.invoice_number,
            "Student": student.full_name if student else "",
            "Issued": invoice.issued_on,
            "Lines": lines,
            "Extra": extra,
            "Waiting": _waiting_for(session, invoice.student_id,
                                    {(d.year, d.month) for d in (as_date(line["Date"]) for line in lines
                                                                 if line["Date"])}),
            "Credit taken off": round(_credit_totals(session, [invoice.id]).get(invoice.id, 0.0), 2),
            "Paid": round(float(invoice.paid_amount), 2) if invoice.paid_amount is not None else None,
        }


def _waiting_for(session, student_id, months):
    """Return a student's unbilled lessons in those months, at today's prices.
    """
    if not months:
        return []
    rows = session.execute(
        select(InvoiceItem, ClassSession)
        .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
        .join(ClassSession, ClassSession.id == InvoiceItem.session_id)
        .where(Invoice.student_id == student_id, Invoice.status == "Open")
        .order_by(ClassSession.session_date, ClassSession.start_time)
    ).all()
    rows = [(item, lesson) for item, lesson in rows
            if (as_date(lesson.session_date).year, as_date(lesson.session_date).month) in months]
    _, classes, teachers, rate_index = _lessons_with_context(session, {lesson.id for _, lesson in rows})
    waiting = []
    for item, lesson in rows:
        hours = round(_class_hours(lesson), 2)
        rate = round(_rate_lookup(rate_index, lesson.class_id, lesson.session_date), 2)
        waiting.append({
            "ID": item.id, "Session ID": lesson.id, "Date": as_date(lesson.session_date),
            "Subject": classes[lesson.class_id].name if lesson.class_id in classes else "",
            "Teacher": teachers[lesson.teacher_id].name if lesson.teacher_id in teachers else "",
            "Hours": hours, "Rate": rate, "Amount": round(hours * rate, 2),
        })
    return waiting


def replace_invoice(invoice_id, lines, extra=(), note=None, today=None, add=()):
    """Correct a sent invoice: void it and issue a replacement under a new number.

    `lines` is {line id: {"Subject", "Hours", "Rate", "Charged"}} for its
    lessons; any left out keep their figures. A lesson not charged stays on
    the replacement at $0, so it's never billed again. `extra` is
    [{"Description", "Amount"}], lines the admin adds (a discount as a
    negative amount), and replaces any the old invoice had. `add` are ids
    of the student's lines still waiting to be billed (from
    get_invoice_for_edit's "Waiting"); those lessons move onto the
    replacement at today's price, so a lesson left off is billed there and
    not again.

    Nothing is refunded twice: a credit waiting for one of these lessons is
    cut to what the lesson now costs, or removed if it costs nothing. The
    credit the old invoice settled is used on the replacement, never past
    its total, and the rest waits for the next invoice. A payment recorded
    on the old invoice counts towards the replacement; any overpayment
    becomes a credit for the next invoice.

    Returns ("replaced", new id, new number), or (reason, None, None) with
    reason "not_issued", "invalid" (a charged lesson needs hours and a price,
    and an added lesson needs a price) or "negative" (the invoice would ask
    for less than nothing).
    """
    today = today or date.today()
    extra = [
        {"Description": " ".join(str(entry.get("Description") or "").split()),
         "Amount": round(float(entry.get("Amount") or 0), 2)}
        for entry in extra or ()
    ]
    extra = [entry for entry in extra if entry["Description"] and entry["Amount"]]

    with _ONE_ISSUE_AT_A_TIME, SessionLocal() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": INVOICE_NUMBER_START})
        old = session.get(Invoice, invoice_id)
        if old is None or old.status != "Issued":
            return "not_issued", None, None
        items = session.scalars(select(InvoiceItem).where(InvoiceItem.invoice_id == old.id)).all()
        classes_on = [item for item in items if not _is_own_line(item)]

        planned = {}
        for item in classes_on:
            change = lines.get(item.id, {})
            charged = bool(change.get("Charged", float(item.amount or 0) or float(item.hours or 0)))
            hours = round(float(change.get("Hours", item.hours or 0) or 0), 2)
            rate = round(float(change.get("Rate", item.hourly_rate or 0) or 0), 2)
            if charged and (hours <= 0 or rate <= UNSET_RATE):
                return "invalid", None, None
            subject = " ".join(str(change.get("Subject") or "").split()) or item.class_name
            planned[item.id] = (charged, hours, rate, subject)
        waiting = {entry["ID"]: entry for entry in _waiting_for(
            session, old.student_id,
            {(as_date(i.session_date).year, as_date(i.session_date).month) for i in classes_on if i.session_date})}
        adding = [waiting[item_id] for item_id in add if item_id in waiting]
        if len(adding) != len(set(add)) or any(entry["Rate"] <= UNSET_RATE for entry in adding):
            return "invalid", None, None
        gross = round(sum(hours * rate for charged, hours, rate, _ in planned.values() if charged)
                      + sum(entry["Amount"] for entry in adding)
                      + sum(entry["Amount"] for entry in extra), 2)
        if gross < 0:
            return "negative", None, None

        # What the old invoice said, saved before anything moves.
        before = _group_invoice_items(old, items, {}, {}, {}, {}) + _credit_lines(session, old)
        said = {
            "issued_on": str(old.issued_on) if old.issued_on else None,
            "lines": _serial(before),
            "total": round(sum(line["Amount"] for line in before), 2),
            "classes": sum(1 for item in classes_on if float(item.hours or 0) > 0),
            "paid_on": str(old.paid_on) if old.paid_on else None,
            "paid_amount": float(old.paid_amount) if old.paid_amount is not None else None,
            "payment_note": old.payment_note,
        }

        new = Invoice(student_id=old.student_id, status="Issued", issued_on=today, note=old.note,
                      paid_on=old.paid_on, paid_amount=old.paid_amount, payment_note=old.payment_note)
        session.add(new)
        session.flush()
        month_of = max((item.session_date for item in classes_on if item.session_date), default=today)
        for item in classes_on:
            charged, hours, rate, subject = planned[item.id]
            item.invoice_id = new.id
            item.class_name = _fit(subject, InvoiceItem.class_name)
            item.hourly_rate = rate
            item.hours = hours if charged else 0
            item.amount = round(hours * rate, 2) if charged else 0
            if item.session_id is not None:
                waiting = session.scalar(select(Credit).where(
                    Credit.student_id == old.student_id, Credit.session_id == item.session_id,
                    Credit.status == "Open"))
                if waiting is not None and float(waiting.amount) > float(item.amount) + 0.005:
                    if float(item.amount) <= 0:
                        session.delete(waiting)
                    else:
                        waiting.amount = item.amount
        drafts = set()
        for entry in adding:
            item = session.get(InvoiceItem, entry["ID"])
            lesson = session.get(ClassSession, entry["Session ID"])
            drafts.add(item.invoice_id)
            item.invoice_id = new.id
            item.class_name = _fit(entry["Subject"], InvoiceItem.class_name)
            item.teacher_name = _fit(entry["Teacher"], InvoiceItem.teacher_name)
            item.teacher_id = lesson.teacher_id
            item.session_date = lesson.session_date
            item.hours, item.hourly_rate, item.amount = entry["Hours"], entry["Rate"], entry["Amount"]
            classes_on.append(item)
        session.flush()
        for draft_id in drafts:
            if not session.scalar(select(func.count()).select_from(InvoiceItem)
                                  .where(InvoiceItem.invoice_id == draft_id)):
                session.delete(session.get(Invoice, draft_id))  # nothing left waiting on it
        for item in items:
            if _is_own_line(item):
                session.delete(item)
        for entry in extra:
            session.add(InvoiceItem(
                invoice_id=new.id, session_id=None, class_name=_fit(entry["Description"], InvoiceItem.class_name),
                teacher_name=None, teacher_id=None, session_date=month_of,
                hours=None, hourly_rate=None, amount=entry["Amount"],
            ))

        # The credit the old invoice settled is used on the replacement, never past its total.
        room = gross
        for credit in session.scalars(
            select(Credit).where(Credit.invoice_id == old.id).order_by(Credit.session_date, Credit.id)
        ).all():
            amount = round(float(credit.amount), 2)
            if amount <= room:
                credit.invoice_id = new.id
                room = round(room - amount, 2)
            elif room > 0:
                session.add(Credit(
                    student_id=credit.student_id, session_id=None, amount=round(amount - room, 2),
                    class_name=credit.class_name, session_date=credit.session_date, reason=credit.reason,
                    status="Open", created_on=credit.created_on, note="Balance carried forward",
                ))
                credit.amount, credit.invoice_id, room = room, new.id, 0.0
            else:
                credit.status, credit.invoice_id, credit.settled_on = "Open", None, None
        net = round(room, 2)  # what the replacement asks for, its credit taken off

        new.invoice_number = _next_invoice_number(session)
        if new.paid_amount is not None and float(new.paid_amount) > net + 0.005:
            session.add(Credit(
                student_id=new.student_id, session_id=None,
                amount=round(float(new.paid_amount) - net, 2),
                class_name=f"invoice #{new.invoice_number}", session_date=today,
                reason=f"Overpaid: invoice #{old.invoice_number} corrected to #{new.invoice_number}",
                status="Open", created_on=today,
            ))
            new.paid_amount = net
        old.status = "Void"
        old.paid_on = old.paid_amount = old.payment_note = None

        lessons, classes, _, rate_index = _lessons_with_context(
            session, {item.session_id for item in classes_on})
        said["seen"] = {str(item.session_id): _calendar_now(lessons, classes, rate_index, item.session_id)
                        for item in classes_on if item.session_id is not None}
        session.add(InvoiceRevision(invoice_id=old.id, replaced_by_id=new.id, revised_on=today,
                                    details=json.dumps(said), note=_fit(note or "", InvoiceRevision.note) or None))
        session.flush()
        _sync_lesson_paid_flags(session, new, new.paid_amount is not None
                                and float(new.paid_amount) >= net - 0.005)
        number = new.invoice_number
        new_id = new.id
        session.commit()
    return "replaced", new_id, number


def update_sent_invoices_to_schedule(invoice_ids, today=None):
    """Replace each of these sent invoices using the schedule's figures for its changed lessons.

    Only the lessons get_sent_invoices_out_of_step lists change, to the
    hours, price and subject the calendar has now; every other line,
    including the admin's own, stays the same. Returns
    [(old number, new id, new number)] for those replaced.
    """
    changed = {entry["ID"]: entry for entry in get_sent_invoices_out_of_step()}
    done = []
    for invoice_id in invoice_ids:
        entry = changed.get(invoice_id)
        data = get_invoice_for_edit(invoice_id) if entry else None
        if data is None:
            continue
        lines = {change["ID"]: change["Now"] for change in entry["Changes"]}
        status, new_id, number = replace_invoice(
            invoice_id, lines, extra=data["Extra"], today=today,
            note="Updated to the schedule's hours, prices and subject names")
        if status == "replaced":
            done.append((data["Number"], new_id, number))
    return done


def keep_invoice_as_sent(invoice_id, today=None):
    """Leave a sent invoice as it is, even though the calendar has changed since.

    Saved with what the calendar says now, so it's only flagged again if one
    of its lessons changes after this.
    """
    with SessionLocal() as session:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None or invoice.status != "Issued":
            return "not_issued"
        items = session.scalars(select(InvoiceItem).where(
            InvoiceItem.invoice_id == invoice.id, InvoiceItem.session_id.is_not(None))).all()
        lessons, classes, _, rate_index = _lessons_with_context(session, {i.session_id for i in items})
        seen = {str(item.session_id): _calendar_now(lessons, classes, rate_index, item.session_id)
                for item in items}
        session.add(InvoiceRevision(invoice_id=invoice.id, replaced_by_id=None,
                                    revised_on=today or date.today(), details=json.dumps({"seen": seen})))
        session.commit()
        return "kept"


def get_sent_invoices_out_of_step():
    """Return sent invoices whose lessons have changed on the calendar since they went out.

    A lesson's hours, price or subject name changed after its invoice was
    sent. The invoice keeps what it said (it only changes when someone edits
    it), and this is how anyone finds out. Lessons a student missed or was
    taken off are left out, since a credit already covers those, and so is
    anything already dealt with: a replaced or kept invoice is compared with
    what the calendar said at that time.

    One entry per invoice, newest first, each with its "Changes".
    """
    with SessionLocal() as session:
        rows = session.execute(
            select(InvoiceItem, Invoice, Student)
            .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
            .join(Student, Student.id == Invoice.student_id)
            .where(Invoice.status == "Issued", InvoiceItem.session_id.is_not(None),
                   InvoiceItem.hours > 0)
        ).all()
        if not rows:
            return []
        lesson_ids = {item.session_id for item, _, _ in rows}
        lessons, classes, _, rate_index = _lessons_with_context(session, lesson_ids)
        attending = set(session.execute(
            select(SessionAttendance.session_id, SessionAttendance.student_id)
            .where(SessionAttendance.session_id.in_(lesson_ids), SessionAttendance.is_cancelled.is_(False))
        ).all())
        credited = set(session.execute(
            select(Credit.session_id, Credit.student_id).where(Credit.session_id.in_(lesson_ids))
        ).all())
        # A price corrected by credit before this feature names the day, not the lesson.
        repriced = {(student_id, as_date(day)) for student_id, day in session.execute(
            select(Credit.student_id, Credit.session_date).where(
                Credit.session_id.is_(None), Credit.reason.like("Price corrected%"))
        ).all() if day}
        seen: dict[int, dict] = {}
        for revision in session.scalars(select(InvoiceRevision).order_by(InvoiceRevision.id)).all():
            looked_at = revision.replaced_by_id or revision.invoice_id
            seen[looked_at] = json.loads(revision.details).get("seen", {})

        found: dict[int, dict] = {}
        for item, invoice, student in rows:
            pair = (item.session_id, invoice.student_id)
            if pair not in attending or pair in credited:
                continue
            if (invoice.student_id, as_date(item.session_date)) in repriced:
                continue
            now = _calendar_now(lessons, classes, rate_index, item.session_id)
            if now is None:
                continue
            hours, rate, name = now
            if list(now) == (seen.get(invoice.id, {}).get(str(item.session_id)) or []):
                continue
            changes = []
            if abs(float(item.hours) - hours) > 0.005:
                changes.append(f"{float(item.hours):g}h → {hours:g}h")
            if rate > UNSET_RATE and abs(float(item.hourly_rate or 0) - rate) > 0.005:
                changes.append(f"${float(item.hourly_rate or 0):,.2f} → ${rate:,.2f}/h")
            if (item.class_name or "") != _fit(name, InvoiceItem.class_name):
                changes.append(f"“{item.class_name}” → “{name}”")
            if not changes:
                continue
            entry = found.setdefault(invoice.id, {
                "ID": invoice.id, "Number": invoice.invoice_number, "Student": student.full_name,
                "Issued": invoice.issued_on, "Changes": [],
            })
            entry["Changes"].append({"ID": item.id, "Date": item.session_date,
                                     "Subject": item.class_name or "", "What": ", ".join(changes),
                                     "Now": {"Hours": hours, "Rate": rate if rate > UNSET_RATE
                                             else float(item.hourly_rate or 0), "Subject": name}})
        return sorted(found.values(), key=lambda entry: -(entry["Number"] or 0))


# ---------------------------------------------------------------------------
# Credits for cancelled classes
# ---------------------------------------------------------------------------


def get_credits(status=None, student_id=None):
    """Return credits with their student, the lesson's teacher, and the invoice that settled it.

    The teacher comes from the lesson while it exists; if the lesson has
    been removed from the schedule, the teacher is blank. One query for all.
    """
    with SessionLocal() as session:
        query = (
            select(Credit, Student, Teacher.name, Invoice.invoice_number)
            .join(Student, Credit.student_id == Student.id)
            .outerjoin(ClassSession, Credit.session_id == ClassSession.id)
            .outerjoin(Teacher, ClassSession.teacher_id == Teacher.id)
            .outerjoin(Invoice, Credit.invoice_id == Invoice.id)
        )
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
                "Teacher": teacher or "",
                "Amount": float(credit.amount or 0),
                "Subject": credit.class_name or "",
                "Class date": credit.session_date,
                "Reason": credit.reason,
                "Status": credit.status,
                "Invoice ID": credit.invoice_id,
                "Invoice number": number,
                "Created": credit.created_on,
                "Settled": credit.settled_on,
                "Note": credit.note or "",
            }
            for credit, student, teacher, number in rows
        ]
        results.sort(
            key=lambda row: (row["Status"] != "Open", row["Class date"] or date.min),
        )
        return results


def refund_credits(credit_ids, refunded_on=None):
    """Mark several credits as refunded in cash, in one commit. Returns how many were open."""
    ids = [int(value) for value in credit_ids]
    if not ids:
        return 0
    with SessionLocal() as session:
        credits = session.scalars(
            select(Credit).where(Credit.id.in_(ids), Credit.status == "Open")
        ).all()
        for credit in credits:
            credit.status = "Refunded"
            credit.settled_on = refunded_on or date.today()
        session.commit()
        return len(credits)


def get_student_credit_totals(student_ids):
    """Return how much each of these students is owed, in one query."""
    ids = [int(value) for value in student_ids]
    if not ids:
        return {}
    with SessionLocal() as session:
        rows = session.execute(
            select(Credit.student_id, func.coalesce(func.sum(Credit.amount), 0.0))
            .where(Credit.student_id.in_(ids), Credit.status == "Open")
            .group_by(Credit.student_id)
        ).all()
    return {student_id: float(total or 0.0) for student_id, total in rows}


def count_billed_cancellations():
    """Count cancelled lessons whose charge hasn't been fixed yet.

    A charge needs fixing if it's on an invoice not yet sent (it can just be
    removed), or on a sent one that hasn't been credited back. Once
    credited, a sent invoice keeps the item on purpose, because sent
    invoices are never changed, so the item alone mustn't count as work to
    do, or the fix would run on every start-up forever.
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
    """Apply the rule that a cancelled lesson is never charged to cancellations already saved.

    Written to fix data from before the rule, but safe to run any time: a
    cancelled lesson on an open invoice is taken off it, one on a sent
    invoice gets a credit, and running it again changes nothing.
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


def workbook_drops(lesson_class_id, day, start, dates, keep_slots, class_ids, filled_slots):
    """Return True if a re-imported workbook no longer has this lesson, so it should go.

    Only on dates the workbook shows (`dates`: every day it has a column
    for); on other days it says nothing, so nothing is removed. On such a
    day, a lesson of a subject in the workbook (`class_ids`) goes unless the
    workbook still has that subject at that time (`keep_slots`). A lesson of
    a subject not in the workbook goes unless something is written at that
    time (`filled_slots`), since that may be the same lesson under a name
    the import couldn't match, so it's left alone.
    """
    if day not in dates:
        return False
    if lesson_class_id in class_ids:
        return (lesson_class_id, day, start) not in keep_slots
    return (day, start) not in filled_slots


def _lessons_between(session, teacher_id, dates):
    """Return a teacher's lessons from the earliest to the latest of `dates`."""
    if not dates:
        return []
    return session.scalars(
        select(ClassSession).where(
            ClassSession.teacher_id == teacher_id,
            ClassSession.session_date >= min(dates),
            ClassSession.session_date <= max(dates),
        )
    ).all()


def remove_lessons_not_in(teacher_id, dates, keep_slots, class_ids, filled_slots):
    """Delete a teacher's lessons that a re-imported workbook no longer has.

    Kept tight on purpose: only that teacher, and only the dates the
    workbook shows (see workbook_drops), so uploading one month can't touch
    another month (not even the one a sheet spills its last columns into)
    or another teacher's schedule.

    A lesson moved to a new day leaves its old slot behind, and without this
    the student would be billed for both. Removing it is handled like a
    cancellation: dropped from an open invoice, or credited back if the
    invoice has already gone out.
    """
    dates = set(dates)
    removed = credited = 0
    with SessionLocal() as session:
        stale = [
            lesson for lesson in _lessons_between(session, teacher_id, dates)
            if workbook_drops(lesson.class_id, lesson.session_date, lesson.start_time,
                              dates, keep_slots, class_ids, filled_slots)
        ]
        rate_index = _rate_index(session, {lesson.class_id for lesson in stale})
        for lesson in stale:
            raised, _ = _remove_lesson(
                session, lesson, "Class removed from the schedule", rate_index
            )
            credited += raised
            removed += 1
        session.commit()
    return {"lessons_removed": removed, "credits_raised": credited}


def get_teacher_lessons_on(teacher_id, dates):
    """Return a teacher's lessons on the given dates, to compare an upload against.

    Keyed by (class id, date, start); each has its roster as
    get_schedule_session gives it, and whether any student has been
    invoiced for it. Four queries however many lessons.
    """
    dates = set(dates)
    with SessionLocal() as session:
        lessons = [
            lesson for lesson in _lessons_between(session, teacher_id, dates)
            if lesson.session_date in dates
        ]
        ids = [lesson.id for lesson in lessons]
        names = dict(session.execute(
            select(AcademyClass.id, AcademyClass.name)
            .where(AcademyClass.id.in_({lesson.class_id for lesson in lessons}))
        ).all()) if lessons else {}
        roster: dict[int, list[dict[str, Any]]] = defaultdict(list)
        invoiced: set[int] = set()
        if ids:
            for attendance, student in session.execute(
                select(SessionAttendance, Student)
                .join(Student, SessionAttendance.student_id == Student.id)
                .where(SessionAttendance.session_id.in_(ids))
                .order_by(Student.full_name)
            ).all():
                roster[attendance.session_id].append(_attendance_entry(attendance, student))
            invoiced = set(session.scalars(
                select(InvoiceItem.session_id)
                .join(Invoice, InvoiceItem.invoice_id == Invoice.id)
                .where(Invoice.status == "Issued", InvoiceItem.session_id.in_(ids))
            ).all())
        return {
            (lesson.class_id, lesson.session_date, lesson.start_time): {
                "ID": lesson.id,
                "Class ID": lesson.class_id,
                "Class": names.get(lesson.class_id, ""),
                "Date": lesson.session_date,
                "Start": lesson.start_time,
                "End": lesson.end_time,
                "Status": lesson.status,
                "Attendance": roster.get(lesson.id, []),
                "Invoiced": lesson.id in invoiced,
            }
            for lesson in lessons
        }


def get_teacher_months(teacher_id):
    """Return every (year, month) a teacher has a lesson in on the calendar."""
    with SessionLocal() as session:
        days = session.scalars(
            select(ClassSession.session_date).where(ClassSession.teacher_id == teacher_id).distinct()
        ).all()
    return sorted({(as_date(day).year, as_date(day).month) for day in days})


def payment_due_date(year, month):
    """Return the date an invoice for that month's lessons becomes overdue.

    The 20th of the month after the lessons, as the academy has always done.
    """
    due_year, due_month = (year, month + 1)
    if due_month == 13:
        due_year, due_month = due_year + 1, 1
    return date(due_year, due_month, 20)


def _sync_lesson_paid_flags(session, invoice, paid):
    """Keep each lesson's paid flags in line with the invoice.

    The schedule grid shows which lessons still have money owing, using
    these flags. They aren't edited by hand any more (marking the invoice is
    the only step), so they're kept correct from here.
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
    """Record money received for an invoice.

    `amount` defaults to what's still owing, which is what nearly every
    payment is. A smaller amount records a part payment: it adds to what's
    already come in rather than replacing it, so a parent who pays in two
    parts ends up settled, and the invoice is chased until the balance is
    paid.
    """
    with SessionLocal() as session:
        invoice = session.get(Invoice, invoice_id)
        if invoice is None:
            return "missing"
        if invoice.status != "Issued":
            return "not issued"

        # What the invoice asked for, not what its lines add up to: a credit settled when
        # it was issued already came off the amount the parent was sent, so chasing the
        # full amount would leave every such invoice short forever.
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
        # The schedule's "owing" markers only clear once the whole invoice is paid; a part
        # payment leaves the lessons owing.
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
    """Return the sent invoices for a month, split into unpaid and paid.

    One query for the invoices and one for their totals, instead of a total
    per invoice. A busy month has well over a hundred of these, and the
    Payments screen shows them all.
    """
    today = today or date.today()
    year, month = int(year), int(month)
    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])
    due = payment_due_date(year, month)

    with SessionLocal() as session:
        # An invoice belongs to one month's screen: the month of its latest lesson, which
        # its due date is also worked out from (see payment_due_date and
        # get_payment_reminders). Listing it under every month it touched showed the same
        # invoice, at full value, on several screens and counted it as owing on each, so
        # one invoice covering nine months overstated eight of them.
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
            if latest is not None and first_day <= as_date(latest) <= last_day
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
            # An invoice issued a month at a time covers one month. One billed all at once
            # ("Bill it now") can cover several, and the screen has to say so rather than let
            # its total look like this month's.
            earliest, latest = spans.get(invoice.id, (None, None))
            row["Covers"] = ""
            if earliest is not None and latest is not None:
                earliest, latest = as_date(earliest), as_date(latest)
                if (earliest.year, earliest.month) != (latest.year, latest.month):
                    row["Covers"] = (
                        f"{month_abbr[earliest.month]} {earliest.year} – "
                        f"{month_abbr[latest.month]} {latest.year}"
                    )
            row["Owing"] = round(row["Total"] - float(invoice.paid_amount or 0.0), 2)
            if row["Owing"] > 0.005:
                # Part-paid invoices go here too: money is still owed, so they belong with the
                # unpaid ones.
                row["Overdue"] = max((today - due).days, 0) if today > due else 0
                unpaid.append(row)
            else:
                paid.append(row)
        # An invoice that a credit brought to $0 is settled but was never paid, so it has
        # no payment date; it sorts after every real payment.
        paid.sort(key=lambda item: (item["Paid on"] or date.min, item["Student"]), reverse=True)
        return {"unpaid": unpaid, "paid": paid, "due": due}


def get_payment_reminders(today=None):
    """Return sent invoices past their due date with no payment recorded.

    An invoice for January lessons is due on 20 February, so it shows here
    from then. One row per unpaid invoice, which is one row per thing you
    would chase a parent about.

    The date limit is applied in SQL, not afterwards in Python, so even if a
    year goes unpaid only the overdue invoices are loaded.
    """
    today = today or date.today()

    # An invoice can only be overdue after the 20th of the month after its lessons, so
    # the newest month that can count is last month (from the 20th on) or the month
    # before.
    newest_year, newest_month = today.year, today.month - (1 if today.day >= 20 else 2)
    while newest_month < 1:
        newest_month += 12
        newest_year -= 1
    cutoff = date(newest_year, newest_month, monthrange(newest_year, newest_month)[1])

    with SessionLocal() as session:
        # The latest lesson on each invoice decides when it's due, and the totals come
        # back in the same grouped query.
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
            # Group by both ids, not just the invoice's. Postgres allows other columns of a
            # table grouped by its id, but the student's name isn't covered by grouping on the
            # invoice, so it refuses the query. SQLite allows it and picks a row, which is why
            # this only failed once the app was on Postgres.
            .group_by(Invoice.id, Student.id)
            # Anything still owing, which includes part payments as well as invoices with
            # nothing paid.
            .having(
                func.sum(InvoiceItem.amount)
                > func.coalesce(Invoice.paid_amount, 0.0) + 0.005
            )
        ).all()
        if not rows:
            return []

        # Subjects in a second grouped query, not a group_concat: a subject name can
        # contain a comma, and splitting a joined string on commas would cut it in two.
        chased = [row[0] for row in rows]
        paid_so_far = {
            invoice_id: float(amount or 0.0)
            for invoice_id, amount in session.execute(
                select(Invoice.id, Invoice.paid_amount).where(Invoice.id.in_(chased))
            ).all()
        }
        # The HAVING above compares against the lines only, because the credit an invoice
        # settled can't be added up in the same query. Subtract it here: an invoice paid
        # by credit asks for nothing, and no payment could ever reach the full amount, so
        # the parent would be chased forever.
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
            latest, earliest = as_date(latest), as_date(earliest)
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


def find_schedule_session(class_id, session_date, start_time):
    """Return the id of the lesson already at this subject, date and time, if any.

    Used by the Excel import to decide whether a lesson is new or should
    update one already on the calendar.
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
    """Add or update the (teacher, year, month) import record the Students screen reads."""
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
    """Return one row per active teacher: whether, and when, they have this month imported."""
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
# Month reports for the Students, Invoices and Data screens
# ---------------------------------------------------------------------------


def get_students_in_month(year, month):
    """Return the students with at least one lesson in this calendar month."""
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
    """Return every student's month breakdown at once, by student id.

    What each student's lessons in a month add up to, grouped like an
    invoice (_invoice_lines): by subject, teacher and price. One query for
    everyone instead of one per student; the Students screen lists up to
    200 of these on one page.

    A lesson already on a sent invoice is priced from that invoice's line,
    not today's price table. Pricing everything at today's prices made this
    screen disagree with the bill the parent has, and showed $0.00 for a
    fully invoiced month if a subject's price was later removed. Lessons not
    yet billed are priced at today's prices, so the figure is still a useful
    estimate for the current month.

    Cancelled lessons are left out either way, the same as
    sync_invoice_items leaves them off the invoice.
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

        # What each lesson was actually billed at, where it has been. Keyed by
        # (student, lesson) because each student on a lesson is billed separately.
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
                # True if any of it came from a sent invoice, so the screen can say the figure is
                # a bill, not an estimate.
                "billed": student_id in billed_students,
            }
        return results


def get_open_invoice_items_for_month(year, month):
    """Return students with an open invoice that has a lesson in this month.

    "Month Amount" is only the part of that open invoice in the selected
    month; issue_invoice_for_month bills exactly that and leaves the rest open.

    It's the charges only. A student's credit is taken off when the invoice
    is issued, so the final amount can be lower. The Invoices screen says so
    next to the total instead of including it here, because which invoice a
    credit goes on is decided when issuing.
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
    """Return the date and teacher filter all the Data screen queries share."""
    return (
        ClassSession.teacher_id.in_(teacher_ids),
        ClassSession.session_date >= first_day,
        ClassSession.session_date <= last_day,
    )


def _teacher_month_money(session, first_day, last_day, teacher_ids):
    """Return what each teacher's lessons were actually invoiced for, per month.

    Read from sent invoices, not worked out from the price table. A sent
    line keeps its hours and price, so this is the money the academy really
    asked for. It doesn't change when a price is later changed or removed,
    and it already leaves out cancelled students who were left off the bill.

    Teacher and date also come from the line, since both are saved on it, so
    a line still counts correctly if its lesson was later edited or removed.
    The teacher is matched by saved id, not saved name, so a rename can't
    make a paid month disappear.

    "Lesson hours" is the teacher's time behind that money: each invoiced
    lesson once, however many students it was billed to. Using every hour on
    the schedule instead would count lessons not billed yet as time that
    earned nothing.
    """
    if not teacher_ids:
        return {}

    rows = session.execute(
        select(
            InvoiceItem.teacher_id,
            InvoiceItem.session_date,
            InvoiceItem.session_id,
            InvoiceItem.class_name,
            func.sum(InvoiceItem.amount),
            func.sum(InvoiceItem.hours),
            func.max(InvoiceItem.hours),
        )
        .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
        .where(
            Invoice.status == "Issued",
            InvoiceItem.teacher_id.in_(list(teacher_ids)),
            InvoiceItem.session_date >= first_day,
            InvoiceItem.session_date <= last_day,
        )
        .group_by(
            InvoiceItem.teacher_id,
            InvoiceItem.session_date,
            InvoiceItem.session_id,
            InvoiceItem.class_name,
        )
    ).all()

    buckets: dict[tuple[int, int, int], dict] = {}
    lessons: dict[tuple[int, int, int], dict] = defaultdict(dict)
    for teacher_id, when, session_id, class_name, amount, hours, longest in rows:
        if teacher_id is None:
            continue
        when = as_date(when)
        key = (teacher_id, when.year, when.month)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = buckets[key] = {"Earnings": 0.0, "Hours": 0.0}
        bucket["Earnings"] += float(amount or 0.0)
        bucket["Hours"] += float(hours or 0.0)
        # A lesson whose class was since deleted has lost its id; its date and saved class
        # name still tell it apart from the others.
        lesson = session_id if session_id is not None else (when, class_name)
        lessons[key][lesson] = max(lessons[key].get(lesson, 0.0), float(longest or 0.0))
    for key, bucket in buckets.items():
        bucket["Lesson hours"] = sum(lessons[key].values())
    return buckets


def _teacher_month_scheduled(session, first_day, last_day, teacher_ids):
    """Return the hours actually taught in a period, invoiced or not.

    Kept apart from the money: a lesson is taught whether or not its invoice
    has gone out, so hours taught mustn't drop to zero just because a month
    isn't billed yet. Future lessons and cancelled ones aren't counted.
    """
    if not teacher_ids:
        return {}
    rows = session.execute(
        select(
            ClassSession.teacher_id,
            ClassSession.session_date,
            ClassSession.start_time,
            ClassSession.end_time,
        ).where(
            *_lesson_window(first_day, min(last_day, date.today()), list(teacher_ids)),
            ClassSession.status != "Cancelled",
        )
    ).all()
    buckets: dict[tuple[int, int, int], float] = {}
    for teacher_id, when, start, end in rows:
        key = (teacher_id, when.year, when.month)
        buckets[key] = buckets.get(key, 0.0) + _span_hours(start, end)
    return buckets


def _teacher_month_uninvoiced(session, first_day, last_day, teacher_ids):
    """Return, per teacher and month, the lessons with a student not yet invoiced, and their value.

    The Data screen's money comes from sent invoices, so a month just
    imported (or a subject just priced) didn't change it, and the screen
    looked like it had missed it. This is what's still to come: each
    student-lesson not on a sent invoice, at the price in force on its date.
    A subject still on the import placeholder is counted but not priced, so
    a guessed price never reaches the total.
    """
    if not teacher_ids:
        return {}
    rows = session.execute(
        select(
            ClassSession.teacher_id,
            ClassSession.id,
            ClassSession.class_id,
            ClassSession.session_date,
            ClassSession.start_time,
            ClassSession.end_time,
            SessionAttendance.student_id,
        )
        .join(SessionAttendance, SessionAttendance.session_id == ClassSession.id)
        .where(
            *_lesson_window(first_day, last_day, list(teacher_ids)),
            ClassSession.status != "Cancelled",
            SessionAttendance.is_cancelled.is_(False),
        )
    ).all()
    billed = set(
        session.execute(
            select(Invoice.student_id, InvoiceItem.session_id)
            .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
            .where(
                Invoice.status == "Issued",
                InvoiceItem.session_id.is_not(None),
                InvoiceItem.session_date >= first_day,
                InvoiceItem.session_date <= last_day,
            )
        ).all()
    )
    rates = _rate_index(session, {row[2] for row in rows})
    waiting: dict[tuple[int, int, int], dict[str, Any]] = {}
    for teacher_id, session_id, class_id, when, start, end, student_id in rows:
        if (student_id, session_id) in billed:
            continue
        when = as_date(when)
        bucket = waiting.setdefault((teacher_id, when.year, when.month), {
            "lessons": {}, "value": 0.0, "unpriced": set()})
        hours = _span_hours(start, end)
        bucket["lessons"][session_id] = hours
        rate = _rate_lookup(rates, class_id, when)
        if rate > UNSET_RATE:
            bucket["value"] += hours * rate
        else:
            bucket["unpriced"].add(session_id)
    return {
        key: {
            "lessons": len(bucket["lessons"]),
            "hours": sum(bucket["lessons"].values()),
            "value": round(bucket["value"], 2),
            "unpriced": len(bucket["unpriced"]),
        }
        for key, bucket in waiting.items()
    }


def _teacher_student_counts(session, first_day, last_day, teacher_ids):
    """Return the number of different students per teacher in a period, counted by the database.

    COUNT(DISTINCT ...) in SQL instead of combining sets in Python: a few
    hundred rows come back instead of tens of thousands.
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
    """Return the active teachers (only those in `teacher_ids`, if given), sorted by name."""
    query = select(Teacher).where(Teacher.is_active.is_(True))
    if teacher_ids is not None:
        query = query.where(Teacher.id.in_(list(teacher_ids)))
    return session.scalars(query.order_by(Teacher.name)).all()


def get_teacher_month_stats(year, month, teacher_ids=None):
    """Return earnings, hours and student count per active teacher with a lesson that month.
    """
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
        waiting = _teacher_month_uninvoiced(session, first_day, last_day, ids)

        results = []
        for teacher in teachers:
            key = (teacher.id, year, month)
            bucket = money.get(key)
            hours = taught.get(key, 0.0)
            count = students.get(teacher.id, 0)
            unbilled = waiting.get(key, {"lessons": 0, "hours": 0.0, "value": 0.0, "unpriced": 0})
            if not bucket and not hours and not count:
                continue
            results.append(
                {
                    "Teacher ID": teacher.id,
                    "Teacher": teacher.name,
                    "Invoiced": round(bucket["Earnings"], 2) if bucket else 0.0,
                    "Billed hours": round(bucket["Hours"], 2) if bucket else 0.0,
                    "Invoiced lesson hours": round(bucket["Lesson hours"], 2) if bucket else 0.0,
                    "Hours": round(hours, 2),
                    "Students": count,
                    "Not invoiced lessons": unbilled["lessons"],
                    "Not invoiced hours": round(unbilled["hours"], 2),
                    "Not invoiced value": unbilled["value"],
                    "Not invoiced unpriced": unbilled["unpriced"],
                }
            )
        return results


def get_teacher_year_trend(year, up_to_month, teacher_ids=None):
    """Return month-by-month earnings for one calendar year, up to and including a month.

    Kept to one year on purpose: December next to the following January on
    one chart looks like a sudden drop rather than the end of a year, so
    each year gets its own chart.

    Every teacher who taught in the period gets a row for every month, with
    zeros filled in, so their line doesn't break in a month they didn't
    teach. Earnings come from the lessons themselves, so a month counts here
    whether or not it's been invoiced.
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
        waiting = _teacher_month_uninvoiced(session, first_day, last_day, ids)
        if not money and not scheduled and not waiting:
            return []

        # A teacher is on the chart if they billed or taught in the period, so a month
        # taught but not invoiced yet still shows their line at zero instead of the
        # teacher disappearing from the chart.
        active = ({tid for tid, _, _ in money} | {tid for tid, _, _ in scheduled}
                  | {tid for tid, _, _ in waiting})
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
                        "Not invoiced": waiting.get(key, {}).get("value", 0.0),
                    }
                )
        return rows


def get_unpriced_classes_for_month(year, month):
    """Return subjects taught in a month that have no price for their lessons.

    "No price" covers a subject never priced and one still on the import's
    UNSET_RATE placeholder. The placeholder is the common case, and it can
    quietly turn a $40,000 month into a $531 one. Better to catch it before
    sending invoices than after a parent gets one.

    A subject counts as unpriced for the lessons outside every real price
    period, so a price change mid-month only flags the part still missing.
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


# ---------------------------------------------------------------------------
# Past lessons for the Teaching figures
# ---------------------------------------------------------------------------


def save_lesson_history(teacher_name, sessions, source=None, dates=None):
    """Save a parsed workbook's lessons for the Teaching figures, apart from anything billed.

    `sessions` is a parser preview's sessions. Saving the same workbook again
    updates what changed instead of adding it twice; a lesson is one
    teacher, date, start time and student. A teacher name that matches an
    existing one apart from capitals uses that spelling, so one teacher's
    history stays together.

    `dates` are the days the worksheets show. On those days the newer copy
    of the workbook wins: a lesson it no longer has (moved, or a student
    taken off) is removed so it isn't counted twice. Other days, and other
    workbooks' lessons, are left alone.
    """
    teacher_name = " ".join(str(teacher_name or "").split())[:100]
    if not teacher_name:
        return {"status": "no_teacher"}
    source = (source or "")[:200] or None  # Postgres refuses a value that's too long

    incoming = {}
    for lesson in sessions:
        for attendance in lesson["attendance"]:
            student = " ".join(attendance["student_name"].split())[:100]
            if not student:
                continue
            incoming[(lesson["date"], lesson["start_time"], student.casefold())] = {
                "class_name": lesson["class_name"][:150],
                "lesson_date": lesson["date"],
                "start_time": lesson["start_time"],
                "end_time": lesson["end_time"],
                "student_name": student,
                "status": attendance["status"][:20],
            }

    with SessionLocal() as session:
        known = session.scalar(
            select(LessonHistory.teacher_name)
            .where(func.lower(LessonHistory.teacher_name) == teacher_name.lower())
            .limit(1)
        )
        teacher_name = known or teacher_name
        existing = {
            (row.lesson_date, row.start_time, row.student_name.casefold()): row
            for row in session.scalars(
                select(LessonHistory).where(LessonHistory.teacher_name == teacher_name)
            )
        }
        added = updated = unchanged = 0
        for key, values in incoming.items():
            row = existing.get(key)
            if row is None:
                session.add(LessonHistory(teacher_name=teacher_name, source=source, **values))
                added += 1
            elif any(getattr(row, field) != value for field, value in values.items()):
                for field, value in values.items():
                    setattr(row, field, value)
                row.source = source
                updated += 1
            else:
                unchanged += 1
        shown = set(dates or ())
        dropped = [row for key, row in existing.items() if key[0] in shown and key not in incoming]
        for row in dropped:
            session.delete(row)
        session.commit()
    return {"status": "saved", "teacher": teacher_name, "added": added,
            "updated": updated, "unchanged": unchanged, "removed": len(dropped)}


def get_lesson_history_summary():
    """Return one row per teacher with past schedules saved."""
    with SessionLocal() as session:
        rows = session.execute(
            select(
                LessonHistory.teacher_name,
                func.count(LessonHistory.id),
                func.count(distinct(LessonHistory.student_name)),
                func.min(LessonHistory.lesson_date),
                func.max(LessonHistory.lesson_date),
            )
            .group_by(LessonHistory.teacher_name)
            .order_by(LessonHistory.teacher_name)
        ).all()
    return [
        {"Teacher": teacher, "Student-lessons": lessons, "Students": students,
         "From": as_date(first), "To": as_date(last)}
        for teacher, lessons, students, first, last in rows
    ]


def _lesson_condition(is_online, has_recording, is_cancelled):
    # Same order as the parser: cancelled beats everything, then recording.
    """Return one label for a lesson's condition."""
    if is_cancelled:
        return "Cancelled"
    if has_recording:
        return "Recording"
    return "Online" if is_online else "Attending"


def get_analysis_lessons(today=None):
    """Return every past lesson for the Data screen, one row per student per lesson.

    `app` is the lessons on the Schedule screen; `history` is past schedules
    saved for the Teaching figures. Cancelled and future lessons are left
    out. teaching.combine sorts out any overlap between the two.
    """
    today = today or date.today()
    with SessionLocal() as session:
        app_rows = session.execute(
            select(
                Teacher.name,
                AcademyClass.name,
                ClassSession.session_date,
                ClassSession.start_time,
                ClassSession.end_time,
                Student.full_name,
                SessionAttendance.is_online,
                SessionAttendance.has_recording,
                SessionAttendance.is_cancelled,
            )
            .join(ClassSession, SessionAttendance.session_id == ClassSession.id)
            .join(AcademyClass, ClassSession.class_id == AcademyClass.id)
            .join(Teacher, ClassSession.teacher_id == Teacher.id)
            .join(Student, SessionAttendance.student_id == Student.id)
            .where(ClassSession.session_date <= today, ClassSession.status != "Cancelled")
        ).all()
        history_rows = session.execute(
            select(
                LessonHistory.teacher_name,
                LessonHistory.class_name,
                LessonHistory.lesson_date,
                LessonHistory.start_time,
                LessonHistory.end_time,
                LessonHistory.student_name,
                LessonHistory.status,
            ).where(LessonHistory.lesson_date <= today)
        ).all()
        # Past schedules name students as the teacher wrote them; a spelling known to
        # belong to a student on file counts as that student.
        known_as = dict(session.execute(
            select(StudentAlias.alias, Student.full_name)
            .join(Student, Student.id == StudentAlias.student_id)
        ).all())
    known_as = {_alias_key(alias): name for alias, name in known_as.items()}
    return {
        "app": [
            {"teacher": teacher, "class_name": class_name, "date": as_date(when),
             "start": start, "end": end, "student": student,
             "status": _lesson_condition(online, recording, cancelled), "source": "app"}
            for teacher, class_name, when, start, end, student, online, recording, cancelled
            in app_rows
        ],
        "history": [
            {"teacher": teacher, "class_name": class_name, "date": as_date(when),
             "start": start, "end": end, "student": known_as.get(_alias_key(student), student),
             "status": status, "source": "history"}
            for teacher, class_name, when, start, end, student, status in history_rows
        ],
    }


def get_analysis_data_version():
    """Return a quick fingerprint that changes when the lessons behind the Teaching figures do.
    """
    # One round trip: this runs every time the Teaching view opens.
    with SessionLocal() as session:
        row = session.execute(
            select(
                select(func.count(SessionAttendance.id)).scalar_subquery(),
                select(func.max(SessionAttendance.id)).scalar_subquery(),
                select(func.count(ClassSession.id)).scalar_subquery(),
                select(func.max(ClassSession.id)).scalar_subquery(),
                select(func.count(LessonHistory.id)).scalar_subquery(),
                select(func.max(LessonHistory.id)).scalar_subquery(),
                # A merge moves lessons without adding any; what changes is the number of
                # students and the spellings on file.
                select(func.count(Student.id)).scalar_subquery(),
                select(func.count(StudentAlias.id)).scalar_subquery(),
            )
        ).one()
    return tuple(int(value or 0) for value in row)


# ---------------------------------------------------------------------------
# Delete records (the screen that deletes anything, with everything linked)
# ---------------------------------------------------------------------------

# How many ids go into one IN (...) list. SQLite refuses very long ones.
_CHUNK = 500


def _chunks(values):
    """Yield `values` as lists of at most _CHUNK, leaving out empty ones."""
    values = [value for value in values if value is not None]
    for start in range(0, len(values), _CHUNK):
        yield values[start:start + _CHUNK]


def _ids_where(session, id_column, link_column, values) -> set:
    """Return `id_column` of every row whose `link_column` is one of `values`."""
    found = set()
    for chunk in _chunks(values):
        found.update(session.scalars(select(id_column).where(link_column.in_(chunk))))
    found.discard(None)
    return found


def _delete_ids(session, model, ids) -> None:
    """Delete the rows of `model` with these ids."""
    for chunk in _chunks(ids):
        session.execute(delete(model).where(model.id.in_(chunk)),
                        execution_options={"synchronize_session": False})


def _in_period(column, year=None, month=None) -> list:
    """Return conditions keeping a date `column` inside a year, a month of any year, or both.

    No year and no month means no conditions: all time.
    """
    conditions = []
    if year:
        conditions.append(extract("year", column) == int(year))
    if month:
        conditions.append(extract("month", column) == int(month))
    return conditions


def _master_plan(session, teacher_ids, student_ids, class_ids, invoice_ids, lesson_ids, history_names,
                 year=None, month=None) -> dict:
    """Work out every row a delete on Delete records removes or changes.

    Starts from what was picked and follows every link out from it. A
    teacher takes their subjects, classes, uploads and past schedules. A
    subject takes its classes, enrolments, weekly times and prices. A class
    takes its attendance, invoice lines and credits. A student takes their
    invoices, credits, enrolments, attendance, other spellings, and their
    parent if no other child has that parent. An invoice takes its lines,
    every version of it (the void ones it replaced and the ones that
    replaced it) and any credit raised when it was corrected. An invoice
    left with no lines goes as well, since nothing is left on it.

    A year, a month or both keep the delete inside that time. A teacher or
    subject then loses only its classes in it, and a student only their
    places, invoice lines and credits in it; the teacher, subject and
    student themselves stay, and so do past schedules unless picked on
    their own. With no year and no month, they go whole.

    Two things are changed instead of deleted. A credit that was used up on
    a deleted invoice goes back to waiting, because that invoice no longer
    exists. And a class that stays but was on a deleted invoice is no longer
    marked paid.
    """
    whole = not year and not month
    picked_teachers = _ids_where(session, Teacher.id, Teacher.id, teacher_ids)
    picked_students = _ids_where(session, Student.id, Student.id, student_ids)
    picked_classes = _ids_where(session, AcademyClass.id, AcademyClass.id, class_ids)
    lessons = _ids_where(session, ClassSession.id, ClassSession.id, lesson_ids)
    own_places, own_credits = set(), set()
    if whole:
        teachers, students = picked_teachers, picked_students
        classes = picked_classes | _ids_where(session, AcademyClass.id, AcademyClass.teacher_id, teachers)
        lessons |= (_ids_where(session, ClassSession.id, ClassSession.class_id, classes)
                    | _ids_where(session, ClassSession.id, ClassSession.teacher_id, teachers))
        items = (_ids_where(session, InvoiceItem.id, InvoiceItem.session_id, lessons)
                 | _ids_where(session, InvoiceItem.id, InvoiceItem.teacher_id, teachers))
        invoices = (_ids_where(session, Invoice.id, Invoice.id, invoice_ids)
                    | _ids_where(session, Invoice.id, Invoice.student_id, students))
    else:
        # Only what falls inside the period goes; the records themselves stay.
        teachers, students, classes = set(), set(), set()
        in_period = select(ClassSession.id).where(*_in_period(ClassSession.session_date, year, month))
        subjects_of = (picked_classes
                       | _ids_where(session, AcademyClass.id, AcademyClass.teacher_id, picked_teachers))
        if picked_teachers or subjects_of:
            lessons |= set(session.scalars(select(ClassSession.id).where(
                ClassSession.id.in_(in_period),
                or_(ClassSession.class_id.in_(list(subjects_of)), ClassSession.teacher_id.in_(list(picked_teachers))))))
        items = _ids_where(session, InvoiceItem.id, InvoiceItem.session_id, lessons)
        if picked_students:
            # A student's own records in the period: their places in its classes, the
            # lines billing them (sent lines whose class has gone keep their date) and
            # the credits for those classes.
            own_places = set(session.scalars(select(SessionAttendance.id).where(
                SessionAttendance.student_id.in_(list(picked_students)),
                SessionAttendance.session_id.in_(in_period))))
            items |= set(session.scalars(
                select(InvoiceItem.id).join(Invoice, Invoice.id == InvoiceItem.invoice_id).where(
                    Invoice.student_id.in_(list(picked_students)),
                    or_(InvoiceItem.session_id.in_(in_period),
                        and_(InvoiceItem.session_id.is_(None),
                             *_in_period(InvoiceItem.session_date, year, month))))))
            own_credits = set(session.scalars(select(Credit.id).where(
                Credit.student_id.in_(list(picked_students)), Credit.session_id.in_(in_period))))
        invoices = _ids_where(session, Invoice.id, Invoice.id, invoice_ids)

    # Invoices that lose lines. One that loses all of them goes too.
    touched = _ids_where(session, InvoiceItem.invoice_id, InvoiceItem.id, items) - invoices
    lines_left = defaultdict(int)
    for chunk in _chunks(touched):
        for invoice_id, item_id in session.execute(
            select(InvoiceItem.invoice_id, InvoiceItem.id).where(InvoiceItem.invoice_id.in_(chunk))
        ):
            if item_id not in items:
                lines_left[invoice_id] += 1
    emptied = {invoice_id for invoice_id in touched if not lines_left[invoice_id]}
    invoices |= emptied

    # Every version of the same bill goes together, however long the chain.
    revisions = set()
    while True:
        linked = set()
        for chunk in _chunks(invoices):
            for revision_id, old_id, new_id in session.execute(
                select(InvoiceRevision.id, InvoiceRevision.invoice_id, InvoiceRevision.replaced_by_id)
                .where(or_(InvoiceRevision.invoice_id.in_(chunk), InvoiceRevision.replaced_by_id.in_(chunk)))
            ):
                revisions.add(revision_id)
                linked.update({old_id, new_id})
        linked.discard(None)
        if linked <= invoices:
            break
        invoices |= linked
    items |= _ids_where(session, InvoiceItem.id, InvoiceItem.invoice_id, invoices)

    gone_numbers, billed_students = set(), set()
    for chunk in _chunks(invoices):
        for number, student_id in session.execute(
            select(Invoice.invoice_number, Invoice.student_id).where(Invoice.id.in_(chunk))
        ):
            if number is not None:
                gone_numbers.add(number)
            billed_students.add(student_id)
    # Sent invoices that stay but lose lines.
    shortened = set()
    for chunk in _chunks(touched - invoices):
        shortened.update(session.scalars(select(Invoice.id).where(
            Invoice.id.in_(chunk), Invoice.status == "Issued")))

    credits = (_ids_where(session, Credit.id, Credit.student_id, students)
               | _ids_where(session, Credit.id, Credit.session_id, lessons) | own_credits)
    # A credit raised by correcting a deleted invoice names it by number, e.g.
    # "Overpaid: invoice #8590 corrected to #8601". It came from that bill, so it goes too.
    if gone_numbers:
        for chunk in _chunks(billed_students - students):
            for credit_id, subject, reason in session.execute(
                select(Credit.id, Credit.class_name, Credit.reason).where(
                    Credit.student_id.in_(chunk),
                    or_(Credit.class_name.contains("#"), Credit.reason.contains("#")))
            ):
                said = {int(number) for number in re.findall(r"#(\d+)", f"{subject or ''} {reason or ''}")}
                if said & gone_numbers:
                    credits.add(credit_id)
    pointing = _ids_where(session, Credit.id, Credit.invoice_id, invoices) - credits
    reopened = set()
    for chunk in _chunks(pointing):
        reopened.update(session.scalars(select(Credit.id).where(Credit.id.in_(chunk), Credit.status == "Applied")))

    attendance = (_ids_where(session, SessionAttendance.id, SessionAttendance.session_id, lessons)
                  | _ids_where(session, SessionAttendance.id, SessionAttendance.student_id, students) | own_places)
    # Classes that stay but were billed on a deleted invoice: no longer paid for.
    unpaid = defaultdict(set)
    for chunk in _chunks(invoices):
        for student_id, lesson_id in session.execute(
            select(Invoice.student_id, InvoiceItem.session_id)
            .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
            .where(InvoiceItem.invoice_id.in_(chunk), InvoiceItem.session_id.is_not(None))
        ):
            if student_id not in students and lesson_id not in lessons:
                unpaid[student_id].add(lesson_id)

    parents = _ids_where(session, Student.parent_id, Student.id, students)
    other_children = _ids_where(session, Student.id, Student.parent_id, parents) - students
    parents -= _ids_where(session, Student.parent_id, Student.id, other_children)

    # Past schedules are saved by teacher name, not linked, so they're found by name. Inside a
    # period only the picked teachers' rows in it go; a whole teacher takes all of theirs.
    names = _ids_where(session, Teacher.name, Teacher.id, teachers) | set(history_names)
    history = _ids_where(session, LessonHistory.id, LessonHistory.teacher_name, names)
    if not whole and history:
        history = set(session.scalars(select(LessonHistory.id).where(
            LessonHistory.teacher_name.in_(list(names)), *_in_period(LessonHistory.lesson_date, year, month))))
    # Upload records are kept per teacher and month, so a period takes the matching months.
    uploads = _ids_where(session, ScheduleImport.id, ScheduleImport.teacher_id, teachers)
    if not whole and picked_teachers:
        uploads = set(session.scalars(select(ScheduleImport.id).where(
            ScheduleImport.teacher_id.in_(list(picked_teachers)),
            *([ScheduleImport.year == int(year)] if year else []),
            *([ScheduleImport.month == int(month)] if month else []))))
    other = {
        Enrolment: (_ids_where(session, Enrolment.id, Enrolment.class_id, classes)
                    | _ids_where(session, Enrolment.id, Enrolment.student_id, students)),
        ClassSchedule: _ids_where(session, ClassSchedule.id, ClassSchedule.class_id, classes),
        ClassRate: _ids_where(session, ClassRate.id, ClassRate.class_id, classes),
        SubjectSpelling: _ids_where(session, SubjectSpelling.id, SubjectSpelling.class_id, classes),
        ScheduleImport: uploads,
        LessonHistory: history,
        StudentAlias: _ids_where(session, StudentAlias.id, StudentAlias.student_id, students),
        DistinctStudents: (_ids_where(session, DistinctStudents.id, DistinctStudents.first_id, students)
                           | _ids_where(session, DistinctStudents.id, DistinctStudents.second_id, students)),
    }

    # The next number once these are gone. A number the admin chose that one of
    # these invoices took (9001 for a demo, say) is forgotten, so numbering
    # carries on from the highest invoice left instead of going back to it.
    left = {number for invoice_id, number in session.execute(
        select(Invoice.id, Invoice.invoice_number).where(Invoice.invoice_number.is_not(None))
    ) if invoice_id not in invoices}
    chosen = session.get(Setting, NEXT_NUMBER_SETTING)
    choice = int(chosen.value) if chosen is not None else None
    drop_choice = choice is not None and choice in gone_numbers
    if choice is not None and not drop_choice and choice >= INVOICE_NUMBER_START and choice not in left:
        after = choice
    else:
        after = max(INVOICE_NUMBER_START, max(left, default=0) + 1)

    return {
        "teachers": teachers, "students": students, "parents": parents, "classes": classes,
        "lessons": lessons, "attendance": attendance, "invoices": invoices, "items": items,
        "revisions": revisions, "credits": credits, "other": other, "pointing": pointing,
        "reopened": reopened, "unpaid": unpaid, "numbers": sorted(gone_numbers),
        "shortened": shortened, "emptied": emptied, "drop_choice": drop_choice,
        "next": (_next_invoice_number(session), after),
        "whole": whole, "picked_teachers": picked_teachers, "picked_students": picked_students,
        "picked_classes": picked_classes,
    }


def _master_summary(plan) -> dict:
    """Return what a delete removes and changes, in the screen's words."""
    other = plan["other"]
    counts = [
        ("Teachers", plan["teachers"]),
        ("Students", plan["students"]),
        ("Parents", plan["parents"]),
        ("Subjects", plan["classes"]),
        ("Classes", plan["lessons"]),
        ("Students' places in classes", plan["attendance"]),
        ("Enrolments", other[Enrolment]),
        ("Weekly times", other[ClassSchedule]),
        ("Subject prices", other[ClassRate]),
        ("Old subject names", other[SubjectSpelling]),
        ("Upload records", other[ScheduleImport]),
        ("Past schedules (student-lessons)", other[LessonHistory]),
        ("Other spellings of names", other[StudentAlias]),
        ("'Not the same child' notes", other[DistinctStudents]),
        ("Invoices", plan["invoices"]),
        ("Invoice lines", plan["items"]),
        ("Invoice corrections", plan["revisions"]),
        ("Credits", plan["credits"]),
    ]
    return {
        "Rows": [{"What": what, "How many": len(ids)} for what, ids in counts if ids],
        "Numbers": plan["numbers"],
        "Next before": plan["next"][0],
        "Next after": plan["next"][1],
        "Shortened": len(plan["shortened"]),
        "Reopened": len(plan["reopened"]),
        "Unpaid": sum(len(lessons) for lessons in plan["unpaid"].values()),
    }


def _lookup(session, id_column, columns, ids) -> dict:
    """Return {id: (columns...)} for the rows with these ids."""
    found = {}
    for chunk in _chunks(ids):
        for row in session.execute(select(id_column, *columns).where(id_column.in_(chunk))):
            found[row[0]] = tuple(row[1:])
    return found


def _master_report(session, plan, class_ids, lesson_ids, invoice_ids, history_names) -> dict:
    """Explain a delete for the Delete records screen, before it happens.

    "Linked" starts at the top: each teacher picked, with their subjects and
    the students in their classes; then subjects, classes, students,
    invoices and past schedules picked on their own. "Beyond" lists what
    reaches records that weren't picked: other students' places in class,
    their sent invoices and their credits. When only test records are being
    deleted it is empty, so anything in it means something real is linked.
    "Effects" is what changes once it's done. Each part also carries the
    full detail the screen shows when opened: every class with its
    students, every place in class, invoice and invoice line.
    """
    # The picks, whether they go whole or only inside a period.
    teachers, students = plan["picked_teachers"], plan["picked_students"]
    lessons = {lesson_id: (class_id, teacher_id, as_date(day), as_time(start))
               for lesson_id, (class_id, teacher_id, day, start) in _lookup(
                   session, ClassSession.id, (ClassSession.class_id, ClassSession.teacher_id,
                                              ClassSession.session_date, ClassSession.start_time),
                   plan["lessons"]).items()}
    seats, lost = defaultdict(set), defaultdict(int)
    for chunk in _chunks(plan["lessons"]):
        for lesson_id, student_id in session.execute(
            select(SessionAttendance.session_id, SessionAttendance.student_id)
            .where(SessionAttendance.session_id.in_(chunk))
        ):
            seats[lesson_id].add(student_id)
            if student_id not in students:
                lost[student_id] += 1
    subjects = _lookup(session, AcademyClass.id, (AcademyClass.name, AcademyClass.teacher_id),
                       plan["classes"] | plan["picked_classes"] | {row[0] for row in lessons.values()})
    bills = _lookup(session, Invoice.id, (Invoice.invoice_number, Invoice.status, Invoice.student_id,
                                          Invoice.paid_amount, Invoice.issued_on), plan["invoices"] | plan["shortened"])
    line_counts = defaultdict(int)
    for chunk in _chunks(bills):
        line_counts.update(session.execute(
            select(InvoiceItem.invoice_id, func.count()).where(InvoiceItem.invoice_id.in_(chunk))
            .group_by(InvoiceItem.invoice_id)).all())
    credits = _lookup(session, Credit.id, (Credit.student_id, Credit.amount, Credit.status), plan["credits"])
    seated = {student_id for students_in in seats.values() for student_id in students_in}
    student_names = {key: row[0] for key, row in _lookup(
        session, Student.id, (Student.full_name,),
        students | seated | {row[2] for row in bills.values()} | {row[0] for row in credits.values()}).items()}
    teacher_names = {key: row[0] for key, row in _lookup(
        session, Teacher.id, (Teacher.name,),
        teachers | {row[1] for row in subjects.values()} | {row[1] for row in lessons.values()}).items()}
    nets = {}
    for chunk in _chunks([key for key, row in bills.items() if row[1] == "Issued"]):
        nets.update(_net_totals(session, chunk))
    history = defaultdict(int)
    for chunk in _chunks(plan["other"][LessonHistory]):
        for name, count in session.execute(
            select(LessonHistory.teacher_name, func.count()).where(LessonHistory.id.in_(chunk))
            .group_by(LessonHistory.teacher_name)
        ):
            history[name] += count

    def bill(invoice_id) -> dict:
        """Return one invoice as the screen shows it."""
        number, status, student_id, paid, issued = bills[invoice_id]
        return {"Number": number, "Status": status, "Student": student_names.get(student_id, ""),
                "Amount": nets.get(invoice_id), "Paid": float(paid) if paid is not None else None,
                "Issued": as_date(issued) if issued else None, "Classes": line_counts[invoice_id]}

    by_subject = defaultdict(list)
    for lesson_id, row in lessons.items():
        by_subject[row[0]].append(lesson_id)

    def taught(chosen) -> list:
        """Return the names of everyone in these classes."""
        return sorted({student_names[s] for lesson_id in chosen for s in seats[lesson_id]}, key=str.lower)

    def class_rows(chosen) -> list:
        """Return these classes in date order, each with its subject, teacher and students."""
        return [{"Date": lessons[key][2], "Time": f"{lessons[key][3]:%H:%M}", "Subject": subjects[lessons[key][0]][0],
                 "Teacher": teacher_names.get(lessons[key][1], ""), "Students": ", ".join(taught([key]))}
                for key in sorted(chosen, key=lambda key: lessons[key][2:])]

    def subject(class_id) -> dict:
        """Return one subject with its classes and students."""
        days = [lessons[lesson_id][2] for lesson_id in by_subject[class_id]]
        return {"Name": subjects[class_id][0], "Teacher": teacher_names.get(subjects[class_id][1], ""),
                "Classes": len(days), "From": min(days, default=None), "To": max(days, default=None),
                "Students": taught(by_subject[class_id]), "Detail": class_rows(by_subject[class_id])}

    def by_name(names):
        """Sort ids by the name they map to."""
        return lambda key: str(names.get(key, "")).lower()

    linked = []
    for teacher_id in sorted(teachers, key=by_name(teacher_names)):
        theirs = [lesson_id for lesson_id, row in lessons.items()
                  if row[1] == teacher_id or subjects[row[0]][1] == teacher_id]
        # Their subjects with classes going, and any subject going that has none.
        theirs_subjects = ({lessons[lesson_id][0] for lesson_id in theirs}
                           | {class_id for class_id in plan["classes"] if subjects[class_id][1] == teacher_id})
        linked.append({
            "Kind": "Teacher", "Name": teacher_names[teacher_id], "Whole": plan["whole"],
            "Subjects": sorted((subject(class_id) for class_id in theirs_subjects),
                               key=lambda item: item["Name"].lower()),
            "Students": taught(theirs),
            "Past schedules": history.get(teacher_names[teacher_id], 0),
            "Detail": class_rows(theirs),
        })
    picked_subjects = plan["picked_classes"]
    for class_id in sorted(picked_subjects, key=lambda key: subjects[key][0].lower()):
        if subjects[class_id][1] not in teachers:
            linked.append({"Kind": "Subject", "Whole": plan["whole"], **subject(class_id)})
    for lesson_id in sorted(set(lesson_ids) & set(lessons), key=lambda key: lessons[key][2:]):
        class_id, teacher_id, day, start = lessons[lesson_id]
        if teacher_id in teachers or subjects[class_id][1] in teachers or class_id in picked_subjects:
            continue
        linked.append({"Kind": "Class", "Date": day, "Start": start, "Subject": subjects[class_id][0],
                       "Teacher": teacher_names.get(teacher_id, ""), "Students": taught([lesson_id])})

    parent_of, places_of = {}, defaultdict(list)
    for chunk in _chunks(students):
        parent_of.update(session.execute(
            select(Student.id, Parent.name).join(Parent, Parent.id == Student.parent_id)
            .where(Student.id.in_(chunk))).all())
        # Only the places that go: all of them for a whole student, those in the period otherwise.
        for place_id, student_id, day, start, subject_name, teacher_name in session.execute(
            select(SessionAttendance.id, SessionAttendance.student_id, ClassSession.session_date,
                   ClassSession.start_time, AcademyClass.name, Teacher.name)
            .join(ClassSession, ClassSession.id == SessionAttendance.session_id)
            .join(AcademyClass, AcademyClass.id == ClassSession.class_id)
            .join(Teacher, Teacher.id == ClassSession.teacher_id)
            .where(SessionAttendance.student_id.in_(chunk))
        ):
            if place_id in plan["attendance"]:
                places_of[student_id].append({"Date": as_date(day), "Time": f"{as_time(start):%H:%M}",
                                              "Subject": subject_name, "Teacher": teacher_name})
    for student_id in sorted(students, key=by_name(student_names)):
        owned = sorted((key for key, row in bills.items() if row[2] == student_id and key in plan["invoices"]),
                       key=lambda key: (bills[key][0] is None, bills[key][0] or 0))
        waiting = sum(float(row[1]) for row in credits.values() if row[0] == student_id and row[2] == "Open")
        places = sorted(places_of[student_id], key=lambda row: (row["Date"], row["Time"]))
        linked.append({"Kind": "Student", "Name": student_names[student_id], "Whole": plan["whole"],
                       "Parent": parent_of.get(student_id, "") if plan["whole"] else "",
                       "Teachers": sorted({row["Teacher"] for row in places}, key=str.lower),
                       "Invoices": [bill(key) for key in owned], "Credits": round(waiting, 2),
                       "Places": places})
    direct = [key for key in sorted(set(invoice_ids) & plan["invoices"], key=lambda key: bills[key][0] or 0)
              if bills[key][2] not in students]
    lines_of = defaultdict(list)
    for chunk in _chunks(direct):
        for invoice_id, day, saved_name, subject_name, hours, amount in session.execute(
            select(InvoiceItem.invoice_id, InvoiceItem.session_date, InvoiceItem.class_name, AcademyClass.name,
                   InvoiceItem.hours, InvoiceItem.amount)
            .outerjoin(ClassSession, ClassSession.id == InvoiceItem.session_id)
            .outerjoin(AcademyClass, AcademyClass.id == ClassSession.class_id)
            .where(InvoiceItem.invoice_id.in_(chunk))
        ):
            lines_of[invoice_id].append({"Date": as_date(day) if day else None,
                                         "Subject": saved_name or subject_name or "",
                                         "Hours": float(hours) if hours is not None else None,
                                         "Amount": float(amount) if amount is not None else None})
    for invoice_id in direct:
        linked.append({"Kind": "Invoice", **bill(invoice_id),
                       "Lines": sorted(lines_of[invoice_id], key=lambda row: (row["Date"] is None, row["Date"] or date.min))})
    for name in sorted(set(history_names) - {teacher_names[key] for key in teachers}, key=str.lower):
        if history.get(name):
            linked.append({"Kind": "Past schedules", "Name": name, "Rows": history[name]})

    # Everything that reaches students who weren't picked.
    attended = defaultdict(int)
    for chunk in _chunks(lost):
        attended.update(session.execute(
            select(SessionAttendance.student_id, func.count()).where(SessionAttendance.student_id.in_(chunk))
            .group_by(SessionAttendance.student_id)).all())
    changed = defaultdict(lambda: [0, 0.0])
    for chunk in _chunks(plan["items"]):
        for invoice_id, amount in session.execute(
            select(InvoiceItem.invoice_id, InvoiceItem.amount).where(InvoiceItem.id.in_(chunk))
        ):
            if invoice_id in plan["shortened"]:
                changed[invoice_id][0] += 1
                changed[invoice_id][1] += float(amount or 0)
    other_credits = defaultdict(float)
    for student_id, amount, _ in credits.values():
        if student_id not in students:
            other_credits[student_id] += float(amount)
    beyond = {
        "Places": [{"Student": student_names[key], "Classes": lost[key], "None left": attended[key] == lost[key]}
                   for key in sorted(lost, key=by_name(student_names))],
        "Changed": [{**bill(key), "Classes": changed[key][0], "Removed": round(changed[key][1], 2)}
                    for key in sorted(plan["shortened"], key=lambda key: bills[key][0] or 0)],
        "Emptied": [bill(key) for key in sorted(plan["emptied"], key=lambda key: bills[key][0] or 0)
                    if bills[key][2] not in students],
        "Credits": [{"Student": student_names[key], "Amount": round(amount, 2)}
                    for key, amount in sorted(other_credits.items(), key=lambda pair: student_names[pair[0]].lower())],
    }

    sent = [key for key in plan["invoices"] if bills[key][1] == "Issued"]
    effects = {
        "Sent": sorted(bills[key][0] for key in sent if bills[key][0] is not None),
        "Sent list": [bill(key) for key in sorted(sent, key=lambda key: bills[key][0] or 0)],
        "Sent amount": round(sum(nets.get(key, 0.0) for key in sent), 2),
        "Paid amount": round(sum(float(bills[key][3] or 0) for key in sent), 2),
        "Void": sum(1 for key in plan["invoices"] if bills[key][1] == "Void"),
        "Drafts": sum(1 for key in plan["invoices"] if bills[key][1] == "Open"),
        "Parents": sorted((row[0] for row in _lookup(session, Parent.id, (Parent.name,), plan["parents"]).values()),
                          key=str.lower),
        "Comes back": bool(teachers or students),
    }
    return {"Linked": linked, "Beyond": beyond, "Effects": effects}


def describe_master_delete(teacher_ids=(), student_ids=(), class_ids=(), invoice_ids=(),
                           lesson_ids=(), history_names=(), year=None, month=None) -> dict:
    """Return what master_delete would remove and change, and what it's all linked to.

    Changes nothing. Has the same keys as master_delete's summary, plus
    "Linked", "Beyond" and "Effects" from _master_report.
    """
    with SessionLocal() as session:
        plan = _master_plan(session, teacher_ids, student_ids, class_ids, invoice_ids, lesson_ids, history_names,
                            year, month)
        return {**_master_summary(plan),
                **_master_report(session, plan, class_ids, lesson_ids, invoice_ids, history_names)}


def master_delete(teacher_ids=(), student_ids=(), class_ids=(), invoice_ids=(),
                  lesson_ids=(), history_names=(), year=None, month=None) -> dict:
    """Delete what was picked and everything linked to it, sent invoices included.

    `class_ids` are subjects and `lesson_ids` single dated classes;
    `history_names` are teachers whose past schedules go. A `year`, a
    `month` or both keep it inside that time (see _master_plan). All in one
    transaction, so either all of it goes or none of it does. The freed
    invoice numbers can be used again: the next number is worked out from
    the invoices left. Returns the same summary as describe_master_delete.
    """
    with _ONE_ISSUE_AT_A_TIME, SessionLocal() as session:
        _lock_numbers(session)
        plan = _master_plan(session, teacher_ids, student_ids, class_ids, invoice_ids,
                            lesson_ids, history_names, year, month)
        quiet = {"synchronize_session": False}

        for chunk in _chunks(plan["pointing"]):
            session.execute(update(Credit).where(Credit.id.in_(chunk)).values(invoice_id=None),
                            execution_options=quiet)
        for chunk in _chunks(plan["reopened"]):
            session.execute(update(Credit).where(Credit.id.in_(chunk)).values(status="Open", settled_on=None),
                            execution_options=quiet)
        for student_id, lessons in plan["unpaid"].items():
            for chunk in _chunks(lessons):
                session.execute(update(SessionAttendance).where(
                    SessionAttendance.student_id == student_id,
                    SessionAttendance.session_id.in_(chunk)).values(is_paid=False),
                    execution_options=quiet)

        # Rows that point at others go first, so nothing is ever left pointing at a missing row.
        _delete_ids(session, Credit, plan["credits"])
        _delete_ids(session, InvoiceRevision, plan["revisions"])
        _delete_ids(session, InvoiceItem, plan["items"])
        _delete_ids(session, Invoice, plan["invoices"])
        _delete_ids(session, SessionAttendance, plan["attendance"])
        for model in (Enrolment, ClassSchedule, ClassRate, SubjectSpelling):
            _delete_ids(session, model, plan["other"][model])
        _delete_ids(session, ClassSession, plan["lessons"])
        _delete_ids(session, AcademyClass, plan["classes"])
        for model in (ScheduleImport, LessonHistory, StudentAlias, DistinctStudents):
            _delete_ids(session, model, plan["other"][model])
        _delete_ids(session, Student, plan["students"])
        _delete_ids(session, Parent, plan["parents"])
        _delete_ids(session, Teacher, plan["teachers"])
        if plan["drop_choice"]:
            session.execute(delete(Setting).where(Setting.key == NEXT_NUMBER_SETTING),
                            execution_options=quiet)
        session.commit()
        return _master_summary(plan)


def get_invoice_lookup() -> list[dict]:
    """Return every invoice of any time, highest number first and drafts last, to find by number."""
    with SessionLocal() as session:
        rows = [
            {"ID": invoice_id, "Number": number, "Status": status, "Student ID": student_id, "Student": name}
            for invoice_id, number, status, student_id, name in session.execute(
                select(Invoice.id, Invoice.invoice_number, Invoice.status, Invoice.student_id, Student.full_name)
                .join(Student, Student.id == Invoice.student_id)
            )
        ]
    # Sorted here, because Postgres and SQLite put empty numbers at opposite ends.
    return sorted(rows, key=lambda row: (row["Number"] is None, -(row["Number"] or 0), -row["ID"]))


def get_delete_choices(year=None, month=None) -> dict:
    """Return what the Delete records screen offers, for a period or for all time.

    Two trees, the way the records are kept: teachers, each with their
    subjects, each with its classes; and students, each with their invoices.
    A period (a year, a month of any year, or both) narrows every list to
    what it touched: teachers and students with a class in it, subjects
    taught in it, its classes, invoices with a line in it, and teachers with
    past schedules in it. With neither, everything is offered.
    """
    period = bool(year or month)
    with SessionLocal() as session:
        lesson_query = select(ClassSession.id, ClassSession.class_id, ClassSession.teacher_id,
                              ClassSession.session_date, ClassSession.start_time)
        if period:
            lesson_query = lesson_query.where(*_in_period(ClassSession.session_date, year, month))
        lessons = session.execute(lesson_query).all()
        history = session.scalars(select(LessonHistory.teacher_name).distinct().where(
            *_in_period(LessonHistory.lesson_date, year, month))).all()
        teachers = dict(session.execute(select(Teacher.id, Teacher.name)).all())
        subjects = {class_id: (name, teacher_id) for class_id, name, teacher_id in session.execute(
            select(AcademyClass.id, AcademyClass.name, AcademyClass.teacher_id))}
        students = dict(session.execute(select(Student.id, Student.full_name)).all())
        invoices = session.execute(
            select(Invoice.id, Invoice.invoice_number, Invoice.status, Invoice.student_id)).all()

        if period:
            # Only what the period touched. A draft's lines have no date until issued,
            # so its period comes from its classes.
            seated, billed = set(), set()
            for chunk in _chunks([row[0] for row in lessons]):
                seated.update(session.scalars(
                    select(SessionAttendance.student_id).where(SessionAttendance.session_id.in_(chunk))))
                billed.update(session.scalars(
                    select(InvoiceItem.invoice_id).where(InvoiceItem.session_id.in_(chunk))))
            billed.update(session.scalars(select(InvoiceItem.invoice_id).where(
                *_in_period(InvoiceItem.session_date, year, month))))
            taught = {row[1] for row in lessons}
            teaching = {row[2] for row in lessons} | {subjects[class_id][1] for class_id in taught}
            teachers = {key: name for key, name in teachers.items() if key in teaching}
            subjects = {key: row for key, row in subjects.items() if key in taught}
            students = {key: name for key, name in students.items() if key in seated}
            invoices = [row for row in invoices if row[0] in billed]

    def by_name(names):
        """Sort ids by the name they map to."""
        return lambda key: str(names[key]).lower()

    return {
        "Teachers": [{"ID": key, "Name": teachers[key]} for key in sorted(teachers, key=by_name(teachers))],
        "Subjects": [{"ID": key, "Name": name, "Teacher ID": teacher_id}
                     for key, (name, teacher_id) in sorted(subjects.items(), key=lambda pair: pair[1][0].lower())],
        "Classes": [{"ID": lesson_id, "Subject ID": class_id, "Subject": subjects[class_id][0],
                     "Date": as_date(day), "Start": as_time(start)}
                    for lesson_id, class_id, _, day, start in sorted(lessons, key=lambda row: (row[3], row[4]))
                    if class_id in subjects],
        "Students": [{"ID": key, "Name": students[key]} for key in sorted(students, key=by_name(students))],
        # Highest number first, drafts last. Sorted here, because Postgres and SQLite
        # put empty numbers at opposite ends.
        "Invoices": [{"ID": invoice_id, "Number": number, "Status": status, "Student ID": student_id}
                     for invoice_id, number, status, student_id in sorted(
                         invoices, key=lambda row: (row[1] is None, -(row[1] or 0), -row[0]))],
        "History": sorted(history, key=str.lower),
    }
