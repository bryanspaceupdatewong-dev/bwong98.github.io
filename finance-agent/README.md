# Finance Notification Agent

Screens a basket of stocks/ETFs on a schedule, flags any that moved more
than a threshold vs. about a week ago, pulls likely-related headlines for
flagged tickers, and emails you a compilation report. Runs entirely on
GitHub Actions + this static site — no server to host or pay for.

## How it works

- `config.json` — the watched basket, check interval (`halfhour` / `hourly`
  / `daily`), the daily run hour (UTC, used only when interval is `daily`),
  and the alert threshold (%).
- `.github/workflows/finance-agent.yml` — a scheduled workflow that fires
  every 30 minutes. The script self-gates against `config.json`'s
  `interval` so one cron schedule serves all three modes (see
  `is_due()` in the script).
- `scripts/check_prices.py` — stdlib-only Python. Pulls current price +
  price from ~5 trading days ago per ticker from Yahoo Finance's public
  chart endpoint (no API key), flags tickers whose move exceeds the
  threshold, pulls up to 3 headlines per flagged ticker from Google News
  RSS, writes `data/report.json`, appends flagged runs to `data/history.json`
  (last 50 kept), and emails a compilation report if anything is flagged.
- `dashboard.html` — browser UI: edit the basket/interval/threshold (draft
  saved to your browser), view the live deployed config, the latest report,
  and alert history. Also offers a browser push notification when the tab
  is open.

## One-time setup

1. In this repo's **Settings → Secrets and variables → Actions**, add:
   - `SMTP_USERNAME` — sending email address (a Gmail address works well)
   - `SMTP_PASSWORD` — an **app password** for that account (Google Account
     → Security → 2-Step Verification → App passwords), not your normal
     login password
   - `EMAIL_TO` — where alerts should land
   - Optional: `SMTP_SERVER` / `SMTP_PORT` (default `smtp.gmail.com` / `587`)

   Without these secrets the job still runs and updates `data/report.json`
   / `data/history.json` (visible on the dashboard) — it just skips
   sending email.

2. Open `finance-agent/dashboard.html` (via GitHub Pages, e.g.
   `https://<you>.github.io/finance-agent/dashboard.html`), add your
   tickers, pick an interval and threshold, click **Generate config.json**,
   then paste that JSON into `finance-agent/config.json` in the repo and
   commit.

3. To confirm it's wired up without waiting for the schedule: repo →
   **Actions** tab → **Finance Notification Agent** → **Run workflow**.

## Notes / limitations

- Yahoo Finance's chart endpoint and Google News RSS are free, unofficial,
  no-key endpoints — the standard approach for a personal tool like this,
  but not a paid, rate-limit-guaranteed API. If Yahoo changes the endpoint
  shape, `get_price_data()` in `check_prices.py` is the one place to fix.
- "Drastic move" = current price vs. the close from ~5 trading days back
  exceeding the configured `thresholdPercent` in either direction. Adjust
  the threshold in the dashboard if it's too noisy or too quiet.
- News per flagged ticker is a keyword search ("`TICKER` stock") against
  Google News, not a verified causal link — treat headlines as context, not
  certainty.
- GitHub disables scheduled workflows on a repo after 60 days with no
  activity; push any commit (e.g. a config change) to reset that clock if
  the agent goes quiet.
- This sandbox's dev environment could not reach `query1.finance.yahoo.com`
  / `news.google.com` when the script was written (network is proxied here
  with an allowlist). GitHub-hosted Actions runners have normal outbound
  internet access, so run the workflow once via **Run workflow** after this
  lands to confirm end-to-end.
