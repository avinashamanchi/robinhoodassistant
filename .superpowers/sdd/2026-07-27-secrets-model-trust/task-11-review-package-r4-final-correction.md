# Task 11 Final Bounded Role-Visibility Correction Review Package

> Superseded in part by the Whole-Plan-2 integration correction. Production
> call tracing proved MCP, paper-drill, and safety-drill do not consume the
> quarantine summarizer; their selected-provider news visibility was removed
> as excess authority. The database-only watchdog, safety-drill Alpaca paper,
> preflight live-confirmation, and separate required/visible-map findings
> remain valid. See `task-11-review-package-plan2-integration.md`.

## Review boundary

- Base: `35eee00d349be9e55624522692f0f263f3c827b5`
- Implementation:
  `b6cee46bbddc3cae147c1cdaa9b3f970a96d6dbb`
- Bounded diff:
  `.superpowers/sdd/2026-07-27-secrets-model-trust/review-35eee00..b6cee46.diff`
- Diff size: 595 lines / 17,679 bytes
- Implementation scope:
  `src/trading_assistant/security/secrets.py` and
  `tests/test_task11_round4.py`
- Evidence-only scope: this package, bounded diff, corrected Task 11
  brief/report/plan, round-4 supersession notice, and progress ledger
- Excluded: Plan 3, push, service startup, ignored runtime database, real
  Keychain/credentials, network, broker/provider/notifier/integration calls,
  trading, reconciliation writes, notification, and breaker reset

This package supersedes only the round-4 statement that exact
startup-required projections were a complete model of optional
branch-consumed visibility. It does not supersede the database-only watchdog
proof or any unrelated round-4 finding.

## Receiving-review disposition

1. **Watchdog-only database retrieval — already protected.** The exact
   standalone fake-Keychain test passed before implementation. It records
   provider account access, verifies only `database_url` was requested and
   returned, verifies no field-encryption keys were returned, and verifies
   every unrelated simple-secret field remained empty. The request for a new
   RED watchdog proof was therefore invalid for this checkout; the existing
   hermetic counterexample was retained.
2. **Safety-drill Alpaca paper branch — confirmed.** The optional branch
   consumes Alpaca paper credentials, but the round-4 required-only projection
   removed them before the branch could inspect them.
3. **Preflight live-confirmation visibility — confirmed.** Preflight consumes
   `live_trading_confirm` in its dangerous-switch check. Projecting it away
   could turn a populated switch into a false all-disabled result.
4. **Complete role-visible authority — confirmed.** Required-at-startup and
   visible-to-an-optional-branch are distinct policies. They now have separate
   immutable canonical maps with an explicit entry for every production role.

## Exact role audit

The role-visible map is a maximum allowlist. Configuration narrows it before
provider retrieval:

| Role | Visible capability beyond base required fields |
| --- | --- |
| app | configured LLM, feature-enabled Telegram pair, key material, live confirmation |
| backup | key material |
| daemon | configured LLM, feature-enabled Telegram pair, key material, live confirmation |
| mcp | key material; configured LLM only when news is enabled |
| migration | key material |
| paper-drill | key material; configured LLM only when news is enabled |
| preflight | configured LLM, feature-enabled Telegram pair, key material, live confirmation |
| safety-drill | optional Alpaca paper pair, key material, live confirmation; configured LLM only when news is enabled |
| validate-analyst | configured LLM and key material |
| watchdog | none; exactly `database_url` |

Here, key material means candidate-signing, backup-encryption, and only the
configured field-encryption key IDs consumed by the existing startup
key-material validation. That validation remains unchanged. In particular,
safety-drill's Alpaca pair remains optional at startup.

Only the selected supported LLM provider field is retrieved. Unselected LLM
fields are not requested. Telegram fields are retrieved only when the feature
is enabled for an authorized role. There is no unknown-role or broad
all-fields fallback. The resolver also proves that startup-required fields
are a subset of visible authority and fails closed with `authority_mismatch`
if the two authorities diverge.

## Exact RED and counterexample evidence

The pre-implementation selection was:

```text
uv run pytest -q \
  tests/test_task11_round4.py::test_watchdog_keychain_load_requests_and_receives_only_database_url \
  tests/test_task11_round4.py::test_other_keychain_roles_request_and_receive_exact_visible_secrets \
  tests/test_task11_round4.py::test_safety_drill_retains_optional_alpaca_paper_branch_credentials \
  tests/test_task11_round4.py::test_preflight_live_confirmation_cannot_be_projected_to_all_disabled \
  tests/test_task11_round4.py::test_news_branch_roles_receive_only_the_selected_optional_llm_secret

9 failed, 6 passed
```

The standalone watchdog test was one of the six passes. The nine failing
cases were:

- four all-role matrix cases: app, daemon, preflight, and safety-drill;
- the direct safety-drill Alpaca consumer;
- the direct preflight live-confirmation consumer;
- three selected news-provider cases: MCP, paper-drill, and safety-drill.

After implementation the same 15 cases passed. An additional characterization
then proved that empty safety-drill Alpaca credentials are accepted at normal
startup, preserving required-field semantics.

## Verification

```text
uv run pytest -q tests/test_task11_round4.py \
  tests/test_secret_provider.py tests/test_launch.py \
  tests/test_safety_drill.py tests/test_watchdog.py

Focused secret/preflight/safety-drill/watchdog:
353 passed, 1 warning

33-file affected trust matrix:
2182 passed, 1 warning in 385.43s

Repository static gate:
release static checks: PASS

Compileall:
PASS

git diff --check:
PASS

Exactly one no-argument full suite:
3660 passed, 1 skipped, 1 warning in 609.41s
```

Pytest exited normally. The warning is the existing third-party
`websockets.legacy` deprecation warning. No no-argument full suite was run
before the focused, matrix, static, compile, and diff prerequisites were
green, and no second full suite was run.

## Residual hard limits

- Watchdog can request and receive only `database_url`.
- Optional visibility is exact role authority, not startup-required status;
  required-field validation remains unchanged.
- Unsupported roles and authority mismatches fail closed. No role receives a
  broad all-fields projection.
- General chat remains read-only. Drafts still require an explicit signed
  queue action and separate human approval.
- Paper-only mode, kill switches, broker-truth checks, no webhook, disabled
  Composio pending provider-side rotation, and the no-profit-guarantee
  boundary are unchanged.
- Verification used fake Keychain account access and local fixtures only. It
  made no external call or runtime-state change.
