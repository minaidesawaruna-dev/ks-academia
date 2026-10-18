"""Write one teacher's month of lessons as a schedule workbook the parser reads back.

The layout is the teachers' own, as in Jason's workbook, so a teacher sees
nothing new and ``schedule_parser`` needs nothing new:

* One worksheet, named by the month (``Nov``), so it can be copied into a
  teacher's yearly spreadsheet beside the months already there.
* Row 1 holds a day label, ``3(TUE)``, over two columns for every day of the
  month: a class sits in the first column, and the second is left for the
  teacher to write ``Online`` / ``Recording`` / ``Cancelled`` with names.
* Column A holds the half-hour time axis, repeated before every Monday so it
  stays in view across a wide sheet.
* A class is a coloured block running down its time rows::

      G11 Chem HL B
      4pm-6pm
      Nam Jihoon
      Oh Minseok

  The time is written out with am/pm on both ends: it is the written time,
  not the row, that the parser goes by.
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

SLOT_MINUTES = 30
# Pale enough that black text reads on every one; a subject keeps its colour
# across the month, as teachers colour theirs by hand.
PALETTE = ["FFF2CC", "E0F7FA", "E8F5E9", "FCE4EC", "EDE7F6",
           "FFF3E0", "E3F2FD", "F1F8E9", "FBE9E7", "E0F2F1"]
WEEKEND_HEADER = "EEEEEE"
DAY_START = dt.time(9, 0)
DAY_END = dt.time(21, 0)
STATUS_ORDER = ("Online", "Recording", "Cancelled")


def month_sheet_name(month: int) -> str:
    """``Nov`` -- the name the teachers' own sheets use."""
    return calendar.month_abbr[month]


def time_label(start: dt.time, end: dt.time) -> str:
    """``4pm-5.30pm``: am/pm written on both ends, so nothing is left to guess."""
    def part(moment: dt.time) -> str:
        hour = moment.hour % 12 or 12
        minutes = f".{moment.minute:02d}" if moment.minute else ""
        return f"{hour}{minutes}{'am' if moment.hour < 12 else 'pm'}"
    return f"{part(start)}-{part(end)}"


def class_text(lesson: dict[str, Any]) -> str:
    return "\n".join([lesson["subject"].strip(), time_label(lesson["start"], lesson["end"]),
                      *[name.strip() for name in lesson.get("students") or [] if name.strip()]])


def status_text(lesson: dict[str, Any]) -> str:
    """``Online`` then its students, and so on: what a teacher writes in the next column."""
    by_status: dict[str, list[str]] = defaultdict(list)
    for name, status in (lesson.get("statuses") or {}).items():
        if status in STATUS_ORDER:
            by_status[status].append(name)
    return "\n".join(line for status in STATUS_ORDER if by_status[status]
                     for line in (status, *by_status[status]))


def _minutes(moment: dt.time) -> int:
    return moment.hour * 60 + moment.minute


def write_month(lessons: Iterable[dict[str, Any]], year: int, month: int) -> bytes:
    """The month as an .xlsx file.

    ``lessons`` are ``{"date", "start", "end", "subject", "students": [names],
    "statuses": {name: "Online" | "Recording" | "Cancelled"}}``, ``statuses``
    optional. Lessons outside the month are left out. Two lessons of one day
    that would share a half-hour row are stacked, the later one a row down:
    its written time still says when it is.
    """
    days_in_month = calendar.monthrange(year, month)[1]
    lessons = sorted(
        (lesson for lesson in lessons
         if lesson["date"].year == year and lesson["date"].month == month and lesson["start"] < lesson["end"]),
        key=lambda lesson: (lesson["date"], lesson["start"], lesson["end"], lesson["subject"]),
    )
    first = min([_minutes(DAY_START)] + [_minutes(lesson["start"]) // SLOT_MINUTES * SLOT_MINUTES
                                         for lesson in lessons])
    last = max([_minutes(DAY_END)] + [_minutes(lesson["end"]) for lesson in lessons])

    # Rows: one a half hour from `first`; a lesson covers the rows its time
    # touches, pushed down past the day's previous block if they would share one.
    placed: list[tuple[dict[str, Any], int, int]] = []
    by_day: dict[dt.date, int] = {}
    for lesson in lessons:
        top = (_minutes(lesson["start"]) - first) // SLOT_MINUTES
        bottom = math.ceil((_minutes(lesson["end"]) - first) / SLOT_MINUTES) - 1
        top = max(top, by_day.get(lesson["date"], -1) + 1)
        bottom = max(bottom, top)
        by_day[lesson["date"]] = bottom
        placed.append((lesson, top, bottom))
    slots = max([(last - first) // SLOT_MINUTES] + [bottom + 1 for _, _, bottom in placed])

    # Columns: the time axis, then two a day, the axis again before each Monday.
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
