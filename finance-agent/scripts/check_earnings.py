#!/usr/bin/env python3
"""
Earnings calendar agent.

Separate script and workflow from the price/news alert agent
(check_prices.py). Once a month it pulls each watched ticker's next
reporting date, this quarter's analyst EPS/revenue estimates, and the
previous quarter's actual-vs-estimate EPS from Yahoo Finance's quoteSummary
API, writes finance-agent/data/earnings_calendar.json, and emails the full
compiled table for every company in the basket. Every day it also checks
that same file for any ticker whose reporting date is tomorrow and sends a
one-day-ahead reminder email if so.

Unlike the plain chart endpoint check_prices.py uses, quoteSummary requires
a session cookie + crumb handshake, done here with http.cookiejar/urllib
(stdlib only, no dependency installation step needed). Small/thinly-covered
tickers may simply have no analyst estimate data on Yahoo - those fields are
left as None ("N/A" in the email) rather than guessed.
"""

import http.cookiejar
import json
import os
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "config.json")
CALENDAR_PATH = os.path.join(REPO_ROOT, "data", "earnings_calendar.json")

REQUEST_TIMEOUT = 10
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def log(msg):
    print(f"[earnings-agent] {msg}", flush=True)


def load_tickers():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    return config.get("tickers", [])


class YahooSession:
    """Handles Yahoo Finance's crumb/cookie handshake, required by the
    quoteSummary API (unlike the plain chart endpoint the price agent uses)."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.crumb = None

    def _get(self, url):
        req = urllib.request.Request(url, headers=HEADERS)
        with self.opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.read()

    def ensure_crumb(self):
        if self.crumb is not None:
            return self.crumb
        try:
            self._get("https://fc.yahoo.com/")
        except Exception as exc:  # noqa: BLE001 - cookie warm-up is best-effort
            log(f"WARN: Yahoo cookie warm-up failed: {exc}")
        try:
            self.crumb = (
                self._get("https://query2.finance.yahoo.com/v1/test/getcrumb")
                .decode("utf-8")
                .strip()
            )
        except Exception as exc:  # noqa: BLE001
            log(f"WARN: could not obtain Yahoo crumb: {exc}")
            self.crumb = ""
        return self.crumb

    def quote_summary(self, ticker, modules):
        crumb = self.ensure_crumb()
        url = (
            "https://query2.finance.yahoo.com/v10/finance/quoteSummary/"
            f"{urllib.parse.quote(ticker)}?modules={','.join(modules)}"
        )
        if crumb:
            url += f"&crumb={urllib.parse.quote(crumb)}"
        raw = self._get(url)
        return json.loads(raw)


def _first(items):
    return items[0] if items else None


def _fmt_value(field):
    """quoteSummary numeric fields usually look like {"raw": x, "fmt": "..."}."""
    if isinstance(field, dict):
        return field.get("fmt", field.get("raw"))
    return field


def parse_earnings_date(calendar_events):
    """Returns an ISO date string (YYYY-MM-DD) for the next earnings date, or None."""
    try:
        raw = _first(calendar_events["earnings"]["earningsDate"])
        if isinstance(raw, dict):
            return raw.get("fmt")
        return None
    except (KeyError, IndexError, TypeError):
        return None


def new_calendar_entry(ticker):
    return {
        "ticker": ticker,
        "name": ticker,
        "earningsDate": None,
        "currentQuarterEpsEstimate": None,
        "currentQuarterRevenueEstimate": None,
        "previousQuarterLabel": None,
        "previousQuarterEpsActual": None,
        "previousQuarterEpsEstimate": None,
        "reminderSent": False,
    }


def fetch_company_earnings(session, ticker):
    """Returns a calendar entry dict for one ticker. Fields are left at their
    new_calendar_entry() defaults (None/False) wherever Yahoo doesn't have
    coverage, rather than failing the whole run over one bad/uncovered ticker."""
    entry = new_calendar_entry(ticker)
    try:
        data = session.quote_summary(
            ticker, ["price", "calendarEvents", "earningsHistory", "earningsTrend"]
        )
        result = _first(data.get("quoteSummary", {}).get("result", []))
        if not result:
            log(f"WARN: no quoteSummary result for {ticker} - leaving earnings fields empty")
            return entry

        price = result.get("price", {})
        entry["name"] = price.get("shortName") or price.get("longName") or ticker

        entry["earningsDate"] = parse_earnings_date(result.get("calendarEvents", {}))

        trend = result.get("earningsTrend", {}).get("trend", [])
        current_q = next((t for t in trend if t.get("period") == "0q"), None)
        if current_q:
            entry["currentQuarterEpsEstimate"] = _fmt_value(
                current_q.get("earningsEstimate", {}).get("avg")
            )
            entry["currentQuarterRevenueEstimate"] = _fmt_value(
                current_q.get("revenueEstimate", {}).get("avg")
            )

        history = result.get("earningsHistory", {}).get("history", [])
        if history:
            last_reported = history[-1]  # Yahoo returns these oldest-first
            entry["previousQuarterLabel"] = _fmt_value(last_reported.get("quarter"))
            entry["previousQuarterEpsActual"] = _fmt_value(last_reported.get("epsActual"))
            entry["previousQuarterEpsEstimate"] = _fmt_value(last_reported.get("epsEstimate"))
    except Exception as exc:  # noqa: BLE001 - one bad ticker shouldn't kill the run
        log(f"WARN: could not fetch earnings data for {ticker}: {exc}")
    return entry


def build_calendar(tickers, session=None):
    session = session or YahooSession()
    companies = [fetch_company_earnings(session, t) for t in tickers]
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "companies": companies,
    }


def carry_over_reminder_flags(new_calendar, previous_calendar):
    """A fresh monthly rebuild shouldn't re-arm a reminder that already went
    out this month for a ticker whose reporting date hasn't changed."""
    if not previous_calendar:
        return
    prev_by_ticker = {c["ticker"]: c for c in previous_calendar.get("companies", [])}
    for c in new_calendar["companies"]:
        prev = prev_by_ticker.get(c["ticker"])
        if prev and prev.get("earningsDate") == c["earningsDate"]:
            c["reminderSent"] = prev.get("reminderSent", False)


def load_calendar():
    try:
        with open(CALENDAR_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_calendar(calendar):
    with open(CALENDAR_PATH, "w", encoding="utf-8") as f:
        json.dump(calendar, f, indent=2)
        f.write("\n")


def _or_na(value):
    return "N/A" if value is None else value


def format_calendar_email(calendar):
    lines = [
        "Monthly earnings calendar for your watched basket",
        f"Generated: {calendar['generatedAt']}",
        "",
    ]
    for c in calendar["companies"]:
        lines.append(f"=== {c['name']} ({c['ticker']}) ===")
        lines.append(f"  Next reporting date: {_or_na(c['earningsDate'])}")
        lines.append(
            "  This quarter estimate: "
            f"EPS {_or_na(c['currentQuarterEpsEstimate'])}, "
            f"Revenue {_or_na(c['currentQuarterRevenueEstimate'])}"
        )
        prev_label = (
            f"Quarter ended {c['previousQuarterLabel']}"
            if c["previousQuarterLabel"]
            else "Previous quarter"
        )
        lines.append(
            f"  {prev_label} EPS: "
            f"{_or_na(c['previousQuarterEpsActual'])} actual vs "
            f"{_or_na(c['previousQuarterEpsEstimate'])} estimate"
        )
        lines.append("")
    return "\n".join(lines)


def format_reminder_email(due_companies):
    lines = ["Earnings reminder: the following companies report tomorrow", ""]
    for c in due_companies:
        lines.append(f"- {c['name']} ({c['ticker']}) reports on {c['earningsDate']}")
    return "\n".join(lines)


def send_email(subject, body):
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    to_addr = os.environ.get("EMAIL_TO")

    if not (username and password and to_addr):
        log(
            "SMTP_USERNAME / SMTP_PASSWORD / EMAIL_TO not all set as repo secrets - "
            "skipping email, calendar is still written to data/earnings_calendar.json"
        )
        return

    smtp_server = os.environ.get("SMTP_SERVER") or "smtp.gmail.com"
    smtp_port = int(os.environ.get("SMTP_PORT") or "587")

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = username
    msg["To"] = to_addr

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=REQUEST_TIMEOUT) as server:
            server.starttls()
            server.login(username, password)
            server.sendmail(username, [to_addr], msg.as_string())
        log(f"Email sent to {to_addr}: {subject}")
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: failed to send email: {exc}")


def check_reminders(calendar, now):
    """Marks + emails about any company whose earnings date is tomorrow.
    Returns True if the calendar was mutated (so the caller knows to persist it)."""
    tomorrow = (now + timedelta(days=1)).date()
    due = []
    changed = False
    for c in calendar["companies"]:
        if c.get("reminderSent") or not c.get("earningsDate"):
            continue
        try:
            earnings_date = date.fromisoformat(c["earningsDate"])
        except ValueError:
            continue
        if earnings_date == tomorrow:
            due.append(c)
            c["reminderSent"] = True
            changed = True

    if due:
        send_email(
            f"[Earnings Agent] {len(due)} compan{'y' if len(due) == 1 else 'ies'} reporting tomorrow",
            format_reminder_email(due),
        )
    else:
        log("No companies reporting tomorrow - no reminder sent")
    return changed


def determine_mode(now):
    mode = (os.environ.get("EARNINGS_MODE") or "auto").lower()
    if mode in ("monthly", "daily"):
        return mode
    return "monthly" if now.day == 1 else "daily"


def main():
    tickers = load_tickers()
    if not tickers:
        log("Basket is empty - nothing to do")
        return 0

    now = datetime.now(timezone.utc)
    mode = determine_mode(now)
    log(f"Running in {mode} mode for {len(tickers)} ticker(s)")

    if mode == "monthly":
        calendar = build_calendar(tickers)
        carry_over_reminder_flags(calendar, load_calendar())
        write_calendar(calendar)
        send_email(
            f"[Earnings Agent] Monthly earnings calendar - {now.strftime('%B %Y')}",
            format_calendar_email(calendar),
        )
        # Worth checking tomorrow's reminders on the monthly run too, in case
        # the 1st happens to land the day before a report.
        if check_reminders(calendar, now):
            write_calendar(calendar)
    else:
        calendar = load_calendar()
        if not calendar:
            log("No earnings_calendar.json yet - run the monthly job first")
            return 0
        if check_reminders(calendar, now):
            write_calendar(calendar)

    return 0


if __name__ == "__main__":
    sys.exit(main())
