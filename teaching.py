"""Counts what was taught each month, for the Teaching view on the Data tab.

The functions here only take plain lists of lesson rows, so they work the same
on lessons from the app, on past schedules, or on test data. Each row is a dict
with: teacher, class_name, date, start, end, student and status (Attending,
Online, Recording or Cancelled).
"""

from __future__ import annotations

import difflib
import re
from collections import defaultdict
from functools import lru_cache
from typing import Any

import schedule_parser
from schedule_parser import CANCELLED

# Names repeat on every lesson, so cache the cleaned-up version of each one.
normalise_name = lru_cache(maxsize=65536)(schedule_parser.normalise_name)


def _said_alike(written: str, on_file: str) -> bool:
    """Return True if two spellings are probably the same child.

    Uses the same checks as the upload screen (word order, romanisation, a
    letter or two off, a Hangul name against an English one), plus two short
    forms teachers use: the given name split in two ("Tae woo" for Jang
    Taewoo), or the given name with a letter off ("Dahey" for Kim Dahye).
    """
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
    # Join the letters into one word first, so "Tae woo" becomes "taewoo".
    joined = schedule_parser._fold("".join(re.findall(r"[A-Za-z]+", schedule_parser.without_brackets(written))))
    if len(mine) >= 2 and len(theirs) >= 2 and len(joined) >= 4 and joined in theirs:
        return True
    # The other way round: "Minjae" written for Min Jae Choi.
    raw = re.findall(r"[A-Za-z]+", schedule_parser.without_brackets(on_file))
    if len(mine) == 1 and len(joined) >= 5 and any(
            schedule_parser._fold(raw[i] + raw[i + 1]) == joined for i in range(len(raw) - 1)):
        return True
    return len(mine) == 1 and len(theirs) >= 2 and len(joined) >= 5 and any(
        len(word) >= 5 and difflib.SequenceMatcher(None, joined, word).ratio() >= schedule_parser.MERGE_THRESHOLD
        for word in theirs)


def link_spellings(app_lessons: list[dict], history_lessons: list[dict]) -> dict[tuple[str, str], str]:
    """Match past-schedule spellings to the name the app uses for that child.

    Only lessons found in both (same teacher, date and start time) are used.
    On such a lesson, a name that only the past schedule has is linked to the
    one name that only the app has, if _said_alike says they match. A
    spelling is never linked if it is already a student's name in the app,
    or if it would match two different students.

    Returns {(teacher in lower case, cleaned spelling): name in the app}.
    """
    def roster(rows):
        # The names on each lesson, keyed by (teacher, date, start).
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
    """Merge app lessons and past-schedule lessons so each lesson counts once.

    If the app has a teacher's month, that month comes from the app only and
    the past schedule for it is skipped. (Past schedules are often a later
    copy of the same workbook, so using both would double count.) Past-schedule
    names that link_spellings matched are changed to the app's spelling.

    Returns the merged rows and some counts shown on the screen.
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
    """Count students, lessons and teaching hours for each month and each teacher.

    - Cancelled places are not counted, and nor is a lesson nobody came to.
    - A lesson's hours count once, however many students were in it.
    - A student taught by two teachers counts once in the academy's total.
    - Teacher names from past schedules are changed to the app's spelling.

    Returns {"by_month": [...], "by_teacher": [...]}.
    """
    # Use the app's spelling of each teacher's name where there is one.
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
        # One entry per lesson, so its hours are only added once.
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
    # Add the teacher rows up into one row per month for the whole academy.
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
