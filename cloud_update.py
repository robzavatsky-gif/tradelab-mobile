#!/usr/bin/env python3
"""TradeLab v0.4 static dashboard publisher. Analysis only; no order endpoints.

Reuses byte-for-byte v0.3 frozen strategy engine. Live cloud execution requires
PAPER Alpaca API credentials supplied through environment variables.
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import tempfile
import zipfile
from zoneinfo import ZoneInfo

import tradelab_v03 as engine
import v01_engine as market

ROOT = Path(__file__).resolve().parent
WEB = ROOT / 'web'
PUBLIC = WEB / 'data'
REPORTS = ROOT / 'reports'
TZ = ZoneInfo('America/New_York')
UTC = dt.timezone.utc
REFERENCE = ROOT / 'research_reference.json'


def run_gate(now=None):
    """Scheduled workflow runs twice UTC to cover US daylight-saving changes."""
    now = (now or dt.datetime.now(UTC)).astimezone(TZ)
    return now.weekday() < 5 and now.time() >= dt.time(16, 30)


def records_to_csv(rows):
    if not rows:
        return ''
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=['timestamp','open','high','low','close','volume'], lineterminator='\n')
    w.writeheader()
    for row in rows:
        w.writerow({k: row[k] for k in w.fieldnames})
    return buf.getvalue()


def build_packet(result, state, demo=False):
    sessions = state['sessions']
    last_date = max(sessions) if sessions else ''
    lines = [
        '# TradeLab daily review package',
        '',
        f'- Generated UTC: {dt.datetime.now(UTC).isoformat(timespec="seconds")}',
        f'- Data status: {"SYNTHETIC DEMONSTRATION" if demo else "ALPACA HISTORICAL IEX"}',
        '- Real orders: NONE. Alpaca paper orders: NONE.',
        '- Trading strategy files: frozen v0.3; no parameter tuning in v0.4.',
        f'- Forward experiment began: {engine.START_DATE}',
        f'- Prospective complete sessions: {result["days"]}',
        f'- Newly archived sessions this run: {result.get("new_sessions", 0)}',
        f'- Most recent session: {last_date or "none yet"}',
        f'- Frozen rules SHA256: {result["freeze_sha256"]}',
        '',
        '## Cumulative prospective results (independent simulated $500 accounts)',
        '',
        '| Strategy | Net P/L ($) | Trades | Win rate (%) | Profit factor |',
        '|---|---:|---:|---:|---:|',
    ]
    for name in engine.STRATEGIES:
        s = result['periods']['prospective'][name]['stats']
        lines.append(f'| {name} | {s["pnl"]:+.2f} | {s["trade_count"]} | {s["win_rate_pct"]:.1f} | {s["profit_factor"] if s["profit_factor"] is not None else "n/a"} |')
    lines += ['', '## Latest session diagnostic decisions', '']
    if last_date:
        for name in engine.STRATEGIES:
            strat = result['periods']['prospective'][name]
            diag = next((x for x in strat['diagnostics'] if x['date'] == last_date), None)
            trade = next((x for x in strat['trades'] if x['date'] == last_date), None)
            if diag:
                detail = f'{name}: {diag["decision"]}; regime={diag["regime"]}; blocked_regime={diag["blocked_regime"]}; blocked_vwap={diag["blocked_below_vwap"]}; blocked_volume={diag["blocked_volume"]}'
                if trade:
                    detail += f'; entry={trade["entry_time_et"]}; exit={trade["exit_time_et"]}; reason={trade["exit_reason"]}; pnl=${trade["pnl"]:+.2f}'
                lines.append('- ' + detail)
        lines += ['', f'## Complete 5-minute OHLCV for {last_date}', '', '```csv',
                  records_to_csv(sessions[last_date]['bars']).rstrip(), '```']
    else:
        lines.append('No eligible forward trading sessions have yet been archived.')
    lines += ['', '## Analysis request', '',
              'Review data quality, new trades, cumulative and per-regime outcomes, cost sensitivity, and whether no-trade remains preferable. Do not optimize rules using prospective results. Note that IEX-only prices, simulated execution and a small trade sample cannot prove a profitable strategy.',
              '', 'Attach the complete research bundle if you need all sessions and per-trade diagnostics.', '']
    return '\n'.join(lines)


def make_payload(result, state, demo=False):
    days = state['sessions']
    last_date = max(days) if days else None
    strategies = []
    for name in engine.STRATEGIES:
        d = result['periods']['prospective'][name]
        s = d['stats']
        diag = next((x for x in d['diagnostics'] if x['date'] == last_date), None)
        trade = next((x for x in d['trades'] if x['date'] == last_date), None)
        strategies.append({
            'name': name,
            'pnl': round(s['pnl'], 4), 'ending_balance': round(s['ending_balance'], 4),
            'trades': s['trade_count'], 'win_rate_pct': round(s['win_rate_pct'], 2),
            'max_eod_drawdown_pct': round(s['max_eod_drawdown_pct'], 3),
            'latest_decision': diag['decision'] if diag else 'not_evaluated',
            'latest_trade': {k: trade.get(k) for k in ('entry_time_et','exit_time_et','exit_reason','pnl','regime')} if trade else None,
            'curve': [{'date': date, 'equity': round(amount, 4)} for date, amount in d['curve']],
        })
    ref = json.loads(REFERENCE.read_text(encoding='utf8')) if REFERENCE.exists() else []
    return {
        'schema': 1, 'app_version': '0.4', 'mode': 'synthetic_demo' if demo else 'prospective',
        'updated_utc': dt.datetime.now(UTC).isoformat(timespec='seconds'),
        'source': 'SYNTHETIC TEST DATA' if demo else 'Alpaca historical IEX SPY (delayed)',
        'ticker': 'SPY', 'simulated_capital': 500, 'sessions': len(days),
        'last_session': last_date, 'new_sessions': result.get('new_sessions', 0),
        'frozen_rules_sha256': result['freeze_sha256'], 'strategies': strategies,
        'opening_regimes': result['regimes'], 'historical_reference': ref,
        'latest_bar_count': len(days[last_date]['bars']) if last_date else 0,
    }


def publish(result, archive_path, demo=False):
    state = json.loads(Path(archive_path).read_text(encoding='utf8')) if Path(archive_path).exists() else {'sessions': {}}
    PUBLIC.mkdir(parents=True, exist_ok=True)
    (PUBLIC/'latest.json').write_text(json.dumps(make_payload(result, state, demo), indent=2), encoding='utf8')
    packet = build_packet(result, state, demo)
    (PUBLIC/'chatgpt_daily_packet.md').write_text(packet, encoding='utf8')
    included = [REPORTS / f'SPY_{"demo_forward" if demo else "forward"}_{s}' for s in (
        'report.html','comparison.csv','diagnostics.csv','by_regime.csv','trades.csv','summary.json')]
    with zipfile.ZipFile(PUBLIC/'complete_research_bundle.zip', 'w', zipfile.ZIP_DEFLATED, compresslevel=7) as z:
        for path in included:
            if path.exists():
                z.write(path, path.name)
        z.writestr('chatgpt_daily_packet.md', packet)
        z.writestr('archived_forward_5minute_bars.json', json.dumps(state, indent=2))
    for path in included:
        if path.suffix == '.html' and path.exists():
            shutil.copyfile(path, WEB/'full_report.html')
    print(f'Published dashboard: {WEB/"index.html"} / {len(state["sessions"])} forward session(s)')
    print('Daily ChatGPT packet:', PUBLIC/'chatgpt_daily_packet.md')


def shifted_demo_bars():
    """A few fake sessions for interface tests; cannot be confused with real prices."""
    bars = market.generate_synthetic(20)
    dates = sorted({b['et'].date() for b in bars})[:4]
    starts = [dt.date(2026,11,16) + dt.timedelta(days=i) for i in range(4)]
    rows = []
    for old_date, new_date in zip(dates, starts):
        for b in bars:
            if b['et'].date() != old_date:
                continue
            et = dt.datetime.combine(new_date, b['et'].timetz().replace(tzinfo=None), tzinfo=TZ)
            rows.append({'t': et.astimezone(UTC).isoformat(),
                         'o':b['open'],'h':b['high'],'l':b['low'],'c':b['close'],'v':b['volume']})
    return market.normalize_bars(rows)


def main():
    parser = argparse.ArgumentParser(description='TradeLab 0.4 after-close report publisher (NO ORDERS)')
    parser.add_argument('--demo', action='store_true', help='Synthetic data: local app preview ONLY')
    parser.add_argument('--seed', action='store_true', help='Prepare neutral waiting-for-data initial dashboard')
    parser.add_argument('--scheduled', action='store_true', help='Skip runs before 4:30 pm New York on weekdays')
    parser.add_argument('--days', type=int, default=240)
    args = parser.parse_args()
    engine.manifest()  # Never bypass frozen rule fingerprints.
    if args.demo and args.seed:
        parser.error('--demo and --seed are mutually exclusive')
    if args.scheduled and not run_gate():
        print('Skipping: not yet 4:30 PM Eastern on a weekday.')
        return 0
    if args.seed:
        with tempfile.TemporaryDirectory(prefix='tradelab-seed-') as t:
            archive = Path(t)/'seed_archive.json'
            result = engine.forward([], 'WAITING FOR PROSPECTIVE MARKET SESSIONS', archive_path=archive)
            engine.export(result, 'SPY_forward', open_browser=False)
            publish(result, archive, demo=False)
    elif args.demo:
        with tempfile.TemporaryDirectory(prefix='tradelab-demo-') as t:
            archive = Path(t)/'demo_archive.json'
            result = engine.forward(shifted_demo_bars(), 'SYNTHETIC DATA - NOT MARKET PRICES', archive_path=archive)
            engine.export(result, 'SPY_demo_forward', open_browser=False)
            publish(result, archive, demo=True)
    else:
        if not (1 <= args.days <= 730):
            parser.error('--days must be 1 through 730')
        # Fetch uses historical market-data GET only, no order APIs.
        bars = market.get_alpaca_bars('SPY', args.days, 'iex')
        if not bars:
            raise RuntimeError('No bars returned by Alpaca.')
        result = engine.forward(bars, 'ALPACA HISTORICAL IEX SPY', archive_path=REPORTS/'forward_archive.json')
        engine.export(result, 'SPY_forward', open_browser=False)
        publish(result, REPORTS/'forward_archive.json')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
