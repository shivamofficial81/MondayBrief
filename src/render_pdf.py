"""Fill templates/report.html with the weekly brief and render it to an A4 PDF.

    brief = analyze_week(df)
    render_pdf(brief, "reports/2026-09-14.pdf")

The pipeline is load_sales_data -> analyze_week -> render_pdf. The chart is drawn by
``src.chart`` and inlined into the page as a base64 data URI, so the HTML is fully
self-contained and needs no network or asset files.

Run ``python -m src.render_pdf [--week YYYY-MM-DD] [--source PATH_OR_URL] [--out FILE]``.
The one-time browser download is ``python -m playwright install chromium``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .analyze import ANOMALY_THRESHOLD_PERCENT, AnalysisError, analyze_week
from .chart import ChartError, weekly_revenue_chart_base64
from .load_data import DataLoadError, load_sales_data

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = PROJECT_ROOT / "templates"
TEMPLATE_NAME = "report.html"
DEFAULT_OUT_DIR = PROJECT_ROOT / "output"

# A4 is 210 x 297 mm. The margins live here so the template's @page rule, its column
# width and the chart's pixel size all come from one place.
PAGE_WIDTH_MM, PAGE_HEIGHT_MM = 210, 297
MARGIN_X_MM, MARGIN_Y_MM = 17, 14
CONTENT_WIDTH_MM = PAGE_WIDTH_MM - 2 * MARGIN_X_MM
# Slightly under the printable height so the footer sits at the bottom without spilling
# onto a second page through rounding.
PAGE_MIN_HEIGHT_MM = PAGE_HEIGHT_MM - 2 * MARGIN_Y_MM - 3
CHART_HEIGHT_IN = 1.75  # plot area ~1.2 in: the caption is dropped, the section subtitle says it
# Printable height in CSS px, less an 8px cushion: the measurement is taken in the browser's
# print layout, and the cushion keeps sub-pixel and engine differences from tipping the last
# line onto page 2. A layout inside the cushion switches to compact spacing early.
AVAILABLE_HEIGHT_PX = (PAGE_HEIGHT_MM - 2 * MARGIN_Y_MM) / 25.4 * 96 - 8

# Returns the report's natural height, tightening the spacing (html.compact) only if it overflows.
_FIT_JS = """(available) => {
  const page = document.querySelector('.page');
  const natural = () => {
    const prev = page.style.minHeight; page.style.minHeight = '0';
    const h = page.getBoundingClientRect().height; page.style.minHeight = prev;
    return h;
  };
  let height = natural(), mode = 'normal';
  if (height > available) {
    document.documentElement.classList.add('compact');
    height = natural(); mode = 'compact';
  }
  return { height, mode };
}"""


class RenderError(Exception):
    """The PDF couldn't be produced. The message is safe to print as-is."""


# --- Public API -------------------------------------------------------------------


def render_html(brief: dict, *, generated_on: date | None = None) -> str:
    """The filled-in report as a self-contained HTML string."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        undefined=StrictUndefined,  # a typo in the template fails loudly instead of printing blank
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        money=_money, number=_number, delta=_delta, trend=_trend, short_date=_short_date
    )
    try:
        template = env.get_template(TEMPLATE_NAME)
    except OSError:
        raise RenderError(f"Report template not found: '{TEMPLATE_DIR / TEMPLATE_NAME}'.") from None

    generated_on = generated_on or date.today()
    return template.render(
        brief=brief,
        chart_b64=weekly_revenue_chart_base64(
            brief, width_in=CONTENT_WIDTH_MM / 25.4, height_in=CHART_HEIGHT_IN, caption=None
        ),
        chart_alt=_chart_alt(brief),
        threshold=ANOMALY_THRESHOLD_PERCENT,
        generated_on=f"{generated_on.day} {generated_on:%b %Y}",
        margin_x_mm=MARGIN_X_MM,
        margin_y_mm=MARGIN_Y_MM,
        content_width_mm=CONTENT_WIDTH_MM,
        page_min_height_mm=PAGE_MIN_HEIGHT_MM,
    )


def render_pdf(brief: dict, out_path: str | Path, *, generated_on: date | None = None) -> Path:
    """Render the brief to an A4 PDF at ``out_path`` and return that path."""
    html = render_html(brief, generated_on=generated_on)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                page.set_content(html, wait_until="load")
                page.emulate_media(media="print")
                page.evaluate("document.fonts.ready")
                fit_to_one_page(page)
                page.pdf(
                    path=str(out_path),
                    format="A4",
                    print_background=True,
                    prefer_css_page_size=True,  # the template's @page rule owns size and margins
                    tagged=True,  # accessible PDF: headings and reading order carried through
                )
            finally:
                browser.close()
    except PlaywrightError as exc:
        if "Executable doesn't exist" in str(exc):
            raise RenderError(
                "The browser used for PDFs isn't installed yet. Run this once, then try again:\n"
                "  python -m playwright install chromium"
            ) from None
        raise RenderError(f"Couldn't render the PDF: {str(exc).splitlines()[0]}") from None
    return out_path


def fit_to_one_page(page) -> dict:
    """Keep the report on one A4 page whatever font the machine substitutes.

    Fallback sans-serifs on Linux CI are wider than Segoe UI, and some weeks carry longer
    summaries or product names. If the natural height overflows one page, the template's
    tighter ``compact`` spacing is switched on. Returns ``{'mode': 'normal'|'compact',
    'height': px}``; if even compact spacing overflows it logs a warning and lets the
    report run to a second page rather than clipping anything.
    """
    fit = page.evaluate(_FIT_JS, AVAILABLE_HEIGHT_PX)
    if fit["height"] > AVAILABLE_HEIGHT_PX:
        logger.warning(
            "The report doesn't fit on one page even with tightened spacing; it will run to two pages."
        )
    return fit


# --- Template filters and helpers -------------------------------------------------


def _money(value: float | None, decimals: int = 0) -> str:
    return "" if value is None else f"${value:,.{decimals}f}"


def _number(value: float | None) -> str:
    return "" if value is None else f"{value:,.0f}"


def _trend(pct: float | None) -> str:
    """'up', 'down', 'flat' or 'none' (no baseline), judged on the displayed whole percent."""
    if pct is None:
        return "none"
    whole = round(pct)
    return "flat" if whole == 0 else ("up" if whole > 0 else "down")


def _delta(pct: float | None) -> str:
    if pct is None:
        return ""
    whole = round(pct)
    return "0%" if whole == 0 else f"{whole:+d}%"


def _short_date(iso: str) -> str:
    day = datetime.fromisoformat(iso)
    return f"{day.day} {day:%b}"


def _chart_alt(brief: dict) -> str:
    """Text alternative that gives the chart's headline values, for screen readers."""
    weeks = brief["weekly_revenue"]
    latest = next((w for w in weeks if w["is_target"]), weeks[-1])
    low = min(weeks, key=lambda w: w["revenue"])
    high = max(weeks, key=lambda w: w["revenue"])
    text = (
        f"Column chart of weekly revenue for the last {len(weeks)} weeks. "
        f"The latest week, starting {latest['label']}, brought in {_money(latest['revenue'])}."
    )
    if low is not latest:
        text += f" The lowest week started {low['label']} at {_money(low['revenue'])}."
    if high is not latest:
        text += f" The highest week started {high['label']} at {_money(high['revenue'])}."
    return text


# --- Console entry point ----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the weekly sales brief to a PDF.")
    parser.add_argument("--week", help="any date in the week to report on (YYYY-MM-DD); default: latest week")
    parser.add_argument("--source", help="CSV path or published Google Sheet CSV URL; default: see load_data")
    parser.add_argument("--out", type=Path, help="PDF path; default: output/weekly_brief_<week end>.pdf")
    args = parser.parse_args(argv)

    try:
        brief = analyze_week(load_sales_data(args.source), args.week)
        out = args.out or DEFAULT_OUT_DIR / f"weekly_brief_{brief['week']['end']}.pdf"
        path = render_pdf(brief, out)
    except (DataLoadError, AnalysisError, ChartError, RenderError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Saved {path} ({path.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
