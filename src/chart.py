"""Weekly revenue chart for the brief, as a PNG returned base64-encoded for inlining.

    brief = analyze_week(df)
    b64 = weekly_revenue_chart_base64(brief)
    html = f'<img src="data:image/png;base64,{b64}" alt="Weekly revenue, last 8 weeks">'

The chart reads ``brief["weekly_revenue"]`` (built by ``analyze_week``), so it never
touches the DataFrame and the same series can be shown as a table beside it.

Design: SalesPulse's tokens on a light surface, one green accent, no chartjunk.
    * Emphasis form: the target week is the accent green, earlier weeks recede in gray.
    * Thin columns (<= 24 CSS px), 4px rounded tops, square on the baseline.
    * Solid hairline gridlines, no title, legend, box or tick marks. A single series
      needs no legend, and the PDF heading names the chart.
    * Values are labelled selectively: the target week and the highest and lowest weeks.
      The y-axis carries the rest.
    * Text stays in ink tokens, never the data color.

Run ``python -m src.chart [--week YYYY-MM-DD] [--source PATH_OR_URL] [--out FILE]`` to
save a preview PNG (default: output/weekly_revenue_preview.png).
"""

from __future__ import annotations

import argparse
import base64
import io
import sys
from pathlib import Path

from matplotlib import rc_context
from matplotlib.figure import Figure
from matplotlib.patches import PathPatch
from matplotlib.path import Path as MplPath
from matplotlib.patheffects import withStroke
from matplotlib.ticker import FuncFormatter, MaxNLocator

from .analyze import AnalysisError, analyze_week
from .load_data import DataLoadError, load_sales_data

# Tokens shared with SalesPulse (src/index.css and chartCommon.css there)
ACCENT = "#0F7A4D"
CONTEXT = "#B4BAC4"  # earlier weeks: recedes, but still clearly reads as a bar
SURFACE = "#FFFFFF"
GRID = "#E4E6EA"
INK = "#14161A"
INK_MUTED = "#6B7280"
# Liberation Sans is what Helvetica/Arial resolve to on Linux CI, and matches Arial's metrics;
# DejaVu Sans ships with matplotlib, so the chart always has a font to fall back on.
FONT_STACK = ["Segoe UI", "SF Pro Text", "Roboto", "Helvetica Neue", "Helvetica", "Arial", "Liberation Sans", "DejaVu Sans"]

PX = 1 / 96  # inches per CSS pixel: mark specs are written in px, matplotlib works in inches
BAR_MAX_WIDTH = 24 * PX
CORNER_RADIUS = 4 * PX
HAIRLINE_PT = 0.75  # 1 CSS px in points
MIN_WEEKS = 2  # one bar isn't a chart; the brief's headline numbers cover that case

DEFAULT_PREVIEW_PATH = Path(__file__).resolve().parent.parent / "output" / "weekly_revenue_preview.png"

_KAPPA = 0.5522847498  # Bezier control-point ratio for a quarter circle


class ChartError(Exception):
    """The chart can't be drawn from the given brief. The message is safe to print as-is."""


def weekly_revenue_chart_base64(brief: dict, **kwargs) -> str | None:
    """Base64 PNG of the weekly revenue chart, or None if there's too little history.

    Returns the bare base64 string (no ``data:`` prefix). Keyword arguments are passed
    to ``render_weekly_revenue_png``.
    """
    png = render_weekly_revenue_png(brief, **kwargs)
    return None if png is None else base64.b64encode(png).decode("ascii")


def render_weekly_revenue_png(
    brief: dict,
    *,
    width_in: float = 7.2,
    height_in: float = 2.9,
    dpi: int = 200,
    caption: str | None = "Week starting (Monday)",
) -> bytes | None:
    """PNG bytes of the chart, or None when fewer than MIN_WEEKS weeks are available.

    The default 7.2 x 2.9 in at 200 dpi (1440 x 580 px) fills an A4 page's text width
    at roughly 1:1, so 8.5 pt axis text stays 8.5 pt on the page. ``caption`` is the small
    x-axis note under the week labels; pass None when the surrounding page already says it.
    """
    weekly = _series(brief)
    if len(weekly) < MIN_WEEKS:
        return None

    n = len(weekly)
    revenues = [w["revenue"] for w in weekly]
    target = next((i for i, w in enumerate(weekly) if w["is_target"]), n - 1)
    partial = not brief.get("week", {}).get("is_complete", True)

    # The axis ends just above the tallest column (room for its label); gridlines and ticks
    # sit on clean numbers up to that point rather than forcing a round top.
    ymax = max(revenues) * 1.15 or 1.0
    ticks = [t for t in MaxNLocator(nbins=5, steps=[1, 2, 2.5, 5, 10], min_n_ticks=3)
             .tick_values(0, ymax) if 0 <= t <= ymax]

    with rc_context({"font.family": "sans-serif", "font.sans-serif": FONT_STACK}):
        fig = Figure(figsize=(width_in, height_in), dpi=dpi, facecolor=SURFACE)

        # Inner margins in inches, fixed up front so mark geometry can be computed exactly.
        # The left margin grows with the widest y label; the bottom holds one line of x labels,
        # plus a second line when the target week is flagged "so far", plus the caption if any.
        widest_label = max(len(f"${t:,.0f}") for t in ticks)
        left, right = max(0.5, 0.14 + widest_label * 0.072), 0.14
        bottom, top = 0.38 + (0.16 if partial else 0) + (0.20 if caption else 0), 0.18
        plot_w, plot_h = width_in - left - right, height_in - bottom - top
        ax = fig.add_axes([left / width_in, bottom / height_in, plot_w / width_in, plot_h / height_in])
        ax.set_facecolor(SURFACE)

        ax.set_xlim(-0.5, n - 0.5)
        ax.set_ylim(0, ymax)

        slot_in = plot_w / n
        half = min(BAR_MAX_WIDTH, slot_in * 0.6) / slot_in / 2  # half bar width, x data units
        rx = min(CORNER_RADIUS / slot_in, half)
        y_per_in = ymax / plot_h  # y data units per inch

        for i, revenue in enumerate(revenues):
            if revenue <= 0:
                continue
            ry = min(CORNER_RADIUS * y_per_in, revenue)
            ax.add_patch(
                PathPatch(
                    _column_path(i - half, i + half, revenue, rx, ry),
                    facecolor=ACCENT if i == target else CONTEXT,
                    edgecolor="none",
                    linewidth=0,
                    zorder=3,
                )
            )

        # Selective labels: the week being reported, plus the extremes as context
        labelled = {target, revenues.index(min(revenues)), revenues.index(max(revenues))}
        for i in labelled:
            ax.annotate(
                f"${revenues[i]:,.0f}",
                xy=(i, revenues[i]),
                xytext=(0, 3),
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=9,
                fontweight="bold" if i == target else "normal",
                color=INK,
                path_effects=[withStroke(linewidth=3, foreground=SURFACE)],  # halo over gridlines
                zorder=5,
            )

        # Axes: recessive, hairline, no ticks
        ax.set_yticks(ticks)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:,.0f}"))
        ax.grid(axis="y", color=GRID, linewidth=HAIRLINE_PT, linestyle="-")
        ax.set_axisbelow(True)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(GRID)
        ax.spines["bottom"].set_linewidth(HAIRLINE_PT)
        ax.tick_params(axis="both", length=0, labelsize=8.5, labelcolor=INK_MUTED, pad=6)

        ax.set_xticks(range(n))
        tick_labels = [w["label"] + ("\nso far" if partial and i == target else "") for i, w in enumerate(weekly)]
        for i, label in enumerate(ax.set_xticklabels(tick_labels)):
            if i == target:
                label.set_color(INK)
                label.set_fontweight("bold")
        if caption:
            ax.set_xlabel(caption, loc="left", fontsize=8, color=INK_MUTED, labelpad=8)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=dpi, facecolor=SURFACE, metadata={"Software": None})
        return buf.getvalue()


def _series(brief: dict) -> list[dict]:
    weekly = brief.get("weekly_revenue") if isinstance(brief, dict) else None
    if weekly is None:
        raise ChartError("The brief has no 'weekly_revenue' series. Build it with analyze_week().")
    return weekly


def _column_path(x0: float, x1: float, top: float, rx: float, ry: float) -> MplPath:
    """A column from y=0 up to ``top``: square at the baseline, rounded 4px at the data end.

    ``rx``/``ry`` are the corner radius in x/y data units (they differ because the
    axes aren't square), which makes each corner a true circle on screen.
    """
    k = _KAPPA
    vertices = [
        (x0, 0),
        (x0, top - ry),
        (x0, top - ry * (1 - k)), (x0 + rx * (1 - k), top), (x0 + rx, top),
        (x1 - rx, top),
        (x1 - rx * (1 - k), top), (x1, top - ry * (1 - k)), (x1, top - ry),
        (x1, 0),
        (x0, 0),
    ]
    codes = [
        MplPath.MOVETO, MplPath.LINETO,
        MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4,
        MplPath.LINETO,
        MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4,
        MplPath.LINETO,
        MplPath.CLOSEPOLY,
    ]
    return MplPath(vertices, codes)


# --- Console preview --------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a preview of the weekly revenue chart.")
    parser.add_argument("--week", help="any date in the target week (YYYY-MM-DD); default: latest week")
    parser.add_argument("--source", help="CSV path or published Google Sheet CSV URL; default: see load_data")
    parser.add_argument("--out", type=Path, default=DEFAULT_PREVIEW_PATH, help="where to save the PNG")
    args = parser.parse_args(argv)

    try:
        brief = analyze_week(load_sales_data(args.source), args.week)
        png = render_weekly_revenue_png(brief)
    except (DataLoadError, AnalysisError, ChartError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if png is None:
        print(f"Error: need at least {MIN_WEEKS} weeks of data to draw a weekly chart.", file=sys.stderr)
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(png)
    b64_len = len(base64.b64encode(png))
    print(f"Saved {args.out} ({len(png) / 1024:.0f} KB; base64 for inlining: {b64_len:,} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
