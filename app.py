"""KS Academia — scheduling and attendance.

Seven tabs:

* **Teachers** — upload a teacher's Excel schedule (the primary way classes
  enter the system), price a month's subjects, and read one subject's classes.
* **Students** — who took a class in a given month, and what it would come to
  at today's prices.
* **Timetable** — the month grid, for one-off manual fixes.
* **Invoices** — bill students for a month's unbilled classes.
* **Payments** — record what parents have actually paid, in full or in part.
* **Data** — earnings, hours and student counts per teacher, by month.
* **Reminders** — students overdue on payment.

A *subject* is what a teacher teaches — its rate and colour. A *class* is one
dated sitting of that subject, with its own students, times and conditions.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import calendar
import datetime as dt
import io
import zipfile
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

import auth
import db
import schedule_backfill
import schedule_parser
from schedule_parser import UNNAMED_CLASS
from timetable_grid import render_month_grid
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
    # The sidebar exists only to hold "signed in as" and the sign-out button,
    # so it starts out of the way rather than taking a column from the
    # timetable grid.
    initial_sidebar_state="collapsed",
)

# Before the database is touched and before a single student's name is drawn.
# The host's own private-app setting was found to still serve the running app
# on its internal path, so the gate has to be here rather than delegated.
_authenticator = auth.require_login()
auth.logout_button(_authenticator)

db.initialise_database()

LESSON_STATUSES = ["Scheduled", "Completed", "Cancelled"]

# A student has one condition per class, so this is a single choice rather
# than three independent ticks that could all be on at once.
IN_CLASS = "In class"
CONDITIONS = [IN_CLASS, "Online", "Recording", "Cancelled"]

# "A month" of a weekly class is four sittings on the same weekday.
WEEKS_PER_MONTH = 4

# How many open invoices to draw at once in the browse-everything list.
# Every row builds a downloadable invoice file up front, so this is a limit
# on payload and widget count, not on the data itself -- the search box
# beside it is how you reach the rest.
DELETE_PHRASE = "delete"
OPEN_INVOICE_ROWS = 25
STUDENT_ROWS = 25
PAID_ROWS = 40


def _rerun() -> None:
    st.rerun()


def _flash(message: str, kind: str = "success") -> None:
    """Queue a message to appear after the rerun that follows.

    Writing a message and immediately rerunning throws the message away --
    the rerun redraws the page from scratch before anything is shown. Almost
    every action here ends that way (save, add, delete, rename), so without
    this an admin clicks a button and sees nothing at all happen. Stashing
    it in session_state carries it across the rerun; ``_show_flash`` prints
    it at the top of the page, where it is visible from any tab.
    """
    st.session_state["_flash"] = (kind, message)


def _show_flash() -> None:
    entry = st.session_state.pop("_flash", None)
    if entry:
        kind, message = entry
        getattr(st, kind)(message)


def _as_date(value) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def _as_time(value) -> dt.time:
    if isinstance(value, dt.time):
        return value
    parts = [int(part) for part in str(value).split(":")[:2]]
    return dt.time(parts[0], parts[1] if len(parts) > 1 else 0)


def _hours(start: dt.time, end: dt.time) -> float:
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
    """A fixed corner button, present on every tab, that scrolls back to top.

    Injected into the parent document (not the iframe it runs in) so it
    can sit fixed over the whole page rather than just the iframe's box --
    same trick ``_scroll_to`` uses. Called once; since the HTML is identical
    on every rerun, Streamlit reuses the existing iframe rather than
    re-running the script, so the button is never appended twice. The guard
    on the element id makes that belt-and-braces.
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
        # Script only -- there is nothing to show, and this must not take
        # up space. st.iframe rejects a height of 0, and "content"
        # measures an empty body at 150px, so: the smallest legal box.
        height=1,
    )


def _inline_back_to_top() -> None:
    """A 'back to top' button at the foot of a tab's own content.

    A plain HTML button in its own iframe rather than an
    ``st.button``: clicking an ``st.button`` reruns the script, and a rerun
    sends Streamlit back to the first tab, so you would be scrolled to the top
    of Teachers instead of the top of whatever you were reading.  The frame is
    left transparent so it does not paint a white strip under a dark theme.
    """
    st.iframe(
        """<style>
          html, body { margin: 0; background: transparent; }
          #inline-top {
            font-family: system-ui, -apple-system, sans-serif;
            font-size: 13px; font-weight: 600; letter-spacing: .01em;
            padding: 8px 16px; border-radius: 8px; cursor: pointer;
            border: 1px solid rgba(128, 128, 128, .4);
            background: transparent; color: #ff4b4b;
          }
          #inline-top:hover { border-color: #ff4b4b; background: rgba(255, 75, 75, .08); }
        </style>
        <button id="inline-top">&uarr; Back to top</button>
        <script>
        document.getElementById('inline-top').onclick = function () {""" + _SCROLL_TOP_JS + """};
        </script>""",
        height=46,
    )


def _offer_billing() -> None:
    """A one-click hop from "schedule imported" to "bill that month".

    Sits directly under the import confirmation because that is the next
    thing anyone does. Pre-sets the Invoices month pickers so the screen
    opens on the month just imported rather than on today's.
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
    """Year + month pickers defaulting to the current month, used across tabs."""
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
    if row.get("is_cancelled"):
        return "Cancelled"
    if row.get("has_recording"):
        return "Recording"
    if row.get("is_online"):
        return "Online"
    return IN_CLASS


def _repeat_weekly(teacher_id, class_id, base, start, end, weeks, rows, note=""):
    """Create weekly copies of a class and report how it went.

    Both the add form and the class editor repeat by the month, so the loop
    lives here rather than twice over.  Returns (created, skipped, last date).
    """
    created = skipped = 0
    last = base
    for step in range(1, weeks + 1):
        later = base + dt.timedelta(weeks=step)
        outcome = db.create_timetable_session(
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
    """A student in a class with nothing marked against them yet."""
    return {
        "student_id": student_id,
        "is_online": False,
        "has_recording": False,
        "is_cancelled": False,
        "is_paid": False,
        "note": "",
    }


def _flags_for(condition: str) -> dict:
    return {
        "is_online": condition == "Online",
        "has_recording": condition == "Recording",
        "is_cancelled": condition == "Cancelled",
    }


def _explain(outcome: str, clash: str = "") -> str:
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
    """Every subject and every student the parse found, before anything commits.

    Built only when asked for: a workbook can hold a couple of years of
    classes, and folding all of that into tables on every rerun would make
    the upload form crawl for something most imports never need to look at.
    """
    sessions = preview.get("sessions") or []
    if not sessions:
        return

    # A class the sheet never named. Worth its own callout rather than being
    # left to the warnings list -- it imports under a placeholder, and only
    # the person holding the spreadsheet can say what it should be called.
    unnamed = [item for item in sessions if item["class_name"] == UNNAMED_CLASS]
    if unnamed:
        st.warning(
            f"**{len(unnamed)} class(es) have no subject name in the sheet.** "
            "They will import as "
            f"\u201c{UNNAMED_CLASS}\u201d. The subject belongs on the line "
            "above the time range \u2014 add it in Excel and upload again, or "
            "rename the subject afterwards on the Teachers screen."
        )
        for item in sorted(unnamed, key=lambda x: (x["date"], x["start_time"])):
            st.caption(
                f"\u2022 **{item['cell']}** \u2014 {item['date']:%d %b %Y} "
                f"{item['start_time']:%H:%M}\u2013{item['end_time']:%H:%M} \u2014 "
                + (", ".join(a["student_name"] for a in item["attendance"]) or "nobody listed")
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
            [
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
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.caption(f"{len(by_student)} student(s) found in the workbook.")
        st.dataframe(
            [
                {
                    "Student": name,
                    "Classes": data["Sessions"],
                    "Hours": round(data["Hours"], 2),
                    "Subjects": ", ".join(sorted(data["Subjects"])),
                    "Flagged": ", ".join(sorted(data["Conditions"])),
                }
                for name, data in sorted(by_student.items())
            ],
            width="stretch",
            hide_index=True,
        )


def _warning_panel(preview: dict) -> None:
    """Every warning, grouped by the cell that raised it.

    A warning is only actionable if the person holding the spreadsheet can
    find what it is about, so each block leads with the cell reference and
    the worksheet, then prints that cell's own text underneath. Several
    warnings about one cell share a block rather than repeating all of that.
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
        st.caption(
            "Each block names the cell it came from and shows what that cell "
            "holds \u2014 open that reference in Excel to fix it at source, or "
            "import as-is and correct it on the other screens."
        )
        for (sheet, coordinate), items in groups.items():
            with st.container(border=True):
                date = next((item["date"] for item in items if item["date"]), None)
                if coordinate:
                    where = f"\U0001f4cd Cell **{coordinate}** \u00b7 worksheet `{sheet}`"
                else:
                    where = f"\U0001f4c4 Worksheet `{sheet}`"
                if date:
                    where += f" \u00b7 {date:%a %d %b %Y}"
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


def _import_upload_panel() -> None:
    """Upload an Excel schedule for a teacher, review the parse, then commit it."""
    # Retired teachers are not offered: marking one inactive means they are
    # off the roster until they are taken back on, so a schedule should not
    # be importable against them. Bring them back from "Rename or retire".
    teachers = [item for item in db.get_all_teachers() if item["Active"]]

    mode = st.radio(
        "Teacher",
        ["Existing teacher", "New teacher"],
        horizontal=True,
        key="import_teacher_mode",
        label_visibility="collapsed",
        index=1 if not teachers else 0,
    )
    if mode == "New teacher" or not teachers:
        teacher_name = st.text_input("Teacher name", key="import_new_teacher_name").strip()
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
        key="import_file",
    )
    if upload is None:
        return

    file_bytes = upload.getvalue()
    try:
        sheets = schedule_parser.get_sheet_names(file_bytes)
    except Exception as error:  # a corrupt or non-Excel file
        st.error(f"Could not read that workbook: {error}")
        return

    chosen_sheets = st.multiselect(
        "Worksheets to import", sheets, default=sheets, key="import_sheets"
    )
    if not chosen_sheets:
        st.info("Pick at least one worksheet.")
        return

    # What was actually parsed, so a different file or a changed worksheet
    # selection can never silently commit a stale preview -- and so widget
    # keys below can be namespaced per parse, rather than reused across a
    # fresh one and showing decisions from a previous, unrelated workbook.
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

    preview = st.session_state.get("import_preview")
    if not preview:
        return
    if st.session_state.get("import_fingerprint") != fingerprint:
        st.warning(
            "The file, worksheets or year changed since this was parsed — "
            "click **Parse workbook** again before importing."
        )
        return
    generation = st.session_state.get("import_generation", 0)

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

    _warning_panel(preview)

    decisions: dict[int, str] = {}
    if preview["name_reviews"]:
        st.markdown(f"**Names that need a decision ({len(preview['name_reviews'])})**")
        st.caption(
            "Nothing is merged automatically — not even a pure capitalisation "
            "difference — because a bracketed tag can mean two people share a "
            "name, not that one is an alias of the other. Confirm each one."
        )
        for index, review in enumerate(preview["name_reviews"]):
            first, second = review["names"]
            first_count, second_count = review["counts"]
            choice = st.radio(
                f"'{first}' ({first_count}×) vs '{second}' ({second_count}×) "
                f"— {review['reason_text']}",
                ["Keep separate", "Merge — same person"],
                horizontal=True,
                key=f"import_review_{generation}_{index}",
            )
            decisions[index] = "merge" if choice.startswith("Merge") else "separate"

    student_matches: dict[str, int] = {}
    match_candidates = schedule_backfill.suggest_student_matches(preview["sessions"])
    if match_candidates:
        st.markdown(f"**Possible existing students ({len(match_candidates)})**")
        st.caption(
            "These parsed names don't exactly match anyone already on file, "
            "but look close to someone who is. Confirm whether each one is "
            "that existing student under a different spelling, or a new one."
        )
        for index, candidate in enumerate(match_candidates):
            reason_text = (
                "same name, tagged differently"
                if candidate["reason"] == "tag"
                else "similar spelling"
            )
            choice = st.radio(
                f"'{candidate['parsed_name']}' — {reason_text} to existing "
                f"student '{candidate['existing_name']}'",
                ["New / different person", f"Same as '{candidate['existing_name']}'"],
                horizontal=True,
                key=f"import_match_{generation}_{index}",
            )
            if choice.startswith("Same as"):
                student_matches[candidate["parsed_name"].casefold()] = candidate["existing_id"]

    name_overrides: dict[str, str] = {}
    if teacher_name:
        # Same subject typed two ways in one workbook. Settled before the
        # cross-teacher check below, because merging changes which names are
        # left to collide with anyone else's.
        # A class the sheet never named. Offered here rather than left as a
        # placeholder, because only the person holding the spreadsheet knows
        # what it should be called, and editing Excel and re-uploading just to
        # supply one word is a poor trade.
        unnamed = [
            item for item in preview["sessions"]
            if item["class_name"] == UNNAMED_CLASS
        ]
        if unnamed:
            st.markdown(f"**Give the unnamed class a subject ({len(unnamed)})**")
            when = ", ".join(
                sorted(
                    f"{item['date']:%d %b} {item['start_time']:%H:%M}"
                    for item in unnamed
                )
            )
            st.caption(
                f"The sheet has a time and a roster but no subject above them "
                f"— {when}. Leave it blank to import as "
                f"“{UNNAMED_CLASS}” and rename it later."
            )
            if len(unnamed) > 1:
                st.warning(
                    f"All {len(unnamed)} would take this one name, which merges "
                    "them into a single subject. If they are different classes, "
                    "name them in the sheet instead."
                )
            for item in sorted(unnamed, key=lambda x: (x["date"], x["start_time"])):
                st.caption(
                    f"• **{item['cell']}** — {item['date']:%d %b %Y} "
                    f"{item['start_time']:%H:%M}–{item['end_time']:%H:%M} — "
                    + (", ".join(a["student_name"] for a in item["attendance"])
                       or "nobody listed")
                )
            typed = st.text_input(
                "Subject name",
                key=f"import_unnamed_{generation}",
                placeholder="e.g. Upper-Sec Science",
            ).strip()
            named_unnamed = typed or ""
        else:
            named_unnamed = ""


        merged_into: dict[str, str] = {}
        merges = schedule_backfill.suggest_subject_merges(preview["sessions"])
        if merges:
            st.markdown(f"**Subjects typed two ways ({len(merges)})**")
            st.caption(
                "These are identical once capitalisation, spaces and "
                "punctuation are ignored — almost always one subject typed "
                "inconsistently. Merging keeps the billing together; keeping "
                "them separate makes two subjects, each needing its own rate."
            )
            for index, item in enumerate(merges):
                keep, drop = item["keep"], item["drop"]
                kept_n, drop_n = item["counts"]
                choice = st.radio(
                    f"'{drop}' ({drop_n} class(es)) and '{keep}' ({kept_n} class(es))",
                    [f"Merge into '{keep}'", "Keep separate"],
                    key=f"import_merge_{generation}_{index}",
                    horizontal=True,
                )
                if choice.startswith("Merge"):
                    merged_into[drop] = keep

        # Collisions are looked for among the names that will actually be
        # created: a subject just merged is checked under the name it merges
        # into, and a newly named class under the name just typed, rather than
        # under the spelling being retired.
        settled = dict(merged_into)
        if named_unnamed:
            settled[UNNAMED_CLASS] = named_unnamed
        effective = [
            {**item, "class_name": settled.get(item["class_name"], item["class_name"])}
            for item in preview["sessions"]
        ]
        renamed_to: dict[str, str] = {}
        renames = schedule_backfill.suggest_class_renames(effective, teacher_name)
        if renames:
            st.markdown("**Class names already used by another teacher**")
            st.caption(
                "A subject's name has to be unique across the whole academy — "
                "these collide with a class taught by someone else."
            )
            for original, suggestion in renames.items():
                new_name = st.text_input(
                    f"Rename '{original}'",
                    value=suggestion,
                    key=f"import_rename_{generation}_{original}",
                )
                renamed_to[original] = new_name.strip() or suggestion

        # backfill applies these in one pass with no chaining, so each original
        # spelling has to name its final destination outright: a merged name
        # follows its target through any rename applied to it.
        name_overrides.update(renamed_to)
        for original, chosen in settled.items():
            name_overrides[original] = renamed_to.get(chosen, chosen)

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
        result = schedule_backfill.backfill(
            preview, commit_id,
            name_overrides=name_overrides,
            student_matches=student_matches,
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
            # "lessons_removed" also begins with "lessons_", but a class the
            # workbook no longer lists was deliberately taken off the
            # calendar -- counting it as skipped read as a failure and
            # inflated the number.
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
            _flash(message)
            # The month just imported is almost always the one about to be
            # billed, so offer the jump instead of making anyone re-pick it.
            months = sorted(
                {(item["date"].year, item["date"].month) for item in sessions
                 if item.get("date")}
            )
            if months:
                st.session_state["offer_billing"] = months[-1]
            st.session_state.pop("import_preview", None)
            _rerun()
        else:
            st.warning(f"Nothing imported ({result['status']}).")


def _save_subject_rate(class_id: int, month_start: dt.date, rate: float) -> str:
    """Price one subject from a whole calendar month onward.

    The work is a single transaction in db.set_class_rate_for_month; this is
    the screen's name for it.
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
    """Tick several subjects at once and set one rate for all of them.

    Subjects are often all the same price -- doing that one at a time through
    the single-subject editor is needless repetition.  Shared by the Teachers
    tab (one teacher's month) and the Invoices tab (whatever is still
    unpriced for the month being billed, which can span teachers), so
    ``subjects`` is a plain list of ``{"Class ID", "Class", "Sessions"}``
    rather than anything either caller's shape.

    Uses the "on_change pre-seeds session_state before the checkboxes render"
    pattern for Select all, which is the reliable way to make a bulk checkbox
    toggle actually work in Streamlit.
    """
    if not subjects:
        return
    month_start = dt.date(year, month, 1)
    subjects = sorted(subjects, key=lambda item: (item.get("Teacher", ""), item["Class"]))
    # Clearing the ticks after applying can't be done by resetting their
    # session_state: on a rerun the frontend still reports each checkbox as
    # checked, so the old value comes straight back. Giving the checkboxes a
    # new key instead -- by bumping this counter -- makes Streamlit treat
    # them as brand-new widgets, which come up at their unticked default.
    generation = st.session_state.get(f"bulk_rate_gen_{base}", 0)
    scope = f"{base}_{generation}"

    if heading:
        st.markdown(f"###### {heading}")

    def _select_all_subjects() -> None:
        value = st.session_state.get(f"bulk_rate_all_{scope}", False)
        for item in subjects:
            st.session_state[f"bulk_rate_pick_{scope}_{item['Class ID']}"] = value

    st.checkbox("Select all", key=f"bulk_rate_all_{scope}", on_change=_select_all_subjects)

    # The rate that actually applies in the month being looked at -- not the
    # month before it. Showing the previous month's price here would mean a
    # subject still reads "no rate set" right after its rate was set for this
    # month, which is exactly backwards. Fetched for every subject at once.
    month_rates = db.get_class_rates_for_date(
        [item["Class ID"] for item in subjects], month_start
    )
    # The price on its own cannot say whether it was chosen for this month or
    # is last month's still running on, and that is the whole question when
    # pricing a fresh month. A rate period always starts on the 1st, so the
    # one covering this month either starts on it -- set here -- or earlier,
    # in which case the label names the month the price carries over from.
    priced_from: dict[int, dt.date] = {}
    for row in db.get_all_class_rates():
        starts = _as_date(row["Effective From"])
        ends = _as_date(row["Effective To"]) if row["Effective To"] else None
        if starts > month_start or (ends and ends < month_start):
            continue
        if priced_from.get(row["Class ID"], dt.date.min) < starts:
            priced_from[row["Class ID"]] = starts

    selected_ids: list[int] = []
    for item in subjects:
        class_id = item["Class ID"]
        month_rate = month_rates.get(class_id)
        starts = priced_from.get(class_id)
        # The placeholder an import seeds is not a price anybody chose, so it
        # reads as unset here -- same as it does in the Invoices warning.
        if not (month_rate and month_rate > db.UNSET_RATE):
            rate_text = "no price set"
        elif starts is None or starts == month_start:
            rate_text = f"${month_rate:,.2f}/h set for this month"
        else:
            rate_text = (
                f"${month_rate:,.2f}/h carried over from "
                f"{calendar.month_name[starts.month]} {starts.year}"
            )
        who = f" ({item['Teacher']})" if show_teacher and item.get("Teacher") else ""
        checked = st.checkbox(
            f"{item['Class']}{who} — {rate_text}, {item['Sessions']} session(s)",
            key=f"bulk_rate_pick_{scope}_{class_id}",
        )
        if checked:
            selected_ids.append(class_id)

    columns = st.columns([2, 1])
    # Keyed on `base`, not `scope`, so the rate typed here survives applying
    # -- handy when the same price is being set for another month next.
    bulk_rate = columns[0].number_input(
        "New hourly rate ($) for the selected subjects",
        min_value=0.0, step=1.0, key=f"bulk_rate_value_{base}",
    )
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
                # Not needed to clear the tick (the new generation does
                # that) -- just keeps spent keys from piling up in state.
                st.session_state.pop(f"bulk_rate_pick_{scope}_{class_id}", None)
            st.session_state.pop(f"bulk_rate_all_{scope}", None)
            if fail:
                st.warning(f"{fail} subject(s) could not be updated.")
            if ok:
                st.session_state[f"bulk_rate_gen_{base}"] = generation + 1
                # Flashed at the top of the page rather than shown here: once
                # the last subject is priced this whole section stops being
                # rendered, and a confirmation inside it would never appear.
                _flash(
                    f"Set ${bulk_rate:,.2f}/h for {ok} subject(s), "
                    f"from {calendar.month_name[month]} {year} onward."
                )
                _rerun()


def _delete_subjects_section(teacher_id: int, teacher_name: str) -> None:
    """Remove subjects, and everything under them, from a teacher.

    The way back from an import filed under the wrong teacher: delete what it
    created, then upload the same workbook again against the right one. Kept
    behind a tick and a typed confirmation because it is the one genuinely
    destructive thing on this screen.
    """
    subjects = [
        item for item in db.get_all_classes() if item["Teacher ID"] == teacher_id
    ]
    if not subjects:
        return

    with st.expander(f"Delete subjects from {teacher_name}"):
        st.caption(
            "Deletes the subject, its classes and its rosters. A charge on an "
            "invoice that has not gone out is simply dropped; one already sent "
            "is credited back to that student's next invoice, so nothing a "
            "parent has received is rewritten."
        )
        generation = st.session_state.get(f"del_subj_gen_{teacher_id}", 0)
        scope = f"{teacher_id}_{generation}"

        def _select_all() -> None:
            value = st.session_state.get(f"del_subj_all_{scope}", False)
            for item in subjects:
                st.session_state[f"del_subj_{scope}_{item['ID']}"] = value

        st.checkbox("Select all", key=f"del_subj_all_{scope}", on_change=_select_all)
        picked = [
            item["ID"] for item in subjects
            if st.checkbox(item["Class"], key=f"del_subj_{scope}_{item['ID']}")
        ]
        if not picked:
            st.caption("Tick the subjects to remove.")
            return

        impact = db.describe_class_deletion(picked)
        st.dataframe(
            [
                {
                    "Subject": row["Class"],
                    "Classes": row["Lessons"],
                    "Students": row["Students"],
                    "Already invoiced": row["On issued invoices"],
                    "From": row["First"],
                    "To": row["Last"],
                }
                for row in impact
            ],
            width="stretch",
            hide_index=True,
        )
        billed = sum(row["On issued invoices"] for row in impact)
        lessons = sum(row["Lessons"] for row in impact)
        if billed:
            st.warning(
                f"**{billed} of these classes are on invoices that have already "
                "gone out.** Deleting them credits that money back to the "
                "students' next invoices rather than changing the sent bills."
            )

        st.markdown(
            f"This removes **{len(picked)} subject(s)** and **{lessons} class(es)**. "
            "It cannot be undone from here \u2014 re-importing the workbook is "
            "what brings them back."
        )
        typed = st.text_input(
            f"Type **{DELETE_PHRASE}** to confirm",
            key=f"del_subj_confirm_{scope}",
            placeholder=DELETE_PHRASE,
        )
        if st.button(
            f"Delete {len(picked)} subject(s)",
            key=f"del_subj_go_{scope}",
            type="primary",
            disabled=typed.strip().lower() != DELETE_PHRASE.lower(),
        ):
            gone = credits = 0
            for class_id in picked:
                outcome = db.delete_class(class_id)
                if outcome.get("status") == "deleted":
                    gone += 1
                    credits += outcome.get("credits_raised", 0)
            message = f"Deleted {gone} subject(s) and {lessons} class(es) from {teacher_name}."
            if credits:
                message += (
                    f" {credits} charge(s) were already invoiced and have been "
                    "credited back."
                )
            st.session_state[f"del_subj_gen_{teacher_id}"] = generation + 1
            _flash(message, "warning")
            _rerun()


def _teacher_drilldown(teachers: list[dict]) -> None:
    """Pick a teacher and a month: price the subjects, then read one's classes.

    Active teachers only, matching the Timetable and Data screens -- a
    retired teacher is off every working screen until somebody takes them
    back on from "Rename or retire", which still lists everybody.
    """
    labels = {item["Name"]: item["ID"] for item in teachers if item["Active"]}
    if not labels:
        st.caption("No active teachers. Bring one back from the panel above.")
        return
    chosen = st.selectbox("Teacher", list(labels), key="teacher_drill_pick")
    teacher_id = labels[chosen]

    year, month = _month_picker("teacher_drill")
    lessons = db.get_month_timetable(teacher_id, year, month)
    if not lessons:
        st.caption(f"No classes for {chosen} in {calendar.month_name[month]} {year}.")
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
    _delete_subjects_section(teacher_id, chosen)
    st.divider()

    subject_labels = {
        f"{items[0]['Class']} ({len(items)} session(s))": class_id
        for class_id, items in sorted(by_class.items(), key=lambda kv: kv[1][0]["Class"])
    }
    # Pricing lives entirely in the section above: it already lists every
    # subject with the price in force and where that price came from, and
    # ticking one is the same edit this used to duplicate -- badly, since it
    # pre-filled the $1 import placeholder as though somebody had chosen it.
    # What is left here is the one thing that section cannot do: read out a
    # single subject's classes.
    st.markdown("###### Or list one subject's classes")
    subject_choice = st.selectbox("Subject", list(subject_labels), key="teacher_drill_subject")
    class_id = subject_labels[subject_choice]
    class_lessons = sorted(by_class[class_id], key=lambda item: (item["Date"], item["Start"]))
    class_name = class_lessons[0]["Class"]

    st.markdown(f"###### {class_name} — {calendar.month_name[month]} {year}")
    for lesson in class_lessons:
        detail = db.get_timetable_session(lesson["ID"])
        tagged = []
        for row in (detail["Attendance"] if detail else []):
            condition = _condition_of(row)
            tagged.append(
                row["student_name"]
                if condition == IN_CLASS
                else f"{row['student_name']} ({condition})"
            )
        st.caption(
            f"{lesson['Date']:%d %b} · {lesson['Start']:%H:%M}–{lesson['End']:%H:%M} "
            f"({_hours(lesson['Start'], lesson['End']):g}h) — "
            f"{', '.join(tagged) or 'no students'}"
        )


def teachers_tab() -> None:
    st.subheader("Teachers")

    teachers = db.get_all_teachers()

    with st.expander("Upload an Excel schedule", expanded=not teachers):
        _import_upload_panel()

    with st.form("add_teacher", clear_on_submit=True):
        name = st.text_input("Teacher name", key="teacher_new")
        if st.form_submit_button("Add teacher", type="primary"):
            if db.create_teacher(name):
                _flash(f"Added {name.strip()}.")
                _rerun()
            else:
                st.warning("Enter a name that is not already on the list.")

    if not teachers:
        st.info("No teachers yet. Add one above, or upload a schedule to create one.")
        return

    per_teacher: dict = {}
    for item in db.get_all_classes():
        per_teacher[item["Teacher"]] = per_teacher.get(item["Teacher"], 0) + 1

    st.dataframe(
        [
            {
                "Teacher": item["Name"],
                "Subjects": per_teacher.get(item["Name"], 0),
                "Active": item["Active"],
            }
            for item in teachers
        ],
        width="stretch",
        hide_index=True,
    )

    with st.expander("Rename or retire a teacher"):
        chosen = st.selectbox(
            "Teacher", [item["Name"] for item in teachers], key="teacher_pick"
        )
        record = next(item for item in teachers if item["Name"] == chosen)
        new_name = st.text_input("New name", value=record["Name"], key="teacher_rename")
        columns = st.columns(3)
        if columns[0].button("Save name", key="teacher_save"):
            if db.update_teacher_name(record["ID"], new_name):
                _flash("Renamed.")
                _rerun()
            else:
                st.warning("That name is not available.")
        label = "Mark inactive" if record["Active"] else "Mark active"
        if columns[1].button(label, key="teacher_toggle"):
            db.update_teacher_status(record["ID"], not record["Active"])
            _rerun()

    st.markdown("#### Teacher detail")
    _teacher_drilldown(teachers)


# ---------------------------------------------------------------------------
# Students
# ---------------------------------------------------------------------------


def students_tab() -> None:
    st.subheader("Students")

    year, month = _month_picker("students_month")

    st.markdown("###### Schedule uploads this month")
    statuses = db.get_import_status(year, month)
    if not statuses:
        st.caption("No active teachers yet.")
    else:
        badge_columns = st.columns(min(len(statuses), 4))
        for index, item in enumerate(statuses):
            slot = badge_columns[index % len(badge_columns)]
            if item["Imported"]:
                slot.success(f"✅ {item['Teacher']} · {item['Imported At']:%d %b %H:%M}")
            else:
                slot.warning(f"⏳ {item['Teacher']} — not uploaded yet")

    st.divider()

    with st.form("add_student", clear_on_submit=True):
        columns = st.columns(3)
        name = columns[0].text_input("Student name", key="student_new_name")
        parent = columns[1].text_input(
            "Parent name", placeholder="optional", key="student_new_parent"
        )
        phone = columns[2].text_input(
            "Contact number", placeholder="optional", key="student_new_phone"
        )
        if st.form_submit_button("Add student", type="primary"):
            # A parent needs both halves to be stored at all, so half of one
            # is a slip to point out rather than quietly drop on the floor.
            if bool(parent.strip()) != bool(phone.strip()):
                outcome = "parent_unavailable"
            elif parent.strip():
                outcome = db.create_student(name[:120], parent[:120], phone[:40])
            else:
                outcome = db.create_quick_student(name[:120])
            if outcome == "created":
                _flash(f"Added {name.strip()}.")
                _rerun()
            elif outcome == "duplicate":
                st.warning("That student is already on the list.")
            elif outcome == "parent_unavailable":
                st.warning(
                    "A parent needs both a name and a contact number — fill "
                    "in the other one, or leave both blank to add the "
                    "student on their own."
                )
            else:
                st.warning("Enter a name first.")
    st.caption("Parent details are optional — a student can be completed later.")

    all_students = db.get_all_students()
    if not all_students:
        st.info("No students yet.")
        return

    student_notes = {item["ID"]: item["Note"] for item in all_students if item["Note"]}
    show_all = st.checkbox(
        "Show all students, not just this month", key="students_show_all"
    )
    month_ids = {item["ID"] for item in db.get_students_in_month(year, month)}
    pool = all_students if show_all else [
        item for item in all_students if item["ID"] in month_ids
    ]

    search = st.text_input(
        "Search", placeholder="Type part of a name", key="student_search"
    )
    shown = [
        item
        for item in pool
        if not search.strip() or search.strip().lower() in item["Name"].lower()
    ]
    if show_all:
        st.caption(f"{len(shown)} of {len(all_students)} students")
    else:
        st.caption(
            f"{len(shown)} student(s) with a class in "
            f"{calendar.month_name[month]} {year} ({len(all_students)} total on file)"
        )

    # One query for everyone shown, rather than one per student expanded.
    breakdowns = db.get_all_student_month_breakdowns(year, month)

    # Capped hard, because Streamlit builds an expander's body whether or not
    # it is open: every row here is a table, three inputs, a note box and the
    # buttons, so a whole academy's worth of them is thousands of widgets and
    # several seconds before anything appears. Searching is the fast path.
    page = shown[:STUDENT_ROWS]
    if len(shown) > len(page):
        st.info(
            f"Showing the first {len(page)} of {len(shown)} — type a name "
            "above to find someone specific."
        )

    for item in page:
        marker = " 📝" if student_notes.get(item["ID"]) else ""
        contact = (
            f" — {item['Parent']} · {item['Phone']}"
            if item["Parent"]
            else " — no parent on file"
        )
        with st.expander(f"{item['Name']}{contact}{marker}"):
            breakdown = breakdowns.get(item["ID"], {"lines": [], "total": 0.0})
            if breakdown["lines"]:
                st.dataframe(
                    [
                        {
                            "Subject": line["Subject"],
                            "Teacher": line["Teacher"],
                            "Sessions": line["Sessions"],
                            "Hours": line["Hours"],
                            "Rate": f"${line['Rate']:,.2f}/h",
                            "Amount": f"${line['Amount']:,.2f}",
                        }
                        for line in breakdown["lines"]
                    ],
                    width="stretch",
                    hide_index=True,
                )
                owed = db.get_student_credit_total(item["ID"])
                figures = st.columns(2) if owed else [st]
                figures[0].metric(
                    f"Total for {calendar.month_name[month]} {year}",
                    f"${breakdown['total']:,.2f}",
                )
                if owed:
                    figures[1].metric(
                        "Credit waiting", f"-${owed:,.2f}",
                        help="Cancelled classes already billed; comes off "
                             "their next invoice automatically.",
                    )
            else:
                st.caption(f"No classes in {calendar.month_name[month]} {year}.")

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
            buttons = st.columns([1, 1, 4])
            if buttons[0].button("Save", key=f"ss{item['ID']}", type="primary"):
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

            usage = db.get_student_usage(item["ID"])
            with buttons[1].popover("Delete"):
                st.write(f"**Delete {item['Name']}?**")
                if usage["invoices"]:
                    st.warning(
                        f"**{item['Name']} has {usage['invoices']} invoice(s) "
                        "already sent, so they cannot be deleted.** An invoice "
                        "keeps the student it was addressed to — without them "
                        "it would vanish from the Invoices, Payments and "
                        "Reminders screens while still counting as earned."
                    )
                elif usage["classes"] or usage["subjects"]:
                    st.warning(
                        f"They are in {usage['classes']} class(es) and enrolled "
                        f"in {usage['subjects']} subject(s). Deleting removes "
                        "them from all of it, including past classes and any "
                        "invoice still being drafted."
                    )
                else:
                    st.caption("They are not in any class yet.")
                if st.button(
                    "Delete for good",
                    key=f"sd{item['ID']}",
                    disabled=bool(usage["invoices"]),
                ):
                    outcome = db.remove_student(item["ID"])
                    if outcome == "deleted":
                        _flash(f"{item['Name']} removed.")
                        _rerun()
                    elif outcome == "has_invoices":
                        st.warning(
                            "They have been invoiced — that has to stay on file."
                        )
                    else:
                        st.warning(f"Could not delete ({outcome}).")


# ---------------------------------------------------------------------------
# Timetable
# ---------------------------------------------------------------------------


def _anchor(name: str) -> None:
    """Drop an invisible marker the scroll helpers can aim at."""
    st.markdown(f"<div id='{name}'></div>", unsafe_allow_html=True)


def _scroll_to(name: str, token: object = "") -> None:
    """Scroll the page to a marker.

    The script runs inside an iframe, which is same-origin, so it can
    reach the parent document that actually scrolls.
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
        # Script only -- there is nothing to show, and this must not take
        # up space. st.iframe rejects a height of 0, and "content"
        # measures an empty body at 150px, so: the smallest legal box.
        height=1,
    )


def _back_to_grid_button() -> None:
    """A plain HTML button, so returning to the grid costs no rerun."""
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
    for item in lessons:
        if _as_date(item["Date"]) != when:
            continue
        if _as_time(item["Start"]) < end and _as_time(item["End"]) > start:
            return (
                f"{item['Class']} already runs {_as_time(item['Start']):%H:%M}"
                f"–{_as_time(item['End']):%H:%M} that day."
            )
    return ""


def _add_lesson(teacher_id: int, year: int, month: int, lessons: list) -> None:
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
            rate = columns[1].number_input(
                "Hourly rate", min_value=0.01, value=80.0, step=5.0, key="new_class_rate"
            )
            colour = columns[2].color_picker(
                "Colour", value="#FFF2CC", key="new_class_colour"
            )

        default_day = min(dt.date.today().day, calendar.monthrange(year, month)[1])
        columns = st.columns([2, 1, 1, 1])
        when = columns[0].date_input(
            "Date", value=dt.date(year, month, default_day), key="new_lesson_date"
        )
        # No suggested times: a pre-filled time is easy to miss and then gets
        # copied into every repeat of the class.
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
                f"{extra + 1} classes in total, every {when:%A} at "
                f"{start:%H:%M}, from {when:%d %b} through {last:%d %b}."
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
            else:
                class_id = next(c["ID"] for c in classes if c["Class"] == choice)
                outcome = db.create_timetable_session(
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
    """Every date this subject runs at this time, from ``base`` onwards.

    Looks six months ahead so a run that has crossed a month boundary is still
    found in full.
    """
    found = []
    year, month = base.year, base.month
    for _ in range(6):
        found += [
            _as_date(item["Date"])
            for item in db.get_month_timetable(teacher_id, year, month)
            if item["Class ID"] == class_id and _as_time(item["Start"]) == start
        ]
        month += 1
        if month == 13:
            month, year = 1, year + 1
    return sorted(set(found))


def _lesson_editor(lesson_id: int, teacher_id: int) -> None:
    """Everything about one class, then everything about its subject.

    Both sit in expanders so the screen reads as two decisions rather than a
    wall of controls: what happened in this sitting, and what the subject is.
    """
    detail = db.get_timetable_session(lesson_id)
    if not detail:
        st.info("That class no longer exists.")
        return

    students = db.get_all_students()
    lookup = {item["Name"]: item["ID"] for item in students}
    student_notes = {item["ID"]: item["Note"] for item in students if item["Note"]}
    attendance = detail.get("Attendance", [])

    st.markdown(
        f"#### {detail['Class']} · {_as_date(detail['Date']):%a %d %b} · "
        f"{_as_time(detail['Start']):%H:%M}–{_as_time(detail['End']):%H:%M}"
    )

    with st.expander("Edit the class", expanded=True):
        columns = st.columns([1.4, 1, 1, 1.2])
        when = columns[0].date_input(
            "Date", value=_as_date(detail["Date"]), key=f"date_{lesson_id}"
        )
        start = columns[1].time_input(
            "Start", value=_as_time(detail["Start"]), step=60, key=f"start_{lesson_id}"
        )
        end = columns[2].time_input(
            "End", value=_as_time(detail["End"]), step=60, key=f"end_{lesson_id}"
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
        if add_columns[1].button("Add", key=f"addgo_{lesson_id}",
                                 width="stretch", disabled=not adding):
            existing = [
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
            for name in adding:
                existing.append(_blank_row(lookup[name]))
            outcome = db.update_timetable_session(
                session_id=lesson_id,
                session_date=_as_date(detail["Date"]),
                start_time=_as_time(detail["Start"]),
                end_time=_as_time(detail["End"]),
                status=detail["Status"],
                note=detail.get("Note") or "",
                attendance_rows=existing,
            )
            if outcome in ("updated", "success", True):
                _flash(f"Added {len(adding)} student(s).")
                _rerun()
            else:
                st.warning(_explain(str(outcome)))

        st.caption(
            "Condition applies to this class only — everyone defaults to being "
            "in the room. Paid is filled in for you when the invoice covering "
            "the class is recorded on the Payments screen."
        )
        rows = [
            {
                "Student": row["student_name"],
                "Condition": _condition_of(row),
                "Paid": bool(row.get("is_paid")),
                "Note": row.get("note") or "",
            }
            for row in attendance
        ]
        edited = st.data_editor(
            rows or [{"Student": None, "Condition": IN_CLASS, "Paid": False, "Note": ""}],
            num_rows="dynamic",
            column_config={
                "Student": st.column_config.SelectboxColumn(
                    options=sorted(lookup), required=True, width="medium"
                ),
                "Condition": st.column_config.SelectboxColumn(
                    options=CONDITIONS, required=True, width="small",
                    help="One condition per student per class — picking a new "
                         "one replaces the old.",
                ),
                # Read-only: payment belongs to the invoice a parent actually
                # pays, and is recorded on the Payments screen. Editable here
                # too, it would be a second source of truth that could
                # disagree with the invoice.
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
                st.markdown(f"📝 **{name}** — {text}")

        buttons = st.columns([1, 1.3, 1, 3])
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
                    # One row per student: the table allows picking the same
                    # name twice, the database does not.
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
                    + ", ".join(sorted(set(duplicates)))
                )
            outcome = db.update_timetable_session(
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
                    # The class moved out of the month on screen; follow it
                    # rather than leaving the user staring at an empty grid.
                    st.session_state["month"] = (when.year, when.month)
                _flash("Saved.")
                _rerun()
            else:
                st.warning(_explain(str(outcome)))

        run = _run_dates(
            teacher_id, detail["Class ID"], _as_time(detail["Start"]),
            _as_date(detail["Date"]),
        )
        is_last = not run or _as_date(detail["Date"]) >= max(run)
        months_key = f"rep_{lesson_id}"
        months = st.session_state.setdefault(months_key, 0)

        with buttons[1]:
            if not is_last:
                # Repeating from the middle would land on dates the run already
                # covers, so point at the class that can actually extend it.
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
            through = _as_date(detail["Date"]) + dt.timedelta(weeks=extra)
            st.info(
                f"**+{months} month{'s' if months > 1 else ''}** — adds {extra} "
                f"weekly classes, every {_as_date(detail['Date']):%A} at "
                f"{_as_time(detail['Start']):%H:%M}, through {through:%d %b}. "
                "Same students, everyone in class and unpaid."
            )
            confirm = st.columns([1, 4])[0]
            if confirm.button(f"Add {extra} classes", key=f"repgo_{lesson_id}",
                              type="primary", width="stretch"):
                base = _as_date(detail["Date"])
                made, blocked, last = _repeat_weekly(
                    teacher_id, detail["Class ID"], base,
                    _as_time(detail["Start"]), _as_time(detail["End"]), extra,
                    [_blank_row(row["student_id"]) for row in attendance],
                )
                st.session_state[months_key] = 0
                if made:
                    message = f"Added {made} class(es), through {last:%d %b}."
                    if blocked:
                        message += f" {blocked} skipped — the teacher was busy."
                    st.success(message)
                else:
                    _flash("Nothing added — those slots are already taken.", "info")
                _rerun()

        with buttons[2].popover("Delete", width="stretch"):
            st.write("Removes this class and its attendance. The subject stays.")
            if st.button("Delete for good", key=f"del_{lesson_id}"):
                db.delete_timetable_session(lesson_id)
                st.session_state.pop("selected_lesson", None)
                _rerun()

    with st.expander(f"Edit the subject — {detail['Class']}"):
        st.caption(
            "A subject is the teacher's, its price and its colour on the grid. "
            "Students belong to each class, so they are set above."
        )
        current = next(
            (
                item
                for item in db.get_teacher_classes_for_schedule(
                    teacher_id, _as_date(detail["Date"])
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
        class_rate = columns[1].number_input(
            "Hourly rate",
            min_value=0.01,
            value=float(current.get("Hourly Rate") or 80.0),
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
                # Students belong to a class, not the subject; keep whatever
                # enrolment exists rather than rewriting it from this screen.
                student_ids=db.get_class_student_ids(detail["Class ID"]),
                display_color=class_colour,
                effective_from=_as_date(detail["Date"]),
            )
            if outcome in ("updated", "success", True):
                # Only once the subject itself saved: writing the note first
                # left it applied under a rename that had been rejected.
                db.set_subject_note(detail["Class ID"], class_note[:1000])
                _flash("Subject saved.")
                _rerun()
            else:
                st.warning(_explain(str(outcome)))


def timetable_tab() -> None:
    teachers = [item for item in db.get_all_teachers() if item["Active"]]
    if not teachers:
        st.info("Add a teacher first — a timetable belongs to one.")
        return

    top = st.columns([2, 6])
    teacher_name = top[0].selectbox(
        "Teacher", [item["Name"] for item in teachers], key="tt_teacher"
    )
    teacher_id = next(item["ID"] for item in teachers if item["Name"] == teacher_name)

    _anchor("grid-top")
    year, month = _month_navigator()
    summary = db.get_month_timetable(teacher_id, year, month)
    # One query for the whole month's rosters, rather than three per class.
    # The grid only needs each card's student names and their conditions,
    # which is exactly what this returns.
    rosters = db.get_month_attendance(teacher_id, year, month)
    lessons = [
        {**item, "Attendance": rosters.get(item["ID"], [])} for item in summary
    ]

    if lessons:
        strip = st.columns(5)
        strip[0].metric("Classes", len(lessons))
        strip[1].metric("Teaching days", len({_as_date(i["Date"]) for i in lessons}))
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
            # Compare the nonce, not the class id: clicking the same class
            # again is a fresh click and should jump to the editor again.
            st.session_state["grid_click"] = clicked.get("nonce")
            st.session_state["selected_lesson"] = clicked.get("lesson_id")
            st.session_state["jump_to_editor"] = True
            _rerun()
    else:
        st.info(f"Nothing scheduled for {calendar.month_name[month]} {year} yet.")

    _add_lesson(teacher_id, year, month, lessons)

    if not lessons:
        return

    selected = st.session_state.get("selected_lesson")
    if selected not in {item["ID"] for item in lessons}:
        selected = None

    if selected is None:
        st.caption("Click a class in the grid above to edit it.")
        return

    _anchor("class-editor")
    _lesson_editor(selected, teacher_id)
    _back_to_grid_button()
    if st.session_state.pop("jump_to_editor", False):
        _scroll_to("class-editor", st.session_state.get("grid_click", ""))


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------


def _invoice_filename(invoice: dict, ext: str) -> str:
    label = invoice.get("Number") or f"draft-{invoice['ID']}"
    student = "".join(
        ch for ch in invoice.get("Student", "student") if ch.isalnum() or ch in " -_"
    ).strip().replace(" ", "-")
    # Name first, invoice number second -- a folder of these sorts
    # alphabetically by student, so finding the one file to send to one
    # parent is a scroll to their name, not a read of every invoice number.
    return f"{student}-invoice-{label}.{ext}"


def _invoice_download(invoice: dict, key: str) -> None:
    """Offer the printable invoice as an HTML file, ready to print or send.

    Deliberately HTML here, not the image format -- this renders inside a
    loop over however many invoices are on screen (up to the whole open
    list), and a download button's data has to be computed eagerly, on
    every render, whether or not anyone clicks it. HTML costs nothing to
    build; a PNG launches a whole headless browser, and doing that once per
    row for a long list would make the page itself crawl. The bulk "download
    as images" button generates every image through one shared browser
    instance instead, which is what makes that one fast.
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
    """Which "send this to a parent" format this machine can actually produce.

    Images are the better artefact and stay the first choice: KakaoTalk shows
    them inline, whereas a PDF arrives as an attachment the parent has to tap
    to open. But images come out of a headless Chromium, and the deployed
    host has neither one nor any way to install one, so PDF -- same
    stylesheet, no browser -- is what is left there. HTML is the floor: it
    always works and is useless on a phone.

    Deciding here rather than at each button keeps the answer consistent
    across the three places invoices can be exported.
    """
    if image_export_available():
        return "png"
    if pdf_export_available():
        return "pdf"
    return "html"


def _send_noun() -> str:
    return _EXPORT_NOUN[_send_format()]


def _invoices_zip(invoices: list[dict], as_images: bool, on_progress=None) -> bytes:
    """Every invoice as its own file inside one zip -- one click gets you
    everyone's invoice as a *separate* file, ready to attach individually
    to each student/parent, rather than one combined document that would
    show every student's billing to whoever it's sent to.
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
                # Two different invoices could sanitise to the same file
                # name (e.g. duplicate student names) -- keep both.
                name = f"{invoice['ID']}-{name}"
            used_names.add(name)
            archive.writestr(name, page)
    return buffer.getvalue()


def _invoice_table(invoice: dict) -> None:
    lines = invoice.get("Lines") or []
    if not lines:
        st.caption("No classes on this invoice yet.")
        return
    st.dataframe(
        [
            {
                "Qty": line["Quantity"],
                "Subject": line["Subject"],
                "Dates": format_dates(line["Dates"]),
                # A credit has no unit price to quote -- nothing is being
                # charged -- so quoting "$0.00 × 0h" would just be noise.
                # Same treatment the printable copy gives it.
                "Unit price": (
                    "credit" if line.get("Credit")
                    else f"${line['Rate']:,.2f} × {line['Hours']:g}h"
                ),
                "Amount": f"{line['Amount']:,.2f}",
            }
            for line in lines
        ],
        width="stretch",
        hide_index=True,
    )


def _just_issued_panel(year: int, month: int) -> None:
    """What was just issued, and a way to get those invoices out to parents.

    Sits at the foot of the tab, not the head: issuing produces a long list,
    and putting it above the month figures buried the thing you came to the
    tab to read. It also belongs to the month it was issued for, so it stays
    out of the way once you move on to a different month.

    Nothing is rendered per row beyond a tick box. Building a downloadable
    invoice up front for every student is what made this section unusable --
    each one embeds the letterhead, so a whole month of them is megabytes of
    payload before you have asked for anything.
    """
    batch = st.session_state.get("invoices_just_issued")
    if not batch or tuple(batch.get("period", ())) != (year, month):
        return
    ids = batch.get("ids") or []
    details = [detail for detail in (db.get_invoice(i) for i in ids) if detail]
    if not details:
        return

    period = f"{calendar.month_name[month]} {year}"
    st.divider()
    st.markdown(f"#### Just issued — {len(details)} invoice(s) for {period}")

    scope = f"{year}_{month}"

    def _select_all() -> None:
        value = st.session_state.get(f"ji_all_{scope}", False)
        for item in details:
            st.session_state[f"ji_pick_{scope}_{item['ID']}"] = value

    st.checkbox("Select all", key=f"ji_all_{scope}", on_change=_select_all)

    chosen = []
    for detail in details:
        if st.checkbox(
            f"#{detail['Number']} — {detail['Student']} — ${detail['Total']:,.2f}",
            key=f"ji_pick_{scope}_{detail['ID']}",
        ):
            chosen.append(detail)

    if not chosen:
        st.caption("Tick the students you want to send to, or use Select all.")
    else:
        # Keyed on exactly who is selected, so changing the selection asks
        # for a fresh render rather than handing back the previous batch.
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
            # A rendered batch runs to tens of megabytes, and every distinct
            # tick-selection would otherwise leave its own copy behind for
            # the life of the session. Only the newest one is worth keeping.
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

        # Deliberately a checkbox, not an expander: Streamlit runs an
        # expander's body on every rerun, and each of these builds a full
        # download payload for every selected invoice (megabytes) before
        # anyone has asked for one.
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
        # A rendered batch can be tens of megabytes; don't keep it in memory
        # for a panel that has been dismissed.
        for key in [k for k in st.session_state if k.startswith("invoices_zip_png_")]:
            st.session_state.pop(key, None)
        _rerun()


def _credits_section() -> None:
    """Cancelled classes a student has already paid for, and what to do next.

    Nothing here needs touching in the normal case -- an open credit comes
    off the student's next invoice by itself. It exists for the rare time
    the money has to go back instead, and so the amounts are never
    invisible.
    """
    outstanding = db.get_credits(status="Open")
    settled = [c for c in db.get_credits() if c["Status"] != "Open"]
    total = sum(c["Amount"] for c in outstanding)

    st.markdown(
        f"#### Cancellation credits ({len(outstanding)} waiting, ${total:,.2f})"
    )

    if not outstanding and not settled:
        st.caption(
            "None. A class cancelled before its invoice goes out is simply "
            "left off it; one cancelled afterwards shows up here."
        )
        return

    if not st.checkbox("Show credits", key="credits_show"):
        return

    if outstanding:
        st.caption(
            "These come off each student's next invoice automatically. Refund "
            "one only if the money is actually going back to the parent."
        )
        for credit in outstanding:
            columns = st.columns([5, 1.4])
            when = credit["Class date"].strftime("%d %b %Y") if credit["Class date"] else ""
            columns[0].write(
                f"**{credit['Student']}** — {credit['Subject']} {when} "
                f"· **${credit['Amount']:,.2f}** · {credit['Reason']}"
            )
            with columns[1].popover("Refund", width="stretch"):
                st.write(
                    f"Pay ${credit['Amount']:,.2f} back to "
                    f"{credit['Student']} instead of deducting it?"
                )
                if st.button("Yes, refunded", key=f"refund_{credit['ID']}"):
                    outcome = db.refund_credit(credit["ID"])
                    if outcome == "refunded":
                        _flash(
                            f"Marked ${credit['Amount']:,.2f} refunded to "
                            f"{credit['Student']}."
                        )
                        _rerun()
                    else:
                        st.warning(f"Could not refund it ({outcome}).")

    if settled:
        with st.expander(f"Already settled ({len(settled)})"):
            st.dataframe(
                [
                    {
                        "Student": c["Student"],
                        "Subject": c["Subject"],
                        "Class date": c["Class date"],
                        "Amount": f"${c['Amount']:,.2f}",
                        "Settled": c["Status"],
                        "On": c["Settled"],
                    }
                    for c in settled[:200]
                ],
                width="stretch",
                hide_index=True,
            )


def _issued_lookup_section(counts: dict) -> None:
    """Find and reprint an invoice that has already gone out."""
    # Issued invoices are done with, so they go behind a fold -- but they
    # still have to be findable when a parent asks for a copy. Also loaded
    # on demand, for the same reason as the open list above.
    if not counts["issued_count"]:
        return
    st.markdown(f"#### Issued invoices ({counts['issued_count']})")
    if not st.checkbox("Look one up", key="invoice_show_issued"):
        return
    with st.container():
        issued = db.get_invoices(status="Issued")
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
            [
                {
                    "Invoice": f"#{item['Number']}",
                    "Student": item["Student"],
                    "Issued": item["Issued"],
                    "Classes": item["Classes"],
                    "Total (SGD)": f"{item['Total']:,.2f}",
                }
                for item in found[:100]
            ],
            width="stretch",
            hide_index=True,
        )
        if not found:
            return
        labels = {
            f"#{item['Number']} · {item['Student']}": item["ID"] for item in found[:100]
        }
        chosen = st.selectbox("Open a copy", list(labels), key="issued_pick")
        detail = db.get_invoice(labels[chosen])
        _invoice_table(detail)
        _invoice_download(detail, "dl_issued")



def _unpriced_warning(year: int, month: int) -> None:
    """Catch $0 invoices before they are sent, not after.

    Prices live in the Teachers tab, but the moment anyone finds out a
    subject has no price is here, on the way to billing it.  So the fix is
    offered where the problem shows up rather than in the tab that happens to
    own rates.
    """
    unpriced = db.get_unpriced_classes_for_month(year, month)
    if not unpriced:
        return
    lessons = sum(item["Sessions"] for item in unpriced)
    hours = sum(item["Hours"] for item in unpriced)
    st.warning(
        f"**{len(unpriced)} subject(s) have no real price for "
        f"{calendar.month_name[month]} {year}** — {lessons} class(es) "
        f"({hours:,.1f}h) would go out at the ${db.UNSET_RATE:,.2f}/h "
        "placeholder an import leaves behind, or at nothing at all where no "
        "price was ever set. Set the prices first."
    )
    if st.checkbox(
        f"Set prices for the {len(unpriced)} unpriced subject(s)",
        key=f"fix_prices_{year}_{month}",
    ):
        _bulk_rate_section(
            unpriced, year, month,
            base=f"unpriced_{year}_{month}",
            heading="",
            show_teacher=len({item["Teacher ID"] for item in unpriced}) > 1,
        )


def _past_invoices_section(year: int, month: int, counts: dict) -> None:
    """Everything that is reference rather than workflow, kept out of the way.

    Looking an old invoice up, browsing what is still accumulating, checking
    outstanding credit -- all useful, none of it part of "bill this month and
    send it", which is what the tab is for.
    """
    st.divider()
    if not st.checkbox(
        "Past invoices, credits and lookups", key="invoice_reference_open"
    ):
        return

    st.markdown(f"###### Still accumulating, any month ({counts['open_count']})")
    open_ones = db.get_invoices(status="Open")
    if not open_ones:
        st.caption("Nothing accumulating right now.")
    else:
        # Each row carries a ready-to-download invoice, and a download
        # button's file has to be built up front -- every invoice embeds the
        # letterhead image, so a few hundred rows is several megabytes of
        # payload and as many widgets, which takes Streamlit a long visible
        # moment to put on screen.  Hence search, and a cap.
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

        # Parent/phone for the printable copy's "Billed to" line -- fetched
        # once for everyone here rather than re-querying per invoice.
        contacts = {
            item["ID"]: (item["Parent"], item["Phone"]) for item in db.get_all_students()
        }
        for invoice in shown_open:
            with st.expander(
                f"{invoice['Student']} — {invoice['Classes']} class(es), "
                f"${invoice['Total']:,.2f}"
            ):
                _invoice_table(invoice)
                # No "bill the whole thing" button. An invoice covers one
                # month, so a draft holding several is billed a month at a
                # time from the list at the top of this tab, on that month.
                st.caption(
                    "Billed from the month's list above — pick the month at "
                    "the top of this tab and send it there, so each invoice "
                    "covers one month."
                )
                columns = st.columns([1.4, 1, 4])
                with columns[0]:
                    parent, phone = contacts.get(invoice["Student ID"], ("", ""))
                    detail = {**invoice, "Parent": parent, "Phone": phone}
                    _invoice_download(detail, f"dl_{invoice['ID']}")
                with columns[1].popover("Discard", width="stretch"):
                    st.write(
                        "Removes this draft. The classes stay, and they will be "
                        "picked up again the next time the roster changes."
                    )
                    if st.button("Discard it", key=f"del_inv_{invoice['ID']}"):
                        db.delete_invoice(invoice["ID"])
                        _rerun()

    _credits_section()
    _issued_lookup_section(counts)


def _send_invoices(invoice_ids: list[int], year: int, month: int) -> None:
    """Bill the selected students for this month, then image the results.

    One action, because that is the whole job -- nobody wants an issued
    invoice they cannot send.  Billing is committed first and on its own: if
    the image renderer is missing or fails, the invoices are still properly
    issued and the panel below offers the other formats.
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

    details = [detail for detail in (db.get_invoice(i) for i in issued_ids) if detail]
    if not details:
        _rerun()
    token = "_".join(str(d["ID"]) for d in details)
    zip_key = f"invoices_zip_png_{token}"
    # A rendered batch runs to tens of megabytes; only the newest is worth
    # keeping in session state.
    for stale in [k for k in st.session_state if k.startswith("invoices_zip_png_")]:
        st.session_state.pop(stale, None)

    # Roughly six tenths of a second per invoice, so a full month is the
    # better part of a minute -- shown moving, never as a page that has hung.
    progress = st.progress(0.0, text=f"Rendering {len(details)} {_send_noun()}(s)…")
    try:
        st.session_state[zip_key] = _invoices_zip(
            details, as_images=True,
            on_progress=lambda done, total: progress.progress(
                done / total, text=f"Rendering {_send_noun()} {done} of {total}…"
            ),
        )
    except Exception as problem:  # noqa: BLE001 - surfaced in the panel below
        st.session_state[f"{zip_key}_error"] = str(problem)
    finally:
        progress.empty()

    # Pre-tick everyone just issued, so the panel below opens with the
    # download ready rather than asking for the selection all over again.
    for detail in details:
        st.session_state[f"ji_pick_{year}_{month}_{detail['ID']}"] = True
    _rerun()


def invoices_tab() -> None:
    st.subheader("Invoices")

    # The month leads and everything below is that month's: an invoice is
    # always *for* a month of classes, never a running total of everything
    # the academy has ever billed.
    year, month = _month_picker("invoice_month")
    period = f"{calendar.month_name[month]} {year}"
    summary = db.get_month_invoice_summary(year, month)
    counts = db.get_invoice_counts()

    if not counts["open_count"] and not counts["issued_count"]:
        st.info("No invoices yet — they appear as soon as a schedule is imported.")
        return

    # One line, not a four-metric dashboard: there is only ever one question
    # on this screen -- how much is there to send this month.
    st.markdown(
        f"### {period} — {summary['to_bill_count']} to bill, "
        f"${summary['to_bill_value']:,.2f}"
    )
    trail = f"{summary['billed_count']} already sent (${summary['billed_value']:,.2f})"
    if summary.get("credit_outstanding"):
        trail += (
            f" · ${summary['credit_outstanding']:,.2f} credit for cancelled "
            "classes comes off automatically"
        )
    st.caption(trail)

    month_items = db.get_open_invoice_items_for_month(year, month)
    # Only warn when something is actually about to go out. A month that is
    # fully billed already has nothing at stake in its prices, and saying so
    # every time would just be noise above an empty list.
    if month_items:
        _unpriced_warning(year, month)

    if not month_items:
        st.info(
            f"Nothing left to bill for {period}. Import a schedule for a month "
            "that has not been billed yet and it will appear here."
        )
    else:
        def _apply_select_all() -> None:
            value = st.session_state.get("invoice_select_all", False)
            for row in month_items:
                st.session_state[f"inv_pick_{row['Invoice ID']}"] = value

        st.checkbox(
            "Select all", key="invoice_select_all", on_change=_apply_select_all
        )
        selected_ids = []
        for row in month_items:
            if st.checkbox(
                f"{row['Student']} — {row['Classes']} class(es) this month, "
                f"${row['Month Amount']:,.2f}",
                key=f"inv_pick_{row['Invoice ID']}",
            ):
                selected_ids.append(row["Invoice ID"])

        chosen_total = sum(
            row["Month Amount"] for row in month_items
            if row["Invoice ID"] in selected_ids
        )
        if st.button(
            f"Send {len(selected_ids)} invoice(s) — ${chosen_total:,.2f}",
            type="primary",
            disabled=not selected_ids,
            key="invoice_bulk_issue",
            width="stretch",
            help="Bills this month only, then renders one image per student "
                 "ready to send on KakaoTalk.",
        ):
            _send_invoices(selected_ids, year, month)

    # Sits below the month's figures rather than on top of them, and belongs
    # to the month it was issued for.
    _just_issued_panel(year, month)
    _past_invoices_section(year, month, counts)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def _part_payment_form(unpaid: list[dict], year: int, month: int) -> None:
    """The rare case: a parent sends less than the invoice asks for.

    Kept behind a tick rather than beside every row -- almost every payment
    is the full amount, and putting an amount box on 80 rows would bury the
    one button that does the job.
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
            message = f"Recorded ${amount:,.2f} from {row['Student']}."
            if short > 0.005:
                message += (
                    f" ${short:,.2f} still owing — they stay on the list until "
                    "it comes in."
                )
            _flash(message)
            _rerun()
        else:
            st.warning(f"Could not record that ({outcome}).")


def _paid_list(paid: list[dict], year: int, month: int) -> None:
    """Who has already paid, and an undo for the inevitable mis-tick."""
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
        amount = row["Paid amount"] if row["Paid amount"] is not None else row["Total"]
        line = (
            f"**{row['Student']}** — ${amount:,.2f} on "
            f"{row['Paid on']:%d %b %Y}"
        )
        if row["Note"]:
            line += f" · _{row['Note']}_"
        columns[0].markdown(line)
        if columns[1].button("Undo", key=f"unpay_{row['ID']}", width="stretch"):
            db.unmark_invoice_paid(row["ID"])
            _flash(f"{row['Student']} put back to unpaid.", "warning")
            _rerun()
    if len(shown) > PAID_ROWS:
        st.caption(f"Showing {PAID_ROWS} of {len(shown)} — search above to narrow it down.")


def payments_tab() -> None:
    st.subheader("Payments")
    st.caption(
        "Tick the parents who have paid and press the button. Payment belongs "
        "to the whole invoice, so one tick settles every class on it."
    )

    year, month = _month_picker("payments_month")
    period = f"{calendar.month_name[month]} {year}"
    data = db.get_invoice_payments(year, month)
    unpaid, paid = data["unpaid"], data["paid"]

    if not unpaid and not paid:
        st.info(
            f"No invoices covering {period} yet. They appear here once "
            "you have sent them from the Invoices screen."
        )
        return

    outstanding = round(sum(row["Owing"] for row in unpaid), 2)
    collected = round(
        sum(r["Paid amount"] if r["Paid amount"] is not None else r["Total"] for r in paid), 2
    )
    st.markdown(f"### {period} — {len(unpaid)} to collect, ${outstanding:,.2f} outstanding")
    trail = f"{len(paid)} paid (${collected:,.2f}) · due {data['due']:%d %b %Y}"
    late = [row for row in unpaid if row["Overdue"] > 0]
    if late:
        trail += f" · **{len(late)} overdue**"
    st.caption(trail)

    st.divider()
    if not unpaid:
        st.success(f"Everyone has paid for {period}.")
    else:
        def _apply_select_all() -> None:
            value = st.session_state.get(f"pay_all_{year}_{month}", False)
            for row in unpaid:
                st.session_state[f"pay_pick_{row['ID']}"] = value

        st.markdown(f"###### Awaiting payment ({len(unpaid)})")
        st.checkbox(
            "Select all", key=f"pay_all_{year}_{month}", on_change=_apply_select_all
        )
        picked = []
        for row in unpaid:
            bits = [f"{row['Student']} — ${row['Owing']:,.2f}"]
            if row["Owing"] < row["Total"] - 0.005:
                bits.append(f"part paid, ${row['Total']:,.2f} invoice")
            bits.append(f"invoice #{row['Number']}")
            if row.get("Covers"):
                # Billed whole rather than a month at a time, so the amount
                # beside their name is not only this month's classes.
                bits.append(f"covers {row['Covers']}")
            if row["Overdue"]:
                bits.append(f"**{row['Overdue']} days overdue**")
            if st.checkbox(" · ".join(bits), key=f"pay_pick_{row['ID']}"):
                picked.append(row)

        total_picked = round(sum(row["Owing"] for row in picked), 2)
        columns = st.columns([1, 1])
        when = columns[0].date_input(
            "Date received", value=dt.date.today(), key=f"pay_on_{year}_{month}"
        )
        note = columns[1].text_input(
            "Note (optional)", key=f"pay_note_{year}_{month}",
            placeholder="e.g. bank transfer, paid at desk",
            help="Saved against every invoice you tick.",
        )
        if st.button(
            f"Mark {len(picked)} paid in full — ${total_picked:,.2f}",
            type="primary",
            disabled=not picked,
            width="stretch",
            key=f"pay_confirm_{year}_{month}",
        ):
            done = failed = 0
            for row in picked:
                if db.mark_invoice_paid(row["ID"], paid_on=when, note=note) in (
                    "paid", "part paid"
                ):
                    done += 1
                    st.session_state.pop(f"pay_pick_{row['ID']}", None)
                else:
                    failed += 1
            st.session_state.pop(f"pay_all_{year}_{month}", None)
            if failed:
                st.warning(f"{failed} could not be recorded.")
            if done:
                _flash(f"Recorded {done} payment(s) — ${total_picked:,.2f} for {period}.")
                _rerun()

        _part_payment_form(unpaid, year, month)

    st.divider()
    _paid_list(paid, year, month)


def data_tab() -> None:
    st.subheader("Data")
    st.caption(
        "Money here is what was actually invoiced — read off the issued "
        "invoices, so it never moves when a rate is changed later. A month "
        "shows nothing until its invoices go out."
    )

    year, month = _month_picker("data_month")
    label = f"{calendar.month_name[month]} {year}"

    trend = db.get_teacher_year_trend(year, month)
    if not trend:
        st.info(f"No classes recorded in {year} up to the end of {label}.")
        return

    names = sorted({row["Teacher"] for row in trend})
    everyone = st.checkbox(
        f"All teachers ({len(names)})", value=True, key="data_all_teachers"
    )
    picked = st.multiselect(
        "Compare teachers",
        options=names,
        default=names,
        key="data_teachers",
        disabled=everyone,
        help="Untick 'All teachers' to compare a few side by side.",
    )
    chosen = set(names) if everyone else set(picked)
    if not chosen:
        st.info("Pick at least one teacher to compare.")
        return

    frame = pd.DataFrame([row for row in trend if row["Teacher"] in chosen])
    order = [calendar.month_abbr[m] for m in range(1, month + 1)]

    st.markdown(f"#### Invoiced by month — January to {calendar.month_name[month]} {year}")
    st.altair_chart(
        alt.Chart(frame)
        .mark_line(point=True)
        .encode(
            x=alt.X("Month name:N", sort=order, title=None),
            y=alt.Y("Invoiced:Q", title="Invoiced", axis=alt.Axis(format="$,.0f")),
            color=alt.Color("Teacher:N", legend=alt.Legend(title="Teacher")),
            tooltip=[
                alt.Tooltip("Teacher:N"),
                alt.Tooltip("Month name:N", title="Month"),
                alt.Tooltip("Invoiced:Q", format="$,.2f"),
                alt.Tooltip("Hours:Q", title="Hours taught"),
            ],
        )
        .properties(height=320),
        width="stretch",
    )
    st.caption(
        f"{year} only — a year is a trend of its own, so December is never "
        "drawn beside the following January."
    )

    st.divider()
    # Both this and the trend price their classes through the same helper, so
    # the figures below always match the last point on the line above.
    stats = [
        row
        for row in db.get_teacher_month_stats(year, month)
        if row["Teacher"] in chosen
    ]
    if not stats:
        st.info(f"None of the teachers selected taught in {label}.")
        return

    snapshot = pd.DataFrame(stats)
    tooltip = [
        alt.Tooltip("Teacher:N"),
        alt.Tooltip("Invoiced:Q", format="$,.2f"),
        alt.Tooltip("Hours:Q", title="Hours taught"),
        alt.Tooltip("Students:Q"),
    ]

    st.markdown(f"#### {label}")
    columns = st.columns(3)
    columns[0].metric("Invoiced", f"${snapshot['Invoiced'].sum():,.2f}")
    columns[1].metric("Hours taught", f"{snapshot['Hours'].sum():,.2f}")
    columns[2].metric("Teachers", len(snapshot))

    st.altair_chart(
        alt.Chart(snapshot)
        .mark_arc(innerRadius=60)
        .encode(
            theta=alt.Theta("Invoiced:Q"),
            color=alt.Color("Teacher:N", legend=alt.Legend(title="Teacher")),
            tooltip=tooltip,
        ),
        width="stretch",
    )

    columns = st.columns(2)
    for column, field, heading in (
        (columns[0], "Hours", "Hours taught"),
        (columns[1], "Students", "Unique students"),
    ):
        with column:
            st.markdown(f"##### {heading}")
            st.altair_chart(
                alt.Chart(snapshot)
                .mark_bar()
                .encode(
                    x=alt.X("Teacher:N", sort="-y", title=None),
                    y=alt.Y(f"{field}:Q", title=None),
                    tooltip=tooltip,
                ),
                width="stretch",
            )

    st.dataframe(
        [
            {
                "Teacher": row["Teacher"],
                "Invoiced": f"${row['Invoiced']:,.2f}",
                "Hours taught": row["Hours"],
                "Unique students": row["Students"],
            }
            for row in sorted(stats, key=lambda r: r["Invoiced"], reverse=True)
        ],
        width="stretch",
        hide_index=True,
    )


# ---------------------------------------------------------------------------
# Payment reminders
# ---------------------------------------------------------------------------


def reminders_tab() -> None:
    st.subheader("Payment reminders")
    st.caption(
        "An invoice is due on the 20th of the month after its classes — "
        "January's is chased from 20 February. One row per unpaid invoice; "
        "record payment on the Payments screen."
    )

    today = dt.date.today()
    all_reminders = db.get_payment_reminders(today)
    if not all_reminders:
        st.success("Nothing overdue — every issued invoice has been paid.")
        return

    def _due_this_month(item: dict) -> bool:
        return item["Due"].year == today.year and item["Due"].month == today.month

    reminders = [item for item in all_reminders if _due_this_month(item)]
    older = [item for item in all_reminders if not _due_this_month(item)]
    if older:
        owed = sum(item["Amount"] for item in older)
        st.warning(
            f"{len(older)} invoice(s) from before this month are still unpaid "
            f"too — ${owed:,.2f}. {calendar.month_name[today.month]} isn't "
            "the whole picture."
        )
    if not reminders:
        st.success("Nothing newly overdue this month.")
        show = older
        st.markdown("###### Everything still outstanding")
    else:
        show = reminders
        strip = st.columns(3)
        strip[0].metric("Invoices due", len(show))
        strip[1].metric("Owed", f"${sum(i['Amount'] for i in show):,.2f}")
        strip[2].metric(
            "Longest overdue", f"{max(i['Days overdue'] for i in show)} days"
        )

    st.dataframe(
        [
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
        ],
        width="stretch",
        hide_index=True,
    )


st.title("KS Academia")
# Above the tabs, so a confirmation from any tab's last action is visible
# wherever the rerun lands.
_show_flash()
_offer_billing()
_floating_back_to_top()
# A segmented control rather than st.tabs, for two reasons that both bit
# this app: st.tabs runs *every* tab's body on every interaction anywhere in
# the app, so the slowest screen taxed all the others; and any st.rerun()
# threw you back to the first tab, which made every confirmation land
# somewhere you were not looking. With the choice held in session state, one
# screen renders and reruns stay put.
SECTIONS = {
    "Teachers": teachers_tab,
    "Students": students_tab,
    "Timetable": timetable_tab,
    "Invoices": invoices_tab,
    "Payments": payments_tab,
    "Data": data_tab,
    "Reminders": reminders_tab,
}

# A jump requested by another screen (the "Bill <month>" button after an
# import) has to land before the selector is created -- Streamlit refuses a
# write to a widget's key once that widget exists in the same run.
_pending = st.session_state.pop("goto_section", None)
if _pending in SECTIONS:
    st.session_state["active_section"] = _pending

# Seeded here rather than through the widget's `default=`: passing a default
# *and* writing the key from elsewhere (the jump below) is the combination
# Streamlit warns about, and session state alone does the same job.
st.session_state.setdefault("active_section", next(iter(SECTIONS)))
_section = st.segmented_control(
    "Section",
    list(SECTIONS),
    key="active_section",
    label_visibility="collapsed",
)
if _section is None:
    # Clicking the already-selected segment deselects it. Bounce back to
    # where we were rather than showing a blank page -- routed through
    # goto_section because Streamlit refuses a write to a widget's own key
    # once that widget has been instantiated this run.
    st.session_state["goto_section"] = st.session_state.get(
        "last_section", next(iter(SECTIONS))
    )
    _rerun()
st.session_state["last_section"] = _section

st.divider()
# `_section or ...` is belt-and-braces: _rerun() above raises under Streamlit
# so None never reaches here, but indexing by None would be a hard crash if
# that ever stopped being true.
SECTIONS[_section or next(iter(SECTIONS))]()
# Outside the screen function, so one that bails out early -- "nothing to
# show for this month" and the like -- still gets a back-to-top at the foot
# of whatever it did render.
st.divider()
_inline_back_to_top()
