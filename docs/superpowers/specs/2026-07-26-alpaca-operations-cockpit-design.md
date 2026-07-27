# Alpaca Operations Cockpit Design

## Subject and single job

This is a one-operator control surface for an Alpaca paper-trading assistant.
Its first job is to answer one question without ambiguity: **what does the
system and broker prove right now, and what is still blocked?**

The user approved continuing the existing evidence-first console and asked for
a substantially better UI, a running Alpaca program, and a published GitHub
branch. This design improves presentation and read-only broker visibility. It
does not weaken paper-only execution, human approval, risk checks, breakers,
reconciliation, or startup gates.

## Approaches considered

### 1. Brokerage-style portfolio dashboard

Lead with an equity chart, gains, watchlists, and prominent buy controls. This
looks familiar, but it overstates performance when the application does not
yet own a trustworthy equity-history series and makes execution feel easier
than verification.

### 2. Dark trading terminal

Use a dense black dashboard with neon market data. This communicates speed,
but it is a generic visual pattern and encourages scanning price movement
instead of noticing stale or incomplete safety evidence.

### 3. Flight-deck operations cockpit — selected

Use a compact proof tape across the top, a server-sourced account masthead,
and a decision ledger beneath it. The console should feel like an instrument
panel for a system that may refuse to act. It emphasizes freshness,
reconciliation, breakers, exact approvals, and receipts before analysis.

## Visual system

### Color tokens

- `Flight ivory #F5F6F1`: page field.
- `Night plum #242033`: header, primary text, and structural anchors.
- `Cobalt #3358D4`: navigation, focus, refresh, and neutral action.
- `Copper #B5672A`: stale, pending, and incomplete evidence.
- `Signal coral #C9474D`: danger and blocked state only.
- `Verified teal #24756A`: explicitly verified state only.
- `Cloud #FFFFFF`: working surfaces.

Color is never the only state indicator. Every status carries direct text.

### Typography

The strict self-only content security policy remains unchanged, so the UI uses
local system faces:

- Display: `Avenir Next Condensed`, `Arial Narrow`, sans-serif.
- Body: `Avenir Next`, `Segoe UI`, sans-serif.
- Data: `SFMono-Regular`, `Cascadia Mono`, monospace.

Display type is reserved for the account masthead and major section titles.
Prices, timestamps, generations, IDs, and modes use tabular data type.

### Signature element: proof tape

A horizontal proof tape sits immediately below the header. Each segment names
one claim and its observation:

```text
ALPACA PAPER | MARKET CLOSED | DATA BLOCKED | DAEMON STALE |
RECONCILIATION CURRENT | BROKER-DRIFT BREAKER TRIPPED
```

The tape is not decoration. Unknown values render as unknown, stale values
show their age, and failures remain visible while the page is usable.

### Desktop layout

```text
┌──────────────── product / navigation / operator ────────────────┐
├──────────── proof tape: broker / market / data / daemon / safety┤
│ Account masthead: equity | buying power | cash | exposure       │
├───────────────────┬─────────────────────────────────────────────┤
│ Truth + breakers  │ Pending decisions and action receipts       │
│ sticky instrument │ Positions / combined holdings               │
│ panel              │ Assistant / audit trail                     │
└───────────────────┴─────────────────────────────────────────────┘
```

On mobile, the proof tape scrolls horizontally, account facts become a
single-column stack, and the truth panel stacks above decisions.

## Data and interaction contracts

### Account summary

Add authenticated `GET /account`, backed by the existing read-only
`TradingService.get_account_summary()`. The response contains decimal strings
for equity, buying power, cash, and gross exposure plus broker positions and a
UTC `observed_at` timestamp. Account and position values are validated before
they can be returned as truth. Provider or integrity failures use the existing
stable dependency-unavailable envelope. The browser never calculates or
guesses account truth.

The account, positions, and combined-holdings routes share a per-session
broker-read limit. Account-backed views use a two-second, fail-closed cache
that coalesces concurrent reads. Expired cache entries are never served as a
fallback after a provider failure.

The account masthead has explicit loading, unavailable, and observed states.
If unavailable, all values say `Unavailable`; prior values are cleared rather
than left looking current.

### Proof tape

The UI derives tape labels only from current `/health`, `/account`, and
`/pending` responses. Account metrics, the position table, and the Alpaca rows
in combined holdings render from the same cached account snapshot so a fill
between two broker calls cannot create a self-contradictory screen. It may
summarize server responses, but cannot infer that trading is ready. The server
remains authoritative.

The current release says `PAPER` everywhere. No UI control or copy claims live
trading, future profitability, or autonomous execution.

### Approvals and panic

The existing exact-order proof dialog, non-empty reason, recent
reauthentication, execution-time risk recheck, and one-shot request behavior
remain unchanged. The visual hierarchy keeps the assistant below the human
decision ledger. Panic and breaker controls stay visually distinct and never
render a partial receipt as success.

### Refresh and failure handling

Account refresh follows the same abort-and-sequence pattern as positions,
holdings, and risk events so a slower older request cannot overwrite fresher
truth. It invalidates prior proof before every request, has a finite 15-second
browser timeout, and rejects malformed or stale observation timestamps. The
10-second refresh remains; timestamps and freshness labels expose what was
actually observed.

Reconciliation proof has a configured 300-second maximum age. Evidence older
than that remains visible with its age but renders stale, never current.

Preflight reports Alpaca account authentication, market clock, and market data
as independent checks. A bad quote must fail the data check without falsely
claiming authentication or clock access failed.

### Local operational artifacts

SQLite databases, migration backups, and submission lock/intent files remain
private local state and are ignored by Git. No credential, database, backup,
lock, or browser session artifact may enter the published commit.

## Motion and accessibility

- One restrained page-load reveal groups the proof tape, account masthead, and
  ledger; there are no continuous market animations.
- `prefers-reduced-motion` removes the reveal and smooth scrolling.
- Keyboard focus stays clearly visible.
- Horizontal proof tape remains keyboard and touch scrollable.
- Status updates keep appropriate live regions.
- Tables retain accessible headers and usable overflow at 360 CSS pixels.
- Numeric cards include labels and do not rely on color.

## Launch and publication contract

- `scripts/start.sh` continues to require a fully green preflight before
  starting the app and daemon.
- A manually launched console may be used to inspect blocked state, but it is
  not called trade-ready and does not authorize execution.
- No breaker is reset automatically.
- No live-mode flag or confirmation string is created.
- Publishing uses the current feature branch, a normal push, and a draft pull
  request; no force push or silent overwrite of `main`.

## Verification

- API test for authenticated `GET /account` and dependency failure.
- Frontend contract tests for required proof-tape/account elements and no
  inline/unsafe content.
- JavaScript tests or static assertions for clearing stale account values and
  sequencing account refreshes.
- Preflight tests proving account, clock, and quote failures are attributed
  independently.
- Focused API/security/static tests, then the complete pytest suite.
- Browser checks at desktop and mobile width, console-log inspection, and a
  final screenshot of the authenticated paper console.
- Fresh credentialed preflight and broker truth check before claiming a
  runnable trading mode.

## Self-review

There are no placeholders. The UI only presents data the server already has or
can read from Alpaca. It does not add performance charts without history,
autonomous orders, live execution, or a safety bypass. Failure states clear
stale values, private runtime artifacts stay out of Git, and the launch claim
is tied to fresh broker evidence.
