"""Verify the Data tab's Teaching figures and the past-schedules store behind them.

Runs against a throwaway SQLite database, never the real one. Names are invented.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
_DB = Path(tempfile.mkdtemp()) / "teaching_check.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB.as_posix()}"

import db  # noqa: E402
import teaching  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name, fn):
    """Run one check and record whether it passed, with its message."""
    try:
        results.append((name, True, fn() or ""))
    except Exception as exc:  # noqa: BLE001
        results.append((name, False, f"{type(exc).__name__}: {exc}"))


def lesson(student, day, teacher="Teacher A", cls="G10 Math", status="Attending", hour=16):
    """Return one past lesson row, the shape teaching.py works with."""
    return {"teacher": teacher, "class_name": cls, "date": day, "start": dt.time(hour),
            "end": dt.time(hour + 1), "student": student, "status": status}


# ----------------------------------------------------------------- the store
def _preview(status="Attending"):
    """Return a small parser preview: two G10 Math lessons in May 2025."""
    return [
        {"date": dt.date(2025, 5, 3), "start_time": dt.time(16), "end_time": dt.time(18),
         "class_name": "G10 Math", "attendance": [
             {"student_name": "Nam Jihoon", "status": status},
             {"student_name": "Oh Minseok", "status": "Attending"}]},
        {"date": dt.date(2025, 5, 10), "start_time": dt.time(16), "end_time": dt.time(18),
         "class_name": "G10 Math", "attendance": [
             {"student_name": "Nam Jihoon", "status": "Attending"}]},
    ]


def t_history_store():
    """Past schedules are saved once, updated in place, never billed, and can be removed."""
    db.initialise_database()
    before = db.get_analysis_data_version()
    first = db.save_lesson_history("Teacher A", _preview(), source="a.xlsx")
    assert (first["added"], first["updated"], first["unchanged"]) == (3, 0, 0), first
    again = db.save_lesson_history("teacher a", _preview(), source="a.xlsx")
    assert again["teacher"] == "Teacher A", again
    assert (again["added"], again["updated"], again["unchanged"]) == (0, 0, 3), again
    changed = db.save_lesson_history("Teacher A", _preview(status="Recording"), source="a.xlsx")
    assert (changed["added"], changed["updated"]) == (0, 1), changed
    assert db.get_analysis_data_version() != before, "version did not change"

    summary = db.get_lesson_history_summary()
    assert summary == [{"Teacher": "Teacher A", "Student-lessons": 3, "Students": 2,
                        "From": dt.date(2025, 5, 3), "To": dt.date(2025, 5, 10)}], summary
    history = db.get_analysis_lessons(dt.date(2026, 1, 1))["history"]
    assert len(history) == 3 and {row["status"] for row in history} == {"Recording", "Attending"}
    assert not db.get_invoices() and not db.get_all_classes() and not db.get_all_students(), \
        "past schedules created something billable"
    removed = db.master_delete(history_names=["Teacher A"])["Rows"]
    assert removed == [{"What": "Past schedules (student-lessons)", "How many": 3}], removed
    assert not db.get_lesson_history_summary()
    return "added once, updated not doubled, billed nothing, removable"


def t_combine_prefers_app():
    """A month the app holds is the app's; past schedules fill in the rest, the app's way.

    The past schedules here are a later copy of the workbook: the same lesson
    with the child written "Tae woo" where the app has Jang Taewoo, and a
    lesson moved to another day. Counting both would make one child two and
    one lesson two.
    """
    may3, may17, apr5 = dt.date(2025, 5, 3), dt.date(2025, 5, 17), dt.date(2025, 4, 5)
    app = [lesson("Jang Taewoo", may3, cls="G10 Math (Teacher A)"), lesson("Seo Yerin", may3),
           lesson("Choi Doyun", may3), lesson("Park Hana", may3),
           # Another child, on file as plain "Hana", in another class.
           lesson("Hana", dt.date(2025, 6, 7), cls="G9 English", hour=10)]
    history = [lesson("Tae woo", may3, teacher="teacher a"), lesson("Seo Yerin", may3, teacher="teacher a"),
               lesson("Oh Minseok", may3), lesson("Hana", may3),
               lesson("Seo Yerin", may17),                       # moved in the later copy
               lesson("Tae woo", apr5), lesson("Seo Yerin", apr5),
               lesson("Oh Minseok", apr5), lesson("Hana", apr5)]
    combined, counts = teaching.combine(app, history)
    april = sorted(row["student"] for row in combined if row["date"] == apr5)
    assert april == ["Hana", "Jang Taewoo", "Oh Minseok", "Seo Yerin"], april
    assert not [row for row in combined if row["date"] == may17], "a moved lesson counted twice"
    assert counts == {"app": 5, "history": 4, "history_already_in_app": 5, "spellings_linked": 1}, counts
    may = next(r for r in teaching.monthly_activity(combined)["by_month"] if r["Month"] == 5)
    assert (may["Students"], may["Lessons"]) == (4, 1), may
    return ("a month the app holds is the app's; a respelled child is the app's student "
            "in earlier months; unlike names and names already on file are not tied")


def t_monthly_activity():
    """Each child and lesson counts once a month, and cancelled ones don't count."""
    day = dt.date(2026, 3, 2)
    app = dict(source="app")
    rows = [
        # One group lesson, 16:00-17:00: three students, one cancelled.
        {**lesson("Nam Jihoon", day), **app}, {**lesson("Seo Yerin", day), **app},
        {**lesson("Oh Minseok", day, status="Cancelled"), **app},
        # The same child with a second teacher, written as that workbook did.
        {**lesson("nam jihoon", day, teacher="teacher b", hour=18), "source": "history"},
        {**lesson("Seo Yerin", dt.date(2026, 3, 9), teacher="Teacher B"), **app},
        # A lesson only a cancelled student was booked on doesn't count.
        {**lesson("Oh Minseok", dt.date(2026, 3, 16), status="Cancelled"), **app},
        {**lesson("Oh Minseok", dt.date(2026, 4, 6)), **app},
    ]
    got = teaching.monthly_activity(rows)
    march = next(r for r in got["by_month"] if (r["Year"], r["Month"]) == (2026, 3))
    assert march == {"Year": 2026, "Month": 3, "Students": 2, "Lessons": 3, "Hours": 3.0,
                     "Teachers": 2}, march
    teachers = {(r["Teacher"], r["Month"]): (r["Students"], r["Lessons"]) for r in got["by_teacher"]}
    assert teachers == {("Teacher A", 3): (2, 1), ("Teacher B", 3): (2, 2), ("Teacher A", 4): (1, 1)}, teachers
    return "a child with two teachers once; cancelled not counted; the app's spelling of a teacher"


for name, fn in [
    ("past-schedules store", t_history_store),
    ("app wins over past schedules", t_combine_prefers_app),
    ("what was taught each month", t_monthly_activity),
]:
    check(name, fn)

width = max(len(n) for n, _, _ in results)
failed = sum(1 for _, ok, _ in results if not ok)
print()
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
print(f"\n{len(results) - failed}/{len(results)} passed")
db.engine.dispose()
sys.exit(1 if failed else 0)
