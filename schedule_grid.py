"""Draws the clickable month grid on the Schedule screen.

Clicking a class sends its id back, so the class editor under the grid
opens that class. The grid shows classes (one dated lesson each),
coloured by subject.

The front end is grid_component/index.html: plain HTML, CSS and
JavaScript, with no build step.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit.components.v1 as components

from db import as_date, as_time

__all__ = ["render_month_grid", "lessons_to_payload"]

COMPONENT_DIR = Path(__file__).resolve().parent / "grid_component"

_component = None


def _declare():
    """Register the grid component the first time it is needed, and return it."""
    global _component
    if _component is None:
        if not COMPONENT_DIR.is_dir():
            raise FileNotFoundError(
                f"Missing {COMPONENT_DIR}. The grid_component folder, with "
                "index.html inside it, must sit next to schedule_grid.py."
            )
        _component = components.declare_component("ks_month_grid", path=str(COMPONENT_DIR))
    return _component


def lessons_to_payload(
    lessons: list[dict[str, Any]],
    class_notes: dict[int, str] | None = None,
    student_notes: dict[int, str] | None = None,
    unpaid: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    """Turn classes into the data the grid draws: times, colour, notes and every student with their flags.
    """
    class_notes = class_notes or {}
    student_notes = student_notes or {}
    unpaid = unpaid or {}
    payload = []

    for lesson in lessons:
        # List every student, not only those marked online, recording or cancelled.
        # A plain name means the student was in class.
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
                "date": as_date(lesson["Date"]).isoformat(),
                "start": as_time(lesson["Start"]).strftime("%H:%M"),
                "end": as_time(lesson["End"]).strftime("%H:%M"),
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
    """Draw the grid and return the class that was clicked, if any.

    The value includes a random token as well as the class id, so clicking
    the same class twice still counts as a new click and Streamlit reruns.
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
