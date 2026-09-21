"""Student retention: who stops coming, and who looks likely to next.

Pure functions over plain lesson rows -- no Streamlit, no database -- so the
same code reads the app's own records, past schedules kept for analysis, or a
hand-built table in a test. A lesson row is a dict with ``teacher``,
``class_name``, ``date``, ``start``, ``end``, ``student`` and ``status`` (one
of the parser's Attending / Online / Recording / Cancelled).

The data is monthly, so the model is too. Each row is one student in one month
they came, described only by what was known by the end of that month, and
labelled by what happened next: did they come back? That is a discrete-time
survival model -- logistic regression on student-months -- rather than a Cox
model, for two reasons. Months tie constantly (dozens of students share "left
after month 3"), which a monthly model handles exactly and Cox only
approximates; and it answers what the academy actually asks, "how likely is
this student not to come back?", as a probability for each current student.

Two rules keep the labels honest:

* Leaving means no lesson for ``GRACE_MONTHS`` months while their teacher's
  records carry on, and never coming back. Missing a single month is not
  leaving -- plenty of students return after a break. A student whose last
  lesson is too recent to tell, or whose teacher's workbook has a gap just
  then, is left unlabelled rather than guessed at: missing records are not a
  student leaving.
* A Grade 12 student whose last lesson falls in May or June has finished
  school, not left. Counted as churn, graduation makes the final year look
  like the problem when it is simply the end.
"""

from __future__ import annotations

import calendar
import datetime as dt
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import numpy as np

import schedule_parser
from schedule_parser import CANCELLED, ONLINE, RECORDING

# The same few thousand names and class names come round once per lesson, and
# the report reads each lesson several times over; at twenty times this
# academy's history, cleaning names up again was half the time it took.
normalise_name = lru_cache(maxsize=65536)(schedule_parser.normalise_name)
grade_of = lru_cache(maxsize=4096)(schedule_parser.grade_of)

GRACE_MONTHS = 2
RIDGE = 1.0
MIN_LEAVERS = 20      # below this, neither the model nor its check means much
AT_RISK_ROWS = 15

# (key, label, step an odds ratio is reported for, how that step reads,
#  reason when the student is high on it, reason when low)
FEATURES = [
    ("first_month", "In their first month", 1.0, "yes vs no", "first month", "past their first month"),
    ("log_tenure", "Months with the academy", math.log(2), "each doubling", "long-standing", "new"),
    ("lessons", "Lessons that month", 1.0, "each extra lesson", "many lessons", "few lessons"),
    ("below_usual", "Lessons below their usual", 1.0, "each lesson fewer",
     "fewer lessons than usual", "keeping up their lessons"),
    ("recording_share", "Watched as recordings", 0.25, "25 more points",
     "watching recordings", "attending live"),
    ("online_share", "Joined online", 0.25, "25 more points", "joining online", "attending in person"),
    ("cancel_share", "Cancelled", 0.25, "25 more points", "cancelling", "not cancelling"),
    ("one_to_one_share", "One-to-one lessons", 0.25, "25 more points",
     "one-to-one lessons", "group lessons"),
    ("several_teachers", "With more than one teacher", 1.0, "yes vs no",
     "several teachers", "a single teacher"),
    ("grade_12", "In Grade 12", 1.0, "yes vs no", "Grade 12", "below Grade 12"),
    ("grade_10_or_below", "In Grade 10 or below", 1.0, "yes vs no", "Grade 10 or below", "Grade 11 or above"),
    ("summer", "June or July", 1.0, "yes vs no", "summer months", "term time"),
]
FEATURE_KEYS = [feature[0] for feature in FEATURES]


def month_index(day: dt.date) -> int:
    return day.year * 12 + day.month - 1


def month_label(index: int) -> str:
    return f"{calendar.month_abbr[index % 12 + 1]} {index // 12}"


def combine(app_lessons: list[dict], history_lessons: list[dict]) -> tuple[list[dict], dict]:
    """Every lesson once. Where past schedules repeat a lesson the app holds, the app wins.

    A lesson is the same when the teacher, date, start time and student match;
    class names are left out of that, because an import may have renamed the
    class the workbook still calls something else.
    """
    def key(row):
        return (row["teacher"].casefold(), row["date"], row["start"], normalise_name(row["student"]))

    seen = {key(row) for row in app_lessons}
    extra = [row for row in history_lessons if key(row) not in seen]
    return app_lessons + extra, {
        "app": len(app_lessons),
        "history": len(extra),
        "history_already_in_app": len(history_lessons) - len(extra),
    }


def student_months(lessons: list[dict], today: dt.date) -> list[dict[str, Any]]:
    """One row per student per month they came, with what followed.

    ``label`` is 1 when the student never came back, 0 when they did, and
    None when it can't be told (too recent, or their teacher has no records
    for the months after) or shouldn't be counted (graduating). Every feature uses that month and the months before it,
    never after, so a row is exactly what could have been known at the time.
    Students are matched across teachers by name.
    """
    lessons = [row for row in lessons if row["date"] <= today]
    booked: dict[tuple, set] = defaultdict(set)
    for row in lessons:
        booked[_lesson_key(row)].add(normalise_name(row["student"]))

    observed: dict[str, set] = defaultdict(set)   # teacher -> months with any lesson on record
    students: dict[str, dict] = {}
    for row in lessons:
        key = normalise_name(row["student"])
        if not key:
            continue
        month = month_index(row["date"])
        observed[row["teacher"].casefold()].add(month)
        student = students.setdefault(key, {"names": Counter(), "months": {}})
        student["names"][row["student"]] += 1
        tally = student["months"].setdefault(month, {
            "attended": 0, "recording": 0, "online": 0, "cancelled": 0,
            "one_to_one": 0, "teachers": set(), "grade": None})
        grade = grade_of(row["class_name"])
        if grade:
            tally["grade"] = max(tally["grade"] or 0, grade)
        if row["status"] == CANCELLED:
            tally["cancelled"] += 1
            continue
        tally["attended"] += 1
        tally["teachers"].add(row["teacher"])
        tally["recording"] += row["status"] == RECORDING
        tally["online"] += row["status"] == ONLINE
        tally["one_to_one"] += len(booked[_lesson_key(row)]) == 1

    rows = []
    for key, student in students.items():
        active = sorted(m for m, tally in student["months"].items() if tally["attended"])
        if not active:
            continue
        name = student["names"].most_common(1)[0][0]
        grade, history = None, []
        for position, month in enumerate(active):
            tally = student["months"][month]
            if tally["grade"]:
                grade = max(grade or 0, tally["grade"])
            attended = tally["attended"]
            usual = sum(history) / len(history) if history else float(attended)
            booked_count = attended + tally["cancelled"]
            came_back = position + 1 < len(active)
            graduating = grade == 12 and month % 12 + 1 in (5, 6) and not came_back
            teachers = {teacher.casefold() for teacher in tally["teachers"]}
            watched = all(
                any(later in observed[teacher] for teacher in teachers)
                for later in range(month + 1, month + 1 + GRACE_MONTHS)
            )
            if came_back:
                label = 0
            elif watched and not graduating:
                label = 1
            else:
                label = None
            rows.append({
                "key": key,
                "student": name,
                "month": month,
                "teachers": sorted(tally["teachers"]),
                "tenure": position + 1,
                "attended": attended,
                "usual": usual,
                "label": label,
                "graduating": graduating,
                "last": not came_back,
                "features": {
                    "first_month": float(position == 0),
                    "log_tenure": math.log(position + 1),
                    "lessons": float(attended),
                    "below_usual": max(0.0, usual - attended),
                    "recording_share": tally["recording"] / attended,
                    "online_share": tally["online"] / attended,
                    "cancel_share": tally["cancelled"] / booked_count,
                    "one_to_one_share": tally["one_to_one"] / attended,
                    "several_teachers": float(len(tally["teachers"]) > 1),
                    "grade_12": float(grade == 12),
                    "grade_10_or_below": float(grade is not None and grade <= 10),
                    "summer": float(month % 12 + 1 in (6, 7)),
                },
            })
            history.append(attended)
    return rows


def by_month(rows: list[dict], latest: int) -> list[dict[str, Any]]:
    """Each calendar month: who came, who was new, and who did not come back.

    The curve elsewhere counts months since a student's first lesson, which
    answers "how long do students stay" and cannot answer "is it getting
    worse". This does: one row per calendar month, in the academy's own time.

    A month is only ``settled`` once ``GRACE_MONTHS`` have passed after it,
    since before that a quiet student cannot be told from one who left. The
    unsettled months are still returned -- their attendance is real -- but
    their leaver count is the floor, not the figure, and the screen says so.
    """
    counts: dict[int, dict[str, Any]] = {}
    for row in rows:
        month = counts.setdefault(row["month"], {
            "month": row["month"], "label": month_label(row["month"]),
            "active": 0, "joined": 0, "left": 0, "unknown": 0, "settled": True,
        })
        month["active"] += 1
        month["joined"] += row["features"]["first_month"] == 1.0
        if row["label"] == 1:
            month["left"] += 1
        elif row["label"] is None:
            month["unknown"] += 1
    out = []
    for month in sorted(counts):
        item = counts[month]
        item["settled"] = month <= latest - GRACE_MONTHS
        item["left_share"] = item["left"] / item["active"] if item["active"] else 0.0
        out.append(item)
    return out


def by_lessons(rows: list[dict], most: int = 5) -> list[dict[str, Any]]:
    """How often a month with so many lessons turned out to be a student's last.

    Plain counting, no model: the one thing on the page a person can act on
    without taking anything on trust.
    """
    buckets: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row["label"] is None:
            continue
        count = min(int(row["attended"]), most)
        bucket = buckets.setdefault(count, {"lessons": count, "months": 0, "last": 0})
        bucket["months"] += 1
        bucket["last"] += row["label"]
    out = []
    for count in sorted(buckets):
        bucket = buckets[count]
        bucket["share"] = bucket["last"] / bucket["months"] if bucket["months"] else 0.0
        bucket["label"] = f"{count}+" if count == most else str(count)
        out.append(bucket)
    return out


def _lesson_key(row: dict) -> tuple:
    return (row["teacher"].casefold(), row["date"], row["start"], row["class_name"].casefold())


def _matrix(rows: list[dict]) -> np.ndarray:
    return np.array([[row["features"][key] for key in FEATURE_KEYS] for row in rows], dtype=float)


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -35.0, 35.0)))


@dataclass
class LogisticModel:
    """Ridge logistic regression, fitted on standardised features."""

    mean: np.ndarray
    scale: np.ndarray
    weights: np.ndarray       # intercept first
    covariance: np.ndarray    # inverse of the penalised information matrix

    def probability(self, X: np.ndarray) -> np.ndarray:
        return _sigmoid(self.weights[0] + ((X - self.mean) / self.scale) @ self.weights[1:])

    def contributions(self, X: np.ndarray) -> np.ndarray:
        """How far each feature moves each row's log-odds from an average student."""
        return ((X - self.mean) / self.scale) * self.weights[1:]


def fit_logistic(X: np.ndarray, y, ridge: float = RIDGE, iterations: int = 100) -> LogisticModel:
    """Newton's method on the ridge-penalised log-likelihood.

    Features are standardised first, so one penalty treats them evenly; the
    intercept is not penalised. A small penalty keeps the fit stable with a
    few hundred leavers and features that overlap (a first month is also a
    short tenure).
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    mean = X.mean(axis=0)
    scale = X.std(axis=0)
    scale[scale == 0] = 1.0
    design = np.column_stack([np.ones(len(X)), (X - mean) / scale])
    penalty = np.full(design.shape[1], float(ridge))
    penalty[0] = 0.0
    rate = min(max(float(y.mean()), 1e-6), 1 - 1e-6)
    weights = np.zeros(design.shape[1])
    weights[0] = math.log(rate / (1 - rate))
    for _ in range(iterations):
        p = _sigmoid(design @ weights)
        gradient = design.T @ (p - y) + penalty * weights
        information = (design * (p * (1 - p))[:, None]).T @ design + np.diag(penalty)
        step = np.linalg.lstsq(information, gradient, rcond=None)[0]
        weights = weights - step
        if np.abs(step).max() < 1e-10:
            break
    p = _sigmoid(design @ weights)
    information = (design * (p * (1 - p))[:, None]).T @ design + np.diag(penalty)
    return LogisticModel(mean, scale, weights, np.linalg.pinv(information))


def auc(labels, scores) -> float | None:
    """Chance a random leaver scores above a random stayer (ties count half)."""
    labels = np.asarray(labels, dtype=bool)
    scores = np.asarray(scores, dtype=float)
    leavers, stayers = int(labels.sum()), int((~labels).sum())
    if not leavers or not stayers:
        return None
    _, inverse, counts = np.unique(scores, return_inverse=True, return_counts=True)
    ends = np.cumsum(counts)
    ranks = ((ends - counts + 1 + ends) / 2)[inverse]
    return float((ranks[labels].sum() - leavers * (leavers + 1) / 2) / (leavers * stayers))


def kaplan_meier(durations, events) -> tuple[list[dict], int | None]:
    """The share still enrolled after each number of months, and the median stay."""
    durations = np.asarray(durations, dtype=int)
    events = np.asarray(events, dtype=bool)
    retained = 1.0
    curve = [{"month": 0, "retained": 1.0, "at_risk": int(len(durations))}]
    for month in np.unique(durations):
        at_risk = int((durations >= month).sum())
        left = int(((durations == month) & events).sum())
        if left:
            retained *= 1 - left / at_risk
        curve.append({"month": int(month), "retained": retained, "at_risk": at_risk})
    median = next((point["month"] for point in curve if point["retained"] <= 0.5), None)
    return curve, median


def odds_ratios(model: LogisticModel) -> list[dict[str, Any]]:
    """Each feature's odds ratio for not coming back, per a readable step, with a rough 95% range.

    The range comes from the penalised fit's curvature, so it is approximate;
    it is there to separate clear effects from noise, not to be quoted to
    three decimals.
    """
    out = []
    for index, (key, label, step, per, _, _) in enumerate(FEATURES):
        beta = model.weights[index + 1] / model.scale[index]
        spread = 1.96 * math.sqrt(max(model.covariance[index + 1, index + 1], 0.0)) / model.scale[index]
        out.append({
            "feature": key,
            "label": label,
            "per": per,
            "odds_ratio": math.exp(beta * step),
            "low": math.exp((beta - spread) * step),
            "high": math.exp((beta + spread) * step),
        })
    return out


def validate(lessons: list[dict], today: dt.date) -> dict[str, Any] | None:
    """Fit as if it were an earlier date, then score the months that came after.

    The model is refitted on lessons before a cutoff only -- labels included,
    so a student who came back after the cutoff counts as unknown, exactly as
    it would have then -- and tested on the most recent months holding about
    three in ten of the labelled rows. None when there is too little of either.
    """
    rows = [row for row in student_months(lessons, today) if row["label"] is not None]
    if not rows:
        return None
    per_month = Counter(row["month"] for row in rows)
    cutoff, held = None, 0
    for month in sorted(per_month, reverse=True):
        held += per_month[month]
        cutoff = month
        if held >= 0.3 * len(rows):
            break
    cutoff_day = dt.date(cutoff // 12, cutoff % 12 + 1, 1)
    train = [row for row in student_months([r for r in lessons if r["date"] < cutoff_day],
                                           cutoff_day - dt.timedelta(days=1))
             if row["label"] is not None]
    test = [row for row in rows if row["month"] >= cutoff]
    train_leavers = sum(row["label"] for row in train)
    test_labels = np.array([row["label"] for row in test])
    if (train_leavers < MIN_LEAVERS or len(train) - train_leavers < MIN_LEAVERS
            or test_labels.sum() < 5 or len(test) - test_labels.sum() < 5):
        return None
    model = fit_logistic(_matrix(train), [row["label"] for row in train])
    risk = model.probability(_matrix(test))
    top = max(1, round(0.2 * len(test)))
    return {
        "auc": auc(test_labels, risk),
        "trained_through": month_label(cutoff - 1),
        "tested_from": month_label(cutoff),
        "tested_to": month_label(max(per_month)),
        "train_rows": len(train),
        "test_rows": len(test),
        "test_leavers": int(test_labels.sum()),
        "predicted_leavers": float(risk.sum()),
        "captured_top_fifth": float(test_labels[np.argsort(-risk)[:top]].sum() / test_labels.sum()),
    }


def retention_report(lessons: list[dict], today: dt.date) -> dict[str, Any]:
    """Everything the Data tab shows about retention, as of the end of last month.

    The month in progress is left out until it ends: halfway through it, every
    student looks as if they came to half their usual lessons.
    """
    as_of = today.replace(day=1) - dt.timedelta(days=1)
    rows = student_months(lessons, as_of)
    labelled = [row for row in rows if row["label"] is not None]
    leavers = sum(row["label"] for row in labelled)
    students = {row["key"] for row in rows}
    if leavers < MIN_LEAVERS or len(labelled) - leavers < MIN_LEAVERS:
        return {
            "enough": False,
            "students": len(students),
            "leavers": leavers,
            "message": (
                f"Not enough history yet: {len(students)} students, {leavers} of whom are known "
                f"to have left. The model needs at least {MIN_LEAVERS} leavers and "
                f"{MIN_LEAVERS} students who stayed — add past schedules under Past schedules."
            ),
        }

    model = fit_logistic(_matrix(labelled), [row["label"] for row in labelled])

    first_month = {}
    for row in rows:
        first_month[row["key"]] = min(first_month.get(row["key"], row["month"]), row["month"])
    last_rows = [row for row in rows if row["last"]]
    curve, median = kaplan_meier(
        [row["month"] - first_month[row["key"]] + 1 for row in last_rows],
        [row["label"] == 1 for row in last_rows],
    )

    latest = month_index(as_of)
    months = by_month(rows, latest)
    recent = [row for row in last_rows if row["label"] is None and row["month"] >= latest - 1]
    current = [row for row in recent if not row["graduating"]]
    # The month in progress is kept out of the model, because halfway through
    # it everybody looks as though they came to half their usual lessons. It
    # is still evidence of one thing: a student who sat in class this week has
    # come back. Naming them as likely to leave is how a list stops being
    # believed, so they are set aside here and counted separately.
    came_back = {
        normalise_name(lesson["student"])
        for lesson in lessons
        if lesson["date"] > as_of and lesson["status"] != CANCELLED
    }
    waiting = [row for row in current if row["key"] not in came_back]
    # When each of them was last in, so the screen can say how long it has
    # been rather than only which month it was.
    last_seen: dict[str, dt.date] = {}
    for lesson in lessons:
        if lesson["status"] == CANCELLED:
            continue
        key = normalise_name(lesson["student"])
        if lesson["date"] > last_seen.get(key, dt.date.min):
            last_seen[key] = lesson["date"]
    at_risk = []
    expected = 0.0
    if waiting:
        X = _matrix(waiting)
        risk = model.probability(X)
        pushes = model.contributions(X)
        expected = float(risk.sum())
        for index in np.argsort(-risk)[:AT_RISK_ROWS]:
            row = waiting[index]
            reasons = [
                FEATURES[f][4] if X[index, f] > model.mean[f] else FEATURES[f][5]
                for f in np.argsort(-pushes[index])[:2] if pushes[index, f] > 0.1
            ]
            at_risk.append({
                "student": row["student"],
                "teachers": row["teachers"],
                "months": row["tenure"],
                "last_month": month_label(row["month"]),
                "lessons": row["attended"],
                "usual": round(row["usual"], 1),
                "last_seen": last_seen.get(row["key"]),
                "risk": float(risk[index]),
                "reasons": reasons,
            })

    return {
        "enough": True,
        "students": len(students),
        "student_months": len(rows),
        "labelled": len(labelled),
        "leavers": leavers,
        "current": len(current),
        "came_back": len(current) - len(waiting),
        "graduating": len(recent) - len(current),
        "expected_leavers": expected,
        "months": months,
        "by_lessons": by_lessons(labelled),
        "km": curve,
        "median_months": median,
        "drivers": odds_ratios(model),
        "validation": validate(lessons, as_of),
        "at_risk": at_risk,
        "first_month": month_label(min(row["month"] for row in rows)),
        "last_month": month_label(latest),
        "grace_months": GRACE_MONTHS,
    }
