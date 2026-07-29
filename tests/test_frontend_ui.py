"""Static contracts for the local Trading Assistant operator interface."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import re

import pytest


STATIC = Path("src/trading_assistant/app/static")
PAGES = ("index.html", "plans.html", "backtests.html", "login.html")
AUTHENTICATED_PAGES = ("index.html", "plans.html", "backtests.html")

APPROVED_TOKENS = {
    "canvas": "#0b0914",
    "surface": "#101114",
    "surface-raised": "#171420",
    "surface-interactive": "#201a2e",
    "border": "#302941",
    "brand": "#7132f5",
    "brand-hover": "#5741d8",
    "brand-deep": "#5b1ecf",
    "brand-wash": "rgba(133, 91, 251, 0.16)",
    "text": "#f7f4ff",
    "text-muted": "#9497a9",
    "verified": "#2bc48a",
    "caution": "#f0b45d",
    "danger": "#ff647c",
}

BEHAVIOR_HOOKS = {
    "index.html": {
        "main-content",
        "session-actor",
        "sign-out",
        "critical-banner",
        "proof-broker",
        "proof-market",
        "proof-data",
        "proof-daemon",
        "proof-reconciliation",
        "proof-safety",
        "pending-list",
        "positions",
        "holdings",
        "receipt-panel",
        "chat-form",
        "chat-log",
        "risk-log",
        "breaker-reset-form",
        "panic-dialog",
        "approval-dialog",
        "reauth-dialog",
        "status-region",
    },
    "plans.html": {
        "main-content",
        "session-actor",
        "sign-out",
        "analysis-form",
        "proposal-form",
        "screen-button",
        "screen-results",
        "plans-list",
        "plan-detail",
        "plan-approval-dialog",
        "plan-cancel-dialog",
        "reauth-dialog",
        "status-region",
    },
    "backtests.html": {
        "main-content",
        "session-actor",
        "sign-out",
        "backtest-form",
        "backtest-runs",
        "backtest-report",
        "refresh-runs",
        "reauth-dialog",
        "status-region",
    },
    "login.html": {"main-content", "login-form", "login-secret", "login-status"},
}


class PageContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.labels: set[str] = set()
        self.controls: list[tuple[str, str]] = []
        self.main_count = 0
        self.h1_count = 0
        self.inline_scripts = 0
        self.inline_styles = 0
        self.stylesheets: list[str] = []
        self.scripts: list[str] = []
        self.skip_targets: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.ids.add(element_id)
        if tag == "main":
            self.main_count += 1
        if tag == "h1":
            self.h1_count += 1
        if tag == "label" and attributes.get("for"):
            self.labels.add(attributes["for"])
        if tag in {"input", "select", "textarea"}:
            if attributes.get("type") != "hidden":
                self.controls.append((tag, element_id or ""))
        if tag == "script":
            source = attributes.get("src")
            if source:
                self.scripts.append(source)
            else:
                self.inline_scripts += 1
        if tag == "style" or "style" in attributes:
            self.inline_styles += 1
        if tag == "link" and attributes.get("rel") == "stylesheet":
            self.stylesheets.append(attributes.get("href", ""))
        if tag == "a" and "skip-link" in attributes.get("class", "").split():
            self.skip_targets.append(attributes.get("href", ""))
        if any(name.lower().startswith("on") for name, _ in attrs):
            self.inline_scripts += 1


def static_text(relative: str) -> str:
    return (STATIC / relative).read_text(encoding="utf-8")


def parse_page(page: str) -> PageContractParser:
    parser = PageContractParser()
    parser.feed(static_text(page))
    return parser


def test_console_declares_the_approved_dark_tokens_and_accessibility_modes():
    """Removing the approved state palette or accessibility modes breaks the UI contract."""
    css = static_text("css/console.css")

    for token, value in APPROVED_TOKENS.items():
        assert f"--{token}: {value};" in css
    assert "color-scheme: dark;" in css
    assert "min-height: 44px;" in css
    assert "0 0 0 3px rgba(113, 50, 245, .35)" in css
    assert "@media (min-width: 1180px)" in css
    assert "@media (min-width: 760px) and (max-width: 1179px)" in css
    assert "@media (max-width: 759px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "@media (forced-colors: active)" in css


@pytest.mark.parametrize("page", PAGES)
def test_every_page_uses_the_original_local_identity_and_explicit_paper_mode(page):
    """A shared page cannot regress to an exchange identity or ambiguous trading mode."""
    html = static_text(page)

    assert "/static/img/trading-orbit.svg" in html
    assert "Trading Assistant" in html
    assert "ALPACA PAPER" in html
    assert "kraken" not in html.lower()
    assert "flight-deck.svg" not in html
    assert not re.search(r"(?:src|href)=[\"']https?://", html)


@pytest.mark.parametrize("page", PAGES)
def test_every_page_has_one_named_main_and_one_page_heading(page):
    """Keyboard and screen-reader navigation must have one unambiguous page target."""
    parsed = parse_page(page)

    assert parsed.main_count == 1
    assert parsed.h1_count == 1
    assert parsed.skip_targets == ["#main-content"]
    assert "main-content" in parsed.ids
    assert parsed.inline_scripts == 0
    assert parsed.inline_styles == 0
    assert parsed.stylesheets == ["/static/css/console.css"]
    assert len(parsed.scripts) == 1
    assert parsed.scripts[0].startswith("/static/js/")


@pytest.mark.parametrize("page", PAGES)
def test_every_form_control_keeps_an_explicit_label(page):
    """Adding an unlabeled operator input must fail before it reaches the console."""
    parsed = parse_page(page)

    for tag, element_id in parsed.controls:
        assert element_id, f"{page}: {tag} is missing an id"
        assert element_id in parsed.labels, f"{page}: #{element_id} is unlabeled"


@pytest.mark.parametrize("page", AUTHENTICATED_PAGES)
def test_authenticated_pages_share_operations_navigation_and_truth_strip(page):
    """Authenticated workspaces must keep the operator oriented to paper truth."""
    html = static_text(page)

    assert 'class="topbar"' in html
    assert 'class="environment-strip"' in html
    assert ">Operations<" in html
    assert ">Plans<" in html
    assert ">Backtests<" in html
    for label in ("Breaker", "Daemon", "Reconciliation", "Observed"):
        assert label in html


@pytest.mark.parametrize(("page", "required_ids"), BEHAVIOR_HOOKS.items())
def test_visual_rework_preserves_existing_behavior_hooks(page, required_ids):
    """Removing a JavaScript or dialog hook during the reskin is a behavior regression."""
    parsed = parse_page(page)

    assert required_ids <= parsed.ids


def test_shared_components_define_distinct_non_optimistic_states():
    """Unknown and stale states need explicit classes independent of verified state."""
    css = static_text("css/console.css")

    for state in (
        "is-loading",
        "is-empty",
        "is-verified",
        "is-caution",
        "is-blocked",
        "is-unknown",
        "is-stale",
        "has-error",
    ):
        assert f".{state}" in css
    assert "font-variant-numeric: tabular-nums;" in css
    for component in (
        "app-shell",
        "topbar",
        "environment-strip",
        "side-nav",
        "panel",
        "metric",
        "status-chip",
        "data-table",
        "button",
        "dialog",
        "skeleton",
        "empty-state",
    ):
        assert f".{component}" in css
