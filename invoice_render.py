"""Draws invoices: as an A4 web page, as PNG images, or as PDFs.

- HTML: one self-contained page per invoice (letterhead, student, a line
  per subject, payment details). It prints to PDF from any browser.
- PNG (render_invoices_png): pictures of the same page, made with a
  headless Chromium through Playwright. Parents get these on KakaoTalk,
  which shows images right in the chat.
- PDF (render_invoices_pdf): made with xhtml2pdf, which is pure Python.
  Used on the hosted app, where Chromium can't be installed. It has its
  own simpler template, because xhtml2pdf understands much less CSS.

Use image_export_available() and pdf_export_available() to see which
formats work on this machine.

The academy's address, phone and bank details come from the app's
secrets under [academy], not from this file (see _AcademyDetails).
"""

from __future__ import annotations

import base64
import datetime as dt
import html
import io
from functools import lru_cache
from pathlib import Path
from typing import Any

# Playwright and xhtml2pdf are imported inside the functions that use them.
# The hosted app has no Chromium, and importing Playwright at the top would stop
# the whole app from starting instead of just turning off image export.

__all__ = [
    "ACADEMY",
    "ACADEMY_FIELDS",
    "render_invoice_html",
    "render_invoices_batch_html",
    "render_invoices_png",
    "render_invoices_pdf",
    "image_export_available",
    "pdf_export_available",
    "format_dates",
]

ACADEMY_FIELDS = (
    "name",
    "address",
    "phone",
    "account_name",
    "account_number",
    "bank",
    "paynow",
)


class _AcademyDetails:
    """The academy's letterhead and bank details, read from the app's secrets.

    Kept out of the code because the repository is public. Set them under
    [academy] in the secrets:

        [academy]
        name = "..."
        address = "..."
        phone = "..."
        account_name = "..."
        account_number = "..."
        bank = "..."
        paynow = "..."

    They are read the first time they're needed. If any are missing this
    raises an error, so an invoice is never made without payment details.
    """

    def __init__(self) -> None:
        """Start with nothing loaded; the secrets are read on first use."""
        self._values: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        """Read and check the [academy] secrets once, then keep them. Raises if any are missing."""
        if self._values is not None:
            return self._values
        import streamlit as st

        try:
            section = st.secrets["academy"] if "academy" in st.secrets else None
        except Exception:  # noqa: BLE001 - absent, unreadable: the same problem
            section = None
        if section is None:
            raise RuntimeError(
                "The academy's details are not configured. Add an [academy] "
                "section to the app's secrets with: "
                + ", ".join(ACADEMY_FIELDS)
            )
        values = {
            field: str(section.get(field, "")).strip()
            for field in ACADEMY_FIELDS
        }
        missing = [field for field, value in values.items() if not value]
        if missing:
            raise RuntimeError(
                "The [academy] section in the app's secrets is missing: "
                + ", ".join(missing)
            )
        self._values = values
        return values

    def __getitem__(self, key: str) -> str:
        """Return one detail, e.g. details["bank"]."""
        return self._load()[key]

    def get(self, key: str, default: Any = None) -> Any:
        """Return one detail, or `default` if it isn't set."""
        return self._load().get(key, default)


ACADEMY = _AcademyDetails()

# Just the shield logo (assets/ks_icon_mark.png), without the wordmark, since the
# academy's name is written next to it. It is embedded in the page so each invoice
# is one self-contained file.
_LOGO_PATH = Path(__file__).resolve().parent / "assets" / "ks_icon_mark.png"


def _logo_data_uri() -> str:
    """Return the logo as a data: URI to embed in the page, or "" if the file is missing."""
    if not _LOGO_PATH.exists():
        return ""
    encoded = base64.b64encode(_LOGO_PATH.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


_LOGO_DATA_URI = _logo_data_uri()

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def format_dates(dates: list[dt.date]) -> str:
    """Write a line's dates compactly, e.g. "4,11,18,25/Apr/2026".

    Dates in different months are written as separate groups, so it's
    always clear which month a day belongs to.
    """
    if not dates:
        return ""
    groups: list[tuple[int, int, list[int]]] = []
    for value in sorted(dates):
        if groups and groups[-1][0] == value.year and groups[-1][1] == value.month:
            groups[-1][2].append(value.day)
        else:
            groups.append((value.year, value.month, [value.day]))
    return "  ·  ".join(
        f"{','.join(str(day) for day in days)}/{MONTHS[month - 1]}/{year}"
        for year, month, days in groups
    )


def _money(value: float) -> str:
    """Write an amount with commas and two decimals, e.g. 1,234.50."""
    return f"{value:,.2f}"


_STYLE = """
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
  @page { size: A4; margin: 16mm; }
  * { box-sizing: border-box; }
  body { font-family: Inter, system-ui, -apple-system, sans-serif; color: #1a2233;
    margin: 0; padding: 28px; background: #fff; font-size: 13px; }
  .sheet { max-width: 820px; margin: 0 auto 28px; }
  /* Header laid out as a table rather than flexbox. Flexbox is the nicer
     tool, but this page is rendered by Chromium for the images and by
     whatever browser a parent happens to open the HTML in, and two-column
     table layout is the construct they all agree on exactly. Verified as a
     pixel-for-pixel no-op against the flexbox version. (The PDF path does
     not read this stylesheet at all -- see _PDF_STYLE.) */
  .head { width: 100%; border-collapse: collapse;
    border-bottom: 3px solid #1f3864; }
  .head > tbody > tr > td { padding: 0 0 18px; vertical-align: top;
    border-bottom: none; background: none; }
  .brand-row { border-collapse: collapse; width: auto; }
  .brand-row > tbody > tr > td { padding: 0; vertical-align: middle;
    border-bottom: none; background: none; }
  .brand-row td.logo { padding-right: 14px; width: 1px; }
  .brand-row img { height: 56px; width: auto; display: block; }
  .brand { font-size: 26px; font-weight: 700; color: #1f3864; letter-spacing: .02em; }
  .contact { margin-top: 8px; color: #55617a; font-size: 12px; line-height: 1.55; }
  .title { text-align: right; }
  .title h1 { margin: 0; font-size: 34px; letter-spacing: .06em; color: #1f3864; }
  .meta { margin-top: 10px; font-size: 12.5px; line-height: 1.7; }
  .meta b { color: #55617a; font-weight: 500; display: inline-block; min-width: 74px; }
  .billed { margin: 26px 0 6px; }
  .billed .label { font-size: 11px; letter-spacing: .12em; text-transform: uppercase;
    color: #7b8598; }
  .billed .who { font-size: 20px; font-weight: 600; margin-top: 3px; }
  /* Scoped to the line-items table so the header table above, which is
     layout rather than data, does not pick up borders and row striping. */
  table.items { width: 100%; border-collapse: collapse; margin-top: 16px; }
  .items th { background: #1f3864; color: #fff; font-weight: 600; font-size: 11.5px;
    letter-spacing: .06em; text-transform: uppercase; padding: 9px 10px; text-align: left; }
  .items td { padding: 10px; border-bottom: 1px solid #e4e8f0; vertical-align: top; }
  .items tbody tr:nth-child(even) td { background: #f8fafd; }
  .mid { text-align: center; }
  .right { text-align: right; font-variant-numeric: tabular-nums; }
  /* Spelled out with the .items prefix so they outrank ".items th" and
     ".items td" above. A bare ".mid" loses to ".items th" on specificity,
     which silently left-aligns the Qty, Unit price and Amount headings. */
  .items th.mid, .items td.mid { text-align: center; }
  .items th.right, .items td.right { text-align: right; }
  .nowrap { white-space: nowrap; }
  .muted { color: #7b8598; }
  .items .credit td { color: #1f6f43; font-style: italic; }
  .items .total td { border-bottom: none; border-top: 2px solid #1f3864; font-size: 15px;
    font-weight: 700; padding-top: 12px; background: #fff !important; }
  .pay { margin-top: 34px; border: 1px solid #e4e8f0; border-radius: 8px; padding: 16px 18px;
    background: #f8fafd; }
  .pay h2 { margin: 0 0 10px; font-size: 11px; letter-spacing: .12em;
    text-transform: uppercase; color: #7b8598; }
  .pay div { line-height: 1.75; }
  .pay b { font-weight: 500; color: #55617a; display: inline-block; min-width: 120px; }
  .foot { margin-top: 26px; text-align: center; color: #7b8598; font-size: 11.5px; }
  .draft { margin-bottom: 14px; padding: 7px 12px; border-radius: 6px;
    background: #fff4e5; color: #8a5a00; font-weight: 600; font-size: 12px;
    display: inline-block; }
  @media print { body { padding: 0; } .noprint { display: none; } }
  /* Only used when several invoices share one page (render_invoices_batch_html) --
     one sheet per printed page, so "print all" produces one invoice per sheet
     of paper instead of running them together. */
  .sheet + .sheet { break-before: page; page-break-before: always; }
"""


def _line_cells(line: dict[str, Any]) -> tuple[str, str, str, str, str, bool]:
    """Return one invoice line as its five columns, plus whether it is a credit.

    Used by both the HTML and the PDF templates, so a line reads the same in
    every format.
    """
    subject = html.escape(str(line.get("Subject", "")))
    dates = html.escape(format_dates(line.get("Dates") or []))
    quantity = str(line.get("Quantity", 0))
    amount = line.get("Amount", 0)
    if line.get("Extra"):
        # An extra line added by hand (materials, a discount): just an amount, no class.
        text = f"-{_money(abs(amount))}" if amount < 0 else _money(amount)
        return "", subject, "", "", text, amount < 0
    if line.get("Credit"):
        # A credit for a class already billed: no unit price, since nothing is being charged.
        return quantity, subject, dates, "credit", f"-{_money(abs(amount))}", True
    hours = line.get("Hours", 0)
    rate = line.get("Rate", 0)
    return (quantity, subject, dates,
            f"${_money(rate)} × {hours:g}h", _money(amount), False)


def _status_mark(invoice: dict[str, Any]) -> str:
    """Return the banner shown across an invoice that isn't for paying: a draft or a void one."""
    if invoice.get("Status") == "Void":
        by = invoice.get("Replaced by")
        return ("<div class='draft'>VOID &mdash; replaced by invoice "
                f"#{by}</div>" if by else "<div class='draft'>VOID</div>")
    return "<div class='draft'>DRAFT &mdash; not yet issued</div>"


def _invoice_sheet(invoice: dict[str, Any]) -> str:
    """Return the HTML for one invoice's sheet, without the <html> and <head> around it."""
    lines = invoice.get("Lines") or []
    number = invoice.get("Number")
    issued = invoice.get("Issued") or dt.date.today()
    is_draft = invoice.get("Status") != "Issued"
    replaces = invoice.get("Replaces")

    rows = ""
    for line in lines:
        # Qty is the number of classes. With "rate x hours" next to it, the two explain the amount.
        quantity, subject, dates, unit, amount_text, is_credit = _line_cells(line)
        rows += (
            f"<tr class='credit'>" if is_credit else "<tr>"
        ) + (
            f"<td class='mid'>{quantity}</td>"
            f"<td>{subject}</td>"
            f"<td>{dates}</td>"
            f"<td class='mid nowrap'>{unit}</td>"
            f"<td class='right'>{amount_text}</td>"
            "</tr>"
        )

    if not rows:
        rows = "<tr><td colspan='5' class='mid muted'>No classes on this invoice yet.</td></tr>"

    draft_mark = _status_mark(invoice) if is_draft else ""

    return f"""<div class="sheet">
  {draft_mark}
  <table class="head"><tr>
    <td>
      <table class="brand-row"><tr>
        {f'<td class="logo"><img src="{_LOGO_DATA_URI}" alt="{html.escape(ACADEMY["name"])} logo"></td>' if _LOGO_DATA_URI else ''}
        <td>
          <div class="brand">{html.escape(ACADEMY['name'])}</div>
          <div class="contact">{html.escape(ACADEMY['address'])}<br>{html.escape(ACADEMY['phone'])}</div>
        </td>
      </tr></table>
    </td>
    <td class="title">
      <h1>INVOICE</h1>
      <div class="meta">
        <div><b>Date</b> {issued:%d %b %Y}</div>
        <div><b>Invoice no</b> {('#' + str(number)) if number else '—'}</div>
        {f"<div><b>Replaces</b> #{replaces}</div>" if replaces else ""}
      </div>
    </td>
  </tr></table>

  <div class="billed">
    <div class="label">Billed to</div>
    <div class="who">{html.escape(invoice.get('Student', ''))}</div>
    {f"<div class='contact'>{html.escape(invoice.get('Parent',''))} · {html.escape(invoice.get('Phone',''))}</div>" if invoice.get('Parent') else ''}
  </div>

  <table class="items">
    <thead><tr>
      <th style="width:48px" class="mid">Qty</th>
      <th>Subject</th>
      <th>Dates</th>
      <th class="mid">Unit price</th>
      <th class="right" style="width:110px">Amount (SGD)</th>
    </tr></thead>
    <tbody>
      {rows}
      <tr class="total">
        <td colspan="4" class="right">Total (SGD)</td>
        <td class="right">{_money(invoice.get('Total', 0))}</td>
      </tr>
    </tbody>
  </table>

  <div class="pay">
    <h2>Payment details</h2>
    <div>
      <div><b>Account name</b> {html.escape(ACADEMY['account_name'])}</div>
      <div><b>Account number</b> {html.escape(ACADEMY['account_number'])}</div>
      <div><b>Bank</b> {html.escape(ACADEMY['bank'])}</div>
      <div><b>PayNow</b> {html.escape(ACADEMY['paynow'])}</div>
    </div>
  </div>

  <div class="foot">Thank you. Please quote the invoice number when paying.</div>
</div>"""


def render_invoice_html(invoice: dict[str, Any]) -> str:
    """Return the full web page for one invoice."""
    number = invoice.get("Number")
    title = f"Invoice {'#' + str(number) if number else 'draft'} — {html.escape(invoice.get('Student', ''))}"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{title}</title>
<style>{_STYLE}</style></head>
<body>
{_invoice_sheet(invoice)}
</body></html>"""


def render_invoices_batch_html(invoices: list[dict[str, Any]]) -> str:
    """Return one web page holding several invoices, one per printed page.

    Lets the admin print a whole batch at once instead of opening each invoice.
    """
    title = f"{len(invoices)} invoices" if len(invoices) != 1 else "1 invoice"
    body = "\n".join(_invoice_sheet(invoice) for invoice in invoices)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>{_STYLE}</style></head>
<body>
{body}
</body></html>"""


def _sheet_page_html(invoice: dict[str, Any]) -> str:
    """Return a bare page holding one invoice, for taking its screenshot."""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{_STYLE}</style></head>
<body>{_invoice_sheet(invoice)}</body></html>"""


def _screenshot_sheet(browser, invoice: dict[str, Any], scale: float) -> bytes:
    """Take a PNG screenshot of one invoice, using an already-open browser."""
    page = browser.new_page(viewport={"width": 900, "height": 200}, device_scale_factor=scale)
    try:
        page.set_content(_sheet_page_html(invoice))
        # Wait for the web font to load, or the picture would show the fallback font.
        page.evaluate("document.fonts.ready")
        return page.locator(".sheet").screenshot()
    finally:
        page.close()


def render_invoices_png(
    invoices: list[dict[str, Any]], *, scale: float = 2.0, on_progress=None
) -> list[bytes]:
    """Turn several invoices into PNG images, using one browser for the whole batch.

    Starting Chromium takes about a second, so it is started once, not once per
    invoice. Each invoice takes about 0.6 seconds, so a full month can take most
    of a minute; on_progress(done, total) is called after each one so the screen
    can show a progress bar.
    """
    from playwright.sync_api import sync_playwright

    if not invoices:
        return []
    total = len(invoices)
    shots: list[bytes] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for index, invoice in enumerate(invoices, start=1):
                shots.append(_screenshot_sheet(browser, invoice, scale))
                if on_progress is not None:
                    on_progress(index, total)
        finally:
            browser.close()
    return shots


@lru_cache(maxsize=1)
def image_export_available() -> bool:
    """Return True if PNG export really works here.

    Playwright can be installed without the Chromium browser it needs (as on the
    hosted app), so this checks the browser file is actually there. The answer is
    cached, since it can't change while the app is running.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as pw:
            return Path(pw.chromium.executable_path).exists()
    except Exception:
        return False


@lru_cache(maxsize=1)
def pdf_export_available() -> bool:
    """Return True if PDF export works here, which just means xhtml2pdf is installed."""
    try:
        from xhtml2pdf import pisa  # noqa: F401
    except Exception:
        return False
    return True


# Adobe's standard Korean font, used only for Korean characters (its English
# letters are fixed-width and look out of place). It is referenced, not embedded,
# so it adds nothing to the file size.
_CJK_FONT = "HYGothic-Medium"


def _register_cjk_font() -> None:
    """Register the Korean font with reportlab, once."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    try:
        pdfmetrics.getFont(_CJK_FONT)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(_CJK_FONT))


def _needs_cjk(character: str) -> bool:
    """Return True for a character that needs the Korean font (Hangul or other CJK)."""
    code = ord(character)
    return (
        0x1100 <= code <= 0x11FF      # Hangul Jamo
        or 0x2E80 <= code <= 0x9FFF   # CJK radicals, kana, unified ideographs
        or 0xA960 <= code <= 0xA97F   # Hangul Jamo Extended-A
        or 0xAC00 <= code <= 0xD7FF   # Hangul syllables, Jamo Extended-B
        or 0xF900 <= code <= 0xFAFF   # CJK compatibility ideographs
        or 0xFF00 <= code <= 0xFFEF   # half and full width forms
    )


def _cjk_spans(escaped: str) -> str:
    """Wrap runs of Korean text in a span, so the PDF uses the Korean font for them.

    The PDF's default Helvetica has no Korean letters and draws them as bars,
    which would garble a student's name. Only the Korean characters switch
    font, so English text keeps its normal look. Safe on text that is already
    HTML-escaped.
    """
    if not escaped:
        return escaped
    out: list[str] = []
    run: list[str] = []
    run_is_cjk = False
    for character in escaped:
        is_cjk = _needs_cjk(character)
        if run and is_cjk != run_is_cjk:
            joined = "".join(run)
            out.append(f"<span class='ko'>{joined}</span>" if run_is_cjk else joined)
            run = []
        run_is_cjk = is_cjk
        run.append(character)
    joined = "".join(run)
    out.append(f"<span class='ko'>{joined}</span>" if run_is_cjk else joined)
    return "".join(out)


_PDF_STYLE = """
@page { size: a4 portrait; margin: 14mm; }
body { font-family: Helvetica; font-size: 9pt; color: #1a2233; }
table { width: 100%; border-collapse: collapse; }
td, th { vertical-align: top; }
/* The rule under the letterhead is a row of its own, for the same reason
   as the one above the total: three cells each carrying their own bottom
   border meet with visible notches where the columns abut, and one
   continuous border does not. */
.hd td { padding: 0 0 8pt 0; }
.hdrule td { border-top: 2pt solid #1f3864; padding: 0; font-size: 2pt; }
.logo { width: 34pt; }
.logo img { width: 30pt; height: 30pt; }
.brand { font-size: 17pt; font-weight: bold; color: #1f3864; }
.contact { color: #55617a; font-size: 7.5pt; }
.ttl { text-align: right; }
.ttl .big { font-size: 23pt; font-weight: bold; color: #1f3864; }
.meta { font-size: 8.5pt; }
.meta span { color: #55617a; }
.draft { margin-top: 8pt; color: #8a5a00; font-weight: bold; font-size: 8.5pt; }
.label { font-size: 7.5pt; color: #7b8598; padding-top: 14pt; }
.who { font-size: 14pt; font-weight: bold; padding-bottom: 2pt; }
.items { margin-top: 10pt; }
.items th { background-color: #1f3864; color: #ffffff; font-size: 7pt;
  padding: 5pt 6pt; text-align: left; font-weight: bold; }
.items td { padding: 5pt 6pt; border-bottom: 0.5pt solid #e4e8f0; font-size: 9pt; }
.items tr.alt td { background-color: #f8fafd; }
.items td.mid, .items th.mid { text-align: center; }
.items td.right, .items th.right { text-align: right; }
.credit td { color: #1f6f43; font-style: italic; }
/* The rule above the total is its own row rather than a border on the total
   itself. This engine ignores padding-top on a cell that carries a border,
   so the line was drawn straight through "Total (SGD)" -- raising the
   padding changed nothing at all. An empty row wearing the border puts the
   line where it belongs and leaves the figures clear of it. */
.rulerow td { border-top: 1.5pt solid #1f3864; border-bottom: none;
  padding: 0; font-size: 2pt; }
.total td { border-bottom: none; border-top: none;
  font-size: 10.5pt; font-weight: bold; padding-top: 8pt; }
.payhd { margin-top: 20pt; padding-bottom: 4pt; font-size: 7.5pt;
  color: #7b8598; font-weight: bold; }
.pay { background-color: #f8fafd; border: 0.5pt solid #e4e8f0; }
.pay td { padding: 4pt 12pt; font-size: 9pt; }
.pay td.k { color: #55617a; width: 135pt; }
.foot { margin-top: 18pt; text-align: center; color: #7b8598; font-size: 8pt; }
.ko { font-family: HYGothic-Medium; }
"""


def _sheet_pdf_html(invoice: dict[str, Any]) -> str:
    """Return the HTML for one invoice in the simpler form xhtml2pdf can handle.

    xhtml2pdf has no flexbox, nth-child, rounded corners or web fonts, and fails
    on nested tables it can't size, so this is a separate template from the web
    page. The lines themselves still come from _line_cells, so the content is
    the same. Row striping uses a class because nth-child doesn't work here.
    """
    rows = ""
    for index, line in enumerate(invoice.get("Lines") or []):
        quantity, subject, dates, unit, amount_text, is_credit = _line_cells(line)
        classes = " ".join(
            filter(None, ["credit" if is_credit else "", "alt" if index % 2 else ""])
        )
        rows += (
            f"<tr class='{classes}'>"
            f"<td class='mid'>{quantity}</td>"
            f"<td>{_cjk_spans(subject)}</td>"
            f"<td>{dates}</td>"
            f"<td class='mid'>{unit}</td>"
            f"<td class='right'>{amount_text}</td></tr>"
        )
    if not rows:
        rows = "<tr><td colspan='5' class='mid'>No classes on this invoice yet.</td></tr>"

    logo = (
        f"<td class='logo'><img src='{_LOGO_DATA_URI}'></td>" if _LOGO_DATA_URI else ""
    )
    issued = invoice.get("Issued") or dt.date.today()
    number = invoice.get("Number")
    parent = invoice.get("Parent")
    draft = _status_mark(invoice) if invoice.get("Status") != "Issued" else ""
    replaces = invoice.get("Replaces")
    who_contact = (
        f"<tr><td class='contact'>{_cjk_spans(html.escape(parent))} &middot; "
        f"{html.escape(invoice.get('Phone', ''))}</td></tr>"
        if parent
        else ""
    )
    return f"""<html><head><meta charset="utf-8">
<style>{_PDF_STYLE}</style></head><body>
<table class="hd"><tr>
  {logo}
  <td style="width:64%"><div class="brand">{_cjk_spans(html.escape(ACADEMY['name']))}</div>
    <div class="contact">{_cjk_spans(html.escape(ACADEMY['address']))}<br/>
    {html.escape(ACADEMY['phone'])}</div></td>
  <td class="ttl" style="width:31%"><div class="big">INVOICE</div>
    <div class="meta"><span>Date</span> {issued:%d %b %Y}<br/>
    <span>Invoice no</span> {('#' + str(number)) if number else '-'}{f"<br/><span>Replaces</span> #{replaces}" if replaces else ""}</div></td>
</tr>
<tr class="hdrule"><td colspan="{3 if _LOGO_DATA_URI else 2}">&nbsp;</td></tr>
</table>
{draft}
<table><tr><td class="label">BILLED TO</td></tr>
<tr><td class="who">{_cjk_spans(html.escape(invoice.get('Student', '')))}</td></tr>
{who_contact}</table>
<table class="items">
<tr><th class="mid" style="width:7%">QTY</th><th style="width:29%">SUBJECT</th>
<th style="width:26%">DATES</th><th class="mid" style="width:20%">UNIT PRICE</th>
<th class="right" style="width:18%">AMOUNT (SGD)</th></tr>
{rows}
<tr class="rulerow"><td colspan="5">&nbsp;</td></tr>
<tr class="total"><td colspan="4" class="right">Total (SGD)</td>
<td class="right">{_money(invoice.get('Total', 0))}</td></tr>
</table>
<div class="payhd">PAYMENT DETAILS</div>
<table class="pay">
<tr><td class="k">Account name</td><td>{html.escape(str(ACADEMY.get('account_name', '')))}</td></tr>
<tr><td class="k">Account number</td><td>{html.escape(str(ACADEMY.get('account_number', '')))}</td></tr>
<tr><td class="k">Bank</td><td>{html.escape(str(ACADEMY.get('bank', '')))}</td></tr>
<tr><td class="k">PayNow</td><td>{html.escape(str(ACADEMY.get('paynow', '')))}</td></tr>
</table>
<div class="foot">Thank you. Please quote the invoice number when paying.</div>
</body></html>"""


def render_invoices_pdf(
    invoices: list[dict[str, Any]], *, on_progress=None
) -> list[bytes]:
    """Turn several invoices into one-page A4 PDFs, without a browser.

    on_progress(done, total) is called after each invoice, the same as for
    PNGs. A conversion that fails raises an error instead of returning an
    empty file, so a broken PDF never goes out to a parent.
    """
    from xhtml2pdf import pisa

    if not invoices:
        return []
    _register_cjk_font()
    total = len(invoices)
    pages: list[bytes] = []
    for index, invoice in enumerate(invoices, start=1):
        buffer = io.BytesIO()
        status = pisa.CreatePDF(_sheet_pdf_html(invoice), dest=buffer, encoding="utf-8")
        if status.err:
            raise RuntimeError(
                "Could not render invoice "
                f"{invoice.get('Number') or invoice.get('ID')} as a PDF."
            )
        pages.append(buffer.getvalue())
        if on_progress is not None:
            on_progress(index, total)
    return pages
