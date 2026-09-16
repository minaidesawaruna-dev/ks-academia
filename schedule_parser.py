"""Parser for the KS Academia teacher schedule workbooks.

The workbooks are calendar grids rather than tables:

* Usually each worksheet is one month (``Aug``, ``Sep``, ``July`` ...).
  Some teachers keep several months, or a whole year, on one sheet
  (``Jan- Feb 2026``, ``2026``): the months then sit side by side, each
  named in the row above its day labels (``JAN.2026``, ``FEB``, ``SEP``).
* One header row holds day labels such as ``15(SAT)``.  A day label is
  usually merged over two columns: the first column holds the class cell,
  the second holds status cells (Online / Recording / Cancelled).
* One or more columns hold the 30 minute time axis and are repeated across
  the sheet purely for readability.
* A class cell looks like::

      G11 Chem HL B
      9am-11am
      Nam Jihoon
      Oh Minseok
      ...

* A status cell looks like::

      Recording
      Yoon Chaewon
      Nam Jihoon

  and may contain several labelled sections in one cell.

The public API is intentionally small:

``get_sheet_names(source)``
``infer_month(sheet_name)``
``parse_schedule(source, sheet_name, year)``
``parse_workbook(source, sheet_names, year)``
"""

from __future__ import annotations

import calendar
import datetime as dt
import difflib
import io
import re
from collections import Counter, defaultdict
from typing import Any, Iterable

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

__all__ = [
    "get_sheet_names",
    "canonicalise_names",
    "infer_month",
    "parse_schedule",
    "parse_workbook",
    "ATTENDING",
    "ONLINE",
    "RECORDING",
    "CANCELLED",
]

# --------------------------------------------------------------------------
# Status vocabulary
# --------------------------------------------------------------------------

ATTENDING = "Attending"
ONLINE = "Online"
RECORDING = "Recording"
CANCELLED = "Cancelled"

# Order matters: "cancel" is checked first so that a line such as
# "Cancelled online class" is treated as a cancellation.
STATUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (CANCELLED, re.compile(r"cancel+ed|cancell?ed|cancel|취소|결석", re.I)),
    # "ONLIN" and "REC" as teachers shorten them: "Sohee(ONLIN)", "Yerin(REC)".
    (ONLINE, re.compile(r"on\s*-?\s*line|(?<![a-z])onlin(?![a-z])|zoom|온라인", re.I)),
    (RECORDING, re.compile(r"record(?:ing|ed)?|(?<![a-z])rec(?![a-z])|녹화|영상", re.I)),
)

# Words that may sit beside a status keyword without being a student name.
_LABEL_FILLER = {
    "review",
    "only",
    "lesson",
    "class",
    "student",
    "students",
    "all",
    "수업",
    "학생",
}

# --------------------------------------------------------------------------
# Regular expressions
# --------------------------------------------------------------------------

# "15(SAT)", and "3(Thu)" with "ONLINE" beside or under it for a day taught online.
DAY_HEADER_RE = re.compile(
    r"^\s*(?P<day>\d{1,2})\s*[\(\[]?\s*(?P<dow>[A-Za-z]{2,9})?\.?\s*[\)\]]?"
    r"(?:\s*(?P<mode>(?i:online|zoom)|온라인))?\s*$"
)

TIME_RANGE_RE = re.compile(
    r"(?P<sh>\d{1,2})\s*(?:[.:]\s*(?P<sm>\d{2}))?\s*(?P<sap>[ap]\.?\s?m\.?)?"
    r"\s*[-~\u2010-\u2015]\s*"
    r"(?P<eh>\d{1,2})\s*(?:[.:]\s*(?P<em>\d{2}))?\s*(?P<eap>[ap]\.?\s?m\.?)?",
    re.I,
)

WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

# Month words as they appear in sheet names and in the labels above a month's
# day columns, lower-case, longest first so "june" is tried before "jun".
MONTH_NUMBERS = {
    **{calendar.month_name[index].lower(): index for index in range(1, 13)},
    **{calendar.month_abbr[index].lower(): index for index in range(1, 13)},
    "sept": 9,
}
_MONTH_WORD = "|".join(sorted(MONTH_NUMBERS, key=len, reverse=True))
_MONTH_WORD_RE = re.compile(rf"(?<![a-z])(?:{_MONTH_WORD})(?![a-z])")

# A month label must *start* with its month -- "JAN.2026", " MARCH 2026",
# "8월", "2026 JAN" -- because the rest of the cell is often a note, and notes
# are full of other dates ("APRIL / 5월2일-6월4일 한국출국") that must not be
# read as the month. "5월2일" on its own is a date, not a month, hence the
# lookahead. Matched against lower-cased text.
MONTH_LABEL_RE = re.compile(
    r"^[\s\W_]*"
    r"(?:(?P<lead>(?:19|20)\d{2})\s*년?[\s.\-/]*)?"
    rf"(?:(?P<word>{_MONTH_WORD})(?![a-z])|(?P<num>\d{{1,2}})\s*월(?!\s*\d))"
    r"(?:[\s.,/\-]*(?P<year>(?:19|20)\d{2})(?!\d))?"
)

# Words that make a line a class rather than a person. Each word is checked
# piece by piece ("Lang&Lit" is "lang" and "lit"). A grade or a subject word
# is enough on its own; a school code or a two-letter IB tag only supports
# one, because on its own it could as easily be part of a name.
_STRONG_SUBJECT = {
    "math", "maths", "mathematics", "eng", "english", "econ", "econs", "economics",
    "chem", "chemistry", "bio", "biology", "phys", "physics", "science", "sci",
    "lang", "lit", "literature", "language", "tok", "igcse", "ib", "dp", "myp",
    "toefl", "tofel", "ielts", "essay", "writing", "reading", "speaking",
    "listening", "grammar", "history", "geography", "geo", "psychology", "psych",
    "business", "calculus", "algebra", "precal", "drill", "mock", "korean",
    "chinese", "japanese", "french", "spanish", "coding", "수학", "영어", "과학",
    "국어", "알지브라", "지오", "특강", "개별진도",
}
_WEAK_SUBJECT = {"sl", "hl", "aa", "ai", "ee", "ia", "ap", "sat", "pre", "cal", "uwc",
                 "sjii", "acsi", "sas", "ofs", "sais", "nlcs", "cis", "tts"}
_GRADE_WORD = re.compile(r"^(?:g|y|gr)\d{1,2}$")

# Cells that sit on the timetable but are not lessons anyone is billed for.
NON_LESSON_RE = re.compile(r"(?i)meeting|consult|interview|상담|설명회|보조\s*강사|회의|면접")

# Things written onto a student's line that are not part of their name.
_GRADE_PREFIX = re.compile(r"^(?:g|y|gr\.?|grade)\s?(?P<grade>\d{1,2})\b[\s.:\-]*(?P<rest>.+)$", re.I)
_UNTIL_NOTE = re.compile(
    r"^(?P<name>.+?)\s+(?P<note>(?:until|till|til|from|after|before|left at|came at)\b.*\d.*)$",
    re.I)
_SLASH_NOTE = re.compile(r"^(?P<name>.+?)\s*/\s*(?P<note>half|late|early|trial|make-?up)$", re.I)
# "Sohee,nam" is one student -- given name, then the surname in lower case --
# not Sohee and a second student called "nam".
_SURNAMES = frozenset(
    "kim lee yi rhee park pak bak choi choe jung jeong chung kang cho jo yoon yun "
    "jang chang lim im han oh seo suh shin sin kwon hwang ahn an song yoo yu ryu "
    "hong jeon chun ko go moon mun yang son sohn bae baek heo huh nam noh roh ha "
    "kwak sung seong cha joo ju woo koo gu na uhm eom won chae pyo byun byeon bang "
    "gong kong yeo choo tak ma gil seok hyun hyeon sim shim".split()
)
_AWAY_NOTE = re.compile(r"여행|한국|휴가|병가|아파|출국|귀국")
# Words that make a free-standing note an absence worth putting in front of whoever imports:
# "안옴" didn't come, "없었음" wasn't there, "참여불가" can't attend, "스킵" skip,
# "필드트립" field trip, "학교행사" school event, "한국행"/"출국" off to Korea.
# A bare "없음" is left out -- "답변없음" is no reply, "필요없음" not needed.
_ABSENCE_WORD = re.compile(
    r"(?i)결석|병가|아파|캔슬|휴가|여행|불참|취소|absent|cancel|안\s*옴|안\s*왔|없었|\s없음"
    r"|참여\s*(?:불가|안\s*함)|스킵|skip|트립|(?<![a-z])trip|학교\s*행사|출국|한국행"
)
# Of those, the words that say the student was absent. "한국" (in Korea) and
# the rest only say where someone was -- they may well have joined online.
_ABSENT_NOTE = re.compile(r"병가|아파|캔슬|휴가|여행")

# Zero-width and non-breaking characters that survive copy-paste into Excel and
# make two visually identical names compare as different strings.
INVISIBLE = dict.fromkeys(
    map(ord, "\u2060\u200b\u200c\u200d\ufeff\u00a0"), " "
)

MERGE_THRESHOLD = 0.88


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _as_stream(source: Any) -> io.BytesIO:
    """Accept raw bytes, a path, or a file-like object."""
    if isinstance(source, (bytes, bytearray)):
        return io.BytesIO(bytes(source))
    if hasattr(source, "read"):
        data = source.read()
        if hasattr(source, "seek"):
            try:
                source.seek(0)
            except Exception:  # pragma: no cover - non seekable stream
                pass
        return io.BytesIO(data)
    with open(source, "rb") as handle:
        return io.BytesIO(handle.read())


# "6pm7.30pm": two times with the dash between them left out.
_JOINED_TIMES = re.compile(
    r"(?i)(\d{1,2}(?:[.:]\d{2})?\s*[ap]\.?\s?m\.?)\s*(?=\d{1,2}(?:[.:]\d{2})?\s*[ap]\.?\s?m)"
)


# "9.30m - 11am": the a or p of am/pm dropped, leaving a lone m before the dash.
_STRAY_M = re.compile(r"(?i)(?<![\d.:])(\d{1,2}(?:[.:]\d{2})?)\s*m(?=\s*[-~‐-―])")


def _cell_lines(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    return [_JOINED_TIMES.sub(r"\1-", _STRAY_M.sub(r"\1", line.strip()))
            for line in value.splitlines() if line.strip()]


def normalise_name(name: str) -> str:
    """Lower-case, drop bracketed aliases and punctuation, collapse spaces."""
    text = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", name.translate(INVISIBLE))
    text = re.sub(r"[^\w\s\uac00-\ud7a3]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip().lower()


def _name_key(name: str) -> tuple[str, ...]:
    return tuple(sorted(normalise_name(name).split()))


def looks_like_name(text: str) -> bool:
    cleaned = text.strip()
    if len(cleaned) < 2:
        return False
    if not re.search(r"[A-Za-z\uac00-\ud7a3]", cleaned):
        return False
    tokens = [t for t in normalise_name(cleaned).split() if t not in _LABEL_FILLER]
    return bool(tokens)


# --------------------------------------------------------------------------
# Month / time parsing
# --------------------------------------------------------------------------


UNNAMED_CLASS = "(unnamed class)"


def infer_month(sheet_name: str) -> int:
    """Return the 1-12 month number encoded in a worksheet name.

    A name that spans several months -- "Jan- Feb 2026", "FEB-MAR 2026",
    "2026(1-7월)" -- means the sheet *starts* at the first of them and its
    day columns roll forward from there, so the first month wins. Taking a
    later one dates every lesson on the sheet a month or more late.
    """
    text = str(sheet_name).strip().lower()
    if not text:
        raise ValueError("The worksheet name is empty.")

    korean = re.search(r"(\d{1,2})\s*(?:[-~]\s*\d{1,2}\s*)?월", text)
    if korean:
        month = int(korean.group(1))
        if 1 <= month <= 12:
            return month

    # Whole month words, earliest in the name first: "Summary June" is June,
    # not the "mar" buried inside "summary".
    words = [(match.start(), MONTH_NUMBERS[match.group(0)])
             for match in _MONTH_WORD_RE.finditer(text)]
    if words:
        return min(words)[1]

    # Anything else keeps the old reading: a month anywhere in the text.
    candidates: list[tuple[int, int]] = []
    for index in range(1, 13):
        for label in (calendar.month_name[index], calendar.month_abbr[index]):
            position = text.find(label.lower())
            if position != -1:
                candidates.append((len(label), index))
    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]

    # "Sheet2" and "시트3" are Excel's default names, not February and March.
    numeric = [] if re.search(r"sheet|시트", text) else re.findall(r"\d{1,2}", text)
    for value in numeric:
        month = int(value)
        if 1 <= month <= 12:
            return month

    raise ValueError(f"Could not work out a month from the worksheet name {sheet_name!r}.")


def infer_year(sheet_name: str, fallback: int) -> int:
    """The year a worksheet names, or ``fallback`` when it names none.

    Some workbooks keep years apart by sheet -- "2026.Jun", "2025.Dec" -- and
    span two or three of them in one file. Taking the year from the name means
    those sheets land on the right dates instead of every one of them being
    forced into whichever year was picked on the upload form, which silently
    moves a class onto the wrong day of the week.

    Only a full four-digit year counts. A trailing "26" could as easily be a
    day or a class number, and guessing wrong is worse than asking.
    """
    match = re.search(r"(19|20)\d{2}", str(sheet_name))
    return int(match.group(0)) if match else int(fallback)


def _sheet_years(sheet_name: str) -> set[int]:
    """Every year a worksheet's name gives, with "2025-26" counting as both."""
    text = str(sheet_name)
    years = {int(y) for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", text)}
    for first, second in re.findall(r"(?<!\d)((?:19|20)\d{2})\s*[-~/]\s*(\d{2})(?!\d)", text):
        years.add(int(first[:2] + second))
    return years


def read_month_label(value: Any) -> tuple[int, int | None] | None:
    """``(month, year or None)`` for a label above a month's columns, else None.

    Several teachers keep many months -- or a whole year -- on one sheet, side
    by side, each named in the row above its day labels: "JAN.2026",
    "FEB 2026", "JUNE", "AUG(8/26, 8/29~9/2)". See MONTH_LABEL_RE.
    """
    if not isinstance(value, str):
        return None
    match = MONTH_LABEL_RE.match(value.strip().lower())
    if not match:
        return None
    if match.group("word"):
        month = MONTH_NUMBERS[match.group("word")]
    else:
        month = int(match.group("num"))
        if not 1 <= month <= 12:
            return None
    year = match.group("lead") or match.group("year")
    return month, (int(year) if year else None)


def _month_labels(worksheet, header_row: int) -> tuple[int | None, list[tuple[int, int, int | None, str]]]:
    """The row of month labels above the day labels, read left to right.

    Returns ``(row, [(column, month, year or None, text), ...])`` for the row
    above the day labels holding the most month labels, or ``(None, [])``
    when there is none -- as when the day labels are themselves in row 1.
    """
    best_row, best = None, []
    for row in range(1, header_row):
        found = []
        for column in range(1, (worksheet.max_column or 1) + 1):
            value = worksheet.cell(row=row, column=column).value
            label = read_month_label(value)
            if label:
                found.append((column, label[0], label[1], " ".join(str(value).split())))
        if len(found) > len(best):
            best_row, best = row, found
    return best_row, best


def _weekday_agreement(columns, days, month: int, year: int) -> int:
    """How many of these day labels name the right weekday for that month."""
    agree = 0
    for column in columns:
        day, dow = days[column]
        if dow is None:
            continue
        try:
            agree += WEEKDAYS[dow] == dt.date(year, month, day).weekday()
        except ValueError:
            continue
    return agree


def _plan_months(sheet_name, label_row, labels, days, name_month, year):
    """Decide which month and year each day column belongs to.

    Returns ``(blocks, notes)``. A block is a run of day columns read as one
    month -- rolling into the next where the day numbers restart, as always
    -- with ``hint`` saying where its month and year came from, for the
    weekday-mismatch warning. ``notes`` are ``(row, column, message)``.

    With no month labels, or a single label on a sheet whose name gives a
    month, the whole sheet is one block dated by its name: exactly how every
    one-month-per-sheet workbook has always been read, so none of them
    changes. Otherwise each label starts a block running to the next label.
    """
    columns = sorted(days)
    named_years = _sheet_years(sheet_name)
    notes: list[tuple[int | None, int | None, str]] = []

    if name_month is None and not named_years:
        # Only a sheet named for its year ("2026", "2026(1-7월)") is read by
        # its labels. Anything else with no month in its name -- "세미나",
        # "Holiday", a student list -- is not a timetable, however much it
        # looks like one: a seminar sign-up sheet has day labels and a "JAN"
        # above them, and importing it would bill everyone who came.
        return [], [(None, None,
                     "skipped — the worksheet name gives neither a month nor a "
                     "year; a sheet holding several months should be named for "
                     "its year, such as '2026'.")]

    if not labels or (len(labels) == 1 and name_month is not None):
        if name_month is None:
            return [], [(None, None,
                         "skipped — the worksheet name gives no month, and no month "
                         "labels such as 'JAN.2026' sit above the day labels.")]
        block_year = year
        hint = "check the worksheet name" if named_years else "check the import year"
        if labels:
            column, label_month, label_year, text = labels[0]
            name = calendar.month_name[name_month]
            if label_month != name_month:
                notes.append((label_row, column,
                              f"the month label '{text}' says "
                              f"{calendar.month_name[label_month]}, but the worksheet "
                              f"is named for {name}; read as {name}."))
            if label_year is not None and not named_years:
                # "Jan" with "Jan 2025" above it: the name has no year, the
                # label does, and the label beats the year on the upload form.
                block_year, hint = label_year, f"check the month label '{text}'"
            elif label_year is not None and label_year not in named_years:
                notes.append((label_row, column,
                              f"the month label '{text}' says {label_year}, but the "
                              f"worksheet is named for {year}; read as {year}."))
        block = {"columns": columns, "month": name_month, "year": block_year,
                 "hint": hint, "label": None}
        if not named_years and not (labels and labels[0][2] is not None):
            # Nothing on the sheet says which year; only the upload form does.
            _fit_year_to_weekdays([block], days, year, notes)
        return [block], notes

    blocks = []
    leading = [column for column in columns if column < labels[0][0]]
    if leading:
        month = labels[0][1]
        blocks.append({"columns": leading, "month": 12 if month == 1 else month - 1,
                       "year": None, "explicit": False, "label": None})
    for index, (column, month, label_year, text) in enumerate(labels):
        end = labels[index + 1][0] if index + 1 < len(labels) else float("inf")
        blocks.append({"columns": [c for c in columns if column <= c < end],
                       "month": month, "year": label_year,
                       "explicit": label_year is not None,
                       "label": text, "label_column": column})

    # A block labelled with a year the sheet isn't for -- "Jan 2025" at the
    # start of the "2026" sheet -- is a leftover copied from elsewhere; the
    # same block turns up, cell for cell, in several teachers' workbooks.
    # Importing it would invent lessons, so it is skipped, and said so.
    kept = []
    for block in blocks:
        if block["explicit"] and named_years and block["year"] not in named_years:
            if block["columns"]:
                notes.append((label_row, block["label_column"],
                              f"'{block['label']}' is on the '{sheet_name}' worksheet, "
                              f"so its lessons were skipped as a leftover; if they are "
                              f"real, move them to the {block['year']} worksheet."))
            continue
        kept.append(block)
    blocks = kept
    if not blocks:
        return [], notes

    # Fill in missing years from the nearest labelled one, stepping over a
    # year end whenever the month goes backwards (DEC then JAN).
    anchor = next((i for i, block in enumerate(blocks) if block["explicit"]), None)
    guessed = anchor is None and not named_years
    if anchor is None:
        anchor = 1 if blocks[0]["label"] is None and len(blocks) > 1 else 0
        blocks[anchor]["year"] = min(named_years) if named_years else year
    for i in range(anchor + 1, len(blocks)):
        if blocks[i]["year"] is None:
            before = blocks[i - 1]
            blocks[i]["year"] = before["year"] + (blocks[i]["month"] < before["month"])
    for i in range(anchor - 1, -1, -1):
        if blocks[i]["year"] is None:
            after = blocks[i + 1]
            blocks[i]["year"] = after["year"] - (blocks[i]["month"] > after["month"])

    for block in blocks:
        if block["label"] is not None:
            block["hint"] = (f"check the month label '{block['label']}'" if block["explicit"]
                             else f"check the month label '{block['label']}' and its year")

    if guessed:
        # No label and no worksheet name gives a year; the weekdays can.
        _fit_year_to_weekdays(blocks, days, year, notes)

    # Day columns before the first label have no month of their own. They
    # are nearly always the month before it, but the weekday labels decide.
    if blocks[0]["label"] is None and len(blocks) > 1:
        lead, first = blocks[0], blocks[1]
        if (_weekday_agreement(lead["columns"], days, first["month"], first["year"])
                > _weekday_agreement(lead["columns"], days, lead["month"], lead["year"])):
            lead["month"], lead["year"] = first["month"], first["year"]
        when = f"{calendar.month_name[lead['month']]} {lead['year']}"
        lead["hint"] = f"these columns have no month label and were read as {when}"
        notes.append((label_row, lead["columns"][0],
                      f"the columns before '{first['label']}' have no month label; "
                      f"read as {when}."))

    return [block for block in blocks if block["columns"]], notes


def _fit_year_to_weekdays(blocks, days, form_year: int, notes) -> None:
    """Move a guessed year to the one the day labels actually fit.

    Every day label names its weekday -- "3(TUE)" -- and a date falls on a
    given weekday in only one of any three neighbouring years. So when
    neither the labels nor the worksheet name write a year, and the year
    came off the upload form, the weekdays settle it: a wrong form year
    otherwise dates every lesson on the sheet a year out. The year only
    moves when the guess fits at most half the labelled days and exactly
    one neighbouring year fits every one of them -- a few mistyped labels
    never move it.
    """
    labelled = sum(1 for block in blocks for column in block["columns"] if days[column][1])
    if labelled < 2:
        return

    def misfits(delta: int) -> int:
        return sum(
            len(_resolve_dates({column: days[column] for column in block["columns"]},
                               block["month"], block["year"] + delta)[1])
            for block in blocks
        )

    if misfits(0) * 2 < labelled:
        return
    fits = [delta for delta in (-1, 1) if misfits(delta) == 0]
    if len(fits) != 1:
        return
    fitted = form_year + fits[0]
    for block in blocks:
        block["year"] += fits[0]
        block["hint"] = "the year was worked out from the day labels"
    notes.append((None, None,
                  f"no year is written on this sheet, and its day labels don't fit "
                  f"{form_year} (the year on the upload form); they fit {fitted}, so "
                  f"it was read as {fitted}."))


def _months_label(months: list[tuple[int, int]], fallback: int | None) -> str:
    """"August", "Jan–Sep 2026" or "Jun 2025 – Jan 2026", for the summary table."""
    months = sorted(set(months))
    if not months:
        return calendar.month_name[fallback] if fallback else "—"
    if len(months) == 1:
        return calendar.month_name[months[0][1]]
    (y0, m0), (y1, m1) = months[0], months[-1]
    if y0 == y1:
        return f"{calendar.month_abbr[m0]}–{calendar.month_abbr[m1]} {y0}"
    return f"{calendar.month_abbr[m0]} {y0} – {calendar.month_abbr[m1]} {y1}"


def _repair_time_range(start, end, grid_time, rows, step_minutes):
    """Put right a time range that can't be what the teacher meant.

    A range that ends before it starts, or runs past five hours, is almost
    always a slip: "11.30pm-1.30pm" on the 11:30 row, "12.30am-2.30pm" on the
    12:30 row. The grid is the second opinion. Moving one end by twelve hours
    is accepted only when that puts the start on the row the cell sits on and
    leaves a sensible length; failing that, a backwards range whose start
    matches its row takes its end from how many rows the cell spans.

    Returns ``(start, end, message or None)``, or None when a backwards range
    has no safe reading -- it would bill as nothing, so it must not slip
    through as though it were fine. A long range with no fix is kept, flagged.
    """
    base = dt.date(2000, 1, 1)
    begin, finish = dt.datetime.combine(base, start), dt.datetime.combine(base, end)
    length = finish - begin
    five, twelve = dt.timedelta(hours=5), dt.timedelta(hours=12)
    if dt.timedelta(0) < length <= five:
        return start, end, None
    backwards = length <= dt.timedelta(0)
    problem = "ends before it starts" if backwards else f"lasts {length.total_seconds() / 3600:g} hours"
    if grid_time is not None:
        grid = dt.datetime.combine(base, grid_time)
        for new_begin, new_finish in ((begin - twelve, finish), (begin + twelve, finish),
                                      (begin, finish + twelve), (begin, finish - twelve)):
            if new_begin.date() != base or new_finish.date() != base:
                continue
            if (abs((new_begin - grid).total_seconds()) <= 45 * 60
                    and dt.timedelta(0) < new_finish - new_begin <= five):
                fixed = (new_begin.time(), new_finish.time())
                return fixed[0], fixed[1], (
                    f"the time range reads {start:%H:%M}-{end:%H:%M}, which {problem}; "
                    f"the cell sits on the {grid_time:%H:%M} row, so it was read as "
                    f"{fixed[0]:%H:%M}-{fixed[1]:%H:%M}.")
        if backwards and rows and abs((begin - grid).total_seconds()) <= 15 * 60:
            new_finish = begin + dt.timedelta(minutes=rows * step_minutes)
            if new_finish.date() == base:
                return start, new_finish.time(), (
                    f"the time range reads {start:%H:%M}-{end:%H:%M}, which {problem}; "
                    f"the cell spans {rows} rows from {grid_time:%H:%M}, so it was "
                    f"read as {start:%H:%M}-{new_finish:%H:%M}.")
    if backwards:
        return None
    return start, end, (f"the time range reads {start:%H:%M}-{end:%H:%M}, which "
                        f"{problem} — check the time.")


def _to_time(hour: int, minute: int, meridiem: str | None) -> dt.time:
    if meridiem:
        flag = meridiem.replace(".", "").replace(" ", "").lower()
        if flag.startswith("p") and hour != 12:
            hour += 12
        elif flag.startswith("a") and hour == 12:
            hour = 0
    hour = hour % 24
    return dt.time(hour, minute)


def parse_time_range(text: str) -> tuple[dt.time, dt.time] | None:
    """Parse ``9am-11am`` / ``11.15am-1.15pm`` / ``5pm-6.30pm`` style ranges."""
    match = TIME_RANGE_RE.search(text)
    if not match:
        return None

    start_hour = int(match.group("sh"))
    end_hour = int(match.group("eh"))
    if not (0 < start_hour <= 24 and 0 < end_hour <= 24):
        return None

    start_minute = int(match.group("sm") or 0)
    end_minute = int(match.group("em") or 0)
    start_flag = match.group("sap")
    end_flag = match.group("eap")

    # Academy hours run 08:00-22:00, so infer the missing am/pm marker.
    if start_flag is None and end_flag is None:
        start_flag = "am" if start_hour >= 8 and start_hour != 12 else "pm"
        end_flag = "am" if end_hour >= 8 and end_hour != 12 and end_hour > start_hour else "pm"
    elif start_flag is None:
        start_flag = end_flag
        if _to_time(start_hour, start_minute, start_flag) >= _to_time(
            end_hour, end_minute, end_flag
        ):
            start_flag = "am" if end_flag.lower().startswith("p") else "pm"
    elif end_flag is None:
        end_flag = start_flag
        if _to_time(end_hour, end_minute, end_flag) <= _to_time(
            start_hour, start_minute, start_flag
        ):
            end_flag = "pm" if start_flag.lower().startswith("a") else "am"

    start = _to_time(start_hour, start_minute, start_flag)
    end = _to_time(end_hour, end_minute, end_flag)
    return start, end


def _is_time_line(line: str) -> bool:
    """True when the line is only a time range (not a class name)."""
    match = TIME_RANGE_RE.search(line)
    if not match:
        return False
    remainder = (line[: match.start()] + line[match.end():]).strip()
    remainder = re.sub(r"[\s\-~()\[\].,:]", "", remainder)
    return remainder == ""


def status_from_line(line: str) -> tuple[str | None, str]:
    """Return ``(status, leftover_text)`` for a possible status label line."""
    for status, pattern in STATUS_PATTERNS:
        match = pattern.search(line)
        if match:
            leftover = (line[: match.start()] + " " + line[match.end():]).strip()
            # Drop the brackets the status word sat in -- "Sohee(ONLINE)" --
            # but keep a real tag: "Sohee(G6) ONLINE" must stay "Sohee(G6)",
            # not lose its closing bracket to a blanket strip.
            leftover = re.sub(r"[\(\[]\s*[\)\]]", " ", leftover).strip(" -:/,.")
            opened = leftover.count("(") + leftover.count("[")
            closed = leftover.count(")") + leftover.count("]")
            if closed > opened and leftover.endswith((")", "]")):
                leftover = leftover[:-1].rstrip(" -:/,.")
            elif opened > closed and leftover.startswith(("(", "[")):
                leftover = leftover[1:].lstrip(" -:/,.")
            tokens = [
                token
                for token in normalise_name(leftover).split()
                if token not in _LABEL_FILLER
            ]
            return status, leftover if tokens else ""

    # Catch single-word typos such as "Reording" or "Onilne".
    token = re.sub(r"[^a-z]", "", line.lower())
    if len(token) >= 5:
        for status, keyword in ((CANCELLED, "cancelled"), (ONLINE, "online"), (RECORDING, "recording")):
            if difflib.SequenceMatcher(None, token, keyword).ratio() >= 0.82:
                return status, ""

    return None, line


# --------------------------------------------------------------------------
# Cell parsing
# --------------------------------------------------------------------------


def _subject_strength(word: str) -> int:
    """2 for a grade or subject word, 1 for a school code or IB tag, else 0."""
    if word.strip() == "1:1":
        return 1
    best = 0
    for piece in re.split(r"[&/_\-+.,()\[\]:]+", word.lower()):
        if not piece:
            continue
        if _GRADE_WORD.match(piece) or piece in _STRONG_SUBJECT:
            return 2
        if piece in _WEAK_SUBJECT:
            best = 1
    return best


def _is_subject_line(line: str) -> bool:
    return any(_subject_strength(word) == 2 for word in line.split())


def _is_person_line(line: str) -> bool:
    untagged = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", line)
    return (looks_like_name(line) and not re.search(r"\d", untagged)
            and not any(_subject_strength(word) for word in untagged.split()))


def _split_subject_line(line: str) -> tuple[str, str | None]:
    """"UWC Lang&Lit Jihoon" -> ("UWC Lang&Lit", "Jihoon"): a name typed onto a class."""
    words = line.split()
    last = max((i for i, word in enumerate(words) if _subject_strength(word)), default=None)
    if last is None or last == len(words) - 1:
        return line, None
    rest = " ".join(words[last + 1:])
    return (" ".join(words[: last + 1]), rest) if _is_person_line(rest) else (line, None)


def _roster_names(line: str, notes: list[str]) -> list[tuple[str, str | None, str | None]]:
    """The students one roster line names, as ``[(name, note, status), ...]``.

    Teachers write more than a name on a student's line, and each extra is a
    student or a bill that doesn't exist: "Jihoon,Yerin" is two students, not
    one called "Jihoon,Yerin"; "G5 Sohee" is Sohee, in grade 5, not a
    different Sohee each school year; "Minseok until 8pm" and "Minseok/half"
    are Minseok with a note. A student marked off sick or away -- "Sohee(병가)",
    "수민여행" -- stays on the roster as a cancellation (``status``), so they
    are neither billed as present nor silently dropped. A part that is no
    name at all -- "2명적용", someone "in Korea" -- goes to ``notes``.
    """
    parts = [part.strip() for part in re.split(r"\s*[,，]\s*", line) if part.strip()]
    if (len(parts) == 2 and " " not in parts[0] and parts[1].islower()
            and parts[1] in _SURNAMES):
        line, parts = f"{parts[0]} {parts[1].capitalize()}", []
    found: list[tuple[str, str | None, str | None]] = []
    for part in parts if len(parts) > 1 else [line.strip()]:
        tag = re.search(r"[\(\[]([^\)\]]*)[\)\]]", part)
        if tag and _ABSENT_NOTE.search(tag.group(1)):
            name = (part[: tag.start()] + part[tag.end():]).strip()
            if _is_person_line(name):
                found.append((name, tag.group(1).strip(), CANCELLED))
                continue
        absent = None if tag else _ABSENT_NOTE.search(part)
        if absent and not part[absent.end():].strip():
            name = part[: absent.start()].strip()
            if name and _is_person_line(name):
                found.append((name, part[absent.start():].strip(), CANCELLED))
                continue
        if _AWAY_NOTE.search(part):
            notes.append(part)
            continue
        name, note = part, None
        match = _SLASH_NOTE.match(name) or _UNTIL_NOTE.match(name)
        if match:
            name, note = match["name"].strip(), match["note"].strip()
        match = _GRADE_PREFIX.match(name)
        if match and _is_person_line(match["rest"]):
            name = match["rest"].strip()
            note = f"G{int(match['grade'])}" + (f"; {note}" if note else "")
        if re.search(r"\d", re.sub(r"[\(\[][^\)\]]*[\)\]]", "", name)):
            notes.append(part)
        elif looks_like_name(name):
            found.append((name, note, None))
        else:
            notes.append(part)
    return found


_CLASS_WORD = re.compile(r"수업|특강|과외|(?i:lesson|class)")
_AMPM = re.compile(r"(?i)\d\s*(?:[.:]\s*\d{2})?\s*[ap]\.?\s?m")


def _time_trailer(after: str):
    """What follows a lesson time on its line, or None if it can't follow one.

    ``("none", None)``, ``("note", None)`` for a room or a slip of the keyboard
    ("11층", "(6pm KR)", the "m" of "5pm-6.30m"), ``("status", STATUS)`` for
    "ONLINE", or ``("names", text)`` for students.
    """
    if not after:
        return ("none", None)
    if after.lower() == "m" or re.fullmatch(r"\(.*\)", after) or re.fullmatch(r"\d+\s*층", after):
        return ("note", None)
    status, leftover = status_from_line(after)
    if status is not None and not leftover:
        return ("status", status)
    parts = [part for part in re.split(r"\s*[,/]\s*", after) if part]
    if parts and all(_is_person_line(part) for part in parts):
        return ("names", after)
    return None


def _time_on_line(line: str):
    """The lesson time a line holds, as ``(time_range, class_text, trailer)``, else None.

    The time usually has a line of its own. Some teachers put more on it: the
    class before it ("G12 Chem HL 1.30pm-3.30pm", "관리수업11am-1pm"), students
    or a status after it ("2pm-4pm Nam Jihoon", "4pm-5.30pm ONLINE"), a room
    ("10am-1.30pm 11층"). Only a time with am or pm counts when it shares a
    line, and only beside a class, a name, a status or a room -- so a date in
    a note, or a shift such as "TA 9am-4pm", never turns into a lesson.
    """
    if _is_time_line(line):
        parsed = parse_time_range(line)
        return (parsed, "", ("none", None)) if parsed else None
    match = TIME_RANGE_RE.search(line)
    if not match or not _AMPM.search(match.group(0)):
        return None
    parsed = parse_time_range(match.group(0))
    if not parsed:
        return None
    before = line[: match.start()].strip(" -/|:,")
    if before and not (_is_subject_line(before) or _CLASS_WORD.search(before)):
        return None
    trailer = _time_trailer(line[match.end():].strip(" -/|:,"))
    return None if trailer is None else (parsed, before, trailer)


def _is_remark(line: str) -> bool:
    """A line that is plainly a note rather than something the parser failed to read.

    A reminder with a date or time in it, someone being away, a meeting or an
    assistant's shift. A line holding an am/pm time range is *not* a remark
    unless it is one of those: it may be a lesson in a layout not yet known,
    and that is worth a warning.
    """
    if NON_LESSON_RE.search(line) or re.match(r"\s*TA\b", line):
        return True
    if TIME_RANGE_RE.search(line) and _AMPM.search(line):
        return False
    return bool(_AWAY_NOTE.search(line)
                or re.search(r"\d", re.sub(r"[\(\[][^\)\]]*[\)\]]", "", line)))


def parse_cell(text: str) -> dict[str, Any]:
    """Split a calendar cell into class name, time range and student entries.

    Returns ``{"class_lines", "time_range", "entries", "notes"}`` where each
    entry is ``{"name", "status"}`` and ``status`` is ``None`` for a plain
    roster line.
    """
    # Two classes can share a cell, an empty line between them, each with its
    # own time. Only the first is this cell's lesson -- a teacher's timetable
    # holds one class at a time -- so the rest is handed back for a warning
    # instead of being read as more students of the first.
    extra_classes: list[str] = []
    if isinstance(text, str):
        chunks = re.split(r"\n[ \t]*\n", text)
        timed = [index for index, chunk in enumerate(chunks)
                 if any(_is_time_line(line) for line in _cell_lines(chunk))]
        if len(timed) > 1:
            extra_classes = ["\n".join(chunks[timed[1]:]).strip()]
            text = "\n".join(chunks[: timed[1]])

    lines = _cell_lines(text)
    time_range = None
    time_index = None
    trailer_status: str | None = None
    class_lines: list[str] = []
    body = lines
    for index, line in enumerate(lines):
        found = _time_on_line(line)
        if found is None:
            continue
        time_range, before, (kind, value) = found
        time_index = index
        class_lines = lines[:index] + ([before] if before else [])
        body = lines[index + 1:]
        if kind == "status":
            trailer_status = value  # "4pm-5.30pm ONLINE": the whole lesson was online
        elif kind == "names":
            body = [value] + body
        break

    if time_index is not None:
        if not class_lines and body and _is_subject_line(body[0]):
            # The time written first, then the class: "11am-12.30pm / G11 TOK".
            subject, student = _split_subject_line(body[0])
            class_lines = [subject]
            body = ([student] if student else []) + body[1:]
        elif (len(class_lines) > 1 and not body and _is_subject_line(class_lines[0])
              and all(_is_person_line(line) for line in class_lines[1:])):
            # The student written above the time: "G6 Math 1:1 / Sohee / 2pm-3.30pm".
            # Only when nothing follows the time; otherwise a class name's
            # second line has to stay part of the class name.
            body = class_lines[1:]
            class_lines = class_lines[:1]

    entries: list[dict[str, Any]] = []
    notes: list[str] = []
    current_status: str | None = trailer_status

    for line in body:
        if _is_time_line(line):
            # A second time among the students -- those who joined late. It
            # is not a student; the lesson keeps the cell's first time.
            notes.append(line)
            continue
        status, leftover = status_from_line(line)
        if status is not None:
            if not leftover:
                # A label on its own line -- "Online" -- and the students under it.
                current_status = status
                continue
            # A status on a student's own line -- "Sohee(Online)", "Jihoon(당일취소)" --
            # is theirs alone. Carried down, it billed nobody below a sick student.
            tag = re.search(r"[\(\[]([^\)\]]*)[\)\]]", line)
            if tag and status_from_line(tag.group(1))[0] == status:
                name = (line[: tag.start()] + line[tag.end():]).strip()
                if name and _is_person_line(name):
                    extra = status_from_line(tag.group(1))[1]
                    entries.append({"name": name, "status": status,
                                    "note": tag.group(1).strip() if extra else None})
                    continue
            for name, note, own_status in _roster_names(leftover, []):
                entries.append({"name": name, "status": own_status or status,
                                "note": note})
            continue

        if line.startswith("(") and entries:
            entries[-1]["note"] = line.strip("()")
            continue

        for name, note, own_status in _roster_names(line, notes):
            entries.append({"name": name, "status": own_status or current_status,
                            "note": note})

    return {
        "class_lines": class_lines,
        "time_range": time_range,
        "entries": entries,
        "notes": notes,
        "extra_classes": extra_classes,
    }


# --------------------------------------------------------------------------
# Roster matching
# --------------------------------------------------------------------------


class _Roster:
    """Matches names found in status cells back onto the class roster."""

    def __init__(self) -> None:
        self.names: list[str] = []
        self._by_exact: dict[str, int] = {}
        self._by_tokens: dict[tuple[str, ...], int] = {}

    def add(self, name: str) -> int:
        key = normalise_name(name)
        if key in self._by_exact:
            return self._by_exact[key]
        index = len(self.names)
        self.names.append(name)
        self._by_exact[key] = index
        self._by_tokens.setdefault(_name_key(name), index)
        return index

    def match(self, name: str) -> tuple[int | None, str | None]:
        """Return ``(index, warning_kind)``; index is ``None`` when unmatched."""
        key = normalise_name(name)
        if key in self._by_exact:
            return self._by_exact[key], None

        token_key = _name_key(name)
        if token_key in self._by_tokens:
            return self._by_tokens[token_key], "reordered"

        scored = sorted(
            (
                (difflib.SequenceMatcher(None, key, normalise_name(candidate)).ratio(), index)
                for index, candidate in enumerate(self.names)
            ),
            reverse=True,
        )
        if scored:
            best_score, best_index = scored[0]
            runner_up = scored[1][0] if len(scored) > 1 else 0.0
            if best_score >= 0.90 and best_score - runner_up >= 0.05:
                return best_index, "fuzzy"
        return None, None


# --------------------------------------------------------------------------
# Sheet geometry
# --------------------------------------------------------------------------


def _find_header_row(worksheet, max_scan: int = 8) -> tuple[int, dict[int, tuple[int, str | None]]]:
    """Find the row holding ``15(SAT)`` style day labels."""
    best_row = 0
    best_days: dict[int, tuple[int, str | None]] = {}
    limit = min(max_scan, worksheet.max_row or max_scan)

    for row in range(1, limit + 1):
        days: dict[int, tuple[int, str | None]] = {}
        for column in range(1, (worksheet.max_column or 1) + 1):
            value = worksheet.cell(row=row, column=column).value
            if not isinstance(value, str):
                continue
            match = DAY_HEADER_RE.match(value)
            if not match:
                continue
            day = int(match.group("day"))
            dow = (match.group("dow") or "").strip().lower() or None
            if 1 <= day <= 31 and (dow is None or dow in WEEKDAYS):
                days[column] = (day, dow)
        if len(days) > len(best_days):
            best_days = days
            best_row = row
    return best_row, best_days


def _time_axis(worksheet, header_row: int) -> tuple[dict[int, dt.time], set[int]]:
    """Return ``(row -> time)`` and the set of columns used as a time axis."""
    counts: dict[int, int] = {}
    row_times: dict[int, dt.time] = {}

    for column in range(1, (worksheet.max_column or 1) + 1):
        hits = 0
        for row in range(header_row + 1, (worksheet.max_row or 1) + 1):
            value = worksheet.cell(row=row, column=column).value
            if isinstance(value, dt.datetime):
                value = value.time()
            if isinstance(value, dt.time):
                hits += 1
        if hits >= 3:
            counts[column] = hits

    for column in counts:
        for row in range(header_row + 1, (worksheet.max_row or 1) + 1):
            value = worksheet.cell(row=row, column=column).value
            if isinstance(value, dt.datetime):
                value = value.time()
            if isinstance(value, dt.time):
                row_times.setdefault(row, value)

    return row_times, set(counts)


def _fill_key(cell) -> str | None:
    """A comparable identity for a cell's background fill.

    Excel stores a fill three different ways -- a literal RGB value, an index
    into the theme palette, or an indexed legacy colour -- and two cells that
    look identical on screen can be stored differently.  Returns ``None`` for
    an unfilled cell, and a string that only compares equal to a fill of the
    same kind.
    """
    fill = cell.fill
    if fill is None or fill.patternType != "solid":
        return None
    colour = fill.fgColor
    if colour is None:
        return None
    if colour.type == "rgb" and isinstance(colour.rgb, str):
        return f"rgb:{colour.rgb}:{colour.tint or 0:.3f}"
    if colour.type == "theme" and isinstance(colour.theme, int):
        return f"theme:{colour.theme}:{colour.tint or 0:.3f}"
    if colour.type == "indexed" and isinstance(colour.indexed, int):
        return f"indexed:{colour.indexed}"
    return None


def _merged_lookup(worksheet) -> dict[tuple[int, int], tuple[int, int, int, int]]:
    lookup: dict[tuple[int, int], tuple[int, int, int, int]] = {}
    for merged in worksheet.merged_cells.ranges:
        bounds = (merged.min_row, merged.min_col, merged.max_row, merged.max_col)
        lookup[(merged.min_row, merged.min_col)] = bounds
    return lookup


def _row_step_minutes(row_times: dict[int, dt.time]) -> int:
    rows = sorted(row_times)
    deltas = []
    for first, second in zip(rows, rows[1:]):
        if second - first != 1:
            continue
        start = dt.datetime.combine(dt.date.today(), row_times[first])
        end = dt.datetime.combine(dt.date.today(), row_times[second])
        minutes = int((end - start).total_seconds() // 60)
        if 0 < minutes <= 120:
            deltas.append(minutes)
    if not deltas:
        return 30
    return max(set(deltas), key=deltas.count)


def _warning(
    message: str,
    *,
    sheet: str,
    coordinate: str | None = None,
    date: dt.date | None = None,
    cell_text: Any = None,
) -> dict[str, Any]:
    """One warning, kept structured so the screen can point at the cell.

    ``text`` repeats the single line the parser has always produced, so
    anything that only prints warnings keeps working unchanged; the separate
    fields let the import screen show the worksheet, the cell reference and
    what is actually typed in that cell.
    """
    where = f"{sheet}!{coordinate}" if coordinate else sheet
    stamp = f" ({date.isoformat()})" if date else ""
    return {
        "sheet": sheet,
        "coordinate": coordinate,
        "cell": where if coordinate else None,
        "date": date,
        "message": message,
        "cell_text": cell_text if isinstance(cell_text, str) else None,
        "text": f"{where}{stamp}: {message}",
    }


def _resolve_dates(
    days: dict[int, tuple[int, str | None]],
    month: int,
    year: int,
    hint: str = "check the import year",
) -> tuple[dict[int, dt.date], list[tuple[int, str]]]:
    """Map header columns to real dates, rolling over into the next month.

    Warnings come back as ``(column, message)`` so the caller can point at
    the day header cell they came from.
    """
    dates: dict[int, dt.date] = {}
    warnings: list[tuple[int, str]] = []
    current_month, current_year = month, year
    previous_day = 0

    for column in sorted(days):
        day, dow = days[column]
        if day < previous_day:  # the sheet spilled into the following month
            current_month += 1
            if current_month > 12:
                current_month, current_year = 1, current_year + 1
        previous_day = day

        last_day = calendar.monthrange(current_year, current_month)[1]
        if day > last_day:
            warnings.append(
                (
                    column,
                    f"day {day} does not exist in "
                    f"{calendar.month_name[current_month]} {current_year}; "
                    "this column was skipped.",
                )
            )
            continue

        value = dt.date(current_year, current_month, day)
        if dow and WEEKDAYS[dow] != value.weekday():
            warnings.append(
                (
                    column,
                    f"{value.isoformat()} is a "
                    f"{calendar.day_abbr[value.weekday()]} but the sheet says "
                    f"{dow.upper()} — {hint}.",
                )
            )
        dates[column] = value

    return dates, warnings


# --------------------------------------------------------------------------
# Sheet parsing
# --------------------------------------------------------------------------


def _parse_sheet(worksheet, month: int | None, year: int):
    """Read one worksheet into ``(sessions, warnings, months)``.

    ``month`` is the month the worksheet's name gives, or None when it gives
    none -- a sheet called "2026", which names its months above the day
    labels instead. ``months`` is the ``(year, month)`` of each month the
    sheet was read as, for the summary table; empty when nothing was read.
    """
    sheet_name = worksheet.title
    warnings: list[dict[str, Any]] = []

    header_row, days = _find_header_row(worksheet)
    if not days:
        if month is None:
            return [], [_warning(
                "skipped — the worksheet name gives no month and there are no day "
                "labels such as '15(SAT)', so it doesn't look like a schedule.",
                sheet=sheet_name)], []
        return [], [
            _warning("no day headers such as '15(SAT)' were found.", sheet=sheet_name)
        ], []

    row_times, time_columns = _time_axis(worksheet, header_row)
    if not row_times:
        warnings.append(
            _warning(
                "no time axis column was found; grid times unavailable.",
                sheet=sheet_name,
            )
        )
    step_minutes = _row_step_minutes(row_times)
    merged = _merged_lookup(worksheet)
    # "3(Thu)" with "ONLINE" under it: that day's lessons were taught online.
    online_days = set()
    for column in days:
        label = DAY_HEADER_RE.match(str(worksheet.cell(row=header_row, column=column).value or ""))
        if label and label.group("mode"):
            online_days.add(column)

    label_row, labels = _month_labels(worksheet, header_row)
    blocks, notes = _plan_months(sheet_name, label_row, labels, days, month, year)
    for note_row, note_column, message in notes:
        cell = (worksheet.cell(row=note_row, column=note_column)
                if note_row and note_column else None)
        warnings.append(_warning(message, sheet=sheet_name,
                                 coordinate=cell.coordinate if cell else None,
                                 cell_text=cell.value if cell else None))
    if not blocks:
        return [], warnings, []

    dates: dict[int, dt.date] = {}
    for block in blocks:
        block_dates, date_warnings = _resolve_dates(
            {column: days[column] for column in block["columns"]},
            block["month"], block["year"], block["hint"])
        dates.update(block_dates)
        for column, message in date_warnings:
            header_cell = worksheet.cell(row=header_row, column=column)
            warnings.append(
                _warning(
                    message,
                    sheet=sheet_name,
                    coordinate=header_cell.coordinate,
                    cell_text=header_cell.value,
                )
            )

    # A month standing many months apart from the next one on the same sheet
    # is almost always a block copied in from elsewhere. At the very start of
    # a sheet it is the copied "Jan 2025" template -- found cell for cell in
    # several teachers' workbooks -- so it is skipped; elsewhere, only flagged.
    for position, (block, following) in enumerate(zip(blocks, blocks[1:])):
        here = [dates[c] for c in block["columns"] if c in dates]
        there = [dates[c] for c in following["columns"] if c in dates]
        if not (block["label"] and here and there
                and (min(there) - max(here)).days > 150):
            continue
        gap = ((min(there).year - max(here).year) * 12
               + min(there).month - max(here).month)
        if position == 0:
            for column in block["columns"]:
                dates.pop(column, None)
            message = (f"'{block['label']}' is {gap} months before the next month, "
                       "at the very start of the sheet -- the mark of a block copied "
                       "in from another sheet -- so its lessons were skipped as a "
                       "leftover.")
        else:
            message = (f"'{block['label']}' is {gap} months before the next month on "
                       "this sheet — check it isn't a leftover copied from another sheet.")
        label_cell = worksheet.cell(row=label_row, column=block["label_column"])
        warnings.append(_warning(message, sheet=sheet_name,
                                 coordinate=label_cell.coordinate,
                                 cell_text=block["label"]))

    # A late lesson is sometimes written under the last time on the axis --
    # 9.30pm below a column that stops at 9pm. Those rows are read too, but
    # only for lessons: anything else down there is a notes row, and letting a
    # note become a status cell would hang it on whichever class sat above.
    axis_end = max(row_times) if row_times else (worksheet.max_row or header_row)
    max_row = min(worksheet.max_row or axis_end, axis_end + 20) if row_times else axis_end
    max_column = worksheet.max_column or 1
    header_columns = sorted(days)

    sessions: list[dict[str, Any]] = []

    for position, anchor in enumerate(header_columns):
        if anchor not in dates:
            continue
        session_date = dates[anchor]

        # The day block runs to the next day header or the next time column.
        block_end = max_column
        if position + 1 < len(header_columns):
            block_end = header_columns[position + 1] - 1
        for column in range(anchor + 1, block_end + 1):
            if column in time_columns:
                block_end = column - 1
                break
        block_columns = [
            column
            for column in range(anchor, block_end + 1)
            if column not in time_columns
        ]

        classes: list[dict[str, Any]] = []
        status_cells: list[tuple[int, int, dict[str, Any], Any, Any]] = []
        attached: set[tuple[int, int]] = set()
        absence_notes: list[tuple[str, str]] = []

        for column in block_columns:
            for row in range(header_row + 1, max_row + 1):
                cell = worksheet.cell(row=row, column=column)
                if not isinstance(cell.value, str) or not cell.value.strip():
                    continue
                # Non-anchor cells of a merged range read back as None, so
                # anything with text here is the anchor of its block.
                bounds = merged.get((row, column))
                parsed = parse_cell(cell.value)
                span_end = bounds[2] if bounds else row

                if parsed["time_range"] is not None:
                    start, end = parsed["time_range"]
                    repaired = _repair_time_range(
                        start, end, row_times.get(row),
                        span_end - row + 1 if bounds else None, step_minutes)
                    if repaired is None:
                        warnings.append(
                            _warning(
                                f"the time range reads {start:%H:%M}-{end:%H:%M}, "
                                "which ends before it starts, and the grid row gives "
                                "no safe reading; this lesson was skipped — fix the "
                                "time in the cell.",
                                sheet=sheet_name,
                                coordinate=cell.coordinate,
                                date=session_date,
                                cell_text=cell.value,
                            )
                        )
                        continue
                    start, end, time_note = repaired
                    parsed["time_range"] = (start, end)
                    duration = (
                        dt.datetime.combine(dt.date.today(), end)
                        - dt.datetime.combine(dt.date.today(), start)
                    ).total_seconds() / 60
                    rows_needed = max(1, int(round(duration / step_minutes)))
                    classes.append(
                        {
                            "row": row,
                            "column": column,
                            "end_row": (span_end if row > axis_end
                                        else max(span_end, row + rows_needed - 1)),
                            "parsed": parsed,
                            "coordinate": cell.coordinate,
                            "text": cell.value,
                            "fill": _fill_key(cell),
                            "time_note": time_note,
                        }
                    )
                elif row > axis_end:
                    if _ABSENCE_WORD.search(cell.value):
                        absence_notes.append((cell.coordinate, cell.value))
                else:
                    status_cells.append(
                        (row, column, parsed, _fill_key(cell), cell.value)
                    )
                    # A teacher's reminder -- a call to someone's mother at
                    # 5pm, the dates a student is away -- is plainly a note;
                    # only lines the parser couldn't place are worth a warning.
                    unplaced = [note for note in parsed["notes"] if not _is_remark(note)]
                    if not parsed["entries"] and unplaced:
                        warnings.append(
                            _warning(
                                f"note ignored — {' / '.join(unplaced)}",
                                sheet=sheet_name,
                                coordinate=cell.coordinate,
                                date=session_date,
                                cell_text=cell.value,
                            )
                        )

        # Trim a class span so it cannot swallow the next class in its column.
        for item in classes:
            following = [
                other["row"]
                for other in classes
                if other["column"] == item["column"] and other["row"] > item["row"]
            ]
            if following:
                item["end_row"] = min(item["end_row"], min(following) - 1)

        for item in classes:
            parsed = item["parsed"]
            start, end = parsed["time_range"]
            class_name = " ".join(
                re.sub(r"\s+", " ", line).strip() for line in parsed["class_lines"]
            ).strip()
            session_warnings: list[dict[str, Any]] = []

            # "TA" in capitals is a teaching assistant's shift: "2.30pm-4pm TA".
            if (NON_LESSON_RE.search(item["text"])
                    or re.search(r"(?<![A-Za-z])TA(?![A-Za-z])", item["text"])):
                warnings.append(
                    _warning(
                        "this looks like a meeting, consultation or staff shift "
                        "rather than a lesson, so it was not imported.",
                        sheet=sheet_name,
                        coordinate=item["coordinate"],
                        date=session_date,
                        cell_text=item["text"],
                    )
                )
                continue

            for extra in parsed.get("extra_classes", []):
                session_warnings.append(
                    _warning(
                        "a second class is written in this cell after an empty line "
                        f"({' / '.join(_cell_lines(extra))[:90]}); it was not imported, "
                        "because a teacher's timetable holds one class at a time. "
                        "Give it its own cell.",
                        sheet=sheet_name,
                        coordinate=item["coordinate"],
                        date=session_date,
                        cell_text=item["text"],
                    )
                )

            if not class_name:
                class_name = UNNAMED_CLASS
                session_warnings.append(
                    _warning(
                        "the class name is missing above the time range — this "
                        "cell starts straight at the time, so the class imports "
                        "under a placeholder until it is given a subject at import.",
                        sheet=sheet_name,
                        coordinate=item["coordinate"],
                        date=session_date,
                        cell_text=item["text"],
                    )
                )

            grid_time = row_times.get(item["row"])
            if grid_time and abs(
                (
                    dt.datetime.combine(dt.date.today(), grid_time)
                    - dt.datetime.combine(dt.date.today(), start)
                ).total_seconds()
            ) > 45 * 60:
                session_warnings.append(
                    _warning(
                        f"'{class_name}' starts at {start.strftime('%H:%M')} but "
                        f"sits on the {grid_time.strftime('%H:%M')} grid row.",
                        sheet=sheet_name,
                        coordinate=item["coordinate"],
                        date=session_date,
                        cell_text=item["text"],
                    )
                )

            roster = _Roster()
            attendance: list[dict[str, Any]] = []

            def add_entry(name: str, status: str | None, source: str, note: str | None = None):
                clean = re.sub(r"\s+", " ", name).strip()
                index = roster.add(clean)
                if index == len(attendance):
                    attendance.append(
                        {
                            "student_name": clean,
                            "status": status or ATTENDING,
                            "source": source,
                            "note": note,
                        }
                    )
                    return index, True
                return index, False

            day_status = ONLINE if anchor in online_days else None
            for entry in parsed["entries"]:
                index, created = add_entry(
                    entry["name"], entry["status"] or day_status, item["coordinate"],
                    entry.get("note"),
                )
                if not created and entry["status"]:
                    attendance[index]["status"] = entry["status"]

            for note in parsed["notes"]:
                session_warnings.append(
                    _warning(
                        (f"a second time, {note}, is written among the students of "
                         f"'{class_name}' — probably students who joined late; "
                         f"everyone is billed for {start:%H:%M}-{end:%H:%M}."
                         if _is_time_line(note) else
                         f"unrecognised line in '{class_name}' — {note}"),
                        sheet=sheet_name,
                        coordinate=item["coordinate"],
                        date=session_date,
                        cell_text=item["text"],
                    )
                )

            # Attach the status cells that sit inside this class's rows.
            for row, column, status_parsed, status_fill, status_text in status_cells:
                if not (item["row"] <= row <= item["end_row"]):
                    continue
                if column <= item["column"]:
                    continue
                reachable = [
                    other
                    for other in classes
                    if other["column"] < column and other["row"] <= row <= other["end_row"]
                ]
                # The sheet colours each class block and its status cells the
                # same, so colour decides the owner when it is available and
                # position decides it otherwise.
                coloured = [
                    other
                    for other in reachable
                    if status_fill is not None and other["fill"] == status_fill
                ]
                owner = max(
                    coloured or reachable,
                    key=lambda other: other["column"],
                    default=None,
                )
                if owner is not item:
                    continue
                attached.add((row, column))
                colour_disagrees = bool(
                    status_fill
                    and item["fill"]
                    and status_fill != item["fill"]
                    and not coloured
                )

                coordinate = f"{get_column_letter(column)}{row}"
                for entry in status_parsed["entries"]:
                    status = entry["status"]
                    if status is None:
                        session_warnings.append(
                            _warning(
                                f"'{entry['name']}' has no Online/Recording/"
                                "Cancelled label; treated as attending.",
                                sheet=sheet_name,
                                coordinate=coordinate,
                                date=session_date,
                                cell_text=status_text,
                            )
                        )
                        status = ATTENDING

                    index, warning_kind = roster.match(entry["name"])
                    if index is None:
                        add_entry(entry["name"], status, coordinate, entry.get("note"))
                        if colour_disagrees:
                            session_warnings.append(
                                _warning(
                                    f"'{entry['name']}' is marked {status} but is "
                                    f"not on the '{class_name}' roster, and this "
                                    "cell is a different colour from that class; "
                                    "it may belong to another class.",
                                    sheet=sheet_name,
                                    coordinate=coordinate,
                                    date=session_date,
                                    cell_text=status_text,
                                )
                            )
                        else:
                            session_warnings.append(
                                _warning(
                                    f"'{entry['name']}' is marked {status} but is "
                                    f"not on the '{class_name}' roster; the cell "
                                    "colour matches the class, so it was added "
                                    "to it.",
                                    sheet=sheet_name,
                                    coordinate=coordinate,
                                    date=session_date,
                                    cell_text=status_text,
                                )
                            )
                        continue

                    if warning_kind:
                        session_warnings.append(
                            _warning(
                                f"'{entry['name']}' matched roster name "
                                f"'{attendance[index]['student_name']}' "
                                f"({warning_kind} spelling).",
                                sheet=sheet_name,
                                coordinate=coordinate,
                                date=session_date,
                                cell_text=status_text,
                            )
                        )
                    previous = attendance[index]["status"]
                    if previous not in (ATTENDING, status):
                        session_warnings.append(
                            _warning(
                                f"'{attendance[index]['student_name']}' is marked "
                                f"both {previous} and {status}; kept {status}.",
                                sheet=sheet_name,
                                coordinate=coordinate,
                                date=session_date,
                                cell_text=status_text,
                            )
                        )
                    attendance[index]["status"] = status
                    if entry.get("note"):
                        attendance[index]["note"] = entry["note"]

                for note in status_parsed["notes"]:
                    session_warnings.append(
                        _warning(
                            f"unrecognised line — {note}",
                            sheet=sheet_name,
                            coordinate=coordinate,
                            date=session_date,
                            cell_text=status_text,
                        )
                    )

            if class_name == UNNAMED_CLASS and not attendance:
                warnings.append(
                    _warning(
                        "this cell holds only a time range — no class name and no "
                        "students — so it was not imported as a lesson.",
                        sheet=sheet_name,
                        coordinate=item["coordinate"],
                        date=session_date,
                        cell_text=item["text"],
                    )
                )
                continue

            if item["time_note"]:
                session_warnings.append(
                    _warning(
                        item["time_note"],
                        sheet=sheet_name,
                        coordinate=item["coordinate"],
                        date=session_date,
                        cell_text=item["text"],
                    )
                )

            if not attendance:
                session_warnings.append(
                    _warning(
                        f"'{class_name}' has no student names.",
                        sheet=sheet_name,
                        coordinate=item["coordinate"],
                        date=session_date,
                        cell_text=item["text"],
                    )
                )

            sessions.append(
                {
                    "worksheet": sheet_name,
                    "date": session_date,
                    "class_name": class_name,
                    "start_time": start,
                    "end_time": end,
                    "attendance": attendance,
                    "warnings": session_warnings,
                    "cell": f"{sheet_name}!{item['coordinate']}",
                    "coordinate": item["coordinate"],
                    "cell_text": item["text"],
                }
            )

        # An absence written as a note -- "지훈결석" in the row under the grid,
        # or a status cell no class claimed -- can't be tied to a student: the
        # note has the name in Korean, the roster in English. It is not guessed
        # at. It is put in front of whoever imports, because the student it
        # describes is otherwise billed as having come.
        for row, column, _, _, text in status_cells:
            if (row, column) not in attached and _ABSENCE_WORD.search(text):
                absence_notes.append((f"{get_column_letter(column)}{row}", text))
        for coordinate, text in absence_notes:
            warnings.append(
                _warning(
                    f"absence note not matched to a lesson — {' / '.join(_cell_lines(text))}. "
                    "If a student here missed a class, mark them Cancelled on it so "
                    "they aren't billed.",
                    sheet=sheet_name,
                    coordinate=coordinate,
                    date=session_date,
                    cell_text=text,
                )
            )

    sessions.sort(key=lambda item: (item["date"], item["start_time"], item["class_name"]))
    return sessions, warnings, [(block["year"], block["month"]) for block in blocks]


# --------------------------------------------------------------------------
# Cross-workbook name canonicalisation
# --------------------------------------------------------------------------


# Punctuation that can only be a leftover from typing several names into one
# cell -- a stray comma, a dangling slash. Never part of a person's name, and
# leaving it attached turns "Woojin" and "Woojin," into two different students.
_EDGE_PUNCTUATION = " -,;/·•"


def _display_clean(name: str) -> str:
    """Tidy a name for display without changing who it refers to."""
    return re.sub(r"\s+", " ", name.translate(INVISIBLE)).strip(_EDGE_PUNCTUATION)


def _bare(name: str) -> str:
    """The name with any bracketed suffix removed, case-folded."""
    text = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", _display_clean(name))
    return text.strip(" -").casefold()


def _suffix(name: str) -> str:
    found = re.findall(r"[\(\[]([^\)\]]*)[\)\]]", _display_clean(name))
    return found[0].strip() if found else ""


_REASON_TEXT = {
    "capitalisation": "differ only by capitalisation",
    "tag": "same name, tagged differently",
    "spelling": "similar spelling",
}

# Shown worst-first: a tag difference is the one most likely to be two real
# people sharing a name (confirmed by the academy -- a bracketed tag is not
# reliably an alias), so it's surfaced ahead of a probably-safe typo.
_REASON_ORDER = {"tag": 0, "spelling": 1, "capitalisation": 2}


def canonicalise_names(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Find spellings of the same student across a workbook -- and flag every
    one for a human decision. Nothing is merged automatically here.

    A workbook mixes three kinds of variation, and none of them is safe to
    assume is the same person on its own:

    * Pure case/spacing differences (``Bae Sujin`` vs ``han seoyoung``)
      are usually a typo, but "usually" isn't "always".
    * A bracketed tag (``Seo Yerin`` vs ``Seo Yerin(UWC D)``) is *not*
      assumed to be an alias on one person -- two students can share a name
      and be told apart only by a tag, so a tag difference is flagged, not
      silently stripped and merged.
    * A similar-but-not-identical spelling (``Bae Sujin`` / ``Han
      Seyoung``) is flagged the same way.

    Attendance entries get only cosmetic clean-up here (invisible
    characters, extra whitespace) -- nothing is renamed until a human
    approves a merge via ``apply_review_decisions`` in schedule_backfill.py.
    """
    roster_mates: dict[str, set[str]] = defaultdict(set)
    counts: Counter = Counter()

    for session in sessions:
        anchor = session["coordinate"]
        enrolled = {
            _display_clean(item["student_name"])
            for item in session["attendance"]
            if item["source"] == anchor
        }
        for key in enrolled:
            roster_mates[key] |= enrolled - {key}

    for session in sessions:
        for item in session["attendance"]:
            cleaned = _display_clean(item["student_name"])
            item["raw_name"] = item["student_name"]
            item["tag"] = _suffix(cleaned) or None
            item["student_name"] = cleaned
            counts[cleaned] += 1

    names = sorted(counts)
    reviews: list[dict[str, Any]] = []
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            if left.casefold() == right.casefold():
                reason, ratio = "capitalisation", 1.0
            else:
                bare_left, bare_right = _bare(left), _bare(right)
                if bare_left == bare_right:
                    reason, ratio = "tag", 1.0
                else:
                    ratio = difflib.SequenceMatcher(None, bare_left, bare_right).ratio()
                    reason = "spelling" if ratio >= MERGE_THRESHOLD else None
            if reason is None:
                continue

            detail = _REASON_TEXT[reason]
            if reason == "tag":
                detail += f" ('{_suffix(left) or 'no tag'}' vs '{_suffix(right) or 'no tag'}')"
            detail += (
                " -- they appear on the same roster together"
                if right in roster_mates[left]
                else " -- they never share a class"
            )

            reviews.append(
                {
                    "names": [left, right],
                    "similarity": round(ratio, 3),
                    "reason": reason,
                    "reason_text": detail,
                    "counts": [counts[left], counts[right]],
                }
            )

    reviews.sort(key=lambda item: (_REASON_ORDER[item["reason"]], -item["similarity"]))
    return {"reviews": reviews}


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def get_sheet_names(source: Any) -> list[str]:
    workbook = load_workbook(_as_stream(source), data_only=True, read_only=True)
    try:
        return list(workbook.sheetnames)
    finally:
        workbook.close()


def _name_unnamed(sessions: list[dict[str, Any]]) -> None:
    """Give each unnamed lesson a placeholder for its student, or its weekly slot.

    One placeholder for every unnamed lesson would make the import screen's
    single name merge them all into one class: several teachers' different
    groups, one bill. A one-to-one is named for its student, however its time
    moves around; a group for its weekday and start time. Placeholders sort
    the lessons for naming at import; typing the same name twice joins them.
    """
    for session in sessions:
        if session["class_name"] != UNNAMED_CLASS:
            continue
        students = sorted({a["student_name"] for a in session["attendance"]})
        if len(students) == 1:
            label = students[0]
        else:
            label = (f"{calendar.day_abbr[session['date'].weekday()]} "
                     f"{session['start_time']:%H:%M}")
        session["class_name"] = f"{UNNAMED_CLASS} · {label}"


def _summarise(
    sessions: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
    sheet_name: str,
    month: int | None,
    year: int,
) -> dict[str, Any]:
    # Counted as the names actually appear, not folded down by
    # normalise_name: that strips bracketed tags, which would report
    # "Seo Yerin" and "Seo Yerin(UWC D)" as one student when the rule is that
    # they are two people. Anything that really is one person typed twice is
    # raised as a review, and only merges once somebody says so.
    unique_students = {
        attendance["student_name"]
        for session in sessions
        for attendance in session["attendance"]
    }
    total_warnings = len(warnings) + sum(len(session["warnings"]) for session in sessions)
    return {
        "sheet_name": sheet_name,
        "month": month,
        "year": year,
        "sessions": sessions,
        "session_count": len(sessions),
        "unique_student_count": len(unique_students),
        "warnings": warnings,
        "warning_count": total_warnings,
        "sheet_summaries": [],
    }


def parse_schedule(source: Any, sheet_name: str, year: int) -> dict[str, Any]:
    """Parse a single worksheet into a preview dictionary."""
    try:
        month = infer_month(sheet_name)
    except ValueError:
        month = None  # it may still name its months above the day labels
    workbook = load_workbook(_as_stream(source), data_only=True)
    try:
        if sheet_name not in workbook.sheetnames:
            raise ValueError(f"The workbook has no worksheet named {sheet_name!r}.")
        sessions, warnings, _ = _parse_sheet(
            workbook[sheet_name], month, infer_year(sheet_name, int(year))
        )
    finally:
        workbook.close()
    report = canonicalise_names(sessions)
    _name_unnamed(sessions)
    preview = _summarise(sessions, warnings, sheet_name, month, int(year))
    preview["name_reviews"] = report["reviews"]
    return preview


def parse_workbook(source: Any, sheet_names: Iterable[str], year: int) -> dict[str, Any]:
    """Parse several worksheets and merge them into one preview dictionary."""
    year = int(year)
    workbook = load_workbook(_as_stream(source), data_only=True)
    all_sessions: list[dict[str, Any]] = []
    all_warnings: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []

    try:
        for sheet_name in sheet_names:
            if sheet_name not in workbook.sheetnames:
                all_warnings.append(
                    _warning("worksheet not found; skipped.", sheet=sheet_name)
                )
                continue
            try:
                month = infer_month(sheet_name)
            except ValueError:
                # No month in the name: a whole year kept on one sheet, which
                # names its months above the day labels, or a sheet that is
                # not a schedule at all. _parse_sheet tells the two apart.
                month = None

            sheet_year = infer_year(sheet_name, year)
            sessions, warnings, months = _parse_sheet(
                workbook[sheet_name], month, sheet_year
            )
            all_sessions.extend(sessions)
            all_warnings.extend(warnings)
            if month is None and not months:
                continue  # not a schedule; its warning says so

            students = {
                attendance["student_name"]
                for session in sessions
                for attendance in session["attendance"]
            }
            summaries.append(
                {
                    "Worksheet": sheet_name,
                    "Month": _months_label(months, month),
                    "Sessions": len(sessions),
                    "Students": len(students),
                    "Warnings": len(warnings)
                    + sum(len(session["warnings"]) for session in sessions),
                }
            )
    finally:
        workbook.close()

    all_sessions.sort(key=lambda item: (item["date"], item["start_time"], item["class_name"]))

    # Month sheets overlap at the edges (a July sheet often carries 1 August).
    seen: dict[tuple[dt.date, dt.time, str], str] = {}
    for session in all_sessions:
        key = (session["date"], session["start_time"], normalise_name(session["class_name"]))
        first_seen = seen.get(key)
        if first_seen is None:
            seen[key] = session["cell"]
            continue
        all_warnings.append(
            _warning(
                f"'{session['class_name']}' also appears at {first_seen}; the two "
                "worksheets overlap, so import only one of them.",
                sheet=session["worksheet"],
                coordinate=session["coordinate"],
                date=session["date"],
                cell_text=session["cell_text"],
            )
        )

    report = canonicalise_names(all_sessions)
    _name_unnamed(all_sessions)
    preview = _summarise(all_sessions, all_warnings, "All worksheets", None, year)
    preview["sheet_summaries"] = summaries
    preview["name_reviews"] = report["reviews"]
    return preview


# --------------------------------------------------------------------------
# Command line helper (handy for checking a workbook outside Streamlit)
# --------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    import argparse

    argument_parser = argparse.ArgumentParser(description=__doc__)
    argument_parser.add_argument("workbook")
    argument_parser.add_argument("--year", type=int, default=dt.date.today().year)
    argument_parser.add_argument("--sheet", default=None)
    argument_parser.add_argument("--warnings", action="store_true")
    options = argument_parser.parse_args()

    sheets = get_sheet_names(options.workbook)
    if options.sheet:
        result = parse_schedule(options.workbook, options.sheet, options.year)
    else:
        result = parse_workbook(options.workbook, sheets, options.year)

    print(f"sessions={result['session_count']} students={result['unique_student_count']} "
          f"warnings={result['warning_count']}")
    for session in result["sessions"]:
        counts: dict[str, int] = {}
        for attendance in session["attendance"]:
            counts[attendance["status"]] = counts.get(attendance["status"], 0) + 1
        print(
            f"{session['date']} {session['start_time']:%H:%M}-{session['end_time']:%H:%M} "
            f"| {session['class_name']} | {len(session['attendance'])} students "
            f"| {counts}"
        )
    if options.warnings:
        for warning in result["warnings"]:
            print("!", warning["text"])
        for session in result["sessions"]:
            for warning in session["warnings"]:
                print("!", warning["text"])
