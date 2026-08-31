"""Printable invoice.

Renders one invoice as a self-contained A4 page: the academy's letterhead, the
student, a line per subject, and the payment details.  Opened in a new tab it
prints straight to PDF from the browser, so nothing else has to be installed.

It can also be rendered as PNGs (``render_invoices_png``)
for sending straight to a parent's phone over a chat app -- an image needs no
"which app opens this" decision the way an HTML or PDF attachment does, and a
chat app shows it inline. That path renders through a real (headless) Chromium
via Playwright, so what's sent is pixel-for-pixel what the HTML version looks
like -- no separate rendering engine to keep in sync.

Chromium is a heavy thing to ask of a host, though, and the free tier this is
deployed to has no way to install one. So there is a third renderer,
``render_invoices_pdf``, which drives xhtml2pdf -- pure Python, with no system
libraries behind it at all, so it runs anywhere pip does. It carries its own
cut-down template, because that engine understands far less CSS than a browser. PDFs are the lesser option for KakaoTalk --
they arrive as an attachment rather than previewing inline -- so the app
prefers images wherever Chromium is actually present and falls back to PDF
where it is not. Ask ``image_export_available`` and ``pdf_export_available``
rather than assuming either works.

Everything the academy needs to change -- address, phone, bank -- lives in
the app's secrets under ``[academy]``, not in this file; see
``_AcademyDetails``.
"""

from __future__ import annotations

import base64
import datetime as dt
import html
import io
from functools import lru_cache
from pathlib import Path
from typing import Any

# Playwright and xhtml2pdf are both imported lazily, inside the functions
# that use them. Playwright especially: the package installs fine but the
# Chromium it drives is absent on the deployed host, and a module-scope
# import would take the whole app down on start rather than disabling one
# button.

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
    """The academy's letterhead and bank details, from the app's secrets.

    These used to sit in this file. They are a bank account number and a
    UEN, and while they are hardly a secret from the parents -- they print on
    every invoice -- a public repository is a different thing from an
    invoice. Account numbers left in one get collected by people who collect
    account numbers.

    Configure them under ``[academy]`` in the app's secrets::

        [academy]
        name = "..."
        address = "..."
        phone = "..."
        account_name = "..."
        account_number = "..."
        bank = "..."
        paynow = "..."

    Read on first use rather than at import, so that importing this module
    needs no Streamlit runtime. Missing configuration raises rather than
    falling back to a placeholder: an invoice that quietly goes to a parent
    with no way to pay it is worse than one that refuses to render.
    """

    def __init__(self) -> None:
        self._values: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
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
        values = {field: str(section.get(field, "")).strip() for field in ACADEMY_FIELDS}
        missing = [field for field, value in values.items() if not value]
        if missing:
            raise RuntimeError(
                "The [academy] section in the app's secrets is missing: "
                + ", ".join(missing)
            )
        self._values = values
        return values

    def __getitem__(self, key: str) -> str:
        return self._load()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._load().get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self._load()

    def keys(self):
        return self._load().keys()


ACADEMY = _AcademyDetails()

# The shield mark cropped out of assets/ks_icon.png (which also carries the
# "ACADEMIA PREP" wordmark and tagline baked in -- not wanted a second time
# next to ACADEMY['name'] above). Embedded as a data URI so the invoice stays
# a single self-contained file, same as the rest of this page.
_LOGO_PATH = Path(__file__).resolve().parent / "assets" / "ks_icon_mark.png"


def _logo_data_uri() -> str:
    if not _LOGO_PATH.exists():
        return ""
    encoded = base64.b64encode(_LOGO_PATH.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


_LOGO_DATA_URI = _logo_data_uri()

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def format_dates(dates: list[dt.date]) -> str:
    """``4,11,18,25/Apr/2026`` -- days collapsed, month and year written once.

    A line that runs across a month boundary is split, so the dates are never
    ambiguous.
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
    """One invoice line as its five display columns, plus "is a credit".

    Shared by the HTML and the PDF renderers. They cannot share markup -- the
    PDF engine needs a much plainer subset -- but they must not disagree about
    what a line actually says, so the formatting decisions live here once
    rather than in whichever renderer the author last edited.
    """
    subject = html.escape(str(line.get("Subject", "")))
    dates = html.escape(format_dates(line.get("Dates") or []))
    quantity = str(line.get("Quantity", 0))
    amount = line.get("Amount", 0)
    if line.get("Credit"):
        # A cancelled class already billed for: no unit price to quote,
        # because nothing is being charged.
        return quantity, subject, dates, "credit", f"-{_money(abs(amount))}", True
    hours = line.get("Hours", 0)
    rate = line.get("Rate", 0)
    return (quantity, subject, dates,
            f"${_money(rate)} × {hours:g}h", _money(amount), False)


def _invoice_sheet(invoice: dict[str, Any]) -> str:
    """The billing content for one invoice -- no <html>/<head>, just the sheet."""
    lines = invoice.get("Lines") or []
    number = invoice.get("Number")
    issued = invoice.get("Issued") or dt.date.today()
    is_draft = invoice.get("Status") != "Issued"

    rows = ""
    for line in lines:
        # Qty is the number of classes, not the row number: the unit price
        # beside it already reads "rate x hours", so the two together
        # explain the amount.
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

    draft_mark = (
        "<div class='draft'>DRAFT &mdash; not yet issued</div>" if is_draft else ""
    )

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
    """Build the full page for one invoice."""
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
    """Build one page holding several invoices, one per printed sheet of paper.

    For "select several, issue them, now print them all" -- opening one file
    and printing (or saving as PDF) once produces one invoice per page,
    instead of downloading and printing each invoice separately.
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
    """A minimal document holding just one invoice, for screenshotting."""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{_STYLE}</style></head>
<body>{_invoice_sheet(invoice)}</body></html>"""


def _screenshot_sheet(browser, invoice: dict[str, Any], scale: float) -> bytes:
    page = browser.new_page(viewport={"width": 900, "height": 200}, device_scale_factor=scale)
    try:
        page.set_content(_sheet_page_html(invoice))
        # The letterhead font loads over the network (Google Fonts) -- wait
        # for it, or a screenshot taken mid-load would freeze the fallback
        # system font into the image instead.
        page.evaluate("document.fonts.ready")
        return page.locator(".sheet").screenshot()
    finally:
        page.close()


def render_invoices_png(
    invoices: list[dict[str, Any]], *, scale: float = 2.0, on_progress=None
) -> list[bytes]:
    """Several invoices at once, as PNGs, sharing one browser instance.

    Launching headless Chromium costs real time (roughly a second); reusing
    one instance across a whole batch instead of relaunching it per invoice
    is what keeps "issue 30 invoices, now image all of them" from being
    dramatically slower than the single-invoice case.

    ``on_progress(done, total)`` is called after each invoice.  A full month
    runs to about six tenths of a second apiece, so a ninety-student month is
    the better part of a minute -- long enough that the caller needs to be
    able to show it moving rather than leave the admin looking at a page that
    appears to have hung.
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
    """Whether PNG export can actually run, not merely whether it imports.

    The Playwright package installing successfully says nothing about the
    Chromium it drives, which is a separate several-hundred-megabyte
    download. On the deployed host the package is present and the browser is
    not, so importing is exactly the wrong thing to test. Ask for the
    executable's path and check something is really there.

    Cached because this is consulted on every rerender of the invoice list
    and the answer cannot change while the process is alive.
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
    """Whether PDF export can run.

    With xhtml2pdf this is simply "is it installed": it is pure Python, with
    no system libraries behind it, which is the whole reason it was chosen
    over better-looking engines for a host where nothing can be
    apt-installed.
    """
    try:
        from xhtml2pdf import pisa  # noqa: F401
    except Exception:
        return False
    return True


# Adobe's stock Korean face. Used only for the characters that need it:
# its Latin glyphs are fixed-width and look wrong next to the rest of the
# invoice, and it is referenced rather than embedded, so it costs nothing in
# file size. Registered lazily because it is a reportlab import.
_CJK_FONT = "HYGothic-Medium"


def _register_cjk_font() -> None:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    try:
        pdfmetrics.getFont(_CJK_FONT)
    except KeyError:
        pdfmetrics.registerFont(UnicodeCIDFont(_CJK_FONT))


def _needs_cjk(character: str) -> bool:
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
    """Mark runs of Korean text so the PDF switches font for just those.

    The academy's students include Korean names, and the PDF engine's default
    Helvetica has no Hangul at all -- it silently draws them as vertical
    bars, so an invoice would go to a parent with their child's name turned
    into gibberish. Rather than set the whole document in the CID font and
    make every Latin word fixed-width, only the characters that need it are
    switched.

    Safe to run over already-escaped markup: escaping only ever emits ASCII,
    so no entity can be split down the middle by this.
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
.hd td { padding: 0 0 8pt 0; border-bottom: 2pt solid #1f3864; }
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
.total td { border-bottom: none; border-top: 1.5pt solid #1f3864;
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
    """The same invoice, in the narrow dialect the PDF engine understands.

    Deliberately a second template rather than the shared one. The PDF engine
    is not a browser: it has no flexbox, no ``nth-child``, no
    ``border-radius`` and no webfonts, and it fails outright on a nested
    table whose columns it cannot size. All of those appear in ``_STYLE``,
    which exists to look right in a browser and should not be dragged down to
    a lowest common denominator to keep this path alive.

    What the two templates must agree on is the *content* of a line, and they
    do -- both go through ``_line_cells``. Row striping is written out as an
    explicit class here because ``nth-child`` silently does nothing.
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
    draft = (
        "<div class='draft'>DRAFT &mdash; not yet issued</div>"
        if invoice.get("Status") != "Issued"
        else ""
    )
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
    <span>Invoice no</span> {('#' + str(number)) if number else '-'}</div></td>
</tr></table>
{draft}
<table><tr><td class="label">BILLED TO</td></tr>
<tr><td class="who">{_cjk_spans(html.escape(invoice.get('Student', '')))}</td></tr>
{who_contact}</table>
<table class="items">
<tr><th class="mid" style="width:7%">QTY</th><th style="width:29%">SUBJECT</th>
<th style="width:26%">DATES</th><th class="mid" style="width:20%">UNIT PRICE</th>
<th class="right" style="width:18%">AMOUNT (SGD)</th></tr>
{rows}
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
    """Several invoices, each as its own one-page A4 PDF, without a browser.

    ``on_progress(done, total)`` is called after each invoice, matching
    ``render_invoices_png`` so either format drives the same progress bar.

    A failed conversion raises rather than returning a broken file. A zip of
    silently empty PDFs going out to parents is a far worse outcome than an
    error the admin can see and fall back from.
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
