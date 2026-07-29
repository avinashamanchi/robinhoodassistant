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
        "environment-mode",
        "environment-breaker",
        "environment-daemon",
        "environment-reconciliation",
        "environment-observed",
        "environment-quote",
        "security-posture-panel",
        "security-posture-list",
        "security-posture-observed",
        "provider-budget-calls",
        "provider-budget-input",
        "provider-budget-output",
        "provider-budget-reset",
        "pending-list",
        "positions",
        "holdings",
        "receipt-panel",
        "chat-form",
        "chat-log",
        "chat-submit",
        "chat-budget-state",
        "assistant-candidates",
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
        "plan-actions-pane",
        "plan-queue-pane",
        "plan-evidence-pane",
        "research-paper-badge",
        "plan-filter",
        "plans-budget-state",
        "plans-budget-calls",
        "plans-budget-input",
        "plans-budget-output",
        "plans-budget-reset",
        "refresh-plan-budget",
        "analysis-form",
        "analysis-submit",
        "proposal-form",
        "proposal-submit",
        "screen-button",
        "screen-results",
        "plans-list",
        "plan-detail",
        "plan-approval-dialog",
        "plan-cancel-dialog",
        "plan-cancel-target-id",
        "plan-cancel-target-symbol",
        "plan-cancel-target-action",
        "reauth-dialog",
        "status-region",
    },
    "backtests.html": {
        "main-content",
        "session-actor",
        "sign-out",
        "backtest-policy-pane",
        "backtest-runtime-limit",
        "backtest-symbol-limit",
        "backtest-range-limit",
        "backtest-provider-mode",
        "backtest-run-budget",
        "backtest-active-state",
        "backtest-form",
        "backtest-symbols",
        "backtest-start-date",
        "backtest-end-date",
        "backtest-submit",
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
        self.attributes_by_id: dict[str, dict[str, str | None]] = {}
        self.labels: set[str] = set()
        self.controls: list[tuple[str, str]] = []
        self.headings: list[int] = []
        self.landmarks: list[tuple[str, dict[str, str | None]]] = []
        self.dialogs: list[dict[str, str | None]] = []
        self.live_regions: list[tuple[str, str]] = []
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
            self.attributes_by_id[element_id] = attributes
        if tag == "main":
            self.main_count += 1
        if tag == "h1":
            self.h1_count += 1
        if re.fullmatch(r"h[1-6]", tag):
            self.headings.append(int(tag[1]))
        if tag in {"main", "nav", "aside"}:
            self.landmarks.append((tag, attributes))
        if tag == "dialog":
            self.dialogs.append(attributes)
        if attributes.get("aria-live"):
            self.live_regions.append(
                (element_id or "", attributes["aria-live"] or "")
            )
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


def test_shared_css_and_original_mark_have_no_remote_asset_escape():
    """CSS imports, font/image URLs, and SVG links must remain self-hosted."""
    css = static_text("css/console.css")
    mark = static_text("img/trading-orbit.svg")

    assert "@import" not in css
    assert not re.search(r"url\(\s*[\"']?https?://", css, re.IGNORECASE)
    assert not re.search(
        r"(?:href|xlink:href)\s*=\s*[\"']https?://",
        mark,
        re.IGNORECASE,
    )


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


@pytest.mark.parametrize("page", PAGES)
def test_accessibility_regions_are_named_and_headings_do_not_skip_levels(page):
    """Removing a landmark name, dialog description, or heading level breaks navigation."""
    parsed = parse_page(page)

    assert parsed.headings
    assert parsed.headings[0] == 1
    for previous, current in zip(parsed.headings, parsed.headings[1:]):
        assert current <= previous + 1, (
            f"{page}: heading level jumps from h{previous} to h{current}"
        )
    for tag, attributes in parsed.landmarks:
        assert (
            attributes.get("aria-label")
            or attributes.get("aria-labelledby")
        ), f"{page}: {tag} landmark has no accessible name"
    for attributes in parsed.dialogs:
        assert attributes.get("aria-labelledby"), (
            f"{page}: dialog has no title association"
        )
        description_id = attributes.get("aria-describedby")
        assert description_id, f"{page}: dialog has no description association"
        assert description_id in parsed.ids, (
            f"{page}: dialog description #{description_id} is missing"
        )


@pytest.mark.parametrize("page", PAGES)
def test_accessibility_live_regions_are_bounded_status_surfaces(page):
    """Arbitrary dynamic containers must not become noisy announcement regions."""
    parsed = parse_page(page)
    allowed = {"chat-log", "status-region", "login-status"}

    for element_id, politeness in parsed.live_regions:
        assert element_id in allowed, (
            f"{page}: #{element_id or '<anonymous>'} is an unbounded live region"
        )
        assert politeness == "polite"


def test_login_accessibility_names_https_paper_boundary_and_one_secret():
    """The entry page must state its transport/account boundary without secret setup hints."""
    html = static_text("login.html")
    parsed = parse_page("login.html")
    password_controls = [
        element_id
        for tag, element_id in parsed.controls
        if (
            tag == "input"
            and parsed.attributes_by_id[element_id].get("type") == "password"
        )
    ]

    assert "Local HTTPS operator console" in html
    assert "Alpaca paper only" in html
    assert password_controls == ["login-secret"]
    described_by = (
        parsed.attributes_by_id["login-secret"]
        .get("aria-describedby", "")
        .split()
    )
    assert {"login-secret-help", "login-status"} <= set(described_by)
    assert "never stored by this page" in html
    assert ".env" not in html
    assert "token" not in html.lower()


@pytest.mark.parametrize("page", AUTHENTICATED_PAGES)
def test_reauthentication_secret_has_help_and_error_associations(page):
    """Recent-auth errors must be announced in the context of the secret field."""
    parsed = parse_page(page)
    described_by = (
        parsed.attributes_by_id["reauth-secret"]
        .get("aria-describedby", "")
        .split()
    )

    assert {"reauth-description", "reauth-status"} <= set(described_by)


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


def test_operations_console_exposes_the_proof_decision_hierarchy():
    """Removing a proof, budget, or signed-candidate region hides operator authority."""
    html = static_text("index.html")
    parsed = parse_page("index.html")
    required = {
        "environment-mode",
        "environment-breaker",
        "environment-daemon",
        "environment-reconciliation",
        "environment-observed",
        "environment-quote",
        "critical-banner",
        "security-posture-panel",
        "security-posture-list",
        "security-posture-observed",
        "provider-budget-calls",
        "provider-budget-input",
        "provider-budget-output",
        "provider-budget-reset",
        "pending-list",
        "positions",
        "holdings",
        "receipt-panel",
        "chat-form",
        "chat-log",
        "chat-submit",
        "chat-budget-state",
        "assistant-candidates",
        "risk-log",
        "breaker-reset-button",
        "panic-open",
        "approval-dialog",
        "rejection-dialog",
        "panic-dialog",
    }

    assert required <= parsed.ids
    assert re.search(
        r'id="environment-mode"[^>]*>ALPACA PAPER<',
        html,
    )
    assert "Candidates never queue themselves." in html
    assert "Queueing creates a proposal or rule only." in html
    assert "Approval later reruns risk." in html
    assert "Approval always performs a fresh execution-time risk check." in html
    assert 'id="assistant-form"' not in html
    assert 'id="assistant-messages"' not in html
    assert 'id="execution-log"' not in html


def test_operations_priority_order_keeps_decisions_before_research():
    """Mobile source order must keep blockers and decisions ahead of model research."""
    html = static_text("index.html")
    ordered_ids = (
        "console-title",
        "critical-banner",
        "account-equity",
        "pending-list",
        "positions",
        "assistant-candidates",
        "risk-log",
    )
    offsets = [html.index(f'id="{element_id}"') for element_id in ordered_ids]

    assert offsets == sorted(offsets)
    assert 'class="operations-layout"' in html
    assert 'class="primary-workspace"' in html
    assert 'class="proof-rail"' in html


def test_pending_renderer_keeps_only_available_order_facts_and_fresh_risk_copy():
    """Pending cards must retain server fields without inventing absent provenance."""
    script = static_text("js/index.js")

    for field in (
        "ticker",
        "side",
        "qty",
        "notional",
        "order_type",
        "limit_price",
        "expires_at",
    ):
        assert f"order.{field}" in script
    assert "Fresh risk check occurs after approval." in script
    assert 'api("/positions"' not in script


def test_operations_layout_moves_the_proof_rail_without_shrinking_actions():
    """Compact/mobile CSS must move proof below decisions and retain touch sizing."""
    css = static_text("css/console.css")

    assert ".operations-layout" in css
    assert ".primary-workspace" in css
    assert ".proof-rail" in css
    assert "grid-template-areas:" in css
    assert "min-height: 44px;" in css


def test_responsive_console_preserves_touch_targets_and_page_width():
    """Mobile layout cannot shrink primary targets or hide page-level overflow bugs."""
    css = static_text("css/console.css")
    nav_start = css.index(".primary-nav a,")
    nav_rule = css[nav_start:css.index("}", nav_start)]
    mobile_start = css.index("@media (max-width: 759px)")
    mobile_rule = css[mobile_start:css.index(
        "/* 11. Reduced motion",
        mobile_start,
    )]

    assert "min-height: 44px;" in nav_rule
    assert "overflow-x: clip;" in css
    assert ".environment-strip" in mobile_rule
    assert "position: sticky;" in mobile_rule
    assert "top: 0;" in mobile_rule


def test_dynamic_tables_use_a_visible_horizontal_scroll_cue():
    """Data tables must scroll inside a named wrapper instead of clipping at mobile width."""
    for script in ("js/index.js", "js/plans.js", "js/backtests.js"):
        source = static_text(script)
        assert '"table-wrap table-scroll-cue"' in source

    css = static_text("css/console.css")
    assert ".table-scroll-cue::before" in css
    assert "Scroll horizontally" in css


def test_print_output_keeps_paper_and_simulation_labels_while_hiding_controls():
    """Printed evidence must remain unmistakably paper-only and simulated where applicable."""
    css = static_text("css/console.css")
    print_start = css.index("@media print")
    print_rule = css[print_start:]

    assert "ALPACA PAPER ONLY" in print_rule
    assert "SIMULATED" in print_rule
    assert ".primary-nav" in print_rule
    assert ".dialog-actions" in print_rule
    assert "display: none !important;" in print_rule


def test_plans_page_exposes_a_three_pane_paper_research_workspace():
    """Plan research must separate paid actions, saved summaries, and persisted evidence."""
    html = static_text("plans.html")
    parsed = parse_page("plans.html")
    ordered_ids = (
        "plan-actions-pane",
        "plan-queue-pane",
        "plan-evidence-pane",
    )
    offsets = [html.index(f'id="{element_id}"') for element_id in ordered_ids]

    assert offsets == sorted(offsets)
    assert 'class="plans-workspace"' in html
    assert "Research / paper-only" in html
    assert {
        "plans-budget-state",
        "plans-budget-calls",
        "plans-budget-input",
        "plans-budget-output",
        "plans-budget-reset",
        "analysis-submit",
        "proposal-submit",
        "screen-button",
        "refresh-plans",
    } <= parsed.ids
    assert "Regime context is available in selected detail." in html


def test_plans_approval_copy_preserves_the_exact_paper_safety_boundary():
    """Approval copy cannot imply execution certainty, profitability, or live authority."""
    html = static_text("plans.html")
    visible_copy = " ".join(html.split())

    assert "Approve exact paper plan" in visible_copy
    assert (
        "may create an ALPACA PAPER bracket or arm paper-only rules"
        in visible_copy
    )
    assert "does not prove profitability" in visible_copy
    assert "remains subject to execution and risk gates" in visible_copy
    for forbidden in ("AI pick", "winner", "guaranteed"):
        assert forbidden.lower() not in html.lower()


def test_plan_cancellation_dialog_names_the_immutable_target():
    """A destructive plan decision must show the same identity bound in JavaScript."""
    html = static_text("plans.html")

    for label, target_id in (
        ("Plan ID", "plan-cancel-target-id"),
        ("Symbol", "plan-cancel-target-symbol"),
        ("Action", "plan-cancel-target-action"),
    ):
        assert label in html
        assert f'id="{target_id}"' in html


def test_plans_css_stacks_the_evidence_workspace_at_compact_widths():
    """The three panes must become one readable flow below the desktop breakpoint."""
    css = static_text("css/console.css")

    assert ".plans-workspace" in css
    assert ".plan-actions-pane" in css
    assert ".plan-queue-pane" in css
    assert ".plan-evidence-pane" in css
    assert re.search(
        r"@media \(min-width: 1180px\).*?\.plans-workspace\s*\{"
        r".*?grid-template-columns:",
        css,
        re.DOTALL,
    )
    assert re.search(
        r"@media \(min-width: 760px\) and \(max-width: 1179px\).*?"
        r"\.plans-workspace\s*\{.*?grid-template-columns:\s*minmax\(0,\s*1fr\)",
        css,
        re.DOTALL,
    )


def test_backtests_page_keeps_simulation_warning_at_every_decision_surface():
    """Run controls, empty reports, and chart regions cannot lose the disclaimer."""
    html = static_text("backtests.html")

    warning = (
        "Simulated — past performance does not predict future results."
    )
    assert html.count(warning) >= 3
    assert 'id="backtest-policy-pane"' in html
    assert 'id="backtest-form"' in html
    assert 'id="backtest-report"' in html
    assert "No broker orders are placed." in html
    assert "Development" in html
    assert "Validation" in html
    assert "Final holdout" in html


def test_backtest_renderer_uses_local_accessible_svg_and_truthful_sections():
    """Charts must be DOM/SVG evidence and missing phases must remain explicit."""
    script = static_text("js/backtests.js")

    assert "document.createElementNS" in script
    assert "Number.isFinite" in script
    assert "strategy_equity" in script
    assert "benchmark_equity" in script
    assert "strategy_drawdown" in script
    assert "benchmark_drawdown" in script
    assert "Holdout access" in script
    assert "Validation not run" in script
    assert "Historical episodes not run" in script
    assert "not_persisted_for_legacy_run" in script
    assert "artifact_invalid" in script
    assert "backtest_busy" in script
    assert "backtest_timed_out" in script
    assert "backtest_bounds_exceeded" in script
    assert "createElement(\"canvas\")" not in script
    assert "innerHTML" not in script
    assert "insertAdjacentHTML" not in script
