"""Writes a parsed Excel schedule into the database.

``schedule_parser`` reads a workbook into sessions; this writes those sessions
in through the same functions the Schedule tab uses, so imported data is
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
import threading
from collections import Counter, defaultdict
from typing import Any

import db
from schedule_parser import (
    GRADE_TAG, JUNIOR_RATE, MERGE_THRESHOLD, _bare, _fold, _suffix, hangul_fit, name_sound,
    rate_for_grade, standard_rate, without_brackets,
)

__all__ = [
    "backfill",
    "price_rule",
    "price_by_rule",
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
    reviews = preview.get("name_reviews") or []
    # The spelling already on file wins, so the child stays the child on file:
    # a teacher writing "Bae soojin" most weeks and "Bae Sujin" (on file)
    # once made the merge land on the new spelling -- a second student.
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
        # Kept for the import to remember: next month's workbook will spell
        # the child both ways again, and shouldn't need asking.
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


def grades_text(grades: list[int]) -> str:
    """[9, 10] -> "Grades 9–10"; [9, 11] -> "Grades 9 and 11"; [12] -> "Grade 12"."""
    if len(grades) == 1:
        return f"Grade {grades[0]}"
    if grades == list(range(grades[0], grades[-1] + 1)):
        return f"Grades {grades[0]}–{grades[-1]}"
    return "Grades " + ", ".join(map(str, grades[:-1])) + f" and {grades[-1]}"


def price_rule(subjects: list[dict], rates: list[dict] | None = None) -> dict[int, tuple[float, str, bool]]:
    """What the academy's price rule makes each subject: ``{class_id: (rate, why, certain)}``.

    Grade 11 and above is $65/h, everything else $60/h. The grade comes from
    the subject's name; failing that the subject's own price from another
    month stands ("Basic Eng", $65 from August); failing that its students'
    grades elsewhere decide. ``certain`` is False where it is the $60
    default -- a group either side of the line, or nobody's grade known --
    which only ever fills a subject that has no price, never replaces one
    somebody chose. ``rates`` is ``db.get_all_class_rates()`` when the caller
    already has it.
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
    """Price every subject listed (from ``db.get_unpriced_subjects``) by the rule.

    Each from the first month it has no price, all in one commit, so nothing
    clicked meanwhile can leave half of them priced. A subject priced again
    part-way through -- placeholder in spring, a real price in summer,
    placeholder again since -- still has a gap after one pass; another closes
    it. Returns how many were priced and any still without a price.
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
    """A name with any grade written in front of it removed: "G5 Sohee" -> "Sohee".

    A name that is nothing but a grade is left whole; reduced to "" it would
    match every other such name.
    """
    stripped = re.sub(r"^(?:g|y|gr\.?|grade)\s?\d{1,2}\b[\s.:\-]*", "", name.strip(), flags=re.I)
    return stripped or name.strip()


def _name_words(name: str) -> list[str]:
    return [_fold(word) for word in re.findall(r"[A-Za-z]+", without_brackets(name))]


def _close_spelling(parsed: str, full: str) -> bool:
    """A letter or two apart, in either word order: "Kim Taewo" and "Taewoo Kim"."""
    ordered = [" ".join(sorted(_bare(name).split())) for name in (parsed, full)]
    return max(difflib.SequenceMatcher(None, _bare(parsed), _bare(full)).ratio(),
               difflib.SequenceMatcher(None, *ordered).ratio()) >= MERGE_THRESHOLD


def _shortened(parsed: str, full: str) -> bool:
    """Whether ``parsed`` is ``full`` cut short or with a name added.

    "Hana" for "Park Hana Leong", "Yerin" for "Seo Yerin",
    "Minsooo" for "Minsoo Kim" (a spelling a letter or two longer), or
    "Emma Nam Jihoon" for "Nam Jihoon" (an English name in front).
    """
    mine, theirs = _name_words(parsed), _name_words(full)
    if len(theirs) < 2 or not mine:
        return False
    if len(mine) == 1:
        word = mine[0]
        return any(word == other or (min(len(word), len(other)) >= 5 and abs(len(word) - len(other)) <= 2
                                     and (word.startswith(other) or other.startswith(word)))
                   for other in theirs)
    return len(mine) == len(theirs) + 1 and set(theirs) <= set(mine)


def suggest_student_matches(sessions: list[dict[str, Any]], teacher_id: int | None = None) -> list[dict[str, Any]]:
    """Parsed names that might already be an existing student under a
    different spelling, tag, or capitalisation -- flagged for a human
    decision rather than either silently merging into the existing record
    or silently creating a second one.

    A name that already matches an existing student exactly
    (case-insensitive) is left alone here -- there's no ambiguity to
    resolve, same as always.
    """
    existing = db.get_all_students()
    exact = {item["Name"].casefold() for item in existing} | set(db.get_student_aliases())
    # The teacher's own students, for names they shorten: "Hana" means
    # their Park Hana Leong, not every Hana in the academy.
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
                # A student imported as "G5 Sohee" before the parser learned
                # to take a grade off the front of a name, now read as "Sohee".
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
        # Said the same way, however far apart the letters: the other order
        # ("Kim Hayun", "Hayun Kim"), another romanisation ("Sohn Taemin",
        # "Tae Min Sohn"), a nickname in brackets. Compared letter by letter
        # these were never close enough to ask about, so each came in as a
        # new student and the child's bills split in two.
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
        # Cut short, with an English name added, or typed a letter or two
        # wrong, by the teacher who teaches them: taken for that student when
        # only one of theirs fits. Said with the academy's other students in
        # mind it would be a guess; among one teacher's, it is their student.
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
        # Written in Hangul -- "다혜" for the "Dahye" on file. Taken for her when
        # she is the only student it fits; asked about when several do.
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
            # Spelled differently but said the same -- Jaaemin and Jaemin, Kyubin
            # and Gyubin -- and by nobody else on file: the same child, so
            # "new person" by default would split their bills in two. Said
            # the same as two students, it's still asked, but not assumed.
            if best["reason"] in ("spelling", "sound") and best["existing_id"] in alike:
                best["reason"] = "sound"
            # Only a grade apart, and from one student only: that student
            # already has this name's invoices, so saying "new person" by
            # default would bill every one of those classes again.
            # A nickname added in brackets -- "Park Jisoo(Emma)" for the
            # "Park Jisoo" on file -- is the same child, when only one
            # student on file has that name and theirs carries no tag of its
            # own (two tagged "Park Hana(A)" and "(B)" are two children).
            # Not a grade in brackets, though: "Nam Jihoon(G9)" is how a
            # teacher tells a ninth-grader from the Nam Jihoon in grade 12.
            nickname = (best["reason"] == "tag" and best["tag"] and not best["existing_tag"]
                        and not GRADE_TAG.match(best["tag"]) and bare_names[bare_new] == 1)
            # A given name alone -- "Yuna" in one workbook, "Yoona" on file
            # -- is too common a name to settle it without asking.
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


# One import at a time. A second run of the app can start while the first is
# still busy -- a second click on "Commit import", or the app open in another
# tab -- and two imports side by side each check that a new student isn't on
# file yet, then each add them: eleven students were put on file
# twice that way, a fraction of a second apart. Waiting here, the second
# import finds everything the first one did already in place.
_ONE_IMPORT_AT_A_TIME = threading.Lock()


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
    with _ONE_IMPORT_AT_A_TIME:
        return _backfill(preview, teacher_id, hourly_rate, name_overrides, student_matches)


def _backfill(preview, teacher_id, hourly_rate, name_overrides, student_matches):
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
    # A spelling already known to be a student on file -- merged into them,
    # or confirmed at an earlier import -- is them; their own name wins.
    students_on_file = db.get_all_students()
    name_to_id = {**db.get_student_aliases(),
                  **{item["Name"].casefold(): item["ID"] for item in students_on_file}}
    # Two students with exactly one name -- two girls called Suhyun -- can't
    # be told apart by the name, so which class they're in decides: the one
    # already in that class, else the one this teacher teaches. Picking one
    # for every class moved a child's month onto the other girl.
    same_name: dict[str, list[int]] = defaultdict(list)
    for item in students_on_file:
        same_name[item["Name"].casefold()].append(item["ID"])
    same_name = {name: ids for name, ids in same_name.items() if len(ids) > 1}
    rosters = db.get_teacher_rosters(teacher_id) if same_name else {}
    taught = set().union(*rosters.values()) if rosters else set()
    confirmed = []
    for session in sessions:
        for entry in session["attendance"]:
            key = entry["student_name"].casefold()
            if key in name_to_id:
                continue
            if key in student_matches:
                # The admin confirmed this spelling is an existing student --
                # not a new one, even though it doesn't match exactly. Kept,
                # so next month's workbook saying it again needs no asking.
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
        names_here = name_to_id
        if same_name:
            names_here = dict(name_to_id)
            for name, ids in same_name.items():
                for pool in (rosters.get(class_id, set()), taught):
                    fits = [student_id for student_id in ids if student_id in pool]
                    if len(fits) == 1:
                        names_here[name] = fits[0]
                        break

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
            # The academy's price follows the grade in the subject's name, so
            # a class that says what grade it teaches arrives priced. One that
            # does not -- "Basic Eng", a class the sheet never named -- stays
            # on the placeholder, which billing refuses to issue at, so
            # somebody has to choose the figure rather than inherit one.
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

            # Already on the calendar -- carry each student's Paid flag and
            # the class's note across, since Excel has no opinion on either.
            current = db.get_schedule_session(existing_session_id) or {}
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

    # No subject is left on the placeholder. A name with a grade was priced
    # as it was created; the rest are priced here, once their lessons are in
    # and their students' grades can be read -- else the $60 default. A
    # placeholder only ever meant an invoice nobody could send.
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

    return {"status": "imported", "created": dict(created)}
