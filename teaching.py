"""What the academy taught: every lesson on record, month by month.

Pure functions over plain lesson rows -- no Streamlit, no database -- so the
same code reads the app's own records, past schedules kept for analysis, or a
hand-built table in a test. A lesson row is a dict with ``teacher``,
``class_name``, ``date``, ``start``, ``end``, ``student`` and ``status`` (one
of the parser's Attending / Online / Recording / Cancelled).
"""

from __future__ import annotations

import difflib
import re
from collections import defaultdict
from functools import lru_cache
from typing import Any

import schedule_parser
from schedule_parser import CANCELLED

# The same few thousand names come round once per lesson; cleaning them up
# again each time was most of the time this took.
normalise_name = lru_cache(maxsize=65536)(schedule_parser.normalise_name)


def _said_alike(written: str, on_file: str) -> bool:
    """Whether two spellings are one child: the upload screen's own tests -- the
    same name said another way, cut short or with a name added, a letter or two
    off, a bracketed tag apart, a Hangul name that fits the English one -- plus
    the two ways teachers shorten a name within one class: the given name alone
    with its syllables spaced ("Tae woo" for Jang Taewoo), or with a letter
    off ("Dahey" for Kim Dahye)."""
    sound = schedule_parser.name_sound(written)
    if ((sound and sound == schedule_parser.name_sound(on_file))
            or schedule_parser._bare(written) == schedule_parser._bare(on_file)
            or schedule_parser._shortened(written, on_file)
            or schedule_parser._shortened(on_file, written)
            or schedule_parser._close_spelling(written, on_file)
            or schedule_parser.hangul_fit(written, on_file)
            or schedule_parser.hangul_fit(on_file, written)):
        return True
    mine, theirs = schedule_parser._name_words(written), schedule_parser._name_words(on_file)
    # Folded as one word, since "Tae woo" folds differently syllable by syllable.
    joined = schedule_parser._fold("".join(re.findall(r"[A-Za-z]+", schedule_parser.without_brackets(written))))
    if len(mine) >= 2 and len(theirs) >= 2 and len(joined) >= 4 and joined in theirs:
        return True
    # And the other way: "Minjae" for Min Jae Choi.
    raw = re.findall(r"[A-Za-z]+", schedule_parser.without_brackets(on_file))
    if len(mine) == 1 and len(joined) >= 5 and any(
            schedule_parser._fold(raw[i] + raw[i + 1]) == joined for i in range(len(raw) - 1)):
        return True
    return len(mine) == 1 and len(theirs) >= 2 and len(joined) >= 5 and any(
        len(word) >= 5 and difflib.SequenceMatcher(None, joined, word).ratio() >= schedule_parser.MERGE_THRESHOLD
        for word in theirs)


def link_spellings(app_lessons: list[dict], history_lessons: list[dict]) -> dict[tuple[str, str], str]:
    """Past-schedule spellings of children the app holds under another name.

    Learned only from lessons in both -- same teacher, date and start. On such
    a lesson, a name the past schedule has and the app doesn't is tied to the
    one name the app has and the past schedule doesn't that passes
    ``_said_alike``. Never tied: a spelling that is itself a student's name in
    the app (another "Hana" may be exactly who it means), or one that points
    at two different students anywhere. A wrong tie would merge two children,
    so a spelling nothing ties stays as written.

    Returns ``{(teacher casefolded, spelling normalised): the app's name}``.
    """
    def roster(rows):
        out: dict[tuple, dict[str, str]] = defaultdict(dict)
        for row in rows:
            key = normalise_name(row["student"])
            if key:
                out[(row["teacher"].casefold(), row["date"], row["start"])][key] = row["student"]
        return out

    app, past = roster(app_lessons), roster(history_lessons)
    in_app = {key for lesson in app.values() for key in lesson}
    votes: dict[tuple[str, str], set[str]] = defaultdict(set)
    for lesson, written in past.items():
        on_file = app.get(lesson)
        if not on_file:
            continue
        free = [name for key, name in on_file.items() if key not in written]
        for key, name in written.items():
            if key in in_app:
                continue
            fits = [other for other in free if _said_alike(name, other)]
            if len(fits) == 1:
                votes[(lesson[0], key)].add(fits[0])
    return {spelling: next(iter(names)) for spelling, names in votes.items() if len(names) == 1}


def combine(app_lessons: list[dict], history_lessons: list[dict]) -> tuple[list[dict], dict]:
    """Every lesson once, taken from the app wherever the app has the month.

    A month the app holds for a teacher is the app's alone: that teacher's
    past schedules for the month are left out. The past schedules are often a
    later copy of the workbook the app imported, so taking both would count a
    moved lesson twice, and a child written one way in each as two children.
    Past schedules fill in only the months the app doesn't have, with any
    spelling that ``link_spellings`` ties to an app student written the app's
    way, so the same child is one student from their first lesson on record.
    """
    held = {(row["teacher"].casefold(), row["date"].year, row["date"].month) for row in app_lessons}
    links = link_spellings(app_lessons, history_lessons)
    extra = []
    for row in history_lessons:
        teacher = row["teacher"].casefold()
        if (teacher, row["date"].year, row["date"].month) in held:
            continue
        name = links.get((teacher, normalise_name(row["student"])))
        extra.append({**row, "student": name} if name else row)
    return app_lessons + extra, {
        "app": len(app_lessons),
        "history": len(extra),
        "history_already_in_app": len(history_lessons) - len(extra),
        "spellings_linked": len(links),
    }


def monthly_activity(lessons: list[dict]) -> dict[str, list[dict[str, Any]]]:
    """What was taught each month: students, lessons and teaching hours.

    From every lesson on record -- the app's and past schedules alike -- so a
    month billed outside the app still counts. A student marked cancelled
    didn't come and isn't counted; a lesson nobody came to isn't either. A
    lesson's hours are the teacher's time, counted once however many came.
    A child taught by two teachers is one student in the academy's month and
    one in each teacher's. Past schedules name a teacher as the workbook did
    ("Jason"); they're matched to the app's spelling of the same teacher.

    Returns ``{"by_month": [...], "by_teacher": [...]}``, one row per month,
    and per teacher per month.
    """
    spelling = {row["teacher"].casefold(): row["teacher"] for row in lessons}
    for row in lessons:
        if row.get("source") == "app":
            spelling[row["teacher"].casefold()] = row["teacher"]
    taught: dict[tuple, dict] = {}
    students: dict[tuple, set] = defaultdict(set)
    academy: dict[tuple, set] = defaultdict(set)
    for row in lessons:
        if row.get("status") == CANCELLED:
            continue
        student = normalise_name(row["student"])
        if not student:
            continue
        teacher = spelling[row["teacher"].casefold()]
        month = (row["date"].year, row["date"].month)
        lesson = (teacher, row["date"], row["start"])
        if lesson not in taught:
            start = row["start"].hour * 60 + row["start"].minute
            end = row["end"].hour * 60 + row["end"].minute
            taught[lesson] = {"month": month, "hours": max(0, end - start) / 60}
        students[(teacher, month)].add(student)
        academy[month].add(student)

    by_teacher: dict[tuple, dict] = {}
    for (teacher, _, _), lesson in taught.items():
        row = by_teacher.setdefault((teacher, lesson["month"]), {
            "Teacher": teacher, "Year": lesson["month"][0], "Month": lesson["month"][1],
            "Students": len(students[(teacher, lesson["month"])]), "Lessons": 0, "Hours": 0.0,
        })
        row["Lessons"] += 1
        row["Hours"] += lesson["hours"]
    by_month: dict[tuple, dict] = {}
    for (teacher, month), row in by_teacher.items():
        total = by_month.setdefault(month, {
            "Year": month[0], "Month": month[1], "Students": len(academy[month]),
            "Lessons": 0, "Hours": 0.0, "Teachers": 0,
        })
        total["Lessons"] += row["Lessons"]
        total["Hours"] += row["Hours"]
        total["Teachers"] += 1
    for row in list(by_teacher.values()) + list(by_month.values()):
        row["Hours"] = round(row["Hours"], 2)
    return {
        "by_month": sorted(by_month.values(), key=lambda r: (r["Year"], r["Month"])),
        "by_teacher": sorted(by_teacher.values(),
                             key=lambda r: (r["Year"], r["Month"], r["Teacher"].casefold())),
    }
