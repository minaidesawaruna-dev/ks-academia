"""Saves a parsed Excel schedule into the database, and prices the subjects it brings in.

schedule_parser reads a workbook into lessons; this writes them using the
same functions as the Schedule screen, so imported lessons are no different
from ones added by hand. The same month can be uploaded again and again as
the teacher keeps editing it:

* A class not seen before is created.
* A class already on the calendar (same subject, date and start time) is
  updated from the workbook: students, times, Online/Recording/Cancelled.
* Things only set in the app, such as Paid ticks and class notes, are kept.

Subject names must be unique across the academy, so suggest_class_renames
flags a name another teacher already uses before anything is saved, and
classes are only ever matched against the importing teacher's own.
"""

from __future__ import annotations

import calendar
import datetime as dt
import difflib
import re
import threading
from collections import Counter, defaultdict
from typing import Any

import db
from schedule_parser import (
    ATTENDING, CANCELLED, GRADE_TAG, JUNIOR_RATE, MERGE_THRESHOLD, ONLINE, RECORDING, _bare, _close_spelling,
    _one_letter_apart, _shortened, _suffix, hangul_fit, name_sound, rate_for_grade, standard_rate, without_brackets,
)

__all__ = [
    "backfill",
    "price_rule",
    "price_by_rule",
    "apply_review_decisions",
    "suggest_class_renames",
    "suggest_student_matches",
    "months_found",
    "default_months",
    "restrict_to_months",
    "plan_import",
    "lessons_on_file",
    "preview_from_lessons",
]

# The price an imported subject gets until someone sets a real one (db rejects $0).
# Billing treats it as "no price".
DEFAULT_RATE = db.UNSET_RATE
PALETTE = [
    "#FFF2CC", "#E0F7FA", "#FCE5CD", "#F4CCCC", "#D9D2E9",
    "#B6D7A8", "#F9CB9C", "#D0E0E3", "#EAD1DC", "#CFE2F3",
]


def apply_review_decisions(
    preview: dict[str, Any], decisions: dict[int, str]
) -> list[dict[str, Any]]:
    """Apply the admin's answers to "are these two spellings the same child?".

    `decisions` maps a question's index to "merge" or anything else (keep
    separate). A merge changes the less used spelling to the more used one
    in every lesson. Changes and returns preview["sessions"].
    """
    renames: dict[str, str] = {}
    reviews = preview.get("name_reviews") or []
    # If one spelling is a student already on file, that one wins, so the lessons
    # stay with the existing student instead of creating a second one.
    known = (
        {item["Name"].casefold() for item in db.get_all_students()} | set(db.get_student_aliases())
        if any(decisions.get(index) == "merge" for index in range(len(reviews))) else set()
    )
    for index, review in enumerate(reviews):
        if decisions.get(index) != "merge":
            continue
        names, counts = review["names"], review["counts"]
        on_file = [name for name in names if name.casefold() in known]
        if len(on_file) == 1:
            winner = on_file[0]
            loser = names[1] if winner == names[0] else names[0]
        else:
            winner, loser = (names[0], names[1]) if counts[0] >= counts[1] else (names[1], names[0])
        renames[loser.casefold()] = winner
        # Remember the merge, so next month's workbook with both spellings isn't asked again.
        preview.setdefault("merged_spellings", {})[loser] = winner

    sessions = preview.get("sessions") or []
    if renames:
        for session in sessions:
            for entry in session["attendance"]:
                replacement = renames.get(entry["student_name"].casefold())
                if replacement:
                    entry["student_name"] = replacement
    return sessions


def _subject_key(name: str) -> str:
    """Reduce a subject name to the letters and digits that tell it apart.

    Case, spaces and punctuation are dropped, so "G11 Math AA SL" and
    "G11 Math AASL" match. Letters and digits stay, so "G10 Add Math_A" and
    "G10 Add Math_B" (or G11 and G12) remain different.
    """
    return re.sub(r"[^0-9a-z\uac00-\ud7a3]", "", name.casefold())


def teacher_subjects(teacher_id: int | None) -> set[str]:
    """Return the teacher's subjects on file, keyed by lower-case name (empty for a new teacher)."""
    teacher = next((t for t in db.get_all_teachers() if t["ID"] == teacher_id), None)
    return set(_teacher_classes(teacher)) if teacher else set()


def use_subject_spellings(preview: dict[str, Any], teacher_id: int | None) -> dict[str, Any]:
    """Return the upload with a renamed subject's old name swapped for its name now.

    The teacher's sheet still says "G9 Englsh" after the subject was renamed
    "G9 English". Left alone, the upload would make "G9 Englsh" a new subject
    and take those classes off the renamed one. A name that is one of the
    teacher's subjects now is left as it is.
    """
    spellings = db.get_subject_spellings(teacher_id)
    if not spellings:
        return preview
    current = teacher_subjects(teacher_id)

    def now(name: str) -> str:
        if name.casefold() in current:
            return name
        return spellings.get(" ".join(name.split()).casefold(), name)

    return {**preview, "sessions": [
        session if now(session["class_name"]) == session["class_name"]
        else {**session, "class_name": now(session["class_name"])}
        for session in preview.get("sessions") or []
    ]}


def student_cells(sessions: list[dict[str, Any]], name: str) -> list[str]:
    """Return the workbook cells a student's name is written in."""
    return [item["cell"] for item in sessions if item.get("cell")
            and any(entry["student_name"] == name for entry in item["attendance"])]


def subject_cells(sessions: list[dict[str, Any]], name: str) -> list[str]:
    """Return the workbook cells of a subject's lessons."""
    return [item["cell"] for item in sessions if item.get("cell") and item["class_name"] == name]


def suggest_subject_merges(sessions: list[dict[str, Any]], on_file=()) -> list[dict[str, Any]]:
    """Find subjects in one workbook that are the same class typed two ways.

    Only names that are identical once case, spaces and punctuation are ignored
    count; merely similar names ("G9 Add math_A" and "_B") are different classes.
    `on_file` is the teacher's subjects already in the app: a spelling on file
    wins, and two spellings that are both on file are never offered (they are
    two real classes). Nothing is merged here; each pair goes to the admin to
    confirm.
    """
    on_file = {name.casefold() for name in on_file}
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
        # Prefer a spelling on file, then the most used one; on a tie, the longer one
        # (usually the one with proper spaces).
        variants.sort(key=lambda name: (name.casefold() not in on_file, -counts[name], -len(name), name))
        keep, *drop = variants
        for other in drop:
            if other.casefold() == keep.casefold() or other.casefold() in on_file:
                continue
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
    """Find subject names another teacher already uses, and suggest a new name for each.

    The suggestion adds the teacher's name, e.g. "Math (Ms Lee)", which is how
    the academy's own invoices tell them apart. The admin can type another name.
    """
    existing = {item["Class"].casefold(): item["Teacher"] for item in db.get_all_classes()}
    names = {session["class_name"] for session in sessions}
    collisions = {}
    for name in sorted(names):
        owner = existing.get(name.casefold())
        if owner and owner.casefold() != teacher_name.casefold():
            collisions[name] = f"{name} ({teacher_name})"
    return collisions


def grades_text(grades: list[int]) -> str:
    """Write a list of grades for display: [9, 10] -> "Grades 9–10", [12] -> "Grade 12"."""
    if len(grades) == 1:
        return f"Grade {grades[0]}"
    if grades == list(range(grades[0], grades[-1] + 1)):
        return f"Grades {grades[0]}–{grades[-1]}"
    return "Grades " + ", ".join(map(str, grades[:-1])) + f" and {grades[-1]}"


def price_rule(subjects: list[dict], rates: list[dict] | None = None) -> dict[int, tuple[float, str, bool]]:
    """Work out each subject's price under the academy's rule: {class_id: (rate, reason, certain)}.

    Grade 11 and above is $65/h, everything else $60/h. The grade comes from
    the subject's name; if the name has none, the subject's own price in
    another month is used; failing that, its students' grades decide.
    `certain` is False when it falls back to the $60 default (students on both
    sides of the line, or no grade known); that default only fills a missing
    price and never replaces one someone chose. Pass `rates` if you already
    have db.get_all_class_rates().
    """
    ungraded = [item["Class ID"] for item in subjects if not standard_rate(item["Class"])]
    student_grades = db.get_subject_student_grades(ungraded) if ungraded else {}
    chosen: dict[int, tuple[dt.date, float]] = {}
    for row in (rates if rates is not None else db.get_all_class_rates()) if ungraded else []:
        starts = db.as_date(row["Effective From"])
        if row["Hourly Rate"] > db.UNSET_RATE and starts >= chosen.get(row["Class ID"], (dt.date.min, 0))[0]:
            chosen[row["Class ID"]] = (starts, row["Hourly Rate"])
    rule: dict[int, tuple[float, str, bool]] = {}
    for item in subjects:
        named = standard_rate(item["Class"])
        grades = sorted(set(student_grades.get(item["Class ID"], [])))
        bands = {rate_for_grade(grade) for grade in grades}
        own = chosen.get(item["Class ID"])
        if named:
            rule[item["Class ID"]] = (named, "", True)
        elif own:
            rule[item["Class ID"]] = (own[1], f"its own price from {own[0]:%B %Y}", True)
        elif len(bands) == 1:
            rule[item["Class ID"]] = (bands.pop(), f"its students are in {grades_text(grades)}", True)
        elif grades:
            rule[item["Class ID"]] = (JUNIOR_RATE, f"students in {grades_text(grades)}", False)
        else:
            rule[item["Class ID"]] = (JUNIOR_RATE, "grade not known", False)
    return rule


def price_by_rule(unpriced: list[dict]) -> tuple[int, list[dict]]:
    """Price every listed subject (from db.get_unpriced_subjects) by the rule, in one save.

    Each is priced from the first month it has no price. Returns how many
    were priced and any still left without a price.
    """
    if not unpriced:
        return 0, []
    rule = price_rule(unpriced)
    done: set[int] = set()
    for _ in range(3):
        db.set_class_rates_for_months(
            (item["Class ID"], dt.date(*item["Months"][0], 1), rule[item["Class ID"]][0])
            for item in unpriced
        )
        done |= {item["Class ID"] for item in unpriced}
        unpriced = db.get_unpriced_subjects(class_ids=list(rule))
        if not unpriced:
            break
    return len(done), unpriced


def _without_grade(name: str) -> str:
    """Remove a grade written in front of a name: "G5 Minji" -> "Minji".

    A name that is only a grade is kept whole, since "" would match every
    other such name.
    """
    stripped = re.sub(r"^(?:g|y|gr\.?|grade)\s?\d{1,2}\b[\s.:\-]*", "", name.strip(), flags=re.I)
    return stripped or name.strip()


def suggest_student_matches(sessions: list[dict[str, Any]], teacher_id: int | None = None) -> list[dict[str, Any]]:
    """Find names in the upload that might be a student already on file, spelled another way.

    Each one becomes a question for the admin, instead of quietly merging or
    quietly creating a second student. A name that matches a student exactly
    (ignoring case) isn't asked about.
    """
    existing = db.get_all_students()
    exact = {item["Name"].casefold() for item in existing} | set(db.get_student_aliases())
    # The teacher's own students, for names they shorten: their "Hana" means their
    # Park Hana, not every Hana in the academy.
    rosters = db.get_teacher_rosters(teacher_id) if teacher_id else {}
    theirs = set().union(*rosters.values()) if rosters else set()
    sounds = {item["ID"]: name_sound(item["Name"]) for item in existing}
    bare_names = Counter(_bare(item["Name"]) for item in existing)

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
        grade_matches = 0
        for item in existing:
            existing_name = item["Name"]
            bare_existing = _bare(existing_name)
            if bare_existing == bare_new:
                reason, ratio = "tag", 1.0
            elif _without_grade(bare_existing) == _without_grade(bare_new):
                # A student saved as "G5 Minji" before the parser took grades off names.
                reason, ratio = "grade", 1.0
                grade_matches += 1
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
        # The same name said the same way: other word order ("Lee Minjun" / "Minjun Lee"),
        # another romanisation, or a nickname in brackets. Letter by letter these look
        # too different, so they used to come in as new students.
        sound = name_sound(name)
        alike = [student for student, said in sounds.items() if sound and said == sound]
        if alike and (best is None or best["reason"] == "spelling"):
            said_same = next(item for item in existing if item["ID"] == alike[0])
            if best is None or best["existing_id"] not in alike:
                best = {
                    "parsed_name": name,
                    "existing_name": said_same["Name"],
                    "existing_id": said_same["ID"],
                    "similarity": round(difflib.SequenceMatcher(
                        None, bare_new, _bare(said_same["Name"])).ratio(), 3),
                    "reason": "sound",
                    "tag": _suffix(name) or None,
                    "existing_tag": _suffix(said_same["Name"]) or None,
                }
        # Shortened, with an English name added, or a letter or two off, for one of this
        # teacher's own students: taken as that student when only one fits.
        if theirs and (best is None or best["reason"] == "spelling"):
            short = [item for item in existing
                     if item["ID"] in theirs and _shortened(name, item["Name"])]
            typo = [item for item in existing
                    if item["ID"] in theirs and _close_spelling(name, item["Name"])]
            found = short or typo
            if found:
                best = {
                    "parsed_name": name, "existing_name": found[0]["Name"], "existing_id": found[0]["ID"],
                    "similarity": 0.0, "reason": "short" if short else "spelling",
                    "tag": None, "existing_tag": None, "likely_same": len(found) == 1,
                }
                candidates.append(best)
                continue
            # One letter off a short name: asked about, but not assumed, because siblings'
            # names can differ by one letter too.
            letter = [item for item in existing
                      if item["ID"] in theirs and _one_letter_apart(name, item["Name"])]
            if letter:
                best = {
                    "parsed_name": name, "existing_name": letter[0]["Name"], "existing_id": letter[0]["ID"],
                    "similarity": 0.0, "reason": "letter", "tag": None, "existing_tag": None,
                    "likely_same": False,
                }
                candidates.append(best)
                continue
        # Written in Hangul (e.g. "다혜" for "Dahye"): taken as that student if only one fits,
        # asked about if several do.
        fits = [item for item in existing if best is None and hangul_fit(name, item["Name"])]
        if fits:
            top = max(fits, key=lambda item: hangul_fit(name, item["Name"]))
            best = {
                "parsed_name": name, "existing_name": top["Name"], "existing_id": top["ID"],
                "similarity": 0.0, "reason": "hangul", "tag": None, "existing_tag": None,
                "likely_same": len(fits) == 1,
            }
            candidates.append(best)
            continue
        if best:
            # Spelled differently but said the same ("Jaaemin" / "Jaemin"), and like no one
            # else on file: start the question on "same child".
            if best["reason"] in ("spelling", "sound") and best["existing_id"] in alike:
                best["reason"] = "sound"
            # Only a grade apart from one student: start on "same child", or every one of
            # that student's billed classes would be billed again. Same for a nickname added
            # in brackets ("Park Jisoo(Emma)"), unless the student on file has a tag of their
            # own ("Park Hana(A)" and "(B)" are two children). A grade in brackets doesn't
            # count as a nickname.
            nickname = (best["reason"] == "tag" and best["tag"] and not best["existing_tag"]
                        and not GRADE_TAG.match(best["tag"]) and bare_names[bare_new] == 1)
            # A given name on its own ("Yuna" / "Yoona") is too common to assume.
            full_names = all(len(re.findall(r"[A-Za-z]+", without_brackets(n))) >= 2
                             for n in (name, best["existing_name"]))
            best["likely_same"] = (
                (best["reason"] == "grade" and grade_matches == 1)
                or (best["reason"] == "sound" and len(alike) == 1 and full_names)
                or bool(nickname)
            )
            candidates.append(best)

    candidates.sort(key=lambda item: -item["similarity"])
    return candidates


def _matches_stored(
    parsed: dict[str, Any],
    status: str,
    attendance_rows: list[dict],
    current: dict[str, Any],
) -> bool:
    """Return True if the workbook says exactly what the database already has for a class.

    Only compares what an import may change: times, status and each student's
    flags. Paid ticks and the class note belong to the app and are ignored.
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
        """Return the class's students and their flags in a form that can be compared."""
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
    """Turn the parser's status for each student into the database's flags.

    One row per student: if a child is listed twice in one class (same name
    or two spellings of one child), the first entry is kept.
    """
    rows = []
    seen: set[int] = set()
    for entry in session["attendance"]:
        student_id = name_to_id.get(entry["student_name"].casefold())
        if student_id is None or student_id in seen:
            continue
        seen.add(student_id)
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


def _listed_twice(session: dict[str, Any], name_to_id: dict[str, int]) -> bool:
    """Return True if the same student appears twice in one lesson."""
    ids = [name_to_id.get(entry["student_name"].casefold()) for entry in session["attendance"]]
    ids = [student_id for student_id in ids if student_id is not None]
    return len(ids) != len(set(ids))


def _final_sessions(sessions: list[dict], name_overrides: dict[str, str]) -> tuple[list[dict], int]:
    """Return the workbook's lessons under their final subject names, each once.

    A month's worksheet often repeats the first days of the next month, so
    a lesson can be read twice from two sheets. The first copy is kept, so
    re-imports don't keep flipping between two versions. Returns the
    lessons and how many repeats were dropped.
    """
    final: list[dict] = []
    seen_slots: set[tuple[str, Any, Any]] = set()
    for session in sessions:
        session = {**session, "class_name": name_overrides.get(session["class_name"], session["class_name"])}
        slot = (session["class_name"].casefold(), session["date"], session["start_time"])
        if slot in seen_slots:
            continue
        seen_slots.add(slot)
        final.append(session)
    return final, len(sessions) - len(final)


def _covered_dates(sessions: list[dict], dates) -> set[dt.date]:
    """Return the days this import covers: the days its worksheets show.

    If those aren't known, every day of each month the lessons fall in.
    """
    if dates is not None:
        return set(dates)
    covered = set()
    for year, month in {(s["date"].year, s["date"].month) for s in sessions}:
        first = dt.date(year, month, 1)
        covered.update(first + dt.timedelta(days=offset)
                       for offset in range(calendar.monthrange(year, month)[1]))
    return covered


def _teacher_classes(teacher: dict) -> dict[str, int]:
    """Return this teacher's subjects by lower-case name (never another teacher's)."""
    return {
        item["Class"].casefold(): item["ID"]
        for item in db.get_all_classes()
        if item["Teacher"].casefold() == teacher["Name"].casefold()
    }


def _same_name_rules(teacher_id: int, students_on_file: list[dict]):
    """Return students who share exactly one name, and which of this teacher's classes each is in.
    """
    same_name: dict[str, list[int]] = defaultdict(list)
    for item in students_on_file:
        same_name[item["Name"].casefold()].append(item["ID"])
    same_name = {name: ids for name, ids in same_name.items() if len(ids) > 1}
    rosters = db.get_teacher_rosters(teacher_id) if same_name else {}
    taught = set().union(*rosters.values()) if rosters else set()
    return same_name, rosters, taught


def _names_for_class(class_id, name_to_id, same_name, rosters, taught) -> dict[str, int]:
    """Return the name-to-student map to use for one class.

    Two students with the same single name (two girls called Hana) can't be
    told apart by name, so the class decides: the one already in that class,
    otherwise the one this teacher teaches.
    """
    if not same_name:
        return name_to_id
    names_here = dict(name_to_id)
    for name, ids in same_name.items():
        for pool in (rosters.get(class_id, set()), taught):
            fits = [student_id for student_id in ids if student_id in pool]
            if len(fits) == 1:
                names_here[name] = fits[0]
                break
    return names_here


def _workbook_slots(sessions, by_class, existing):
    """Return what the workbook still has, for db.workbook_drops.

    That is: the subjects in it that are already on file, each of their
    lessons as (id, date, start), and every (date, start) it writes anything
    at, including classes whose name couldn't be matched.
    """
    class_ids = {existing[name.casefold()] for name in by_class if name.casefold() in existing}
    keep_slots = {
        (existing[session["class_name"].casefold()], session["date"], session["start_time"])
        for session in sessions
        if session["class_name"].casefold() in existing
    }
    filled_slots = {(session["date"], session["start_time"]) for session in sessions}
    return class_ids, keep_slots, filled_slots


# Only one import runs at a time. Two at once (a double click, or two tabs) could
# each add the same new student, which once put eleven students on file twice.
_ONE_IMPORT_AT_A_TIME = threading.Lock()


def backfill(
    preview: dict[str, Any],
    teacher_id: int,
    hourly_rate: float = DEFAULT_RATE,
    name_overrides: dict[str, str] | None = None,
    student_matches: dict[str, int] | None = None,
    dates=None,
) -> dict[str, Any]:
    """Save a parsed workbook into the database for one teacher.

    `dates` are the days the uploaded worksheets show. On those days the
    teacher's calendar is made to match the workbook; other days are left
    alone. Without `dates`, every day of each month the lessons fall in.

    `student_matches` maps a name in the workbook (lower case) to an existing
    student id, from the admin's answers to suggest_student_matches. Any other
    new name becomes a new student.
    """
    with _ONE_IMPORT_AT_A_TIME:
        return _backfill(preview, teacher_id, hourly_rate, name_overrides, student_matches, dates)


def _backfill(preview, teacher_id, hourly_rate, name_overrides, student_matches, dates):
    """Do the import for backfill(), which holds the lock. Returns what was created and changed."""
    preview = use_subject_spellings(preview, teacher_id)
    sessions = preview.get("sessions") or []
    if not sessions:
        return {"status": "empty"}

    teacher = next((t for t in db.get_all_teachers() if t["ID"] == teacher_id), None)
    if teacher is None:
        return {"status": "no_such_teacher"}

    created: dict[str, int] = defaultdict(int)
    sessions, repeats = _final_sessions(sessions, name_overrides or {})
    if repeats:
        created["duplicate_slots_skipped"] = repeats
    period_stats: dict[tuple[int, int], dict[str, int]] = defaultdict(
        lambda: {"created": 0, "updated": 0}
    )
    warnings_by_month: dict[tuple[int, int], int] = defaultdict(int)
    for session in sessions:
        if session["warnings"]:
            key = (session["date"].year, session["date"].month)
            warnings_by_month[key] += len(session["warnings"])

    # 1. Students first, so every name has an id before any class is saved.
    # A new student's id is looked up by name, not by reloading the whole students
    # table, which grows every year.
    student_matches = student_matches or {}
    # A spelling already linked to a student (merged before, or confirmed at an
    # earlier import) is that student.
    students_on_file = db.get_all_students()
    name_to_id = {**db.get_student_aliases(),
                  **{item["Name"].casefold(): item["ID"] for item in students_on_file}}
    same_name, rosters, taught = _same_name_rules(teacher_id, students_on_file)
    confirmed = []
    for session in sessions:
        for entry in session["attendance"]:
            key = entry["student_name"].casefold()
            if key in name_to_id:
                continue
            if key in student_matches:
                # The admin confirmed this spelling is an existing student. Remember it, so the
                # next workbook with the same spelling doesn't ask again.
                name_to_id[key] = student_matches[key]
                confirmed.append((student_matches[key], entry["student_name"]))
                continue
            if db.create_student(entry["student_name"]) == "created":
                created["students"] += 1
            new_id = db.get_student_id_by_name(entry["student_name"])
            if new_id is not None:
                name_to_id[key] = new_id
    for loser, winner in (preview.get("merged_spellings") or {}).items():
        if winner.casefold() in name_to_id:
            confirmed.append((name_to_id[winner.casefold()], loser))
    if confirmed:
        db.remember_student_aliases(confirmed)

    # 2. Subjects, each with its own colour, created along with their first class.
    # Only this teacher's subjects are matched, so a name another teacher uses never
    # attaches a class to them; create_class_and_first_session refuses it instead.
    by_class: dict[str, list[dict]] = defaultdict(list)
    for session in sessions:
        by_class[session["class_name"]].append(session)

    existing = _teacher_classes(teacher)

    # Remove classes the workbook no longer shows on its days (moved or dropped), or
    # the student would be billed for the old slot as well. Done before adding
    # anything, so a subject swapped for another at the same time isn't refused as a
    # clash. db.workbook_drops decides which lessons go.
    imported_class_ids, keep_slots, filled_slots = _workbook_slots(sessions, by_class, existing)
    reconciled = db.remove_lessons_not_in(
        teacher_id, _covered_dates(sessions, dates), keep_slots, imported_class_ids, filled_slots
    )
    if reconciled["lessons_removed"]:
        created["lessons_removed"] = reconciled["lessons_removed"]
    if reconciled["credits_raised"]:
        created["credits_raised"] = reconciled["credits_raised"]

    listed_twice: list[str] = []
    for index, (class_name, class_sessions) in enumerate(sorted(by_class.items())):
        class_sessions.sort(key=lambda item: (item["date"], item["start_time"]))
        class_id = existing.get(class_name.casefold())
        names_here = _names_for_class(class_id, name_to_id, same_name, rosters, taught)
        listed_twice += [
            item.get("cell") or f"{item['date']:%d %b} {item['class_name']}"
            for item in class_sessions if _listed_twice(item, names_here)
        ]

        roster = sorted(
            {
                names_here[entry["student_name"].casefold()]
                for session in class_sessions
                for entry in session["attendance"]
                if entry["student_name"].casefold() in names_here
            }
        )

        start_at = 0
        if class_id is None:
            first = class_sessions[0]
            # Price a new subject from the grade in its name. One with no grade (e.g. "Basic Eng")
            # keeps the placeholder for now and is priced at the end, once its students are in.
            standard = standard_rate(class_name)
            outcome = db.create_class_and_first_session(
                name=class_name,
                teacher_id=teacher_id,
                hourly_rate=max(standard or hourly_rate, 0.01),
                display_color=PALETTE[index % len(PALETTE)],
                student_ids=roster,
                session_date=first["date"],
                start_time=first["start_time"],
                end_time=first["end_time"],
                status="Completed" if first["date"] <= dt.date.today() else "Scheduled",
                note="",
                attendance_rows=_attendance_rows(first, names_here),
            )
            if outcome != "created":
                # "teacher_conflict": the teacher already has a class at that time (usually a day
                # repeated across two month sheets), so skip it. "duplicate": another teacher's
                # subject has this name and it wasn't renamed, so skip it rather than file it
                # under the wrong teacher.
                created[f"class_{outcome}"] += 1
                continue
            created["classes"] += 1
            created["sessions_created"] += 1
            period_stats[(first["date"].year, first["date"].month)]["created"] += 1
            # Look up the new subject's id by name, instead of reloading every class.
            class_id = db.get_class_id_by_name(class_name)
            existing[class_name.casefold()] = class_id
            start_at = 1

        for session in class_sessions[start_at:]:
            status = "Completed" if session["date"] <= dt.date.today() else "Scheduled"
            attendance_rows = _attendance_rows(session, names_here)
            month_key = (session["date"].year, session["date"].month)
            existing_session_id = db.find_schedule_session(
                class_id, session["date"], session["start_time"]
            )

            if existing_session_id is None:
                outcome = db.create_schedule_session(
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

            # Already on the calendar: keep each student's Paid tick and the class note,
            # which the workbook doesn't have.
            current = db.get_schedule_session(existing_session_id) or {}
            paid_by_student = {
                row["student_id"]: row["is_paid"] for row in current.get("Attendance", [])
            }
            for row in attendance_rows:
                row["is_paid"] = paid_by_student.get(row["student_id"], False)

            # Skip a class the workbook still describes exactly as saved, so re-uploading a
            # month to add one class doesn't rewrite all the others.
            if _matches_stored(session, status, attendance_rows, current):
                created["sessions_unchanged"] += 1
                continue

            outcome = db.update_schedule_session(
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

    db.remember_lesson_cells(teacher_id, {
        (existing[name.casefold()], item["date"], item["start_time"]): item["cell"]
        for name, items in by_class.items() if name.casefold() in existing
        for item in items if item.get("cell")
    })

    # Price every subject in this workbook that still has the placeholder: by its
    # students' grades, or $60 by default. Done now that the lessons and students are in.
    imported_class_ids = {
        existing[name.casefold()] for name in by_class if name.casefold() in existing
    }
    priced, _ = price_by_rule(db.get_unpriced_subjects(class_ids=imported_class_ids))
    if priced:
        created["priced_by_rule"] = priced

    for (year, month), stats in period_stats.items():
        db._record_import(
            teacher_id,
            year,
            month,
            sessions_created=stats["created"],
            sessions_updated=stats["updated"],
            warning_count=warnings_by_month.get((year, month), 0),
        )

    if listed_twice:
        created["listed_twice"] = len(listed_twice)
    return {"status": "imported", "created": dict(created), "listed_twice": listed_twice}


# ---------------------------------------------------------------------------
# Choosing the months, and saying what an upload will change
# ---------------------------------------------------------------------------


def _month_of(day: dt.date) -> tuple[int, int]:
    """Return a date's (year, month)."""
    return day.year, day.month


def months_found(preview: dict[str, Any]) -> list[dict[str, Any]]:
    """List each month an upload covers, in order, with its classes and the days shown.

    `own` is False for a month the worksheets only spill into (the first day or
    two of the next month at the end of a month sheet).
    """
    classes = Counter(_month_of(session["date"]) for session in preview.get("sessions") or [])
    days = Counter(_month_of(day) for day in preview.get("dates_shown") or [])
    own = set(map(tuple, preview.get("sheet_months") or [])) or set(classes) | set(days)
    return [
        {"month": month, "classes": classes[month], "days": days[month], "own": month in own}
        for month in sorted(set(classes) | set(days))
    ]


def default_months(preview: dict[str, Any], teacher_id: int | None,
                   today: dt.date | None = None) -> list[tuple[int, int]]:
    """Return the months an upload imports unless the admin picks others.

    That is the months the app already has for this teacher, and any later ones.
    Earlier months are left out because they were billed outside the app, and
    importing them would put them up for billing again. A month the sheets only
    spill into is left to its own sheet. A new teacher starts from this month.
    """
    today = today or dt.date.today()
    held = db.get_teacher_months(teacher_id) if teacher_id else []
    onwards = max(held) if held else (today.year, today.month)
    return [
        item["month"] for item in months_found(preview)
        if item["own"] and (item["month"] in held or item["month"] >= onwards)
    ]


def restrict_to_months(preview: dict[str, Any], months) -> dict[str, Any]:
    """Return the upload cut down to the chosen months' lessons, days and name questions."""
    months = set(map(tuple, months))
    sessions = [s for s in preview.get("sessions") or [] if _month_of(s["date"]) in months]
    names = {a["student_name"] for s in sessions for a in s["attendance"]}
    warnings = [w for w in preview.get("warnings") or []
                if not w.get("date") or _month_of(w["date"]) in months]
    return {
        **preview,
        "sessions": sessions,
        "dates_shown": (None if preview.get("dates_shown") is None else
                        [d for d in preview["dates_shown"] if _month_of(d) in months]),
        "name_reviews": [r for r in preview.get("name_reviews") or []
                         if any(name in names for name in r["names"])],
        "warnings": warnings,
        "session_count": len(sessions),
        "unique_student_count": len(names),
        "warning_count": len(warnings) + sum(len(s["warnings"]) for s in sessions),
    }


def mark_names_on_file(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Start the name question on "keep separate" when both spellings are already two students.

    They were told apart before (at an earlier import or by hand), so
    defaulting to "merge" would undo that and move classes between siblings.
    """
    if not reviews:
        return reviews
    name_to_id = {**db.get_student_aliases(),
                  **{item["Name"].casefold(): item["ID"] for item in db.get_all_students()}}
    marked = []
    for review in reviews:
        ids = [name_to_id.get(name.casefold()) for name in review["names"]]
        if None not in ids and ids[0] != ids[1]:
            review = {**review, "likely_same": False,
                      "reason_text": review["reason_text"] + " -- two different students on file"}
        marked.append(review)
    return marked


def lessons_on_file(teacher_id: int | None, year: int, month: int) -> list[dict[str, Any]]:
    """Return a teacher's month as the app has it, as lessons for Create/Edit schedule.

    One per class in date order: date, start, end, subject, the students in
    the order they were added, and each one's mark if they have one
    ({name: "Online" | "Recording" | "Cancelled"}). A class cancelled as a
    whole marks everyone Cancelled, which is how a sheet says it.
    """
    if not teacher_id:
        return []
    rosters = db.get_month_attendance(teacher_id, year, month)
    lessons = []
    for lesson in sorted(db.get_month_schedule(teacher_id, year, month), key=lambda item: (item["Date"], item["Start"])):
        roster = rosters.get(lesson["ID"], [])
        statuses = {}
        for row in roster:
            if row["is_cancelled"] or lesson["Status"] == "Cancelled":
                statuses[row["student_name"]] = CANCELLED
            elif row["has_recording"]:
                statuses[row["student_name"]] = RECORDING
            elif row["is_online"]:
                statuses[row["student_name"]] = ONLINE
        lessons.append({"date": db.as_date(lesson["Date"]), "start": db.as_time(lesson["Start"]),
                        "end": db.as_time(lesson["End"]), "subject": lesson["Class"],
                        "students": [row["student_name"] for row in roster], "statuses": statuses})
    return lessons


def preview_from_lessons(lessons: list[dict[str, Any]], year: int, month: int) -> dict[str, Any]:
    """Return planned lessons as an upload that shows every day of the month.

    Saving it is the same as uploading a sheet of that month: the app's month
    is made to match, a class no longer listed is taken off (credited if
    invoiced), and everything else is matched as any upload is.
    """
    days = calendar.monthrange(year, month)[1]
    return {
        "sessions": [
            {"date": lesson["date"], "class_name": lesson["subject"], "start_time": lesson["start"],
             "end_time": lesson["end"], "warnings": [],
             "attendance": [{"student_name": name, "status": (lesson.get("statuses") or {}).get(name, ATTENDING)}
                            for name in lesson["students"]]}
            for lesson in lessons
        ],
        "dates_shown": [dt.date(year, month, day) for day in range(1, days + 1)],
    }


def plan_import(preview: dict[str, Any], teacher_id: int | None,
                name_overrides: dict[str, str] | None = None,
                student_matches: dict[str, int] | None = None,
                dates=None, today: dt.date | None = None,
                not_added: list[dict] | None = None) -> dict[tuple[int, int], Counter]:
    """Work out what an upload would change, month by month, without saving anything.

    Follows backfill step by step (same names, same students, same lessons
    removed, same clashes) so the screen can show the result before the import.
    For each month it counts: new, changed, unchanged, removed, changed_invoiced,
    removed_invoiced, held (only its date has passed), clash (the teacher already
    has a class then) and skipped (another teacher's subject has that name).
    A list passed as `not_added` is filled with each clash and skipped lesson
    and the reason, so the screen can point at its cell in the workbook.
    """
    today = today or dt.date.today()
    preview = use_subject_spellings(preview, teacher_id)
    sessions, _ = _final_sessions(preview.get("sessions") or [], name_overrides or {})
    counts: dict[tuple[int, int], Counter] = defaultdict(Counter)
    teacher = next((t for t in db.get_all_teachers() if t["ID"] == teacher_id), None)
    if teacher is None:
        for session in sessions:
            counts[_month_of(session["date"])]["new"] += 1
        return counts

    students_on_file = db.get_all_students()
    name_to_id = {**(student_matches or {}), **db.get_student_aliases(),
                  **{item["Name"].casefold(): item["ID"] for item in students_on_file}}
    # A name not on file becomes a new student, so it can't match anyone saved.
    for session in sessions:
        for entry in session["attendance"]:
            name_to_id.setdefault(entry["student_name"].casefold(), -len(name_to_id) - 1)
    same_name, rosters, taught = _same_name_rules(teacher_id, students_on_file)

    existing = _teacher_classes(teacher)
    taken_elsewhere = {item["Class"].casefold() for item in db.get_all_classes()} - set(existing)
    by_class: dict[str, list[dict]] = defaultdict(list)
    for session in sessions:
        by_class[session["class_name"]].append(session)
    covered = _covered_dates(sessions, dates)
    class_ids, keep_slots, filled_slots = _workbook_slots(sessions, by_class, existing)
    stored = db.get_teacher_lessons_on(teacher_id, covered | {s["date"] for s in sessions})

    on_calendar: dict[dt.date, list[tuple]] = defaultdict(list)
    for (class_id, day, start), lesson in stored.items():
        if db.workbook_drops(class_id, day, start, covered, keep_slots, class_ids, filled_slots):
            month = counts[_month_of(day)]
            month["removed"] += 1
            month["removed_invoiced"] += lesson["Invoiced"]
        else:
            on_calendar[day].append((lesson["Start"], lesson["End"], lesson["ID"]))

    def clashes(session, excluded=None):
        # A lesson this upload adds has no id yet (None), but still takes up its time.
        """Return True if a lesson would overlap one already on the calendar that day."""
        return any(start < session["end_time"] and end > session["start_time"]
                   and (excluded is None or lesson_id != excluded)
                   for start, end, lesson_id in on_calendar[session["date"]])

    def leave_out(session, reason):
        """Count a lesson that won't go in, and note it for the screen."""
        counts[_month_of(session["date"])][reason] += 1
        if not_added is not None:
            not_added.append({**session, "reason": reason})

    for class_name, class_sessions in sorted(by_class.items()):
        class_sessions = sorted(class_sessions, key=lambda item: (item["date"], item["start_time"]))
        class_id = existing.get(class_name.casefold())
        if class_id is None:
            if class_name.casefold() in taken_elsewhere or clashes(class_sessions[0]):
                reason = "skipped" if class_name.casefold() in taken_elsewhere else "clash"
                for session in class_sessions:
                    leave_out(session, reason)
                continue
        names_here = _names_for_class(class_id, name_to_id, same_name, rosters, taught)
        for session in class_sessions:
            month = counts[_month_of(session["date"])]
            current = stored.get((class_id, session["date"], session["start_time"])) if class_id else None
            if current is None:
                if clashes(session):
                    leave_out(session, "clash")
                    continue
                on_calendar[session["date"]].append((session["start_time"], session["end_time"], None))
                month["new"] += 1
                continue
            status = "Completed" if session["date"] <= today else "Scheduled"
            rows = _attendance_rows(session, names_here)
            if _matches_stored(session, status, rows, current):
                month["unchanged"] += 1
            elif (current["Status"], status) == ("Scheduled", "Completed") and _matches_stored(
                    session, "Scheduled", rows, current):
                # Nothing in the workbook changed; only the class's date has passed.
                month["held"] += 1
            elif clashes(session, excluded=current["ID"]):
                leave_out(session, "clash")
            else:
                month["changed"] += 1
                month["changed_invoiced"] += current["Invoiced"]
    return counts
