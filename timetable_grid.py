"""Clickable month grid.

Draws the timetable and returns the id of any class clicked, so the editor
below the grid follows the pointer instead of making the user find the same
class twice.

A *subject* is the recurring thing a teacher teaches; a *class* is one dated
sitting of it. The grid shows classes, coloured by their subject.

The frontend is ``grid_component/index.html`` -- plain HTML, CSS and
JavaScript, no build step.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import streamlit.components.v1 as components

__all__ = ["render_month_grid", "lessons_to_payload"]

COMPONENT_DIR = Path(__file__).resolve().parent / "grid_component"

_component = None


def _declare():
    global _component
    if _component is None:
        if not COMPONENT_DIR.is_dir():
            raise FileNotFoundError(
                f"Missing {COMPONENT_DIR}. The grid_component folder, with "
                "index.html inside it, must sit next to timetable_grid.py."
            )
        _component = components.declare_component("ks_month_grid", path=str(COMPONENT_DIR))
    return _component


def _as_time(value: Any) -> dt.time:
    if isinstance(value, dt.time):
        return value
    if isinstance(value, dt.datetime):
        return value.time()
    parts = [int(part) for part in str(value).split(":")[:2]]
    return dt.time(parts[0], parts[1] if len(parts) > 1 else 0)


def _as_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def lessons_to_payload(
    lessons: list[dict[str, Any]],
    class_notes: dict[int, str] | None = None,
    student_notes: dict[int, str] | None = None,
    unpaid: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Shape classes for the frontend, naming only students with something set."""
    class_notes = class_notes or {}
    student_notes = student_notes or {}
    unpaid = unpaid or {}
    payload = []

    for lesson in lessons:
        # Every student on the card, not only the exceptions: the roster is
        # what the card is for, and a plain name reads as "was in the room".
        marked = []
        for row in lesson.get("Attendance") or []:
            note = row.get("note") or student_notes.get(row.get("student_id"), "")
            marked.append(
                {
                    "name": row.get("student_name", ""),
                    "online": bool(row.get("is_online")),
                    "recording": bool(row.get("has_recording")),
                    "cancelled": bool(row.get("is_cancelled")),
                    "unpaid": not bool(row.get("is_paid")),
                    "note": note,
                }
            )

        payload.append(
            {
                "id": lesson["ID"],
                "date": _as_date(lesson["Date"]).isoformat(),
                "start": _as_time(lesson["Start"]).strftime("%H:%M"),
                "end": _as_time(lesson["End"]).strftime("%H:%M"),
                "class_name": lesson.get("Class", ""),
                "colour": lesson.get("Colour") or "#8a93a6",
                "students": lesson.get("Students", 0),
                "cancelled": str(lesson.get("Status", "")).lower() == "cancelled",
                "note": lesson.get("Note") or "",
                "class_note": class_notes.get(lesson.get("Class ID"), ""),
                "unpaid": unpaid.get(lesson["ID"], 0),
                "marked": marked,
            }
        )
    return payload


def render_month_grid(
    lessons: list[dict[str, Any]],
    *,
    selected: int | None = None,
    class_notes: dict[int, str] | None = None,
    student_notes: dict[int, str] | None = None,
    unpaid: dict[int, int] | None = None,
    key: str = "month_grid",
) -> dict | None:
    """Draw the grid; return the click, if any.

    The return carries a nonce as well as the class id, so clicking the same
    class twice is two distinct values.  Returning the id alone made a repeat
    click look unchanged to Streamlit, which then did not rerun.
    """
    value = _declare()(
        lessons=lessons_to_payload(lessons, class_notes, student_notes, unpaid),
        selected=selected,
        key=key,
        default=None,
    )
    if isinstance(value, dict) and value.get("lesson_id"):
        return value
    return None
