"""Load and validate the sales data behind the weekly brief.

The source is either a published Google Sheet CSV URL or a local CSV file:

    * an explicit ``source`` argument (URL or file path), else
    * the ``SALES_DATA_URL`` environment variable, else
    * the bundled ``data/sample_sales.csv`` so the repo runs with no network.

If a URL is configured but can't be fetched, loading fails loudly. It does not
quietly fall back to the sample file, because a client report built from
sample data would be worse than no report.

Every problem a person can fix (missing column, bad date, unreachable URL) is
raised as a ``DataLoadError`` whose message is written to be printed as-is.

Run ``python -m src.load_data [source]`` to check a source from the console.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pandas as pd
import requests

REQUIRED_COLUMNS = ["Date", "Product", "Region", "Amount"]
DEFAULT_LOCAL_PATH = Path(__file__).resolve().parent.parent / "data" / "sample_sales.csv"
URL_ENV_VAR = "SALES_DATA_URL"
REQUEST_TIMEOUT_SECONDS = 20
MAX_EXAMPLES = 5  # bad rows quoted per problem, so a broken sheet doesn't flood the console


class DataLoadError(Exception):
    """The data source has a problem the user can fix. The message is safe to print as-is."""


def load_sales_data(source: str | os.PathLike | None = None) -> pd.DataFrame:
    """Return a clean DataFrame with columns Date, Product, Region, Amount.

    Date is datetime64 (midnight), Amount is float, Product and Region are
    stripped strings. Rows are sorted by Date and the index is reset.
    Raises DataLoadError with a plain-English message for anything wrong.
    """
    source = _resolve_source(source)
    content = _fetch_url(source) if _is_url(source) else _read_local(Path(source))
    raw = _parse_csv(content, _describe(source))
    df = _select_required_columns(raw)
    return _clean(df)


# --- Source resolution and reading ------------------------------------------------


def _resolve_source(source: str | os.PathLike | None) -> str | Path:
    if source is not None and str(source).strip():
        return str(source).strip() if _is_url(str(source).strip()) else Path(source)
    from_env = os.environ.get(URL_ENV_VAR, "").strip()
    return from_env if from_env else DEFAULT_LOCAL_PATH


def _is_url(source: str | os.PathLike) -> bool:
    return str(source).lower().startswith(("http://", "https://"))


def _describe(source: str | Path) -> str:
    """Short label for messages. Shows only the host of a URL, never the full link."""
    if _is_url(source):
        host = urlparse(str(source)).netloc
        return f"the Google Sheet URL ({host})" if host else "the Google Sheet URL"
    return f"'{source}'"


def _sentence_start(label: str) -> str:
    return label[:1].upper() + label[1:]


def _fetch_url(url: str) -> bytes:
    label = _describe(url)
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        raise DataLoadError(
            f"Timed out after {REQUEST_TIMEOUT_SECONDS}s waiting for {label}. "
            "Try again in a minute."
        ) from None
    except requests.exceptions.ConnectionError:
        raise DataLoadError(
            f"Couldn't reach {label}. Check your internet connection and that the URL is correct."
        ) from None
    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        raise DataLoadError(
            f"{_sentence_start(label)} answered with an HTTP {status} error. Check the link is right and that "
            "the sheet is still published to the web."
        ) from None
    except requests.exceptions.RequestException:
        raise DataLoadError(
            f"{_sentence_start(label)} doesn't look like a valid link. It should start with https://."
        ) from None

    content_type = response.headers.get("Content-Type", "").lower()
    if "text/html" in content_type or response.content.lstrip()[:1] == b"<":
        raise DataLoadError(
            f"{_sentence_start(label)} returned a web page instead of CSV data. In Google Sheets, use "
            "File > Share > Publish to web, pick the sheet tab and 'Comma-separated values "
            "(.csv)', then use the link it gives you."
        )
    return response.content


def _read_local(path: Path) -> bytes:
    if not path.is_file():
        raise DataLoadError(
            f"Data file not found: '{path}'. Point to an existing CSV, or set the "
            f"{URL_ENV_VAR} environment variable to a published Google Sheet CSV URL."
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise DataLoadError(f"Couldn't read '{path}': {exc.strerror or exc}.") from None


def _parse_csv(content: bytes, label: str) -> pd.DataFrame:
    try:
        # Read everything as text; typing and validation happen in _clean so that
        # bad values can be reported with their row numbers.
        return pd.read_csv(
            io.BytesIO(content), dtype=str, keep_default_na=False, encoding="utf-8-sig"
        )
    except pd.errors.EmptyDataError:
        raise DataLoadError(f"{label} is empty. Expected a header row and data rows.") from None
    except (pd.errors.ParserError, UnicodeDecodeError):
        raise DataLoadError(
            f"{label} couldn't be read as a CSV file. Make sure it's plain CSV, "
            "not an Excel file or a web page."
        ) from None


# --- Validation and cleaning ------------------------------------------------------


def _select_required_columns(raw: pd.DataFrame) -> pd.DataFrame:
    """Match headers ignoring case and stray spaces; keep only the required columns."""
    by_key = {str(col).strip().lower(): col for col in raw.columns}
    missing = [name for name in REQUIRED_COLUMNS if name.lower() not in by_key]
    if missing:
        found = ", ".join(str(c) for c in raw.columns) or "none"
        raise DataLoadError(
            f"Missing required column(s): {', '.join(missing)}. "
            f"Found: {found}. "
            f"Expected: {', '.join(REQUIRED_COLUMNS)} (capitalisation doesn't matter)."
        )
    df = raw[[by_key[name.lower()] for name in REQUIRED_COLUMNS]].copy()
    df.columns = REQUIRED_COLUMNS
    return df


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    for col in REQUIRED_COLUMNS:
        df[col] = df[col].str.strip()

    # Rows that are blank across all four columns are spreadsheet padding, not data.
    df = df[(df != "").any(axis=1)].copy()
    if df.empty:
        raise DataLoadError(
            f"The data has the right columns ({', '.join(REQUIRED_COLUMNS)}) but no rows."
        )

    dates = pd.to_datetime(df["Date"].where(df["Date"] != ""), errors="coerce")
    amounts = _parse_amount(df["Amount"])

    problems = [
        *_row_problems(df, df["Product"] == "", "Product", "is empty"),
        *_row_problems(df, df["Region"] == "", "Region", "is empty"),
        *_row_problems(df, dates.isna(), "Date", "isn't a valid date"),
        *_row_problems(df, ~np.isfinite(amounts), "Amount", "isn't a valid number"),
    ]
    if problems:
        raise DataLoadError(
            "Some rows have problems. Row numbers match your sheet (row 1 is the header):\n"
            + "\n".join(f"  - {p}" for p in problems)
        )

    df["Date"] = dates.dt.normalize()
    df["Amount"] = amounts.astype(float)
    return df.sort_values("Date", kind="stable").reset_index(drop=True)


def _parse_amount(series: pd.Series) -> pd.Series:
    """Text to float. Tolerates '$1,234.50' but not ambiguous formats like '1.234,50'."""
    text = series.str.replace(r"[\s$€£¥]", "", regex=True)
    # Only strip commas that are clearly thousands separators; anything else stays
    # non-numeric and gets reported, rather than being silently misread.
    grouped = text.str.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?")
    text = text.mask(grouped, text.str.replace(",", "", regex=False))
    return pd.to_numeric(text, errors="coerce")


def _row_problems(df: pd.DataFrame, bad: pd.Series, column: str, what: str) -> list[str]:
    count = int(bad.sum())
    if not count:
        return []
    examples = []
    for idx in df.index[bad][:MAX_EXAMPLES]:
        shown = df.at[idx, column]
        examples.append(f"row {idx + 2}" + (f" ('{shown}')" if shown else ""))
    more = f" and {count - MAX_EXAMPLES} more" if count > MAX_EXAMPLES else ""
    noun = "row" if count == 1 else "rows"
    return [f"{column}: {count} {noun} where the value {what}, e.g. {', '.join(examples)}{more}"]


# --- Console check ----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        df = load_sales_data(args[0] if args else None)
    except DataLoadError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Loaded {len(df)} rows.")
    print(f"Dates: {df['Date'].min():%Y-%m-%d} to {df['Date'].max():%Y-%m-%d}")
    print(f"Total revenue: {df['Amount'].sum():,.2f}")
    print()
    print(df.dtypes.to_string())
    print()
    print(df.head().to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
