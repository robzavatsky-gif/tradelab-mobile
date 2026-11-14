#!/usr/bin/env python3
"""TradeLab 0.3: paper-only research and chronological forward replay.

Only historical market-data GETs are made by inherited v01_engine. NO order APIs.
Versioned and locked rule set for future unseen sessions, from 2026-11-16.
Python 3.10+, no external packages.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import datetime as dt
import hashlib
import html
import json
import math
from pathlib import Path
import statistics
import sys
import tempfile
import webbrowser

import tradelab_v02 as old
import v01_engine as market

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
REPORTS = ROOT / 'reports'
FREEZE = ROOT / 'frozen_rules.json'
ARCHIVE = REPORTS / 'forward_archive.json'
START_DATE = '2026-11-16'
STRATEGIES = (
    'ORB baseline',
    'VWAP baseline',
    'EMA baseline',
    'ORB filtered',
    'EMA filtered',
    'ORB filtered 90m exit',
    'EMA filtered adaptive exit',
)
BASE_MAP = {
    'ORB': old.STRATEGIES[0],
    'VWAP': old.STRATEGIES[1],
    'EMA': old.STRATEGIES[2],
}
SENSITIVITY_BPS = (0, 3, 6, 10)
TRADES_COLUMNS = [
    'period', 'strategy', 'date', 'regime', 'opening_hour_range_pct',
    'opening_hour_direction', 'signal_time_et', 'entry_time_et',
    'entry_fill', 'quantity', 'notional', 'stop', 'target',
    'exit_time_et', 'exit_fill', 'exit_reason', 'pnl',
    'signal_vwap_distance_pct', 'signal_volume_ratio',
    'slippage_bps_per_side',
]
DIAG_COLUMNS = [
    'period', 'date', 'strategy', 'regime', 'opening_hour_range_pct',
    'opening_hour_direction', 'first_hour_median_volume',
    'raw_signals', 'skipped_early', 'blocked_regime',
    'blocked_below_vwap', 'blocked_volume', 'eligible_signals',
    'decision', 'first_signal_time_et', 'trade_pnl',
]
COMPARE_COLUMNS = [
    'period', 'strategy', 'sessions', 'trade_count', 'trade_days_pct',
    'pnl', 'ending_balance', 'win_rate_pct', 'profit_factor',
    'max_eod_drawdown_pct', 'avg_trade_pnl', 'blocked_regime',
    'blocked_below_vwap', 'blocked_volume', 'no_signal_days',
]


def json_sha(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode('utf-8')).hexdigest()


def code_digests():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ('tradelab_v03.py', 'tradelab_v02.py', 'v01_engine.py')}


def manifest():
    frozen = json.loads(FREEZE.read_text(encoding='utf8'))
    if frozen.get('code_sha256') != code_digests():
        raise RuntimeError('Frozen strategy code changed. Forward evaluation STOPPED. Restore the original v0.3 package; do not silently re-score prospective days.')
    if frozen.get('first_forward_date') != START_DATE:
        raise RuntimeError('Forward start date changed; stopping.')
    return frozen, json_sha(frozen)


def opening_regime(daily):
    """Classification at 10:30 ET, using 9:30..10:25 bars ONLY.

    Deterministic prior rule thresholds, not derived from full-day prices.
    This classification is *not* retrospective knowledge of a true trend.
    """
    if len(daily) < 12:
        raise ValueError('Not enough opening-hour bars')
    first = daily[:12]
    opening = first[0]['open']
    range_frac = (max(x['high'] for x in first) - min(x['low'] for x in first)) / opening
    direction = (first[-1]['close'] - opening) / max((max(x['high'] for x in first) - min(x['low'] for x in first)), 1e-9)
    if range_frac >= .0075:
        regime = 'high_volatility'
    elif range_frac < .0025 or abs(direction) < .40:
        regime = 'sideways'
    elif direction >= .40:
        regime = 'uptrend'
    else:
        regime = 'downtrend'
    volumes = [max(0, x['volume']) for x in first]
    return {'regime': regime, 'opening_hour_range_pct': range_frac*100,
            'opening_hour_direction': direction,
            'first_hour_median_volume': statistics.median(volumes)}


def signal_features(daily):
    pv = 0.0
    vol = 0.0
    out = []
    for b in daily:
        v = max(0, b['volume'])
        pv += v * (b['high'] + b['low'] + b['close'])/3
        vol += v
        out.append({'vwap': pv / vol if vol else float('nan')})
    return out


def strategy_info(strategy):
    if strategy not in STRATEGIES:
        raise ValueError('Unknown strategy')
    underlying = 'ORB' if strategy.startswith('ORB') else 'VWAP' if strategy.startswith('VWAP') else 'EMA'
    gated = 'filtered' in strategy
    exit_mode = '90m' if '90m exit' in strategy else 'adaptive' if 'adaptive exit' in strategy else 'fixed'
    return underlying, gated, exit_mode


def choose_signal(daily, strategy):
    """Same-day diagnostic assessment with no future-looking entry features."""
    regime = opening_regime(daily)
    underlying, gated, mode = strategy_info(strategy)
    indexes = old.signals(daily, BASE_MAP[underlying])
    feats = signal_features(daily)
    diag = {'regime': regime['regime'], 'opening_hour_range_pct': regime['opening_hour_range_pct'],
            'opening_hour_direction': regime['opening_hour_direction'],
            'first_hour_median_volume': regime['first_hour_median_volume'],
            'raw_signals': len(indexes), 'skipped_early': 0, 'blocked_regime': 0,
            'blocked_below_vwap': 0, 'blocked_volume': 0, 'eligible_signals': 0,
            'first_signal_time_et': daily[indexes[0]]['et'].isoformat() if indexes else '',
            'decision': 'no_signal'}
    chosen = None
    for i in indexes:
        if gated:
            # We can classify the opening hour by 10:30, so only signals
            # observed after that instant can be allowed by the gate.
            if daily[i]['et'].time() < dt.time(10, 30):
                diag['skipped_early'] += 1
                continue
            if regime['regime'] != 'uptrend':
                diag['blocked_regime'] += 1
                continue
            vwap = feats[i]['vwap']
            if not math.isfinite(vwap) or daily[i]['close'] <= vwap:
                diag['blocked_below_vwap'] += 1
                continue
            if regime['first_hour_median_volume'] <= 0 or daily[i]['volume'] < .60 * regime['first_hour_median_volume']:
                diag['blocked_volume'] += 1
                continue
        diag['eligible_signals'] += 1
        if chosen is None:
            chosen = i
    if chosen is not None:
        diag['decision'] = 'trade'
    elif indexes:
        diag['decision'] = 'all_signals_blocked'
    # This is the confirmed signal bar, not the next bar's entry.
    if chosen is None:
        return None, diag, {}
    v = feats[chosen]['vwap']
    feature = {
        'signal_vwap_distance_pct': ((daily[chosen]['close']/v-1)*100 if math.isfinite(v) and v > 0 else None),
        'signal_volume_ratio': (daily[chosen]['volume']/regime['first_hour_median_volume']
                                if regime['first_hour_median_volume'] else None)}
    return chosen, diag, feature


def simulate(daily, date, strategy, equity, slip_bps=3):
    signal, diag, features = choose_signal(daily, strategy)
    if signal is None:
        return None, diag
    underlying, gated, mode = strategy_info(strategy)
    entry_i = signal+1
    entry_bar = daily[entry_i]
    slip = slip_bps / 10000
    if equity <= 2:
        diag['decision'] = 'insufficient_equity'
        return None, diag
    notional = min(old.MAX_NOTIONAL, equity*old.MAX_EQUITY_FRACTION)
    entry = entry_bar['open'] * (1+slip)
    qty = notional/entry
    range_frac = diag['opening_hour_range_pct']/100
    stop_pct = old.STOP_PCT if mode != 'adaptive' else max(.0035, min(.008, range_frac*.65))
    target_pct = old.TARGET_PCT if mode != 'adaptive' else 1.5*stop_pct
    stop, target = entry*(1-stop_pct), entry*(1+target_pct)
    for j in range(entry_i, 77):
        bar = daily[j]
        # Exits strictly at this bar's OPEN on time-based decisions. Intrabar
        # high/low cannot influence a decision known at the same opening.
        if j >= 76:
            px, why = bar['open'], 'end_of_day'
        elif mode == '90m' and j-entry_i >= 18:
            px, why = bar['open'], 'time_90m'
        elif bar['open'] <= stop:
            px, why = bar['open'], 'stop_gap'
        elif bar['low'] <= stop:
            px, why = stop, 'stop'
        elif bar['high'] >= target:
            px, why = target, 'target'
        else:
            continue
        sell_fill = px*(1-slip)
        trade = {'strategy': strategy, 'date': date, 'regime': diag['regime'],
                 'opening_hour_range_pct': diag['opening_hour_range_pct'],
                 'opening_hour_direction': diag['opening_hour_direction'],
                 'signal_time_et': daily[signal]['et'].isoformat(),
                 'entry_time_et': entry_bar['et'].isoformat(), 'entry_fill': entry,
                 'quantity': qty, 'notional': notional, 'stop': stop, 'target': target,
                 'exit_time_et': bar['et'].isoformat(), 'exit_fill': sell_fill,
                 'exit_reason': why, 'pnl': (sell_fill-entry)*qty,
                 'slippage_bps_per_side': slip_bps, **features}
        return trade, diag
    raise RuntimeError('Complete day missing exit despite data quality filter')


def evaluate(days, strategy, slip_bps=3, period='research'):
    equity = old.CAPITAL
    curve = [('start', equity)]
    trades, diagnostics = [], []
    for date, daily in sorted(days.items()):
        trade, d = simulate(daily, date, strategy, equity, slip_bps)
        if trade:
            equity += trade['pnl']
            trades.append({'period': period, **trade})
        diagnostics.append({'period': period, 'date': date, 'strategy': strategy,
                            **d, 'trade_pnl': trade['pnl'] if trade else 0.0})
        curve.append((date, equity))
    allbars = [b for arr in days.values() for b in arr]
    first = allbars[0]['open'] if allbars else 1
    last = allbars[-1]['close'] if allbars else 1
    stats = old.summarize(trades, curve, len(days), first, last)
    stats.update({
        'trade_days_pct': len(trades)/len(days)*100 if days else 0,
        'blocked_regime': sum(d['blocked_regime'] for d in diagnostics),
        'blocked_below_vwap': sum(d['blocked_below_vwap'] for d in diagnostics),
        'blocked_volume': sum(d['blocked_volume'] for d in diagnostics),
        'no_signal_days': sum(d['decision'] == 'no_signal' for d in diagnostics),
    })
    return {'stats': stats, 'trades': trades, 'diagnostics': diagnostics, 'curve': curve}



def strat_regime_stats(groups):
    """Exploratory outcome breakdown; *never* used to choose the same-day trades."""
    rows = []
    for period, strategies in groups.items():
        for name, result in strategies.items():
            mapped = {x['date']:x for x in result['trades']}
            grouped = {}
            for d in result['diagnostics']:
                state = grouped.setdefault(d['regime'], {'sessions':0, 'trade_count':0,
                                                          'pnl':0.0, 'wins':0})
                state['sessions'] += 1
                tr = mapped.get(d['date'])
                if tr:
                    state['trade_count'] += 1
                    state['pnl'] += tr['pnl']
                    state['wins'] += tr['pnl'] > 0
            for regime, values in sorted(grouped.items()):
                rows.append({'period':period,'strategy':name,'regime':regime, **values,
                             'win_rate_pct': 100*values['wins']/values['trade_count']
                             if values['trade_count'] else 0})
    return rows


def equity_svg(group, options=('ORB baseline','ORB filtered','EMA baseline','EMA filtered')):
    """End-of-day simulated equity comparison, on same $500 per-strategy basis."""
    palettes = ('#90b5fa','#67e5b5','#f8b67a','#e4a8ef')
    seqs = [group[name]['curve'] for name in options]
    if not seqs or not seqs[0]:
        return '<p>No sessions evaluated.</p>'
    all_values = [x[1] for seq in seqs for x in seq]
    bottom, top = min(all_values+[500.0]), max(all_values+[500.0])
    if top-bottom < 0.2:
        bottom -= 1
        top += 1
    w, h = 900, 220
    def x(i, count): return 46+(w-70)*i/max(count-1,1)
    def y(v): return 20+(h-50)*(top-v)/(top-bottom)
    lines = [f'<line x1="46" x2="{w-24}" y1="{y(500):.1f}" y2="{y(500):.1f}" stroke="#8091a5" stroke-dasharray="5 5"/>']
    for name,curve,color in zip(options,seqs,palettes):
        coords = ' '.join(f'{x(i,len(curve)):.1f},{y(v):.1f}' for i,(_,v) in enumerate(curve))
        lines.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linecap="round"/>')
    labels = ''.join(f'<span style="display:inline-flex;align-items:center;gap:7px;margin-right:22px"><i style="width:12px;height:3px;background:{color}"></i>{html.escape(name)}</span>'
                     for name,color in zip(options,palettes))
    return (f'<svg role="img" aria-label="Simulated end of day equity for four compared strategies" viewBox="0 0 {w} {h}" style="width:100%;height:auto;max-height:340px">'
            +f'<text x="46" y="13" fill="#b1c7dd" font-size="12">${top:.2f}</text>'
            +f'<text x="46" y="{h-8}" fill="#b1c7dd" font-size="12">${bottom:.2f}</text>'
            +''.join(lines)+'</svg><div class="muted">'+labels+'</div>')


def regime_counts(days):
    return dict(Counter(opening_regime(day)['regime'] for day in days.values()))


def research(bars, source, min_days=15):
    days, rejected, _ = old.assess_sessions(bars)
    if len(days) < min_days:
        raise ValueError(f'Only {len(days)} complete sessions; need {min_days}. Try longer data.')
    early, later = old.split_sessions(days)
    data = {'mode': 'research', 'source': source, 'days': len(days),
            'first_day': min(days), 'last_day': max(days),
            'rejected': rejected, 'regimes': regime_counts(days), 'periods': {}, 'costs': {}}
    for label, group in (('earlier', early), ('later', later), ('all', days)):
        data['periods'][label] = {s: evaluate(group, s, period=label) for s in STRATEGIES}
    # Cost stress only on the two filtered strategies, not a giant optimizer grid.
    for strategy in ('ORB filtered', 'EMA filtered'):
        data['costs'][strategy] = {str(bps): evaluate(later, strategy, bps, 'later')['stats']['pnl']
                                   for bps in SENSITIVITY_BPS}
    return data


def canonical_bars(bars):
    return [{key: b[key] for key in ('timestamp','open','high','low','close','volume')} for b in bars]


def archive_forward(bars, archive_path=ARCHIVE):
    """Append new *complete* sessions; reject revisions to already archived sessions.

    This is an append-only research convention, not a secure/tamper-proof vault.
    The forward report reconstructs all sessions from the archive rather than
    silently dropping older sessions as the 240-day fetch window advances.
    """
    _, freeze_id = manifest()
    days, rejected, _ = old.assess_sessions(bars)
    forward_days = {d: day for d,day in days.items() if d >= START_DATE}
    if archive_path.exists():
        state = json.loads(archive_path.read_text(encoding='utf8'))
        if state.get('freeze_sha256') != freeze_id or state.get('start_date') != START_DATE:
            raise RuntimeError('Forward archive belongs to a different frozen ruleset. Aborting.')
    else:
        state = {'schema': 1, 'freeze_sha256': freeze_id, 'start_date': START_DATE,
                 'sessions': {}}
    pending = {}
    for date, data in sorted(forward_days.items()):
        canonical = canonical_bars(data)
        digest = json_sha(canonical)
        if date in state['sessions']:
            if state['sessions'][date]['sha256'] != digest:
                raise RuntimeError(f'Data revision on archived {date}. No changes made. Preserve archive and investigate source correction.')
        else:
            pending[date] = {'sha256': digest, 'bars': canonical}
    state['sessions'].update(pending)
    if pending:
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically in the same folder. Do not touch archive if validation fails.
        with tempfile.NamedTemporaryFile('w', encoding='utf8', dir=archive_path.parent,
                                         prefix='.forward-', suffix='.json', delete=False) as f:
            temp_path = Path(f.name)
            json.dump(state, f, indent=2)
        temp_path.replace(archive_path)
    rows = [b for d in sorted(state['sessions']) for b in state['sessions'][d]['bars']]
    restored = market.normalize_bars(rows)
    archived_days, invalid, _ = old.assess_sessions(restored)
    if invalid:
        raise RuntimeError('Forward archive has incomplete sessions: ' + str(invalid))
    return archived_days, len(pending), {d:why for d,why in rejected.items() if d >= START_DATE}, freeze_id


def forward(bars, source, archive_path=ARCHIVE):
    days, added, excluded, freeze_id = archive_forward(bars, archive_path)
    return {'mode': 'forward', 'source': source, 'days': len(days),
            'new_sessions': added, 'freeze_sha256': freeze_id,
            'first_day': min(days) if days else '', 'last_day': max(days) if days else '',
            'rejected': excluded, 'regimes': regime_counts(days), 'periods': {
                'prospective': {s: evaluate(days, s, period='prospective') for s in STRATEGIES}},
            'costs': {}}


def make_html(result):
    e = html.escape
    def dollar(v): return f'${v:+,.2f}'
    def cls(v): return 'gain' if v > 0 else 'loss' if v < 0 else ''
    mode = result['mode']
    segments = ['earlier', 'later', 'all'] if mode == 'research' else ['prospective']
    rows = []
    for strategy in STRATEGIES:
        parts = [f'<td>{e(strategy)}</td>']
        for segment in segments:
            stat = result['periods'][segment][strategy]['stats']
            parts.append(f'<td class="{cls(stat["pnl"])}">{dollar(stat["pnl"])}</td>'
                         f'<td>{stat["trade_count"]}</td>'
                         f'<td>{stat["trade_days_pct"]:.0f}%</td>')
        rows.append('<tr>' + ''.join(parts) + '</tr>')
    headers = ''.join(f'<th colspan="3">{e(label.title())}</th>' for label in segments)
    subheaders = '<th>Net P/L</th><th>Trades</th><th>Trade days</th>'*len(segments)
    # The market condition snapshot is available only after 10:30 each day.
    curves = equity_svg(result['periods'][segments[-1]])
    grouped = strat_regime_stats({segments[-1]: result['periods'][segments[-1]]})
    by_regime = ''.join('<tr>'+f'<td>{e(row["strategy"])}</td><td>{e(row["regime"])}</td><td>{row["sessions"]}</td><td>{row["trade_count"]}</td><td class="{cls(row["pnl"])}">{dollar(row["pnl"])}</td>'+'</tr>' for row in grouped)
    regimes = ''.join(f'<tr><td>{e(k.replace("_", " ").title())}</td><td>{v}</td></tr>'
                      for k,v in sorted(result['regimes'].items()))
    diag_rows = []
    last_segment = segments[-1]
    for strategy in STRATEGIES:
        s = result['periods'][last_segment][strategy]
        x = s['stats']
        diag_rows.append('<tr>'+f'<td>{e(strategy)}</td><td>{x["no_signal_days"]}</td>'
                         f'<td>{x["blocked_regime"]}</td><td>{x["blocked_below_vwap"]}</td>'
                         f'<td>{x["blocked_volume"]}</td><td>{x["full_day_stop_exits"]}</td>'
                         f'<td>{x["profit_factor"]:.2f}</td>' if x['profit_factor'] is not None else
                         '<tr>'+f'<td>{e(strategy)}</td><td>{x["no_signal_days"]}</td>'
                         f'<td>{x["blocked_regime"]}</td><td>{x["blocked_below_vwap"]}</td>'
                         f'<td>{x["blocked_volume"]}</td><td>{x["full_day_stop_exits"]}</td><td>n/a</td>')
        diag_rows[-1] += '</tr>'
    missed = ''.join(f'<tr><td>{e(d)}</td><td>{e(reason)}</td></tr>'
                     for d,reason in sorted(result['rejected'].items())[:30]) or '<tr><td colspan="2">None</td></tr>'
    cost_rows = ''.join('<tr>'+f'<td>{e(strategy)}</td>'+
                        ''.join(f'<td class="{cls(by_cost[str(bp)])}">{dollar(by_cost[str(bp)])}</td>'
                                for bp in SENSITIVITY_BPS)+'</tr>'
                        for strategy, by_cost in result['costs'].items())
    warning = ('SYNTHETIC EXAMPLE. No genuine trading performance evidence.' if 'SYNTHETIC' in result['source'].upper()
               else 'IEX SINGLE-EXCHANGE data; historical simulation, not broker paper fills. Financial results are not verified against consolidated quotes.')
    phase = ('RESEARCH: familiar historical data. This is NOT a blind holdout.' if mode == 'research' else
             'PROSPECTIVE: rules frozen before 2026-11-16. Results only reflect archived full sessions on/after that date. No real or paper orders.')
    notes = ('Run again on a later completed trading day to add new sessions; nothing will appear on weekends or before a full session is available.'
             if mode == 'forward' and not result['days'] else
             'Figures are per-strategy independent $500 accounts; never sum them to represent one portfolio.')
    extra = (f'<section><h2>Later-period slippage stress</h2><p>These results are for exploratory filtered strategies only. '
             f'Not an optimization grid.</p><div class="scroll"><table><tr><th>Strategy</th><th>0 bps</th><th>3 bps</th><th>6 bps</th><th>10 bps</th></tr>{cost_rows}</table></div></section>'
             if mode == 'research' else '')
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TradeLab v0.3 — {e(mode.title())}</title><style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{background:#0b1322;color:#eef3fa;font:14px/1.55 system-ui,Segoe UI,sans-serif;margin:0}}
main{{max-width:1190px;margin:auto;padding:32px 20px 60px}}h1{{font-size:31px;letter-spacing:-.7px;margin:3px 0 0}}p{{color:#aec1d6}}.tag{{font-size:12px;letter-spacing:1.7px;color:#8da9d5;text-transform:uppercase}}section{{background:#142035;border:1px solid #2b3c55;border-radius:12px;margin:15px 0;padding:20px}}.alert{{padding:14px;background:#49331f;color:#ffe8b4;border:1px solid #926132;border-radius:10px}}
table{{border-collapse:collapse;min-width:600px;width:100%;font-variant-numeric:tabular-nums}}th,td{{padding:10px 13px;border-bottom:1px solid #35465d;text-align:left;white-space:nowrap}}th{{color:#aabfd5}}.scroll{{overflow:auto}}.gain{{color:#80dbaf}}.loss{{color:#ff9f99}}.muted{{color:#aebfd4;font-size:12px}}.chips{{display:flex;flex-wrap:wrap;gap:12px}}.chip{{background:#20334e;border-radius:9px;padding:13px;min-width:160px}}.chip b{{display:block;font-size:19px;color:white}}
</style></head><body><main><div class="tag">TRADELAB / Research version 0.3 / NO ORDERS</div><h1>{e(mode.title())} · SPY strategy laboratory</h1>
<p>{e(result['source'])} &middot; {e(result['first_day'] or 'No sessions')} through {e(result['last_day'] or 'No sessions')} &middot; {result['days']} full sessions</p>
<div class="alert"><b>{e(phase)}</b> {e(warning)}</div>
<section><div class="chips"><div class="chip">Days checked<b>{result['days']}</b></div><div class="chip">Starting equity<b>$500 fake</b></div><div class="chip">Forward new days<b>{result.get('new_sessions','—')}</b></div><div class="chip">No-trade comparator<b>$0.00</b></div></div><p>{e(notes)}</p></section>
<section><h2>Side-by-side outcomes</h2><p>Baseline vs predeclared conservative filters. A lower loss is not proof of a positive edge.</p><div class="scroll"><table><thead><tr><th rowspan="2">&nbsp;</th>{headers}</tr><tr>{subheaders}</tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>
<section><h2>Equity comparison, {e(last_segment)}</h2><p>End-of-day simulated account values. Four key approaches; all begin at $500 independently.</p>{curves}</section>
<section><h2>Performance by opening-hour regime</h2><p>Exploratory attribution, not a signal rule or evidence of profitable conditions. Days with zero trades still count.</p><div class="scroll"><table><tr><th>Strategy</th><th>Regime</th><th>Days</th><th>Trades</th><th>Net P/L</th></tr>{by_regime}</table></div></section>
<section><h2>What the opening hour looked like</h2><p>Market regimes are decided with the first 12 five-minute bars, no prices from the rest of the day.</p><table><tr><th>Regime</th><th>Sessions</th></tr>{regimes or '<tr><td colspan="2">No future sessions recorded</td></tr>'}</table></section>
<section><h2>Diagnostics, {e(last_segment)}</h2><div class="scroll"><table><tr><th>Strategy</th><th>No-signal days</th><th>Signals blocked: regime</th><th>Blocked: VWAP</th><th>Blocked: volume</th><th>Stops</th><th>Profit factor</th></tr>{''.join(diag_rows)}</table></div><p>Blocked signal counts are not necessarily unique trading days. Filter order is regime, then VWAP, then volume. Each strategy trades at most once per day.</p></section>
{extra}
<section><h2>Data screening</h2><p>{len(result['rejected'])} incomplete sessions excluded. Requiring 78 complete bars excludes valid early closes and may induce selection bias.</p><div class="scroll"><table><tr><th>Date</th><th>Reason</th></tr>{missed}</table></div></section>
<section><h2>Research constraints and important limitations</h2><p>All strategies are long-only, intraday, simulated fractional shares; maximum $250 notional, 50% of starting segment equity, 3 bps slippage per side unless otherwise indicated, no commissions, no real spread or impact, and no hard guaranteed loss cap. Base stops 0.5%, targets 1%; adaptive exit is bounded at 0.35–0.8% stop with target 1.5× stop; 90-minute variant exits at the open of the relevant later bar. If both stop and target touch in the same 5-minute candle, stop is assumed first. No margin, options, shorts, broker order routing or money movement.</p>
<p>Gate: only allow signals at/after 10:30 when first-hour range is below 0.75% and at least 0.25%, first-hour net direction exceeds 40% of that range, signal close is above approximate IEX intraday VWAP, and signal volume is at least 60% of the opening hour median. These thresholds are research hypotheses, not proven edges. The baseline strategies are kept for comparison. Opening-hour regime is only an approximate classification, not knowledge of the day's realized trend.</p>
<p>Only IEX data is used. The filtered price/volume and exits may not correspond to real-world fills. Regimes, signals and decisions are logged in per-day diagnostics CSV. Existing known historical data is for research; do not call it an untouched test. Prospective scores must be allowed to include losses without changing frozen rules.</p></section>
</main></body></html>'''


def export(result, prefix, open_browser=True):
    REPORTS.mkdir(parents=True, exist_ok=True)
    base = REPORTS / prefix
    summary = {'version':'0.3','mode':result['mode'],'source':result['source'],
               'sessions':result['days'],'new_sessions':result.get('new_sessions'),
               'first_day':result['first_day'],'last_day':result['last_day'],
               'regimes':result['regimes'],'exclusions':result['rejected'],
               'freeze_sha256':result.get('freeze_sha256'),
               'summary': {p:{s: x['stats'] for s,x in items.items()}
                           for p,items in result['periods'].items()}}
    (Path(str(base)+'_summary.json')).write_text(json.dumps(summary,indent=2),encoding='utf8')
    htmlpath = Path(str(base)+'_report.html')
    htmlpath.write_text(make_html(result),encoding='utf8')
    regime_csv = Path(str(base)+'_by_regime.csv')
    with regime_csv.open('w',newline='',encoding='utf8') as f:
        w = csv.DictWriter(f, fieldnames=['period','strategy','regime','sessions','trade_count','pnl','wins','win_rate_pct'])
        w.writeheader()
        w.writerows(strat_regime_stats(result['periods']))
    for suffix, fields, item in (('comparison',COMPARE_COLUMNS,'stats'),('trades',TRADES_COLUMNS,'trades'),
                                  ('diagnostics', DIAG_COLUMNS, 'diagnostics')):
        with Path(str(base)+'_'+suffix+'.csv').open('w',newline='',encoding='utf8') as f:
            w = csv.DictWriter(f,fieldnames=fields,extrasaction='ignore')
            w.writeheader()
            for period, group in result['periods'].items():
                for strategy, val in group.items():
                    if item == 'stats':
                        w.writerow({'period':period,'strategy':strategy,**val['stats']})
                    else:
                        w.writerows(val[item])
    print(f'\nTradeLab v0.3 - {result["mode"]}: {result["days"]} complete sessions')
    if result['mode'] == 'forward':
        print('New sessions appended:', result['new_sessions'])
    for s in STRATEGIES:
        period = 'later' if result['mode'] == 'research' else 'prospective'
        stats = result['periods'][period][s]['stats']
        print(f'  {s:29s} P/L ${stats["pnl"]:+7.2f} | Trades: {stats["trade_count"]}')
    print('Report:', htmlpath)
    print('Comparison:', str(base)+'_comparison.csv')
    print('Day-by-day diagnostics:', str(base)+'_diagnostics.csv')
    print('By-market-regime analysis:', regime_csv)
    if open_browser:
        webbrowser.open(htmlpath.resolve().as_uri())


def main(argv=None):
    parser = argparse.ArgumentParser(description='TradeLab 0.3; offline simulation and frozen prospective replay; NO orders')
    subs = parser.add_subparsers(dest='cmd',required=True)
    demo = subs.add_parser('demo',help='Synthetic data demonstration')
    demo.add_argument('--days',type=int,default=60)
    hist = subs.add_parser('backtest',help='Get real IEX historical data to research')
    hist.add_argument('--days',type=int,default=180)
    replay = subs.add_parser('replay',help='Research any local 5min OHLC CSV')
    replay.add_argument('csv_file',type=Path)
    forward_cmd = subs.add_parser('forward', help='Append full unseen sessions from 2026-11-16, with rules frozen')
    forward_cmd.add_argument('--days',type=int,default=240)
    for cmd in (demo,hist,replay,forward_cmd):
        cmd.add_argument('--no-open',action='store_true')
    args=parser.parse_args(argv)
    if args.cmd == 'demo':
        if not 20 <= args.days <= 500: parser.error('demo --days must be between 20 and 500')
        bars=market.generate_synthetic(args.days)
        market.write_csv(DATA/'synthetic_demo.csv',bars)
        result=research(bars,'SYNTHETIC OFFLINE DEMO')
        prefix='demo'
    elif args.cmd == 'replay':
        result=research(market.load_csv(args.csv_file),f'LOCAL CSV {args.csv_file.name}; check provenance')
        prefix='SPY_replay'
    else:
        if not 30 <= args.days <= 730: parser.error('--days must be between 30 and 730')
        # Protect forward mode *before* making an API request.
        if args.cmd == 'forward':
            manifest()
        bars=market.get_alpaca_bars('SPY',args.days,'iex')
        if not bars: raise RuntimeError('Alpaca returned zero historical IEX bars.')
        if args.cmd == 'backtest':
            market.write_csv(DATA/'SPY_alpaca_iex_5min.csv',bars)
            result=research(bars,f'ALPACA IEX SPY | {args.days} calendar days')
            prefix='SPY_historical'
        else:
            # No overwrite of stored research CSV or archived days.
            result=forward(bars,f'ALPACA IEX SPY | forward evaluation since {START_DATE}')
            prefix='SPY_forward'
    export(result,prefix,not args.no_open)
    return 0


if __name__=='__main__':
    try: sys.exit(main())
    except (RuntimeError, ValueError, OSError) as e:
        print('\nERROR:',e,file=sys.stderr)
        sys.exit(1)
