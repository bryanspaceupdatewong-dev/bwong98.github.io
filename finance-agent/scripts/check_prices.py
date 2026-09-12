#!/usr/bin/env python3
"""
Financial notification agent.

Reads finance-agent/config.json for the watched ticker basket, interval and
alert threshold, pulls current + one-week-ago prices from Yahoo Finance's
public chart endpoint (no API key required), flags tickers whose price has
moved more than the configured threshold since a week ago, pulls a few
recent headlines per flagged ticker from Google News RSS, writes the results
to finance-agent/data/report.json (+ appends to data/history.json), and
emails a compilation report when anything is flagged. Skips entirely on
weekends, since none of the watched markets are open then. Sends at most
one alert email per ticker per calendar day, however many times it stays
flagged that day.

Uses only the Python standard library so the GitHub Actions workflow needs
no dependency installation step.
"""

import json
import os
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "config.json")
REPORT_PATH = os.path.join(REPO_ROOT, "data", "report.json")
HISTORY_PATH = os.path.join(REPO_ROOT, "data", "history.json")
LAST_ALERTED_PATH = os.path.join(REPO_ROOT, "data", "last_alerted_news.json")

MAX_HISTORY_ENTRIES = 50
REQUEST_TIMEOUT = 10
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def log(msg):
    print(f"[finance-agent] {msg}", flush=True)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    config.setdefault("tickers", [])
    config.setdefault("interval", "daily")
    config.setdefault("dailyRunHourUtc", 14)
    config.setdefault("thresholdPercent", 5)
    return config


def is_due(config, now):
    """Self-gate: the workflow fires every 30 minutes; decide whether this
    particular firing should actually run a check, based on the configured
    interval."""
    if now.weekday() >= 5:  # Saturday/Sunday (UTC) - markets are closed, nothing to check
        return False
    interval = config.get("interval", "daily")
    if interval == "halfhour":
        return True
    if interval == "hourly":
        return now.minute < 30
    if interval == "daily":
        return now.hour == int(config.get("dailyRunHourUtc", 14)) and now.minute < 30
    return True


def fetch_url(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return resp.read()


def fetch_json(url):
    return json.loads(fetch_url(url))


def get_price_data(ticker):
    """Returns (current_price, week_ago_price) or (None, None) on failure."""
    quote_url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(ticker)}?range=5d&interval=15m"
    )
    daily_url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{urllib.parse.quote(ticker)}?range=1mo&interval=1d"
    )

    current_price = None
    try:
        data = fetch_json(quote_url)
        result = data["chart"]["result"][0]
        meta = result.get("meta", {})
        current_price = meta.get("regularMarketPrice")
        if current_price is None:
            closes = result["indicators"]["quote"][0]["close"]
            for c in reversed(closes):
                if c is not None:
                    current_price = c
                    break
    except Exception as exc:  # noqa: BLE001 - one bad ticker shouldn't kill the run
        log(f"WARN: could not fetch current price for {ticker}: {exc}")
        return None, None

    week_ago_price = None
    try:
        data = fetch_json(daily_url)
        result = data["chart"]["result"][0]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
        if len(closes) >= 6:
            week_ago_price = closes[-6]
        elif len(closes) >= 2:
            week_ago_price = closes[0]
    except Exception as exc:  # noqa: BLE001
        log(f"WARN: could not fetch weekly history for {ticker}: {exc}")

    return current_price, week_ago_price


def get_news(ticker, now, limit=3):
    """Latest news for a ticker: only items published within the past week,
    newest first, capped at `limit`. An item whose publish date can't be
    parsed is skipped rather than assumed recent."""
    url = (
        "https://news.google.com/rss/search?q="
        f"{urllib.parse.quote(ticker + ' stock')}&hl=en-US&gl=US&ceid=US:en"
    )
    cutoff = now - timedelta(days=7)
    try:
        raw = fetch_url(url)
        root = ET.fromstring(raw)
        dated_items = []
        for item in root.findall("./channel/item"):
            pub_date_raw = item.findtext("pubDate") or ""
            try:
                pub_date = parsedate_to_datetime(pub_date_raw)
            except (TypeError, ValueError):
                continue
            if pub_date is None:
                continue
            if pub_date.tzinfo is None:
                pub_date = pub_date.replace(tzinfo=timezone.utc)
            if pub_date < cutoff:
                continue
            title = item.findtext("title") or ""
            link = item.findtext("link") or ""
            dated_items.append((pub_date, {"title": title, "link": link}))
        dated_items.sort(key=lambda pair: pair[0], reverse=True)
        return [entry for _, entry in dated_items[:limit]]
    except Exception as exc:  # noqa: BLE001
        log(f"WARN: could not fetch news for {ticker}: {exc}")
        return []


def build_report(config, now):
    threshold = float(config.get("thresholdPercent", 5))
    results = []
    for ticker in config.get("tickers", []):
        current_price, week_ago_price = get_price_data(ticker)
        entry = {
            "ticker": ticker,
            "currentPrice": current_price,
            "weekAgoPrice": week_ago_price,
            "percentChange": None,
            "flagged": False,
            "news": [],
        }
        if current_price is not None and week_ago_price:
            pct = (current_price - week_ago_price) / week_ago_price * 100
            entry["percentChange"] = round(pct, 2)
            entry["flagged"] = abs(pct) >= threshold
        results.append(entry)

    for entry in results:
        if entry["flagged"]:
            entry["news"] = get_news(entry["ticker"], now)

    flagged_count = sum(1 for r in results if r["flagged"])
    return {
        "generatedAt": now.isoformat().replace("+00:00", "Z"),
        "interval": config.get("interval"),
        "thresholdPercent": threshold,
        "results": results,
        "flaggedCount": flagged_count,
    }


def write_report(report):
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.write("\n")


def update_history(report):
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = []

    if report["flaggedCount"] > 0:
        history.append(
            {
                "generatedAt": report["generatedAt"],
                "thresholdPercent": report["thresholdPercent"],
                "flagged": [r for r in report["results"] if r["flagged"]],
            }
        )
        history = history[-MAX_HISTORY_ENTRIES:]

    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
        f.write("\n")


def load_last_alerted_news():
    try:
        with open(LAST_ALERTED_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_last_alerted_news(last_alerted):
    with open(LAST_ALERTED_PATH, "w", encoding="utf-8") as f:
        json.dump(last_alerted, f, indent=2)
        f.write("\n")


def news_signature(news_items):
    """Order-independent fingerprint of a ticker's news items, used to detect
    that a follow-up alert would be reporting the exact same news as last
    time. Empty when there's no news, since there's nothing to compare."""
    return sorted(n["title"] for n in news_items if n.get("title"))


def select_candidates(flagged, last_alerted, today_str):
    """Drops flagged tickers we've already emailed about today (at most one
    alert per ticker per calendar day - a ticker that stays flagged all day
    only needs to be reported once), and tickers whose news is identical to
    what we already emailed last time. Returns (ticker_result, news_sig)
    pairs still eligible to send; the caller decides which of those actually
    go out (e.g. only ones with fresh news) and persists state only for
    those, so a candidate skipped here for lack of news remains eligible
    later the same day."""
    candidates = []
    for r in flagged:
        entry = last_alerted.get(r["ticker"], {})
        if entry.get("date") == today_str:
            log(f"Skipping {r['ticker']}: already emailed about this ticker today")
            continue
        sig = news_signature(r["news"])
        if sig and entry.get("news") == sig:
            log(f"Skipping {r['ticker']}: already emailed this exact news - no follow-up")
            continue
        candidates.append((r, sig))
    return candidates


def format_email_body(alerts, report):
    lines = [
        "Compilation report: drastic movers in your watched basket",
        f"Generated: {report['generatedAt']}",
        f"Threshold: +/-{report['thresholdPercent']}% vs ~1 week ago",
        "",
    ]
    for r in alerts:
        if not r["news"]:
            continue
        direction = "UP" if r["percentChange"] >= 0 else "DOWN"
        lines.append(f"=== {r['ticker']}: {direction} {r['percentChange']}% ===")
        lines.append(f"  Current price: {r['currentPrice']}")
        lines.append(f"  ~1 week ago:   {r['weekAgoPrice']}")
        lines.append("  Likely related news (past week):")
        for n in r["news"]:
            lines.append(f"    - {n['title']}")
            lines.append(f"      {n['link']}")
        lines.append("")
    return "\n".join(lines)


def send_email(alerts, report):
    username = os.environ.get("SMTP_USERNAME")
    password = os.environ.get("SMTP_PASSWORD")
    to_addr = os.environ.get("EMAIL_TO")

    if not (username and password and to_addr):
        log(
            "SMTP_USERNAME / SMTP_PASSWORD / EMAIL_TO not all set as repo secrets - "
            "skipping email, alerts are still written to data/report.json and data/history.json"
        )
        return

    smtp_server = os.environ.get("SMTP_SERVER") or "smtp.gmail.com"
    smtp_port = int(os.environ.get("SMTP_PORT") or "587")

    subject = f"[Finance Agent] {len(alerts)} stock(s) moved sharply: " + ", ".join(
        r["ticker"] for r in alerts
    )
    body = format_email_body(alerts, report)

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = username
    msg["To"] = to_addr

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=REQUEST_TIMEOUT) as server:
            server.starttls()
            server.login(username, password)
            server.sendmail(username, [to_addr], msg.as_string())
        log(f"Alert email sent to {to_addr}")
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: failed to send alert email: {exc}")


def main():
    now = datetime.now(timezone.utc)
    config = load_config()

    if not is_due(config, now):
        log(
            f"Not due yet for interval={config.get('interval')} at {now.isoformat()} - skipping run"
        )
        return 0

    if not config.get("tickers"):
        log("Basket is empty - writing an empty report and exiting")
        report = {
            "generatedAt": now.isoformat().replace("+00:00", "Z"),
            "interval": config.get("interval"),
            "thresholdPercent": config.get("thresholdPercent"),
            "results": [],
            "flaggedCount": 0,
        }
        write_report(report)
        return 0

    log(f"Checking {len(config['tickers'])} ticker(s): {', '.join(config['tickers'])}")
    report = build_report(config, now)
    write_report(report)
    update_history(report)

    log(f"{report['flaggedCount']} ticker(s) flagged as drastic movers")
    if report["flaggedCount"] > 0:
        flagged = [r for r in report["results"] if r["flagged"]]
        last_alerted = load_last_alerted_news()
        today_str = now.date().isoformat()
        candidates = select_candidates(flagged, last_alerted, today_str)
        # Only mail candidates that actually have fresh (past-week) news to
        # show; if none of them do, there's nothing to email at all. Only
        # these get stamped as "alerted today", so a ticker skipped here for
        # lack of news is still free to alert later the same day.
        with_news = [(r, sig) for r, sig in candidates if r["news"]]
        if with_news:
            send_email([r for r, _ in with_news], report)
            for r, sig in with_news:
                last_alerted[r["ticker"]] = {"date": today_str, "news": sig}
            save_last_alerted_news(last_alerted)
        else:
            log("No fresh, recent news for any flagged ticker - no email sent")

    return 0


if __name__ == "__main__":
    sys.exit(main())
