# MondayBrief — Project Context

## What this is
An automated weekly sales report generator, built as a portfolio demo
for Upwork. It reads sales data, computes a weekly brief, renders a
branded PDF, and runs itself on a schedule via GitHub Actions.
Target audience: small business owners who rebuild the same report
manually every week.

Companion project to SalesPulse (a React KPI dashboard) — same studio,
same visual language.

## Stack (do not deviate)
- Python 3.11+, pandas for analysis
- Jinja2 HTML template rendered to PDF via Playwright page.pdf()
  (NOT ReportLab — the PDF is the portfolio artifact and must look designed)
- matplotlib for the embedded chart, rendered to PNG, inlined as base64
- Data source: a published Google Sheet CSV export URL, with a local
  CSV fallback so the repo runs without network access
- Scheduling: GitHub Actions cron
- Windows dev machine, PowerShell — all commands must work there

## Delivery (important)
Primary output: the generated PDF is committed to reports/ with a dated
filename (reports/YYYY-MM-DD.pdf) by the scheduled workflow. This is the
proof-of-automation artifact.

Email delivery: write a send_report() function supporting an SMTP/API-key
provider (Resend-style), fully documented in the README, but DISABLED in
the workflow and requiring no credentials to run the project. Never
commit, request, or hardcode any credential. No OAuth, no service-account
JSON.

## Design rules for the PDF
- Match SalesPulse's visual language: light theme, one green accent,
  flat surfaces, generous whitespace, system font stack
- A4 portrait, one page if possible, two maximum
- Must look like a document a consultant would send a client
- Every section readable at a glance; no dense tables

## Features (v1 scope — nothing more)
1. Load data (Sheet URL or local CSV fallback)
2. Weekly brief: revenue this week vs last week, order count, AOV,
   top 5 products, best and worst day
3. Anomaly flags: call out any metric that moved more than ±20% WoW
4. Plain-English summary paragraph with a recommended action,
   conditional on the data (same philosophy as SalesPulse insights)
5. Branded PDF with an embedded weekly revenue chart
6. GitHub Actions workflow: Mondays 09:00, commits the PDF to reports/
7. Friendly failure: clear console errors for missing columns or
   unreachable data source

## Out of scope (refuse politely if I ask mid-build)
Web UI, database, Google OAuth, multiple templates, Slack delivery,
multi-tenant support