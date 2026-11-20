"""Conservative, read-only evaluation of dated account observations.

Activity coverage must attest to the entire preceding interval. Returns are
Modified Dietz estimates, not exact time-weighted returns.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation


def _time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('timestamp must include timezone')
    return parsed.astimezone(timezone.utc)


def _number(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('non-finite number')
    return number


def _drawdown(values):
    peak = values[0]
    absolute = percent = Decimal(0)
    for value in values:
        peak = max(peak, value)
        absolute = max(absolute, peak - value)
        if peak > 0:
            percent = max(percent, (peak - value) / peak * 100)
    return str(absolute), str(percent)


def evaluate(snapshots, *, benchmark, since=None, until=None, unreadable_rows=0):
    """Calculate performance from chronologically ordered, dated observations.

    ``activity_coverage`` on each endpoint covers (previous, endpoint]. Only
    CSD/CSW and explicitly classified income/fees are accepted. Missing or
    malformed evidence makes adjusted performance unavailable.
    """
    rows, excluded = [], unreadable_rows
    seen = set()
    for item in snapshots:
        try:
            at = _time(item['observed_at'])
            if since and at < since.astimezone(timezone.utc) or until and at >= until.astimezone(timezone.utc):
                continue
            equity = _number(item['equity'])
            if any(not isinstance(item.get(key), str) or not item[key].strip()
                   for key in ('source', 'currency')):
                raise ValueError('invalid source or currency')
            if equity <= 0 or at in seen:
                raise ValueError('invalid equity or duplicate timestamp')
            seen.add(at)
            rows.append((at, equity, item))
        except (ValueError, TypeError, KeyError, InvalidOperation):
            excluded += 1
    rows.sort(key=lambda row: row[0])
    result = {'snapshots': len(rows), 'excluded_rows': excluded, 'unreadable_rows': unreadable_rows,
              'invalid_rows': excluded - unreadable_rows, 'complete': False,
              'equity_start': None, 'equity_end': None, 'currency': None,
              'flow_adjusted_return_pct': None, 'flow_adjusted_profit': None,
              'flow_adjusted_max_drawdown': None, 'flow_adjusted_max_drawdown_pct': None,
              'flow_adjusted_drawdown_units': 'initial equity scaled by chained return index',
              'benchmark_return_pct': None, 'comparison_verified': False,
              'benchmark': benchmark, 'benchmark_method': 'buy and hold over the same observation window',
              'benchmark_status': 'unverified: missing, stale, mismatched, or legacy quote timestamp',
              'costs_status': 'account equity includes costs; activity coverage unverified',
              'method': 'Modified Dietz per interval, chained; estimate, not exact time-weighted return'}
    if rows:
        result['equity_start'] = str(rows[0][1])
        result['equity_end'] = str(rows[-1][1])
        currencies = {row[2]['currency'] for row in rows}
        result['currency'] = next(iter(currencies)) if len(currencies) == 1 else None
    if len(rows) < 2:
        return result
    result['activity_coverage'] = {state: sum(row[2].get('activity_coverage', 'unknown') == state for row in rows[1:])
                                   for state in ('complete', 'provisional', 'incomplete', 'unknown', 'baseline')}
    source = {r[2].get('source') for r in rows}
    currency = {r[2].get('currency') for r in rows}
    identities_valid = len(source) == len(currency) == 1 and None not in source and None not in currency
    result.update({'from': rows[0][0].isoformat(), 'to': rows[-1][0].isoformat(),
                   'largest_gap_hours': max((b[0]-a[0]).total_seconds() / 3600 for a, b in zip(rows, rows[1:])),
                   'raw_equity_change': str(rows[-1][1] - rows[0][1]),
                   'raw_return_pct': str((rows[-1][1] / rows[0][1] - 1) * 100)})
    result['raw_max_drawdown'], result['raw_max_drawdown_pct'] = _drawdown([r[1] for r in rows])
    exposures = []
    for _, equity, item in rows:
        try:
            exposure = _number(item['gross_exposure'])
            if exposure < 0:
                raise ValueError('negative exposure')
            exposures.append((exposure, exposure / equity * 100))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            pass
    result.update({'exposure_observations': len(exposures),
                   'peak_observed_gross_exposure': str(max((x[0] for x in exposures), default=0)) if exposures else None,
                   'peak_observed_gross_exposure_pct': str(max((x[1] for x in exposures), default=0)) if exposures else None})
    try:
        marks = []
        for at, _, item in rows:
            mark = _number(item['benchmark_last'])
            quote_at = _time(item['benchmark_at'])
            if item.get('benchmark') != benchmark or not timedelta(0) <= at - quote_at <= timedelta(minutes=15) or quote_at.date() != at.date() or mark <= 0:
                raise ValueError('benchmark mismatch or stale mark')
            marks.append(mark)
        benchmark_ok = identities_valid and not excluded
        if benchmark_ok:
            result['benchmark_status'] = 'dated marks matched to observation window'
            result['benchmark_return_pct'] = str((marks[-1] / marks[0] - 1) * 100)
    except (KeyError, ValueError, TypeError, InvalidOperation):
        benchmark_ok = False
    try:
        index, profit, curve = Decimal(1), Decimal(0), [Decimal(1)]
        activity_ids = set()
        for previous, current in zip(rows, rows[1:]):
            start, opening, _ = previous
            end, closing, item = current
            if (item.get('activity_coverage') != 'complete' or
                _time(item['activity_interval_start']) != start or
                not isinstance(item.get('activities'), list)):
                raise ValueError('incomplete activity coverage')
            flow = weighted = Decimal(0)
            for activity in item['activities']:
                ident = activity['id']
                if not isinstance(ident, str) or not ident or ident in activity_ids:
                    raise ValueError('missing or duplicate activity identity')
                activity_ids.add(ident)
                at, amount, kind = _time(activity['at']), _number(activity['amount']), activity['type']
                if not start < at <= end:
                    raise ValueError('activity outside interval')
                if kind in ('CSD', 'CSW'):
                    if kind == 'CSD' and amount < 0 or kind == 'CSW' and amount > 0:
                        raise ValueError('invalid transfer sign')
                    flow += amount
                    weighted += amount * Decimal(str((end-at).total_seconds())) / Decimal(str((end-start).total_seconds()))
                elif kind not in ('income', 'fee'):
                    raise ValueError('ambiguous activity')
            denominator = opening + weighted
            if denominator <= 0:
                raise ValueError('invalid Dietz denominator')
            interval_profit = closing - opening - flow
            profit += interval_profit
            index *= 1 + interval_profit / denominator
            if index <= 0:
                raise ValueError('invalid chained return')
            curve.append(index)
        if not identities_valid or excluded:
            raise ValueError('incomplete observations')
        result['flow_adjusted_return_pct'] = str((index - 1) * 100)
        result['flow_adjusted_profit'] = str(profit)
        index_drawdown, result['flow_adjusted_max_drawdown_pct'] = _drawdown(curve)
        result['flow_adjusted_max_drawdown'] = str(Decimal(index_drawdown) * rows[0][1])
        result['costs_status'] = 'reported activity fees are included in equity and not subtracted again; independent fill and borrow cost coverage is not established'
        flow_ok = True
    except (KeyError, ValueError, TypeError, InvalidOperation, ZeroDivisionError):
        flow_ok = False
    result['comparison_verified'] = benchmark_ok and flow_ok
    result['complete'] = result['comparison_verified'] and len(exposures) == len(rows)
    return result
