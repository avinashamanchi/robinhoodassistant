# Decision record: which database the runtime uses

**Status: OPEN — awaiting the operator's decision. Nothing below has been executed.**

`$R` is `~/Desktop/project/robinhood/trading-assistant`; `$W` is
`$R/.worktrees/safety-foundation`.

## Why a decision is needed

Two databases exist, and they are **independent**, not an older and a newer
copy of one history. A read-only comparison of every history table found no
row in common, not even one audit event:

| | `$W/trading_assistant.db` | `$R/trading_assistant.db` |
|---|---|---|
| Written by | the old app and daemon jobs (working directory `$W`) | the retired scheduled autopilot job (working directory `$R`) |
| Last written | 2026-07-30 | 2026-10-05 |
| Schema | `20260729_0017` (one behind the code) | `20260730_0018` (current) |
| Orders | 7: 4 filled, 2 expired, 1 proposed | 49: 2 filled, 47 rejected |
| Fills | 4 | 3 |
| Rules, plans | 5 active, 1 processing, 1 triggered rules; 1 approved, 2 proposed, 1 canceled plans | none |
| Breakers | `operator_global` tripped | none tripped |
| `quick_check` | ok | ok |

The Keychain `database_url` was not read. The file activity shows each job
wrote the database in its own working directory, so it is almost certainly a
relative URL (`sqlite:///./trading_assistant.db`).

Code in this pass makes the choice explicit: production startup, preflight,
the installer and the evaluation report refuse any database other than the
one recorded with `installation.py choose-database`, and runtime
consolidation refuses to discard destination history unless given the
destination's exact SHA-256.

## Options

### Option A — keep the main checkout's database; do not consolidate

- **Action:** choose `$R/trading_assistant.db` (one command, below). Leave
  `$W/trading_assistant.db` untouched as a historical record.
- **Preserves:** both databases byte-for-byte. Nothing is moved, merged or
  migrated.
- **Consequence:** the runtime starts from the autopilot-era history. The
  worktree's rules, plans and 7 orders are not active.
- **Risk:** if the Alpaca paper account still holds positions from fills that
  only the worktree database knows, startup reconciliation reports them as
  drift and blocks new orders until you review them. That fails closed and is
  expected.
- **Reversibility:** full. Choosing another file later is one command with
  `--replace`.

### Option B — adopt the worktree database; migrate, then consolidate

- **Action:**
  1. Migrate `$W/trading_assistant.db` from `0017` to `0018`; the migrator
     makes its own pre-migration backup.
  2. Consolidate it into `$R`, acknowledging the main database's exact hash:
     `--replace-divergent-destination <sha256 of $R/trading_assistant.db>`.
  3. Choose the new `$R/trading_assistant.db`.
- **Preserves:** consolidation writes and verifies an encrypted backup of both
  databases first. The main database's 49 orders, 3 fills and 375 audit
  events then exist only in that encrypted backup.
- **Risk:** higher.
  - It writes to both databases.
  - The migration targets whatever the Keychain URL resolves to from the
    working directory, which was not read. Confirm it first.
  - The tripped `operator_global` breaker and the 5 active rules come back to
    life.
- **Reversibility: partial.** There is no command to restore a database from
  an encrypted backup. Recovery needs the reviewed manual procedure in the
  RUNBOOK (decrypt to private staging, verify, install under maintenance
  tenure). It is untested end to end.

### Also possible — Option C: start a fresh database

Archive both files untouched, migrate a new empty database at
`$R/trading_assistant.db`, and choose it. This is the cleanest baseline for
evaluating the prototype, ideally after resetting the paper account in the
Alpaca dashboard (your action). Any positions left in the account will be
reported as unknown to the runtime.

## Recommendation

Option A, unless you need the worktree's July rules and plans. It loses
nothing, writes nothing, and is fully reversible. If you want a clean
evaluation baseline instead, Option C plus an Alpaca paper reset is better
than B.

## Exact commands (run only after deciding)

Option A:

```bash
cd ~/Desktop/project/robinhood/trading-assistant
shasum -a 256 trading_assistant.db
.venv/bin/python -I src/trading_assistant/installation.py choose-database \
  --path "$PWD/trading_assistant.db" --sha256 <the hash printed above>
.venv/bin/python -I src/trading_assistant/installation.py status
```

Option B: do not run this until the Keychain `database_url` is confirmed
relative, all runtimes are stopped, and the repair steps A1–A3 are done.

```bash
cd ~/Desktop/project/robinhood/trading-assistant/.worktrees/safety-foundation
../../.venv/bin/python -m trading_assistant.db.migrate status    # must show this worktree's database at 20260729_0017
../../.venv/bin/python -m trading_assistant.db.migrate upgrade
cd ~/Desktop/project/robinhood/trading-assistant
shasum -a 256 trading_assistant.db                                # the destination you are discarding
.venv/bin/python -m trading_assistant.ops.runtime_consolidation \
  --source-root "$PWD/.worktrees/safety-foundation" --destination-root "$PWD" \
  --replace-divergent-destination <that hash>
shasum -a 256 trading_assistant.db
.venv/bin/python -I src/trading_assistant/installation.py choose-database \
  --path "$PWD/trading_assistant.db" --sha256 <new hash> --replace
```

## Decision

- Chosen option: _______
- Date: _______
- Chosen file and SHA-256 at choice: _______
