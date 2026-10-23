# Decision record: which database the runtime uses

**Status: DECIDED — Option C, executed 2026-10-22.** Both old files are
preserved (see *Decision* below).

`$R` is `~/Desktop/project/robinhood/trading-assistant`; `$W` is
`$R/.worktrees/safety-foundation`.

## Evidence

Read-only, immutable SQLite opens on 2026-10-16/17. Only counts, states and
timestamps were read; no account details.

| | `$W/trading_assistant.db` | `$R/trading_assistant.db` |
|---|---|---|
| SHA-256 (prefix) | `63d4a238dd8747a1` | `41d55322f5022219` |
| Schema | `20260729_0017` (one migration behind the code) | `20260730_0018` (current) |
| Era of orders | 2026-07-17 to 2026-07-30 | 2026-07-31 to 2026-08-11 (audit events to 2026-10-05) |
| Written by | old app and daemon (human approvals, rules, plans) | retired scheduled autopilot |
| Orders | 7: 4 filled, 2 expired, 1 proposed | 49: 2 filled, 47 rejected |
| Fills | 4, all trusted, all tied to orders | 3, all trusted, all tied to orders |
| Orders in flight | none | none |
| Rules and plans | 5 active, 1 processing, 1 triggered rule; 1 approved, 2 proposed, 1 canceled plan | none |
| Breakers | `operator_global` **tripped** | none tripped |
| Last startup reconciliation | **current** (succeeded, 2026-07-30) | **failed** |
| `integrity_check` / foreign keys | ok / 0 violations | ok / 0 violations |
| History in common | **none** | **none** |

What this means:

- The two files are **consecutive eras of the same Alpaca paper account**,
  not an older and a newer copy.
- Each knows only its own era's fills. The main database's last startup
  reconciliation **failed**, which is consistent with the broker holding
  positions from the worktree era that this database never recorded.
- Neither file is corrupt, and neither has an order in flight.
- **Neither alone is likely to reconcile with the current paper account.**
  That cannot be confirmed without reading the account, which needs your
  approval (checklist item D1).

## Options

**A. Keep the main checkout's database.** Choose `$R/trading_assistant.db`.
- Writes nothing until the first start, which switches it to WAL mode.
- Continuity with the autopilot era.
- Expect startup reconciliation to report worktree-era positions as drift
  and block new orders until you review them. Reversible: choose another
  file later.

**B. Adopt the worktree database.** Back it up, migrate it to `0018`, then
consolidate it into `$R` with the main database's exact hash.
- Restores the July rules, plans and approvals.
- **Also restores the tripped global breaker**, and is still missing the
  autopilot-era fills.
- Writes to both files; the main file's history then lives only in an
  encrypted backup.
- The highest-effort and highest-risk option.

**C. Start a fresh database.** Archive both files untouched, encrypted
backups of each taken first. Migrate a new, empty
`$R/trading_assistant.db`, then either:
- **C1** adopt the account's *closed* Alpaca paper history with
  `ops.paper_history`, which refuses if the account holds any position; or
- **C2** start with no history.

C1 produces a database that matches the broker by construction. Both old
files remain available read-only for reference and for the evaluation report
(`paper_report --database <file>` reports on either one, read-only).

## Recommendation, from the evidence

- **C1 if your paper account is flat,** or if you are willing to flatten it
  yourself in the Alpaca dashboard. It is the only option that is expected
  to reconcile cleanly, it carries no stale rules or tripped breaker into a
  test, and it preserves both histories untouched.
- **A if you would rather not change the account.** You get the most recent
  history but must review the expected drift on first start.
- **B** is not recommended unless you specifically need the July rules and
  plans.

The first approved broker read (D1) tells you whether C1 is possible.

## Exact commands, after your decision

Before any option, back up both candidates. This needs the backup key from
the Keychain and the repaired venv.

```bash
cd ~/Desktop/project/robinhood/trading-assistant
.venv/bin/python -m trading_assistant.ops.backup --database "$PWD/trading_assistant.db" --destination "$PWD/.local/encrypted-backups"
.venv/bin/python -m trading_assistant.ops.backup --database "$PWD/.worktrees/safety-foundation/trading_assistant.db" --destination "$PWD/.local/encrypted-backups"
```

**Option A:**

```bash
.venv/bin/python -I src/trading_assistant/installation.py choose-database \
  --path "$PWD/trading_assistant.db" --sha256 "$(shasum -a 256 trading_assistant.db | cut -c1-64)"
```

**Option C:**

1. Keep the old file under a dated name, **with any `-wal`/`-shm` sidecars**:
   left behind, they sit next to the new file and SQLite may replay them
   into it.
2. Migrate a new, empty file and initialise sensitive-field encryption.
3. Optionally, if the account is flat, adopt closed history (C1).
4. Choose the new file.

```bash
A=trading_assistant.db.archived-autopilot-era-$(date +%Y%m%d)
for f in trading_assistant.db trading_assistant.db-wal trading_assistant.db-shm; do
  [ -e "$f" ] && mv -n "$f" "${f/trading_assistant.db/$A}"
done
.venv/bin/python -m trading_assistant.db.migrate --database "$PWD/trading_assistant.db" upgrade
.venv/bin/python -m trading_assistant.ops.encrypt_sensitive migrate
.venv/bin/python -m trading_assistant.ops.encrypt_sensitive verify
.venv/bin/python -m trading_assistant.ops.paper_history --help
.venv/bin/python -I src/trading_assistant/installation.py choose-database \
  --path "$PWD/trading_assistant.db" --sha256 "$(shasum -a 256 trading_assistant.db | cut -c1-64)"
```

**Option B:** migrate `$W`'s file with `db.migrate --database <that file>
upgrade`, then run `ops.runtime_consolidation` with
`--replace-divergent-destination <sha256 of $R/trading_assistant.db>`, then
choose the new file with `--replace`.

## Decision

- Chosen option: **C** (fresh database). C1 waits until the paper account
  is flat: on 2026-10-22 it still held AAPL and META from the earlier eras.
- Date: 2026-10-22.
- Preserved: `trading_assistant.db.archived-autopilot-era-20261022`
  (SHA-256 prefix `41d55322f5022219`, plus an encrypted backup in
  `.local/encrypted-backups/option-c-20261022`, restored and validated in
  isolation) and the untouched worktree file (`63d4a238dd8747a1`, plus a
  private copy in `.local/archive-20261022`; the backup tool refuses its
  `0017` schema).
- Lesson: the main file's `-wal`/`-shm` sidecars were left behind by the
  move. The new database was compared with a pristine migration and is
  identical apart from constraint order; the archived file lacks only the
  lock rows the backup run wrote, which the encrypted backup holds.
