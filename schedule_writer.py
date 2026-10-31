"""Writes one teacher's month of lessons as an Excel sheet in the teachers' own layout.

The sheet looks like the schedules teachers already keep, so the upload on
the Teachers screen can read it back with no changes:

* One worksheet, named after the month (for example "Nov"), so it can be
  copied into the teacher's yearly file next to the other months.
* Row 1 has a day label such as "3(TUE)" over two columns for each day.
  The class goes in the first column; the second is left for the teacher
  to write Online, Recording or Cancelled and the names.
* Column A is the half-hour time axis, repeated before every Monday. It
  runs from the month's first lesson to the end of its last.
* Each class is a coloured block down its time rows:

      G11 Chem HL B
      4pm-6pm
      Nam Jihoon
      Oh Minseok

  The time is written with am/pm on both ends, because the upload reads
  the written time, not the row the block sits in.
"""

from __future__ import annotations

import calendar
import datetime as dt
import io
import math
from collections import defaultdict
from typing import Any, Iterable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import schedule_parser

SLOT_MINUTES = 30
# The upload only takes a column for the time axis if it holds at least three times.
MIN_SLOTS = 3
# Light colours so black text stays readable. Each subject keeps one colour for the month.
PALETTE = ["FFF2CC", "E0F7FA", "E8F5E9", "FCE4EC", "EDE7F6",
           "FFF3E0", "E3F2FD", "F1F8E9", "FBE9E7", "E0F2F1"]
WEEKEND_HEADER = "EEEEEE"
DAY_START = dt.time(9, 0)
DAY_END = dt.time(21, 0)
STATUS_ORDER = ("Online", "Recording", "Cancelled")


def month_sheet_name(month: int) -> str:
    """Return the worksheet name for a month, e.g. "Nov"."""
    return calendar.month_abbr[month]


def time_label(start: dt.time, end: dt.time) -> str:
    """Write a time range like "4pm-5.30pm", with am or pm on both ends."""
    def part(moment: dt.time) -> str:
        """Write one time, e.g. "4pm" or "5.30pm"."""
        hour = moment.hour % 12 or 12
        minutes = f".{moment.minute:02d}" if moment.minute else ""
        return f"{hour}{minutes}{'am' if moment.hour < 12 else 'pm'}"
    return f"{part(start)}-{part(end)}"


def class_text(lesson: dict[str, Any]) -> str:
    """Return the text of a class block: subject, time range, then one student per line."""
    return "\n".join([lesson["subject"].strip(), time_label(lesson["start"], lesson["end"]),
                      *[name.strip() for name in lesson.get("students") or [] if name.strip()]])


def status_text(lesson: dict[str, Any]) -> str:
    """Return what goes in the column next to a class: "Online" and its students, and so on."""
    by_status: dict[str, list[str]] = defaultdict(list)
    for name, status in (lesson.get("statuses") or {}).items():
        if status in STATUS_ORDER:
            by_status[status].append(name)
    return "\n".join(line for status in STATUS_ORDER if by_status[status]
                     for line in (status, *by_status[status]))


def _minutes(moment: dt.time) -> int:
    """Return a time as minutes after midnight."""
    return moment.hour * 60 + moment.minute


def write_month(lessons: Iterable[dict[str, Any]], year: int, month: int) -> bytes:
    """Build the month's sheet and return it as .xlsx bytes.

    Each lesson is a dict with date, start, end, subject, students (a list of
    names) and optionally statuses ({name: "Online" | "Recording" | "Cancelled"}).
    Lessons outside the month are skipped. If two lessons on one day would land
    on the same half-hour row, the later one moves down a row; its written time
    still shows when it really is.
    """
    days_in_month = calendar.monthrange(year, month)[1]
    lessons = sorted(
        (lesson for lesson in lessons
         if lesson["date"].year == year and lesson["date"].month == month and lesson["start"] < lesson["end"]),
        key=lambda lesson: (lesson["date"], lesson["start"], lesson["end"], lesson["subject"]),
    )
    # The rows run from the month's first lesson to the end of its last, so a month of
    # evening classes doesn't open on a screen of empty mornings.
    first = min((_minutes(lesson["start"]) // SLOT_MINUTES * SLOT_MINUTES for lesson in lessons),
                default=_minutes(DAY_START))
    last = max((_minutes(lesson["end"]) for lesson in lessons), default=_minutes(DAY_END))

    # Work out each lesson's rows: one row per half hour from `first`. If a lesson
    # would overlap the previous block that day, push it down below that block.
    placed: list[tuple[dict[str, Any], int, int]] = []
    by_day: dict[dt.date, int] = {}
    for lesson in lessons:
        top = (_minutes(lesson["start"]) - first) // SLOT_MINUTES
        bottom = math.ceil((_minutes(lesson["end"]) - first) / SLOT_MINUTES) - 1
        top = max(top, by_day.get(lesson["date"], -1) + 1)
        bottom = max(bottom, top)
        by_day[lesson["date"]] = bottom
        placed.append((lesson, top, bottom))
    slots = max([MIN_SLOTS, (last - first) // SLOT_MINUTES] + [bottom + 1 for _, _, bottom in placed])

    # Columns: the time axis, then two columns per day, with the axis repeated before each Monday.
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = month_sheet_name(month)
    axis_columns, day_column = [1], {}
    column = 2
    for day in range(1, days_in_month + 1):
        if dt.date(year, month, day).weekday() == 0 and day != 1:
            axis_columns.append(column)
            column += 1
        day_column[day] = column
        column += 2

    small = Font(size=9, color="666666")
    for axis in axis_columns:
        sheet.column_dimensions[get_column_letter(axis)].width = 7
        for slot in range(slots):
            minutes = first + slot * SLOT_MINUTES
            cell = sheet.cell(row=2 + slot, column=axis, value=dt.time(minutes // 60 % 24, minutes % 60))
            cell.number_format = "hh:mm"
            cell.font = small
            cell.alignment = Alignment(vertical="top")

    thin = Side(style="thin", color="DDDDDD")
    for day, col in day_column.items():
        when = dt.date(year, month, day)
        sheet.column_dimensions[get_column_letter(col)].width = 26
        sheet.column_dimensions[get_column_letter(col + 1)].width = 16
        header = sheet.cell(row=1, column=col, value=f"{day}({calendar.day_abbr[when.weekday()].upper()})")
        header.font = Font(bold=True)
        header.alignment = Alignment(horizontal="center", vertical="center")
        if when.weekday() >= 5:
            for c in (col, col + 1):
                sheet.cell(row=1, column=c).fill = PatternFill("solid", fgColor=WEEKEND_HEADER)
        sheet.merge_cells(start_row=1, start_column=col, end_row=1, end_column=col + 1)
        for slot in range(slots):
            sheet.cell(row=2 + slot, column=col + 1).border = Border(right=thin)

    colours: dict[str, str] = {}
    for lesson, top, bottom in placed:
        subject = lesson["subject"].strip()
        colour = colours.setdefault(subject.casefold(), PALETTE[len(colours) % len(PALETTE)])
        fill = PatternFill("solid", fgColor=colour)
        col = day_column[lesson["date"].day]
        for offset, text in ((0, class_text(lesson)), (1, status_text(lesson))):
            cell = sheet.cell(row=2 + top, column=col + offset, value=text)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            for row in range(2 + top, 3 + bottom):
                sheet.cell(row=row, column=col + offset).fill = fill
            if bottom > top:
                sheet.merge_cells(start_row=2 + top, start_column=col + offset,
                                  end_row=2 + bottom, end_column=col + offset)

    sheet.row_dimensions[1].height = 22
    for slot in range(slots):
        sheet.row_dimensions[2 + slot].height = 30
    sheet.freeze_panes = "B2"

    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


def readback_problems(lessons: list, data: bytes, year: int, month: int) -> list[str]:
    """Check the sheet reads back exactly as planned, using the upload's own parser.

    Returns one line per problem. This catches names the upload would misread:
    a comma splits one name into two, a digit drops the name, and a name that
    starts with Online, Rec or Cancel is read as a status.
    """
    same = schedule_parser.normalise_name
    back = schedule_parser.parse_workbook(data, [month_sheet_name(month)], year)["sessions"]
    read = {(item["date"], item["start_time"], item["end_time"]): item for item in back}
    lines = []
    for lesson in lessons:
        label = f"{lesson['date']:%a %d %b} {lesson['subject']}"
        got = read.get((lesson["date"], lesson["start"], lesson["end"]))
        if got is None:
            lines.append(f"{label}: this lesson wouldn't be read back from the sheet.")
            continue
        if got["class_name"] != lesson["subject"]:
            lines.append(f"{label}: the subject would read back as “{got['class_name']}”.")
        planned = {same(name): name for name in lesson["students"]}
        returned = {same(entry["student_name"]): entry["student_name"] for entry in got["attendance"]}
        extra = [name for key, name in returned.items() if key not in planned]
        for key, name in planned.items():
            if key not in returned:
                lines.append(f"{label}: “{name}” would read back as "
                             + (", ".join(f"“{other}”" for other in extra) if extra else "nobody")
                             + " — a comma, a number, or Online / Rec / Cancel at the start changes how a "
                               "name is read.")
    return lines
