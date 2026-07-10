"""External account data sources — READ-ONLY.

This package lets the system SEE holdings held at other brokers (e.g. Robinhood)
so cross-broker exposure and correlation are visible to the risk warnings and the
analyst. It is deliberately OUTSIDE ``broker/`` so nothing can ever treat it as a
tradable venue.

NON-GOALS (hard, permanent): no order placement, no transfers, no account
modification, no watchlist writes — nothing that sends a mutating request to an
external broker, ever. The protocol has no write methods at all. If a future phase
wants external execution, that is a separate, explicit decision — this package
does not scaffold toward it.
"""
