#!/usr/bin/env python3
"""TradeLab v0.1 - offline demo and historical intraday PAPER backtesting.

Uses only the Python standard library. Absolutely no broker order submission.
The bundled demonstration data is synthetic, never real market evidence.
"""
import argparse
import csv
import datetime as dt
import html
import json
import math
import os
from pathlib import Path
import random
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
REPORTS = ROOT / 'reports'
UTC = dt.timezone.utc
STARTING_CASH = 500.0
MAX_NOTIONAL = 250.0
RISK_FRACTION = 0.50
SLIP_BPS_PER_SIDE = 3.0
STOP_FRACTION = 0.005   # 0.50% of entry price
TARGET_FRACTION = 0.010  # 1.00% of entry price
DAILY_LOSS_LIMIT = 5.0


def us_eastern_from_utc(t):
    """US equity venue time conversion without third-party tzdata on Windows."""
    if t.tzinfo is None:
        raise ValueError('Timestamp must include UTC offset')
    t = t.astimezone(UTC)
    y = t.year
    march = dt.date(y, 3, 1)
    november = dt.date(y, 11, 1)
    # Second Sunday in March, 02:00 EST = 07:00 UTC.
    first_sun_march = 1 + (6 - march.weekday()) % 7
    start = dt.datetime(y, 3, first_sun_march + 7, 7, tzinfo=UTC)
    # First Sunday in November, 02:00 EDT = 06:00 UTC.
    first_sun_nov = 1 + (6 - november.weekday()) % 7
    end = dt.datetime(y, 11, first_sun_nov, 6, tzinfo=UTC)
    offset = -4 if start <= t < end else -5
    return t.astimezone(dt.timezone(dt.timedelta(hours=offset)))


def read_settings():
    config = {}
    p = ROOT / 'settings.env'
    if p.exists():
        for line in p.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            config[key.strip()] = value.strip().strip('"').strip("'")
    for k in ('APCA_API_KEY_ID', 'APCA_API_SECRET_KEY'):
        if os.environ.get(k):
            config[k] = os.environ[k]
    return config


def get_alpaca_bars(symbol, days, feed='iex'):
    creds = read_settings()
    key = creds.get('APCA_API_KEY_ID', '')
    secret = creds.get('APCA_API_SECRET_KEY', '')
    if not (key and secret and not key.startswith('PASTE') and not secret.startswith('PASTE')):
        raise RuntimeError('Alpaca PAPER API keys are missing. Copy settings.env.example to settings.env and fill in both keys. See README.md.')
    # Use a 20-minute delay on end-of-window data to avoid free-tier recent-SIP restrictions.
    # Explicit feed=iex ensures this always uses IEX only, not consolidated SIP.
    end = dt.datetime.now(UTC) - dt.timedelta(minutes=20)
    start = end - dt.timedelta(days=days)
    params = {
        'timeframe': '5Min',
        'start': start.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'end': end.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'limit': '10000',
        'feed': feed,
        'adjustment': 'raw',
    }
    bars = []
    last_token = None
    for page in range(100):
        if last_token:
            params['page_token'] = last_token
        url = 'https://data.alpaca.markets/v2/stocks/' + urllib.parse.quote(symbol, safe='') + '/bars?' + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            'APCA-API-KEY-ID': key,
            'APCA-API-SECRET-KEY': secret,
            'Accept': 'application/json',
            'User-Agent': 'TradeLab-v0.1-historical-backtest',
        })
        try:
            with urllib.request.urlopen(req, timeout=35) as response:
                data = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode('utf-8', 'replace')
            raise RuntimeError('Alpaca data HTTP ' + str(exc.code) + ': ' + detail) from None
        except urllib.error.URLError as exc:
            raise RuntimeError('Could not reach Alpaca. Check network or firewall: ' + str(exc.reason)) from None
        bars.extend(data.get('bars') or [])
        token = data.get('next_page_token')
        if not token:
            break
        if token == last_token:
            raise RuntimeError('Alpaca returned a repeated pagination token; stopping.')
        last_token = token
    else:
        raise RuntimeError('Too many pages returned; narrow the date range.')
    return normalize_bars(bars)


def normalize_bars(rows):
    out = []
    seen = set()
    for row in rows:
        ts = row.get('timestamp', row.get('t', ''))
        if not ts:
            continue
        t = dt.datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
        if t.tzinfo is None:
            raise ValueError('CSV timestamps must have UTC offsets or end in Z')
        t = t.astimezone(UTC)
        if t in seen:
            continue
        seen.add(t)
        values = {k: float(row.get(k, row.get(short))) for k, short in [('open', 'o'), ('high', 'h'), ('low', 'l'), ('close', 'c')]}
        if not all(math.isfinite(v) and v > 0 for v in values.values()):
            raise ValueError('Invalid nonpositive or infinite OHLC at ' + str(t))
        if values['low'] > min(values['open'], values['close']) or values['high'] < max(values['open'], values['close']) or values['high'] < values['low']:
            raise ValueError('Inconsistent OHLC at ' + str(t))
        et = us_eastern_from_utc(t)
        # Regular session only; timestamp refers to the start of the 5-min bar.
        if et.weekday() >= 5 or not (dt.time(9, 30) <= et.time() < dt.time(16, 0)):
            continue
        out.append({'timestamp': t.isoformat(), 't': t, 'et': et, **values,
                    'volume': float(row.get('volume', row.get('v', 0)) or 0)})
    return sorted(out, key=lambda b: b['t'])


def load_csv(path):
    with open(path, 'r', newline='', encoding='utf-8-sig') as f:
        return normalize_bars(list(csv.DictReader(f)))


def write_csv(path, bars):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as f:
        wr = csv.DictWriter(f, fieldnames=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        wr.writeheader()
        for b in bars:
            wr.writerow({k: b[k] for k in wr.fieldnames})


def generate_synthetic(days=30, seed=20261114):
    rng = random.Random(seed)
    bars = []
    start = dt.date(2026, 9, 21)
    price = 575.0
    included = 0
    d = start
    while included < days:
        if d.weekday() >= 5:
            d += dt.timedelta(days=1)
            continue
        drift = rng.choice([-0.14, -0.08, 0.06, 0.13, 0.20])
        for n in range(78):
            hour, minute = divmod(9 * 60 + 30 + n * 5, 60)
            # Derive Eastern offset for the date, including winter standard time.
            et_offset = us_eastern_from_utc(dt.datetime(d.year, d.month, d.day, 15, tzinfo=UTC)).utcoffset()
            local = dt.datetime(d.year, d.month, d.day, hour, minute, tzinfo=dt.timezone(et_offset))
            # Opening range and breakout behavior are intentionally synthetic.
            volatility = 0.13 if n < 6 else 0.12
            momentum = (drift if 6 <= n < 42 else -drift * 0.20) + rng.gauss(0, volatility)
            opening = price
            close = max(1.0, opening + momentum)
            high = max(opening, close) + abs(rng.gauss(0.04, 0.035))
            low = min(opening, close) - abs(rng.gauss(0.04, 0.035))
            bars.append({'t': local.astimezone(UTC).isoformat(), 'o': opening, 'h': high,
                         'l': low, 'c': close, 'v': rng.randint(100, 25000)})
            price = close
        included += 1
        d += dt.timedelta(days=1)
    return normalize_bars(bars)


def calculate_drawdown(equity_curve):
    peak = -float('inf')
    biggest = 0
    for _, equity in equity_curve:
        peak = max(peak, equity)
        if peak > 0:
            biggest = max(biggest, (peak - equity) / peak)
    return biggest * 100


def backtest(bars, symbol='SPY'):
    """Long-only 30-minute opening-range breakout, next-bar-open entries.

    Conservative ambiguities: stop wins if stop and target both hit one bar;
    close position at final observed bar if day is incomplete.
    """
    if not bars:
        raise ValueError('No valid 5-minute regular-session bars found.')
    groups = {}
    for b in bars:
        groups.setdefault(b['et'].date(), []).append(b)
    cash = STARTING_CASH
    trades = []
    curve = [('Start', cash)]
    slip = SLIP_BPS_PER_SIDE / 10000.0
    skipped = 0
    for date, daily in sorted(groups.items()):
        daily.sort(key=lambda b: b['t'])
        morning = [b for b in daily if dt.time(9, 30) <= b['et'].time() < dt.time(10, 0)]
        if len(morning) < 4:
            skipped += 1
            curve.append((date.isoformat(), cash))
            continue
        range_high = max(b['high'] for b in morning)
        position = None
        scheduled_buy = False
        traded_today = False
        day_pnl = 0.0
        prev_close = None
        for i, b in enumerate(daily):
            bt = b['et'].time()
            if position is not None and bt >= dt.time(15, 50):
                # Exit at 15:50 bar open. This requires the 15:50 bar to exist.
                px = b['open'] * (1 - slip)
                pnl = (px - position['entry_price']) * position['qty']
                cash += pnl
                day_pnl += pnl
                trades.append({'date': date.isoformat(), 'symbol': symbol, **position,
                               'exit_time': b['et'].isoformat(), 'exit_price': px,
                               'pnl': pnl, 'reason': 'end_of_day'})
                position = None
            if scheduled_buy:
                scheduled_buy = False
                if position is None and not traded_today and day_pnl > -DAILY_LOSS_LIMIT and dt.time(10, 0) <= bt < dt.time(15, 0):
                    entry = b['open'] * (1 + slip)
                    equity = max(0.0, cash)
                    notional = min(MAX_NOTIONAL, equity * RISK_FRACTION)
                    if notional >= 1.0:
                        qty = notional / entry  # fractional units, simulated
                        position = {'entry_time': b['et'].isoformat(), 'entry_price': entry,
                                    'qty': qty, 'stop': entry * (1 - STOP_FRACTION),
                                    'target': entry * (1 + TARGET_FRACTION)}
                        traded_today = True
            if position is not None and bt < dt.time(15, 50):
                # Bar-open gaps past a stop receive the worse opening price.
                reason = None
                if b['open'] <= position['stop']:
                    px = b['open'] * (1 - slip)
                    reason = 'stop_gap'
                elif b['low'] <= position['stop']:
                    px = position['stop'] * (1 - slip)
                    reason = 'stop'
                elif b['high'] >= position['target']:
                    px = position['target'] * (1 - slip)
                    reason = 'target'
                if reason:
                    pnl = (px - position['entry_price']) * position['qty']
                    cash += pnl
                    day_pnl += pnl
                    trades.append({'date': date.isoformat(), 'symbol': symbol, **position,
                                   'exit_time': b['et'].isoformat(), 'exit_price': px,
                                   'pnl': pnl, 'reason': reason})
                    position = None
            if (not traded_today and position is None and not scheduled_buy and
                dt.time(10, 0) <= bt < dt.time(14, 55) and i + 1 < len(daily) and
                day_pnl > -DAILY_LOSS_LIMIT and prev_close is not None and
                prev_close <= range_high and b['close'] > range_high):
                scheduled_buy = True  # enter at next bar OPEN, never same close
            prev_close = b['close']
        if position is not None:
            # Missing 15:50 bar (e.g. truncated data); mark that fill as uncertain.
            b = daily[-1]
            px = b['close'] * (1 - slip)
            pnl = (px - position['entry_price']) * position['qty']
            cash += pnl
            trades.append({'date': date.isoformat(), 'symbol': symbol, **position,
                           'exit_time': b['et'].isoformat(), 'exit_price': px,
                           'pnl': pnl, 'reason': 'last_available_bar_ESTIMATE'})
            position = None
        curve.append((date.isoformat(), cash))
    wins = [t['pnl'] for t in trades if t['pnl'] > 0]
    losses = [t['pnl'] for t in trades if t['pnl'] < 0]
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else None
    closed_days = len(groups) - skipped
    benchmark_pct = 100 * (bars[-1]['close'] / bars[0]['close'] - 1)
    return {
        'symbol': symbol, 'capital': STARTING_CASH, 'final_equity': cash,
        'pnl': cash - STARTING_CASH, 'return_pct': 100 * (cash / STARTING_CASH - 1),
        'trades': trades, 'trade_count': len(trades), 'wins': len(wins),
        'win_rate': (len(wins) / len(trades) * 100) if trades else 0.0,
        'profit_factor': profit_factor, 'max_drawdown_pct': calculate_drawdown(curve),
        'curve': curve, 'benchmark_pct': benchmark_pct,
        'bars': len(bars), 'sessions': len(groups), 'eligible_sessions': closed_days,
        'skipped_sessions': skipped,
        'start': bars[0]['et'].isoformat(), 'end': bars[-1]['et'].isoformat(),
    }


def save_report(result, source, prefix, open_browser=True):
    REPORTS.mkdir(exist_ok=True, parents=True)
    trades_path = REPORTS / (prefix + '_trades.csv')
    summary_path = REPORTS / (prefix + '_summary.json')
    report_path = REPORTS / (prefix + '_report.html')
    trade_fields = ['date', 'symbol', 'entry_time', 'entry_price', 'exit_time', 'exit_price', 'qty', 'pnl', 'reason']
    with trades_path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=trade_fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(result['trades'])
    summary = {k: v for k, v in result.items() if k not in ('trades', 'curve')}
    summary['data_source'] = source
    summary['strategy'] = 'Long-only 30-minute opening-range breakout; next-bar-open entry; 0.5% stop; 1% target'
    summary['assumed_slippage_bps_per_side'] = SLIP_BPS_PER_SIDE
    summary_path.write_text(json.dumps(summary, indent=2), encoding='utf-8')
    report_path.write_text(make_html(result, source), encoding='utf-8')
    print('\n  Data:', source)
    print('  Sessions:', result['sessions'], '| Trades:', result['trade_count'])
    print('  Ending balance: $%.2f | Net P/L: $%+.2f (%.2f%%)' % (result['final_equity'], result['pnl'], result['return_pct']))
    print('  Win rate: %.1f%% | Max drawdown: %.2f%%' % (result['win_rate'], result['max_drawdown_pct']))
    print('  Full report:', report_path)
    print('  Trade ledger:', trades_path)
    if open_browser:
        webbrowser.open(report_path.resolve().as_uri())
    return report_path


def make_html(result, source):
    esc = html.escape
    curve = result['curve']
    values = [v for _, v in curve]
    lo, hi = min(values), max(values)
    if abs(hi - lo) < 0.01:
        lo -= 2
        hi += 2
    x0, y0, width, height = 46, 18, 920, 242
    pts = []
    for i, (_, val) in enumerate(curve):
        x = x0 + (i / max(1, len(curve) - 1)) * width
        y = y0 + (hi - val) / (hi - lo) * height
        pts.append('%.1f,%.1f' % (x, y))
    baseline_y = y0 + (hi - STARTING_CASH) / (hi - lo) * height
    polyline = ' '.join(pts)
    rows = []
    for t in result['trades']:
        style = 'pos' if t['pnl'] >= 0 else 'neg'
        rows.append('<tr><td>%s</td><td>%s</td><td>$%.2f</td><td>%s</td><td>$%.2f</td><td class="%s">%+.2f</td><td>%s</td></tr>' % (
            esc(t['date']), esc(t['entry_time'][11:16]), t['entry_price'],
            esc(t['exit_time'][11:16]), t['exit_price'], style, t['pnl'], esc(t['reason'])))
    if not rows:
        rows.append('<tr><td colspan="7">No simulated trades triggered this period.</td></tr>')
    result_color = 'pos' if result['pnl'] >= 0 else 'neg'
    warning = ('SYNTHETIC SAMPLE: Generated numbers demonstrate the backtesting program only. '
               'This is not a real-world strategy result.' if 'SYNTHETIC' in source.upper() else
               'HISTORICAL IEX ONLY: This is a simplified backtest, not paper-broker executions. '
               'IEX bars cover one exchange, not the consolidated US market. Results may be unreliable.')
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>TradeLab | %s</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0b1220;color:#e9eef9;font:15px/1.55 system-ui,Segoe UI,Arial,sans-serif}
main{max-width:1100px;margin:auto;padding:36px 22px 70px}.eyebrow{color:#8ba9cf;font-size:12px;letter-spacing:1.8px;text-transform:uppercase}
h1{margin:4px 0 6px;font-size:32px}p{color:#adbed3}.alert{background:#42301b;border:1px solid #986631;border-radius:12px;padding:14px 17px;margin:24px 0;color:#ffe5b0;font-weight:650}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.tile,.panel{background:#141f30;border:1px solid #26394f;border-radius:12px;padding:19px}.tile small{color:#a6b6c9}.big{font-weight:750;font-size:26px;margin-top:6px;white-space:nowrap}.pos{color:#6cdda9}.neg{color:#ffa09b}.panel{margin-top:17px}h2{font-size:20px;margin:0 0 10px}svg{display:block;max-width:100%%;height:auto}
table{width:100%%;border-collapse:collapse}th,td{text-align:left;border-bottom:1px solid #2a3b4f;padding:10px 8px;white-space:nowrap}th{color:#b8cce3} .tablewrap{overflow:auto}
.notes{line-height:1.75;color:#bac6d5;font-size:13px}code{color:#badbf4}.fine{color:#8fa3b9;font-size:12px}
@media(max-width:780px){.grid{grid-template-columns:repeat(2,1fr)}.big{font-size:20px}}
</style></head><body><main><div class="eyebrow">Trading experiment / version 0.1</div>
<h1>TradeLab <span style="font-weight:400;color:#86a5c5">%s</span></h1>
<p>Source: %s<br>%s to %s (%s five-minute bars; %s observed sessions).</p>
<div class="alert">%s No real money or broker orders used.</div>
<div class="grid">
<div class="tile"><small>Simulated final balance</small><div class="big">$%.2f</div></div>
<div class="tile"><small>Net profit / loss</small><div class="big %s">$%+.2f</div></div>
<div class="tile"><small>Simulated trades</small><div class="big">%d</div></div>
<div class="tile"><small>Win rate</small><div class="big">%.1f%%</div></div>
<div class="tile"><small>Return on $500</small><div class="big %s">%+.2f%%</div></div>
<div class="tile"><small>Max end-of-day drawdown</small><div class="big">%.2f%%</div></div>
<div class="tile"><small>Buy-and-hold price change*</small><div class="big">%+.2f%%</div></div>
<div class="tile"><small>Eligible sessions</small><div class="big">%d / %d</div></div>
</div><div class="panel"><h2>Simulated end-of-day equity</h2>
<svg viewBox="0 0 1010 288" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Equity chart">
<line x1="46" y1="%.1f" x2="966" y2="%.1f" stroke="#637b91" stroke-dasharray="4 6"/>
<text x="48" y="12" fill="#b8cce3" font-size="12">$%.2f</text>
<text x="48" y="278" fill="#b8cce3" font-size="12">$%.2f</text>
<polyline fill="none" stroke="#69c6e8" stroke-width="3" stroke-linejoin="round" points="%s"/>
</svg><div class="fine">Daily close-to-close simulated balance. Intraday drawdown can be larger.</div></div>
<div class="panel"><h2>Trades</h2><div class="tablewrap"><table><thead><tr><th>Date</th><th>Entered (ET)</th><th>Buy</th><th>Exited (ET)</th><th>Sell</th><th>P/L</th><th>Exit reason</th></tr></thead><tbody>%s</tbody></table></div></div>
<div class="panel"><h2>What the strategy is actually doing</h2><div class="notes">
Only one long position in SPY per day. It observes the first 30 minutes (9:30-10:00 ET), watches for a later 5-minute bar to close above that range, then simulates buying at the next bar's open. It uses up to 50%% of the experiment equity, never exceeding $250, a 0.5%% stop and 1%% price target. It sells before market close if the bar exists. No shorting or margin. Assumes 3 basis points of slippage on BOTH entry and exit; no commission or taxes. If the stop and target both occur inside the same 5-minute candle, the stop is assumed to hit first. Incomplete session data may force an estimated final-bar exit. Daily loss control is only a $5 pre-entry check, NOT a hard guaranteed loss cap. This is an educational research experiment, not investment advice.</div>
<p class="fine">*Buy-and-hold is SPY's unadjusted price change across observed IEX bars, not a total-return or equal-risk benchmark. Historical results do not predict future performance.</p></div>
</main></body></html>''' % (
        esc(result['symbol']), esc(result['symbol']), esc(source), esc(result['start']), esc(result['end']),
        result['bars'], result['sessions'], esc(warning),
        result['final_equity'], result_color, result['pnl'], result['trade_count'], result['win_rate'],
        result_color, result['return_pct'], result['max_drawdown_pct'], result['benchmark_pct'],
        result['eligible_sessions'], result['sessions'],
        baseline_y, baseline_y, hi, lo, esc(polyline), ''.join(rows))


def main(argv=None):
    p = argparse.ArgumentParser(description='TradeLab v0.1: SIMULATION ONLY. Never places broker orders.')
    sub = p.add_subparsers(dest='mode', required=True)
    demo = sub.add_parser('demo', help='Offline synthetic demo (no account, no internet)')
    demo.add_argument('--days', type=int, default=30)
    historic = sub.add_parser('backtest', help='Fetch real Alpaca IEX 5-min bars with FREE paper API credentials')
    historic.add_argument('--symbol', default='SPY')
    historic.add_argument('--days', type=int, default=60)
    replay = sub.add_parser('replay', help='Backtest a local CSV of 5-min OHLC bars')
    replay.add_argument('csv_path')
    replay.add_argument('--symbol', default='SPY')
    for subparser in (demo, historic, replay):
        subparser.add_argument('--no-open', action='store_true', help='Do not open the HTML report')
    args = p.parse_args(argv)
    if args.mode == 'demo':
        if not 1 <= args.days <= 1000:
            p.error('--days must be 1 to 1000')
        bars = generate_synthetic(args.days)
        write_csv(DATA / 'synthetic_demo_5min.csv', bars)
        result = backtest(bars, 'SPY')
        save_report(result, 'SYNTHETIC OFFLINE DEMO', 'demo', not args.no_open)
    elif args.mode == 'backtest':
        if not 1 <= args.days <= 365:
            p.error('--days must be 1 to 365')
        if args.symbol.upper() != 'SPY':
            p.error('v0.1 intentionally supports SPY only, to avoid misleading multi-symbol comparisons')
        print('Fetching', args.days, 'calendar days of 5-minute SPY bars, feed=iex (single exchange) ...')
        bars = get_alpaca_bars('SPY', args.days)
        if not bars:
            raise RuntimeError('Alpaca returned no usable regular-session bars.')
        write_csv(DATA / 'SPY_alpaca_iex_5min.csv', bars)
        result = backtest(bars, 'SPY')
        save_report(result, 'REAL ALPACA IEX HISTORICAL DATA (simplified simulation)', 'SPY_historical', not args.no_open)
    else:
        bars = load_csv(args.csv_path)
        result = backtest(bars, args.symbol.upper())
        save_report(result, 'USER-PROVIDED CSV (provenance unverified)', 'csv_replay', not args.no_open)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print('\nERROR:', exc, file=sys.stderr)
        sys.exit(1)
