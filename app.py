"""KS Academia: the admin app for schedules, invoices and payments.

Ten screens:

* Teachers: upload a teacher's Excel schedule (the main way lessons get
  into the app), price a month's subjects, and list or rename one subject.
* Students: who had a lesson in a month, and what it comes to.
* Schedule: the month grid, for one-off fixes by hand.
* Create/Edit schedule: plan a teacher's month, or edit one the app has,
  save it to the app and download it as their sheet.
* Invoices: bill students for a month's lessons.
* Payments: record what parents have paid, in full or in part.
* Credits: lessons invoiced and then missed, which come off the next invoice.
* Data: what was taught and invoiced, per teacher and month, plus past
  schedules kept for those figures.
* Reminders: invoices past their due date.
* Delete records: remove test records, with everything linked to them.

A subject is what a teacher teaches, with its price and colour. A class
(or lesson) is one dated sitting of a subject, with its own students,
times and conditions.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import calendar
import copy
import difflib
import datetime as dt
import io
import os
import re
import time
import zipfile
from pathlib import Path

# The academy is in Singapore but the host runs on UTC, so before 8am the server's
# "today" was still yesterday: overdue counts were a day short, and on the 1st the
# current month was still last month. Every date.today() here and in db.py uses
# the process clock, so it's set first. "SGT-8" needs no time zone files on the
# host, and Singapore has no daylight saving. (Windows has no tzset, and a
# Windows machine is already on local time.)
os.environ["TZ"] = "SGT-8"
if hasattr(time, "tzset"):
    time.tzset()

import altair as alt
import pandas as pd
import streamlit as st

import auth
import db
import schedule_backfill
import schedule_parser
import schedule_writer
import teaching
from schedule_parser import UNNAMED_CLASS
from schedule_grid import render_month_grid
from invoice_render import (
    format_dates,
    image_export_available,
    pdf_export_available,
    render_invoice_html,
    render_invoices_batch_html,
    render_invoices_pdf,
    render_invoices_png,
)

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
_APP_ICON = str(ASSETS_DIR / "ks_icon.png")

st.set_page_config(
    page_title="KS Academia",
    page_icon=_APP_ICON if (ASSETS_DIR / "ks_icon.png").exists() else "📘",
    layout="wide",
    # The sidebar only holds "signed in as" and Sign out, so it starts closed and
    # leaves the full width for the schedule grid.
    initial_sidebar_state="collapsed",
)

# Sign-in comes before the database is touched or any student's name is shown.
# The host's own private-app setting still served the app on an internal path,
# so the check has to be here.
_authenticator = auth.require_login()
auth.logout_button(_authenticator)

@st.cache_resource
def _ensure_database_ready(tables: tuple[str, ...]) -> bool:
    """Check the database tables once per process, not on every click.

    Streamlit reruns this file from the top on every click, so anything at
    the top level runs on every button press. Even the quick check costs a
    few database round trips each time.

    The cache key is the list of tables the code defines. If a new version
    is deployed without a restart, a new table changes the key, so the check
    runs again instead of the new code hitting a database without the table.
    """
    db.initialise_database()
    return True


_ensure_database_ready(tuple(sorted(db.Base.metadata.tables)))

LESSON_STATUSES = ["Scheduled", "Completed", "Cancelled"]

# A student has one condition per class, so it's one choice, not three ticks that
# could all be on at once.
IN_CLASS = "In class"
CONDITIONS = [IN_CLASS, "Online", "Recording", "Cancelled"]

# "A month" of a weekly class is four lessons on the same weekday.
WEEKS_PER_MONTH = 4

# DELETE_PHRASE is the word typed to confirm a delete on Delete records. The row limits say
# how many open invoices, students and paid invoices to draw at once: each row
# builds its widgets (and sometimes an invoice file) up front, so these keep the
# pages quick. The search box reaches the rest.
DELETE_PHRASE = "delete"
# How many of each kind Delete records' warning lists by name before it gives a count.
BEYOND_ROWS = 5
OPEN_INVOICE_ROWS = 25
STUDENT_ROWS = 25
PAID_ROWS = 40


def _rerun() -> None:
    """Start the page again from the top."""
    st.rerun()


def _md(text: str) -> str:
    """Escape "$" for markdown: two of them on one line are read as a formula."""
    return text.replace("$", r"\$")


# Each kind of word (who, which subject, when, how much) gets one colour wherever
# it appears, so a message can be read at a glance. Streamlit draws
# :color-badge[...] in any of its markdown: alerts, captions, toasts.
CHIP_COLOURS = {"student": "blue", "teacher": "violet", "subject": "orange",
                "when": "gray", "money": "green", "flag": "red"}
_CHIP_KEY = {"student": "Student", "teacher": "Teacher", "subject": "Subject",
             "when": "Date / time", "money": "Amount", "flag": "Needs attention"}
_MARKDOWN_SYNTAX = re.compile(r"([\\`*_{}\[\]<>()#+\-.!|$:~])")


def _chip(kind: str, text) -> str:
    """Return `text` as a badge in its kind's colour.

    Anything markdown or the badge would treat as syntax is escaped, so a
    name with a bracket, or two amounts on one line (otherwise read as a
    formula), shows exactly as written.
    """
    safe = _MARKDOWN_SYNTAX.sub(r"\\\1", str(text))
    return f":{CHIP_COLOURS[kind]}-badge[{safe}]"


def _student(name) -> str:
    """Return a name as a student badge."""
    return _chip("student", name)


def _teacher(name) -> str:
    """Return a name as a teacher badge."""
    return _chip("teacher", name)


def _subject(name) -> str:
    """Return a name as a subject badge."""
    return _chip("subject", name)


def _when(text) -> str:
    """Return a date or time as a badge."""
    return _chip("when", text)


def _money(amount) -> str:
    """Return an amount as a badge: a number is written $1,234.50, text as given."""
    return _chip("money", amount if isinstance(amount, str) else f"${float(amount):,.2f}")


def _flag(text) -> str:
    """Return something that needs doing, like a missing price, as a red badge.
    """
    return _chip("flag", text)


def _chips(kind: str, names, most: int = 6) -> str:
    """Return several badges of one kind: "A, B and C", or "A, B, C and 4 more" past `most`."""
    chips = [_chip(kind, name) for name in names]
    if len(chips) > most:
        return ", ".join(chips[:most]) + f" and {len(chips) - most} more"
    return ", ".join(chips[:-1]) + " and " + chips[-1] if len(chips) > 1 else "".join(chips)


def _chip_key() -> None:
    """Show what the colours mean, once, next to Sign out."""
    st.sidebar.markdown(" ".join(_chip(kind, label) for kind, label in _CHIP_KEY.items()))


# Table cells in the same colours as the badges: (text, background).
_CELL_COLOURS = {
    "student": ("rgb(0, 84, 163)", "rgba(28, 131, 255, 0.1)"),
    "teacher": ("rgb(88, 63, 132)", "rgba(154, 93, 255, 0.1)"),
    "subject": ("rgb(226, 102, 12)", "rgba(255, 164, 33, 0.1)"),
    "when": ("rgba(49, 51, 63, 0.75)", "rgba(49, 51, 63, 0.06)"),
    "money": ("rgb(21, 130, 55)", "rgba(33, 195, 84, 0.1)"),
    "flag": ("rgb(189, 64, 67)", "rgba(255, 43, 43, 0.1)"),
}
# What each table column holds, by its heading. A table can pass its own for a
# heading that means something else there.
_COLUMN_KINDS = {
    "Student": "student", "Who": "student",
    "Teacher": "teacher",
    "Subject": "subject", "Subjects": "subject",
    "Date": "when", "Dates": "when", "From": "when", "To": "when", "Issued": "when",
    "Month": "when", "On": "when", "Overdue since": "when",
    "Amount": "money", "Total (SGD)": "money", "Rate": "money", "Price": "money",
    "Unit price": "money", "Invoiced": "money", "Still to invoice": "money",
    "Per teaching hour": "money",
}


def _plain_number(value) -> str:
    """Return a number written the way a plain table shows it: 1.5, 65, 97.5."""
    return ("%f" % value).rstrip("0").rstrip(".")


def _coloured(rows, kinds: dict | None = None):
    """Return a table for st.dataframe with its names, dates and money in colour.
    """
    frame = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    styler = frame.style
    # A styled table would print 1.5 as 1.500000, so decimals keep their plain form.
    decimals = [column for column in frame.columns if pd.api.types.is_float_dtype(frame[column])]
    if decimals:
        styler = styler.format(_plain_number, subset=decimals, na_rep="")
    for column in frame.columns:
        kind = (kinds or {}).get(column, _COLUMN_KINDS.get(column))
        if kind:
            colour, background = _CELL_COLOURS[kind]
            styler = styler.set_properties(subset=[column], **{"color": colour, "background-color": background})
    return styler


def _flash(message: str, kind: str = "success") -> None:
    """Save a message to show after the next rerun.

    A message shown just before a rerun is lost, because the rerun redraws
    the page from scratch. Almost every action here ends with a rerun (save,
    add, delete, rename), so without this the admin clicks a button and sees
    nothing happen. The message is kept in session_state, and _show_flash
    shows it at the top of the page, where it's visible from any screen.
    """
    st.session_state["_flash"] = (kind, message)


_TOAST_ICONS = {"success": "✅", "warning": "⚠️", "info": "ℹ️", "error": "❌"}


def _show_flash() -> None:
    """Show the message saved by _flash, at the top of the page and as a toast."""
    entry = st.session_state.pop("_flash", None)
    if entry:
        kind, message = entry
        getattr(st, kind)(message)
        # The message above is at the top of the page, but the button is often far below
        # it, so a confirmation after a long import preview was out of sight. A toast
        # floats over the page wherever it's scrolled.
        st.toast(message, icon=_TOAST_ICONS.get(kind), duration="long")


def _hours(start: dt.time, end: dt.time) -> float:
    """Return the hours between two times, to 2 decimals and never below 0."""
    minutes = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
    return round(max(minutes, 0) / 60, 2)


_SCROLL_TO_ANCHOR_JS = """
  // Jump the page to a marker, and report where the page ended up.
  //
  // scrollIntoView and scrollTo both silently do nothing when called on the
  // parent document from inside one of these iframes -- only assigning
  // scrollTop moves anything. Which element is the real scroller also varies
  // with Streamlit version and window size, so every plausible one is tried
  // and the ones that cannot scroll are skipped.
  function ksScrollToAnchor(anchorId) {
    const doc = window.parent.document;
    const target = doc.getElementById(anchorId);
    if (!target) return -1;
    let landed = -1;
    for (const box of [
      doc.querySelector('[data-testid="stMain"]'),
      doc.querySelector('section.main'),
      doc.scrollingElement,
      doc.documentElement,
      doc.body,
    ]) {
      if (!box || box.scrollHeight <= box.clientHeight) continue;
      const top = target.getBoundingClientRect().top
                - box.getBoundingClientRect().top + box.scrollTop;
      box.scrollTop = Math.max(top, 0);
      if (landed < 0) landed = box.scrollTop;
    }
    return landed;
  }
"""


_SCROLL_TOP_JS = """
  const doc = window.parent.document;
  // Which element actually scrolls differs between Streamlit versions and
  // window sizes, so move every plausible one.
  const targets = [
    doc.querySelector('[data-testid="stMain"]'),
    doc.querySelector('section.main'),
    doc.scrollingElement,
    doc.documentElement,
    doc.body,
  ];
  for (const target of targets) {
    if (!target) continue;
    try { target.scrollTo({top: 0, behavior: 'smooth'}); } catch (e) {}
    if (target.scrollTop) target.scrollTop = 0;
  }
  try { window.parent.scrollTo({top: 0, behavior: 'smooth'}); } catch (e) {}
"""


def _floating_back_to_top() -> None:
    """Add a fixed "back to top" button in the corner of every screen.

    The script is added to the parent page, not the iframe it runs in, so
    the button can sit over the whole page (_scroll_to works the same way).
    It's called once per run; because the HTML is the same each time,
    Streamlit keeps the existing iframe instead of running the script again,
    so the button is never added twice. The check on the element id is an
    extra guard.
    """
    st.iframe(
        """<script>
        (function () {
          const doc = window.parent.document;
          if (doc.getElementById('ks-back-to-top')) return;
          const button = doc.createElement('button');
          button.id = 'ks-back-to-top';
          button.innerHTML = '&uarr; Top';
          button.style.cssText = `
            position: fixed; right: 22px; bottom: 22px; z-index: 9999;
            font-family: system-ui, -apple-system, sans-serif;
            font-size: 13px; font-weight: 600; letter-spacing: .01em;
            padding: 10px 18px; border-radius: 999px; border: none;
            background: #ff4b4b; color: #fff; cursor: pointer;
            box-shadow: 0 2px 8px rgba(0,0,0,.25);
          `;
          button.onmouseenter = function () { button.style.background = '#e03c3c'; };
          button.onmouseleave = function () { button.style.background = '#ff4b4b'; };
          button.onclick = function () {""" + _SCROLL_TOP_JS + """};
          doc.body.appendChild(button);
        })();
        </script>""",
        # Script only: there's nothing to show and it mustn't take up space. st.iframe
        # refuses a height of 0, and "content" makes an empty page 150px tall, so use the
        # smallest height allowed.
        height=1,
    )


def _offer_billing() -> None:
    """Offer a button straight from "schedule imported" to "bill that month".

    It sits right under the import message because billing is the next step.
    It sets the Invoices month pickers, so that screen opens on the month just
    imported instead of today's.
    """
    period = st.session_state.get("offer_billing")
    if not period:
        return
    year, month = period
    label = f"{calendar.month_name[month]} {year}"
    columns = st.columns([2, 5])
    if columns[0].button(
        f"Bill {label} →", type="primary", key="jump_to_billing",
        width="stretch",
    ):
        st.session_state.pop("offer_billing", None)
        st.session_state["invoice_month_year"] = year
        st.session_state["invoice_month_month"] = month
        st.session_state["goto_section"] = "Invoices"
        _rerun()
    if columns[1].button("Not now", key="dismiss_billing_offer"):
        st.session_state.pop("offer_billing", None)
        _rerun()


def _month_picker(key_prefix: str) -> tuple[int, int]:
    """Draw year and month pickers, starting on the current month. Used on several screens."""
    today = dt.date.today()
    columns = st.columns(2)
    year = columns[0].number_input(
        "Year",
        min_value=2020,
        max_value=today.year + 1,
        value=today.year,
        step=1,
        key=f"{key_prefix}_year",
    )
    month = columns[1].selectbox(
        "Month",
        options=list(range(1, 13)),
        format_func=lambda value: calendar.month_name[value],
        index=today.month - 1,
        key=f"{key_prefix}_month",
    )
    return int(year), int(month)


def _condition_of(row: dict) -> str:
    """Return a student's condition as one choice: Cancelled, Recording, Online or In class."""
    if row.get("is_cancelled"):
        return "Cancelled"
    if row.get("has_recording"):
        return "Recording"
    if row.get("is_online"):
        return "Online"
    return IN_CLASS


def _repeat_weekly(teacher_id, class_id, base, start, end, weeks, rows, note=""):
    """Create weekly copies of a class and say how it went.

    Both the add form and the class editor repeat by the month, so the loop
    is kept here once. Returns (created, skipped, last date).
    """
    created = skipped = 0
    last = base
    for step in range(1, weeks + 1):
        later = base + dt.timedelta(weeks=step)
        outcome = db.create_schedule_session(
            teacher_id=teacher_id,
            class_id=class_id,
            session_date=later,
            start_time=start,
            end_time=end,
            status="Completed" if later <= dt.date.today() else "Scheduled",
            note=note,
            attendance_rows=rows,
        )
        if outcome == "created":
            created += 1
            last = later
        else:
            skipped += 1
    return created, skipped, last


def _blank_row(student_id: int) -> dict:
    """Return a student on a class with nothing marked yet."""
    return {
        "student_id": student_id,
        "is_online": False,
        "has_recording": False,
        "is_cancelled": False,
        "is_paid": False,
        "note": "",
    }


def _flags_for(condition: str) -> dict:
    """Turn a condition choice back into the three database flags."""
    return {
        "is_online": condition == "Online",
        "has_recording": condition == "Recording",
        "is_cancelled": condition == "Cancelled",
    }


def _explain(outcome: str, clash: str = "") -> str:
    """Return a plain message for a save that failed."""
    if outcome == "teacher_conflict":
        return "This teacher is already busy then. " + (
            clash or "Pick a different time or day."
        )
    if outcome == "duplicate":
        return "A subject with that name already exists — pick it from the list."
    if outcome == "invalid":
        return "Check the name, rate and times: the end must be after the start."
    return f"Could not do that ({outcome})."


# ---------------------------------------------------------------------------
# Teachers
# ---------------------------------------------------------------------------


def _import_detail(preview: dict) -> None:
    """Show every subject and student the upload found, before anything is saved.

    Only built when asked for: a workbook can hold a couple of years of
    lessons, and building these tables on every rerun would slow the upload
    form down for something most imports never need.
    """
    sessions = preview.get("sessions") or []
    if not sessions:
        return

    # A class the sheet never named. Shown on its own, not left in the warnings list:
    # it imports under a placeholder, and only the person with the spreadsheet knows
    # what it should be called.
    unnamed = [item for item in sessions if item["class_name"].startswith(UNNAMED_CLASS)]
    if unnamed:
        st.warning(
            f"**{len(unnamed)} class(es) have no subject name in the sheet.** "
            "Name them below, or later on the Teachers screen."
        )
        for item in sorted(unnamed, key=lambda x: (x["date"], x["start_time"])):
            st.caption(
                f"\u2022 **{item['cell']}** \u2014 "
                + _when(f"{item['date']:%d %b %Y} {item['start_time']:%H:%M}\u2013{item['end_time']:%H:%M}")
                + " \u2014 "
                + (_chips("student", [a["student_name"] for a in item["attendance"]], most=99)
                   or "nobody listed")
            )

    if not st.checkbox(
        "Show every subject and student this will import", key="import_show_detail"
    ):
        return

    by_subject: dict[str, dict] = {}
    by_student: dict[str, dict] = {}
    for item in sessions:
        hours = _hours(item["start_time"], item["end_time"])
        subject = by_subject.setdefault(
            item["class_name"],
            {"Sessions": 0, "Hours": 0.0, "Students": set(),
             "First": item["date"], "Last": item["date"]},
        )
        subject["Sessions"] += 1
        subject["Hours"] += hours
        subject["First"] = min(subject["First"], item["date"])
        subject["Last"] = max(subject["Last"], item["date"])
        for attendance in item["attendance"]:
            name = attendance["student_name"]
            subject["Students"].add(name)
            student = by_student.setdefault(
                name, {"Sessions": 0, "Hours": 0.0, "Subjects": set(), "Conditions": set()}
            )
            student["Sessions"] += 1
            student["Hours"] += hours
            student["Subjects"].add(item["class_name"])
            if attendance["status"] and attendance["status"] != "Attending":
                student["Conditions"].add(attendance["status"])

    view = st.radio(
        "Look at it by", ["Subject", "Student"],
        horizontal=True, key="import_detail_view", label_visibility="collapsed",
    )

    if view == "Subject":
        st.caption(f"{len(by_subject)} subject(s) across {len(sessions)} class(es).")
        st.dataframe(
            _coloured([
                {
                    "Subject": name,
                    "Classes": data["Sessions"],
                    "Hours": round(data["Hours"], 2),
                    "Students": len(data["Students"]),
                    "From": data["First"],
                    "To": data["Last"],
                    "Who": ", ".join(sorted(data["Students"])),
                }
                for name, data in sorted(by_subject.items())
            ]),
            width="stretch",
            hide_index=True,
        )
    else:
        st.caption(f"{len(by_student)} student(s) found in the workbook.")
        st.dataframe(
            _coloured([
                {
                    "Student": name,
                    "Classes": data["Sessions"],
                    "Hours": round(data["Hours"], 2),
                    "Subjects": ", ".join(sorted(data["Subjects"])),
                    "Flagged": ", ".join(sorted(data["Conditions"])),
                }
                for name, data in sorted(by_student.items())
            ]),
            width="stretch",
            hide_index=True,
        )


def _warning_panel(preview: dict) -> None:
    """Show every warning, grouped by the cell it came from.

    A warning only helps if the person with the spreadsheet can find the
    cell, so each group starts with the cell reference and worksheet, then
    shows the cell's text. Several warnings about one cell share a group.
    """
    all_warnings = list(preview["warnings"]) + [
        warning for session in preview["sessions"] for warning in session["warnings"]
    ]
    if not all_warnings:
        return

    groups: dict[tuple[str, str | None], list[dict]] = {}
    for warning in all_warnings:
        groups.setdefault((warning["sheet"], warning["coordinate"]), []).append(warning)

    placed = sum(1 for _, coordinate in groups if coordinate)
    title = f"Warnings ({len(all_warnings)})"
    if placed:
        title += f" \u2014 {placed} cell(s) to look at"

    with st.expander(title):
        st.caption("Each names its Excel cell. Fix it there and upload again, or import as is.")
        for (sheet, coordinate), items in groups.items():
            with st.container(border=True):
                date = next((item["date"] for item in items if item["date"]), None)
                if coordinate:
                    where = f"\U0001f4cd Cell **{coordinate}** \u00b7 worksheet `{sheet}`"
                else:
                    where = f"\U0001f4c4 Worksheet `{sheet}`"
                if date:
                    where += " \u00b7 " + _when(f"{date:%a %d %b %Y}")
                st.markdown(where)

                text = next(
                    (item["cell_text"] for item in items if item["cell_text"]), None
                )
                if text:
                    st.code(text, language=None, wrap_lines=True)
                elif coordinate:
                    st.caption("That cell is empty.")

                for item in items:
                    st.markdown(f"\u26a0\ufe0f {item['message']}")


def _import_changes(preview, decisions, teacher_id, name_overrides, student_matches) -> None:
    """Show what saving will change on the calendar, month by month, before anything is written.

    Re-uploading a corrected workbook is routine; this lets the admin check
    it changes what they expect and nothing else. It works on a copy of the
    upload, because answering the name questions changes it.
    """
    planned = {**preview, "sessions": copy.deepcopy(preview["sessions"])}
    planned["sessions"] = schedule_backfill.apply_review_decisions(planned, decisions)
    plan = schedule_backfill.plan_import(
        planned, teacher_id, name_overrides=name_overrides,
        student_matches=student_matches, dates=planned.get("dates_shown"),
    )
    if not plan:
        return
    st.markdown("**What this changes**")
    st.dataframe(
        _coloured([
            {
                "Month": f"{calendar.month_abbr[month]} {year}",
                "New": counts["new"],
                "Changed": counts["changed"],
                "Removed": counts["removed"],
                "Unchanged": counts["unchanged"] + counts["held"],
                "Not added": counts["clash"] + counts["skipped"],
            }
            for (year, month), counts in sorted(plan.items())
        ]),
        width="stretch",
        hide_index=True,
    )
    totals = {key: sum(counts[key] for counts in plan.values())
              for key in ("removed", "removed_invoiced", "changed_invoiced", "held", "clash", "skipped")}
    notes = []
    if totals["held"]:
        notes.append(f"{totals['held']} class(es) whose date has passed will be marked held; "
                     "nothing else about them changes.")
    if totals["removed"]:
        notes.append("Removed: no longer in the workbook on a day it shows"
                     + (f"; {totals['removed_invoiced']} already invoiced, credited back on the "
                        "next invoice." if totals["removed_invoiced"] else "."))
    if totals["changed_invoiced"]:
        notes.append(f"{totals['changed_invoiced']} changed class(es) already invoiced: anyone "
                     "taken off or cancelled is credited, anyone added is billed next.")
    if totals["clash"] + totals["skipped"]:
        notes.append("Not added: the teacher already has a class at that time, or another "
                     "teacher's subject has that name.")
    if notes:
        st.caption(" ".join(notes))


def _import_upload_panel() -> None:
    """Upload a teacher's Excel schedule, check what was read, then save it."""
    # Retired teachers aren't offered: a retired teacher shouldn't get a schedule
    # until they're taken back on. They can be brought back from "Rename or retire".
    teachers = [item for item in db.get_all_teachers() if item["Active"]]
    # Bumped after each import, so the file box and the new teacher's name start empty
    # for the next workbook. They used to keep the one just saved, and a second click
    # on Parse read it again, ready to be imported under the next teacher typed in.
    round_ = st.session_state.get("import_round", 0)

    mode = st.radio(
        "Teacher",
        ["Existing teacher", "New teacher"],
        horizontal=True,
        key="import_teacher_mode",
        label_visibility="collapsed",
        index=1 if not teachers else 0,
    )
    if mode == "New teacher" or not teachers:
        teacher_name = st.text_input("Teacher name", key=f"import_new_teacher_name_{round_}").strip()
        teacher_id = None
    else:
        labels = {item["Name"]: item["ID"] for item in teachers}
        chosen = st.selectbox("Teacher", list(labels), key="import_existing_teacher")
        teacher_id = labels[chosen]
        teacher_name = chosen

    year = st.number_input(
        "Year",
        min_value=2020,
        max_value=dt.date.today().year + 1,
        value=dt.date.today().year,
        step=1,
        key="import_year",
    )
    upload = st.file_uploader(
        "Excel schedule (.xlsx) — one worksheet per month, same layout as always",
        type=["xlsx"],
        key=f"import_file_{round_}",
    )
    if upload is None:
        return

    file_bytes = upload.getvalue()
    try:
        sheets = schedule_parser.get_sheet_names(file_bytes)
    except Exception as error:  # a damaged or non-Excel file
        st.error(f"Could not read that workbook: {error}")
        return

    chosen_sheets = st.multiselect(
        "Worksheets to import", sheets, default=sheets, key="import_sheets"
    )
    if not chosen_sheets:
        st.info("Pick at least one worksheet.")
        return

    # What was actually read. A different file or worksheet choice can then never save
    # an old preview by mistake, and the widget keys below are unique to each upload,
    # so answers from a previous workbook don't show up on a new one.
    fingerprint = (upload.file_id, tuple(sorted(chosen_sheets)), int(year))

    if st.button("Parse workbook", key="import_parse"):
        with st.spinner("Reading the workbook…"):
            st.session_state["import_preview"] = schedule_parser.parse_workbook(
                file_bytes, chosen_sheets, int(year)
            )
        st.session_state["import_fingerprint"] = fingerprint
        st.session_state["import_generation"] = (
            st.session_state.get("import_generation", 0) + 1
        )
        st.session_state.pop("import_done", None)

    preview = st.session_state.get("import_preview")
    if not preview:
        done = st.session_state.get("import_done")
        if done:
            st.success(done)
        return
    if st.session_state.get("import_fingerprint") != fingerprint:
        st.warning(
            "The file, worksheets or year changed since this was parsed — "
            "click **Parse workbook** again before importing."
        )
        return
    generation = st.session_state.get("import_generation", 0)

    # A worksheet can hold a whole year, and importing every month in it would put
    # months billed before the teacher joined the app up for billing again. So the
    # months are chosen, starting with the ones the app already has for this teacher
    # and any after them.
    found = {
        f"{item['month'][0]}-{item['month'][1]:02d}": item
        for item in schedule_backfill.months_found(preview)
    }
    suggested = [
        f"{year_}-{month_:02d}"
        for year_, month_ in schedule_backfill.default_months(preview, teacher_id)
    ]

    def _month_option(key: str) -> str:
        """Return the label for one month in the month list."""
        item = found[key]
        label = f"{calendar.month_name[item['month'][1]]} {item['month'][0]} — {item['classes']} class(es)"
        return label if item["own"] else label + f", {item['days']} day(s) spilling from the month before"

    chosen_months = st.multiselect(
        "Months to import", list(found), default=suggested, format_func=_month_option,
        key=f"import_months_{generation}_{teacher_id}",
        help="Only these months are imported. On the days they show, the calendar is made "
             "to match the workbook: new classes added, changed ones updated, and classes "
             "no longer in it removed — credited back if already invoiced.",
    )
    if teacher_id and any(item["own"] and key < min(suggested or ["9999"]) for key, item in found.items()):
        first = found[suggested[0]]["month"] if suggested else None
        st.caption(
            (f"Months before {_when(f'{calendar.month_name[first[1]]} {first[0]}')} are not ticked"
             if first else "None is ticked: every month here comes before this teacher's months in the app")
            + " — billed outside the app, so importing one puts its classes up for billing again."
        )
    if not chosen_months:
        st.info("Pick at least one month.")
        return
    preview = schedule_backfill.restrict_to_months(
        preview, [tuple(map(int, key.split("-"))) for key in chosen_months]
    )
    # A subject renamed in the app still has its old name in the teacher's sheet.
    preview = schedule_backfill.use_subject_spellings(preview, teacher_id)
    preview["name_reviews"] = schedule_backfill.mark_names_on_file(preview["name_reviews"])
    # The questions below are numbered within the chosen months, so their answers
    # mustn't carry over to a different choice of months.
    scope = f"{generation}_{'_'.join(sorted(chosen_months))}"

    pending = len(preview["name_reviews"])
    headline = (
        f"{preview['session_count']} sessions · "
        f"{preview['unique_student_count']} student names · "
        f"{preview['warning_count']} warning(s)"
    )
    if pending:
        headline += f" · {pending} name(s) awaiting your decision below"
    st.info(headline)
    if preview.get("sheet_summaries"):
        st.dataframe(preview["sheet_summaries"], width="stretch", hide_index=True)

    _import_detail(preview)

    # Too big to leave in the closed list: a whole calendar that wasn't read.
    for warning in preview["warnings"]:
        if warning.get("kind") == "second_calendar":
            st.warning(f"Worksheet `{warning['sheet']}`: {warning['message']}")
    _warning_panel(preview)

    decisions: dict[int, str] = {}
    if preview["name_reviews"]:
        st.markdown(f"**Names that need a decision ({len(preview['name_reviews'])})**")
        st.caption("The likely answer is picked for you — check each one.")
        for index, review in enumerate(preview["name_reviews"]):
            first, second = review["names"]
            first_count, second_count = review["counts"]
            choice = st.radio(
                f"{_student(first)} ({first_count}×) vs {_student(second)} ({second_count}×) "
                f"— {review['reason_text']}",
                ["Keep separate", "Merge — same person"],
                index=1 if review.get("likely_same") else 0,
                horizontal=True,
                key=f"import_review_{scope}_{index}",
            )
            decisions[index] = "merge" if choice.startswith("Merge") else "separate"

    student_matches: dict[str, int] = {}
    match_candidates = schedule_backfill.suggest_student_matches(preview["sessions"], teacher_id)
    if match_candidates:
        st.markdown(f"**Possible existing students ({len(match_candidates)})**")
        st.caption("Close to a student already on file — the same child, or someone new?")
        for index, candidate in enumerate(match_candidates):
            reason_text = {
                "hangul": "the same name written in Korean",
                "short": "this teacher's student, name shortened",
                "tag": "same name, tagged differently",
                "grade": "same name, with a grade written in front",
                "sound": "same name, spelled another way",
                "letter": "one letter different",
            }.get(candidate["reason"], "similar spelling")
            choice = st.radio(
                f"{_student(candidate['parsed_name'])} — {reason_text} to existing "
                f"student {_student(candidate['existing_name'])}",
                ["New / different person", f"Same as {_student(candidate['existing_name'])}"],
                index=1 if candidate.get("likely_same") else 0,
                horizontal=True,
                key=f"import_match_{scope}_{index}",
            )
            if choice.startswith("Same as"):
                student_matches[candidate["parsed_name"].casefold()] = candidate["existing_id"]

    name_overrides: dict[str, str] = {}
    if teacher_name:
        # The same subject typed two ways in one workbook. Settled before the check
        # against other teachers below, because merging changes which names are left.
        # A class the sheet never named is named here, not left as a placeholder: only
        # the person with the spreadsheet knows its name, and editing Excel and uploading
        # again just to add one word is a lot of work.
        unnamed_groups: dict[str, list[dict]] = {}
        for item in preview["sessions"]:
            if item["class_name"].startswith(UNNAMED_CLASS):
                unnamed_groups.setdefault(item["class_name"], []).append(item)
        named_unnamed: dict[str, str] = {}
        if unnamed_groups:
            lesson_count = sum(len(items) for items in unnamed_groups.values())
            st.markdown(
                f"**Give the unnamed classes a subject ({len(unnamed_groups)} "
                f"class(es), {lesson_count} lesson(s))**"
            )
            st.caption("Give two groups the same name to make them one class; leave blank to name later.")
            for placeholder, items in sorted(unnamed_groups.items()):
                items.sort(key=lambda x: (x["date"], x["start_time"]))
                students = sorted({a["student_name"] for x in items for a in x["attendance"]})
                cells = ", ".join(x["cell"] for x in items[:4])
                span = f"{items[0]['date']:%d %b %Y} to {items[-1]['date']:%d %b %Y}"
                st.caption(
                    f"{_subject(placeholder)} — {len(items)} lesson(s), {_when(span)}, "
                    f"{cells}{' …' if len(items) > 4 else ''} — "
                    + (_chips("student", students, most=99) or "nobody listed")
                )
                typed = st.text_input(
                    f"Subject for {_subject(placeholder)}",
                    key=f"import_unnamed_{generation}_{placeholder}",
                    placeholder="e.g. Upper-Sec Science",
                ).strip()
                if typed:
                    named_unnamed[placeholder] = typed


        merged_into: dict[str, str] = {}
        merges = schedule_backfill.suggest_subject_merges(
            preview["sessions"], schedule_backfill.teacher_subjects(teacher_id))
        if merges:
            st.markdown(f"**Subjects typed two ways ({len(merges)})**")
            st.caption("Same subject typed differently — merging keeps its billing together.")
            for index, item in enumerate(merges):
                keep, drop = item["keep"], item["drop"]
                kept_n, drop_n = item["counts"]
                choice = st.radio(
                    f"{_subject(drop)} ({drop_n} class(es)) and {_subject(keep)} ({kept_n} class(es))",
                    [f"Merge into {_subject(keep)}", "Keep separate"],
                    key=f"import_merge_{scope}_{index}",
                    horizontal=True,
                )
                if choice.startswith("Merge"):
                    merged_into[drop] = keep

        # Clashes are checked against the names that will actually be created: a merged
        # subject under the name it merges into, and a newly named class under the name
        # just typed, not the old spelling.
        settled = dict(merged_into)
        settled.update(named_unnamed)
        effective = [
            {**item, "class_name": settled.get(item["class_name"], item["class_name"])}
            for item in preview["sessions"]
        ]
        renamed_to: dict[str, str] = {}
        renames = schedule_backfill.suggest_class_renames(effective, teacher_name)
        if renames:
            st.markdown("**Class names already used by another teacher**")
            st.caption("Each subject name must be unique across the academy.")
            for original, suggestion in renames.items():
                new_name = st.text_input(
                    f"Rename {_subject(original)}",
                    value=suggestion,
                    key=f"import_rename_{generation}_{original}",
                )
                renamed_to[original] = new_name.strip() or suggestion

        # backfill applies these in one pass, without following chains, so each original
        # spelling must point straight to its final name: a merged name follows its
        # target if that target is renamed.
        name_overrides.update(renamed_to)
        for original, chosen in settled.items():
            name_overrides[original] = renamed_to.get(chosen, chosen)

    _import_changes(preview, decisions, teacher_id, name_overrides, student_matches)

    if st.button("Commit import", type="primary", key="import_commit"):
        if not teacher_name:
            st.warning("Enter a teacher name first.")
            return
        commit_id = teacher_id
        if commit_id is None:
            if not db.create_teacher(teacher_name):
                st.warning("Could not create that teacher (name may already exist).")
                return
            commit_id = next(
                item["ID"]
                for item in db.get_all_teachers()
                if item["Name"].casefold() == teacher_name.casefold()
            )

        sessions = schedule_backfill.apply_review_decisions(preview, decisions)
        preview["sessions"] = sessions
        with st.spinner(f"Importing {len(sessions)} class(es) for {teacher_name}…"):
            result = schedule_backfill.backfill(
                preview, commit_id,
                name_overrides=name_overrides,
                student_matches=student_matches,
                dates=preview.get("dates_shown"),
            )
        if result["status"] == "imported":
            counts = result["created"]
            message = (
                f"Imported: {counts.get('sessions_created', 0)} new class(es), "
                f"{counts.get('sessions_updated', 0)} updated, "
                f"{counts.get('sessions_unchanged', 0)} already up to date, "
                f"{counts.get('students', 0)} new student(s), "
                f"{counts.get('classes', 0)} new subject(s)."
            )
            # "lessons_removed" also starts with "lessons_", but a lesson the workbook no longer
            # has was taken off the calendar on purpose. Counting it as skipped looked like a
            # failure and made the number too big.
            skipped = sum(
                value for key, value in counts.items()
                if (key.startswith("class_") or key.startswith("lessons_"))
                and key != "lessons_removed"
            )
            if skipped:
                message += (
                    f" {skipped} class(es)/subject(s) were skipped — usually a "
                    "duplicate time slot from the workbook's overlapping month "
                    "columns, or an unresolved class-name collision."
                )
            priced = counts.get("priced_by_rule", 0)
            if priced:
                message += (
                    f" {priced} subject(s) without a grade in the name were priced by "
                    "their students' grades, or $60/h — change any under Teacher detail."
                )
            removed = counts.get("lessons_removed", 0)
            if removed:
                message += (
                    f" {removed} class(es) no longer in the workbook were "
                    "removed from the calendar."
                )
                credited = counts.get("credits_raised", 0)
                if credited:
                    message += (
                        f" {credited} of them had already been invoiced and "
                        "were credited back."
                    )
            twice = result.get("listed_twice") or []
            if twice:
                message += (
                    f" {len(twice)} class(es) list one student twice — kept once, as first "
                    f"written; check {', '.join(twice[:5])}{' …' if len(twice) > 5 else ''}."
                )
            _flash(message)
            # Also shown where the preview was, which is where the admin who just clicked
            # is looking, until the next workbook is read.
            st.session_state["import_done"] = f"{_teacher(teacher_name)} — {message}"
            # The month just imported is almost always the next one to bill, so offer to go
            # straight there instead of making the admin pick it again.
            months = sorted(
                {(item["date"].year, item["date"].month) for item in sessions
                 if item.get("date")}
            )
            if months:
                st.session_state["offer_billing"] = months[-1]
            st.session_state.pop("import_preview", None)
            st.session_state["import_round"] = round_ + 1
            _rerun()
        else:
            st.warning(f"Nothing imported ({result['status']}).")


def _price_all_button(unpriced: list[dict], key: str) -> None:
    """Show one button that prices every listed subject by the academy's rule."""
    if st.button(
        f"Price all {len(unpriced)} by grade — ${schedule_parser.SENIOR_RATE:,.0f}/h Grade 11 "
        f"and above, ${schedule_parser.JUNIOR_RATE:,.0f}/h otherwise",
        type="primary", key=key, width="stretch",
    ):
        with st.spinner(f"Pricing {len(unpriced)} subject(s)…"):
            done, left = schedule_backfill.price_by_rule(unpriced)
        _flash(f"Priced {done} subject(s) by grade."
               + (f" {len(left)} still have no price." if left else ""))
        _rerun()


def _unnamed_subjects(key: str, year: int | None = None, month: int | None = None) -> None:
    """List subjects a workbook left without a name, with a box to name each.

    Parents' invoices call such a subject "(unnamed class) · Sun 13:30". They
    used to be asked about only once, at import, so a few got through and no
    screen showed them.
    """
    unnamed = db.get_unnamed_subjects(year, month)
    if not unnamed:
        return
    where = f" with classes in {calendar.month_name[month]} {year}" if month else ""
    st.warning(
        f"**{len(unnamed)} subject(s){where} have no name**, so invoices say “(unnamed class)”. "
        "Name them here; invoices already sent update too, amounts unchanged."
    )
    typed: dict[int, str] = {}
    for item in unnamed:
        slot = item["Class"].split("·", 1)[-1].strip() if "·" in item["Class"] else ""
        who = _chips("student", item["Students"], most=4)
        span = (_when(f"{item['First']:%d %b}–{item['Last']:%d %b}") if item["First"] else "no classes")
        typed[item["Class ID"]] = st.text_input(
            # An unnamed group is labelled by its weekly time, or by its one student.
            f"{_teacher(item['Teacher'])} · "
            f"{(_when(slot) if re.search(r'[0-9]:[0-9]', slot) else _student(slot)) if slot else 'no time'} · "
            f"{item['Lessons']} class(es), {span} · {who}",
            key=f"name_subject_{key}_{item['Class ID']}",
            placeholder="Subject name, e.g. G9 English",
        )
    chosen = {class_id: name for class_id, name in typed.items() if name.strip()}
    if st.button(
        f"Save {len(chosen)} name(s)" if chosen else "Save names",
        key=f"name_subjects_{key}", type="primary", disabled=not chosen,
    ):
        result = db.name_subjects(chosen)
        named = [name for status, name in result["outcome"].values() if status == "named"]
        refused = [class_id for class_id, (status, _) in result["outcome"].items() if status != "named"]
        for class_id in chosen:
            if class_id not in refused:
                st.session_state.pop(f"name_subject_{key}_{class_id}", None)
        _flash(
            f"Named {len(named)} subject(s): {_chips('subject', named, most=99)}."
            + (f" {result['relabelled']} line(s) already sent now show the name." if result["relabelled"] else "")
            + (f" {len(refused)} couldn't be named — that name is taken twice over." if refused else "")
        )
        _rerun()


def _unpriced_everywhere() -> None:
    """List every subject without a price, in any month, with a way to fix it.

    An upload prices every new subject now; this catches any left from
    before that, or a price removed by hand. A warning, not a closed list: a
    subject without a price is an invoice nobody can send.
    """
    unpriced = db.get_unpriced_subjects()
    if not unpriced:
        return
    lessons = sum(item["Sessions"] for item in unpriced)
    st.warning(
        f"**{len(unpriced)} subject(s) have no price** — {lessons} lesson(s) that can't be "
        "invoiced until they do."
    )
    _price_all_button(unpriced, "price_everything")
    with st.expander("Which, and the price each would get"):
        rule = schedule_backfill.price_rule(unpriced)
        st.dataframe(
            _coloured([
                {
                    "Teacher": item["Teacher"],
                    "Subject": item["Class"],
                    "From": dt.date(*item["Months"][0], 1).strftime("%b %Y"),
                    "Lessons": item["Sessions"],
                    "Price": f"${rule[item['Class ID']][0]:,.0f}/h",
                    "Because": rule[item["Class ID"]][1] or "the grade in its name",
                }
                for item in unpriced
            ]),
            width="stretch",
            hide_index=True,
        )


def _save_subject_rate(class_id: int, month_start: dt.date, rate: float) -> str:
    """Price one subject from a whole calendar month onward.

    The work is one transaction in db.set_class_rate_for_month.
    """
    return db.set_class_rate_for_month(class_id, month_start, rate)


def _bulk_rate_section(
    subjects: list[dict],
    year: int,
    month: int,
    base: str,
    *,
    heading: str = "Set a rate for several subjects at once",
    show_teacher: bool = False,
) -> None:
    """Tick several subjects and set one price for all of them.

    Subjects often share a price, and setting them one by one is slow. Used
    by the Teachers screen (one teacher's month) and the Invoices screen
    (whatever is unpriced in the month being billed, across teachers), so
    `subjects` is a plain list of {"Class ID", "Class", "Sessions"}.

    Select all sets each checkbox's session_state in on_change, before the
    checkboxes are drawn, which is the reliable way to make a select-all box
    work in Streamlit.
    """
    if not subjects:
        return
    month_start = dt.date(year, month, 1)
    subjects = sorted(subjects, key=lambda item: (item.get("Teacher", ""), item["Class"]))
    # The ticks can't be cleared after saving by resetting their session_state: on a
    # rerun the browser still reports each box as ticked, so the old value comes
    # back. Bumping this counter gives the checkboxes new keys, so Streamlit treats
    # them as new widgets, which start unticked.
    generation = st.session_state.get(f"bulk_rate_gen_{base}", 0)
    scope = f"{base}_{generation}"

    if heading:
        st.markdown(f"###### {heading}")

    def _select_all_subjects() -> None:
        """Tick or untick every subject to match Select all."""
        value = st.session_state.get(f"bulk_rate_all_{scope}", False)
        for item in subjects:
            st.session_state[f"bulk_rate_pick_{scope}_{item['Class ID']}"] = value

    st.checkbox("Select all", key=f"bulk_rate_all_{scope}", on_change=_select_all_subjects)

    # The price that applies in the month on screen, not the month before. Showing
    # last month's price would make a subject still read "no price set" right after
    # its price was set for this month. Fetched for every subject at once.
    month_rates = db.get_class_rates_for_date(
        [item["Class ID"] for item in subjects], month_start
    )
    # The price alone can't say whether it was set for this month or carried over
    # from an earlier one, and that's what matters when pricing a new month. A price
    # period always starts on the 1st, so the one covering this month either starts
    # on it (set here) or earlier, and then the label names the month it carries over
    # from.
    month_end = dt.date(year, month, calendar.monthrange(year, month)[1])
    priced_from: dict[int, dt.date] = {}
    # A subject created by an import is priced from its first lesson, which can be
    # after the 1st. That price is still this month's: checking only the 1st called a
    # G11 subject "no price set" when billing would charge $65 on every lesson.
    starts_within: dict[int, tuple[dt.date, float]] = {}
    all_rates = db.get_all_class_rates()
    for row in all_rates:
        starts = db.as_date(row["Effective From"])
        ends = db.as_date(row["Effective To"]) if row["Effective To"] else None
        if month_start < starts <= month_end:
            if row["Class ID"] not in starts_within or starts < starts_within[row["Class ID"]][0]:
                starts_within[row["Class ID"]] = (starts, row["Hourly Rate"])
            continue
        if starts > month_start or (ends and ends < month_start):
            continue
        if priced_from.get(row["Class ID"], dt.date.min) < starts:
            priced_from[row["Class ID"]] = starts

    rule = schedule_backfill.price_rule(subjects, rates=all_rates)

    selected_ids: list[int] = []
    for item in subjects:
        class_id = item["Class ID"]
        month_rate = month_rates.get(class_id)
        starts = priced_from.get(class_id)
        if not (month_rate and month_rate > db.UNSET_RATE) and class_id in starts_within:
            month_rate = starts_within[class_id][1]
            starts = month_start
        # The import placeholder isn't a price anyone chose, so it reads as unset here,
        # the same as in the Invoices warning.
        if not (month_rate and month_rate > db.UNSET_RATE):
            rate_text = _flag("no price")
        elif starts is None or starts == month_start:
            rate_text = _money(f"${month_rate:,.0f}/h")
        else:
            rate_text = _money(f"${month_rate:,.0f}/h") + f" since {calendar.month_abbr[starts.month]}" + (
                f" {starts.year}" if starts.year != year else "")
        # Not when an import already put the teacher in the subject's name.
        who = (f" with {_teacher(item['Teacher'])}" if show_teacher and item.get("Teacher")
               and not item["Class"].endswith(f"({item['Teacher']})") else "")
        # What the rule says it should cost, when that differs from its current price.
        # The screen knows the rule, so it should say so instead of leaving it to memory.
        standard, why, certain = rule[class_id]
        unset = not (month_rate and month_rate > db.UNSET_RATE)
        if certain and abs((month_rate or 0) - standard) > 0.005:
            suggestion = f", standard {_money(f'${standard:,.0f}')}" + (f" ({why})" if why else "")
        elif not certain and unset:
            suggestion = f", {_money(f'${standard:,.0f}')} by default ({why})"
        else:
            suggestion = ""
        checked = st.checkbox(
            f"{_subject(item['Class'])}{who} — {rate_text} · {item['Sessions']} classes{suggestion}",
            key=f"bulk_rate_pick_{scope}_{class_id}",
        )
        if checked:
            selected_ids.append(class_id)

    columns = st.columns([2, 1])
    # Keyed on `base`, not `scope`, so the price typed here stays after saving, which
    # helps when the same price is set for another month next.
    bulk_rate = columns[0].number_input(
        "New rate ($ per hour)",
        min_value=0.0, step=1.0, key=f"bulk_rate_value_{base}",
    )
    # Only where the rule would change something, and for everything listed unless
    # some are ticked, so pricing a month by the rule is one click, not thirty ticks.
    # A default only fills a subject with no price; it never replaces one someone chose.
    by_grade = {}
    for item in subjects:
        rate, _, certain = rule[item["Class ID"]]
        current = month_rates.get(item["Class ID"]) or 0
        if selected_ids and item["Class ID"] not in selected_ids:
            continue
        if (certain or current <= db.UNSET_RATE) and abs(current - rate) > 0.005:
            by_grade[item["Class ID"]] = rate
    if by_grade and st.button(
        f"Price {len(by_grade)} {'ticked ' if selected_ids else ''}subject(s) by grade "
        f"(G11+ ${schedule_parser.SENIOR_RATE:,.0f}, else ${schedule_parser.JUNIOR_RATE:,.0f})",
        key=f"bulk_rate_standard_{scope}",
        width="stretch",
    ):
        done = db.set_class_rates_for_months(
            (class_id, month_start, rate) for class_id, rate in by_grade.items()
        )
        for class_id in by_grade:
            st.session_state.pop(f"bulk_rate_pick_{scope}_{class_id}", None)
        st.session_state.pop(f"bulk_rate_all_{scope}", None)
        if done:
            st.session_state[f"bulk_rate_gen_{base}"] = generation + 1
            _flash(f"Priced {done} subject(s) by grade, from "
                   f"{_when(f'{calendar.month_name[month]} {year}')} onward.")
            _rerun()
    if columns[1].button(
        f"Apply to {len(selected_ids)} selected",
        type="primary",
        disabled=not selected_ids,
        key=f"bulk_rate_apply_{scope}",
        width="stretch",
    ):
        if bulk_rate <= 0:
            st.warning("Enter a rate above zero.")
        else:
            ok = fail = 0
            for class_id in selected_ids:
                outcome = _save_subject_rate(class_id, month_start, bulk_rate)
                if outcome in ("created", "updated"):
                    ok += 1
                else:
                    fail += 1
                # Not needed to clear the tick (the new counter does that); it just stops old
                # keys piling up in session_state.
                st.session_state.pop(f"bulk_rate_pick_{scope}_{class_id}", None)
            st.session_state.pop(f"bulk_rate_all_{scope}", None)
            if fail:
                st.warning(f"{fail} subject(s) could not be updated.")
            if ok:
                st.session_state[f"bulk_rate_gen_{base}"] = generation + 1
                # Shown at the top of the page, not here: once the last subject is priced this
                # section isn't drawn at all, so a message inside it would never appear.
                _flash(
                    f"Set {_money(f'${bulk_rate:,.2f}/h')} for {ok} subject(s), "
                    f"from {_when(f'{calendar.month_name[month]} {year}')} onward."
                )
                _rerun()


def _teacher_drilldown(teachers: list[dict]) -> None:
    """Pick a teacher and month: price the subjects, then list one subject's lessons.

    Active teachers only, like the Schedule and Data screens. A retired
    teacher is off every working screen until they're taken back on from
    "Rename or retire", which still lists everyone.
    """
    labels = {item["Name"]: item["ID"] for item in teachers if item["Active"]}
    if not labels:
        st.caption("No active teachers. Bring one back from the panel above.")
        return
    if st.session_state.get("teacher_drill_pick") not in labels:
        # Start on whoever teaches most this month, not whoever sorts first: "No classes
        # for ..." isn't a useful first screen after signing in.
        today = dt.date.today()
        busy = db.get_teacher_session_counts(today.year, today.month)
        st.session_state["teacher_drill_pick"] = max(
            labels, key=lambda name: busy.get(labels[name], 0)
        )
    chosen = st.selectbox("Teacher", list(labels), key="teacher_drill_pick")
    teacher_id = labels[chosen]

    year, month = _month_picker("teacher_drill")
    lessons = db.get_month_schedule(teacher_id, year, month)
    if not lessons:
        st.caption(f"No classes for {_teacher(chosen)} in {_when(f'{calendar.month_name[month]} {year}')}.")
        return

    by_class: dict[int, list[dict]] = {}
    for lesson in lessons:
        by_class.setdefault(lesson["Class ID"], []).append(lesson)

    _bulk_rate_section(
        [
            {"Class ID": class_id, "Class": items[0]["Class"], "Sessions": len(items)}
            for class_id, items in by_class.items()
        ],
        year, month, base=f"{teacher_id}_{year}_{month}",
    )
    st.divider()

    subject_labels = {
        f"{items[0]['Class']} ({len(items)} session(s))": class_id
        for class_id, items in sorted(by_class.items(), key=lambda kv: kv[1][0]["Class"])
    }
    # Pricing is all in the section above, which already lists every subject with
    # its price and where it came from. This part used to repeat it, badly, showing
    # the $1 import placeholder as if someone had chosen it. What's left here is the
    # one thing that section can't do: list one subject's lessons. And rename it.
    st.markdown("###### One subject: its classes, or rename it")
    # After a rename the list shows the new name, so the pick moves to it before the
    # list is drawn (a widget's value can't be changed once it's on the page).
    if "teacher_drill_subject_next" in st.session_state:
        st.session_state["teacher_drill_subject"] = st.session_state.pop("teacher_drill_subject_next")
    subject_choice = st.selectbox("Subject", list(subject_labels), key="teacher_drill_subject")
    class_id = subject_labels[subject_choice]
    class_lessons = sorted(by_class[class_id], key=lambda item: (item["Date"], item["Start"]))
    class_name = class_lessons[0]["Class"]

    # For a typo in the name. Classes, prices and students stay; sent invoices show the
    # new name; the teacher's sheet can keep the old one, since uploads still match it.
    rename = st.columns([4, 1], vertical_alignment="bottom")
    new_name = rename[0].text_input("Rename to", value=class_name, key=f"subject_rename_{class_id}")
    if rename[1].button("Rename", key=f"subject_rename_go_{class_id}", width="stretch",
                        disabled=" ".join(new_name.split()) in ("", class_name)):
        done = db.rename_subject(class_id, new_name)
        if done["result"] == "renamed":
            st.session_state["teacher_drill_subject_next"] = f"{done['name']} ({len(class_lessons)} session(s))"
            _flash(f"Renamed {_subject(class_name)} to {_subject(done['name'])}. Sent invoices show the new "
                   "name; uploads with the old one still match.")
            _rerun()
        elif done["result"] == "duplicate":
            st.warning("Another subject already has that name.")
        else:
            st.warning("Enter a subject name.")

    st.markdown(f"###### {_subject(class_name)} — {_when(f'{calendar.month_name[month]} {year}')}")
    # One query for the month's rosters, not two per lesson listed. The Schedule
    # screen reads them the same way; a busy subject here used to be thirty round
    # trips to the database.
    rosters = db.get_month_attendance(teacher_id, year, month)
    for lesson in class_lessons:
        tagged = []
        for row in rosters.get(lesson["ID"], []):
            condition = _condition_of(row)
            tagged.append(
                _student(row["student_name"])
                if condition == IN_CLASS
                else f"{_student(row['student_name'])} ({condition})"
            )
        slot = f"{lesson['Date']:%d %b} · {lesson['Start']:%H:%M}–{lesson['End']:%H:%M}"
        st.caption(
            f"{_when(slot)} ({_hours(lesson['Start'], lesson['End']):g}h) — "
            f"{', '.join(tagged) or 'no students'}"
        )


def teachers_tab() -> None:

    """Draw the Teachers screen: upload, manage teachers, then prices and subjects."""
    teachers = db.get_all_teachers()

    with st.expander("Upload an Excel schedule", expanded=not teachers):
        _import_upload_panel()

    # Imports add teachers by themselves, so adding, renaming and retiring one is
    # occasional and kept out of the way.
    with st.expander(f"Manage teachers ({len(teachers)})" if teachers else "Add a teacher",
                     expanded=not teachers):
        with st.form("add_teacher", clear_on_submit=True, border=False):
            columns = st.columns([3, 1])
            name = columns[0].text_input("New teacher", key="teacher_new",
                                         placeholder="Name", label_visibility="collapsed")
            if columns[1].form_submit_button("Add teacher", width="stretch"):
                if db.create_teacher(name):
                    _flash(f"Added {_teacher(name.strip())}.")
                    _rerun()
                else:
                    st.warning("Enter a name that is not already on the list.")
        if teachers:
            per_teacher: dict = {}
            for item in db.get_all_classes():
                per_teacher[item["Teacher"]] = per_teacher.get(item["Teacher"], 0) + 1
            st.dataframe(
                _coloured([
                    {
                        "Teacher": item["Name"],
                        "Subjects": per_teacher.get(item["Name"], 0),
                        "Active": item["Active"],
                    }
                    for item in teachers
                ], {"Subjects": None}),
                width="stretch",
                hide_index=True,
                height=250,
            )
            st.markdown("###### Rename or retire")
            columns = st.columns([2, 2, 1, 1])
            chosen = columns[0].selectbox(
                "Teacher", [item["Name"] for item in teachers], key="teacher_pick",
                label_visibility="collapsed",
            )
            record = next(item for item in teachers if item["Name"] == chosen)
            new_name = columns[1].text_input("New name", value=record["Name"], key="teacher_rename",
                                             label_visibility="collapsed")
            if columns[2].button("Rename", key="teacher_save", width="stretch"):
                if db.update_teacher_name(record["ID"], new_name):
                    _flash("Renamed.")
                    _rerun()
                else:
                    st.warning("That name is not available.")
            label = "Retire" if record["Active"] else "Reactivate"
            if columns[3].button(label, key="teacher_toggle", width="stretch"):
                db.update_teacher_status(record["ID"], not record["Active"])
                _rerun()

    if not teachers:
        st.info("No teachers yet. Upload a schedule, or add one above.")
        return

    _unpriced_everywhere()
    _unnamed_subjects("teachers")
    st.markdown("#### Teacher detail")
    _teacher_drilldown(teachers)


# ---------------------------------------------------------------------------
# Students
# ---------------------------------------------------------------------------


def _two_children(dates: list[dt.date]) -> None:
    """Say the two records shared a lesson, so they're two children."""
    st.warning(
        "They sat the same class on "
        + ", ".join(_when(f"{day:%d %b %Y}") for day in dates[:3])
        + " — one child can't be in a class twice, so these are two children."
    )


def _merge_students_section(all_students: list[dict]) -> None:
    """Merge two records of one child, spelled two ways by different teachers, into one.

    Only shown when asked for (the tick box on the Students screen), because
    finding pairs compares every student's name, and this screen is opened
    far more often than anyone merges.
    """
    st.caption(
        "Merging moves classes, invoices and credits onto one record. Sent invoices keep "
        "their numbers and amounts."
    )
    names = {item["ID"]: item["Name"] for item in all_students}

    def merge(keep: int, drop: int) -> None:
        """Merge `drop` into `keep` and say what moved, or why it couldn't."""
        result = db.merge_students(keep, drop)
        if result["status"] == "merged":
            for key in ("merge_keep", "merge_drop"):
                st.session_state.pop(key, None)
            _flash(
                f"Merged {_student(result['dropped'])} into {_student(result['kept'])}: {result['lessons']} class(es), "
                f"{result['invoices']} invoice(s) sent and {result['credits']} credit(s) moved."
                + (f" “{result['dropped']}” in a workbook now means {_student(result['kept'])}."
                   if result["spellings"] else "")
            )
            _rerun()
        elif result["status"] == "share_a_lesson":
            _two_children(result["dates"])
        else:
            st.warning("One of them is no longer on file — refresh the page.")

    pairs = db.find_duplicate_students()
    if pairs:
        st.markdown(f"###### Possibly one child ({len(pairs)})")
    else:
        st.caption("Nobody looks like the same child on file twice.")

    def about(index: int, pair: dict) -> str:
        """Return one side of a pair: its lessons, teachers and grade."""
        grade = pair["grades"][index]
        return (f"{pair['lessons'][index]} class(es)"
                + (f", {_chips('teacher', pair['teachers'][index], most=99)}" if pair["teachers"][index] else "")
                + (f", G{grade}" if grade else ""))

    for pair in pairs[:30]:
        (first, second), (first_lessons, second_lessons) = pair["ids"], pair["lessons"]
        keep, drop = (first, second) if first_lessons >= second_lessons else (second, first)
        why = ("exactly the same name" if pair["names"][0].casefold() == pair["names"][1].casefold()
               else "same name, written another way" if pair["reason"] == "sound"
               else "a letter or two apart")
        columns = st.columns([6, 2.2, 1.8])
        columns[0].markdown(
            f"{_student(pair['names'][0])} ({about(0, pair)}) · {_student(pair['names'][1])} "
            f"({about(1, pair)}) — {why}"
        )
        if columns[1].button(f"Merge into {names.get(keep, '')}", key=f"dupe_merge_{first}_{second}"):
            merge(keep, drop)
        if columns[2].button("Different children", key=f"dupe_apart_{first}_{second}"):
            db.mark_students_distinct(first, second)
            _flash(f"{_student(pair['names'][0])} and {_student(pair['names'][1])} kept apart; "
                   "they won't be suggested again.")
            _rerun()

    st.markdown("###### Merge any two")
    options = sorted(names, key=lambda student_id: names[student_id].casefold())
    columns = st.columns(2)
    keep = columns[0].selectbox("Keep", options, index=None, format_func=names.get,
                                placeholder="The record to keep", key="merge_keep")
    drop = columns[1].selectbox("Fold into it", [i for i in options if i != keep], index=None,
                                format_func=names.get, placeholder="The other record", key="merge_drop")
    if keep and drop:
        preview = db.merge_students(keep, drop, dry_run=True)
        if preview["status"] == "share_a_lesson":
            _two_children(preview["dates"])
        elif preview["status"] == "would_merge":
            st.caption(
                f"{_student(names[drop])}'s {preview['lessons']} class(es), {preview['invoices']} invoice(s) "
                f"sent and {preview['credits']} credit(s) move to {_student(names[keep])}, and "
                f"{_student(names[drop])} is removed."
            )
            if st.button(f"Merge {names[drop]} into {names[keep]}", type="primary", key="merge_go"):
                merge(keep, drop)


def students_tab() -> None:

    """Draw the Students screen: missing schedules, then the students and their month."""
    year, month = _month_picker("students_month")

    # Only the teachers missing: a box for every teacher who had uploaded was two
    # dozen green boxes above the students on every visit.
    statuses = db.get_import_status(year, month)
    missing = [item["Teacher"] for item in statuses if not item["Imported"]]
    if missing:
        st.warning(f"⏳ No {_when(calendar.month_name[month])} schedule yet from "
                   f"{_chips('teacher', missing, most=10)}.")
    elif statuses:
        st.caption(f"✅ Every teacher's {_when(calendar.month_name[month])} schedule is in.")

    all_students = db.get_all_students()
    search = st.text_input(
        "Search", placeholder="Search by name", key="student_search", label_visibility="collapsed"
    )
    columns = st.columns([1.3, 1.3, 1])
    show_all = columns[0].checkbox("All students, not just this month", key="students_show_all")
    find_twins = columns[1].checkbox("Same child on file twice?", key="students_dupes_open")
    with columns[2].popover("Add a student", width="stretch"):
        with st.form("add_student", clear_on_submit=True, border=False):
            name = st.text_input("Student name", key="student_new_name")
            parent = st.text_input("Parent name", placeholder="optional", key="student_new_parent")
            phone = st.text_input("Contact number", placeholder="optional", key="student_new_phone")
            if st.form_submit_button("Add student", type="primary"):
                # A parent is only saved with both a name and a number, so half of one is pointed
                # out instead of quietly dropped.
                outcome = db.create_student(name[:120], parent[:120], phone[:40])
                if outcome == "created":
                    _flash(f"Added {_student(name.strip())}.")
                    _rerun()
                elif outcome == "duplicate":
                    st.warning("That student is already on the list.")
                elif outcome == "parent_unavailable":
                    st.warning("A parent needs both a name and a number — or leave both blank.")
                else:
                    st.warning("Enter a name first.")
    if not all_students:
        st.info("No students yet.")
        return
    if find_twins:
        _merge_students_section(all_students)

    student_notes = {item["ID"]: item["Note"] for item in all_students if item["Note"]}
    month_ids = {item["ID"] for item in db.get_students_in_month(year, month)}
    pool = all_students if show_all else [
        item for item in all_students if item["ID"] in month_ids
    ]

    shown = [
        item
        for item in pool
        if not search.strip() or search.strip().lower() in item["Name"].lower()
    ]

    # One query for everyone shown, not one per student opened.
    breakdowns = db.get_all_student_month_breakdowns(year, month)

    # A hard limit, because Streamlit builds an expander's contents whether it's open
    # or not: each row is a table, three inputs, a note box and buttons, so the whole
    # academy is thousands of widgets and several seconds before anything appears.
    # Searching is the quick way to find someone.
    page = shown[:STUDENT_ROWS]
    # Loaded for the whole page in one go, because Streamlit builds every expander
    # whether it's open or not: asking per row made this screen about a hundred
    # round trips to the database.
    page_ids = [item["ID"] for item in page]
    credit_waiting = db.get_student_credit_totals(page_ids)
    if len(shown) > len(page):
        st.caption(f"Showing {len(page)} of {len(shown)} — search to find anyone else.")
    elif not shown:
        st.caption("Nobody matches." if search.strip()
                   else f"Nobody had a class in {_when(calendar.month_name[month])}.")

    for item in page:
        marker = " 📝" if student_notes.get(item["ID"]) else ""
        parent = f" · {item['Parent']}" if item["Parent"] else ""
        owes = breakdowns.get(item["ID"], {}).get("total") or 0
        with st.expander(f"{_student(item['Name'])}{parent}{marker}"
                         + (f" — {_money(f'${owes:,.0f}')}" if owes else "")):
            breakdown = breakdowns.get(item["ID"], {"lines": [], "total": 0.0})
            if breakdown["lines"]:
                # A subject still on the import placeholder has no real price; showing its $1/h
                # sums as money made a month of lessons read "$10.00".
                priced = [line for line in breakdown["lines"] if line["Rate"] > db.UNSET_RATE]
                unpriced = len(breakdown["lines"]) - len(priced)
                st.dataframe(
                    _coloured([
                        {
                            "Subject": line["Subject"],
                            "Teacher": line["Teacher"],
                            "Sessions": line["Sessions"],
                            "Hours": line["Hours"],
                            "Rate": (f"${line['Rate']:,.2f}/h" if line["Rate"] > db.UNSET_RATE
                                     else "no price yet"),
                            "Amount": (f"${line['Amount']:,.2f}" if line["Rate"] > db.UNSET_RATE
                                       else "—"),
                        }
                        for line in breakdown["lines"]
                    ]),
                    width="stretch",
                    hide_index=True,
                )
                owed = credit_waiting.get(item["ID"], 0.0)
                figures = st.columns(2) if owed else [st]
                figures[0].metric(
                    f"Total for {calendar.month_name[month]} {year}",
                    f"${sum(line['Amount'] for line in priced):,.2f}"
                    + (" + unpriced" if unpriced else ""),
                    help=(f"Leaves out {unpriced} subject(s) with no price yet — set "
                          "one on the Teachers screen." if unpriced else None),
                )
                if owed:
                    figures[1].metric(
                        "Credit waiting", f"-${owed:,.2f}",
                        help="Cancelled classes already billed; comes off "
                             "their next invoice automatically.",
                    )
            else:
                st.caption(f"No classes in {_when(f'{calendar.month_name[month]} {year}')}.")

            st.markdown("###### Details")
            columns = st.columns([2, 2, 2])
            new_name = columns[0].text_input(
                "Name", value=item["Name"], key=f"sn{item['ID']}"
            )
            new_parent = columns[1].text_input(
                "Parent", value=item["Parent"] or "", key=f"sp{item['ID']}"
            )
            new_phone = columns[2].text_input(
                "Contact", value=item["Phone"] or "", key=f"sc{item['ID']}"
            )
            note = st.text_area(
                "Notes",
                value=student_notes.get(item["ID"], ""),
                placeholder="Anything worth remembering — allergies, billing "
                "arrangements, a parent's request.",
                key=f"sx{item['ID']}",
                height=80,
            )
            if st.button("Save", key=f"ss{item['ID']}", type="primary"):
                outcome = db.update_student(
                    item["ID"], new_name[:120], new_parent[:120], new_phone[:40]
                )
                if outcome == "updated":
                    db.set_student_note(item["ID"], note[:1000])
                    _flash("Saved.")
                    _rerun()
                elif outcome == "parent_unavailable":
                    st.warning(
                        "A parent needs both a name and a contact number — "
                        "fill in the other one, or clear both."
                    )
                elif outcome == "duplicate":
                    st.warning("Another student already has that name.")
                else:
                    st.warning("Enter a name first.")


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def _anchor(name: str) -> None:
    """Add an invisible marker the scroll helpers can jump to."""
    st.markdown(f"<div id='{name}'></div>", unsafe_allow_html=True)


def _scroll_to(name: str, token: object = "") -> None:
    """Scroll the page to a marker.

    The script runs in an iframe from the same site, so it can reach the
    parent page, which is the part that scrolls.
    """
    st.iframe(
        f"""<!-- {token} -->
        <script>""" + _SCROLL_TO_ANCHOR_JS + f"""
        // Identical HTML is reused, iframe and all, so the script would never
        // run a second time; the token above keeps each call distinct.
        // Streamlit is still laying the page out when this first runs, so the
        // anchor moves under us: jump a few times over the next second and
        // stop once the position stops changing.
        (function () {{
          let previous = -1;
          let tries = 0;
          const settle = setInterval(function () {{
            const landed = ksScrollToAnchor('{name}');
            if (landed >= 0) {{
              if (landed === previous) {{ clearInterval(settle); return; }}
              previous = landed;
            }}
            if (++tries > 8) clearInterval(settle);
          }}, 150);
        }})();
        </script>""",
        # Script only: there's nothing to show and it mustn't take up space. st.iframe
        # refuses a height of 0, and "content" makes an empty page 150px tall, so use the
        # smallest height allowed.
        height=1,
    )


def _back_to_grid_button() -> None:
    """Show a plain HTML button back to the grid, so going back needs no rerun."""
    st.iframe(
        """<style>
          .top-link { font-family: system-ui, -apple-system, sans-serif;
            font-size: 14px; font-weight: 600; letter-spacing: .01em;
            padding: 10px 18px; border-radius: 8px; border: none;
            background: #ff4b4b; color: #fff; cursor: pointer;
            box-shadow: 0 1px 3px rgba(0,0,0,.2); }
          .top-link:hover { background: #e03c3c; }
          .top-link:active { transform: translateY(1px); }
        </style>
        <button class="top-link" id="to-grid">&uarr;&nbsp; Back to the grid</button>
        <script>""" + _SCROLL_TO_ANCHOR_JS + """
        document.getElementById('to-grid').onclick = function () {
          ksScrollToAnchor('grid-top');
        };
        </script>""",
        height=60,
    )


def _month_navigator() -> tuple:
    """Draw Previous and Next month buttons for the Schedule screen. Returns (year, month)."""
    today = dt.date.today()
    if "month" not in st.session_state:
        st.session_state["month"] = (today.year, today.month)
    year, month = st.session_state["month"]

    back, middle, forward = st.columns([1, 4, 1])
    if back.button("← Previous", width="stretch", key="month_back"):
        month -= 1
        if month == 0:
            month, year = 12, year - 1
        st.session_state["month"] = (year, month)
        st.session_state.pop("selected_lesson", None)
        _rerun()
    if forward.button("Next →", width="stretch", key="month_next"):
        month += 1
        if month == 13:
            month, year = 1, year + 1
        st.session_state["month"] = (year, month)
        st.session_state.pop("selected_lesson", None)
        _rerun()
    middle.markdown(
        f"<h4 style='text-align:center;margin:.2rem 0'>"
        f"{calendar.month_name[month]} {year}</h4>",
        unsafe_allow_html=True,
    )
    return year, month


def _describe_clash(lessons: list, when: dt.date, start: dt.time, end: dt.time) -> str:
    """Return a sentence naming the teacher's lesson that clashes, or "" if none does."""
    for item in lessons:
        if db.as_date(item["Date"]) != when:
            continue
        if db.as_time(item["Start"]) < end and db.as_time(item["End"]) > start:
            return (
                f"{_subject(item['Class'])} already runs "
                + _when(f"{db.as_time(item['Start']):%H:%M}–{db.as_time(item['End']):%H:%M}")
                + " that day."
            )
    return ""


def _clash_lines(clashes: list, dated: bool = False) -> str:
    """Return the students booked into another class at the same time, a line each."""
    lines = [
        f"{_student(item['Student'])} is also in {_subject(item['Class'])}"
        + (f" with {_teacher(item['Teacher'])}" if item["Teacher"] else "")
        + ", " + _when((f"{db.as_date(item['Date']):%a %d %b} " if dated else "")
                       + f"{db.as_time(item['Start']):%H:%M}–{db.as_time(item['End']):%H:%M}")
        for item in clashes[:8]
    ]
    if len(clashes) > 8:
        lines.append(f"…and {len(clashes) - 8} more")
    return "  \n".join(lines)


def _roster_rows(attendance: list) -> list:
    """Return a class's roster as saved, in the order the students were added."""
    return [
        {
            "student_id": row["student_id"],
            "is_online": bool(row.get("is_online")),
            "has_recording": bool(row.get("has_recording")),
            "is_cancelled": bool(row.get("is_cancelled")),
            "is_paid": bool(row.get("is_paid")),
            "note": row.get("note") or "",
        }
        for row in attendance
    ]


def _saved_message(message: str, lesson_id: int) -> None:
    """Flash `message`, with a warning if a student is now booked twice at once."""
    clashes = db.get_session_clashes(lesson_id)
    if clashes:
        _flash(message + " Booked into two classes at once:  \n" + _clash_lines(clashes), "warning")
    else:
        _flash(message)


def _month_clashes(year: int, month: int) -> None:
    """Show every student booked into two classes at once this month, across all teachers."""
    clashes = db.get_month_student_clashes(year, month)
    if not clashes:
        return
    with st.expander(
        f"{len(clashes)} double booking(s) in {calendar.month_name[month]} — "
        "a student in two classes at once",
        icon=":material/warning:",
    ):
        for item in clashes:
            first, second = item["First"], item["Second"]
            st.markdown(
                f"{_student(item['Student'])} · {_when(db.as_date(item['Date']).strftime('%a %d %b'))} · "
                + " and ".join(
                    f"{_subject(side['Class'])} with {_teacher(side['Teacher'])} "
                    + _when(f"{db.as_time(side['Start']):%H:%M}–{db.as_time(side['End']):%H:%M}")
                    for side in (first, second)
                )
            )


def _add_lesson(teacher_id: int, year: int, month: int, lessons: list) -> None:
    """Draw the "Add a class" form on the Schedule screen."""
    classes = db.get_teacher_classes_for_schedule(teacher_id, dt.date(year, month, 1))
    students = db.get_all_students()
    lookup = {item["Name"]: item["ID"] for item in students}
    names = [item["Class"] for item in classes]

    with st.expander("Add a class", expanded=not lessons):
        choice = st.selectbox("Subject", names + ["+ New subject"], key="add_class_pick")
        is_new = choice == "+ New subject"

        new_name = rate = colour = None
        if is_new:
            columns = st.columns([3, 1, 1])
            new_name = columns[0].text_input(
                "Subject name", key="new_class_name"
            )
            # Empty unless someone types a price: the academy's rule prices it (the grade in
            # its name, else its students' grades, else $60). A number in the box by default
            # was a price nobody chose.
            rate = columns[1].number_input(
                "Hourly rate ($)", min_value=0.01, value=None, step=5.0,
                placeholder="by grade", key="new_class_rate",
            )
            colour = columns[2].color_picker(
                "Colour", value="#FFF2CC", key="new_class_colour"
            )

        default_day = min(dt.date.today().day, calendar.monthrange(year, month)[1])
        columns = st.columns([2, 1, 1, 1])
        when = columns[0].date_input(
            "Date", value=dt.date(year, month, default_day), key="new_lesson_date"
        )
        # No suggested times: a filled-in time is easy to miss and then gets copied into
        # every repeat of the class.
        start = columns[1].time_input(
            "Start", value=None, step=60, key="new_lesson_start"
        )
        end = columns[2].time_input(
            "End", value=None, step=60, key="new_lesson_end"
        )
        months = st.session_state.setdefault("repeat_months", 0)
        with columns[3]:
            st.markdown(
                "<div style='font-size:14px;margin-bottom:.35rem'>Repeat</div>",
                unsafe_allow_html=True,
            )
            add_month, clear_month = st.columns([2, 1])
            if add_month.button("+1 month", key="repeat_add",
                                width="stretch"):
                st.session_state["repeat_months"] = months + 1
                _rerun()
            if clear_month.button("↺", key="repeat_clear", width="stretch",
                                  disabled=not months,
                                  help="Back to a single class"):
                st.session_state["repeat_months"] = 0
                _rerun()

        extra = months * WEEKS_PER_MONTH
        times_set = start is not None and end is not None
        if extra and times_set:
            last = when + dt.timedelta(weeks=extra)
            st.info(
                f"**{months} month{'s' if months > 1 else ''}** — "
                f"{extra + 1} classes in total, {_when(f'every {when:%A} at {start:%H:%M}')}, "
                f"from {_when(f'{when:%d %b}')} through {_when(f'{last:%d %b}')}."
            )
        elif extra:
            st.info(
                f"**{months} month{'s' if months > 1 else ''}** — "
                f"{extra + 1} classes in total, once the times are set."
            )
        else:
            st.caption(
                "One class. Press **+1 month** to repeat it weekly; press again "
                "for another month."
            )

        lesson_note = st.text_input(
            "Note for this class", placeholder="optional", key="new_lesson_note"
        )

        selected_ids: list = []
        if is_new:
            picked = st.multiselect("Students", sorted(lookup), key="new_class_students")
            selected_ids = [lookup[name] for name in picked]
        else:
            existing = next((c for c in classes if c["Class"] == choice), None)
            if existing and existing["Student IDs"]:
                if st.checkbox(
                    f"Start with the {len(existing['Student IDs'])} students "
                    "already in this subject",
                    value=True,
                    key="prefill_roster",
                ):
                    selected_ids = list(existing["Student IDs"])

        clash = _describe_clash(lessons, when, start, end) if times_set else ""
        if clash:
            st.warning(f"That time is taken. {clash}")
        if times_set and start >= end:
            st.warning("The end time needs to be after the start.")
        elif times_set and selected_ids:
            booked = db.find_student_clashes(
                selected_ids, [when + dt.timedelta(weeks=week) for week in range(extra + 1)],
                start, end,
            )
            if booked:
                st.warning("Booked into another class at this time:  \n"
                           + _clash_lines(booked, dated=bool(extra)))

        if st.button("Add class", type="primary", key="add_lesson_go",
                     disabled=not times_set,
                     help=None if times_set else "Set a start and end time first"):
            rows = [_blank_row(student_id) for student_id in selected_ids]
            status = "Completed" if when <= dt.date.today() else "Scheduled"
            class_id = None

            if is_new:
                if not (new_name or "").strip():
                    st.warning("Name the subject first.")
                    return
                typed_rate = rate
                rate = rate or schedule_parser.standard_rate(new_name) or db.UNSET_RATE
                outcome = db.create_class_and_first_session(
                    name=new_name, teacher_id=teacher_id, hourly_rate=rate,
                    display_color=colour, student_ids=selected_ids,
                    session_date=when, start_time=start, end_time=end,
                    status=status, note=lesson_note[:500], attendance_rows=rows,
                )
                if outcome != "created":
                    st.warning(_explain(outcome, clash))
                    return
                created = [
                    c for c in db.get_teacher_classes_for_schedule(teacher_id, when)
                    if c["Class"].strip().lower() == new_name.strip().lower()
                ]
                class_id = created[0]["ID"] if created else None
                if class_id and not typed_rate and rate <= db.UNSET_RATE:
                    # No grade in the name: its students' grades decide, else $60.
                    schedule_backfill.price_by_rule(db.get_unpriced_subjects(class_ids=[class_id]))
            else:
                class_id = next(c["ID"] for c in classes if c["Class"] == choice)
                outcome = db.create_schedule_session(
                    teacher_id=teacher_id, class_id=class_id, session_date=when,
                    start_time=start, end_time=end, status=status,
                    note=lesson_note[:500], attendance_rows=rows,
                )
                if outcome != "created":
                    st.warning(_explain(outcome, clash))
                    return

            copied = skipped = 0
            if extra and class_id:
                copied, skipped, _ = _repeat_weekly(
                    teacher_id, class_id, when, start, end, extra, rows,
                    note=lesson_note[:500],
                )

            st.session_state["repeat_months"] = 0
            message = f"Added {copied + 1} class(es)."
            if skipped:
                message += f" {skipped} skipped — the teacher was already busy."
            _flash(message)
            _rerun()


def _run_dates(teacher_id: int, class_id: int, start: dt.time, base: dt.date) -> list:
    """Return every date this subject runs at this time, from `base` onward.

    Looks six months ahead, so a run that crosses into the next month is
    still found in full.
    """
    found = []
    year, month = base.year, base.month
    for _ in range(6):
        found += [
            db.as_date(item["Date"])
            for item in db.get_month_schedule(teacher_id, year, month)
            if item["Class ID"] == class_id and db.as_time(item["Start"]) == start
        ]
        month += 1
        if month == 13:
            month, year = 1, year + 1
    return sorted(set(found))


def _lesson_editor(lesson_id: int, teacher_id: int) -> None:
    """Show everything about one class, then everything about its subject.

    Both are in expanders so the screen reads as two decisions instead of a
    wall of controls: what happened in this lesson, and what the subject is.
    """
    detail = db.get_schedule_session(lesson_id)
    if not detail:
        st.info("That class no longer exists.")
        return

    students = db.get_all_students()
    lookup = {item["Name"]: item["ID"] for item in students}
    student_notes = {item["ID"]: item["Note"] for item in students if item["Note"]}
    attendance = detail.get("Attendance", [])

    st.markdown(
        f"#### {detail['Class']} · {db.as_date(detail['Date']):%a %d %b} · "
        f"{db.as_time(detail['Start']):%H:%M}–{db.as_time(detail['End']):%H:%M}"
    )
    clashes = db.get_session_clashes(lesson_id)
    if clashes:
        st.warning("Booked into another class at the same time:  \n" + _clash_lines(clashes))

    with st.expander("Edit the class", expanded=True):
        columns = st.columns([1.4, 1, 1, 1.2])
        when = columns[0].date_input(
            "Date", value=db.as_date(detail["Date"]), key=f"date_{lesson_id}"
        )
        start = columns[1].time_input(
            "Start", value=db.as_time(detail["Start"]), step=60, key=f"start_{lesson_id}"
        )
        end = columns[2].time_input(
            "End", value=db.as_time(detail["End"]), step=60, key=f"end_{lesson_id}"
        )
        status = columns[3].selectbox(
            "Status",
            LESSON_STATUSES,
            index=LESSON_STATUSES.index(detail["Status"])
            if detail["Status"] in LESSON_STATUSES
            else 0,
            key=f"status_{lesson_id}",
        )

        lesson_note = st.text_input(
            "Note for this class",
            value=detail.get("Note") or "",
            placeholder="Shows on the grid — a room change, a makeup, anything unusual.",
            key=f"note_{lesson_id}",
        )

        st.markdown("**Students and their condition**")
        already = {row["student_name"] for row in attendance}
        addable = sorted(name for name in lookup if name not in already)
        add_columns = st.columns([4, 1])
        adding = add_columns[0].multiselect(
            "Add students to this class",
            addable,
            key=f"add_{lesson_id}",
            placeholder="Pick one or more names",
        )
        if adding:
            booked = db.find_student_clashes(
                [lookup[name] for name in adding], [db.as_date(detail["Date"])],
                db.as_time(detail["Start"]), db.as_time(detail["End"]), excluded_session_id=lesson_id,
            )
            if booked:
                st.warning("Booked into another class at this time:  \n" + _clash_lines(booked))
        if add_columns[1].button("Add", key=f"addgo_{lesson_id}",
                                 width="stretch", disabled=not adding):
            existing = _roster_rows(attendance)
            for name in adding:
                existing.append(_blank_row(lookup[name]))
            outcome = db.update_schedule_session(
                session_id=lesson_id,
                session_date=db.as_date(detail["Date"]),
                start_time=db.as_time(detail["Start"]),
                end_time=db.as_time(detail["End"]),
                status=detail["Status"],
                note=detail.get("Note") or "",
                attendance_rows=existing,
            )
            if outcome in ("updated", "success", True):
                _saved_message(f"Added {len(adding)} student(s).", lesson_id)
                _rerun()
            else:
                st.warning(_explain(str(outcome)))

        # Taking a student out had no button of its own: you could delete a row in the
        # table and save, which nobody found, and the "Delete" below, which people tried
        # instead, deletes the whole class.
        on_class = [row["student_name"] for row in attendance]
        if on_class:
            out_columns = st.columns([4, 1])
            removing = out_columns[0].multiselect(
                "Take students out of this class", on_class, key=f"out_{lesson_id}",
                placeholder="Pick one or more names",
                help="Anyone already invoiced for this class is owed it back as a credit.",
            )
            if out_columns[1].button("Remove", key=f"outgo_{lesson_id}",
                                     width="stretch", disabled=not removing):
                kept = _roster_rows([row for row in attendance if row["student_name"] not in removing])
                outcome = db.update_schedule_session(
                    session_id=lesson_id,
                    session_date=db.as_date(detail["Date"]),
                    start_time=db.as_time(detail["Start"]),
                    end_time=db.as_time(detail["End"]),
                    status=detail["Status"],
                    note=detail.get("Note") or "",
                    attendance_rows=kept,
                )
                if outcome in ("updated", "success", True):
                    _flash(f"Took {len(removing)} student(s) out of this class.")
                    _rerun()
                else:
                    st.warning(_explain(str(outcome)))

        st.caption("In the order they were added. For this class only. "
                   "Paid fills in when the invoice is paid on Payments.")
        rows = [
            {
                "No.": number,
                "Student": row["student_name"],
                "Condition": _condition_of(row),
                "Paid": bool(row.get("is_paid")),
                "Note": row.get("note") or "",
            }
            for number, row in enumerate(attendance, 1)
        ]
        edited = st.data_editor(
            rows or [{"No.": None, "Student": None, "Condition": IN_CLASS, "Paid": False, "Note": ""}],
            num_rows="dynamic",
            column_config={
                "No.": st.column_config.NumberColumn(width=50, disabled=True),
                "Student": st.column_config.SelectboxColumn(
                    options=sorted(lookup), required=True, width="medium"
                ),
                "Condition": st.column_config.SelectboxColumn(
                    options=CONDITIONS, required=True, width="small",
                    help="One condition per student per class — picking a new "
                         "one replaces the old.",
                ),
                # Read-only: payment belongs to the invoice the parent pays and is recorded on
                # the Payments screen. Editable here too, it could disagree with the invoice.
                "Paid": st.column_config.CheckboxColumn(
                    width="small",
                    disabled=True,
                    help="Set automatically when the invoice covering this "
                         "class is marked paid on the Payments screen.",
                ),
                "Note": st.column_config.TextColumn(width="medium"),
            },
            width="stretch",
            hide_index=True,
            key=f"roster_{lesson_id}",
        )

        on_roster = {row.get("Student") for row in edited}
        flagged = [
            (item["Name"], student_notes[item["ID"]])
            for item in students
            if item["ID"] in student_notes and item["Name"] in on_roster
        ]
        if flagged:
            st.caption("Notes on students in this class")
            for name, text in flagged:
                st.markdown(f"📝 {_student(name)} — {text}")

        buttons = st.columns([1, 1.3, 4])
        if buttons[0].button("Save class", key=f"save_{lesson_id}", type="primary",
                             width="stretch"):
            prepared = []
            seen = set()
            duplicates = []
            for row in edited:
                name = (row.get("Student") or "").strip()
                if not name or name not in lookup:
                    continue
                student_id = lookup[name]
                if student_id in seen:
                    # One row per student: the table lets you pick the same name twice, the database
                    # doesn't.
                    duplicates.append(name)
                    continue
                seen.add(student_id)
                prepared.append(
                    {
                        "student_id": student_id,
                        **_flags_for(row.get("Condition") or IN_CLASS),
                        "is_paid": bool(row.get("Paid")),
                        "note": (row.get("Note") or "")[:200],
                    }
                )
            if duplicates:
                st.warning(
                    "Listed more than once, so only the first row was kept: "
                    + _chips("student", sorted(set(duplicates)), most=99)
                )
            outcome = db.update_schedule_session(
                session_id=lesson_id,
                session_date=when,
                start_time=start,
                end_time=end,
                status=status,
                note=lesson_note[:500],
                attendance_rows=prepared,
            )
            if outcome in ("updated", "success", True):
                if (when.year, when.month) != st.session_state.get("month"):
                    # The class moved out of the month on screen; follow it instead of leaving an
                    # empty grid.
                    st.session_state["month"] = (when.year, when.month)
                _saved_message("Saved.", lesson_id)
                _rerun()
            else:
                st.warning(_explain(str(outcome)))

        run = _run_dates(
            teacher_id, detail["Class ID"], db.as_time(detail["Start"]),
            db.as_date(detail["Date"]),
        )
        is_last = not run or db.as_date(detail["Date"]) >= max(run)
        months_key = f"rep_{lesson_id}"
        months = st.session_state.setdefault(months_key, 0)

        with buttons[1]:
            if not is_last:
                # Repeating from the middle would land on dates the run already has, so point to
                # the class that can extend it.
                st.button(
                    "Repeat", disabled=True, width="stretch",
                    key=f"repdis_{lesson_id}",
                    help=f"Only the last class of a run can be repeated. "
                         f"This run already reaches {max(run):%d %b} — open that "
                         "class to extend it.",
                )
            else:
                add, reset = st.columns([2, 1])
                if add.button("+1 month", key=f"repadd_{lesson_id}",
                              width="stretch"):
                    st.session_state[months_key] = months + 1
                    _rerun()
                if reset.button("↺", key=f"repclr_{lesson_id}",
                                width="stretch", disabled=not months,
                                help="Clear"):
                    st.session_state[months_key] = 0
                    _rerun()

        if is_last and months:
            extra = months * WEEKS_PER_MONTH
            through = db.as_date(detail["Date"]) + dt.timedelta(weeks=extra)
            st.info(
                f"**+{months} month{'s' if months > 1 else ''}** — adds {extra} "
                f"weekly classes, "
                f"{_when(db.as_date(detail['Date']).strftime('every %A at ') + db.as_time(detail['Start']).strftime('%H:%M'))}, "
                f"through {_when(through.strftime('%d %b'))}. "
                "Same students, everyone in class and unpaid."
            )
            confirm = st.columns([1, 4])[0]
            if confirm.button(f"Add {extra} classes", key=f"repgo_{lesson_id}",
                              type="primary", width="stretch"):
                base = db.as_date(detail["Date"])
                made, blocked, last = _repeat_weekly(
                    teacher_id, detail["Class ID"], base,
                    db.as_time(detail["Start"]), db.as_time(detail["End"]), extra,
                    [_blank_row(row["student_id"]) for row in attendance],
                )
                st.session_state[months_key] = 0
                if made:
                    message = f"Added {made} class(es), through {_when(last.strftime('%d %b'))}."
                    if blocked:
                        message += f" {blocked} skipped — the teacher was busy."
                    st.success(message)
                else:
                    _flash("Nothing added — those slots are already taken.", "info")
                _rerun()

    with st.expander(f"Edit the subject — {detail['Class']}"):
        st.caption("Applies to every class of this subject.")
        current = next(
            (
                item
                for item in db.get_teacher_classes_for_schedule(
                    teacher_id, db.as_date(detail["Date"])
                )
                if item["ID"] == detail["Class ID"]
            ),
            {},
        )
        subject_note_value = current.get("Note", "")
        columns = st.columns([3, 1, 1])
        class_name = columns[0].text_input(
            "Subject name", value=detail["Class"], key=f"cname_{lesson_id}"
        )
        on_record = float(current.get("Hourly Rate") or 0)
        if on_record <= db.UNSET_RATE:
            on_record = schedule_backfill.price_rule(
                [{"Class ID": detail["Class ID"], "Class": detail["Class"]}]
            )[detail["Class ID"]][0]
        class_rate = columns[1].number_input(
            "Hourly rate",
            min_value=0.01,
            value=on_record,
            step=5.0,
            key=f"crate_{lesson_id}",
        )
        class_colour = columns[2].color_picker(
            "Colour",
            value=current.get("Colour") or "#FFF2CC",
            key=f"ccolour_{lesson_id}",
        )
        class_note = st.text_input(
            "Subject note — flags every class of this subject on the grid",
            value=subject_note_value,
            key=f"cnote_{lesson_id}",
        )
        if st.button("Save subject", key=f"csave_{lesson_id}", type="primary"):
            outcome = db.update_scheduling_class(
                class_id=detail["Class ID"],
                name=class_name,
                hourly_rate=class_rate,
                # Students belong to a class, not the subject; keep the existing enrolment instead
                # of rewriting it from this screen.
                student_ids=db.get_class_student_ids(detail["Class ID"]),
                display_color=class_colour,
                effective_from=db.as_date(detail["Date"]),
            )
            if outcome in ("updated", "success", True):
                # Only once the subject itself has saved: saving the note first left it on a
                # rename that was refused.
                db.set_subject_note(detail["Class ID"], class_note[:1000])
                _flash("Subject saved.")
                _rerun()
            else:
                st.warning(_explain(str(outcome)))


def schedule_tab() -> None:
    """Draw the Schedule screen: the month grid, then the editor for the class clicked."""
    teachers = [item for item in db.get_all_teachers() if item["Active"]]
    if not teachers:
        st.info("Add a teacher first — a schedule belongs to one.")
        return

    top = st.columns([2, 6])
    teacher_name = top[0].selectbox(
        "Teacher", [item["Name"] for item in teachers], key="tt_teacher"
    )
    teacher_id = next(item["ID"] for item in teachers if item["Name"] == teacher_name)

    _anchor("grid-top")
    year, month = _month_navigator()
    _month_clashes(year, month)
    summary = db.get_month_schedule(teacher_id, year, month)
    # One query for the whole month's rosters, not three per class. The grid only
    # needs each card's student names and conditions, which is what this returns.
    rosters = db.get_month_attendance(teacher_id, year, month)
    lessons = [
        {**item, "Attendance": rosters.get(item["ID"], [])} for item in summary
    ]

    if lessons:
        strip = st.columns(5)
        strip[0].metric("Classes", len(lessons))
        strip[1].metric("Teaching days", len({db.as_date(i["Date"]) for i in lessons}))
        strip[2].metric("Online", sum(i.get("Online", 0) for i in summary))
        strip[3].metric("Recording", sum(i.get("Recording", 0) for i in summary))
        strip[4].metric("Cancelled", sum(i.get("Cancelled students", 0) for i in summary))

        clicked = render_month_grid(
            lessons,
            selected=st.session_state.get("selected_lesson"),
            class_notes={
                item["Class ID"]: item.get("Subject note", "")
                for item in summary
                if item.get("Subject note")
            },
            student_notes={
                item["ID"]: item["Note"] for item in db.get_all_students() if item["Note"]
            },
            unpaid={item["ID"]: item.get("Unpaid", 0) for item in summary},
            key=f"grid_{teacher_id}_{year}_{month}",
        )
        if clicked and clicked.get("nonce") != st.session_state.get("grid_click"):
            # Compare the click's nonce, not the class id: clicking the same class again is
            # a new click and should jump to the editor again.
            st.session_state["grid_click"] = clicked.get("nonce")
            st.session_state["selected_lesson"] = clicked.get("lesson_id")
            st.session_state["jump_to_editor"] = True
            _rerun()
    else:
        st.info(f"Nothing scheduled for {_when(f'{calendar.month_name[month]} {year}')} yet.")

    _add_lesson(teacher_id, year, month, lessons)

    if not lessons:
        return

    selected = st.session_state.get("selected_lesson")
    if selected not in {item["ID"] for item in lessons}:
        selected = None

    if selected is None:
        return  # the grid's own legend says to click a class

    _anchor("class-editor")
    _lesson_editor(selected, teacher_id)
    _back_to_grid_button()
    if st.session_state.pop("jump_to_editor", False):
        _scroll_to("class-editor", st.session_state.get("grid_click", ""))


# ---------------------------------------------------------------------------
# Invoices and Credits
# ---------------------------------------------------------------------------


def _invoice_filename(invoice: dict, ext: str) -> str:
    """Return the file name for an invoice: student name, then invoice number."""
    label = invoice.get("Number") or f"draft-{invoice['ID']}"
    student = "".join(
        ch for ch in invoice.get("Student", "student") if ch.isalnum() or ch in " -_"
    ).strip().replace(" ", "-")
    # Name first, then number, so a folder of these sorts by student and the file
    # for one parent is easy to find.
    return f"{student}-invoice-{label}.{ext}"


def _invoice_download(invoice: dict, key: str) -> None:
    """Offer the printable invoice as an HTML file, ready to print or send.

    HTML on purpose, not an image: this runs for every invoice on screen
    (up to the whole open list), and a download button's file has to be made
    on every page load, clicked or not. HTML costs nothing to build; a PNG
    starts a whole headless browser, and doing that per row would make the
    page crawl. The "download as images" button makes every image with one
    shared browser instead, which is why that one is fast.
    """
    st.download_button(
        "Download / print",
        data=render_invoice_html(invoice),
        file_name=_invoice_filename(invoice, "html"),
        mime="text/html",
        key=key,
        width="stretch",
        help="Opens in a browser; print from there to get a PDF. For an "
        "image ready to send in a chat app, use the image download above.",
    )


_EXPORT_NOUN = {"png": "image", "pdf": "PDF", "html": "HTML file"}


def _send_format() -> str:
    """Return the format this machine can make for sending to a parent: png, pdf or html.

    Images come first: KakaoTalk shows them in the chat, while a PDF arrives
    as an attachment the parent has to tap open. But images need a headless
    Chromium, which the hosted app doesn't have and can't install, so it
    uses PDF there (same layout, no browser). HTML always works but is no
    use on a phone.

    Deciding it here keeps the answer the same in all three places invoices
    can be downloaded.
    """
    if image_export_available():
        return "png"
    if pdf_export_available():
        return "pdf"
    return "html"


def _send_noun() -> str:
    """Return the word for the send format: "image", "PDF" or "HTML file"."""
    return _EXPORT_NOUN[_send_format()]


def _invoices_zip(invoices: list[dict], as_images: bool, on_progress=None) -> bytes:
    """Return every invoice as its own file inside one zip.

    One click gets each student's invoice as a separate file, ready to send
    to each parent, instead of one combined document that would show every
    student's billing to whoever gets it.
    """
    buffer = io.BytesIO()
    ext = _send_format() if as_images else "html"
    if ext == "png":
        pages = render_invoices_png(invoices, on_progress=on_progress)
    elif ext == "pdf":
        pages = render_invoices_pdf(invoices, on_progress=on_progress)
    else:
        pages = [render_invoice_html(invoice) for invoice in invoices]
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        used_names: set[str] = set()
        for invoice, page in zip(invoices, pages):
            name = _invoice_filename(invoice, ext)
            if name in used_names:
                # Two invoices could end up with the same file name (e.g. students with the same
                # name), so add the id to keep both.
                name = f"{invoice['ID']}-{name}"
            used_names.add(name)
            archive.writestr(name, page)
    return buffer.getvalue()


def _invoice_table(invoice: dict) -> None:
    """Show an invoice's lines as a table."""
    lines = invoice.get("Lines") or []
    if not lines:
        st.caption("No classes on this invoice yet.")
        return
    st.dataframe(
        _coloured([
            {
                "Qty": line["Quantity"],
                "Subject": line["Subject"],
                "Dates": format_dates(line["Dates"]),
                # A credit has no unit price (nothing is charged), so "$0.00 × 0h" would just be
                # clutter. The printable copy does the same.
                "Unit price": (
                    "credit" if line.get("Credit")
                    else "" if line.get("Extra")
                    else f"${line['Rate']:,.2f} × {line['Hours']:g}h"
                ),
                "Amount": f"{line['Amount']:,.2f}",
            }
            for line in lines
        ]),
        width="stretch",
        hide_index=True,
    )


def _just_issued_panel(year: int, month: int) -> None:
    """Show what was just issued, and a way to send those invoices to parents.

    It sits at the bottom of the screen, not the top: issuing makes a long
    list, and above the month figures it hid what you came to read. It
    belongs to the month it was issued for, so it's out of the way once you
    move to another month.

    Each row is only a tick box. Building a downloadable invoice for every
    student up front made this section unusable: each one includes the
    letterhead, so a whole month was megabytes before anything was asked for.
    """
    batch = st.session_state.get("invoices_just_issued")
    if not batch or tuple(batch.get("period", ())) != (year, month):
        return
    ids = batch.get("ids") or []
    details = db.get_invoices_detailed(ids)
    if not details:
        return

    period = f"{calendar.month_name[month]} {year}"
    st.divider()
    st.markdown(f"#### Just issued — {len(details)} invoice(s) for {_when(period)}")

    scope = f"{year}_{month}"

    def _select_all() -> None:
        """Tick or untick every invoice to match Select all."""
        value = st.session_state.get(f"ji_all_{scope}", False)
        for item in details:
            st.session_state[f"ji_pick_{scope}_{item['ID']}"] = value

    st.checkbox("Select all", key=f"ji_all_{scope}", on_change=_select_all)

    chosen = []
    for detail in details:
        if st.checkbox(
            f"#{detail['Number']} — {_student(detail['Student'])} — {_money(detail['Total'])}",
            key=f"ji_pick_{scope}_{detail['ID']}",
        ):
            chosen.append(detail)

    if not chosen:
        st.caption("Tick the students you want to send to, or use Select all.")
    else:
        # Keyed on exactly who is ticked, so changing the ticks asks for new files
        # instead of returning the previous batch.
        token = "_".join(str(d["ID"]) for d in chosen)
        zip_key = f"invoices_zip_png_{token}"
        error_key = f"{zip_key}_error"
        zip_bytes = st.session_state.get(zip_key)
        error = st.session_state.get(error_key)

        if zip_bytes:
            st.download_button(
                f"📱 Download {len(chosen)} {_send_noun()}(s) (.zip)"
                + (" — ready for KakaoTalk" if _send_format() == "png" else ""),
                data=zip_bytes,
                file_name=f"invoices-{period.replace(' ', '-')}-{len(chosen)}.zip",
                mime="application/zip",
                key=f"dl_zip_png_{scope}",
                type="primary",
                width="stretch",
                help=f"One {_send_noun()} per student inside the zip — extract and send "
                "each one straight to its parent.",
            )
        elif st.button(
            f"📱 Generate {_send_noun()}s for {len(chosen)} selected",
            key=f"prep_zip_png_{scope}",
            type="primary",
            width="stretch",
            help=f"Renders each selected invoice as a{'n' if _send_noun()[0] in 'aeiou' else ''} {_send_noun()}, then gives you "
            "a zip to download.",
        ):
            st.session_state.pop(error_key, None)
            # A batch of files can be tens of megabytes, and every different selection would
            # leave its own copy in memory for the whole session. Only the newest is kept.
            for stale in [k for k in st.session_state
                          if k.startswith("invoices_zip_png_") and k != zip_key]:
                st.session_state.pop(stale, None)
            try:
                with st.spinner(f"Rendering {len(chosen)} {_send_noun()}(s)…"):
                    st.session_state[zip_key] = _invoices_zip(chosen, as_images=True)
            except Exception as problem:  # noqa: BLE001 - shown to the user below
                st.session_state[error_key] = str(problem)
            _rerun()

        if error:
            st.error(
                f"Could not render the {_send_noun()}s. The invoices "
                "themselves are issued and safe — use an option below "
                "meanwhile."
            )
            if "Executable doesn't exist" in error or "playwright install" in error:
                st.caption(
                    "The image renderer's browser is missing. Close the app "
                    "and run this once, then start it again:"
                )
                st.code(r".venv\Scripts\python -m playwright install chromium", language="bash")
            else:
                st.caption(f"Details: {error}")

        # A checkbox, not an expander, on purpose: Streamlit runs an expander's contents
        # on every rerun, and each of these builds the files for every ticked invoice
        # (megabytes) before anyone asks for them.
        if st.checkbox(
            f"Other formats for the {len(chosen)} selected",
            key=f"other_formats_{scope}",
        ):
            columns = st.columns(2)
            columns[0].download_button(
                "Separate HTML files (.zip)",
                data=_invoices_zip(chosen, as_images=False),
                file_name=f"invoices-{period.replace(' ', '-')}-{len(chosen)}-html.zip",
                mime="application/zip",
                key=f"dl_zip_html_{scope}",
                width="stretch",
            )
            columns[1].download_button(
                "One combined printable file",
                data=render_invoices_batch_html(chosen),
                file_name=f"invoices-{period.replace(' ', '-')}-{len(chosen)}.html",
                mime="text/html",
                key=f"dl_batch_{scope}",
                width="stretch",
                help="One invoice per printed page — for a paper stack, not for sending.",
            )

    if st.button("Dismiss", key=f"dismiss_just_issued_{scope}"):
        st.session_state.pop("invoices_just_issued", None)
        # A batch of files can be tens of megabytes; don't keep it in memory once the
        # panel is closed.
        for key in [k for k in st.session_state if k.startswith("invoices_zip_png_")]:
            st.session_state.pop(key, None)
        _rerun()


def _credits_pointer() -> None:
    """Show a line on the Invoices screen saying what credit is waiting, and where to see it."""
    waiting = db.get_credits(status="Open")
    if waiting:
        st.caption(
            f"{len(waiting)} credit(s) for missed classes, {_money(sum(c['Amount'] for c in waiting))}, "
            "come off next invoices by themselves — see Credits."
        )


_ALL_MONTHS = "All months"


def credits_tab() -> None:
    """Draw the Credits screen: credit waiting to come off an invoice, and credit already used.

    A lesson a student was invoiced for and then missed (marked cancelled, or
    taken off the lesson) is owed back and comes off their next invoice by
    itself. This screen shows that one student at a time, and lets the admin
    mark a credit as refunded when it was paid back in cash, so it doesn't
    come off anything.
    """
    st.caption("Classes already invoiced, then missed. Each comes off the student's next invoice.")
    credits = db.get_credits()

    # By the month of the missed lesson, to answer "what did September's
    # cancellations come to".
    today = dt.date.today()
    years = sorted({c["Class date"].year for c in credits if c["Class date"]} | {today.year},
                   reverse=True)
    columns = st.columns([1, 1.4, 2.6])
    year = columns[0].selectbox("Year", years, key="credits_year")
    month = columns[1].selectbox(
        "Month of the class", [_ALL_MONTHS] + list(calendar.month_name[1:]), key="credits_month"
    )
    search = columns[2].text_input(
        "Search", placeholder="Type part of a student's name", key="credits_search"
    ).strip().casefold()

    def shown(credit) -> bool:
        """Return True if a credit is in the chosen month and matches the search."""
        day = credit["Class date"]
        if day is None or day.year != year:
            return False
        if month != _ALL_MONTHS and day.month != list(calendar.month_name).index(month):
            return False
        return not search or search in credit["Student"].casefold()

    picked = [c for c in credits if shown(c)]
    waiting = [c for c in picked if c["Status"] == "Open"]
    applied = [c for c in picked if c["Status"] == "Applied"]
    refunded = [c for c in picked if c["Status"] == "Refunded"]
    period = f"{month} {year}" if month != _ALL_MONTHS else str(year)
    columns = st.columns(3)
    columns[0].metric(
        "Waiting", f"${sum(c['Amount'] for c in waiting):,.2f}",
        help=f"{len(waiting)} credit(s) for classes in {period}, to come off each "
             "student's next invoice.",
    )
    columns[1].metric(
        "Taken off invoices", f"${sum(c['Amount'] for c in applied):,.2f}",
        help=f"{len(applied)} credit(s) for classes in {period}, already deducted.",
    )
    columns[2].metric(
        "Refunded", f"${sum(c['Amount'] for c in refunded):,.2f}",
        help=f"{len(refunded)} credit(s) for classes in {period}, paid back in money.",
    )
    # The filter stays on screen even with nothing to filter: a screen that hides its
    # controls until there's data looks like it's missing them.
    if not credits:
        st.info("No credits yet.")
        return

    def when(credit):
        """Return a credit's lesson date as text."""
        return credit["Class date"].strftime("%d %b %Y") if credit["Class date"] else ""

    def line(credit):
        """Return one credit as a line of badges."""
        return (f"{_when(when(credit))} · {_subject(credit['Subject'])} · "
                f"{_teacher(credit['Teacher']) if credit['Teacher'] else '—'} · "
                f"{_money(credit['Amount'])} · {credit['Reason']}")

    # One line per student, with what they're owed; open it to see the lessons behind
    # the figure. When a parent asks why there's a credit on the bill, the answer is
    # one click from the child's name. A credit already paid back in cash is ticked
    # and marked refunded right there, so it stops coming off the next invoice.
    by_student: dict[int, list[dict]] = {}
    for credit in waiting:
        by_student.setdefault(credit["Student ID"], []).append(credit)
    st.markdown(f"#### Waiting for the next invoice ({len(by_student)} student(s))")
    if not waiting:
        st.caption(f"None waiting for classes in {_when(period)}.")
    for owed in sorted(by_student.values(), key=lambda cs: cs[0]["Student"].casefold()):
        student_id = owed[0]["Student ID"]
        with st.expander(
            f"{_student(owed[0]['Student'])} — {_money(sum(c['Amount'] for c in owed))} "
            f"· {len(owed)} class(es)"
        ):
            st.caption("Tick a class only if its money has already been paid back to the parent.")
            ticked = [
                c for c in sorted(owed, key=lambda c: c["Class date"] or dt.date.min)
                if st.checkbox(line(c), key=f"credit_refund_{c['ID']}")
            ]
            if st.button(
                f"Mark {len(ticked)} refunded — ${sum(c['Amount'] for c in ticked):,.2f} paid back"
                if ticked else "Mark refunded",
                key=f"credit_refund_go_{student_id}", disabled=not ticked,
            ):
                done = db.refund_credits([c["ID"] for c in ticked])
                for c in ticked:
                    st.session_state.pop(f"credit_refund_{c['ID']}", None)
                _flash(f"Marked {done} credit(s) refunded to {_student(owed[0]['Student'])} — "
                       f"{_money(sum(c['Amount'] for c in ticked))}. They won't come off an invoice.")
                _rerun()

    settled_by: dict[int, list[dict]] = {}
    for credit in applied + refunded:
        settled_by.setdefault(credit["Student ID"], []).append(credit)
    st.markdown(f"#### Settled ({len(settled_by)} student(s))")
    if not settled_by:
        st.caption(f"None settled for classes in {_when(period)}.")
        return
    # Most recently settled first, and no more than a screenful of names; the month
    # and search narrow it to anyone further back.
    latest = sorted(settled_by.values(),
                    key=lambda cs: max(c["Settled"] or dt.date.min for c in cs), reverse=True)
    for done in latest[:60]:
        with st.expander(
            f"{_student(done[0]['Student'])} — {_money(sum(c['Amount'] for c in done))} "
            f"· {len(done)} class(es)"
        ):
            st.dataframe(
                _coloured([
                    {
                        "Class": when(c),
                        "Subject": c["Subject"],
                        "Amount": f"${c['Amount']:,.2f}",
                        "How": (f"Taken off invoice #{c['Invoice number']}"
                                if c["Status"] == "Applied" and c["Invoice number"]
                                else "Taken off an invoice" if c["Status"] == "Applied"
                                else "Refunded"),
                        "On": c["Settled"],
                    }
                    for c in sorted(done, key=lambda c: c["Class date"] or dt.date.min)
                ], {"Class": "when"}),
                width="stretch",
                hide_index=True,
            )
    if len(latest) > 60:
        st.caption(f"Showing the 60 most recent of {len(latest)} — pick a month or search.")


def _issued_lookup_section(counts: dict) -> None:
    """Find and reprint an invoice that has already gone out."""
    # Sent invoices are finished with, so they're in a closed section, but they still
    # need to be easy to find when a parent asks for a copy. Also only loaded when
    # opened, like the open list above.
    if not counts["issued_count"]:
        return
    st.markdown(f"#### Issued invoices ({counts['issued_count']})")
    # Set by "Edit" on the warning above, or by saving a replacement: the invoice to
    # open, set before the widgets it fills are drawn.
    wanted = st.session_state.pop("issued_search_next", None)
    if wanted is not None:
        st.session_state["issued_search"] = wanted
        st.session_state["invoice_show_issued"] = True
        st.session_state.pop("issued_pick", None)
    if not st.checkbox("Look one up", key="invoice_show_issued"):
        return
    with st.container():
        # A void invoice is still one a parent may quote, so it can be found.
        issued = sorted(
            db.get_invoices(status="Issued") + db.get_invoices(status="Void"),
            key=lambda item: -(item["Number"] or 0),
        )
        search = st.text_input(
            "Find by student or invoice number", key="issued_search"
        )
        needle = search.strip().lower()
        found = [
            item
            for item in issued
            if not needle
            or needle in item["Student"].lower()
            or needle in str(item["Number"])
        ]
        st.caption(f"{len(found)} of {len(issued)} shown, newest first.")
        st.dataframe(
            _coloured([
                {
                    "Invoice": f"#{item['Number']}" + (
                        f" · void, now #{item['Replaced by']}" if item["Status"] == "Void" else ""),
                    "Student": item["Student"],
                    "Issued": item["Issued"],
                    "Classes": item["Classes"],
                    "Total (SGD)": f"{item['Total']:,.2f}",
                }
                for item in found[:100]
            ]),
            width="stretch",
            hide_index=True,
        )
        if not found:
            return
        labels = {
            f"#{item['Number']} · {item['Student']}"
            + (" · void" if item["Status"] == "Void" else ""): item["ID"]
            for item in found[:100]
        }
        chosen = st.selectbox("Open a copy", list(labels), key="issued_pick")
        detail = db.get_invoice(labels[chosen])
        if detail["Status"] == "Void":
            st.warning(
                f"Void — replaced by invoice #{detail['Replaced by']} on "
                f"{_when(detail['Replaced on'].strftime('%d %b %Y'))}. This is what #{detail['Number']} said."
                + (f" Why: {detail['Note']}" if detail.get("Note") else "")
            )
            _invoice_table(detail)
            return
        if detail.get("Replaces"):
            st.caption(f"Replaces invoice #{detail['Replaces']}.")
        _invoice_table(detail)
        columns = st.columns([1.4, 1.6, 3])
        with columns[0]:
            _invoice_download(detail, "dl_issued")
        with columns[1]:
            _send_copy(detail, "send_issued")
        if st.toggle("Edit this invoice", key=f"invoice_edit_{detail['ID']}"):
            _invoice_editor(detail)
        if st.toggle("Change its number", key=f"invoice_renumber_{detail['ID']}"):
            _number_changer(detail)


def _forget_made_files() -> None:
    """Forget invoice files already made: after a renumbering they show the old number.

    Each is made again, with the new number, next time it's asked for.
    """
    for key in [k for k in st.session_state if k.startswith("invoices_zip_png_")
                or k in ("invoices_replaced_zip", "month_download_file", "send_issued_file")]:
        st.session_state.pop(key, None)


def _number_changer(detail: dict) -> None:
    """Give one sent invoice another number.

    A number another invoice has isn't just refused: it shows whose it is,
    with the choice of giving that one another number (this invoice's old
    number swaps the two).
    """
    key = f"renumber_{detail['ID']}"
    with st.form(f"{key}_form", border=False):
        number = st.number_input("New number", min_value=db.INVOICE_NUMBER_START,
                                 value=int(detail["Number"]), step=1)
        if st.form_submit_button("Change the number", type="primary"):
            outcome, info = db.renumber_invoice(detail["ID"], number)
            if outcome == "taken":
                st.session_state[f"{key}_clash"] = {"wanted": int(number), "holder": info}
                _rerun()
            st.session_state.pop(f"{key}_clash", None)
            _renumbered(detail, outcome, info)
    clash = st.session_state.get(f"{key}_clash")
    if not clash:
        return
    wanted, holder = clash["wanted"], clash["holder"]
    issued = f", issued {db.as_date(holder['Issued']):%d %b %Y}" if holder.get("Issued") else ""
    st.warning(
        f"**#{wanted} is already used** by {_student(holder['Student'])}'s invoice"
        f"{' (void)' if holder['Status'] == 'Void' else ''}{issued}."
    )
    with st.form(f"{key}_clash_form", border=False):
        move_to = st.number_input(
            f"New number for {_student(holder['Student'])}'s invoice", min_value=db.INVOICE_NUMBER_START,
            value=db.get_next_invoice_number()["top"], step=1,
            help=f"#{detail['Number']} swaps the two invoices' numbers.",
        )
        move = st.form_submit_button(f"Renumber it and give this one #{wanted}", type="primary",
                                     width="stretch")
        cancel = st.form_submit_button("Keep it, choose another number", width="stretch")
    if cancel:
        st.session_state.pop(f"{key}_clash", None)
        _rerun()
    if move:
        outcome, info = db.renumber_invoice(detail["ID"], wanted, move_holder_to=move_to)
        if outcome == "move_taken":
            st.error(f"#{int(move_to)} is used too, by {_student(info['Student'])}'s invoice. Try another.")
            return
        st.session_state.pop(f"{key}_clash", None)
        _renumbered(detail, outcome, info)


def _renumbered(detail: dict, outcome: str, info) -> None:
    """Say what a renumbering did, and open the invoice again under its new number."""
    if outcome == "renumbered":
        _forget_made_files()
        moved = info["Moved"]
        _flash(
            f"{_student(info['Student'])}'s invoice #{info['From']} is now #{info['To']}."
            + (f" {_student(moved['Student'])}'s #{moved['From']} is now #{moved['To']}." if moved else "")
            + " If a parent already has the old number, send them the re-made one."
            + f" The next new invoice will be #{db.get_next_invoice_number()['next']}."
        )
        st.session_state.pop(f"invoice_renumber_{detail['ID']}", None)
        st.session_state["issued_search_next"] = str(info["To"])
        _rerun()
    elif outcome == "same":
        st.info("That is already its number.")
    elif outcome == "low":
        st.error(f"Numbers below #{info} are the academy's invoices from before the app.")
    else:
        st.warning("Only a sent invoice can be given another number.")



def _send_copy(detail: dict, key: str) -> None:
    """Offer the one file to send a parent for this invoice.

    Made only when asked, since an image means starting a browser.
    """
    fmt = _send_format()
    if fmt == "html":
        return
    noun = _EXPORT_NOUN[fmt]
    made = st.session_state.get(f"{key}_file")
    if made and made[0] == (detail["ID"], detail["Total"], detail["Number"]):
        st.download_button(
            f"Download the {noun}", data=made[1], file_name=_invoice_filename(detail, fmt),
            mime="image/png" if fmt == "png" else "application/pdf",
            key=f"{key}_dl", width="stretch",
        )
    elif st.button(f"Make the {noun} to send", key=f"{key}_make", width="stretch"):
        render = render_invoices_png if fmt == "png" else render_invoices_pdf
        st.session_state[f"{key}_file"] = ((detail["ID"], detail["Total"], detail["Number"]),
                                           render([detail])[0])
        _rerun()


def _schedule_now_text(line: dict) -> str:
    """Return what the calendar says about a lesson today, to show next to what the invoice said."""
    now = line["Now"]
    if now is None:
        return "class no longer on the schedule"
    hours, rate, name = now
    differences = []
    if line["Charged"] and abs(hours - line["Hours"]) > 0.005:
        differences.append(f"{hours:g}h")
    if rate > db.UNSET_RATE and abs(rate - line["Rate"]) > 0.005:
        differences.append(f"${rate:,.2f}/h")
    if name != line["Subject"]:
        differences.append(f"“{name}”")
    return ", ".join(differences) if differences else "same"


def _invoice_editor(detail: dict) -> None:
    """Correct a sent invoice by hand: it's voided and a replacement is issued.

    A sent invoice never changes by itself; this is the only way it does.
    Someone edits its lessons and lines, and saving voids it (number retired,
    what it said kept) and issues a replacement under a new number that says
    which one it replaces. The credit it settled and any payment recorded
    move across.
    """
    invoice_id = detail["ID"]
    data = db.get_invoice_for_edit(invoice_id)
    if data is None:
        return
    st.caption(
        f"Saving voids #{data['Number']} and issues a replacement under a new number, "
        "which says it replaces it. Send the parent the new one."
    )
    version = st.session_state.get(f"edit_version_{invoice_id}", 0)
    use_now = st.session_state.get(f"edit_use_now_{invoice_id}", False)
    rows = []
    for line in data["Lines"]:
        now = line["Now"] if use_now else None
        hours = line["Hours"] if line["Charged"] else (line["Now"][0] if line["Now"] else 0.0)
        rows.append({
            "Date": line["Date"],
            "Subject": now[2] if now else line["Subject"],
            "Hours": now[0] if now else hours,
            "Price/h": now[1] if now and now[1] > db.UNSET_RATE else line["Rate"],
            "Charge": line["Charged"],
            "Schedule now": _schedule_now_text(line),
        })
    edited = st.data_editor(
        rows,
        column_config={
            "Date": st.column_config.DateColumn(format="D MMM YYYY", width="small"),
            "Subject": st.column_config.TextColumn(width="medium"),
            "Hours": st.column_config.NumberColumn(min_value=0.0, step=0.25, width="small"),
            "Price/h": st.column_config.NumberColumn(min_value=0.0, step=5.0, format="$%.2f", width="small"),
            "Charge": st.column_config.CheckboxColumn(
                width="small", help="Untick to keep the class on the invoice without charging for it."),
            "Schedule now": st.column_config.TextColumn(
                width="medium", help="What the schedule says today, where it differs from the invoice."),
        },
        disabled=["Date", "Schedule now"],
        hide_index=True,
        width="stretch",
        key=f"edit_lines_{invoice_id}_{version}",
    )
    if not use_now and any(row["Schedule now"] not in ("same", "class no longer on the schedule")
                           for row in rows):
        if st.button("Use the schedule's figures", key=f"edit_use_now_btn_{invoice_id}"):
            st.session_state[f"edit_use_now_{invoice_id}"] = True
            st.session_state[f"edit_version_{invoice_id}"] = version + 1
            _rerun()

    add_ids = []
    if data["Waiting"]:
        st.caption(
            f"Not billed yet — {_student(data['Student'])}'s classes these months still waiting for "
            "their next invoice. Tick one to put it on this invoice instead."
        )
        waiting = st.data_editor(
            [{"Add": False, "Date": entry["Date"], "Subject": entry["Subject"],
              "Hours": entry["Hours"], "Price/h": entry["Rate"], "Amount": entry["Amount"]}
             for entry in data["Waiting"]],
            column_config={
                "Add": st.column_config.CheckboxColumn(width="small"),
                "Date": st.column_config.DateColumn(format="D MMM YYYY", width="small"),
                "Price/h": st.column_config.NumberColumn(format="$%.2f"),
                "Amount": st.column_config.NumberColumn(format="$%.2f"),
            },
            disabled=["Date", "Subject", "Hours", "Price/h", "Amount"],
            hide_index=True,
            width="stretch",
            key=f"edit_waiting_{invoice_id}_{version}",
        )
        add_ids = [entry["ID"] for entry, row in zip(data["Waiting"], waiting) if row["Add"]]
    added = [entry for entry in data["Waiting"] if entry["ID"] in add_ids]
    unpriced = [entry for entry in added if entry["Rate"] <= db.UNSET_RATE]

    st.caption(
        "Extra lines — materials, a discount (as a minus amount). Add or delete rows. "
        "Not for a class on the schedule: tick it above, or it is billed again on the next invoice."
    )
    extra = st.data_editor(
        pd.DataFrame(
            [{"Description": e["Description"], "Amount": e["Amount"]} for e in data["Extra"]],
            columns=["Description", "Amount"],
        ).astype({"Description": "string", "Amount": "float"}),
        column_config={
            "Description": st.column_config.TextColumn(width="large"),
            "Amount": st.column_config.NumberColumn(format="$%.2f", step=5.0),
        },
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        key=f"edit_extra_{invoice_id}_{version}",
    )
    extra_rows = [
        {"Description": str(row["Description"]).strip(), "Amount": float(row["Amount"])}
        for row in extra.to_dict("records")
        if isinstance(row.get("Description"), str) and row["Description"].strip()
        and pd.notna(row.get("Amount")) and float(row["Amount"]) != 0
    ]

    missing = [row for row in edited if row["Charge"]
               and (not row["Hours"] or not row["Price/h"] or row["Price/h"] <= db.UNSET_RATE)]
    gross = round(sum(round(row["Hours"] * row["Price/h"], 2) for row in edited
                      if row["Charge"] and row["Hours"] and row["Price/h"])
                  + sum(entry["Amount"] for entry in added)
                  + sum(row["Amount"] for row in extra_rows), 2)
    credit = min(data["Credit taken off"], max(gross, 0.0))
    net = round(gross - credit, 2)
    summary = f"New total {_money(net)}, was {_money(detail['Total'])}"
    if credit:
        summary += f", after {_money(credit)} credit already taken off"
    st.markdown(summary + ".")
    paid = data["Paid"]
    if paid is not None and abs(paid - net) > 0.005:
        st.caption(
            f"{_money(paid)} has been paid: {_money(net - paid)} more will be owed."
            if net > paid else
            f"{_money(paid)} has been paid: the {_money(paid - net)} paid over becomes a credit "
            "on their next invoice."
        )
    note = st.text_input("What changed (kept with the old invoice)", key=f"edit_note_{invoice_id}")
    if unpriced:
        st.warning("A class ticked to add has no price yet — price its subject first.")
    if missing:
        st.warning("A class that is charged needs hours and a price — or untick Charge.")
    elif gross < 0:
        st.warning("That would ask for less than nothing — check the discount.")
    if st.button(f"Save — void #{data['Number']} and issue the replacement", type="primary",
                 key=f"edit_save_{invoice_id}", disabled=bool(missing or unpriced) or gross < 0):
        lines = {
            line["ID"]: {"Subject": row["Subject"], "Hours": row["Hours"] or 0,
                         "Rate": row["Price/h"] or 0, "Charged": bool(row["Charge"])}
            for line, row in zip(data["Lines"], edited)
        }
        status, _, number = db.replace_invoice(invoice_id, lines, extra_rows, note, add=add_ids)
        if status != "replaced":
            st.warning({
                "not_issued": "That invoice has already been replaced — look up the new number.",
                "invalid": "A class that is charged needs hours and a price — or untick Charge.",
                "negative": "That would ask for less than nothing — check the discount.",
            }.get(status, f"Could not save that ({status})."))
            return
        for key in [k for k in st.session_state if k.startswith(("edit_", "invoice_edit_"))]:
            st.session_state.pop(key, None)
        st.session_state["issued_search_next"] = str(number)
        _flash(f"#{data['Number']} is void; replaced by #{number} for {_money(net)}. "
               "Download the new one below and send it.")
        _rerun()


def _replaced_download() -> None:
    """Offer the invoices just replaced in one go, ready to send, one file each."""
    ids = st.session_state.get("invoices_replaced")
    if not ids:
        return
    noun = _EXPORT_NOUN[_send_format()]
    columns = st.columns([3, 1])
    columns[0].markdown(f"**{len(ids)} replacement invoice(s) to send**")
    if columns[1].button("Done", key="invoices_replaced_done", width="stretch"):
        st.session_state.pop("invoices_replaced", None)
        st.session_state.pop("invoices_replaced_zip", None)
        _rerun()
    made = st.session_state.get("invoices_replaced_zip")
    if made and made[0] == tuple(ids):
        st.download_button(
            f"Download {len(ids)} {noun}(s) (.zip)", data=made[1],
            file_name=f"replaced-invoices-{dt.date.today():%Y-%m-%d}.zip", mime="application/zip",
            key="invoices_replaced_dl", type="primary",
        )
    elif st.button(f"Make the {noun}s", key="invoices_replaced_make"):
        with st.spinner(f"Making {len(ids)} {noun}(s)…"):
            st.session_state["invoices_replaced_zip"] = (
                tuple(ids), _invoices_zip(db.get_invoices_detailed(ids), as_images=True))
        _rerun()


def _out_of_step_warning() -> None:
    """Show sent invoices the schedule has changed since, and a way to deal with each.

    A sent invoice only changes when someone edits it, so a price, time or
    subject name changed afterwards would otherwise go unnoticed.
    """
    _replaced_download()
    found = db.get_sent_invoices_out_of_step()
    if not found:
        return
    st.warning(
        f"**{len(found)} sent invoice(s) no longer match the schedule** — a price, class time "
        "or subject name changed after they went out. They stay as sent until you edit them."
    )
    with st.popover(f"Update all {len(found)} to the schedule"):
        st.write(
            f"Voids these {len(found)} invoices and issues replacements with the schedule's "
            "hours, prices and subject names for the classes listed — nothing else on them "
            "changes. Credit and payments move across. You then send each parent their new one."
        )
        if st.button("Update them all", type="primary", key="oos_update_all"):
            with st.spinner(f"Replacing {len(found)} invoice(s)…"):
                done = db.update_sent_invoices_to_schedule([entry["ID"] for entry in found])
            st.session_state["invoices_replaced"] = [new_id for _, new_id, _ in done]
            _flash(f"Replaced {len(done)} invoice(s): "
                   + ", ".join(f"#{old} → #{new}" for old, _, new in done[:8])
                   + (" …" if len(done) > 8 else "") + ". Download them below the list.")
            _rerun()
    with st.expander("Which, and what changed"):
        for entry in found[:40]:
            columns = st.columns([6, 1, 1.4])
            changes = "; ".join(f"{_when(change['Date'].strftime('%d %b'))} {_subject(change['Subject'])}: "
                                + _md(change["What"])
                                for change in entry["Changes"][:3])
            more = len(entry["Changes"]) - 3
            columns[0].markdown(f"**#{entry['Number']}** · {_student(entry['Student'])} — {changes}"
                                + (f" and {more} more" if more > 0 else ""))
            if columns[1].button("Edit", key=f"oos_edit_{entry['ID']}", width="stretch"):
                st.session_state["invoice_reference_open"] = True
                st.session_state["issued_search_next"] = str(entry["Number"])
                st.session_state[f"invoice_edit_{entry['ID']}"] = True
                _rerun()
            if columns[2].button("Keep as sent", key=f"oos_keep_{entry['ID']}", width="stretch"):
                db.keep_invoice_as_sent(entry["ID"])
                _flash(f"#{entry['Number']} kept as sent.")
                _rerun()
        if len(found) > 40:
            st.caption(f"And {len(found) - 40} more — the newest are shown.")


def _unpriced_warning(year: int, month: int) -> None:
    """Catch invoices with no price before they're sent, not after.

    Prices are set on the Teachers screen, but this is where anyone finds out
    a subject has no price, on the way to billing it. So the fix is offered
    here, where the problem shows up.
    """
    unpriced = db.get_unpriced_classes_for_month(year, month)
    if not unpriced:
        return
    lessons = sum(item["Sessions"] for item in unpriced)
    hours = sum(item["Hours"] for item in unpriced)
    st.warning(
        f"**{len(unpriced)} subject(s) have no price for** "
        f"{_when(f'{calendar.month_name[month]} {year}')} — {lessons} class(es) ({hours:,.1f}h) "
        "can't be invoiced until they do."
    )
    _price_all_button(
        db.get_unpriced_subjects(class_ids=[item["Class ID"] for item in unpriced]),
        f"price_month_{year}_{month}",
    )
    if st.checkbox(
        "Or set them one by one",
        key=f"fix_prices_{year}_{month}",
    ):
        _bulk_rate_section(
            unpriced, year, month,
            base=f"unpriced_{year}_{month}",
            heading="",
            show_teacher=len({item["Teacher ID"] for item in unpriced}) > 1,
        )


def _month_download(year: int, month: int) -> None:
    """Offer a month's sent invoices as one zip again, any time, from any browser.

    The download offered after issuing only lasts in the browser that issued
    them, until the next batch: issuing October removed September's, and its
    34 files could then only be made one invoice at a time.
    """
    period = f"{calendar.month_name[month]} {year}"
    issued = db.get_issued_invoices_for_month(year, month)
    st.markdown(f"###### Download the invoices for {_when(period)} ({len(issued)})")
    if not issued:
        st.caption(f"None issued for {_when(period)} yet.")
        return
    days: dict = {}
    for item in issued:
        if item["Issued"]:
            days[item["Issued"]] = days.get(item["Issued"], 0) + 1
    options = ["all"] + sorted(days, reverse=True)
    choice = st.selectbox(
        "Which", options, key=f"month_download_pick_{year}_{month}", label_visibility="collapsed",
        format_func=lambda o: f"All {len(issued)}" if o == "all" else f"Issued {o:%d %b %Y} — {days[o]}",
    )
    chosen = issued if choice == "all" else [item for item in issued if item["Issued"] == choice]
    noun = _send_noun()
    wanted = (year, month, tuple(item["ID"] for item in chosen))
    made = st.session_state.get("month_download_file")
    if made and made[0] == wanted:
        st.download_button(
            f"📱 Download {len(chosen)} {noun}(s) (.zip)", data=made[1], mime="application/zip",
            file_name=f"invoices-{period.replace(' ', '-')}-{len(chosen)}.zip",
            key="month_download_button", width="stretch",
        )
    elif st.button(f"Make the {len(chosen)} {noun}(s)", key="month_download_make", width="stretch"):
        with st.spinner(f"Making {len(chosen)} {noun}(s)…"):
            details = db.get_invoices_detailed([item["ID"] for item in chosen])
            # One batch kept at a time: a month of them can be tens of megabytes.
            st.session_state["month_download_file"] = (wanted, _invoices_zip(details, as_images=True))
        _rerun()


def _past_invoices_section(counts: dict, year: int, month: int) -> None:
    """Show the reference sections, kept out of the way.

    Looking up an old invoice, browsing what's still building up, checking
    outstanding credit: all useful, but none of it is part of "bill this
    month and send it", which is what this screen is for.
    """
    st.divider()
    if not st.checkbox(
        "Past invoices, credits and lookups", key="invoice_reference_open"
    ):
        return

    _month_download(year, month)

    st.markdown(f"###### Still accumulating, any month ({counts['open_count']})")
    open_ones = db.get_invoices(status="Open")
    if not open_ones:
        st.caption("Nothing accumulating right now.")
    else:
        # Each row has a ready-to-download invoice, and a download button's file must be
        # built up front. Every invoice includes the letterhead image, so a few hundred
        # rows is several megabytes and as many widgets, which takes Streamlit a while to
        # show. Hence the search and the limit.
        needle = st.text_input(
            "Find a student", key="open_invoice_search",
            placeholder="Type part of a name",
        ).strip().lower()
        if needle:
            open_ones = [i for i in open_ones if needle in i["Student"].lower()]
        shown_open = open_ones[:OPEN_INVOICE_ROWS]
        if len(open_ones) > len(shown_open):
            st.caption(
                f"Showing {len(shown_open)} of {len(open_ones)} — search above "
                "to narrow it down."
            )

        # Parent and phone for the printable copy's "Billed to" line, loaded once for
        # everyone here instead of per invoice.
        contacts = {
            item["ID"]: (item["Parent"], item["Phone"]) for item in db.get_all_students()
        }
        for invoice in shown_open:
            with st.expander(
                f"{_student(invoice['Student'])} — {invoice['Classes']} class(es), "
                f"{_money(invoice['Total'])}"
            ):
                _invoice_table(invoice)
                # No "bill the whole thing" button. An invoice covers one month, so a draft with
                # several months is billed one month at a time from the list at the top of this
                # screen.
                st.caption("Bill it from the month's list at the top of this screen.")
                with st.columns([1.4, 5])[0]:
                    parent, phone = contacts.get(invoice["Student ID"], ("", ""))
                    detail = {**invoice, "Parent": parent, "Phone": phone}
                    _invoice_download(detail, f"dl_{invoice['ID']}")

    _credits_pointer()
    _issued_lookup_section(counts)


def _send_invoices(invoice_ids: list[int], year: int, month: int) -> None:
    """Bill the ticked students for this month, then make the images to send.

    One step, because that's the whole job: nobody wants an invoice they
    can't send. Billing is saved first, on its own, so if the image maker is
    missing or fails, the invoices are still issued and the panel below
    offers the other formats.
    """
    issued_ids = []
    unpriced_count = 0
    failed_count = 0
    for invoice_id in invoice_ids:
        outcome, new_id = db.issue_invoice_for_month(invoice_id, year, month)
        if outcome == "issued":
            issued_ids.append(new_id)
            st.session_state.pop(f"inv_pick_{invoice_id}", None)
        elif outcome == "unpriced":
            unpriced_count += 1
        else:
            failed_count += 1

    if unpriced_count:
        st.error(
            f"**{unpriced_count} invoice(s) were not sent: they hold a class "
            "with no real price.** Set the prices above and send again — an "
            "invoice is never issued at the placeholder, because the figure "
            "it would ask for is not a price anybody chose."
        )
    if failed_count:
        st.warning(f"{failed_count} invoice(s) could not be issued.")
    if not issued_ids:
        return

    st.session_state["invoices_just_issued"] = {"period": (year, month), "ids": issued_ids}
    st.session_state.pop("invoice_select_all", None)

    details = db.get_invoices_detailed(issued_ids)
    if not details:
        _rerun()
    token = "_".join(str(d["ID"]) for d in details)
    zip_key = f"invoices_zip_png_{token}"
    # A batch of files can be tens of megabytes; only the newest is kept in
    # session_state.
    for stale in [k for k in st.session_state if k.startswith("invoices_zip_png_")]:
        st.session_state.pop(stale, None)

    # About 0.6 seconds per invoice, so a full month takes most of a minute. A
    # progress bar shows it's working, so the page never looks frozen.
    progress = st.progress(0.0, text=f"Rendering {len(details)} {_send_noun()}(s)…")
    try:
        st.session_state[zip_key] = _invoices_zip(
            details, as_images=True,
            on_progress=lambda done, total: progress.progress(
                done / total, text=f"Rendering {_send_noun()} {done} of {total}…"
            ),
        )
    except Exception as problem:  # noqa: BLE001 - shown in the panel below
        st.session_state[f"{zip_key}_error"] = str(problem)
    finally:
        progress.empty()

    # Tick everyone just issued, so the panel below opens with the download ready
    # instead of asking who to include again.
    for detail in details:
        st.session_state[f"ji_pick_{year}_{month}_{detail['ID']}"] = True
    _rerun()


def _next_number_control() -> None:
    """Show the next invoice's number, and a way to choose another.

    For invoices written outside the app, whose numbers the next one here
    mustn't repeat. A number an invoice in the app already has isn't just
    refused: it shows who has it, with the choice of giving their invoice
    another number instead.
    """
    numbering = db.get_next_invoice_number()
    with st.popover(f"Next invoice number: #{numbering['next']}"):
        with st.form("next_number_form", border=False):
            chosen = st.number_input(
                "Next invoice number", min_value=db.INVOICE_NUMBER_START, value=numbering["next"],
                step=1, help=f"Numbering carries on from here. A free number below "
                             f"#{numbering['top']} is used once, then numbering goes back to "
                             f"#{numbering['top']} and up.",
            )
            if st.form_submit_button("Save", type="primary"):
                outcome, detail = db.set_next_invoice_number(chosen)
                if outcome == "set":
                    st.session_state.pop("number_clash", None)
                    _flash(f"The next invoice will be #{detail}.")
                elif outcome == "taken":
                    st.session_state["number_clash"] = {"wanted": int(chosen), "holder": detail}
                else:
                    _flash(f"Numbers below #{detail} are the academy's invoices from before the "
                           "app, which it can't check.", "warning")
                _rerun()
        # Shown in the popover, under the number that clashed: a rerun that leaves the
        # label the same keeps the popover open, and it would cover a warning shown
        # below it on the page.
        _number_clash(numbering)


def _number_clash(numbering: dict) -> None:
    """Show who already has the chosen number, and a way to free it."""
    clash = st.session_state.get("number_clash")
    if not clash:
        return
    wanted, holder = clash["wanted"], clash["holder"]
    issued = f", issued {db.as_date(holder['Issued']):%d %b %Y}" if holder.get("Issued") else ""
    st.warning(
        f"**#{wanted} is already used** by {_student(holder['Student'])}'s invoice"
        f"{' (void)' if holder['Status'] == 'Void' else ''}{issued}."
    )
    with st.form("number_clash_form", border=False):
        move_to = st.number_input(
            f"New number for {_student(holder['Student'])}'s invoice", min_value=db.INVOICE_NUMBER_START,
            value=numbering["top"], step=1,
        )
        move = st.form_submit_button(f"Renumber it and use #{wanted} next", type="primary",
                                     width="stretch")
        cancel = st.form_submit_button("Keep it, choose another number", width="stretch")
    st.caption(
        f"If {_student(holder['Student'])} already has it as #{wanted}, send them the re-made one: "
        "Past invoices → Look one up. It shows the new number."
    )
    if cancel:
        st.session_state.pop("number_clash", None)
        _rerun()
    if move:
        outcome, detail = db.move_invoice_number(wanted, move_to)
        if outcome == "taken":
            st.error(f"#{int(move_to)} is used too, by {_student(detail['Student'])}'s invoice. Try another.")
            return
        if outcome == "low":
            st.error(f"Numbers below #{detail} are the academy's invoices from before the app.")
            return
        st.session_state.pop("number_clash", None)
        if outcome == "moved":
            _forget_made_files()
            _flash(f"{_student(detail['Student'])}'s invoice is now #{detail['To']}. "
                   f"The next invoice will be #{wanted}.")
        else:
            _flash(f"The next invoice will be #{wanted}.")
        _rerun()


def invoices_tab() -> None:

    # The month comes first and everything below is for that month: an invoice is
    # always for a month of lessons, never a running total.
    """Draw the Invoices screen for one month: what to bill, then sent invoices and credits."""
    year, month = _month_picker("invoice_month")
    period = f"{calendar.month_name[month]} {year}"
    summary = db.get_month_invoice_summary(year, month)
    counts = db.get_invoice_counts()

    if not counts["open_count"] and not counts["issued_count"]:
        st.info("No invoices yet — they appear as soon as a schedule is imported.")
        return

    # One line, not four metrics: the only question here is how much there is to
    # send this month.
    st.markdown(
        f"#### {_when(period)} — {summary['to_bill_count']} to bill, "
        f"{_money(summary['to_bill_value'])}"
    )
    trail = f"{summary['billed_count']} already sent, {_money(summary['billed_value'])}"
    if summary.get("credit_outstanding"):
        trail += (
            f" · {_money(summary['credit_outstanding'])} credit for cancelled "
            "classes comes off automatically"
        )
    st.caption(trail)
    _next_number_control()
    _out_of_step_warning()

    month_items = db.get_open_invoice_items_for_month(year, month)
    # Only warn when something is about to go out. A month that's fully billed has
    # nothing riding on its prices, and saying so every time would be clutter.
    if month_items:
        _unpriced_warning(year, month)
        _unnamed_subjects("invoices", year, month)

    if not month_items:
        st.info(f"Nothing left to bill for {_when(period)}.")
    else:
        # A form, as on Payments: ticking is instant, and the button is above the list
        # instead of below every student.
        with st.form(f"invoice_form_{year}_{month}", border=False):
            send = st.form_submit_button(
                "Send ticked invoices", type="primary", key="invoice_bulk_issue", width="stretch",
                help="Bills this month only, then makes one image per student to send on KakaoTalk.",
            )
            every = st.checkbox(f"All {len(month_items)}", key="invoice_select_all")
            ticked = {
                row["Invoice ID"]: st.checkbox(
                    f"{_student(row['Student'])} — {row['Classes']} class(es), {_money(row['Month Amount'])}",
                    key=f"inv_pick_{row['Invoice ID']}",
                )
                for row in month_items
            }
        if send:
            selected_ids = [row["Invoice ID"] for row in month_items
                            if every or ticked.get(row["Invoice ID"])]
            if selected_ids:
                _send_invoices(selected_ids, year, month)
            else:
                st.warning("Tick who to bill first.")

    # Below the month's figures, not above them, and only for the month it was issued
    # for.
    _just_issued_panel(year, month)
    _past_invoices_section(counts, year, month)


# ---------------------------------------------------------------------------
# Payments, then Data
# ---------------------------------------------------------------------------


def _part_payment_form(unpaid: list[dict], year: int, month: int) -> None:
    """Record a part payment: a parent paid less than the invoice asks for.

    Behind a tick instead of on every row: almost every payment is the full
    amount, and an amount box on 80 rows would hide the one button that's
    usually needed.
    """
    if not st.checkbox("Someone paid a different amount", key=f"part_pay_{year}_{month}"):
        return
    by_label = {
        f"{row['Student']} — invoice #{row['Number']} — ${row['Owing']:,.2f} owing": row
        for row in unpaid
    }
    chosen = st.selectbox("Which invoice", options=sorted(by_label), key=f"part_who_{year}_{month}")
    row = by_label[chosen]
    columns = st.columns([1, 1, 2])
    amount = columns[0].number_input(
        "Amount received ($)", min_value=0.0, max_value=float(row["Owing"]),
        value=float(row["Owing"]), step=10.0, key=f"part_amt_{year}_{month}",
    )
    when = columns[1].date_input("Date received", value=dt.date.today(), key=f"part_on_{year}_{month}")
    note = columns[2].text_input(
        "Note", placeholder="e.g. paid $200 now, rest next month",
        key=f"part_note_{year}_{month}",
    )
    if st.button("Record this payment", key=f"part_save_{year}_{month}", type="primary"):
        outcome = db.mark_invoice_paid(row["ID"], paid_on=when, amount=amount, note=note)
        if outcome in ("paid", "part paid"):
            short = row["Owing"] - amount
            message = f"Recorded {_money(amount)} from {_student(row['Student'])}."
            if short > 0.005:
                message += (
                    f" {_money(short)} still owing — they stay on the list until "
                    "it comes in."
                )
            _flash(message)
            _rerun()
        else:
            st.warning(f"Could not record that ({outcome}).")


def _paid_list(paid: list[dict], year: int, month: int) -> None:
    """Show who has already paid, with an undo for a wrong tick."""
    st.markdown(f"###### Received ({len(paid)})")
    if not paid:
        st.caption("Nobody yet.")
        return
    needle = ""
    if len(paid) > 12:
        needle = st.text_input(
            "Find a student", key=f"paid_find_{year}_{month}",
            placeholder="Type part of a name",
        ).strip().lower()
    shown = [row for row in paid if not needle or needle in row["Student"].lower()]
    if needle and not shown:
        st.caption("Nobody by that name has paid this month.")
        return
    for row in shown[:PAID_ROWS]:
        columns = st.columns([5, 1])
        if row["Paid on"] is None:
            # Nothing was ever owed: credit for missed lessons brought the invoice to $0, so
            # there's no payment to show or undo.
            columns[0].markdown(
                f"{_student(row['Student'])} — nothing to pay: credit for missed classes covered it"
            )
            continue
        amount = row["Paid amount"] if row["Paid amount"] is not None else row["Total"]
        line = (
            f"{_student(row['Student'])} — {_money(amount)} on "
            f"{_when(row['Paid on'].strftime('%d %b %Y'))}"
        )
        if row["Note"]:
            line += f" · _{row['Note']}_"
        columns[0].markdown(line)
        if columns[1].button("Undo", key=f"unpay_{row['ID']}", width="stretch"):
            db.unmark_invoice_paid(row["ID"])
            _flash(f"{_student(row['Student'])} put back to unpaid.", "warning")
            _rerun()
    if len(shown) > PAID_ROWS:
        st.caption(f"Showing {PAID_ROWS} of {len(shown)} — search above to narrow it down.")


def payments_tab() -> None:
    """Draw the Payments screen for one month: who still owes, then who has paid."""
    year, month = _month_picker("payments_month")
    period = f"{calendar.month_name[month]} {year}"
    data = db.get_invoice_payments(year, month)
    unpaid, paid = data["unpaid"], data["paid"]

    if not unpaid and not paid:
        st.info(
            f"No invoices covering {_when(period)} yet. They appear here once "
            "you have sent them from the Invoices screen."
        )
        return

    outstanding = round(sum(row["Owing"] for row in unpaid), 2)
    collected = round(
        sum(r["Paid amount"] if r["Paid amount"] is not None else r["Total"] for r in paid), 2
    )
    st.markdown(f"#### {_when(period)} — {len(unpaid)} to collect, {_money(outstanding)}")
    trail = f"{len(paid)} paid, {_money(collected)} · due {_when(data['due'].strftime('%d %b %Y'))}"
    late = [row for row in unpaid if row["Overdue"] > 0]
    if late:
        trail += f" · {_flag(f'{len(late)} overdue')}"
    st.caption(trail)

    if not unpaid:
        st.success(f"Everyone has paid for {_when(period)}.")
    else:
        # A form, so ticking is instant: outside a form every tick reran the whole screen,
        # two seconds each on the live app, with the button below every parent.
        search = st.text_input(
            "Find", placeholder="Search by student or invoice number",
            key=f"pay_find_{year}_{month}", label_visibility="collapsed",
        ).strip().casefold()
        shown = [row for row in unpaid
                 if not search or search in row["Student"].casefold() or search in str(row["Number"])]
        with st.form(f"pay_form_{year}_{month}", border=False):
            columns = st.columns([1, 1.2, 1.2])
            when = columns[0].date_input("Date received", value=dt.date.today())
            note = columns[1].text_input("Note", placeholder="optional, e.g. bank transfer")
            columns[2].markdown("<div style='height: 1.75rem'></div>", unsafe_allow_html=True)
            submitted = columns[2].form_submit_button(
                "Mark ticked as paid", type="primary", width="stretch", key=f"pay_confirm_{year}_{month}",
            )
            every = shown and st.checkbox(f"All {len(shown)} below" if search else f"All {len(shown)}")
            ticks = {}
            for row in shown:
                bits = [f"{_student(row['Student'])} — {_money(row['Owing'])}"]
                if row["Owing"] < row["Total"] - 0.005:
                    bits.append(f"part paid, {_money(row['Total'])} invoice")
                bits.append(f"#{row['Number']}")
                if row.get("Covers"):
                    # Billed all at once, not month by month, so the amount next to their name isn't
                    # just this month's lessons.
                    bits.append(f"covers {_when(row['Covers'])}")
                if row["Overdue"]:
                    bits.append(_flag(f"{row['Overdue']} days overdue"))
                ticks[row["ID"]] = st.checkbox(" · ".join(bits), key=f"pay_pick_{row['ID']}")
            if not shown:
                st.caption("Nobody matches that search.")
        if submitted:
            picked = [row for row in shown if every or ticks.get(row["ID"])]
            if not picked:
                st.warning("Tick who has paid first.")
            else:
                done = failed = 0
                for row in picked:
                    if db.mark_invoice_paid(row["ID"], paid_on=when, note=note) in ("paid", "part paid"):
                        done += 1
                    else:
                        failed += 1
                if failed:
                    st.warning(f"{failed} could not be recorded.")
                if done:
                    total_picked = round(sum(row["Owing"] for row in picked), 2)
                    st.session_state.pop(f"pay_find_{year}_{month}", None)
                    _flash(f"Recorded {done} payment(s) — {_money(total_picked)} for {_when(period)}.")
                    _rerun()

        _part_payment_form(unpaid, year, month)

    st.divider()
    _paid_list(paid, year, month)


def _level_axis() -> alt.Axis:
    """Return an axis for months, or other short labels, written level.

    Vega-Lite turns a category axis's labels on their side, so "Jan" to "Sep"
    read sideways.
    """
    return alt.Axis(labelAngle=0)


def _history_view() -> None:
    """Draw the Past schedules view: what's saved, and an upload to add more."""
    st.caption("For the charts only — never billed. Adding a workbook again updates it.")
    summary = db.get_lesson_history_summary()
    if summary:
        st.dataframe(summary, width="stretch", hide_index=True)

    st.markdown("#### Add a teacher's workbook")
    upload = st.file_uploader("Excel schedule (.xlsx)", type=["xlsx"], key="history_file")
    if upload is None:
        return
    stem = Path(upload.name).stem
    cut = stem.lower().find("schedule")
    columns = st.columns([2, 1])
    teacher = columns[0].text_input(
        "Teacher",
        value=(stem[:cut] if cut > 0 else stem).strip(" -_"),
        key=f"history_teacher_{upload.file_id}",
    )
    year = columns[1].number_input(
        "Year, for sheets that name none",
        min_value=2015,
        max_value=dt.date.today().year + 1,
        value=dt.date.today().year,
        step=1,
        key="history_year",
    )
    file_bytes = upload.getvalue()
    try:
        sheets = schedule_parser.get_sheet_names(file_bytes)
    except Exception as error:  # a damaged or non-Excel file
        st.error(f"Could not read that workbook: {error}")
        return
    chosen = st.multiselect(
        "Worksheets", sheets, default=sheets, key=f"history_sheets_{upload.file_id}"
    )
    fingerprint = (upload.file_id, tuple(sorted(chosen)), int(year))
    if st.button("Read workbook", key="history_parse", disabled=not chosen):
        with st.spinner("Reading the workbook…"):
            st.session_state["history_preview"] = (
                fingerprint,
                schedule_parser.parse_workbook(file_bytes, chosen, int(year)),
            )
    stored = st.session_state.get("history_preview")
    if not stored or stored[0] != fingerprint:
        return
    preview = stored[1]
    sessions = preview["sessions"]
    if not sessions:
        st.warning("No lessons found in those worksheets.")
        return

    dates = sorted(item["date"] for item in sessions)
    st.markdown(
        f"**{len(sessions):,} lessons**, "
        f"{sum(len(item['attendance']) for item in sessions):,} student-lessons, "
        f"{preview['unique_student_count']} students, "
        f"{_when(dates[0].strftime('%b %Y'))} to {_when(dates[-1].strftime('%b %Y'))}."
    )
    if preview["sheet_summaries"]:
        st.dataframe(preview["sheet_summaries"], width="stretch", hide_index=True)
    if preview["warning_count"]:
        with st.expander(f"{preview['warning_count']} warning(s) — worth a look"):
            texts = [item["text"] for item in preview["warnings"]] + [
                warning["text"] for item in sessions for warning in item["warnings"]
            ]
            st.text("\n".join(texts[:300]))
    if st.button(
        "Add to past schedules", type="primary", key="history_save",
        disabled=not teacher.strip(),
    ):
        result = db.save_lesson_history(
            teacher, sessions, source=upload.name, dates=preview.get("dates_shown")
        )
        st.session_state.pop("history_preview", None)
        _flash(
            f"Past schedules for {_teacher(result['teacher'])}: {result['added']:,} student-lessons "
            f"added, {result['updated']:,} updated, {result['unchanged']:,} already on file"
            + (f", {result['removed']:,} no longer in the workbook removed" if result["removed"] else "")
            + ". Nothing was billed."
        )
        st.rerun()


_COMPARE_MAX = 8


def _name_list(names: list[str], most: int = 6) -> str:
    """Return "A, B and C", or "A, B, C and 4 more" past `most`."""
    names = list(names)
    if len(names) > most:
        return ", ".join(names[:most]) + f" and {len(names) - most} more"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _compare_picker(key: str, names: list[str], ranked: list[str]) -> list[str]:
    """Pick up to eight teachers to chart side by side, starting with the top of `ranked`.

    Picks made for another year or month can name a teacher not in this
    list, which the widget refuses, so only the ones it can show are kept.
    Streamlit also forgets a widget's value on any run where it isn't drawn
    (a year with nothing recorded), so a copy is kept to restore it.
    """
    kept = st.session_state.get(key, st.session_state.get(f"_{key}_kept"))
    st.session_state[key] = (
        [name for name in kept if name in names] if kept is not None else ranked[:_COMPARE_MAX]
    )
    picked = st.multiselect(
        "Compare teachers",
        options=names,
        key=key,
        max_selections=_COMPARE_MAX,
        placeholder="Pick teachers to compare",
        help=f"Up to {_COMPARE_MAX} at a time, so each keeps its own colour.",
    )
    st.session_state[f"_{key}_kept"] = picked
    return picked


@st.cache_data(show_spinner="Reading every lesson…", max_entries=4)
def _teaching_activity(version: tuple, today: dt.date) -> dict:
    """Return students, lessons and hours by month, for one set of lessons on record."""
    sources = db.get_analysis_lessons(today)
    lessons, _ = teaching.combine(sources["app"], sources["history"])
    return teaching.monthly_activity(lessons)


_TEACHING_MEASURES = {"Students": "Students taught", "Hours": "Teaching hours", "Lessons": "Lessons"}


def _teaching_view() -> None:
    """Show what the academy taught, month by month and teacher by teacher.

    The Invoiced view only shows money billed through the app, and most
    teachers joined the app in September 2026; before that it's two
    teachers' takings. The lessons themselves are on record for everyone,
    from the Schedule and from past schedules, so the number of students
    and hours taught is known for every month.
    """
    st.caption("Every lesson taught, from the Schedule and Past schedules. Money is under Invoiced.")
    year, month = _month_picker("data_month")
    today = dt.date.today()
    label = f"{calendar.month_name[month]} {year}"
    activity = _teaching_activity(db.get_analysis_data_version(), today)
    months = [row for row in activity["by_month"] if row["Year"] == year and row["Month"] <= month]
    if not months:
        st.info(f"No lessons on record in {year} up to the end of {_when(label)}.")
        return

    # The chosen month against the one before, unless the month isn't over yet:
    # half a month next to a whole one tells you nothing.
    so_far = (year, month) == (today.year, today.month)
    before = (year, month - 1) if month > 1 else (year - 1, 12)
    by_month = {(row["Year"], row["Month"]): row for row in activity["by_month"]}
    current, previous = by_month.get((year, month)), by_month.get(before)
    # Teachers' records start at different times (four of the biggest only in January
    # 2026), so a month where some start can't be compared with the one before: the
    # jump is new records, not new students.
    first_on_record: dict[str, tuple[int, int]] = {}
    for row in activity["by_teacher"]:
        key = (row["Year"], row["Month"])
        if row["Teacher"] not in first_on_record or key < first_on_record[row["Teacher"]]:
            first_on_record[row["Teacher"]] = key
    beginning = sorted(t for t, first in first_on_record.items() if first == (year, month))
    st.markdown(f"#### {label}" + (f" — up to {today:%d %b}" if so_far else ""))
    if current is None:
        st.caption(f"No lessons on record in {_when(label)}.")
    else:
        columns = st.columns(4)
        for column, key, text in zip(columns, ["Students", "Lessons", "Hours", "Teachers"],
                                     ["Students taught", "Lessons", "Teaching hours", "Teachers"]):
            value = current[key]
            delta = None
            if previous and not so_far and not beginning:
                change = value - previous[key]
                delta = f"{change:+,.0f} on {calendar.month_abbr[before[1]]}"
            column.metric(text, f"{value:,.0f}", delta=delta)
        if beginning and previous and not so_far:
            st.caption(
                f"Not compared with {_when(calendar.month_abbr[before[1]])}: records for "
                + _chips("teacher", beginning) + f" begin in {_when(calendar.month_abbr[month])}."
            )

    order = [calendar.month_abbr[m] for m in range(1, month + 1)]
    frame = pd.DataFrame(months)
    frame["Month name"] = frame["Month"].map(lambda m: calendar.month_abbr[m])
    st.markdown(f"#### Students taught each month — {year}")
    st.altair_chart(
        alt.Chart(frame)
        .mark_bar(color="#2a78d6")
        .encode(
            x=alt.X("Month name:N", sort=order, title=None, axis=_level_axis()),
            y=alt.Y("Students:Q", title="Students"),
            tooltip=[alt.Tooltip("Month name:N", title="Month"), "Students:Q", "Lessons:Q",
                     alt.Tooltip("Hours:Q", title="Teaching hours", format=",.1f"), "Teachers:Q"],
        )
        .properties(height=240),
        width="stretch",
    )
    starts = sorted(
        ((first[1], t) for t, first in first_on_record.items()
         if first[0] == year and 1 < first[1] <= month),
    )
    later = sorted(t for t, first in first_on_record.items() if first > (year, month))
    st.caption(
        "Absences aren't counted; a child with two teachers counts once. "
        + ("Records start later for "
           + _name_list([f"{_teacher(t)} ({calendar.month_abbr[m]})" for m, t in starts])
           + ". " if starts else "")
        + (f"Not on record yet: {_chips('teacher', later)}." if later else "")
    )

    teachers = pd.DataFrame(
        [row for row in activity["by_teacher"] if row["Year"] == year and row["Month"] <= month]
    )
    teachers["Month name"] = teachers["Month"].map(lambda m: calendar.month_abbr[m])
    measure = st.radio("Compare by", list(_TEACHING_MEASURES), horizontal=True, key="teach_measure")
    this_month = teachers[teachers["Month"] == month].sort_values(measure, ascending=False)
    ranked = list(this_month["Teacher"]) or list(
        teachers.groupby("Teacher")[measure].sum().sort_values(ascending=False).index)
    names = sorted(teachers["Teacher"].unique(), key=str.casefold)
    picked = _compare_picker("teach_compare", names, ranked)
    if picked:
        st.altair_chart(
            alt.Chart(teachers[teachers["Teacher"].isin(picked)])
            .mark_line(point=True)
            .encode(
                x=alt.X("Month name:N", sort=order, title=None, axis=_level_axis()),
                y=alt.Y(f"{measure}:Q", title=_TEACHING_MEASURES[measure]),
                color=alt.Color("Teacher:N", sort=picked, legend=alt.Legend(
                    title=None, orient="bottom", columns=4, labelLimit=0)),
                tooltip=["Teacher:N", alt.Tooltip("Month name:N", title="Month"), "Students:Q",
                         "Lessons:Q", alt.Tooltip("Hours:Q", title="Teaching hours", format=",.1f")],
            )
            .properties(height=260),
            width="stretch",
        )

    if not this_month.empty:
        last = teachers[(teachers["Year"] == before[0]) & (teachers["Month"] == before[1])] \
            if before[0] == year else pd.DataFrame(
                [row for row in activity["by_teacher"] if (row["Year"], row["Month"]) == before])
        last_students = dict(zip(last["Teacher"], last["Students"])) if not last.empty else {}
        st.markdown(f"#### Each teacher in {label}")
        st.dataframe(
            [
                {
                    "Teacher": row.Teacher,
                    "Students": int(row.Students),
                    "Lessons": int(row.Lessons),
                    "Teaching hours": round(float(row.Hours), 1),
                    f"Students in {calendar.month_abbr[before[1]]}": last_students.get(row.Teacher, 0),
                }
                for row in this_month.sort_values("Students", ascending=False).itertuples()
            ],
            width="stretch",
            hide_index=True,
        )


def _per_hour(invoiced: float, hours: float) -> str:
    """Return money per hour as text, or a dash if there are no hours."""
    return f"${invoiced / hours:,.0f}" if hours else "—"


def _lessons(count: int) -> str:
    """Return "1 lesson" or "N lessons"."""
    return f"{count} lesson{'' if count == 1 else 's'}"


def data_tab() -> None:
    """Draw the Data screen: the Teaching, Invoiced or Past schedules view."""
    view = st.radio(
        "View",
        ["Teaching", "Invoiced", "Past schedules"],
        horizontal=True,
        key="data_view",
        label_visibility="collapsed",
    )
    if view == "Teaching":
        _teaching_view()
        return
    if view == "Past schedules":
        _history_view()
        return
    st.caption("Invoiced: invoices sent. Still to invoice: taught or booked, at today's prices.")

    year, month = _month_picker("data_month")
    label = f"{calendar.month_name[month]} {year}"

    trend = db.get_teacher_year_trend(year, month)
    if not trend:
        st.info(f"No classes recorded in {year} up to the end of {_when(label)}.")
        return

    frame = pd.DataFrame(trend)
    order = [calendar.month_abbr[m] for m in range(1, month + 1)]

    # The whole academy first: one bar per month, whoever taught it.
    st.markdown(f"#### Invoiced by month — January to {calendar.month_name[month]} {year}")
    totals = frame.groupby("Month name", as_index=False)[["Invoiced", "Not invoiced", "Hours"]].sum()
    stacked = totals.melt(id_vars=["Month name", "Hours"], value_vars=["Invoiced", "Not invoiced"],
                          var_name="Kind", value_name="Amount")
    stacked["Kind"] = stacked["Kind"].map({"Invoiced": "Invoiced", "Not invoiced": "Still to invoice"})
    st.altair_chart(
        alt.Chart(stacked)
        .mark_bar()
        .encode(
            x=alt.X("Month name:N", sort=order, title=None, axis=_level_axis()),
            y=alt.Y("Amount:Q", stack=True, title="All teachers", axis=alt.Axis(format="$,.0f")),
            color=alt.Color("Kind:N", scale=alt.Scale(
                domain=["Invoiced", "Still to invoice"], range=["#2a78d6", "#a9c8ee"]),
                legend=alt.Legend(title=None, orient="top")),
            order=alt.Order("Kind:N", sort="ascending"),
            tooltip=[alt.Tooltip("Month name:N", title="Month"),
                     alt.Tooltip("Kind:N", title=" "),
                     alt.Tooltip("Amount:Q", format="$,.2f"),
                     alt.Tooltip("Hours:Q", title="Hours taught")],
        )
        .properties(height=240),
        width="stretch",
    )
    # Teachers joined the app at different times (most in September 2026), and a
    # month before that only has the few billed through it then. Without a note, the
    # jump looks like the academy's takings quadrupled.
    active = frame[(frame["Invoiced"] + frame["Not invoiced"]) > 0]
    per_month = active.groupby("Month")["Teacher"].nunique()
    if len(per_month) > 1:
        full = int(per_month.max())
        first_full = int(per_month[per_month == full].index.min())
        earlier = sorted(set(active.loc[active["Month"] < first_full, "Teacher"]), key=str.casefold)
        if earlier and len(earlier) * 2 <= full:
            who = f"{len(earlier)} teachers" if len(earlier) > 3 else _chips("teacher", earlier)
            st.caption(
                f"Before {_when(calendar.month_name[first_full])} only {who} "
                f"{'was' if len(earlier) == 1 else 'were'} billed through the app — see Teaching "
                "for every teacher's months."
            )

    # Then teachers side by side. A line for each of two dozen teachers was a tangle:
    # ten colours, a key twice the chart's height and cut off, most lines flat at $0.
    # So only the chosen teachers are drawn, eight at most, which is as many colours
    # as stay easy to tell apart. A teacher's month is what was invoiced plus what's
    # still to invoice; invoices alone showed every teacher not yet billed through
    # the app as flat at $0, whatever they taught.
    frame["Month total"] = frame["Invoiced"] + frame["Not invoiced"]
    by_teacher = frame.groupby("Teacher")["Month total"].sum().sort_values(ascending=False)
    names = sorted(by_teacher.index, key=str.casefold)
    earning = [name for name, total in by_teacher.items() if total > 0]
    picked = _compare_picker("data_compare", names, earning)
    if picked:
        st.altair_chart(
            alt.Chart(frame[frame["Teacher"].isin(picked)])
            .mark_line(point=True)
            .encode(
                x=alt.X("Month name:N", sort=order, title=None, axis=_level_axis()),
                y=alt.Y("Month total:Q", title="Invoiced + still to invoice",
                        axis=alt.Axis(format="$,.0f")),
                color=alt.Color("Teacher:N", sort=picked, legend=alt.Legend(
                    title=None, orient="bottom", columns=4, labelLimit=0)),
                tooltip=[
                    alt.Tooltip("Teacher:N"),
                    alt.Tooltip("Month name:N", title="Month"),
                    alt.Tooltip("Invoiced:Q", format="$,.2f"),
                    alt.Tooltip("Not invoiced:Q", title="Still to invoice", format="$,.2f"),
                    alt.Tooltip("Hours:Q", title="Hours taught"),
                ],
            )
            .properties(height=260),
            width="stretch",
        )

    st.divider()
    # This and the trend use the same function to price lessons, so the figures below
    # always match the last point on the chart above.
    stats = db.get_teacher_month_stats(year, month)
    if not stats:
        st.info(f"Nobody taught in {_when(label)}.")
        return

    snapshot = pd.DataFrame(stats)
    waiting = int(snapshot["Not invoiced lessons"].sum())

    st.markdown(f"#### {label}")
    columns = st.columns(4)
    columns[0].metric("Invoiced", f"${snapshot['Invoiced'].sum():,.2f}")
    columns[1].metric(
        "Hours taught", f"{snapshot['Hours'].sum():,.2f}",
        help="Lessons held so far this month; ones still to come are not counted yet.",
    )
    columns[2].metric(
        "Per teaching hour",
        _per_hour(snapshot["Invoiced"].sum(), snapshot["Invoiced lesson hours"].sum()),
        help="Invoiced ÷ hours of those lessons. A class of six earns six rates an hour.",
    )
    # Invoiced money only changes when invoices go out, so an import or a new price
    # changed nothing on this screen. What's still to come, at today's prices, shows
    # both.
    unpriced = int(snapshot["Not invoiced unpriced"].sum())
    columns[3].metric(
        "Still to invoice", f"${snapshot['Not invoiced value'].sum():,.2f}",
        help=f"{_lessons(waiting)} with a student not yet invoiced, at today's prices."
             + (f" {_lessons(unpriced)} have no price yet and count as nothing." if unpriced else ""),
    )
    behind = sorted((row for row in stats if row["Not invoiced lessons"]),
                    key=lambda r: -r["Not invoiced value"])
    if behind:
        shown = ", ".join(_teacher(row["Teacher"]) + " " + _money("$" + format(row["Not invoiced value"], ",.0f"))
                          for row in behind[:6])
        st.caption(
            f"Still to invoice: {shown}"
            + (f", and {len(behind) - 6} more" if len(behind) > 6 else "") + "."
            + (f" {_lessons(unpriced)} have no price yet — price them under Teachers."
               if unpriced else "")
        )

    # Bars, longest first, not a pie: names read the same with two teachers or forty,
    # where pie slices turn into slivers with a colour key to look up. The chart
    # gets taller with more teachers so nothing is squeezed.
    st.markdown("##### Invoiced")
    invoiced = snapshot[snapshot["Invoiced"] > 0]
    st.altair_chart(
        alt.Chart(invoiced if len(invoiced) else snapshot)
        .mark_bar(color="#2a78d6")
        .encode(
            y=alt.Y("Teacher:N", sort="-x", title=None, axis=alt.Axis(labelLimit=0)),
            x=alt.X("Invoiced:Q", title=None, axis=alt.Axis(format="$,.0f")),
            tooltip=[
                alt.Tooltip("Teacher:N"),
                alt.Tooltip("Invoiced:Q", format="$,.2f"),
                alt.Tooltip("Hours:Q", title="Hours taught"),
                alt.Tooltip("Not invoiced lessons:Q", title="Lessons not invoiced yet"),
                alt.Tooltip("Students:Q"),
            ],
        )
        .properties(height=max(120, 26 * max(len(invoiced), 1))),
        width="stretch",
    )

    st.dataframe(
        [
            {
                "Teacher": row["Teacher"],
                "Invoiced": f"${row['Invoiced']:,.2f}",
                "Still to invoice": (
                    f"${row['Not invoiced value']:,.2f} · {_lessons(row['Not invoiced lessons'])}"
                    + (f", {row['Not invoiced unpriced']} unpriced" if row["Not invoiced unpriced"] else "")
                    if row["Not invoiced lessons"] else "—"),
                "Hours taught": row["Hours"],
                "Per teaching hour": _per_hour(row["Invoiced"], row["Invoiced lesson hours"]),
                "Unique students": row["Students"],
            }
            for row in sorted(stats, key=lambda r: r["Invoiced"], reverse=True)
        ],
        width="stretch",
        hide_index=True,
    )


# ---------------------------------------------------------------------------
# Create/Edit schedule, then Reminders
# ---------------------------------------------------------------------------


# The marks a teacher writes beside a student, each a column of the table.
_PLAN_MARKS = (schedule_parser.ONLINE, schedule_parser.RECORDING, schedule_parser.CANCELLED)
_PLAN_BLANK = {"Date": None, "Start": None, "End": None, "Subject": [], "Students": [],
               **{mark: [] for mark in _PLAN_MARKS}, "Every week": False}


def _rows_from_lessons(lessons: list) -> list:
    """Turn lessons, from the app or a sheet, into the table's rows: one per class, marks included."""
    return [
        {"Date": lesson["date"], "Start": lesson["start"], "End": lesson["end"], "Subject": [lesson["subject"]],
         "Students": list(lesson["students"]),
         **{mark: [name for name in lesson["students"] if lesson["statuses"].get(name) == mark]
            for mark in _PLAN_MARKS},
         "Every week": False}
        for lesson in sorted(lessons, key=lambda lesson: (lesson["date"], lesson["start"]))
    ]


def _subjects_in(value) -> list:
    """Return the names in a Subject cell as a list (a pick-list cell; usually one name)."""
    if isinstance(value, str):
        value = [value]
    return [name.strip() for name in value or [] if isinstance(name, str) and name.strip()]


def _as_date(value):
    """Return a table cell as a date, or None if it's empty."""
    if value is None or value == "" or (isinstance(value, float) and value != value):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def _as_clock(value):
    """Return a table cell as a time, or None if it's empty."""
    if value is None or value == "" or (isinstance(value, float) and value != value):
        return None
    if isinstance(value, dt.datetime):
        return value.time()
    if isinstance(value, dt.time):
        return value
    return dt.time.fromisoformat(str(value)[:8])


def _planned_lessons(rows: list, year: int, month: int) -> tuple[list, int, int, int]:
    """Turn the table's rows into lessons, repeating each weekly one to the month's end.

    Returns (lessons, rows not filled in yet, rows outside the month, rows
    given more than one subject); the last are used with their first subject.
    Each lesson carries its students' marks, and in "wrong" any mark that
    names someone not in the class or a second mark for one student.
    """
    last = dt.date(year, month, calendar.monthrange(year, month)[1])
    lessons, unfinished, elsewhere, doubled = [], 0, 0, 0
    for row in rows:
        when, start, end = _as_date(row.get("Date")), _as_clock(row.get("Start")), _as_clock(row.get("End"))
        subjects = _subjects_in(row.get("Subject"))
        subject = " ".join(subjects[0].split()) if subjects else ""
        doubled += len(subjects) > 1
        if not (when and start and end and subject):
            unfinished += any([when, start, end, subject, row.get("Students")])
            continue
        if (when.year, when.month) != (year, month):
            elsewhere += 1
            continue
        students = list(dict.fromkeys(" ".join(name.split()) for name in row.get("Students") or []
                                      if name and name.strip()))
        statuses, wrong = {}, []
        for mark in _PLAN_MARKS:
            for name in (" ".join(str(name).split()) for name in row.get(mark) or []):
                if not name:
                    continue
                if name not in students:
                    wrong.append(f"{name} is marked {mark} but isn't in the class")
                elif name in statuses:
                    wrong.append(f"{name} has two marks")
                else:
                    statuses[name] = mark
        day = when
        while day <= last:
            lessons.append({"date": day, "start": start, "end": end, "subject": subject,
                            "students": list(students), "statuses": dict(statuses),
                            "wrong": wrong if day == when else []})
            if not row.get("Every week"):
                break
            day += dt.timedelta(weeks=1)
    lessons.sort(key=lambda lesson: (lesson["date"], lesson["start"]))
    return lessons, unfinished, elsewhere, doubled


def _plan_problems(lessons: list) -> list[str]:
    """Return what would make the sheet wrong: a class ending before it starts, two at once, or a bad mark."""
    problems = []
    for lesson in lessons:
        if lesson["end"] <= lesson["start"]:
            problems.append(f"{lesson['date']:%a %d %b} {lesson['subject']}: ends before it starts.")
        for line in lesson.get("wrong") or []:
            problems.append(f"{lesson['date']:%a %d %b} {lesson['subject']}: {line}.")
    by_day: dict = {}
    for lesson in lessons:
        by_day.setdefault(lesson["date"], []).append(lesson)
    for day, items in by_day.items():
        for index, first in enumerate(items):
            for second in items[index + 1:]:
                if second["start"] < first["end"] and second["end"] > first["start"]:
                    problems.append(f"{day:%a %d %b}: {first['subject']} and {second['subject']} overlap "
                                    "— a teacher can only take one class at a time.")
    return problems


def _student_finder(students: list):
    """Return a lookup from a name to a student id that matches names the way the upload does.

    A name matches if it's on file, is a known spelling of a student, or is
    the same once tidied. So "Dami Ryu", the way one teacher writes Ryu Dami,
    isn't called new (the upload knows that spelling), and that student's
    other classes are still checked for clashes.
    """
    exact = {item["Name"]: item["ID"] for item in students}
    known = db.get_student_aliases()
    cleaned = {schedule_parser.normalise_name(name): sid for name, sid in exact.items()}
    return lambda name: (exact.get(name) or known.get(name.casefold())
                         or cleaned.get(schedule_parser.normalise_name(name)))


def _plan_clashes(lessons: list, teacher_name: str, find, year: int, month: int) -> list[str]:
    """Return students booked into another teacher's class at the same time, from what's in the app.

    One query for the month. The teacher's own classes in the app are left
    out: this table replaces them once it's saved or uploaded.
    """
    ids = {find(name) for lesson in lessons for name in lesson["students"]} - {None}
    first = dt.date(year, month, 1)
    booked = [item for item in db.get_student_bookings(ids, first, dt.date(year, month,
                                                       calendar.monthrange(year, month)[1]))
              if item["Teacher"].casefold() != teacher_name.casefold()]
    lines = []
    for lesson in lessons:
        here = {find(name) for name in lesson["students"]}
        for item in booked:
            if (item["Student ID"] in here and db.as_date(item["Date"]) == lesson["date"]
                    and db.as_time(item["Start"]) < lesson["end"] and db.as_time(item["End"]) > lesson["start"]):
                lines.append(f"{_student(item['Student'])} · {_when(lesson['date'].strftime('%a %d %b'))} "
                             f"{_subject(lesson['subject'])} — also in "
                             f"{_subject(item['Class'])} with {_teacher(item['Teacher'])}, "
                             + _when(f"{db.as_time(item['Start']):%H:%M}–{db.as_time(item['End']):%H:%M}"))
    return lines


def _rows_from_sheet(upload, year: int, month: int) -> list | None:
    """Turn a sheet's month into table rows (a sheet downloaded before, or a teacher's own), marks included."""
    data = upload.getvalue()

    def month_of(name):
        """Return the month a sheet's name gives, or None."""
        try:
            return schedule_parser.infer_month(name)
        except ValueError:
            return None

    chosen = next((name for name in schedule_parser.get_sheet_names(data) if month_of(name) == month), None)
    if chosen is None:
        return None
    preview = schedule_parser.parse_workbook(data, [chosen], year)
    return _rows_from_lessons([
        {"date": item["date"], "start": item["start_time"], "end": item["end_time"], "subject": item["class_name"],
         "students": [entry["student_name"] for entry in item["attendance"]],
         "statuses": {entry["student_name"]: entry["status"] for entry in item["attendance"]
                      if entry["status"] in _PLAN_MARKS}}
        for item in preview["sessions"] if (item["date"].year, item["date"].month) == (year, month)
    ]) or None


def create_schedule_tab() -> None:
    """Draw the Create/Edit schedule screen: plan or edit one teacher's month, save it, download it.

    A month the app already has is loaded into the table to edit. Saving is
    the same as uploading that month's sheet on Teachers: the app's month is
    made to match the table, with credits for anything already invoiced that
    changes, and new teachers, students and subjects created. The download
    is the teacher's sheet for Google Drive, marks included.
    """
    st.caption("Plan a teacher's month, or edit one the app has. Save it to the app, and download "
               "it as their sheet.")
    teachers = [item for item in db.get_all_teachers() if item["Active"]]
    labels = {item["Name"]: item["ID"] for item in teachers}
    # Widgets on a screen that isn't drawn lose their values, so going to another
    # screen and back reset the month and teacher while the table still held the
    # planned month. Their values are kept alongside and put back. They're set
    # through session_state, not the widgets' defaults, since Streamlit won't take both.
    ahead = dt.date.today().replace(day=1) + dt.timedelta(days=32)
    for key, default in (("plan_year", ahead.year), ("plan_month", ahead.month),
                         ("plan_teacher_mode", "Existing teacher" if teachers else "New teacher"),
                         ("plan_teacher", next(iter(labels), None)), ("plan_new_teacher", "")):
        if key not in st.session_state:
            kept = st.session_state.get(f"plan_kept_{key}", default)
            if key != "plan_teacher" or kept in labels:
                st.session_state[key] = kept
    columns = st.columns(2)
    year = int(columns[0].number_input("Year", min_value=2020, max_value=dt.date.today().year + 1,
                                       step=1, key="plan_year"))
    month = int(columns[1].selectbox("Month", options=list(range(1, 13)),
                                     format_func=lambda value: calendar.month_name[value], key="plan_month"))
    period = f"{calendar.month_name[month]} {year}"
    mode = st.radio("Teacher", ["Existing teacher", "New teacher"], horizontal=True,
                    key="plan_teacher_mode", label_visibility="collapsed")
    teacher_id = None
    if mode == "New teacher" or not teachers:
        teacher_name = st.text_input("Teacher name", key="plan_new_teacher").strip()
    else:
        teacher_name = st.selectbox("Teacher", list(labels), key="plan_teacher")
        teacher_id = labels[teacher_name]
    for key in ("plan_year", "plan_month", "plan_teacher_mode", "plan_teacher", "plan_new_teacher"):
        if key in st.session_state:
            st.session_state[f"plan_kept_{key}"] = st.session_state[key]

    # The month as the app has it, loaded here to edit. The table remembers which teacher
    # and month it was loaded from, so it can't be saved over another one.
    here = (teacher_id, year, month)
    source = st.session_state.get("plan_source")
    on_file_lessons = schedule_backfill.lessons_on_file(teacher_id, year, month)
    if on_file_lessons and source != here:
        st.info(f"{_teacher(teacher_name)} has {_plural(len(on_file_lessons), 'class', 'classes')} in "
                f"{_when(period)} in the app.")
        if st.button("Edit them here", key="plan_edit"):
            st.session_state["plan_rows"] = _rows_from_lessons(on_file_lessons)
            st.session_state["plan_version"] = st.session_state.get("plan_version", 0) + 1
            st.session_state["plan_source"] = here
            _rerun()

    students = db.get_all_students()
    lookup = {item["Name"]: item["ID"] for item in students}
    # The teacher's subjects on file, then any typed into the table, so a new subject
    # typed once is offered on the next row, spelled the same.
    subjects: dict = {}
    on_file = [item["Class"] for item in db.get_teacher_classes_for_schedule(
        teacher_id, dt.date(year, month, 1))] if teacher_id else []
    for name in sorted(on_file, key=str.casefold) + [name for row in st.session_state.get("plan_rows") or []
                                                     for name in _subjects_in(row.get("Subject"))]:
        subjects.setdefault(name.casefold(), name)

    with st.expander("Start from a sheet"):
        upload = st.file_uploader(f"A schedule sheet with {calendar.month_name[month]} in it (.xlsx)",
                                  type=["xlsx"], key="plan_upload")
        if upload is not None and st.button("Put its lessons in the table", key="plan_load"):
            rows = _rows_from_sheet(upload, year, month)
            if rows is None:
                st.warning(f"No lessons for {_when(period)} found in that sheet.")
            else:
                st.session_state["plan_rows"] = rows
                st.session_state["plan_version"] = st.session_state.get("plan_version", 0) + 1
                st.session_state.pop("plan_source", None)
                _flash(f"{len(rows)} lessons put in the table.")
                _rerun()

    # The table keeps its rows across screens: a widget's own state is lost when its
    # screen isn't drawn, so the rows are kept alongside and a new table is started
    # from them on the way back.
    version = st.session_state.get("plan_version", 0)
    table_key = f"plan_table_{version}"
    if table_key not in st.session_state:
        st.session_state["plan_base"] = st.session_state.get("plan_rows") or [dict(_PLAN_BLANK)]
    first_day = dt.date(year, month, 1)
    last_day = dt.date(year, month, calendar.monthrange(year, month)[1])
    # A mark can name anyone in the table, including a new student typed into a class.
    in_table = {name for row in st.session_state["plan_base"] for name in row.get("Students") or [] if name}
    mark_options = sorted(set(lookup) | in_table, key=str.casefold)
    st.markdown(f"**Lessons in {period}**")
    edited = st.data_editor(
        st.session_state["plan_base"],
        num_rows="dynamic",
        key=table_key,
        width="stretch",
        hide_index=True,
        column_config={
            "Date": st.column_config.DateColumn(min_value=first_day, max_value=last_day,
                                                format="ddd D MMM", required=True),
            "Start": st.column_config.TimeColumn(format="h:mm a", step=dt.timedelta(minutes=15)),
            "End": st.column_config.TimeColumn(format="h:mm a", step=dt.timedelta(minutes=15)),
            "Subject": st.column_config.MultiselectColumn(
                options=list(subjects.values()), accept_new_options=True, width="medium",
                help="Pick one of the teacher's subjects, or type a new one. One per lesson."),
            "Students": st.column_config.MultiselectColumn(
                options=sorted(lookup, key=str.casefold), accept_new_options=True, width="large",
                help="Pick students on file, or type a new name."),
            **{mark: st.column_config.MultiselectColumn(
                options=mark_options, width="medium",
                help=f"Students in this class who are {mark.lower()}, as the teacher marks them on the sheet.")
               for mark in _PLAN_MARKS},
            "Every week": st.column_config.CheckboxColumn(
                help="Repeats on the same weekday to the end of the month."),
        },
    )
    st.session_state["plan_rows"] = edited

    lessons, unfinished, elsewhere, doubled = _planned_lessons(edited, year, month)
    if unfinished:
        st.caption(f"{unfinished} row(s) not filled in yet (date, times and subject) — left out.")
    if elsewhere:
        st.warning(f"{elsewhere} row(s) fall outside {_when(period)} — left out.")
    problems = _plan_problems(lessons)
    if doubled:
        problems.insert(0, f"{doubled} lesson(s) have more than one subject — one per lesson.")
    for problem in problems:
        st.error(problem)
    find = _student_finder(students)
    new_names = sorted({name for lesson in lessons for name in lesson["students"] if find(name) is None},
                       key=str.casefold)
    if new_names:
        st.info("New students, created when saved: " + _chips("student", new_names, most=99))
    # A subject typed instead of picked is a new one. Say so, as for students, with
    # the one on file it's close to: "G12 Bio" picked by pressing Enter half-typed
    # would otherwise quietly become a second subject.
    known = {name.casefold() for name in on_file}
    new_subjects = sorted({lesson["subject"] for lesson in lessons if lesson["subject"].casefold() not in known},
                          key=str.casefold)
    if new_subjects:
        st.info("New subjects, created when saved: " + ", ".join(
            _subject(name) + (f" (close to {_subject(close[0])})" if (close := difflib.get_close_matches(name, on_file, n=1,
                                                                                     cutoff=0.7)) else "")
            for name in new_subjects))
    clashes = _plan_clashes(lessons, teacher_name, find, year, month) if lessons and teacher_name else []
    if clashes:
        st.warning("Booked into another class at the same time:  \n" + "  \n".join(clashes[:12])
                   + (f"  \n…and {len(clashes) - 12} more" if len(clashes) > 12 else ""))

    # The sheet is read back the way the upload will read it, so anything that
    # wouldn't survive is fixed here, not found at upload.
    data = schedule_writer.write_month(lessons, year, month) if lessons and teacher_name and not problems else b""
    if data:
        for line in schedule_writer.readback_problems(lessons, data, year, month):
            problems.append(line)
            st.error(line)
    if lessons:
        st.caption(f"{len(lessons)} lessons on {len({lesson['date'] for lesson in lessons})} days, "
                   f"{len({name for lesson in lessons for name in lesson['students']})} students.")
    ready = bool(data) and not problems
    _save_planned_month(lessons, ready, teacher_id, teacher_name, year, month, on_file_lessons, find)
    st.download_button(
        f"Download {teacher_name or 'the'} sheet for {period} (.xlsx)",
        data=data if ready else b"",
        file_name=f"{teacher_name} Schedule {calendar.month_abbr[month]} {year}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary", disabled=not ready, key="plan_download",
        help=None if ready else "Add a teacher and at least one complete lesson, without overlaps.",
    )
    st.caption(
        "Upload it to Google Drive and open it with Google Sheets; the worksheet is named "
        f"{calendar.month_abbr[month]}, ready to copy into the teacher's yearly sheet. Teachers write "
        "Online, Recording or Cancelled, then the names, in the column to the right of a class. To bring "
        "it into the app, download it from Google Sheets as .xlsx and upload it on Teachers."
    )
    if st.button("Clear the table", key="plan_clear"):
        st.session_state["plan_rows"] = [dict(_PLAN_BLANK)]
        st.session_state["plan_version"] = version + 1
        st.session_state.pop("plan_source", None)
        _rerun()


def _save_planned_month(lessons, ready, teacher_id, teacher_name, year, month, on_file_lessons, find) -> None:
    """Show what saving the table changes in the app, and save it.

    Saving is the same as uploading the month's sheet: the app's month is
    made to match the table. So a month the app already has must be loaded
    into the table first; otherwise a few planned classes would take the
    rest of the month off. A table loaded for one teacher and month can't be
    saved as another.
    """
    period = f"{calendar.month_name[month]} {year}"
    here, source = (teacher_id, year, month), st.session_state.get("plan_source")
    st.markdown("**Save to the app**")
    blocked = ""
    if source is not None and source != here:
        blocked = "This table was loaded for another teacher or month. Load this one, or clear the table."
    elif on_file_lessons and source != here:
        blocked = f"The app already has {period} for this teacher: press Edit them here first."
    elif not ready:
        blocked = "Add a teacher and at least one complete lesson, without overlaps."
    if blocked:
        st.caption(blocked)
        st.button("Save to the app", key="plan_save", type="primary", disabled=True)
        return

    preview = schedule_backfill.preview_from_lessons(lessons, year, month)
    # The students the screen calls on file are saved as those students, spellings included.
    matches = {name.casefold(): sid for name in {name for lesson in lessons for name in lesson["students"]}
               if (sid := find(name))}
    # Another teacher's subject with the same name gets this teacher's name added, as at upload.
    renames = schedule_backfill.suggest_class_renames(preview["sessions"], teacher_name)
    if renames:
        st.info("Another teacher has the same subject name, so it's saved as: " + ", ".join(
            f"{_subject(old)} → {_subject(new)}" for old, new in renames.items()))
    _import_changes(preview, {}, teacher_id, renames, matches)
    if not st.button("Save to the app", key="plan_save", type="primary"):
        return

    commit_id = teacher_id
    if commit_id is None:
        if not db.create_teacher(teacher_name):
            st.warning("That teacher is already on file: pick them as an existing teacher.")
            return
        commit_id = next(item["ID"] for item in db.get_all_teachers()
                         if item["Name"].casefold() == teacher_name.casefold())
    with st.spinner(f"Saving {period} for {teacher_name}…"):
        result = schedule_backfill.backfill(preview, commit_id, name_overrides=renames, student_matches=matches,
                                            dates=preview["dates_shown"])
    if result["status"] != "imported":
        st.warning(f"Nothing saved ({result['status']}).")
        return
    counts = result["created"]
    message = (f"Saved {_when(period)} for {_teacher(teacher_name)}: {counts.get('sessions_created', 0)} new, "
               f"{counts.get('sessions_updated', 0)} changed, {counts.get('lessons_removed', 0)} removed, "
               f"{counts.get('sessions_unchanged', 0)} unchanged.")
    if counts.get("credits_raised"):
        message += f" {_plural(counts['credits_raised'], 'credit')} for classes already invoiced."
    _flash(message)
    # The table now shows the month as saved, and a new teacher is picked as an existing one.
    st.session_state["plan_rows"] = _rows_from_lessons(schedule_backfill.lessons_on_file(commit_id, year, month))
    st.session_state["plan_version"] = st.session_state.get("plan_version", 0) + 1
    st.session_state["plan_source"] = (commit_id, year, month)
    if teacher_id is None:
        st.session_state["plan_kept_plan_teacher_mode"] = "Existing teacher"
        st.session_state["plan_kept_plan_teacher"] = next(
            item["Name"] for item in db.get_all_teachers() if item["ID"] == commit_id)
        for key in ("plan_teacher_mode", "plan_teacher"):
            st.session_state.pop(key, None)
    st.session_state["offer_billing"] = (year, month)
    _rerun()


def reminders_tab() -> None:
    """Draw the Reminders screen: invoices that fell due this month, and older unpaid ones."""
    st.caption("Due on the 20th of the following month. Record payments on Payments.")

    today = dt.date.today()
    all_reminders = db.get_payment_reminders(today)
    if not all_reminders:
        st.success("Nothing overdue — every issued invoice has been paid.")
        return

    def _due_this_month(item: dict) -> bool:
        """Return True if the invoice fell due this month."""
        return item["Due"].year == today.year and item["Due"].month == today.month

    reminders = [item for item in all_reminders if _due_this_month(item)]
    older = [item for item in all_reminders if not _due_this_month(item)]
    if older:
        owed = sum(item["Amount"] for item in older)
        st.warning(f"Also unpaid from earlier months: {len(older)} invoice(s), {_money(owed)}.")
    if not reminders:
        st.success("Nothing newly overdue this month.")
        show = older
        st.markdown("###### Everything still outstanding")
    else:
        show = reminders
        strip = st.columns(3)
        strip[0].metric("Invoices due", len(show))
        strip[1].metric("Owed", f"${sum(i['Amount'] for i in show):,.2f}")
        longest = max(i["Days overdue"] for i in show)
        strip[2].metric(
            "Longest overdue", f"{longest} day{'' if longest == 1 else 's'}"
        )

    st.dataframe(
        _coloured([
            {
                "Student": item["Student"],
                "Invoice": f"#{item['Number']}" if item["Number"] else "",
                "Amount": f"${item['Amount']:,.2f}",
                "Month": item["Month"].strftime("%b %Y"),
                "Classes": item["Classes"],
                "Subjects": item["Subjects"],
                "Overdue since": item["Due"],
                "Days": item["Days overdue"],
            }
            for item in show
        ], {"Days": "flag"}),
        width="stretch",
        hide_index=True,
    )


# ---------------------------------------------------------------------------
# Delete records
# ---------------------------------------------------------------------------


def _plural(count: int, word: str, words: str = "") -> str:
    """Return "1 class" or "3 classes": the count with the word to match."""
    return f"{count:,} {word if count == 1 else (words or word + 's')}"


def _number_runs(numbers: list[int], most: int = 8) -> str:
    """Return invoice numbers as runs: "#9001–#9008, #9010", and "+40 more" past `most` runs."""
    runs = []
    for number in sorted(numbers):
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    text = ", ".join(f"#{first}" if first == last else f"#{first}–#{last}" for first, last in runs[:most])
    rest = sum(last - first + 1 for first, last in runs[most:])
    return text + (f" +{rest} more" if rest else "")


# Delete records draws each pick in a box with a coloured left edge, in the colour
# its badges use: violet teachers, blue students, orange subjects, grey classes,
# green invoices, red for what reaches students who weren't picked.
_CARD_KINDS = {"Teacher": "teacher", "Student": "student", "Subject": "subject", "Class": "when",
               "Invoice": "money", "Past schedules": "when"}
_CARD_STYLE = "<style>" + " ".join(
    f"[class*='st-key-card-{kind}'] {{ border-left: 0.4rem solid {colour} !important; }}"
    for kind, (colour, _) in _CELL_COLOURS.items()
) + "</style>"


# A picked name in each Delete records box shows in the colours its badges use
# everywhere else, so a teacher looks like a teacher and an invoice like money.
_PICK_KINDS = {"delete_numbers": "money", "delete_teachers": "teacher", "delete_subjects": "subject",
               "delete_classes": "when", "delete_students": "student", "delete_invoices": "money",
               "delete_history": "teacher"}
_PICK_STYLE = "<style>" + " ".join(
    f"[class*='st-key-{key}'] span:has(> span[title]) {{ background-color: {_CELL_COLOURS[kind][1]} !important; }} "
    f"[class*='st-key-{key}'] span:has(> span[title]) * {{ color: {_CELL_COLOURS[kind][0]} !important; "
    f"fill: {_CELL_COLOURS[kind][0]} !important; }}"
    for key, kind in _PICK_KINDS.items()
) + "</style>"


def _invoice_row(bill: dict) -> dict:
    """Return one invoice as a table row for Delete records' detail."""
    return {
        "Invoice": f"#{bill['Number']}" if bill["Number"] is not None else "Draft",
        "Student": bill["Student"],
        "Status": {"Issued": "Sent", "Void": "Void"}.get(bill["Status"], "Draft"),
        "Issued": bill["Issued"],
        "Classes": bill["Classes"],
        "Amount": f"${bill['Amount']:,.2f}" if bill["Amount"] is not None else "",
        "Paid": f"${bill['Paid']:,.2f}" if bill["Paid"] else "",
    }


def _detail_table(label: str, rows: list, kinds: dict | None = None) -> None:
    """Show rows in an expander, closed until clicked."""
    if rows:
        with st.expander(label):
            st.dataframe(_coloured(rows, kinds), width="stretch", hide_index=True)


def _linked_detail(item: dict) -> None:
    """Show the full detail behind one pick on Delete records, each part in its own expander."""
    names = {"Students": "student", "Paid": "money"}
    if item["Kind"] in ("Teacher", "Subject"):
        _detail_table(f"Show {_plural(len(item['Detail']), 'class', 'classes')}", item["Detail"], names)
    elif item["Kind"] == "Student":
        _detail_table(f"Show {_plural(len(item['Places']), 'class', 'classes')}", item["Places"])
        _detail_table(f"Show {_plural(len(item['Invoices']), 'invoice')}",
                      [_invoice_row(bill) for bill in item["Invoices"]], names)
        _detail_table(f"Show {_plural(len(item['Shortened']), 'changed invoice')}", [
            {**_invoice_row(bill), "Classes lost": bill["Classes"], "Amount lost": f"${bill['Removed']:,.2f}"}
            for bill in item["Shortened"]], {"Amount lost": "money", "Paid": "money"})
    elif item["Kind"] == "Invoice":
        _detail_table(f"Show {_plural(len(item['Lines']), 'line')}", [
            {**line, "Amount": f"${line['Amount']:,.2f}" if line["Amount"] is not None else ""}
            for line in item["Lines"]])


def _beyond_detail(beyond: dict) -> None:
    """Show everything a delete reaches beyond the picks, in full, each kind in an expander."""
    _detail_table(f"Show {_plural(len(beyond['Places']), 'student')}", [
        {"Student": row["Student"], "Classes lost": row["Classes"], "None left": "Yes" if row["None left"] else ""}
        for row in beyond["Places"]])
    _detail_table(f"Show {_plural(len(beyond['Changed']), 'changed invoice')}", [
        {**_invoice_row(row), "Classes lost": row["Classes"], "Amount lost": f"${row['Removed']:,.2f}"}
        for row in beyond["Changed"]], {"Amount lost": "money", "Paid": "money"})
    _detail_table(f"Show {_plural(len(beyond['Emptied']), 'deleted invoice')}",
                  [_invoice_row(row) for row in beyond["Emptied"]], {"Paid": "money"})
    _detail_table(f"Show {_plural(len(beyond['Credits']), 'credit')}", [
        {"Student": row["Student"], "Amount": f"${row['Amount']:,.2f}"} for row in beyond["Credits"]])


def _escaped(text) -> str:
    """Return text with anything markdown would treat as syntax escaped, for a name with no badge."""
    return _MARKDOWN_SYNTAX.sub(r"\\\1", str(text))


def _day(value: dt.date) -> str:
    """Return a date as "3 Dec 2026"."""
    return f"{value.day} {value:%b %Y}"


def _days(first: dt.date, last: dt.date) -> str:
    """Return a run of dates as a badge, as short as it reads: "2 Dec 2026", "2–9 Dec 2026"."""
    if first == last:
        text = _day(first)
    elif (first.year, first.month) == (last.year, last.month):
        text = f"{first.day}–{_day(last)}"
    elif first.year == last.year:
        text = f"{first.day} {first:%b} – {_day(last)}"
    else:
        text = f"{_day(first)} – {_day(last)}"
    return _when(text)


def _invoice_text(bill: dict) -> str:
    """Return one invoice in a few words: "#9001 sent $390.00 (paid)", "#8000 void" or "draft"."""
    if bill["Number"] is None:
        return "draft"
    if bill["Status"] != "Issued":
        return f"#{bill['Number']} {'void' if bill['Status'] == 'Void' else 'draft'}"
    text = f"#{bill['Number']} sent {_money(bill['Amount'] or 0)}"
    if bill["Paid"]:
        text += " (paid)" if abs(bill["Paid"] - (bill["Amount"] or 0)) < 0.005 else f" (paid {_money(bill['Paid'])})"
    return text


def _linked_text(item: dict, period: str = "") -> str:
    """Return one thing picked on Delete records, and what it's linked to, as a short list.

    A teacher, subject or student picked for a period says so, since only its
    records in that time go and the record itself stays.
    """
    kind = item["Kind"]
    scope = " · whole record" if item.get("Whole", True) else f" · only {_when(period)}"
    if kind == "Teacher":
        lines = [f"**Teacher** {_teacher(item['Name'])}{scope}"]
        lines += [f"- {_subject(row['Name'])} · {_plural(row['Classes'], 'class', 'classes')}"
                  + (f" · {_days(row['From'], row['To'])}" if row["From"] else "") for row in item["Subjects"]]
        if not item["Subjects"]:
            lines.append("- No subjects")
        if item["Students"]:
            lines.append(f"- Students: {_chips('student', item['Students'], most=10)}")
        if item["Past schedules"]:
            lines.append(f"- {item['Past schedules']:,} past student-lessons")
    elif kind == "Subject":
        lines = [f"**Subject** {_subject(item['Name'])} · {_teacher(item['Teacher'])}{scope}",
                 f"- {_plural(item['Classes'], 'class', 'classes')}"
                 + (f" · {_days(item['From'], item['To'])}" if item["From"] else "")]
        if item["Students"]:
            lines.append(f"- Students: {_chips('student', item['Students'], most=10)}")
    elif kind == "Class":
        when = f"{item['Date']:%a} {_day(item['Date'])}, {item['Start']:%H:%M}"
        lines = [f"**Class** {_when(when)} · {_subject(item['Subject'])} · {_teacher(item['Teacher'])}",
                 f"- Students: {_chips('student', item['Students'], most=10)}" if item["Students"] else "- Empty"]
    elif kind == "Student":
        if item["Only"]:
            # Taken out of some classes only: say which, since the student stays.
            scope = f" · only from {_chips('subject', item['Only'])}" + (
                f", {_when(period)}" if period != "all time" else "")
        lines = [f"**Student** {_student(item['Name'])}{scope}"]
        if item["Parent"]:
            lines.append(f"- Parent: {_escaped(item['Parent'])}")
        if item["Teachers"]:
            lines.append(f"- Teachers: {_chips('teacher', item['Teachers'])}")
        if item["Invoices"]:
            lines.append("- Invoices deleted: " + ", ".join(_invoice_text(bill) for bill in item["Invoices"]))
        for bill in item["Shortened"]:
            lines.append(f"- {_invoice_text(bill)} loses {_plural(bill['Classes'], 'class', 'classes')} "
                         f"· {_money(bill['Removed'])}")
        if item["Credits"]:
            lines.append(f"- Credit: {_money(item['Credits'])}")
    elif kind == "Invoice":
        lines = [f"**Invoice** {_invoice_text(item)} · {_student(item['Student'])}"]
    else:
        lines = [f"**Past schedules** {_teacher(item['Name'])} · {item['Rows']:,} student-lessons"]
    return "\n".join(lines)


def _beyond_text(beyond: dict) -> str:
    """Return what a delete does to students who weren't picked, or "" if nothing.

    One line per student, with everything that happens to them. Past
    BEYOND_ROWS students it's counts instead, since a real teacher picked by
    mistake reaches hundreds.
    """
    hits: dict[str, list[str]] = {}
    for row in beyond["Places"]:
        hits.setdefault(row["Student"], []).append(
            f"removed from {_plural(row['Classes'], 'class', 'classes')}" + (" (none left)" if row["None left"] else ""))
    for row in beyond["Changed"]:
        hits.setdefault(row["Student"], []).append(
            f"#{row['Number']} loses {_plural(row['Classes'], 'class', 'classes')} · {_money(row['Removed'])}")
    for row in beyond["Emptied"]:
        name = f"#{row['Number']}" if row["Number"] is not None else "draft"
        hits.setdefault(row["Student"], []).append(
            f"{name} deleted" + (f" · {_money(row['Amount'] or 0)}" if row["Status"] == "Issued" else ""))
    for row in beyond["Credits"]:
        hits.setdefault(row["Student"], []).append(f"{_money(row['Amount'])} credit gone")
    if not hits:
        return ""
    names = sorted(hits, key=str.lower)
    if len(names) <= BEYOND_ROWS:
        lines = [f"- {_student(name)}: " + "; ".join(hits[name]) for name in names]
    else:
        lines = [f"- {_plural(len(names), 'student')}: {_chips('student', names, most=10)}"]
        if beyond["Changed"]:
            lines.append(f"- {_plural(len(beyond['Changed']), 'sent invoice')} lose classes "
                         f"· {_money(sum(row['Removed'] for row in beyond['Changed']))}")
        if beyond["Emptied"]:
            lines.append(f"- {_plural(len(beyond['Emptied']), 'invoice')} deleted")
        if beyond["Credits"]:
            lines.append(f"- {_money(sum(row['Amount'] for row in beyond['Credits']))} credit gone")
    return "**Not picked, but affected.** Check these are test records:\n" + "\n".join(lines)


def _effects_text(plan: dict) -> str:
    """Return what changes once a delete is done, as a short list."""
    effects = plan["Effects"]
    lines = []
    if effects["Sent"]:
        paid = f" · {_money(effects['Paid amount'])} paid" if effects["Paid amount"] else ""
        lines.append(f"- {_plural(len(effects['Sent']), 'sent invoice')} deleted: {_number_runs(effects['Sent'])} "
                     f"· {_money(effects['Sent amount'])}{paid}")
    if effects["Void"]:
        lines.append(f"- {_plural(effects['Void'], 'void version')} deleted")
    if effects["Drafts"]:
        lines.append(f"- {_plural(effects['Drafts'], 'draft')} deleted")
    if plan["Next after"] != plan["Next before"]:
        lines.append(f"- Next invoice no.: **#{plan['Next before']}** → **#{plan['Next after']}**")
    elif plan["Numbers"]:
        lines.append(f"- Freed: {_number_runs(plan['Numbers'])} (next stays **#{plan['Next before']}**)")
    if plan["Reopened"]:
        lines.append(f"- {_plural(plan['Reopened'], 'credit')} back to waiting")
    if plan["Unpaid"]:
        lines.append(f"- {_plural(plan['Unpaid'], 'class', 'classes')} unbilled again")
    if effects["Parents"]:
        lines.append(f"- Parents deleted: {', '.join(_escaped(name) for name in effects['Parents'])}")
    if effects["Comes back"]:
        lines.append("- Re-uploading their workbook brings them back")
    return "\n".join(lines) or "- Nothing else"


def _pick(label: str, options: dict, key: str, placeholder: str = "Choose options") -> list:
    """Draw one picker on Delete records and return what's picked.

    Picks no longer on offer are dropped first: a teacher unpicked takes their
    subjects with them, and a different month offers different classes.
    """
    if key in st.session_state:
        st.session_state[key] = [value for value in st.session_state[key] if value in options]
    return st.multiselect(label, list(options), format_func=options.get, key=key, placeholder=placeholder)


def _invoice_label(item: dict) -> str:
    """Return how an invoice is listed on Delete records: "#9001 · sent", "#8000 · void", "Draft"."""
    if item["Number"] is None:
        return "Draft (not sent)"
    return f"#{item['Number']} · {'sent' if item['Status'] == 'Issued' else item['Status'].lower()}"


def delete_records_tab() -> None:
    """Draw the Delete records screen: remove anything, with everything linked to it.

    The app's one place to delete. Records are picked down the two trees they
    are kept in: a teacher, then their subjects, then those subjects'
    classes; a student, then their invoices. An invoice can also be picked
    straight by its number, from any time. The deepest pick on each branch
    is what goes, with everything under it. A year, a month of any year, or
    both keep the delete inside that time: a teacher picked for December
    2026 loses only their December 2026 classes and stays on file. With any
    year and any month the pick goes whole. Sent invoices go too, and their
    numbers can be used again. Everything that will go is listed first, and
    the confirmation word must be typed.
    """
    st.caption("Pick down either side. The last thing you pick loses its records in the time above; "
               "with any year and any month, it's deleted outright. Sent invoices too. Can't be undone.")
    # Bumped after a delete, so the boxes start empty again.
    generation = st.session_state.get("delete_gen", 0)
    # The cards' coloured edges, and picked names in each box in their badge colours.
    st.markdown(_CARD_STYLE + _PICK_STYLE, unsafe_allow_html=True)

    when = st.columns(2)
    today = dt.date.today()
    years = [0] + list(range(today.year + 1, 2019, -1))
    year = when[0].selectbox("Year", years, index=years.index(today.year),
                             format_func=lambda value: str(value) if value else "Any year",
                             key=f"delete_year_{generation}")
    month = when[1].selectbox("Month", [0] + list(range(1, 13)), index=today.month,
                              format_func=lambda value: calendar.month_name[value] if value else "Any month",
                              key=f"delete_month_{generation}")
    whole = not year and not month
    choices = db.get_delete_choices(year or None, month or None)
    if year and month:
        period = f"{calendar.month_name[month]} {year}"
    elif month:
        period = f"{calendar.month_name[month]}, any year"
    else:
        period = str(year) if year else "all time"

    # A shortcut past both trees: any invoice of any time, found by typing its number.
    lookup = db.get_invoice_lookup()
    numbers = _pick("Invoice numbers (any time)",
                    {row["ID"]: f"{_invoice_label(row)} · {row['Student']}" for row in lookup},
                    f"delete_numbers_{generation}", placeholder="Know the number? Type it, e.g. 9001")

    teacher_names = {row["ID"]: row["Name"] for row in choices["Teachers"]}
    teacher_side, student_side = st.columns(2)
    with teacher_side:
        teachers = _pick(f"Teachers ({period})", teacher_names, f"delete_teachers_{generation}")
        subjects, lessons = [], []
        if teachers:
            subjects = _pick(
                "Their subjects",
                {row["ID"]: row["Name"] + ("" if len(teachers) == 1 else f" ({teacher_names[row['Teacher ID']]})")
                 for row in choices["Subjects"] if row["Teacher ID"] in teachers},
                f"delete_subjects_{generation}",
                placeholder="None: the whole teacher goes" if whole else f"None: all their classes in {period}")
        if subjects:
            lessons = _pick(
                "Those subjects' classes",
                {row["ID"]: f"{row['Date']:%a %d %b} · {row['Start']:%H:%M}"
                 + ("" if len(subjects) == 1 else f" · {row['Subject']}")
                 for row in choices["Classes"] if row["Subject ID"] in subjects},
                f"delete_classes_{generation}",
                placeholder="None: the whole subject goes" if whole else f"None: all its classes in {period}")
    with student_side:
        students = _pick(f"Students ({period})", {row["ID"]: row["Name"] for row in choices["Students"]},
                         f"delete_students_{generation}")
        invoices = []
        if students:
            names = {row["ID"]: row["Name"] for row in choices["Students"]}
            invoices = _pick(
                "Their invoices",
                {row["ID"]: _invoice_label(row) + ("" if len(students) == 1 else f" · {names[row['Student ID']]}")
                 for row in choices["Invoices"] if row["Student ID"] in students},
                f"delete_invoices_{generation}",
                placeholder="None: the whole student goes" if whole else f"None: all their records in {period}")
    with st.expander("Past schedules (Teaching figures only)"):
        history = _pick(f"Teachers' past schedules ({period})", {name: name for name in choices["History"]},
                        f"delete_history_{generation}")

    # The deepest pick on each branch is what goes.
    teacher_of = {row["ID"]: row["Teacher ID"] for row in choices["Subjects"]}
    subject_of = {row["ID"]: row["Subject ID"] for row in choices["Classes"]}
    student_of = {row["ID"]: row["Student ID"] for row in lookup}
    picked = {
        "teacher_ids": [key for key in teachers if key not in {teacher_of[s] for s in subjects}],
        "class_ids": [key for key in subjects if key not in {subject_of[c] for c in lessons}],
        "lesson_ids": lessons,
        "student_ids": [key for key in students if key not in {student_of[i] for i in invoices + numbers}],
        "invoice_ids": list(dict.fromkeys(invoices + numbers)),
        "history_names": history,
    }
    if not any(picked.values()):
        return
    # A student picked with a teacher, subject or class can mean "this student, only there":
    # a month of one subject they can't make, say, with their other subjects left alone.
    meet = False
    if picked["student_ids"] and (picked["teacher_ids"] or picked["class_ids"] or picked["lesson_ids"]):
        meet = st.checkbox("Only take these students out of these classes", key=f"delete_meet_{generation}",
                           help="Their other subjects stay, and so do the classes and the students themselves.")
    picked.update(year=year or None, month=month or None, meet=meet)

    plan = db.describe_master_delete(**picked)
    total = sum(row["How many"] for row in plan["Rows"])
    # The verdict first, so it can be read at a glance: green when only the picks are touched.
    beyond = _beyond_text(plan["Beyond"])
    if beyond:
        st.error(f"**Affects students you didn't pick** (red box) · {_plural(total, 'record')} to delete")
    else:
        st.success(f"**Only your picks are affected** · {_plural(total, 'record')} to delete")

    for index, item in enumerate(plan["Linked"]):
        with st.container(border=True, key=f"card-{_CARD_KINDS[item['Kind']]}-{index}"):
            st.markdown(_linked_text(item, period))
            _linked_detail(item)
    if beyond:
        with st.container(border=True, key="card-flag-beyond"):
            st.markdown(beyond)
            _beyond_detail(plan["Beyond"])
    with st.container(border=True, key="card-plain-effects"):
        st.markdown("**What happens**\n" + _effects_text(plan))
        _detail_table(f"Show {_plural(len(plan['Effects']['Sent list']), 'sent invoice')}",
                      [_invoice_row(bill) for bill in plan["Effects"]["Sent list"]], {"Paid": "money"})
        _detail_table(f"Show all {_plural(total, 'record')}", plan["Rows"])

    typed = st.text_input(f"Type **{DELETE_PHRASE}** to confirm", key=f"delete_confirm_{generation}",
                          placeholder=DELETE_PHRASE)
    if st.button("Delete all", key=f"delete_go_{generation}", type="primary",
                 disabled=typed.strip().lower() != DELETE_PHRASE.lower()):
        done = db.master_delete(**picked)
        st.session_state["delete_gen"] = generation + 1
        removed = sum(row["How many"] for row in done["Rows"])
        _flash(f"Deleted {_plural(removed, 'record')}. Next invoice no. #{done['Next after']}.", "warning")
        _rerun()


# No page title: the browser tab already names the app, and the row of screens is
# the first thing anyone needs. Streamlit's top padding goes too.
st.markdown(
    "<style>[data-testid='stMainBlockContainer'] { padding-top: 2.5rem; }</style>",
    unsafe_allow_html=True,
)
# Above the screens, so the message from the last action shows wherever the rerun
# lands.
_show_flash()
_chip_key()
_offer_billing()
_floating_back_to_top()
# A segmented control instead of st.tabs, for two reasons. st.tabs runs every
# tab's code on every click anywhere in the app, so the slowest screen slowed all
# the others. And any st.rerun() went back to the first tab, so confirmations
# landed somewhere you weren't looking. With the choice kept in session_state,
# only one screen is drawn and reruns stay on it.
SECTIONS = {
    "Teachers": teachers_tab,
    "Students": students_tab,
    "Schedule": schedule_tab,
    "Create/Edit schedule": create_schedule_tab,
    "Invoices": invoices_tab,
    "Payments": payments_tab,
    "Credits": credits_tab,
    "Data": data_tab,
    "Reminders": reminders_tab,
    "Delete records": delete_records_tab,
}

# A jump asked for by another screen (the "Bill <month>" button after an import)
# must be applied before the selector is created: Streamlit refuses a write to a
# widget's key once that widget exists in the same run.
_pending = st.session_state.pop("goto_section", None)
if _pending in SECTIONS:
    st.session_state["active_section"] = _pending

# Set here instead of through the widget's `default=`: a default plus writing the
# key from elsewhere (the jump below) makes Streamlit warn, and session_state alone
# does the job. A new session (a refresh, or the connection dropping and coming
# back) opens the screen named in the address instead of going back to Teachers.
if "active_section" not in st.session_state:
    _named = st.query_params.get("screen")
    st.session_state["active_section"] = _named if _named in SECTIONS else next(iter(SECTIONS))
# A screen name that has since changed mustn't carry over from a browser left open
# across the change; it goes to the first screen instead.
if st.session_state["active_section"] not in SECTIONS:
    st.session_state["active_section"] = next(iter(SECTIONS))
if st.session_state.get("last_section") not in SECTIONS:
    st.session_state.pop("last_section", None)
_section = st.segmented_control(
    "Section",
    list(SECTIONS),
    key="active_section",
    label_visibility="collapsed",
)
if _section is None:
    # Clicking the selected screen again unselects it. Go back to where we were
    # instead of showing a blank page, through goto_section, because Streamlit
    # refuses a write to a widget's own key once it's been created this run.
    st.session_state["goto_section"] = st.session_state.get(
        "last_section", next(iter(SECTIONS))
    )
    _rerun()
st.session_state["last_section"] = _section
if _section and st.query_params.get("screen") != _section:
    st.query_params["screen"] = _section
# `_section or ...` is an extra guard: _rerun() above stops the run under
# Streamlit, so None never gets here, but indexing by None would crash if that
# ever changed.
SECTIONS[_section or next(iter(SECTIONS))]()
