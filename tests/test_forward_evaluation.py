from decimal import Decimal
from trading_assistant.ops.forward_evaluation import evaluate


def point(at, equity, benchmark, **extra):
    return dict(observed_at=at, equity=equity, benchmark='SPY', benchmark_last=benchmark,
                benchmark_at=at, source='alpaca_paper', currency='USD', gross_exposure='200', **extra)


def test_modified_dietz_and_benchmark_with_midpoint_deposit():
    rows = [point('2026-01-01T00:00:00+00:00', '1000', '100'),
            point('2026-01-03T00:00:00+00:00', '1600', '110',
                  activity_coverage='complete', activity_interval_start='2026-01-01T00:00:00+00:00', activities=[{'id':'deposit-1', 'type':'CSD', 'amount':'500', 'at':'2026-01-02T00:00:00+00:00'}])]
    result = evaluate(rows, benchmark='SPY')
    assert Decimal(result['flow_adjusted_return_pct']) == Decimal('8')
    assert Decimal(result['flow_adjusted_profit']) == Decimal('100')
    assert Decimal(result['benchmark_return_pct']) == Decimal('10')
    assert result['comparison_verified'] is True


def test_unknown_coverage_and_legacy_benchmark_are_unverified():
    rows = [point('2026-01-01T00:00:00+00:00', '1000', '100'),
            point('2026-01-02T00:00:00+00:00', '1100', '110')]
    del rows[1]['benchmark_at']
    result = evaluate(rows, benchmark='SPY')
    assert result['flow_adjusted_return_pct'] is None
    assert result['benchmark_return_pct'] is None
    assert result['comparison_verified'] is False


def test_drawdown_percentage_independent_of_dollar_maximum():
    rows = [point(f'2026-01-0{i}T00:00:00+00:00', e, '100') for i, e in enumerate(['100', '50', '1000', '800'], 1)]
    result = evaluate(rows, benchmark='SPY')
    assert Decimal(result['raw_max_drawdown']) == Decimal('200')
    assert Decimal(result['raw_max_drawdown_pct']) == Decimal('50')


def test_invalid_and_duplicate_rows_are_reported():
    a = point('2026-01-01T00:00:00+00:00', '1000', '100')
    b = point('2026-01-02T00:00:00+00:00', 'NaN', '100')
    result = evaluate([a, a.copy(), b], benchmark='SPY')
    assert result['excluded_rows'] == 2
    assert result['complete'] is False


def test_window_fill_counts_exclude_basis_fills(session_factory):
    from tests.test_paper_report import _seed, T0
    from datetime import timedelta
    from trading_assistant.ops.paper_report import build_report
    _seed(session_factory)
    with session_factory() as session:
        report = build_report(session, since=T0 + timedelta(minutes=30))
    assert report['fills']['trusted'] == 1
    assert report['pnl']['turnover'] == '103.00'
    assert report['pnl']['realized'] == '3.00'


def test_ambiguous_activity_invalidates_adjustment():
    rows = [point('2026-01-01T00:00:00+00:00', '1000', '100'),
            point('2026-01-02T00:00:00+00:00', '1100', '110', activity_coverage='complete',
                  activities=[{'type': 'UNKNOWN', 'amount': '100', 'at': '2026-01-02T00:00:00+00:00'}])]
    assert evaluate(rows, benchmark='SPY')['flow_adjusted_return_pct'] is None


def test_broker_economic_activity_read_is_bounded_and_sanitized():
    from datetime import datetime, timezone
    from trading_assistant.broker.alpaca import AlpacaBroker
    class Trading:
        def __init__(self): self.calls = []
        def get(self, path, params):
            self.calls.append((path, dict(params)))
            return [{'id': '20260102000000000::abc', 'activity_type': 'CSD',
                     'net_amount': '500', 'date': '2026-01-02', 'description': 'private'}]
    trading = Trading()
    result = AlpacaBroker(trading, None).get_economic_activities(
        datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 3, tzinfo=timezone.utc))
    assert trading.calls[0][0] == '/account/activities'
    assert result['coverage'] == 'incomplete'  # settlement date alone is not a flow timestamp
    assert 'private' not in str(result)


def test_recorder_captures_quote_time_and_activity_coverage(make_service):
    from datetime import timedelta
    from tests.test_equity_and_fill_quality import _service, T0
    from trading_assistant.ops.equity import EquitySnapshotRecorder, read_snapshots
    service, broker = _service(make_service)
    broker.get_economic_activities = lambda start, end: {'coverage': 'complete', 'activities': []}
    now = {'at': T0}
    recorder = EquitySnapshotRecorder(service, interval_seconds=1, now=lambda: now['at'])
    assert recorder.maybe_record()
    now['at'] += timedelta(minutes=1)
    assert recorder.maybe_record()
    first, second = read_snapshots(service.session_factory)
    assert first['activity_coverage'] == 'baseline'
    assert second['activity_coverage'] == 'complete'
    assert 'benchmark_at' in second


def test_interval_coverage_must_match_previous_observation():
    rows = [point('2026-01-01T00:00:00+00:00', '1000', '100'),
            point('2026-01-02T00:00:00+00:00', '1100', '110', activity_coverage='complete',
                  activity_interval_start='2025-12-31T00:00:00+00:00', activities=[])]
    assert evaluate(rows, benchmark='SPY')['flow_adjusted_return_pct'] is None


def test_pagination_repeated_token_fails_closed():
    from datetime import datetime, timezone
    import pytest
    from trading_assistant.broker.alpaca import AlpacaBroker
    class Trading:
        def get(self, path, params):
            return [{'id': f'{i}::id', 'activity_type': 'FILL'} for i in range(100)]
    with pytest.raises(ValueError):
        AlpacaBroker(Trading(), None).get_economic_activities(
            datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc))


def test_stale_or_mixed_benchmark_invalidates_comparison():
    first = point('2026-01-01T00:00:00+00:00', '1000', '100')
    last = point('2026-01-02T00:00:00+00:00', '1100', '110', activity_coverage='complete',
                 activity_interval_start='2026-01-01T00:00:00+00:00', activities=[])
    last['benchmark_at'] = '2026-01-01T23:00:00+00:00'
    assert evaluate([first, last], benchmark='SPY')['comparison_verified'] is False
    last['benchmark_at'] = last['observed_at']
    last['source'] = 'mock'
    assert evaluate([first, last], benchmark='SPY')['comparison_verified'] is False


def test_activity_identity_duplicates_invalidate_flow():
    activity = {'id': 'same', 'type': 'CSD', 'amount': '50', 'at': '2026-01-01T12:00:00+00:00'}
    rows = [point('2026-01-01T00:00:00+00:00', '1000', '100'),
            point('2026-01-02T00:00:00+00:00', '1100', '110', activity_coverage='complete',
                  activity_interval_start='2026-01-01T00:00:00+00:00', activities=[activity, activity])]
    assert evaluate(rows, benchmark='SPY')['flow_adjusted_return_pct'] is None


def test_empty_broker_activity_page_is_provisional_for_late_postings():
    from datetime import datetime, timezone
    from trading_assistant.broker.alpaca import AlpacaBroker
    class Trading:
        def get(self, path, params): return []
    result = AlpacaBroker(Trading(), None).get_economic_activities(
        datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert result['coverage'] == 'provisional'


def test_account_currency_is_taken_from_broker_response():
    from types import SimpleNamespace
    from trading_assistant.broker.alpaca import AlpacaBroker
    class Trading:
        def get_account(self):
            return SimpleNamespace(buying_power='100', equity='100', cash='100', currency='USD')
    assert AlpacaBroker(Trading(), None).get_account().currency == 'USD'


def test_adjusted_drawdown_is_expressed_in_starting_capital_units():
    first = point('2026-01-01T00:00:00+00:00', '1000', '100')
    second = point('2026-01-02T00:00:00+00:00', '900', '100', activity_coverage='complete',
                   activity_interval_start='2026-01-01T00:00:00+00:00', activities=[])
    result = evaluate([first, second], benchmark='SPY')
    assert Decimal(result['flow_adjusted_max_drawdown']) == Decimal('100')
    assert Decimal(result['flow_adjusted_max_drawdown_pct']) == Decimal('10')


def test_legacy_equity_statistics_discloses_unverified_quote_and_independent_drawdown():
    from trading_assistant.ops.equity import equity_statistics
    rows = [point(f'2026-01-0{i}T00:00:00+00:00', e, '100') for i, e in enumerate(['100', '50', '1000', '800'], 1)]
    stats = equity_statistics(rows)
    assert stats['max_equity_drawdown_pct'] == '50.000'
    assert stats['benchmark_verified'] is False
