"""Compute the weekly brief from a sales DataFrame.

    brief = analyze_week(df)                 # latest week in the data
    brief = analyze_week(df, "2026-09-07")   # the Mon-Sun week containing this date
    print(brief["summary"])

Weeks run Monday to Sunday. ``analyze_week`` returns one JSON-friendly dict
(plain str / int / float / list / dict, dates as ISO strings) so the PDF
template can consume it directly.

Percent changes are rounded to one decimal *before* any threshold is applied,
so a flag, the summary and the number a reader sees always agree.

Run ``python -m src.analyze [--week YYYY-MM-DD] [--source PATH_OR_URL]`` to
print the brief to the console.
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from datetime import date, datetime

import pandas as pd

from .load_data import REQUIRED_COLUMNS, DataLoadError, load_sales_data

ANOMALY_THRESHOLD_PERCENT = 20  # a metric moving more than this either way is flagged
STEADY_PERCENT = 5  # within this either way counts as "flat"
TOP_PRODUCT_COUNT = 5
CHART_WEEKS = 8  # weeks of history in the brief's weekly revenue series, target week included

_METRIC_LABELS = {"revenue": "revenue", "orders": "orders", "aov": "average order value"}


class AnalysisError(Exception):
    """The brief can't be built for the requested week. The message is safe to print as-is."""


# --- Public entry point -----------------------------------------------------------


def analyze_week(df: pd.DataFrame, target_week: str | date | datetime | None = None) -> dict:
    """Build the weekly brief for the Mon-Sun week containing ``target_week``.

    ``target_week`` defaults to the week containing the latest date in the data.
    Raises AnalysisError if the data or week can't produce a brief.
    """
    _check_frame(df)
    data_start, data_end = df["Date"].min(), df["Date"].max()

    anchor = _parse_target(target_week) if target_week is not None else data_end
    start, end = _week_bounds(anchor)
    prev_start, prev_end = start - pd.Timedelta(days=7), end - pd.Timedelta(days=7)

    week_df = _slice(df, start, end)
    if week_df.empty:
        raise AnalysisError(
            f"No orders found for the week of {_week_label(start, end)}. "
            f"The data covers {data_start:%d %b %Y} to {data_end:%d %b %Y}."
        )
    prev_df = _slice(df, prev_start, prev_end)

    this_week, last_week = _totals(week_df), _totals(prev_df)
    metrics = {
        name: {
            "this_week": this_week[name],
            "last_week": last_week[name],
            "change_pct": _pct_change(this_week[name], last_week[name]),
        }
        for name in ("revenue", "orders", "aov")
    }

    # Days after the last date in the data haven't happened yet, so they can't be "worst".
    last_day = min(end, data_end)
    brief = {
        "week": {
            "start": start.date().isoformat(),
            "end": end.date().isoformat(),
            "label": _week_label(start, end),
            "is_complete": bool(data_end >= end),
            "covered_through": last_day.date().isoformat(),
        },
        "previous_week": {
            "start": prev_start.date().isoformat(),
            "end": prev_end.date().isoformat(),
            "label": _week_label(prev_start, prev_end),
            "has_orders": bool(last_week["orders"]),
        },
        "metrics": metrics,
        "top_products": _top_products(week_df, this_week["revenue"]),
        **_best_and_worst_day(week_df, start, last_day),
        "anomalies": _anomalies(metrics),
        "weekly_revenue": _weekly_revenue(df, start),
    }
    brief["summary_parts"] = build_summary_parts(brief)
    brief["summary"] = build_summary(brief)
    return brief


# --- Metrics ----------------------------------------------------------------------


def _check_frame(df: pd.DataFrame) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise AnalysisError(
            f"The data is missing column(s): {', '.join(missing)}. "
            "Load it with load_sales_data() first."
        )
    if df.empty:
        raise AnalysisError("There are no orders to analyse.")


def _parse_target(value: str | date | datetime) -> pd.Timestamp:
    try:
        return pd.Timestamp(value)
    except (ValueError, TypeError):
        raise AnalysisError(
            f"'{value}' isn't a valid date. Use YYYY-MM-DD, e.g. 2026-09-07."
        ) from None


def _week_bounds(anchor: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp]:
    monday = (anchor - pd.Timedelta(days=anchor.weekday())).normalize()
    return monday, monday + pd.Timedelta(days=6)


def _week_label(start: pd.Timestamp, end: pd.Timestamp) -> str:
    """'7-13 Sep 2026', '28 Sep-4 Oct 2026' or '29 Dec 2026-4 Jan 2027' (ASCII-only for any console)."""
    if start.year != end.year:
        return f"{start.day} {start:%b %Y}-{end.day} {end:%b %Y}"
    if start.month != end.month:
        return f"{start.day} {start:%b}-{end.day} {end:%b %Y}"
    return f"{start.day}-{end.day} {end:%b %Y}"


def _slice(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    return df[(df["Date"] >= start) & (df["Date"] <= end)]


def _totals(week_df: pd.DataFrame) -> dict:
    orders = int(len(week_df))
    revenue = round(float(week_df["Amount"].sum()), 2)
    aov = round(revenue / orders, 2) if orders else None
    return {"revenue": revenue, "orders": orders, "aov": aov}


def _pct_change(current: float | None, previous: float | None) -> float | None:
    """Week-over-week change in percent, rounded to 1 decimal. None if there's no baseline."""
    if current is None or previous is None or previous <= 0:
        return None
    return round((current - previous) / previous * 100, 1)


def _top_products(week_df: pd.DataFrame, total_revenue: float) -> list[dict]:
    grouped = (
        week_df.groupby("Product")
        .agg(revenue=("Amount", "sum"), orders=("Amount", "size"))
        .reset_index()
        .sort_values(["revenue", "Product"], ascending=[False, True])  # ties: A-Z, stable
        .head(TOP_PRODUCT_COUNT)
    )
    return [
        {
            "rank": rank,
            "product": str(row.Product),
            "revenue": round(float(row.revenue), 2),
            "orders": int(row.orders),
            "share_pct": round(float(row.revenue) / total_revenue * 100, 1) if total_revenue else 0.0,
        }
        for rank, row in enumerate(grouped.itertuples(index=False), start=1)
    ]


def _best_and_worst_day(
    week_df: pd.DataFrame, start: pd.Timestamp, last_day: pd.Timestamp
) -> dict:
    """Best and worst day by revenue. A day with no orders counts as $0. Ties: earliest day."""
    days = pd.date_range(start, last_day, freq="D")
    daily = (
        week_df.groupby("Date")
        .agg(revenue=("Amount", "sum"), orders=("Amount", "size"))
        .reindex(days, fill_value=0)
    )

    def describe(day: pd.Timestamp) -> dict:
        return {
            "date": day.date().isoformat(),
            "weekday": day.strftime("%A"),
            "revenue": round(float(daily.at[day, "revenue"]), 2),
            "orders": int(daily.at[day, "orders"]),
        }

    return {
        "best_day": describe(daily["revenue"].idxmax()),
        "worst_day": describe(daily["revenue"].idxmin()),
    }


def _weekly_revenue(df: pd.DataFrame, target_start: pd.Timestamp) -> list[dict]:
    """Revenue for each of the last CHART_WEEKS weeks, oldest first, ending with the target week.

    Feeds the chart and doubles as its table view. A week inside the data's date range
    with no orders is $0; weeks before the data begins are left out, because "no data
    yet" is not the same as "no sales".
    """
    first_data_week, _ = _week_bounds(df["Date"].min())
    first = max(target_start - pd.Timedelta(weeks=CHART_WEEKS - 1), first_data_week)
    starts = pd.date_range(first, target_start, freq="7D")

    days = df["Date"].dt.normalize()
    in_range = df[(days >= first) & (days <= target_start + pd.Timedelta(days=6))]
    in_days = in_range["Date"].dt.normalize()
    monday = in_days - pd.to_timedelta(in_days.dt.weekday, unit="D")
    per_week = (
        in_range.groupby(monday)
        .agg(revenue=("Amount", "sum"), orders=("Amount", "size"))
        .reindex(starts, fill_value=0)
    )
    return [
        {
            "start": start.date().isoformat(),
            "end": (start + pd.Timedelta(days=6)).date().isoformat(),
            "label": f"{start.day} {start:%b}",
            "revenue": round(float(row.revenue), 2),
            "orders": int(row.orders),
            "is_target": bool(start == target_start),
        }
        for start, row in per_week.iterrows()
    ]


def _anomalies(metrics: dict) -> list[dict]:
    flags = []
    for name, values in metrics.items():
        pct = values["change_pct"]
        if pct is not None and abs(pct) > ANOMALY_THRESHOLD_PERCENT:
            flags.append(
                {
                    "metric": name,
                    "label": _METRIC_LABELS[name],
                    "direction": "up" if pct > 0 else "down",
                    "change_pct": pct,
                    "this_week": values["this_week"],
                    "last_week": values["last_week"],
                }
            )
    return flags


# --- Plain-English summary --------------------------------------------------------
# Same shape as SalesPulse's insights: small builders, each producing an observation
# or an action in everyday words, chosen by what the numbers actually say.


def build_summary_parts(brief: dict) -> dict:
    """The summary in two pieces, for layouts that set the action apart.

    ``overview`` is what happened and what stood out; ``action`` is the recommended
    action as a capitalised sentence, ready to display under its own label.
    """
    diagnosis, action = _action_parts(brief)
    sentences = [
        _revenue_sentence(brief),
        _orders_sentence(brief),
        _partial_week_sentence(brief),
        _anomaly_sentence(brief),
        _highlights_sentence(brief),
        diagnosis,
    ]
    return {
        "overview": " ".join(s for s in sentences if s),
        "action": action[0].upper() + action[1:],
    }


def build_summary(brief: dict) -> str:
    """One paragraph: what happened, what stood out, and one recommended action."""
    parts = build_summary_parts(brief)
    action = parts["action"]
    return f"{parts['overview']} Recommended action: {action[0].lower()}{action[1:]}"


def _money(value: float, decimals: int = 0) -> str:
    return f"${value:,.{decimals}f}"


def _move(pct: float | None) -> str:
    """'up 62%', 'down 37%' or 'flat', for a rounded percent change."""
    if pct is None or abs(pct) <= STEADY_PERCENT:
        return "flat"
    return f"{'up' if pct > 0 else 'down'} {abs(pct):.0f}%"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _has_baseline(brief: dict) -> bool:
    """True when last week has a usable baseline (orders, and a positive revenue and AOV)."""
    return all(m["change_pct"] is not None for m in brief["metrics"].values())


def _revenue_sentence(brief: dict) -> str:
    label = brief["week"]["label"]
    revenue = brief["metrics"]["revenue"]
    if not _has_baseline(brief):
        return (
            f"Revenue for {label} came to {_money(revenue['this_week'])}. "
            "There's no earlier week in the data to compare against yet."
        )
    change = _move(revenue["change_pct"])
    prev = _money(revenue["last_week"])
    relation = "in line with" if change == "flat" else f"{change} from"
    return (
        f"Revenue for {label} came to {_money(revenue['this_week'])}, "
        f"{relation} {prev} the week before."
    )


def _orders_sentence(brief: dict) -> str:
    orders, aov = brief["metrics"]["orders"], brief["metrics"]["aov"]
    base = f"That was {orders['this_week']} orders"
    if not _has_baseline(brief):
        return f"{base} at an average order value of {_money(aov['this_week'], 2)}."
    return (
        f"{base} ({_move(orders['change_pct'])}) at an average order value of "
        f"{_money(aov['this_week'], 2)} ({_move(aov['change_pct'])})."
    )


def _partial_week_sentence(brief: dict) -> str:
    if brief["week"]["is_complete"]:
        return ""
    last = datetime.fromisoformat(brief["week"]["covered_through"])
    return (
        f"Heads up: the data only runs through {last:%a} {last.day} {last:%b}, "
        "so this week may be incomplete."
    )


def _anomaly_sentence(brief: dict) -> str:
    if not brief["anomalies"]:
        return ""
    names = _join([a["label"] for a in brief["anomalies"]])
    return (
        f"{names[0].upper()}{names[1:]} moved more than {ANOMALY_THRESHOLD_PERCENT}% "
        "versus the week before, which is worth a closer look."
    )


def _highlights_sentence(brief: dict) -> str:
    top = brief["top_products"][0]
    text = (
        f"{top['product']} was the top seller at {_money(top['revenue'])} "
        f"({top['share_pct']:.0f}% of revenue)."
    )
    best, worst = brief["best_day"], brief["worst_day"]
    if best["date"] != worst["date"]:
        text += (
            f" {best['weekday']} was your strongest day ({_money(best['revenue'])}) "
            f"and {worst['weekday']} the quietest ({_money(worst['revenue'])})."
        )
    return text


def _action_parts(brief: dict) -> tuple[str, str]:
    """(diagnosis, action): a short read on why, then the one thing to do about it."""
    if not brief["week"]["is_complete"]:
        return "", "confirm the week's data is complete before acting on these numbers."
    if not _has_baseline(brief):
        return "", "treat this week as your baseline and compare against it in next week's brief."

    metrics = brief["metrics"]
    revenue = metrics["revenue"]["change_pct"]
    orders, aov = metrics["orders"]["change_pct"], metrics["aov"]["change_pct"]
    volume_led = abs(orders) >= abs(aov)  # revenue = orders x average order value
    top = brief["top_products"][0]["product"]

    # Each branch gives a short diagnosis (may be empty) and the action that follows from it.
    if revenue < -ANOMALY_THRESHOLD_PERCENT:
        if volume_led:
            diagnosis = "The drop came mostly from fewer orders, so this looks like a demand problem."
            action = (
                "check what changed during the week, such as a paused campaign, a stock-out "
                "or a site issue, and consider a short promotion to win shoppers back."
            )
        else:
            diagnosis = (
                "The drop came mostly from a lower average order, so shoppers are spending "
                "less each time."
            )
            action = (
                "review recent pricing, discounts and bundles to bring the average order "
                "back up."
            )
    elif revenue > ANOMALY_THRESHOLD_PERCENT:
        if volume_led:
            diagnosis = "The jump came mostly from more orders."
            action = (
                "make sure stock and fulfilment can keep pace, and note what drove the "
                "surge so you can repeat it."
            )
        else:
            diagnosis = "The jump came mostly from a bigger average order."
            action = (
                "find out which products or bundles drove that and feature them more "
                "prominently."
            )
    elif revenue > STEADY_PERCENT:
        diagnosis = "A healthy gain."
        action = f"keep doing what's working, and consider giving {top} extra promotion."
    elif revenue < -STEADY_PERCENT:
        diagnosis = "A modest slip, nothing alarming yet."
        action = "if it continues next week, look at recent pricing, promotions and stock levels."
    else:
        diagnosis = "Sales held steady."
        action = "this is a good moment to test a promotion or a new bundle."
    return diagnosis, action


# --- Console output ---------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the weekly sales brief.")
    parser.add_argument("--week", help="any date in the week to report on (YYYY-MM-DD); default: latest week")
    parser.add_argument("--source", help="CSV path or published Google Sheet CSV URL; default: see load_data")
    args = parser.parse_args(argv)

    try:
        brief = analyze_week(load_sales_data(args.source), args.week)
    except (DataLoadError, AnalysisError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps({k: v for k, v in brief.items() if k != "summary"}, indent=2))
    print()
    print("SUMMARY")
    print(textwrap.fill(brief["summary"], width=88))
    return 0


if __name__ == "__main__":
    sys.exit(main())
