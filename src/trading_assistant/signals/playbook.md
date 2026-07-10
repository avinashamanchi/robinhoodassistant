# Trading Playbook (analyst knowledge)

**These are heuristics with mixed, contested evidence — not laws.** The academic
record on technical patterns is genuinely uncertain: some effects show up in some
markets and regimes and vanish in others. Treat every rule below as a prior to be
weighed against the specific situation, never as a guarantee. When you form a
thesis you MUST name which concepts here drove it, and you must state the current
regime and how it conditioned your read.

## Trend (SMA/EMA/MACD/ADX)

- Moving-average stacks (price > 50 > 200) describe an uptrend; MACD line above
  signal adds momentum confirmation; ADX > 25 says the trend has strength.
- **Fails when:** the market is ranging — moving-average crossovers whipsaw and
  generate losing signal after losing signal. High ADX can also mark a *blow-off*
  right before exhaustion.

## Momentum (RSI, ROC)

- RSI < 30 is "oversold," RSI > 70 "overbought." Bullish divergence (price makes a
  lower low, RSI a higher low) can precede a bounce.
- **Fails when:** in a strong downtrend, oversold stays oversold — RSI can sit
  under 30 for weeks while price keeps falling. Do not buy oversold in
  TRENDING_DOWN on RSI alone.

## Volatility (ATR, Bollinger Bands, realized vol)

- ATR sizes stops to current noise. A Bollinger squeeze (bandwidth in its bottom
  decile) signals coiled volatility and often precedes a large move.
- **Fails when:** a squeeze tells you a move is coming but NOT its direction —
  breakouts from squeezes fake out routinely. Rising realized vol widens the range
  of outcomes both ways.

## Volume (relative volume)

- A move on above-average volume carries more conviction; breakouts want > 1.5×
  average volume to be trusted.
- **Fails when:** low-volume breakouts frequently reverse. Volume can also spike on
  capitulation, marking a bottom rather than continuation.

## Structure (support/resistance, 52-week, gaps)

- Prior swing highs/lows act as support/resistance; distance to the 52-week high/low
  frames where price sits in its range. Gaps mark discontinuities that sometimes
  fill.
- **Fails when:** levels break decisively on a regime shift — old support becomes
  resistance and "buying the dip" at a broken level is how downtrends bleed you.

## Regime conditioning (required)

- **TRENDING_UP:** favor trend-following; buy pullbacks, respect the trend.
- **TRENDING_DOWN:** favor caution/exits; mean-reversion longs are dangerous.
- **RANGING:** favor mean-reversion (fade extremes); trend signals whipsaw.
- **HIGH_VOLATILITY:** reduce size; stops need more room; conviction should be
  higher to act at all.

State the regime and how it shaped your thesis — a signal that works in one regime
misleads in another.

## Position management

- Scale into pullbacks within an uptrend rather than chasing extension.
- Tighten stops after a parabolic move; mean reversion after a vertical move is common.
- Size to volatility (ATR), not to conviction alone.

## Earnings handling (required when relevant)

If an earnings date falls inside your holding horizon (`days_to_next_earnings`),
you MUST explicitly choose: reduce the position, exit before the print, or
knowingly accept the gap risk with a reason. **Silence is not allowed** — holding
through earnings unaddressed is a discrete, un-modeled risk.

## Correlation (required when relevant)

Multiple positions in highly correlated names (e.g. several mega-cap tech longs) are
effectively ONE bet, not several. When your view adds to existing correlated
exposure, flag it — it concentrates risk even though it looks diversified.

## Uncertainty & citation

Every thesis must (1) cite the concepts above that drove it, (2) state the regime,
(3) address earnings and correlation if relevant, and (4) express calibrated
confidence. If the signals conflict or the regime is unclear, HOLD is a valid,
often correct, answer.
