# Mac runtime operations

The designated source checkout is the runtime installation. Install its existing
paper settings with `./scripts/launchd/install.sh --with-daemon`. This installs the
app, daemon, watchdog, encrypted daily backup, power guard, health doctor, release
receipt importer and Telegram alert delivery jobs. Without `--with-daemon`, the
installer retains the app/watchdog/backup workflow and refuses an existing daemon.
Uninstall removes every managed job. Reinstalling preserves the database/history,
approval settings and breaker states; it does not clear a stop.

## Power

Keep the lid open. On AC, the power job uses `caffeinate -i -s -w <owner-pid>`.
It does not change global sleep settings or keep the display awake. Closing the
lid, logging out, shutting down or losing connectivity can interrupt operation.
An existing Mac on battery cannot provide dependable continuous operation.

For explicitly authorized temporary battery work:

```sh
.venv/bin/python scripts/runtime_operations.py battery --minutes 120
.venv/bin/python scripts/runtime_operations.py release-battery
.venv/bin/python scripts/runtime_operations.py status
```

Battery permission is limited to 1–120 minutes, expires by timestamp, and releases
at 30% charge or when power information is unreadable. The controller also bounds
an existing permission by monotonic elapsed time. It checks every 15 seconds;
the assertion ends if its owner exits. There is no automatic permission renewal.

## Health, incidents and recovery

`runtime_operations.py doctor` observes the app's public loopback liveness,
read-only database heartbeat/breakers/reconciliation metadata, managed launchd
jobs, power state, committed backup headers and exact-commit release evidence.
It loads no broker credentials, field decryption key or account balances. A recent
backup header establishes a committed artifact's age; it does not replace a fresh
decryption/restore test. Unknown checks never resolve an existing incident.

Private JSON under `.local/operations` retains current checks, incidents and the
last 100 transition events. The discarded-event count makes retention visible.
Repeated failures do not create repeated alerts. A gap over three minutes is
recorded as a monitoring gap; its cause is unknown. Verified exclusive backup
maintenance suppresses expected runtime outages for at most 15 minutes, after
which `backup_stalled` is actionable. An unreadable maintenance lock opens an
incident and does not suppress other checks.

The watchdog can restart loaded app/daemon jobs. Missing jobs require the canonical
installer. It never bootstraps an uninstalled daemon. The doctor never clears a
breaker, changes strategy/risk configuration, or submits an order. Recovery of a
safety stop still requires the existing authenticated guarded recovery workflow.

`com.trading.release-sync` runs the separate managed-auth GitHub importer every
five minutes. It imports only successful CI for the clean current commit, with
authenticated provenance and artifact digest checks. Dirty source, missing CI,
failed jobs and partial evidence remain unverified. Only the operator sync and
alert jobs receive the managed CLI directory in PATH; product Composio integration
remains disabled.

## Telegram

Connect the bot through the secure managed connection form using alias
`trading-alerts`. Enter its token in that form, never in chat, command arguments,
the repository or operational JSON. Then run:

```sh
.venv/bin/python scripts/runtime_alerts.py pair
```

Open the returned `https://t.me/...?...` link and press Start in a private chat.
The nonce expires after 30 minutes. The delivery job matches only the exact nonce
from a human whose sender ID equals that private chat ID. It advances through
queued bot updates; it does not delete an existing webhook. A webhook/another
polling consumer may require operator setup before this dedicated delivery path
can work. The bot identity must match before pairing or incident delivery.

Pairing sends one setup test alert. `runtime_alerts.py status` reports whether its
response was confirmed. Incident messages contain only fixed component/code/time
and recovery guidance, with no trading account payload. Delivery is bounded to
five events per run. Each attempt is persisted before network I/O; uncertain
responses/process death remain `unknown` and are not automatically resent. Check
`.local/operations/telegram-delivery.json` for those results. CLI exit failures are
sanitized; raw managed-auth responses, tokens and signed URLs are not logged.
Successful local tests do not establish real Telegram delivery.

## Changed volume identity

Database choice, backup commit receipts and cooperative app control deliberately
bind file identities. A changed filesystem device number can invalidate those
bindings even when paths/inodes remain the same. Do not weaken the identity checks
or edit historical backup receipts. Stop the affected managed jobs, establish the
exact database/history with integrity checks, create a fresh verified encrypted
backup, and reselect the exact inspected database through `choose-database` with
its current hash. Retain prior evidence. A stale control socket may be quarantined
only after proving its recorded process absent, no listener, and matching prior
path/inode/timestamps. Restore jobs and require fresh broker reconciliation before
guarded recovery. This is an operator recovery, never an automatic rebind.
