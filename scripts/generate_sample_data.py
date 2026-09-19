"""Generate data/sample_sales.csv: 8 weeks of daily orders for a small online store.

Deterministic (fixed seed) so the committed CSV can be reproduced exactly:

    python scripts/generate_sample_data.py

Shape of the data, on purpose:
  * weeks 1-4  steady baseline
  * week 5     visible dip (fewer orders, and Europe nearly disappears)
  * weeks 6-7  recovery
  * week 8     strong finish (more orders, autumn-driven product mix shift)
"""

from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path

import numpy as np

SEED = 42
FIRST_MONDAY = date(2026, 7, 20)  # 8 full Mon-Sun weeks, ending Sunday 2026-09-13
WEEKS = 8
BASE_ORDERS_PER_DAY = 16

OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "sample_sales.csv"

# product -> (unit price, base popularity weight)
PRODUCTS = {
    "Ceramic Mug": (24.00, 20),
    "Linen Tote Bag": (32.00, 16),
    "Soy Candle": (18.00, 22),
    "Notebook Set": (15.00, 18),
    "Desk Plant Pot": (28.00, 12),
    "Enamel Pin Pack": (12.00, 14),
    "Throw Blanket": (68.00, 8),
    "Leather Card Holder": (38.00, 10),
}

# region -> base weight
REGIONS = {
    "North America": 42,
    "Europe": 26,
    "UK": 18,
    "Asia Pacific": 14,
}

# Day-of-week demand (Mon..Sun): soft midweek, busier weekend
DOW_FACTOR = [1.00, 0.95, 0.90, 0.95, 1.05, 1.15, 1.10]

# Weekly volume multipliers (week 1..8): dip in week 5, strong week 8
WEEK_FACTOR = [1.00, 1.02, 0.98, 1.03, 0.62, 0.97, 1.05, 1.42]


def _weights(base: dict[str, int], overrides: dict[str, float] | None = None) -> np.ndarray:
    w = np.array([base[k] * (overrides or {}).get(k, 1.0) for k in base], dtype=float)
    return w / w.sum()


def _stratified(names: list[str], p: np.ndarray, n: int, rng: np.random.Generator) -> np.ndarray:
    """n shuffled labels matching proportions p almost exactly.

    Plain iid sampling lets a $68 product swing 4-9 orders week to week, which
    throws revenue around by +/-25% and buries the dip. Allocating the weekly
    mix proportionally (random only in the rounding remainder) keeps baseline
    weeks steady while every order is still assigned at random within the week.
    """
    expected = p * n
    counts = np.floor(expected).astype(int)
    remainder = n - counts.sum()
    if remainder:
        frac = expected - counts
        extra = rng.choice(len(names), size=remainder, replace=False, p=frac / frac.sum())
        counts[extra] += 1
    labels = np.repeat(np.array(names), counts)
    rng.shuffle(labels)
    return labels


def generate() -> list[tuple[str, str, str, float]]:
    rng = np.random.default_rng(SEED)
    product_names = list(PRODUCTS)
    region_names = list(REGIONS)
    rows: list[tuple[str, str, str, float]] = []

    for week in range(WEEKS):
        region_over = {"Europe": 0.2} if week == 4 else None
        product_over = (
            {"Throw Blanket": 3.0, "Soy Candle": 1.5, "Enamel Pin Pack": 0.6}
            if week == 7
            else None
        )
        region_p = _weights(REGIONS, region_over)
        product_p = _weights({k: v[1] for k, v in PRODUCTS.items()}, product_over)

        # Daily order counts for the week. Tight noise (~8%) keeps the baseline
        # steady so the dip and the final-week surge are the only moves that
        # clear the +/-20% WoW bar.
        day_counts = []
        for dow in range(7):
            lam = BASE_ORDERS_PER_DAY * WEEK_FACTOR[week] * DOW_FACTOR[dow]
            day_counts.append(max(0, round(rng.normal(lam, lam * 0.08))))

        n_week = sum(day_counts)
        products = _stratified(product_names, product_p, n_week, rng)
        regions = _stratified(region_names, region_p, n_week, rng)

        i = 0
        for dow, n_orders in enumerate(day_counts):
            day = FIRST_MONDAY + timedelta(days=week * 7 + dow)
            for _ in range(n_orders):
                product, region = str(products[i]), str(regions[i])
                i += 1
                unit_price = PRODUCTS[product][0]
                # Multi-unit orders only for cheaper items; a 3x blanket is rare
                qty = 1
                if unit_price <= 30:
                    qty = int(rng.choice([1, 2, 3], p=[0.75, 0.20, 0.05]))
                amount = unit_price * qty
                if rng.random() < 0.12:  # occasional promo code, 10% off
                    amount *= 0.90
                rows.append((day.isoformat(), product, region, round(amount, 2)))

    return rows


def main() -> None:
    rows = generate()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Date", "Product", "Region", "Amount"])
        for date_str, product, region, amount in rows:
            writer.writerow([date_str, product, region, f"{amount:.2f}"])
    print(f"Wrote {len(rows)} rows to {OUT_PATH}")


if __name__ == "__main__":
    main()
