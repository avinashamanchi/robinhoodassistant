"""LLM analyst: interprets MarketFeatures into a graded, cited AnalysisReport.

The analyst never computes indicators — it interprets the deterministic features.
Its calls are graded against realized forward returns and gated behind a
minimum-track-record requirement before any promotion toward live.
"""
