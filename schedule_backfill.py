"""Writes a parsed Excel schedule into the database.

``schedule_parser`` reads a workbook into sessions; this writes those sessions
in through the same functions the Timetable tab uses, so imported data is
indistinguishable from data typed by hand -- and admins can keep re-uploading
the same month's workbook as they keep editing it in Excel:

* A class never seen before is created, same as always.
* A class already on the calendar (same class, date, start time) is
  *updated* from the new parse -- roster, hours, Online/Recording/Cancelled --
  so edits made in the spreadsheet after the first import actually land.
* Anything only ever settable inside the app -- a class's ``Paid`` flags, its
  free-text note -- is carried over untouched. Excel has no opinion on those.

``AcademyClass.name`` is unique across the whole academy (not per teacher), so
two teachers can't both have a class literally called "G11 Chem HL B" --
``suggest_class_renames`` flags that before anything is written, and classes
are only ever matched against the *importing* teacher's own, so a stale
collision can never quietly attach a class to the wrong teacher.
"""

from __future__ import annotations

import datetime as dt
import difflib
import re
from collections import defaultdict
from typing import Any

import db
from schedule_parser import MERGE_THRESHOLD, _bare, _suffix

__all__ = [
    "backfill",
    "apply_review_decisions",
    "suggest_class_renames",
    "suggest_student_matches",
]

# db.py rejects a rate of zero, so an import seeds this placeholder and the
# Invoices tab treats anything still on it as unpriced. Defined there so the
# two can never drift apart.
DEFAULT_RATE = db.UNSET_RATE
PALETTE = [
    "#FFF2CC", "#E0F7FA", "#FCE5CD", "#F4CCCC", "#D9D2E9",
    "#B6D7A8", "#F9CB9C", "#D0E0E3", "#EAD1DC", "#CFE2F3",
]


def apply_review_decisions(
    preview: dict[str, Any], decisions: dict[int, str]
) -> list[dict[str, Any]]:
    """Rewrite ambiguous-spelling pairs per the admin's merge/keep-separate choices.

    ``decisions`` maps an index into ``preview["name_reviews"]`` to
    ``"merge"`` or anything else (kept separate, the safe default). A merge
    rewrites the less-used spelling onto the more-used one across every
    session's attendance. Mutates and returns ``preview["sessions"]``.
    """
    renames: dict[str, str] = {}
    for index, review in enumerate(preview.get("name_reviews") or []):
        if decisions.get(index) != "merge":
            continue
        names, counts = review["names"], review["counts"]
        winner, loser = (names[0], names[1]) if counts[0] >= counts[1] else (names[1], names[0])
        renames[loser.casefold()] = winner

    sessions = preview.get("sessions") or []
    if renames:
        for session in sessions:
            for entry in session["attendance"]:
                replacement = renames.get(entry["student_name"].casefold())
                if replacement:
                    entry["student_name"] = replacement
    return sessions


def _subject_key(name: str) -> str:
    """A subject name reduced to what actually distinguishes it.

    Case, spacing and punctuation all vary between one typing of a subject and
    the next -- "G11 Math AA SL" / "G11 Math AASL", "Pre Cal 1:1" /
    "Pre-Cal 1:1". What is left after stripping them is the thing a human
    would call the same class.

    Deliberately keeps digits and letters, so "G10 Add Math_A" and
    "G10 Add Math_B" stay different, as do "G11" and "G12".
    """
    return re.sub(r"[^0-9a-z\uac00-\ud7a3]", "", name.casefold())


def suggest_subject_merges(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Subject names in one workbook that are the same class typed two ways.

    Only names that are *identical* once case, spacing and punctuation are
    ignored are offered -- that is a typo, not a judgement call. Anything
    merely similar ("G9 Add math_A" vs "_B") is left alone, because those are
    genuinely different classes and guessing would merge two teachers' worth
    of billing into one.

    Nothing is merged here. Each pair is returned for a human to confirm, in
    the same spirit as the student name reviews.
    """
    counts: dict[str, int] = defaultdict(int)
    for session in sessions:
        counts[session["class_name"]] += 1

    grouped: dict[str, list[str]] = defaultdict(list)
    for name in counts:
        grouped[_subject_key(name)].append(name)

    suggestions = []
    for variants in grouped.values():
        if len(variants) < 2:
            continue
        # The spelling used most often wins; a tie falls to the longer one,
        # which is usually the one with the spaces typed properly.
        variants.sort(key=lambda name: (-counts[name], -len(name), name))
        keep, *drop = variants
        for other in drop:
            suggestions.append(
                {
                    "keep": keep,
                    "drop": other,
                    "counts": (counts[keep], counts[other]),
                }
            )
    suggestions.sort(key=lambda item: (-item["counts"][0], item["keep"]))
    return suggestions


def suggest_class_renames(
    sessions: list[dict[str, Any]], teacher_name: str
) -> dict[str, str]:
    """Class names already used by a *different* teacher, with a suggested fix.

    The academy's real invoices already suffix a subject with the teacher's
    name for exactly this reason (e.g. "Math (Ara TR)"), so that's the
    suggestion offered here -- the admin can still type something else.
    """
    existing = {item["Class"].casefold(): item["Teacher"] for item in db.get_all_classes()}
    names = {session["class_name"] for session in sessions}
    collisions = {}
    for name in sorted(names):
        owner = existing.get(name.casefold())
        if owner and owner.casefold() != teacher_name.casefold():
            collisions[name] = f"{name} ({teacher_name})"
    return collisions


def suggest_student_matches(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parsed names that might already be an existing student under a
    different spelling, tag, or capitalisation -- flagged for a human
    decision rather than either silently merging into the existing record
    or silently creating a second one.

    A name that already matches an existing student exactly
    (case-insensitive) is left alone here -- there's no ambiguity to
    resolve, same as always.
    """
    existing = db.get_all_students()
    exact = {item["Name"].casefold() for item in existing}

    parsed_names = sorted(
        {
            entry["student_name"]
            for session in sessions
            for entry in session["attendance"]
        }
    )

    candidates: list[dict[str, Any]] = []
    for name in parsed_names:
        if name.casefold() in exact:
            continue
        bare_new = _bare(name)
        best: dict[str, Any] | None = None
        for item in existing:
            existing_name = item["Name"]
            bare_existing = _bare(existing_name)
            if bare_existing == bare_new:
                reason, ratio = "tag", 1.0
            else:
                ratio = difflib.SequenceMatcher(None, bare_new, bare_existing).ratio()
                if ratio < MERGE_THRESHOLD:
                    continue
                reason = "spelling"
            if best is None or ratio > best["similarity"]:
                best = {
                    "parsed_name": name,
                    "existing_name": existing_name,
                    "existing_id": item["ID"],
                    "similarity": round(ratio, 3),
                    "reason": reason,
                    "tag": _suffix(name) or None,
                    "existing_tag": _suffix(existing_name) or None,
                }
        if best:
            candidates.append(best)

    candidates.sort(key=lambda item: -item["similarity"])
    return candidates


def _matches_stored(
    parsed: dict[str, Any],
    status: str,
    attendance_rows: list[dict],
    current: dict[str, Any],
) -> bool:
    """True when the spreadsheet says exactly what the database already holds.

    Compares only the fields an import is allowed to change -- times, status
    and each student's condition. ``is_paid`` and the class note are
    deliberately excluded: those belong to the app, are carried over
    untouched by the caller, and must never make a class look "changed".
    """
    if not current:
        return False
    if (
        current.get("Start") != parsed["start_time"]
        or current.get("End") != parsed["end_time"]
        or current.get("Status") != status
    ):
        return False

    def signature(rows):
        return sorted(
            (
                row["student_id"],
                bool(row.get("is_online")),
                bool(row.get("has_recording")),
                bool(row.get("is_cancelled")),
                (row.get("note") or "").strip(),
            )
            for row in rows
        )

    return signature(attendance_rows) == signature(current.get("Attendance", []))


def _attendance_rows(session: dict[str, Any], name_to_id: dict[str, int]) -> list[dict]:
    """Turn the parser's single status into the database's condition flags."""
    rows = []
    for entry in session["attendance"]:
        student_id = name_to_id.get(entry["student_name"].casefold())
        if student_id is None:
            continue
        status = (entry.get("status") or "Attending").casefold()
        rows.append(
            {
                "student_id": student_id,
                "is_online": status == "online",
                "has_recording": status == "recording",
                "is_cancelled": status == "cancelled",
                "note": (entry.get("note") or "")[:200],
            }
        )
    return rows


def backfill(
    preview: dict[str, Any],
    teacher_id: int,
    hourly_rate: float = DEFAULT_RATE,
    name_overrides: dict[str, str] | None = None,
    student_matches: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Write a parsed workbook into the database for one teacher.

    ``student_matches`` maps a parsed name (casefolded) to an *existing*
    student id -- the admin's confirmed answer to "is this really
    so-and-so, just spelled differently", from ``suggest_student_matches``.
    A name with no exact match and no confirmed match still gets its own
    new student, same as always -- this only short-circuits that for names
    the admin actually looked at and said yes to.
    """
    sessions = preview.get("sessions") or []
    if not sessions:
        return {"status": "empty"}

    teacher = next((t for t in db.get_all_teachers() if t["ID"] == teacher_id), None)
    if teacher is None:
        return {"status": "no_such_teacher"}

    name_overrides = name_overrides or {}
    for session in sessions:
        session["class_name"] = name_overrides.get(session["class_name"], session["class_name"])

    created: dict[str, int] = defaultdict(int)

    # A month sheet usually repeats the first day or two of the next month,
    # so the same class can be parsed twice from two worksheets. Writing
    # both is pure waste, and where the two copies disagree -- typically one
    # sheet spelling a student's name differently -- they would overwrite
    # each other on every single import, forever. The parser already warns
    # that the class appears twice; here the first copy wins, so an import
    # settles instead of oscillating.
    deduped: list[dict[str, Any]] = []
    seen_slots: set[tuple[str, Any, Any]] = set()
    for session in sessions:
        slot = (
            session["class_name"].casefold(),
            session["date"],
            session["start_time"],
        )
        if slot in seen_slots:
            created["duplicate_slots_skipped"] += 1
            continue
        seen_slots.add(slot)
        deduped.append(session)
    sessions = deduped
    period_stats: dict[tuple[int, int], dict[str, int]] = defaultdict(
        lambda: {"created": 0, "updated": 0}
    )
    warnings_by_month: dict[tuple[int, int], int] = defaultdict(int)
    for session in sessions:
        if session["warnings"]:
            key = (session["date"].year, session["date"].month)
            warnings_by_month[key] += len(session["warnings"])

    # 1. Students, so every name has an id before any class is written.
    # Learning a just-created student's id is one indexed lookup, not a
    # refetch of the whole students table -- with years of history that
    # table only grows, and refetching it once per *new* student in every
    # import is what made later imports slower than earlier ones, even
    # though each import's own workload never changed size.
    student_matches = student_matches or {}
    name_to_id = {item["Name"].casefold(): item["ID"] for item in db.get_all_students()}
    for session in sessions:
        for entry in session["attendance"]:
            key = entry["student_name"].casefold()
            if key in name_to_id:
                continue
            if key in student_matches:
                # The admin confirmed this spelling is an existing student --
                # not a new one, even though it doesn't match exactly.
                name_to_id[key] = student_matches[key]
                continue
            if db.create_quick_student(entry["student_name"]) == "created":
                created["students"] += 1
            new_id = db.get_student_id_by_name(entry["student_name"])
            if new_id is not None:
                name_to_id[key] = new_id

    # 2. Classes, each with its own colour, created with their first class.
    # Scoped to this teacher's own classes only, so a name that collides with
    # a *different* teacher's class (flagged by suggest_class_renames before
    # this ran) can never be mistaken for a match here -- it falls through to
    # create_class_and_first_session, which rejects it as a duplicate rather
    # than silently attaching a class to someone else's class.
    by_class: dict[str, list[dict]] = defaultdict(list)
    for session in sessions:
        by_class[session["class_name"]].append(session)

    existing = {
        item["Class"].casefold(): item["ID"]
        for item in db.get_all_classes()
        if item["Teacher"].casefold() == teacher["Name"].casefold()
    }

    for index, (class_name, class_sessions) in enumerate(sorted(by_class.items())):
        class_sessions.sort(key=lambda item: (item["date"], item["start_time"]))
        class_id = existing.get(class_name.casefold())

        roster = sorted(
            {
                name_to_id[entry["student_name"].casefold()]
                for session in class_sessions
                for entry in session["attendance"]
                if entry["student_name"].casefold() in name_to_id
            }
        )

        start_at = 0
        if class_id is None:
            first = class_sessions[0]
            outcome = db.create_class_and_first_session(
                name=class_name,
                teacher_id=teacher_id,
                hourly_rate=max(hourly_rate, 0.01),
                display_color=PALETTE[index % len(PALETTE)],
                student_ids=roster,
                session_date=first["date"],
                start_time=first["start_time"],
                end_time=first["end_time"],
                status="Completed" if first["date"] <= dt.date.today() else "Scheduled",
                note="",
                attendance_rows=_attendance_rows(first, name_to_id),
            )
            if outcome != "created":
                # "teacher_conflict" means the teacher already has a class at
                # that time -- the workbook's duplicated month-boundary
                # columns land here, and skipping is the right answer.
                # "duplicate" means the name still collides with another
                # teacher's class -- an unresolved rename, so this class is
                # skipped rather than risk attaching it to the wrong teacher.
                created[f"class_{outcome}"] += 1
                continue
            created["classes"] += 1
            created["sessions_created"] += 1
            period_stats[(first["date"].year, first["date"].month)]["created"] += 1
            # One indexed lookup for the new class's id -- not a refetch of
            # every class in the academy, which also re-runs a per-class
            # enrolment count each time (get_all_classes' own cost grows
            # with the *academy's* total class count, not this import's).
            class_id = db.get_class_id_by_name(class_name)
            existing[class_name.casefold()] = class_id
            start_at = 1

        for session in class_sessions[start_at:]:
            status = "Completed" if session["date"] <= dt.date.today() else "Scheduled"
            attendance_rows = _attendance_rows(session, name_to_id)
            month_key = (session["date"].year, session["date"].month)
            existing_session_id = db.find_timetable_session(
                class_id, session["date"], session["start_time"]
            )

            if existing_session_id is None:
                outcome = db.create_timetable_session(
                    teacher_id=teacher_id,
                    class_id=class_id,
                    session_date=session["date"],
                    start_time=session["start_time"],
                    end_time=session["end_time"],
                    status=status,
                    note="",
                    attendance_rows=attendance_rows,
                )
                if outcome == "created":
                    created["sessions_created"] += 1
                    period_stats[month_key]["created"] += 1
                else:
                    created[f"lessons_{outcome}"] += 1
                continue

            # Already on the calendar -- carry each student's Paid flag and
            # the class's note across, since Excel has no opinion on either.
            current = db.get_timetable_session(existing_session_id) or {}
            paid_by_student = {
                row["student_id"]: row["is_paid"] for row in current.get("Attendance", [])
            }
            for row in attendance_rows:
                row["is_paid"] = paid_by_student.get(row["student_id"], False)

            # Re-uploading a workbook to add one class otherwise rewrites
            # every class in it -- each one deleting and reinserting its
            # attendance and re-syncing invoices, for no change at all.
            # Skip the ones the spreadsheet still describes exactly as
            # stored, comparing only what Excel actually owns.
            if _matches_stored(session, status, attendance_rows, current):
                created["sessions_unchanged"] += 1
                continue

            outcome = db.update_timetable_session(
                existing_session_id,
                session_date=session["date"],
                start_time=session["start_time"],
                end_time=session["end_time"],
                status=status,
                note=current.get("Note", "") or "",
                attendance_rows=attendance_rows,
            )
            if outcome == "updated":
                created["sessions_updated"] += 1
                period_stats[month_key]["updated"] += 1
            else:
                created[f"lessons_{outcome}"] += 1

    # The workbook is the record for the months it covers, so a class that
    # has disappeared from it -- moved to another day, or dropped -- is
    # removed here. Without this a rescheduled class would leave its old
    # slot behind and the student would be billed for both.
    # Restricted to the classes this workbook actually contains: if a class
    # failed to import (an unresolved name collision, say) its classes must
    # not look "missing" and be deleted. A class dropped from the sheet
    # entirely therefore keeps its classes, to be removed by hand.
    imported_class_ids = {
        existing[name.casefold()] for name in by_class if name.casefold() in existing
    }
    keep_slots = {
        (existing[session["class_name"].casefold()],
         session["date"], session["start_time"])
        for session in sessions
        if session["class_name"].casefold() in existing
    }
    periods = sorted({(s["date"].year, s["date"].month) for s in sessions})
    reconciled = db.remove_lessons_not_in(
        teacher_id, periods, keep_slots, imported_class_ids
    )
    if reconciled["lessons_removed"]:
        created["lessons_removed"] = reconciled["lessons_removed"]
    if reconciled["credits_raised"]:
        created["credits_raised"] = reconciled["credits_raised"]

    for (year, month), stats in period_stats.items():
        db._record_import(
            teacher_id,
            year,
            month,
            sessions_created=stats["created"],
            sessions_updated=stats["updated"],
            warning_count=warnings_by_month.get((year, month), 0),
        )

    return {"status": "imported", "created": dict(created)}
