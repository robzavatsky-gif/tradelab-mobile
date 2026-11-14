#!/usr/bin/env python3
"""TradeLab 0.2: reproducible multi-strategy historical simulations ONLY.

No brokerage trading requests, no paper order execution, no money movement.
Python standard library only. All data is single-exchange IEX or supplied CSV.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import math
from pathlib import Path
import statistics
import sys
import webbrowser

import v01_engine as market

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
REPORTS = ROOT / 'reports'
CAPITAL = 500.0
MAX_NOTIONAL = 250.0
MAX_EQUITY_FRACTION = 0.50
STOP_PCT = 0.005
TARGET_PCT = 0.010
DEFAULT_SLIP_BPS = 3
MIN_VALID_DAYS = 15
STRATEGIES = ('Opening-range breakout', 'VWAP reclaim', 'EMA trend pullback')
TRADE_COLUMNS = ['segment', 'strategy', 'date', 'symbol', 'signal_time_et',
                 'entry_time_et', 'entry_open', 'entry_fill', 'quantity',
                 'notional', 'stop', 'target', 'exit_time_et', 'exit_reference',
                 'exit_fill', 'exit_reason', 'pnl', 'slippage_bps_per_side']


def by_session(bars):
    groups = {}
    for b in bars:
        groups.setdefault(b['et'].date().isoformat(), []).append(b)
    return {d: sorted(arr, key=lambda b: b['t']) for d, arr in sorted(groups.items())}


def assess_sessions(bars):
    """Retain only complete, consecutive, full 78-bar regular sessions.
    Screening applies equally across strategies, before evaluating any signals.
    This limits missing-price fill assumptions, but can introduce selection bias.
    """
    kept, rejected, notes = {}, {}, {}
    grid_start = dt.time(9, 30)
    for date, daily in by_session(bars).items():
        times = [b['et'].time() for b in daily]
        expected_first = {dt.time(9, 30 + 5 * i) for i in range(6)}
        morning_ok = expected_first.issubset(set(times))
        missing_interval = any((daily[i]['t'] - daily[i-1]['t']) != dt.timedelta(minutes=5)
                               for i in range(1, len(daily)))
        reasons = []
        if len(daily) != 78: reasons.append(f'{len(daily)} of 78 expected bars')
        if not morning_ok: reasons.append('missing opening-range bar')
        if grid_start not in times: reasons.append('missing 09:30 bar')
        if dt.time(15, 50) not in times: reasons.append('missing 15:50 exit bar')
        if dt.time(15, 55) not in times: reasons.append('missing 15:55 bar')
        if missing_interval: reasons.append('nonconsecutive 5-minute bars')
        if reasons:
            rejected[date] = '; '.join(reasons)
        else:
            kept[date] = daily
            notes[date] = len(daily)
    return kept, rejected, notes


def signals(daily, strategy):
    """Compute signal-bar index from closes, never referencing future bars.

    Bars are time-stamped at the BEGINNING of each five-minute period.
    Signals fire only after the signal bar finishes; entry at next bar open.
    """
    if strategy not in STRATEGIES:
        raise ValueError('Unknown strategy ' + str(strategy))
    n = len(daily)
    values = []
    ema9 = ema21 = None
    cumulative_pv = cumulative_v = 0.0
    prev_vwap = 0.0
    for i, b in enumerate(daily):
        close = b['close']
        ema9 = close if ema9 is None else 2 / 10 * close + 8 / 10 * ema9
        ema21 = close if ema21 is None else 2 / 22 * close + 20 / 22 * ema21
        vol = max(b['volume'], 0.0)
        cumulative_v += vol
        cumulative_pv += vol * (b['high'] + b['low'] + close) / 3
        vwap = cumulative_pv / cumulative_v if cumulative_v else float('nan')
        values.append({'ema9': ema9, 'ema21': ema21, 'vwap': vwap})
    if n < 30: return []
    opening_high = max(b['high'] for b in daily[:6])
    found = []
    for i in range(6, n - 1):
        b = daily[i]
        nxt = daily[i+1]
        t = b['et'].time()
        next_t = nxt['et'].time()
        # Signal bar 10:00 is only observed after 10:05. Last entry 14:55.
        if not (dt.time(10, 0) <= next_t <= dt.time(14, 55)):
            continue
        if nxt['t'] - b['t'] != dt.timedelta(minutes=5):
            continue
        prior = daily[i-1]
        now_i, prev_i = values[i], values[i-1]
        hit = False
        if strategy == STRATEGIES[0]:
            hit = prior['close'] <= opening_high and b['close'] > opening_high
        elif strategy == STRATEGIES[1] and i >= 12:
            # 5-min OHLC-based *approximate* intraday VWAP, not tick VWAP.
            hit = (b['volume'] > 0 and math.isfinite(prev_i['vwap']) and
                   math.isfinite(now_i['vwap']) and
                   prior['close'] <= prev_i['vwap'] and b['close'] > now_i['vwap'] and
                   now_i['vwap'] > prev_i['vwap'])
        elif strategy == STRATEGIES[2] and i >= 25:
            hit = (now_i['ema9'] > now_i['ema21'] > values[i-1]['ema21'] and
                   prior['close'] <= prev_i['ema9'] and b['close'] > now_i['ema9'] and
                   math.isfinite(now_i['vwap']) and b['close'] > now_i['vwap'])
        if hit: found.append(i)
    return found


def simulate_day(date, daily, strategy, equity, slip_bps, symbol='SPY'):
    """One round-trip at most. Stop before target within ambiguous OHLC bar.
    Never make assumptions about missing bars while position is held.
    """
    signal_ids = signals(daily, strategy)
    if not signal_ids: return None
    slip = slip_bps / 10000.0
    # Data quality screening is uniform per whole day; no strategy-specific
    # retrospective skip based on what happened *after* an entry signal.
    for si in signal_ids:
        b = daily[si+1]
        final = 76  # bar starting 15:50 ET in a complete regular session
        notional = min(MAX_NOTIONAL, max(equity, 0) * MAX_EQUITY_FRACTION)
        if notional < 1.0: return None
        entry_ref = b['open']
        entry = entry_ref * (1 + slip)
        qty = notional / entry
        stop, target = entry * (1 - STOP_PCT), entry * (1 + TARGET_PCT)
        for j in range(si+1, final+1):
            cur = daily[j]
            tm = cur['et'].time()
            if tm >= dt.time(15, 50):
                exit_ref = cur['open']
                reason = 'end_of_day'
            elif cur['open'] <= stop:
                exit_ref = cur['open']
                reason = 'stop_gap'
            elif cur['low'] <= stop:
                exit_ref = stop
                reason = 'stop'
            elif cur['high'] >= target:
                exit_ref = target
                reason = 'target'
            else:
                continue
            exit_fill = exit_ref * (1 - slip)
            return {'date': date, 'symbol': symbol, 'strategy': strategy,
                    'signal_time_et': daily[si]['et'].isoformat(),
                    'entry_time_et': b['et'].isoformat(),
                    'entry_open': entry_ref, 'entry_fill': entry,
                    'quantity': qty, 'notional': notional, 'stop': stop, 'target': target,
                    'exit_time_et': cur['et'].isoformat(), 'exit_reference': exit_ref,
                    'exit_fill': exit_fill, 'exit_reason': reason,
                    'pnl': (exit_fill - entry) * qty,
                    'slippage_bps_per_side': slip_bps}
    return None


def summarize(orders, curve, sessions, start_price, end_price):
    pnl = sum(t['pnl'] for t in orders)
    gains = sum(t['pnl'] for t in orders if t['pnl'] > 0)
    losses = sum(t['pnl'] for t in orders if t['pnl'] < 0)
    peak, maxdd = CAPITAL, 0.0
    for _, v in curve:
        peak = max(peak, v)
        if peak: maxdd = max(maxdd, (peak - v) / peak * 100)
    wins = sum(t['pnl'] > 0 for t in orders)
    return {'capital': CAPITAL, 'ending_balance': CAPITAL + pnl, 'pnl': pnl,
            'return_pct': pnl / CAPITAL * 100, 'trade_count': len(orders),
            'wins': wins, 'losses': len(orders)-wins,
            'win_rate_pct': wins / len(orders) * 100 if orders else 0,
            'avg_trade_pnl': pnl / len(orders) if orders else 0,
            'profit_factor': gains / abs(losses) if losses < 0 else None,
            'max_eod_drawdown_pct': maxdd, 'worst_trade_pnl': min((t['pnl'] for t in orders), default=0),
            'sessions': sessions,
            'benchmark_spy_price_pct': (end_price / start_price - 1)*100 if start_price else 0,
            'full_day_stop_exits': sum(t['exit_reason'].startswith('stop') for t in orders)}


def evaluate(days, strategy, slip_bps=DEFAULT_SLIP_BPS, symbol='SPY'):
    if not days: raise ValueError('No eligible sessions')
    equity = CAPITAL
    orders = []
    curve = [('Start', equity)]
    for date, daily in sorted(days.items()):
        trade = simulate_day(date, daily, strategy, equity, slip_bps, symbol)
        if trade:
            orders.append(trade)
            equity += trade['pnl']
        curve.append((date, equity))
    whole = [b for arr in days.values() for b in arr]
    return {'stats': summarize(orders, curve, len(days), whole[0]['open'], whole[-1]['close']),
            'trades': orders, 'curve': curve}


def split_sessions(days, fraction=0.7):
    dates = sorted(days)
    if len(dates) < MIN_VALID_DAYS:
        raise ValueError(f'Only {len(dates)} usable full sessions; need at least {MIN_VALID_DAYS}. Try a longer history.')
    cut = max(1, min(len(dates)-1, int(len(dates)*fraction)))
    return ({d: days[d] for d in dates[:cut]}, {d: days[d] for d in dates[cut:]})


def research(bars, source, symbol='SPY'):
    days, rejected, bar_counts = assess_sessions(bars)
    train_days, validate_days = split_sessions(days)
    results = {'version': '0.2', 'symbol': symbol, 'data_source': source,
               'received_bars': len(bars), 'usable_days': len(days),
               'excluded_days': rejected, 'partial_days': {d: n for d,n in bar_counts.items() if n != 78},
               'first_session': min(days), 'last_session': max(days),
               'train_start': min(train_days), 'train_end': max(train_days),
               'validation_start': min(validate_days), 'validation_end': max(validate_days),
               'slippage_bps_per_side': DEFAULT_SLIP_BPS,
               'cost_sensitivity_bps_per_side': [0, 3, 6, 10],
               'strategies': {}, 'cost_sensitivity': {}}
    for strategy in STRATEGIES:
        results['strategies'][strategy] = {
            'earlier': evaluate(train_days, strategy, symbol=symbol),
            'later': evaluate(validate_days, strategy, symbol=symbol),
            'all': evaluate(days, strategy, symbol=symbol)}
        results['cost_sensitivity'][strategy] = {
            str(cost): evaluate(validate_days, strategy, cost, symbol)['stats']
            for cost in results['cost_sensitivity_bps_per_side']}
    return results


def draw_svg(curve, color='#67b8df'):
    if len(curve) < 2: return '<p>No data.</p>'
    vals = [v for _, v in curve]
    lo, hi = min(min(vals), CAPITAL), max(max(vals), CAPITAL)
    if hi-lo < 0.05: hi += 1; lo -= 1
    points = []
    for i, (_, val) in enumerate(curve):
        x = 38+840 * (i / max(len(curve)-1, 1))
        y = 18+192 * ((hi-val)/(hi-lo))
        points.append(f'{x:.1f},{y:.1f}')
    base = 18+192*((hi-CAPITAL)/(hi-lo))
    return (f'<svg viewBox="0 0 920 234" role="img" aria-label="Simulated equity over sessions">'
            f'<line x1="38" y1="{base:.1f}" x2="878" y2="{base:.1f}" stroke="#8d9bad" stroke-dasharray="4 7"/>'
            f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="3"/>'
            f'<text x="38" y="11" fill="#b8c7d9" font-size="12">${hi:.2f}</text>'
            f'<text x="38" y="228" fill="#b8c7d9" font-size="12">${lo:.2f}</text></svg>')


def make_report(r):
    e = html.escape
    def money(v): return f'${v:+.2f}'
    def cls(v): return 'win' if v > 0 else ('lose' if v < 0 else '')
    summary_rows = []
    for strategy in STRATEGIES:
        result = r['strategies'][strategy]
        early, later = result['earlier']['stats'], result['later']['stats']
        summary_rows.append('<tr>' + f'<td><strong>{e(strategy)}</strong></td>' +
                            f'<td class="{cls(early["pnl"])}">{money(early["pnl"])}</td><td>{early["trade_count"]}</td>' +
                            f'<td class="{cls(later["pnl"])}">{money(later["pnl"])}</td><td>{later["trade_count"]}</td>' +
                            f'<td>{later["win_rate_pct"]:.1f}%</td><td>{later["max_eod_drawdown_pct"]:.2f}%</td></tr>')
    cards = []
    for strategy in STRATEGIES:
        seg = r['strategies'][strategy]['later']
        s = seg['stats']
        curves = draw_svg(seg['curve'])
        worst = s['worst_trade_pnl']
        sens = r['cost_sensitivity'][strategy]
        sensitivity_rows = ''.join(f'<tr><td>{cost} bps / side</td><td class="{cls(v["pnl"])}">{money(v["pnl"])}</td>'
                                   f'<td>{v["trade_count"]}</td></tr>' for cost, v in sens.items())
        sample_warning = ('<p class="notes"><b>Small sample warning:</b> fewer than 20 later-period trades. '
                          'Do not infer an edge from this result.</p>' if s['trade_count'] < 20 else '')
        trades_rows = ''.join('<tr>' +
            f'<td>{e(t["date"])}</td><td>{e(t["entry_time_et"][11:16])}</td>' +
            f'<td>{e(t["exit_time_et"][11:16])}</td>' +
            f'<td class="{cls(t["pnl"])}">{money(t["pnl"])}</td><td>{e(t["exit_reason"])}</td></tr>'
            for t in seg['trades']) or '<tr><td colspan="5">No trades in this segment.</td></tr>'
        cards.append(f'<section><h2>{e(strategy)} <small>later-period check</small></h2>'
                     f'<div class="metrics"><span>Net <b class="{cls(s["pnl"])}">{money(s["pnl"])}</b></span>'
                     f'<span>Win rate <b>{s["win_rate_pct"]:.1f}%</b></span>'
                     f'<span>Worst trade <b>{money(worst)}</b></span>'
                     f'<span>EOD drawdown <b>{s["max_eod_drawdown_pct"]:.2f}%</b></span></div>{curves}'
                     f'<h3>Slippage sensitivity (later period)</h3><table><tr><th>Cost assumption</th><th>Net P/L</th><th>Trades</th></tr>'
                     + sensitivity_rows + '</table>' + sample_warning +
                     '<h3>Later-period trades</h3><div class="scroll"><table>'
                     '<tr><th>Date</th><th>Entered (ET)</th><th>Exited (ET)</th><th>Net P/L</th><th>Exit</th></tr>'
                     + trades_rows + '</table></div></section>')
    excluded = list(r['excluded_days'].items())
    excluded_rows = ''.join(f'<tr><td>{e(d)}</td><td>{e(reason)}</td></tr>' for d,reason in excluded[:25])
    quality = (f'{len(excluded)} excluded partial or incomplete session(s). All {r["usable_days"]} '
               f'retained sessions have 78 consecutive five-minute bars (09:30-15:55 ET). '
               f'Screening out incomplete IEX days can itself bias the sample. Up to 25 exclusion reasons shown below.')
    alert = 'SYNTHETIC DEMONSTRATION — cannot estimate real-world strategy performance.' if 'SYNTHETIC' in r['data_source'].upper() else ('IEX SINGLE-EXCHANGE DATA ONLY — not consolidated US prices or real broker fills. Historical simulated results may be inaccurate.')
    later_n = r['strategies'][STRATEGIES[0]]['later']['stats']['sessions']
    earlier_n = r['strategies'][STRATEGIES[0]]['earlier']['stats']['sessions']
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>TradeLab 0.2 - Strategy comparison</title><style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{margin:0;background:#0a1320;color:#ecf2f8;font:15px/1.5 system-ui,Segoe UI,Arial,sans-serif}}
main{{max-width:1180px;margin:auto;padding:34px 20px 60px}}h1{{font-size:31px;margin:5px 0 9px}}h2{{margin-top:0}}h3{{font-size:16px}}
p,small{{color:#b0c0d5}}.sub{{color:#88abd2;text-transform:uppercase;letter-spacing:2px;font-size:12px}}.warn{{background:#3b2d1f;border:1px solid #8a6038;border-radius:12px;padding:15px;color:#ffe5b2;font-weight:600;margin:22px 0}}
section{{background:#142135;border:1px solid #293e57;border-radius:13px;padding:20px;margin:17px 0}}
table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}th,td{{padding:10px 12px;border-bottom:1px solid #2e4055;text-align:left;white-space:nowrap}}th{{color:#a6b9d2}}.scroll{{overflow:auto}}.win{{color:#7de0b4}}.lose{{color:#ff9898}}
.metrics{{display:flex;flex-wrap:wrap;gap:20px;margin-bottom:10px}}.metrics span{{color:#a8bbd1}}.metrics b{{color:#ecf2f8;display:block;font-size:23px}}.metrics .win{{color:#7de0b4}}.metrics .lose{{color:#ff9898}}
.notes{{font-size:13px;color:#b1c2d4}}.callout{{border-left:3px solid #7096b7;padding-left:14px}}svg{{display:block;width:100%;height:auto;max-height:234px}}
</style></head><body><main><div class="sub">Simulated trading research · version 0.2</div><h1>TradeLab — SPY strategy comparison</h1>
<p>Data: {e(r['data_source'])}<br>Full observed period: {e(r['first_session'])} to {e(r['last_session'])}; {r['received_bars']:,} market bars; {r['usable_days']} eligible sessions.</p>
<div class="warn">{e(alert)} NO brokerage orders are submitted. No real money is involved.</div>
<section><h2>Chronological test design</h2><p><b>Earlier period:</b> {r['train_start']} to {r['train_end']} ({earlier_n} sessions). &nbsp; <b>Later period:</b> {r['validation_start']} to {r['validation_end']} ({later_n} sessions).</p>
<p class="notes">The three strategy definitions are fixed before evaluation. The later period is a chronological check, not proof of a genuinely untouched holdout: earlier TradeLab results already showed some recent data. Do not pick a winner and then claim its later-period outcome is an independent blind test.</p>
<div class="scroll"><table><thead><tr><th>Strategy</th><th>Earlier P/L</th><th>Trades</th><th>Later P/L</th><th>Trades</th><th>Later win rate</th><th>Later EOD drawdown</th></tr></thead><tbody>{''.join(summary_rows)}</tbody></table></div>
<p class="notes">Each strategy and period independently begins at $500. Dollar outcomes are not portfolio-combinable. Baseline: no-trade strategy returns $0. SPY buy-and-hold price changes are included in summary JSON but are not risk matched.</p></section>
{''.join(cards)}
<section><h2>Data quality</h2><p>{e(quality)}</p><div class="scroll"><table><tr><th>Excluded date</th><th>Reason</th></tr>{excluded_rows or '<tr><td colspan="2">No sessions excluded.</td></tr>'}</table></div></section>
<section><h2>Execution assumptions & limitations</h2><div class="notes callout">
<p>Each method is <b>long only</b>, uses at most one simulated trade per day, and never holds overnight. Position size is the lesser of $250 and 50% of current segment equity; fractional shares are assumed. Stops are 0.5% and targets 1% of the slipped entry price, applied identically to all strategies. Base slippage is 3 bps on entry and 3 bps on exit. No commissions, dividends, fees, taxes, real bid/ask spreads, market impact, or actual fill verification.</p>
<p>Signals use only completed 5-minute bars and execute at the <i>next</i> bar opening price. If a bar touches both target and stop, the stop is assumed first. Gap-through stops fill at the worse opening price. All incomplete trading sessions are excluded uniformly before evaluating signals; this may bias the sample. End-of-day exit uses the 15:50 ET bar open. The displayed drawdown is based on <i>end-of-day realized equity only</i> and can understate intraday risk. There is no guaranteed loss cap.</p>
<p>VWAP is approximated from volume and 5-minute typical price, not the consolidated tick stream. IEX provides only one exchange's prices and volume. These backtests cannot establish a profitable edge. For the next genuinely prospective test, freeze a strategy and evaluate future, unseen market days without tuning.</p>
</div></section></main></body></html>'''


def save(r, name, open_browser=True):
    REPORTS.mkdir(exist_ok=True, parents=True)
    html_path = REPORTS / (name + '_report.html')
    trades_path = REPORTS / (name + '_trades.csv')
    summary_path = REPORTS / (name + '_summary.json')
    compare_path = REPORTS / (name + '_comparison.csv')
    html_path.write_text(make_report(r), encoding='utf-8')
    compact = {k: v for k, v in r.items() if k != 'strategies'}
    compact['strategies'] = {s: {seg: r['strategies'][s][seg]['stats'] for seg in ('earlier','later','all')}
                             for s in STRATEGIES}
    summary_path.write_text(json.dumps(compact, indent=2), encoding='utf-8')
    with trades_path.open('w', newline='', encoding='utf-8') as f:
        wr = csv.DictWriter(f, fieldnames=TRADE_COLUMNS)
        wr.writeheader()
        for strategy in STRATEGIES:
            for segment in ('earlier', 'later'):
                for trade in r['strategies'][strategy][segment]['trades']:
                    wr.writerow({'segment': segment, **trade})
    with compare_path.open('w', newline='', encoding='utf-8') as f:
        fields = ['strategy','segment','capital','ending_balance','pnl','return_pct','trade_count','wins',
                  'losses','win_rate_pct','avg_trade_pnl','profit_factor','max_eod_drawdown_pct',
                  'worst_trade_pnl','sessions','benchmark_spy_price_pct','full_day_stop_exits']
        wr = csv.DictWriter(f, fieldnames=fields)
        wr.writeheader()
        for strategy in STRATEGIES:
            for seg in ('earlier','later','all'):
                wr.writerow({'strategy': strategy, 'segment': seg, **r['strategies'][strategy][seg]['stats']})
    print(f'\nREPORT: {html_path}\nTRADES: {trades_path}\nCOMPARISON: {compare_path}\nSUMMARY: {summary_path}')
    print(f'Eligible sessions: {r["usable_days"]}; excluded sessions: {len(r["excluded_days"])}')
    print('Strategy                     Earlier P/L    Later P/L   Later trades')
    for strategy in STRATEGIES:
        es = r['strategies'][strategy]['earlier']['stats']
        ls = r['strategies'][strategy]['later']['stats']
        print(f'{strategy:28} ${es["pnl"]:+9.2f}   ${ls["pnl"]:+9.2f}      {ls["trade_count"]}')
    if open_browser: webbrowser.open(html_path.resolve().as_uri())
    return html_path


def main(argv=None):
    p = argparse.ArgumentParser(description='TradeLab 0.2, historical backtesting only, NO broker orders')
    sub = p.add_subparsers(dest='cmd', required=True)
    synthetic = sub.add_parser('demo', help='Offline synthetic illustration - not real market data')
    synthetic.add_argument('--days', type=int, default=60)
    historic = sub.add_parser('backtest', help='Download new SPY IEX historical prices using paper API keys')
    historic.add_argument('--days', type=int, default=180)
    replay = sub.add_parser('replay', help='Re-evaluate a local CSV of five-minute OHLC bars')
    replay.add_argument('csv_file', type=Path)
    for cmd in (synthetic,historic,replay):
        cmd.add_argument('--no-open', action='store_true')
    args = p.parse_args(argv)
    if args.cmd == 'demo':
        if not 20 <= args.days <= 500: p.error('--days must be 20-500')
        bars = market.generate_synthetic(args.days)
        source = 'SYNTHETIC DEMONSTRATION, NOT REAL PRICES'
        name = 'demo'
        market.write_csv(DATA / 'sample_synthetic_5min.csv', bars)
    elif args.cmd == 'backtest':
        if not 30 <= args.days <= 730: p.error('--days must be 30-730')
        print('Retrieving SPY IEX 5-minute bars only; no orders. Days:',args.days)
        bars = market.get_alpaca_bars('SPY', args.days)
        if not bars: raise ValueError('No data returned by Alpaca')
        DATA.mkdir(parents=True, exist_ok=True)
        market.write_csv(DATA / 'SPY_alpaca_iex_5min.csv', bars)
        source = f'ALPACA HISTORICAL IEX SPY, {args.days} calendar days, raw prices'
        name = 'SPY_historical'
    else:
        bars = market.load_csv(args.csv_file)
        source = f'USER-SUPPLIED OHLC CSV: {args.csv_file.name} (verify provenance)'
        name = 'SPY_replay'
    r = research(bars, source)
    save(r, name, not args.no_open)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print('\nERROR:', exc, file=sys.stderr)
        sys.exit(1)
