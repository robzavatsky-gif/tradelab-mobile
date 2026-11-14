# TradeLab v0.4 - Mobile dashboard, automatic after-close processing, manual ChatGPT sharing

**Research only. No real orders. No Alpaca paper orders. No funds moved.**

This keeps all three v0.3 engine files and `frozen_rules.json` **byte-for-byte unchanged**. The app remains a historical/replay simulator, not a real-time trading bot.

The first deployment path is GitHub Actions + GitHub Pages. A cloud runner fetches Alpaca IEX historical SPY 5-minute bars, applies the frozen v0.3 rules, and publishes a responsive mobile dashboard after close. An optional `Send to ChatGPT` button opens Android's sharing chooser with a daily Markdown report (or downloads it for manual attachment). **No OpenAI API key, no paid AI billing, and no automatic message to ChatGPT.**

## What's in the package

- `web/` - mobile PWA app, installable from your browser once hosted over HTTPS.
- `cloud_update.py` - runs the existing frozen v0.3 forward evaluator and exports static web data, a daily Markdown packet, and a complete ZIP.
- `.github/workflows/after_close.yml` - cloud automation at 20:35 and 21:35 UTC Monday-Friday, with a 4:30 PM New York gate to handle daylight saving time.
- `reports/forward_archive.json` - generated on your first eligible run; must persist between runs. The workflow commits it to the repository.
- `Run_Local_Preview.bat` - offline synthetic preview on Windows; launches a local web server. No account needed.
- `Run_Forward_Check_Local.bat` - optional real IEX fetch from Windows and local mobile-style dashboard. Requires your *paper* API keys.
- `research_reference.json` - the seven already-known v0.3 historical results, marked exploratory, from the previously shared 126-day backtest.

## Preview on your computer (free and offline)

1. Extract the ZIP, open the `TradeLab_v0.4` folder.
2. Double-click `Run_Local_Preview.bat` on a Windows machine with Python 3.10+.
3. Your browser opens `http://localhost:8765/web/`. This preview uses **synthetic data**, which the app clearly labels.
4. Close the command window to stop the local preview server. When you publish to the cloud, you **do not** need a PC left on.

You can also run `python cloud_update.py --seed` to reset the packaged app to an empty forward state (no fake profits/trades).

## Deploy the phone app with automatic cloud updates

**Important privacy warning:** GitHub Pages sites are generally public; GitHub Free Pages requires a public repository. The dashboard, prospective simulated results, archived market bars, and repository code will be accessible to others. Keep **only paper-trading** credentials in GitHub Actions encrypted Secrets. Do not put your identity, passwords, sensitive personal finances, or live account keys into the files. **If you require a private dashboard, do not publish with this starter workflow; choose an authenticated hosting service first.**

The following is a manual one-time deployment. Nothing is hosted or scheduled merely by downloading this ZIP.

1. Create a GitHub account if you don't have one; on [github.com](https://github.com), create a **public repository** named `tradelab-mobile` (only if you're comfortable with public synthetic research data). Initialize it empty.
2. Upload **the CONTENTS of the `TradeLab_v0.4` folder to the root of that repository**, keeping `.github/workflows/after_close.yml` and the rest of the paths intact. GitHub Desktop or `git` works well. Avoid uploading `settings.env` or API keys.
3. Open the repository > **Settings > Secrets and variables > Actions > New repository secret**. Add **`APCA_API_KEY_ID`** and **`APCA_API_SECRET_KEY`** with your *Alpaca PAPER* credentials. Keep the keys out of source code and chats.
4. In the repository > **Settings > Pages**, set **Build and deployment > Source: GitHub Actions**.
5. In repository > **Actions**, enable workflows if prompted; select **TradeLab after-close (simulated only)**, click **Run workflow** once to publish the initial dashboard. If before first eligible market close, it stays in the waiting state.
6. After successful deployment, go to `https://YOUR_GITHUB_USERNAME.github.io/tradelab-mobile/` (substitute your account name). On Android Chrome, use browser menu > **Add to Home screen** (or **Install app**, when offered).
7. The workflow attempts to run after each weekday close. From Monday Nov 16, 2026, eligible *complete* SPY IEX sessions can be added. Check the **Actions** page if a day doesn't appear: GitHub cron can be delayed/dropped; public-repo scheduled workflows may be disabled after 60 days of inactivity.

If your repository's Actions token cannot push archive updates, check **Settings > Actions > General > Workflow permissions**, and grant the workflow the required content write permission. The workflow sets `permissions.contents=write` but organizational policy may override it. If the archive cannot be saved, the run fails rather than pretending persistence worked.

**Cloud-running costs:** normally $0 at this scale if within GitHub Free usage and limits. It's not a guarantee that hosting services will remain free. Alpaca Basic IEX market data is free. No OpenAI API is called.

## Daily use on Android

- Open the saved TradeLab icon after market close; it shows the updated prospective results once the scheduled job has completed.
- Press **Send to ChatGPT**. If Android Chrome offers file sharing, select ChatGPT and attach the daily Markdown report. If unavailable, the app downloads `TradeLab_daily_review.md`; upload it to this ChatGPT conversation manually.
- Press **Full data ZIP** when deeper analysis requires the full prospective five-minute history, report, trade ledger, diagnostics, and cumulative results. Upload that ZIP in ChatGPT.
- **Nothing is automatically transmitted to ChatGPT.** The sharing process involves an explicit user action. An actual automatic analysis workflow would be Option B using an OpenAI API key; it is deliberately not included.

## Data integrity and caveats

- Original strategy files' hashes are enforced by `frozen_rules.json`; modifying them prevents prospective runs.
- Existing archived sessions are not silently re-written if Alpaca reissues differing price bars. Investigate reported revision errors before changing state.
- Script checks for 78 consecutive five-minute bars; normal full sessions only. Early-close sessions and incomplete IEX days may be excluded.
- IEX is one exchange, not consolidated US equities. Historical replay fills are simplified estimates and not actual market fills.
- Each strategy runs a separate $500 virtual account; **don't combine strategy balances**. No broker orders are placed.
- The automated workflow is intended to run at **4:35 PM Eastern** (adjusted for DST), not to fire exactly at market close. GitHub-scheduled workflows may start late or occasionally not run; the PWA shows a last-update date rather than inventing freshness.
- Source history before Nov 16 is already known research, not a truly blind validation period.
- Data is published as a public dashboard, and `forward_archive.json` is stored in the source repository. Public static pages do not provide login protection.

## Developer commands

```sh
python cloud_update.py --seed              # neutral initial dashboard, no keys
python cloud_update.py --demo              # four synthetic prospective sessions, no keys
python cloud_update.py                    # actual IEX after-close catch-up, keys required
python cloud_update.py --scheduled        # skip before 16:30 New York time
python -m unittest discover -s tests -v
python -m http.server 8765               # open http://localhost:8765/web/
```

Reference documentation:
- https://docs.alpaca.markets/us/reference/stockbarsingle-1
- https://docs.alpaca.markets/us/docs/about-market-data-api
- https://docs.github.com/en/pages/getting-started-with-github-pages/creating-a-github-pages-site
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
