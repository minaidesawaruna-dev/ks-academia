"""Verify the retention analysis: its arithmetic, its labels, and the past-schedules store.

Runs against a throwaway SQLite database, never the real one. Names are invented.
"""
from __future__ import annotations

import datetime as dt
import math
import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
_DB = Path(tempfile.mkdtemp()) / "retention_check.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB.as_posix()}"

import numpy as np  # noqa: E402

import db  # noqa: E402
import retention as rt  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name, fn):
    try:
        results.append((name, True, fn() or ""))
    except Exception as exc:  # noqa: BLE001
        results.append((name, False, f"{type(exc).__name__}: {exc}"))


def lesson(student, day, teacher="Teacher A", cls="G10 Math", status="Attending", hour=16):
    return {"teacher": teacher, "class_name": cls, "date": day, "start": dt.time(hour),
            "end": dt.time(hour + 1), "student": student, "status": status}


def monthly(student, year, first_month, count, per_month=4, **kw):
    out = []
    for offset in range(count):
        index = year * 12 + first_month - 1 + offset
        for week in range(per_month):
            out.append(lesson(student, dt.date(index // 12, index % 12 + 1, 3 + 7 * week), **kw))
    return out


# ------------------------------------------------------------------ arithmetic
def t_grades():
    cases = {"G11 Econs HL": 11, "G12(Y6) Econs SL": 12, "Y3(G9) ACSI": 9, "Y5 Chemi SL": 11,
             "Pre-G9 UWC": 9, "Eng 1:1": None, "IGCSE Lit": None, "Basic Eng A": None}
    for name, want in cases.items():
        assert rt.grade_of(name) == want, (name, rt.grade_of(name))
    return f"{len(cases)} class names"


def t_kaplan_meier():
    curve, median = rt.kaplan_meier([1, 1, 2, 3, 3], [1, 0, 1, 1, 0])
    got = [round(point["retained"], 4) for point in curve]
    assert got == [1.0, 0.8, 0.5333, 0.2667], got
    assert median == 3, median
    return "hand-worked survival curve and median reproduced"


def t_logistic_recovers_odds_ratio():
    # 30 of 100 leave with the factor, 10 of 100 without: odds ratio (30/70)/(10/90).
    X = np.array([[1.0]] * 100 + [[0.0]] * 100)
    y = np.array([1] * 30 + [0] * 70 + [1] * 10 + [0] * 90)
    model = rt.fit_logistic(X, y, ridge=0.0)
    got = math.exp(model.weights[1] / model.scale[0])
    want = (30 / 70) / (10 / 90)
    assert abs(got - want) < 1e-6, (got, want)
    p = model.probability(X)
    assert abs(p[:100].mean() - 0.30) < 1e-6 and abs(p[100:].mean() - 0.10) < 1e-6
    return f"odds ratio {got:.4f} = closed form {want:.4f}"


def t_auc():
    assert rt.auc([1, 1, 0, 0], [0.9, 0.4, 0.4, 0.1]) == 0.875
    assert rt.auc([1, 1], [0.2, 0.3]) is None
    return "ties count half; one-class input refused"


# --------------------------------------------------------------------- labels
def _labelled_students():
    lessons = (
        monthly("Nam Jihoon", 2025, 1, 6)                   # Jan-Jun, never back: left
        + monthly("Oh Minseok", 2025, 1, 3)                 # Jan-Mar ...
        + monthly("Oh Minseok", 2025, 5, 8)                 # ... gap, then May-Dec
        + monthly("Seo Yerin", 2025, 1, 5, cls="G12 Econs") # Jan-May, Grade 12: graduated
        + monthly("Park Sohee", 2025, 1, 10)                # Jan-Oct: gone two months, left
        + monthly("Choi Doyun", 2025, 1, 11)                # Jan-Nov: too recent to tell
    )
    return {(row["student"], rt.month_label(row["month"])): row
            for row in rt.student_months(lessons, dt.date(2025, 12, 31))}


def t_labels():
    rows = _labelled_students()
    want = {("Nam Jihoon", "May 2025"): 0, ("Nam Jihoon", "Jun 2025"): 1,
            ("Oh Minseok", "Mar 2025"): 0, ("Oh Minseok", "Dec 2025"): None,
            ("Seo Yerin", "May 2025"): None, ("Park Sohee", "Oct 2025"): 1,
            ("Choi Doyun", "Nov 2025"): None}
    for key, label in want.items():
        assert rows[key]["label"] == label, (key, rows[key]["label"], label)
    assert rows[("Seo Yerin", "May 2025")]["graduating"]
    return "left, came back, graduated and too-recent all told apart"


def t_gap_is_not_leaving():
    lessons = (monthly("Nam Jihoon", 2025, 1, 3, teacher="Teacher B")
               + monthly("Oh Minseok", 2025, 1, 3, teacher="Teacher B")
               + monthly("Oh Minseok", 2025, 10, 3, teacher="Teacher B"))
    rows = {(r["student"], rt.month_label(r["month"])): r["label"]
            for r in rt.student_months(lessons, dt.date(2025, 12, 31))}
    assert rows[("Nam Jihoon", "Mar 2025")] is None, rows      # B has no records Apr-Sep
    assert rows[("Oh Minseok", "Mar 2025")] == 0, rows
    return "months with no records for the teacher are unknown, not leaving"


def t_no_look_ahead():
    full = monthly("Nam Jihoon", 2025, 1, 6)
    cut = [row for row in full if row["date"] < dt.date(2025, 4, 1)]
    later = {rt.month_label(r["month"]): r["features"]
             for r in rt.student_months(full, dt.date(2025, 12, 31))}
    then = {rt.month_label(r["month"]): r["features"]
            for r in rt.student_months(cut, dt.date(2025, 3, 31))}
    assert later["Mar 2025"] == then["Mar 2025"], (later["Mar 2025"], then["Mar 2025"])
    return "a month's features are the same whether or not later months exist"


def t_features():
    day = dt.date(2025, 3, 3)
    lessons = (
        monthly("Nam Jihoon", 2025, 1, 2)
        + [lesson("Nam Jihoon", day), lesson("Nam Jihoon", day, status="Recording", hour=18),
           lesson("Nam Jihoon", day, status="Cancelled", hour=19),
           lesson("Oh Minseok", day, hour=18), lesson("Nam Jihoon", day, teacher="Teacher B", hour=20)]
    )
    rows = {rt.month_label(r["month"]): r for r in rt.student_months(lessons, dt.date(2025, 3, 31))
            if r["student"] == "Nam Jihoon"}
    march = rows["Mar 2025"]["features"]
    assert march["lessons"] == 3 and march["below_usual"] == 1.0, march
    assert abs(march["recording_share"] - 1 / 3) < 1e-9, march
    assert march["cancel_share"] == 0.25, march
    assert abs(march["one_to_one_share"] - 2 / 3) < 1e-9, march
    assert march["several_teachers"] == 1.0 and march["first_month"] == 0.0, march
    return "lessons below usual, shares, one-to-one and teachers counted as defined"


# ---------------------------------------------------------------------- model
def _simulated(seed=7):
    """Three teachers, two years, with two patterns planted.

    Students who watch recordings leave far more often, and most students who
    are about to stop drift first -- one lesson in their last month instead of
    four. Neither is known to the model; it has to find both.
    """
    rng = np.random.default_rng(seed)
    lessons = []
    for number in range(420):
        teacher = f"Teacher {'ABC'[number % 3]}"
        student = f"Student {number:03d}"
        records = rng.random() < 0.3
        month = 2024 * 12 + int(rng.integers(0, 18))
        while month <= 2025 * 12 + 11:
            leaving = rng.random() < (0.30 if records else 0.06)
            weeks = 1 if leaving and rng.random() < 0.8 else 4
            for week in range(weeks):
                status = "Recording" if records and week < 3 else "Attending"
                lessons.append(lesson(student, dt.date(month // 12, month % 12 + 1, 3 + 7 * week),
                                      teacher=teacher, status=status, hour=9 + number % 9))
            if leaving:
                break
            month += 1
    return lessons


def t_report_finds_planted_effect():
    report = rt.retention_report(_simulated(), dt.date(2025, 12, 31))
    assert report["enough"], report.get("message")
    drivers = {d["feature"]: d for d in report["drivers"]}
    assert drivers["recording_share"]["low"] > 1, drivers["recording_share"]
    # The drift shows as few lessons, lessons below usual, or both. The two
    # overlap, so the model may credit either -- but it must find it.
    assert drivers["lessons"]["high"] < 1 or drivers["below_usual"]["low"] > 1, drivers
    assert report["last_month"] == "Nov 2025", report["last_month"]
    validation = report["validation"]
    assert validation and validation["auc"] > 0.8, validation
    assert report["at_risk"] and 0 <= report["at_risk"][0]["risk"] <= 1
    assert report["km"][0]["retained"] == 1.0 and report["median_months"]
    return (f"both planted patterns found; on later months it hadn't seen, "
            f"AUC {validation['auc']:.2f}")


def t_too_little_data():
    report = rt.retention_report(monthly("Nam Jihoon", 2025, 1, 3), dt.date(2025, 12, 31))
    assert not report["enough"] and "Past schedules" in report["message"], report
    return "a clear message instead of a model fitted on nothing"


# ----------------------------------------------------------------- the store
def _preview(status="Attending"):
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
    assert db.delete_lesson_history("Teacher A") == 3 and not db.get_lesson_history_summary()
    return "added once, updated not doubled, billed nothing, removable"


def t_combine_prefers_app():
    app = [lesson("Nam Jihoon", dt.date(2025, 5, 3), cls="G10 Math (Teacher A)")]
    history = [lesson("nam  jihoon", dt.date(2025, 5, 3), teacher="teacher a", cls="G10 Math"),
               lesson("Oh Minseok", dt.date(2025, 5, 3))]
    combined, counts = rt.combine(app, history)
    assert len(combined) == 2 and counts == {"app": 1, "history": 1, "history_already_in_app": 1}
    return "a lesson in both is counted once, from the app"


for name, fn in [
    ("grade read from class names", t_grades),
    ("Kaplan-Meier by hand", t_kaplan_meier),
    ("logistic fit = closed form", t_logistic_recovers_odds_ratio),
    ("AUC with ties", t_auc),
    ("leaving vs gap vs graduation", t_labels),
    ("a gap in records isn't leaving", t_gap_is_not_leaving),
    ("no look-ahead in features", t_no_look_ahead),
    ("features as defined", t_features),
    ("planted effect recovered", t_report_finds_planted_effect),
    ("too little data", t_too_little_data),
    ("past-schedules store", t_history_store),
    ("app wins over past schedules", t_combine_prefers_app),
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
