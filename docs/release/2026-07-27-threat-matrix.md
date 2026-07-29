# Loopback Paper Release Threat Matrix

**Date:** 2026-07-27
**Scope:** local loopback HTTPS operator console, Alpaca paper only
**Authority:** prevention and recovery remain deterministic; this document
does not authorize an order, a breaker reset, a daemon start, or a provider
call.

## Two-dimensional release status

Software verification and operational readiness are independent claims. The
pure evaluator is
`src/trading_assistant/ops/release_status.py::evaluate_release_status`.
Every result also reports `paper_only=true` and
`execution_authorized=false`.

| Deterministic tests | Fresh operational evidence | Software status | Operational status |
| --- | --- | --- | --- |
| Pass | Preflight ready | Verified | Ready |
| Pass | Breaker tripped | Verified | Blocked |
| Pass | Broker truth unknown | Verified | Blocked |
| Pass | Daemon stale | Verified | Blocked |
| Pass | Encryption mixed | Blocked | Blocked |
| Fail | Preflight ready | Blocked | Blocked |

“Operational ready” means only that the bounded paper preflight supplied its
required evidence. It is not execution authority. The status vocabulary does
not claim profitable behavior, unattended authority, a running daemon, or a
non-paper trading mode. Unclassified evidence is rejected rather than coerced.

Exact release-state evidence:

- `tests/test_release_gate_branches.py::test_release_status_keeps_software_and_operations_independent`
- `tests/test_release_gate_branches.py::test_release_status_vocabulary_never_claims_unproved_trading_state`
- `tests/test_release_gate_branches.py::test_release_status_rejects_coerced_or_unclassified_evidence`

## 1. Paid-call and resource exhaustion

- **Prevention:** `src/trading_assistant/app/limits.py` provides durable
  request windows and leases; `src/trading_assistant/llm/budget.py` reserves
  calls and tokens before provider I/O; `src/trading_assistant/llm/factory.py`
  wraps provider backends; `src/trading_assistant/backtest/llm_runner.py`
  enforces a separate backtest call ceiling.
- **Detection:** durable budget-day, reservation, and rate-window rows feed
  `src/trading_assistant/operations/security_posture.py`; started or
  acceptance-unknown reservations remain charged.
- **Recovery:** deny further paid work, preserve unknown reservations, and
  reconcile exact provider usage. Never release a started/unknown reservation
  merely to regain capacity.
- **Owner:** provider-budget and route-policy maintainer; operator for
  provider-console caps.
- **Exact tests:** `tests/test_llm_budget.py::test_denied_budget_never_calls_delegate`,
  `tests/test_llm_budget.py::test_delegate_exception_leaves_reservation_fully_charged_and_unknown`,
  `tests/test_llm_budget.py::test_parallel_reservations_cannot_cross_any_daily_ceiling`,
  `tests/test_llm_runner.py::test_budget_aborts_run`.

## 2. Credential exposure and rotation failure

- **Prevention:** `src/trading_assistant/security/secrets.py` admits the macOS
  Keychain provider for production roles; `src/trading_assistant/ops/secrets.py`
  performs private migration, validation, and rotation; redaction is registered
  before provider construction.
- **Detection:** role validation, Keychain audit metadata, and the release
  history scanner report stable codes and paths without values.
- **Recovery:** revoke the exposed credential at its provider, store a newly
  scoped replacement in Keychain, validate the complete role, and keep that
  provider disabled until an external revocation/rotation receipt exists.
  Never copy the old value into source, logs, SQLite, or a command line.
- **Owner:** operator for provider-side revocation; secret-lifecycle maintainer
  for local validation.
- **Exact tests:** `tests/test_secret_provider.py::test_keychain_provider_reads_exact_accounts_without_subprocess_or_logging`,
  `tests/test_secret_provider.py::test_production_roles_reject_environment_provider`,
  `tests/test_secret_provider.py::test_migrate_env_rolls_back_failure_at_every_write_and_retries_safely`,
  `tests/test_release_static.py::test_current_credential_fingerprint_is_reported_without_value`.

## 3. Public host, origin, proxy, or endpoint exposure

- **Prevention:** `src/trading_assistant/security/transport.py`,
  `src/trading_assistant/app/security.py`, and
  `src/trading_assistant/ops/serve.py` require loopback HTTPS, exact hosts,
  same origin, secure cookies, bounded requests, and disabled proxy trust.
- **Detection:** startup inspection rejects unsafe bind/TLS state; transport
  middleware rejects before route, session, broker, or provider work.
- **Recovery:** stop the operator service, correct bind/TLS/host configuration,
  and rotate credentials if exposure is plausible. Do not fall back to public
  HTTP or weaken host/origin checks.
- **Owner:** transport-boundary maintainer and local operator.
- **Exact tests:** `tests/test_transport_boundary.py::test_untrusted_host_is_rejected_before_anonymous_liveness`,
  `tests/test_transport_boundary.py::test_cross_origin_request_is_rejected_before_anonymous_liveness`,
  `tests/test_transport_boundary.py::test_strict_launcher_uses_only_loopback_tls_and_disables_proxy_headers`.

## 4. Plaintext sensitive persistence or backup

- **Prevention:** `src/trading_assistant/security/crypto.py` and
  `src/trading_assistant/security/sensitive_fields.py` bind AES-GCM envelopes
  to exact rows and columns; `src/trading_assistant/ops/encrypt_sensitive.py`
  and `src/trading_assistant/ops/backup.py` publish only verified encrypted
  artifacts under maintenance tenure.
- **Detection:** startup scans reject plaintext, malformed envelopes, retained
  old-key state, and mixed migration state; temporary and final artifacts are
  checked before publication.
- **Recovery:** keep startup blocked, retain the last verified encrypted
  backup, reacquire exact maintenance tenure, and resume from authoritative
  database scans. Never mark a mixed migration complete, discard a tripped
  breaker, or publish a partially verified backup.
- **Owner:** encryption/migration maintainer and operator holding encryption
  keys.
- **Exact tests:** `tests/test_sensitive_crypto.py::test_store_first_insert_and_update_never_send_plaintext_to_sql_or_disk`,
  `tests/test_sensitive_migration.py::test_migration_backs_up_before_mutation_and_encrypts_every_registered_field`,
  `tests/test_sensitive_migration.py::test_startup_crypto_scan_blocks_valid_retained_key_as_mixed_state`.

## 5. Direct or indirect prompt injection

- **Prevention:** `src/trading_assistant/analyst/untrusted.py` canonicalizes,
  bounds, and quarantines external text without mutable tools;
  `src/trading_assistant/security/candidates.py` accepts only signed,
  schema-valid candidates through a separate operator queue action;
  `src/trading_assistant/app/agent.py` exposes read-only research behavior.
- **Detection:** stable injection flags and metadata-only quarantine records
  capture direct, encoded, Unicode, HTML, and tool-manipulation cues.
- **Recovery:** reject the affected observation or candidate, preserve
  metadata-only evidence, and require a clean new analysis. Never forward the
  raw payload into a privileged model or convert its instructions into state.
- **Owner:** model-trust boundary maintainer; operator for candidate review.
- **Exact tests:** `tests/test_untrusted_content.py::test_gateway_decodes_base64_only_to_flag_and_never_forwards_payload`,
  `tests/test_untrusted_content.py::test_unicode_smuggling_is_removed_before_instruction_detection`,
  `tests/test_candidate_boundary.py::test_http_queue_requires_csrf_idempotency_and_never_approves_or_submits`.

## 6. Webhook replay, DNS rebinding, or redirect abuse

- **Prevention:** this release has no inbound webhook route;
  `src/trading_assistant/security/outbound.py` requires exact HTTPS origins,
  rejects credential query names and redirects, ignores proxy/CA environment
  overrides, and validates final URLs.
- **Detection:** route inventory/static analysis rejects hidden webhook
  registration; outbound adapters report stable policy errors before second
  requests or body exposure.
- **Recovery:** keep the receiver absent, reject the response, and investigate
  provider/DNS/TLS state. A future webhook requires a separately approved
  signed, timestamped, replay-safe service; do not add an emergency bypass.
- **Owner:** transport/outbound-policy maintainer.
- **Exact tests:** `tests/test_release_static.py::test_route_branch_union_catches_webhook_hidden_by_safe_else`,
  `tests/test_outbound_policy.py::test_requests_session_rejects_redirect_before_second_request`,
  `tests/test_outbound_policy.py::test_origin_normalizes_idna_case_and_default_port`.

## 7. Duplicate order, acceptance unknown, or partial fill

- **Prevention:** `src/trading_assistant/orders/submission.py`,
  `src/trading_assistant/orders/reconciliation.py`, and
  `src/trading_assistant/risk/submission_barrier.py` use client order IDs,
  durable claim state, broker-identity validation, exact fill economics, and
  submission fencing.
- **Detection:** acceptance-unknown and partial-fill states remain explicit;
  reconciliation compares broker identity, cumulative quantity, activities,
  and local fills before lifecycle progress.
- **Recovery:** stop submission, reconcile by the exact client order ID and
  broker truth, and account for confirmed partial fills. Never resubmit an
  acceptance-unknown order and never flatten an unattributed position.
- **Owner:** order-submission and reconciliation maintainer; operator for
  broker investigation.
- **Exact tests:** `tests/test_order_submission.py::test_accept_then_disconnect_becomes_unknown_without_duplicate`,
  `tests/test_order_submission.py::test_synchronous_terminal_partial_fill_preserves_status_and_latches`,
  `tests/test_safety_drill.py::test_unconfirmed_partially_filled_original_is_never_compensated`.

## 8. Stale quote, reconciliation, or daemon state

- **Prevention:** `src/trading_assistant/risk/staleness.py`,
  `src/trading_assistant/orders/startup.py`,
  `src/trading_assistant/daemon/monitor.py`, and
  `src/trading_assistant/ops/watchdog.py` require bounded quote age, a fresh
  reconciliation generation, heartbeat evidence, and scoped breaker state.
- **Detection:** stale/missing/future timestamps, unknown remote orders,
  reconciliation failures, and stale daemon heartbeats produce blocked
  posture rather than inferred health.
- **Recovery:** block orders and rule mutations, restore data/heartbeat
  sources, then complete broker reconciliation. Any breaker stays tripped
  until separately investigated and explicitly reset with current health
  evidence.
- **Owner:** daemon/reconciliation maintainer and operator.
- **Exact tests:** `tests/test_startup_reconciliation.py::test_unknown_remote_open_order_cannot_look_reconciled_or_submit`,
  `tests/test_monitor.py::test_runtime_reconciliation_failure_trips_switches_before_rules`,
  `tests/test_watchdog.py::test_healthy_api_and_stale_daemon_restart_daemon_only`,
  `tests/stress/test_stress_scenarios.py::test_stale_quote_blocked`.

## 9. Breaker scope or reset race

- **Prevention:** `src/trading_assistant/risk/breakers.py`,
  `src/trading_assistant/risk/killswitch.py`, and
  `src/trading_assistant/orders/safety_state.py` persist canonical,
  generation-bound scopes and require reason plus prior-health evidence.
- **Detection:** every trip/reset is audited; restart reads persisted state;
  stale generations cannot clear a replacement latch.
- **Recovery:** investigate the exact scope and reset only through the explicit
  operator path after health proof. A concurrent retrip wins. Never broaden a
  reset, erase history, or reset as part of release verification.
- **Owner:** deterministic risk maintainer; operator is the only reset
  authority.
- **Exact tests:** `tests/test_breakers.py::test_scoped_breakers_persist_and_reset_independently`,
  `tests/test_breakers.py::test_reset_requires_reason_and_prior_health`,
  `tests/test_breakers.py::test_reset_is_bound_to_observed_generation_and_a_retrip_wins`,
  `tests/test_killswitch.py::test_compatibility_reset_is_explicitly_disabled_and_stays_tripped`.

## 10. Backtest lookahead or holdout misuse

- **Prevention:** `src/trading_assistant/backtest/engine.py` exposes only a
  timestamp-bounded `DataView`; `src/trading_assistant/backtest/holdout.py`
  isolates holdout data; `src/trading_assistant/backtest/evaluate.py` separates
  tuning, validation, and holdout reporting.
- **Detection:** future access raises, SPY context shares the same cutoff,
  holdout sweeps are refused, and persisted artifacts are validated as one
  atomic report.
- **Recovery:** invalidate the affected simulated report and rerun from a clean
  bounded view. Do not retune against the holdout, relabel an invalid artifact,
  or promote a strategy automatically.
- **Owner:** backtest/research maintainer.
- **Exact tests:** `tests/test_backtest_engine.py::test_dataview_future_access_raises`,
  `tests/test_backtest_engine.py::test_spy_context_cannot_see_future`,
  `tests/test_backtest_evaluate.py::test_holdout_split_and_sweep_refused`,
  `tests/test_backtest_evaluate.py::test_artifact_validation_rolls_back_nonfinite_run_atomically`.

## 11. UI stale state or paper/non-paper deception

- **Prevention:** local assets and text-only DOM sinks in
  `src/trading_assistant/app/static/`; exact mutation receipts and failed-fetch
  clearing in `js/plans.js`, `js/index.js`, and `js/backtests.js`; paper and
  simulation labels are permanent. `release_status.py` keeps software and
  operations separate.
- **Detection:** malformed/current refreshes clear prior authority; receipt
  identity/status mismatches reject success; desktop, mobile, and print
  contracts require paper/simulation labels.
- **Recovery:** clear stale facts, dialogs, and action authority and require a
  fresh exact receipt. Never preserve a stale approval control, infer broker
  health, or present software verification as permission to trade.
- **Owner:** operator-console maintainer.
- **Exact tests:** `tests/test_security.py::test_plan_list_failure_or_malformed_envelope_clears_stale_authority`,
  `tests/test_security.py::test_plan_mutation_receipts_must_match_exact_frozen_target`,
  `tests/test_frontend_ui.py::test_every_page_uses_the_original_local_identity_and_explicit_paper_mode`,
  `tests/test_release_gate_branches.py::test_release_status_keeps_software_and_operations_independent`.

## 12. Dependency, build, or publication compromise

- **Prevention:** `.github/workflows/ci.yml` pins action commits and uses full
  history; `scripts/check_release_safety.py` inspects current tree and history;
  `scripts/verify_loopback_release.py` runs a fixed offline command manifest
  from a clean commit with a sanitized environment and private evidence.
- **Detection:** dirty-tree, migration-head, skipped-suite, network-command,
  history-secret, unsafe-artifact, action-pin, and changed-during-run checks
  fail closed.
- **Recovery:** block publication, preserve the failing evidence, repair the
  dependency/history/migration issue, and rerun from a clean exact commit. Do
  not bypass a scanner, force-push, weaken a breaker, or publish a partial pass.
- **Owner:** release maintainer; repository owner for branch protection and
  provider-side controls.
- **Exact tests:** `tests/test_release_verifier.py::test_release_verifier_has_only_the_exact_offline_commands`,
  `tests/test_release_verifier.py::test_dirty_tree_blocks_before_any_verification_command`,
  `tests/test_release_verifier.py::test_command_that_changes_candidate_tree_cannot_produce_pass`,
  `tests/test_release_static.py::test_ci_actions_are_commit_pinned_and_checkout_complete_history`.

## Separately authorized credentialed paper drill

The credentialed paper safety drill remains a distinct operator activity. Its
`--alpaca-paper` mode is capable of broker writes and therefore requires
separate explicit authorization, valid paper credentials, exact paper-target
validation, and its dedicated database copy. It is not a release-verifier
step, a prerequisite for software verification, or permission for routine
execution.

The offline verifier neither imports nor invokes this drill, and the drill
does not import or invoke the offline verifier. This release task runs neither
credentialed path.

Exact separation evidence:

- `tests/test_release_verifier.py::test_release_verifier_has_only_the_exact_offline_commands`
- `tests/test_safety_drill.py::test_credentialed_paper_drill_is_separate_from_offline_release_verifier`
- `tests/test_safety_drill.py::test_offline_release_verifier_import_and_commands_exclude_safety_drill`
