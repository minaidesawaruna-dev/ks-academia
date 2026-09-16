"""Verify the schedule parser: pure functions, then a real workbook."""
from __future__ import annotations

import datetime as dt
import io
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from openpyxl import Workbook  # noqa: E402

import schedule_parser as sp  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name, fn):
    try:
        results.append((name, True, fn() or ""))
    except Exception as exc:  # noqa: BLE001
        results.append((name, False, f"{type(exc).__name__}: {exc}"))


# ----------------------------------------------------------- pure functions
def t_time_ranges():
    cases = {
        "9am-11am": (dt.time(9, 0), dt.time(11, 0)),
        "9:30am-11:00am": (dt.time(9, 30), dt.time(11, 0)),
        "2pm-3:30pm": (dt.time(14, 0), dt.time(15, 30)),
        "10-11:30am": (dt.time(10, 0), dt.time(11, 30)),
    }
    for text, want in cases.items():
        got = sp.parse_time_range(text)
        assert got == want, f"{text!r} -> {got}, wanted {want}"
    assert sp.parse_time_range("not a time") is None, "accepted nonsense"
    return f"{len(cases)} formats parsed, nonsense rejected"


def t_status_vocabulary():
    cases = {
        "Cancelled": sp.CANCELLED,
        "cancelled online class": sp.CANCELLED,   # cancel wins over online
        "Online": sp.ONLINE,
        "zoom": sp.ONLINE,
        "Recording": sp.RECORDING,
        "취소": sp.CANCELLED,                      # Korean: cancelled
        "온라인": sp.ONLINE,                        # Korean: online
        "녹화": sp.RECORDING,                       # Korean: recording
    }
    for text, want in cases.items():
        got, _ = sp.status_from_line(text)
        assert got == want, f"{text!r} -> {got}, wanted {want}"
    return f"{len(cases)} labels incl. Korean, precedence correct"


def t_month_year_inference():
    for sheet, month in {"Aug": 8, "August": 8, "Sep": 9, "July": 7,
                         "Jan": 1, "Dec": 12}.items():
        got = sp.infer_month(sheet)
        assert got == month, f"{sheet!r} -> {got}, wanted {month}"
    assert sp.infer_year("Aug", 2026) == 2026, "fallback year ignored"
    return "6 month names + year fallback"


def t_name_normalising():
    a = sp.normalise_name("  Nam   Jihoon ")
    b = sp.normalise_name("Nam Jihoon")
    assert a == b, f"{a!r} != {b!r}"
    assert sp.looks_like_name("Nam Jihoon"), "rejected a real name"
    # looks_like_name is a low-level predicate: times are removed upstream by
    # _is_time_line and status words by status_from_line, both of which
    # parse_cell applies first. Test those real guards, not a contract this
    # predicate does not claim.
    assert sp._is_time_line("9am-11am"), "time line not recognised"
    assert not sp._is_time_line("Nam Jihoon"), "name mistaken for a time"
    status, leftover = sp.status_from_line("Recording")
    assert status == sp.RECORDING and not leftover.strip(), (status, leftover)
    cell = sp.parse_cell("Recording\nPark Sohee")
    names = [e["name"] for e in cell["entries"]]
    assert names == ["Park Sohee"], f"status leaked into names: {names}"
    assert cell["entries"][0]["status"] == sp.RECORDING, cell["entries"]
    return "times and status words never become student names"


def t_parse_cell():
    cell = sp.parse_cell("G11 Chem HL B\n9am-11am\nNam Jihoon\nOh Minseok")
    assert cell["time_range"] == (dt.time(9, 0), dt.time(11, 0)), cell
    assert cell["class_lines"] == ["G11 Chem HL B"], cell
    names = [e["name"] for e in cell["entries"]]
    assert names == ["Nam Jihoon", "Oh Minseok"], names
    assert all(e["status"] is None for e in cell["entries"]), cell["entries"]
    return f"subject, time and {len(names)} students split out of one cell"


# ------------------------------------------------------------- real workbook
def build_workbook() -> io.BytesIO:
    """A calendar-grid sheet in the shape the teachers actually send."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Aug"

    # Header row: a time-axis header, then day labels merged over two columns
    # (class column + status column), exactly as the docstring describes.
    ws.cell(row=1, column=1, value="Time")
    ws.cell(row=1, column=2, value="3(MON)")
    ws.merge_cells(start_row=1, start_column=2, end_row=1, end_column=3)
    ws.cell(row=1, column=4, value="5(WED)")
    ws.merge_cells(start_row=1, start_column=4, end_row=1, end_column=5)

    # 30-minute time axis down column A.
    times = ["9:00", "9:30", "10:00", "10:30", "11:00", "11:30",
             "12:00", "12:30", "13:00", "13:30"]
    for offset, label in enumerate(times):
        ws.cell(row=2 + offset, column=1, value=label)

    # Monday: one class, with a student marked as a recording watcher.
    ws.cell(row=2, column=2,
            value="G11 Chem HL B\n9am-11am\nNam Jihoon\nOh Minseok\nPark Sohee")
    ws.merge_cells(start_row=2, start_column=2, end_row=5, end_column=2)
    ws.cell(row=2, column=3, value="Recording\nPark Sohee")
    ws.merge_cells(start_row=2, start_column=3, end_row=5, end_column=3)

    # Wednesday: a different class, one cancellation and one online student.
    ws.cell(row=6, column=4,
            value="G11 Maths SL\n11am-12:30pm\nSeo Yerin\nChoi Doyun\n수민")
    ws.merge_cells(start_row=6, start_column=4, end_row=8, end_column=4)
    ws.cell(row=6, column=5, value="Online\nChoi Doyun\nCancelled\nSeo Yerin")
    ws.merge_cells(start_row=6, start_column=5, end_row=8, end_column=5)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


WB = build_workbook().getvalue()


def t_sheet_names():
    names = sp.get_sheet_names(io.BytesIO(WB))
    assert names == ["Aug"], names
    return f"{names}"


def t_parse_schedule():
    out = sp.parse_schedule(io.BytesIO(WB), "Aug", 2026)
    assert out["month"] == 8, out["month"]
    assert out["session_count"] >= 2, f"only {out['session_count']} sessions"
    subjects = {s.get("class_name") or s.get("subject") for s in out["sessions"]}
    assert any("Chem" in str(x) for x in subjects), subjects
    assert any("Maths" in str(x) for x in subjects), subjects
    return (f"{out['session_count']} sessions, "
            f"{out['unique_student_count']} students, "
            f"{out['warning_count']} warnings")


def t_dates_resolved():
    out = sp.parse_schedule(io.BytesIO(WB), "Aug", 2026)
    dates = sorted({s["date"] for s in out["sessions"] if s.get("date")})
    assert dates, "no dates resolved"
    assert all(d.year == 2026 and d.month == 8 for d in dates), dates
    assert dt.date(2026, 8, 3) in dates, f"Monday the 3rd missing: {dates}"
    assert dt.date(2026, 8, 5) in dates, f"Wednesday the 5th missing: {dates}"
    return f"day labels resolved to {[str(d) for d in dates]}"


def t_times_resolved():
    out = sp.parse_schedule(io.BytesIO(WB), "Aug", 2026)
    chem = [s for s in out["sessions"]
             if "Chem" in str(s.get("class_name") or s.get("subject"))][0]
    assert chem.get("start_time") == dt.time(9, 0), chem.get("start_time")
    assert chem.get("end_time") == dt.time(11, 0), chem.get("end_time")
    return "9am-11am read off the class cell"


def t_statuses_applied():
    out = sp.parse_schedule(io.BytesIO(WB), "Aug", 2026)
    found = {}
    for session in out["sessions"]:
        for att in session["attendance"]:
            found[att["student_name"]] = att.get("status")
    assert found.get("Park Sohee") == sp.RECORDING, found
    assert found.get("Choi Doyun") == sp.ONLINE, found
    assert found.get("Seo Yerin") == sp.CANCELLED, found
    assert found.get("Nam Jihoon") == sp.ATTENDING, found
    return ("recording/online/cancelled/attending all assigned to the "
            "right students")


def t_korean_student_parsed():
    out = sp.parse_schedule(io.BytesIO(WB), "Aug", 2026)
    names = {a["student_name"] for s in out["sessions"] for a in s["attendance"]}
    assert "수민" in names, f"Korean student lost: {sorted(names)}"
    return "Korean student name read out of the grid"


def t_parse_workbook_multi():
    out = sp.parse_workbook(io.BytesIO(WB), ["Aug"], 2026)
    assert out["session_count"] >= 2, out["session_count"]
    out2 = sp.parse_workbook(io.BytesIO(WB), ["Aug", "Nope"], 2026)
    assert any("not found" in str(w) for w in out2["warnings"]), out2["warnings"]
    return "multi-sheet merge works; missing sheet warns instead of crashing"


def t_backfill_suggestions():
    import schedule_backfill as sb
    out = sp.parse_schedule(io.BytesIO(WB), "Aug", 2026)
    merges = sb.suggest_subject_merges(out["sessions"])
    matches = sb.suggest_student_matches(out["sessions"])
    assert isinstance(merges, list) and isinstance(matches, list)
    return f"{len(merges)} subject merges, {len(matches)} student matches suggested"


# ------------------------------------------------ several months on one sheet
GRID_TIMES = [dt.time(9, 0), dt.time(9, 30), dt.time(10, 0), dt.time(10, 30),
              dt.time(11, 0), dt.time(11, 30), dt.time(12, 0), dt.time(12, 30)]
LESSON = "G10 Math\n9am-11am\nNam Jihoon\nOh Minseok"


def _grid(ws, blocks):
    """Lay month blocks side by side, the way the whole-year sheets do.

    ``blocks`` is ``[(label, [(day_label, class_text), ...]), ...]``. Each
    block is a time-axis column followed by one column per day; the label
    goes in row 1 above the block, day labels in row 2, and class text on
    the 9:00 row. A label of ``None`` leaves that month unlabelled.
    """
    column = 1
    for label, days in blocks:
        if label is not None:
            ws.cell(row=1, column=column, value=label)
        for offset, value in enumerate(GRID_TIMES):
            ws.cell(row=3 + offset, column=column, value=value)
        column += 1
        for day_label, text in days:
            ws.cell(row=2, column=column, value=day_label)
            if text:
                ws.cell(row=3, column=column, value=text)
            column += 1
    return ws


def _book(*sheets):
    wb = Workbook()
    wb.remove(wb.active)
    for title, blocks in sheets:
        _grid(wb.create_sheet(title), blocks)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _dates(out):
    return sorted({s["date"] for s in out["sessions"]})


def _said(out, *words):
    texts = [w["message"] for w in out["warnings"]] + [
        w["message"] for s in out["sessions"] for w in s["warnings"]]
    return any(all(word in text for word in words) for text in texts)


def t_month_ranges_in_names():
    cases = {"Jan- Feb 2026": 1, "FEB-MAR 2026": 2, "May-Aug 2026": 5,
             "2026(1-7월)": 1, "Oct-Dec2022": 10, "2026.Aug": 8, "Sep26": 9,
             "Summary June": 6}
    for sheet, month in cases.items():
        got = sp.infer_month(sheet)
        assert got == month, f"{sheet!r} -> {got}, wanted {month}"
    try:
        sp.infer_month("2026")
    except ValueError:
        pass
    else:
        raise AssertionError("'2026' was read as a month")
    return f"{len(cases)} names; a range starts at its first month"


def t_month_labels():
    cases = {
        "JAN.2026": (1, 2026), " MARCH 2026": (3, 2026), "JUN.2026": (6, 2026),
        "JULY 2026(7/1-11일휴가)": (7, 2026), "AUG(8/26, 8/29~9/2)": (8, None),
        "APRIL / 5월2일-6월4일 한국출국": (4, None),
        "NOV.2025(12/6-1/9한국)": (11, 2025), "OCT 2025\nONLINE": (10, 2025),
        "8월": (8, None), "SEP": (9, None),
    }
    for text, want in cases.items():
        got = sp.read_month_label(text)
        assert got == want, f"{text!r} -> {got}, wanted {want}"
    for text in ("adr", "Jane", " ", "5월2일", "15(SAT)", "2026", None, 7):
        assert sp.read_month_label(text) is None, f"{text!r} read as a label"
    return f"{len(cases)} labels read; notes, names and day labels ignored"


def t_whole_year_sheet():
    book = _book(("2026", [("JAN.2026", [("3(SAT)", LESSON), ("5(MON)", None)]),
                           ("FEB", [("2(MON)", LESSON), ("7(SAT)", None)])]))
    out = sp.parse_workbook(io.BytesIO(book), ["2026"], 2025)
    want = [dt.date(2026, 1, 3), dt.date(2026, 2, 2)]
    assert _dates(out) == want, _dates(out)
    assert not _said(out, "but the sheet says"), out["warnings"]
    month = out["sheet_summaries"][0]["Month"]
    assert month == "Jan–Feb 2026", month
    return "a sheet named '2026' read by its month labels, year from the sheet"


def t_unlabelled_first_month():
    book = _book(("2026", [(None, [("3 SAT", LESSON)]),
                           ("FEB 2026", [("2(MON)", LESSON)]),
                           ("MARCH 2026", [("2(MON)", LESSON)])]))
    out = sp.parse_workbook(io.BytesIO(book), ["2026"], 2026)
    want = [dt.date(2026, 1, 3), dt.date(2026, 2, 2), dt.date(2026, 3, 2)]
    assert _dates(out) == want, _dates(out)
    assert _said(out, "January"), "unlabelled month not reported"
    return "columns before the first label taken as the month before it"


def t_multi_month_sheet_name():
    book = _book(("Jan- Feb 2026", [(None, [("11(SUN)", LESSON),
                                            ("2(MON)", LESSON)])]))
    out = sp.parse_workbook(io.BytesIO(book), ["Jan- Feb 2026"], 2026)
    want = [dt.date(2026, 1, 11), dt.date(2026, 2, 2)]
    assert _dates(out) == want, _dates(out)
    assert not _said(out, "but the sheet says"), out["warnings"]
    return "starts in January and rolls into February"


def t_leftover_year_block():
    book = _book(("2026", [("Jan 2025", [("4(SAT)", LESSON)]),
                           ("AUG.2026", [("1(SAT)", LESSON)])]),
                 ("2025", [("Jan 2025", [("4(SAT)", LESSON)]),
                           ("NOV.2025", [("1(SAT)", LESSON)])]))
    out = sp.parse_workbook(io.BytesIO(book), ["2026"], 2026)
    assert _dates(out) == [dt.date(2026, 8, 1)], _dates(out)
    assert _said(out, "Jan 2025", "skipped"), out["warnings"]
    out = sp.parse_workbook(io.BytesIO(book), ["2025"], 2026)
    assert _dates(out) == [dt.date(2025, 11, 1)], _dates(out)
    assert _said(out, "Jan 2025", "skipped as a"), out["warnings"]
    # Stranded mid-sheet it is only flagged: it may be real.
    book = _book(("2024", [("JAN 2024", [("6(SAT)", LESSON)]),
                           ("FEB 2024", [("3(SAT)", LESSON)]),
                           ("NOV 2024", [("2(SAT)", LESSON)])]))
    out = sp.parse_workbook(io.BytesIO(book), ["2024"], 2026)
    assert len(_dates(out)) == 3, _dates(out)
    assert _said(out, "FEB 2024", "check it"), out["warnings"]
    return "leftovers skipped: other-year, or stranded at a sheet's start"


def t_single_month_label_disagrees():
    book = _book(("Aug", [("JULY", [("3(MON)", LESSON)])]),
                 ("Jan", [("Jan 2025", [("5(Sun)", LESSON)])]))
    out = sp.parse_workbook(io.BytesIO(book), ["Aug"], 2026)
    assert _dates(out) == [dt.date(2026, 8, 3)], _dates(out)
    assert _said(out, "JULY"), "disagreeing label not reported"
    out = sp.parse_workbook(io.BytesIO(book), ["Jan"], 2026)
    assert _dates(out) == [dt.date(2025, 1, 5)], _dates(out)
    return "one-month sheet keeps its name's month; label supplies a missing year"


def t_not_a_schedule():
    wb = Workbook()
    wb.active.title = "학생 리스트"
    wb.active["A1"] = "Nam Jihoon"
    _grid(wb.create_sheet("Notes"), [(None, [("3(MON)", LESSON)])])
    buf = io.BytesIO()
    wb.save(buf)
    out = sp.parse_workbook(io.BytesIO(buf.getvalue()), ["학생 리스트", "Notes"], 2026)
    assert out["session_count"] == 0, out["session_count"]
    assert out["sheet_summaries"] == [], out["sheet_summaries"]
    assert len(out["warnings"]) == 2, out["warnings"]
    return "no month anywhere: skipped with a warning, left out of the summary"


def t_backwards_time():
    wb = Workbook()
    ws = _grid(wb.active, [(None, [("3(MON)", None), ("5(WED)", None)])])
    ws.title = "Aug"
    ws.cell(row=8, column=2, value="G11 Bio HL\n11.30pm-1.30pm\nNam Jihoon")
    ws.cell(row=3, column=3, value="G10 Chem\n5pm-3pm\nOh Minseok")
    buf = io.BytesIO()
    wb.save(buf)
    out = sp.parse_workbook(io.BytesIO(buf.getvalue()), ["Aug"], 2026)
    times = [(s["start_time"], s["end_time"]) for s in out["sessions"]]
    assert times == [(dt.time(11, 30), dt.time(13, 30))], times
    assert _said(out, "ends before it starts", "11:30"), out["warnings"]
    assert _said(out, "ends before it starts", "skipped"), out["warnings"]
    return "12-hour slip fixed from the grid row; the unfixable one refused"


def t_time_only_cell_skipped():
    book = _book(("Aug", [(None, [("3(MON)", "13-11")])]))
    out = sp.parse_workbook(io.BytesIO(book), ["Aug"], 2026)
    assert out["session_count"] == 0, out["sessions"]
    assert _said(out, "only a time"), out["warnings"]
    return "a bare time range with no class or students is not a lesson"


def t_year_from_weekdays():
    # 2(SUN) and 4(TUE) fall on those days in February 2025, not 2026.
    book = _book(("Feb", [(None, [("2(SUN)", LESSON), ("4(TUE)", LESSON),
                                  ("6(THU)", None)])]))
    out = sp.parse_workbook(io.BytesIO(book), ["Feb"], 2026)
    assert _dates(out) == [dt.date(2025, 2, 2), dt.date(2025, 2, 4)], _dates(out)
    assert _said(out, "fit 2025"), out["warnings"]
    assert not _said(out, "but the sheet says"), out["warnings"]
    # Labels that fit the form's year leave it alone.
    book = _book(("Feb", [(None, [("2(MON)", LESSON), ("3(TUE)", LESSON)])]))
    out = sp.parse_workbook(io.BytesIO(book), ["Feb"], 2026)
    assert _dates(out) == [dt.date(2026, 2, 2), dt.date(2026, 2, 3)], _dates(out)
    return "no year written: the weekdays pick it, not the upload form"


def t_roster_lines():
    def read(text):
        cell = sp.parse_cell(text)
        return [(e["name"], e.get("note")) for e in cell["entries"]], cell["notes"]
    got, _ = read("G8 Math\n4.30pm-6pm\nJihoon,yerin")
    assert got == [("Jihoon", None), ("yerin", None)], got
    got, _ = read("Math\n4pm-5pm\nG5 Sohee\nG7 Nam Jihoon")
    assert got == [("Sohee", "G5"), ("Nam Jihoon", "G7")], got
    got, _ = read("Lit\n5pm-7pm\nMinseok until 8pm\nOh Minseok/half")
    assert got == [("Minseok", "until 8pm"), ("Oh Minseok", "half")], got
    got, notes = read("Math\n4pm-5pm\nYerin,수민여행\n2명적용\n지훈한국")
    assert got == [("Yerin", None), ("수민", "여행")], got
    assert notes == ["2명적용", "지훈한국"], notes
    cell = sp.parse_cell("Math\n4pm-5pm\nSohee(아파서당일캔슬)\n수민병가\nYerin")
    found = [(e["name"], e["status"], e.get("note")) for e in cell["entries"]]
    assert found == [("Sohee", sp.CANCELLED, "아파서당일캔슬"),
                     ("수민", sp.CANCELLED, "병가"), ("Yerin", None, None)], found
    cell = sp.parse_cell("Lit\n5.30pm-7.30pm\nSohee(G8)\nMinseok(G6) ONLINE")
    found = [(e["name"], e["status"]) for e in cell["entries"]]
    assert found == [("Sohee(G8)", None), ("Minseok(G6)", sp.ONLINE)], found
    got, notes = read("G10 Math\n4.30pm-6.30pm\nJihoon,Yerin\n5pm-6.30pm\nSohee,Doyun")
    assert [n for n, _ in got] == ["Jihoon", "Yerin", "Sohee", "Doyun"], got
    assert notes == ["5pm-6.30pm"], notes
    got, _ = read("G8 Math\n4.30pm-6pm\nSohee,nam")
    assert got == [("Sohee Nam", None)], got
    return "commas split, but not a surname from its name; grades and notes taken off names"


def t_status_stays_with_student():
    def read(text):
        return [(e["name"], e["status"], e.get("note")) for e in sp.parse_cell(text)["entries"]]
    got = read("G8 Math\n9am-11am\nNam Jihoon(당일취소)\nSeo Yerin\nOh Minseok(ONLINE)\nChoi Doyun")
    assert got == [("Nam Jihoon", sp.CANCELLED, "당일취소"), ("Seo Yerin", None, None),
                   ("Oh Minseok", sp.ONLINE, None), ("Choi Doyun", None, None)], got
    got = read("G10 Econs\n1.30pm-3.30pm\nSohee(Recording)\nYerin\n수민결석\nMinseok")
    assert got == [("Sohee", sp.RECORDING, None), ("Yerin", None, None),
                   ("수민", sp.CANCELLED, None), ("Minseok", None, None)], got
    got = read("G8 Math\n5pm-7pm\nSohee (REC)\nYerin(Rec)\nMinseok(ONLIN)\nRebecca")
    assert got == [("Sohee", sp.RECORDING, None), ("Yerin", sp.RECORDING, None),
                   ("Minseok", sp.ONLINE, None), ("Rebecca", None, None)], got
    # A label on its own line still covers the students under it.
    got = read("G8\n9am-11am\nOnline\nChoi Doyun\nSeo Yerin\nCancelled\nNam Jihoon")
    assert got == [("Choi Doyun", sp.ONLINE, None), ("Seo Yerin", sp.ONLINE, None),
                   ("Nam Jihoon", sp.CANCELLED, None)], got
    return "a status on a student's line is theirs; a label line covers those under it"


def t_class_line_order():
    def split(text):
        cell = sp.parse_cell(text)
        return cell["class_lines"], [e["name"] for e in cell["entries"]]
    cases = {
        "11am-12.30pm\nUWC Lang&Lit Jihoon": (["UWC Lang&Lit"], ["Jihoon"]),
        "3.30pm-5pm\nG11 TOK\nSeo Yerin": (["G11 TOK"], ["Seo Yerin"]),
        "G6 Math 1:1\nSohee\n2pm-3.30pm": (["G6 Math 1:1"], ["Sohee"]),
        "11.30am-1.30pm\nNam Jihoon\nOh Minseok": ([], ["Nam Jihoon", "Oh Minseok"]),
        "G11 Econs HL\nSJII\n9am-11am\nNam Jihoon": (["G11 Econs HL", "SJII"], ["Nam Jihoon"]),
    }
    for text, want in cases.items():
        assert split(text) == want, (text, split(text))
    return "class after the time, and a student above it, both put right"


def t_two_classes_one_cell():
    cell = sp.parse_cell("P2 DRILL\n11.15am-2.15pm\nNam Jihoon\n\n"
                         "11.15am-1.15pm\nG11 Econs IA\nSeo Yerin")
    assert [e["name"] for e in cell["entries"]] == ["Nam Jihoon"], cell["entries"]
    assert cell["extra_classes"] and "G11 Econs IA" in cell["extra_classes"][0], cell
    return "a second class after an empty line is held back, not billed to the first"


def t_not_a_lesson():
    book = _book(("Aug", [(None, [("3(MON)", "Meeting with Nam Jihoon's mom\n9am-10am"),
                                  ("4(TUE)", "수학 보조 강사\n9am-12pm"),
                                  ("5(WED)", "consultation\nSeo Yerin\n9am-10am"),
                                  ("6(THU)", LESSON),
                                  ("7(FRI)", "9am-10am interview with Seo Yerin"),
                                  ("8(SAT)", "9am-10am TA")])]))
    out = sp.parse_workbook(io.BytesIO(book), ["Aug"], 2026)
    assert _dates(out) == [dt.date(2026, 8, 6)], _dates(out)
    skipped = [w for w in out["warnings"] if "not a lesson" in w["message"]
               or "rather than a lesson" in w["message"]]
    assert len(skipped) == 5, out["warnings"]
    return "meetings, consultations and staff shifts are not billed"


def t_unnamed_grouped():
    book = _book(("Aug", [(None, [("3(MON)", "9am-10am\nPark Sohee"),
                                  ("5(WED)", "9am-11am\nPark Sohee"),
                                  ("8(SAT)", "9am-11am\nNam Jihoon\nOh Minseok"),
                                  ("15(SAT)", "9am-11am\nOh Minseok\nSeo Yerin")])]))
    out = sp.parse_workbook(io.BytesIO(book), ["Aug"], 2026)
    names = sorted({s["class_name"] for s in out["sessions"]})
    want = [f"{sp.UNNAMED_CLASS} · Park Sohee", f"{sp.UNNAMED_CLASS} · Sat 09:00"]
    assert names == want, names
    return "one placeholder per student, or per weekly slot for a group"


def t_time_repairs():
    wb = Workbook()
    ws = _grid(wb.active, [(None, [("3(MON)", None), ("5(WED)", None)])])
    ws.title = "Aug"
    ws.cell(row=10, column=2, value="G9 Science\n12.30am-2.30pm\nNam Jihoon")
    ws.cell(row=3, column=3, value="G10 Science\n9am-8am\nOh Minseok")
    ws.merge_cells(start_row=3, start_column=3, end_row=6, end_column=3)
    buf = io.BytesIO()
    wb.save(buf)
    out = sp.parse_workbook(io.BytesIO(buf.getvalue()), ["Aug"], 2026)
    times = sorted((s["start_time"], s["end_time"]) for s in out["sessions"])
    want = [(dt.time(9, 0), dt.time(11, 0)), (dt.time(12, 30), dt.time(14, 30))]
    assert times == want, times
    return "a 14-hour typo and a backwards range fixed from the grid"


def t_generic_sheet_names():
    for name in ("시트3", "Sheet2"):
        try:
            sp.infer_month(name)
        except ValueError:
            continue
        raise AssertionError(f"{name!r} read as a month")
    return "'Sheet2' and '시트3' are not February and March"


def t_grade_prefix_match():
    import schedule_backfill as sb
    for name, want in {"g5 sohee": "sohee", "Grade 7 Nam Jihoon": "Nam Jihoon",
                       "Yerin": "Yerin", "G11": "G11"}.items():
        assert sb._without_grade(name) == want, (name, sb._without_grade(name))
    real = sb.db.get_all_students
    lesson = {"attendance": [{"student_name": "Sohee"}, {"student_name": "Yerin"}]}
    try:
        sb.db.get_all_students = lambda: [{"ID": 1, "Name": "G5 Sohee"}, {"ID": 2, "Name": "G6 Yerin"},
                                          {"ID": 3, "Name": "G7 Yerin"}]
        found = {m["parsed_name"]: (m["existing_id"], m["likely_same"]) for m in sb.suggest_student_matches([lesson])}
    finally:
        sb.db.get_all_students = real
    # One grade-apart match is taken as the same student by default; two are left to the admin.
    assert found == {"Sohee": (1, True), "Yerin": (2, False)}, found
    return "an existing 'G5 Sohee' is offered, and pre-selected, as the match for 'Sohee'"


def t_time_shares_a_line():
    def read(text):
        cell = sp.parse_cell(text)
        return (cell["class_lines"], cell["time_range"],
                [(e["name"], e["status"]) for e in cell["entries"]])
    t = dt.time
    cases = {
        "G12 Chem HL  1.30pm-3.30pm\nNam Jihoon\nOh Minseok":
            (["G12 Chem HL"], (t(13, 30), t(15, 30)), [("Nam Jihoon", None), ("Oh Minseok", None)]),
        "Y5 ACSI Chemi 10am-11.30am Seo Yerin":
            (["Y5 ACSI Chemi"], (t(10), t(11, 30)), [("Seo Yerin", None)]),
        "관리수업11am-1pm": (["관리수업"], (t(11), t(13)), []),
        "G6-7 Math\n2pm-4pm   Nam Jihoon\nChoi Doyun":
            (["G6-7 Math"], (t(14), t(16)), [("Nam Jihoon", None), ("Choi Doyun", None)]),
        "Y5 Chemi SL\nPark Sohee\n4pm-5.30pm ONLINE":
            (["Y5 Chemi SL"], (t(16), t(17, 30)), [("Park Sohee", sp.ONLINE)]),
        "G11 Math 1:1\n5pm-6.30m\nSeo Yerin":
            (["G11 Math 1:1"], (t(17), t(18, 30)), [("Seo Yerin", None)]),
        "G9 Math\n10am-1.30pm 11층\nOh Minseok":
            (["G9 Math"], (t(10), t(13, 30)), [("Oh Minseok", None)]),
    }
    for text, want in cases.items():
        assert read(text) == want, (text, read(text))
    for text in ("TA 9am-4pm", "수민 3/26-4/10 한국", "5시 (6층) -->", "interview 9am"):
        assert sp.parse_cell(text)["time_range"] is None, text
    return "class, students, status or room on the time's line; notes left alone"


def t_late_lesson_below_axis():
    wb = Workbook()
    ws = _grid(wb.active, [(None, [("3(MON)", None), ("5(WED)", None)])])
    ws.title = "Aug"
    ws.cell(row=11, column=2, value="G10 Math\n9.30pm-11pm\nNam Jihoon")  # axis stops at row 10
    ws.cell(row=12, column=2, value="지훈결석")
    ws.cell(row=12, column=3, value="Oh Minseok cancelled")
    buf = io.BytesIO()
    wb.save(buf)
    out = sp.parse_workbook(io.BytesIO(buf.getvalue()), ["Aug"], 2026)
    got = [(s["start_time"], [a["student_name"] for a in s["attendance"]]) for s in out["sessions"]]
    assert got == [(dt.time(21, 30), ["Nam Jihoon"])], got
    notes = [w for w in out["warnings"] if w["message"].startswith("absence note")]
    assert len(notes) == 2, out["warnings"]
    for note in ("민석안옴", "Oh Minseok 없었음", "지훈수업참여불가", "스킵", "Field Trip", "지훈,민석 없음", "한국행"):
        assert sp._ABSENCE_WORD.search(note), note
    for note in ("답변없음", "일대일수업필요없음", "내일 테스트", "Strip", "보강희망"):
        assert not sp._ABSENCE_WORD.search(note), note
    return "a lesson under the time axis is read; notes under it are flagged, not attached"


def t_joined_times():
    cell = sp.parse_cell("G9 English\n6pm7.30pm\nSeo Yerin")
    assert cell["time_range"] == (dt.time(18), dt.time(19, 30)), cell
    assert [e["name"] for e in cell["entries"]] == ["Seo Yerin"], cell
    assert sp.parse_cell("Math\n5pm 11층\nOh Minseok")["time_range"] is None
    assert sp.parse_cell("G6 Eng\n9.30m - 11am\nSeo Yerin")["time_range"] == (dt.time(9, 30), dt.time(11))
    return "'6pm7.30pm' and '9.30m - 11am' read; a room number isn't a time"


def t_online_day_label():
    for text in ("3(Thu)\nONLINE", "5(Mon)   ONLINE", "7(WED) online", "15(SAT)"):
        assert sp.DAY_HEADER_RE.match(text), text
    assert not sp.DAY_HEADER_RE.match("14(THU)-25(MON)")
    book = _book(("Aug", [(None, [("3(MON)\nONLINE", "G10 Math\n9am-10am\nNam Jihoon\nRecording\nOh Minseok"),
                                  ("5(Wed)   ONLINE", LESSON), ("6(THU)", LESSON)])]))
    out = sp.parse_workbook(io.BytesIO(book), ["Aug"], 2026)
    got = {(s["date"].day, a["student_name"]): a["status"] for s in out["sessions"] for a in s["attendance"]}
    assert got == {(3, "Nam Jihoon"): sp.ONLINE, (3, "Oh Minseok"): sp.RECORDING,
                   (5, "Nam Jihoon"): sp.ONLINE, (5, "Oh Minseok"): sp.ONLINE,
                   (6, "Nam Jihoon"): sp.ATTENDING, (6, "Oh Minseok"): sp.ATTENDING}, got
    return "an ONLINE day is recognised and its lessons marked online"


for name, fn in [
    ("time ranges parse", t_time_ranges),
    ("status vocabulary incl. Korean", t_status_vocabulary),
    ("month/year inference", t_month_year_inference),
    ("name normalising", t_name_normalising),
    ("single class cell parses", t_parse_cell),
    ("workbook sheet names", t_sheet_names),
    ("full sheet parses", t_parse_schedule),
    ("day labels -> real dates", t_dates_resolved),
    ("class times resolved", t_times_resolved),
    ("attendance statuses applied", t_statuses_applied),
    ("Korean student parsed", t_korean_student_parsed),
    ("multi-sheet + missing sheet", t_parse_workbook_multi),
    ("backfill suggestions run", t_backfill_suggestions),
    ("month ranges in sheet names", t_month_ranges_in_names),
    ("month labels read, notes ignored", t_month_labels),
    ("whole-year sheet by its labels", t_whole_year_sheet),
    ("unlabelled first month inferred", t_unlabelled_first_month),
    ("multi-month sheet name", t_multi_month_sheet_name),
    ("leftover other-year block", t_leftover_year_block),
    ("one-month sheet: name vs label", t_single_month_label_disagrees),
    ("non-schedule sheets skipped", t_not_a_schedule),
    ("backwards time fixed or refused", t_backwards_time),
    ("time-only cell not a lesson", t_time_only_cell_skipped),
    ("year from the weekday labels", t_year_from_weekdays),
    ("roster lines cleaned up", t_roster_lines),
    ("class written after the time", t_class_line_order),
    ("two classes in one cell", t_two_classes_one_cell),
    ("meetings and shifts not billed", t_not_a_lesson),
    ("unnamed lessons grouped", t_unnamed_grouped),
    ("time typos fixed from the grid", t_time_repairs),
    ("Sheet2 and 시트3 not months", t_generic_sheet_names),
    ("grade-prefix student match", t_grade_prefix_match),
    ("time sharing a line", t_time_shares_a_line),
    ("lesson under the time axis", t_late_lesson_below_axis),
    ("'6pm7.30pm' joined times", t_joined_times),
    ("ONLINE day labels", t_online_day_label),
    ("status stays with its student", t_status_stays_with_student),
]:
    check(name, fn)

width = max(len(n) for n, _, _ in results)
failed = sum(1 for _, ok, _ in results if not ok)
print()
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name.ljust(width)}  {detail}")
print(f"\n{len(results) - failed}/{len(results)} passed")
sys.exit(1 if failed else 0)
