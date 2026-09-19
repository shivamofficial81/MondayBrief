"""Rebuild the README's sample report and screenshot from the bundled sample data.

    python scripts/make_docs_assets.py

Writes:
    docs/sample-report.pdf     always
    docs/report-preview.png    a picture of that PDF's page; needs PyMuPDF
                               (pip install -r requirements-dev.txt)

The report is dated Monday 14 Sep 2026, the day after the sample data's last week ends,
so it reads like a real Monday run and the files come out the same each time.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analyze import analyze_week  # noqa: E402
from src.load_data import load_sales_data  # noqa: E402
from src.render_pdf import render_pdf  # noqa: E402

DOCS = ROOT / "docs"
PREPARED_ON = date(2026, 9, 14)
PREVIEW_DPI = 150


def main() -> int:
    DOCS.mkdir(exist_ok=True)
    pdf_path = render_pdf(analyze_week(load_sales_data()), DOCS / "sample-report.pdf", generated_on=PREPARED_ON)
    print(f"Wrote {pdf_path.relative_to(ROOT)}")

    try:
        import pymupdf
    except ImportError:
        print("PyMuPDF isn't installed, so the PNG preview was skipped. Run: pip install -r requirements-dev.txt")
        return 0

    png_path = DOCS / "report-preview.png"
    with pymupdf.open(pdf_path) as doc:
        doc[0].get_pixmap(dpi=PREVIEW_DPI).save(str(png_path))
    print(f"Wrote {png_path.relative_to(ROOT)} ({png_path.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
