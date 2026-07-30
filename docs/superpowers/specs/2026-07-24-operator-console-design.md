# Operator Console Design

## Subject and job

The product is a single-operator control room for an Alpaca paper-trading
assistant. Its primary job is not to make trading feel exciting. It is to let
the operator understand current truth, inspect a proposed action, and make a
deliberate decision without mistaking uncertainty for success.

The user has pre-approved the safety-foundation plan and asked execution to
continue without phase pauses. This design refines the already approved Task 8
brief; it does not broaden its scope.

## Considered directions

### 1. Chat-first assistant

Chat occupies most of the screen and operational controls sit beside it. This
is familiar, but it overemphasizes LLM prose and makes broker truth secondary.
That is the wrong hierarchy for a financial control surface.

### 2. Dense terminal dashboard

Every feed appears in a compact grid with terminal-like styling. It makes good
use of space, but high density can blur the difference between observed facts,
operator actions, and unresolved state.

### 3. Operations ledger with a truth rail — selected

The center presents pending decisions and execution receipts as a chronological
operations ledger. A persistent side rail shows only state the system can
currently prove: broker, paper mode, authenticated operator, daemon freshness,
and scoped breaker state. Chat and research remain available, but do not
visually outrank broker truth.

This direction is specific to the product's safety model and avoids the generic
dark-neon trading terminal treatment.

## Visual system

### Color tokens

- `Harbor ink #17212B`: primary text and structural rules.
- `Frost #F4F7FA`: page field.
- `Porcelain #FFFFFF`: working surfaces.
- `Instrument blue #2E5B88`: navigation, focus, and selected state.
- `Caution amber #A96012`: stale, incomplete, or pending confirmation.
- `Alarm red #A12B32`: unsafe state and destructive actions only.
- `Verified green #2F6B4F`: explicitly confirmed safe/success state only.

Colors never carry meaning alone; every state also has a label or icon-free
text marker.

### Typography

The strict self-only CSP rules out remote font dependencies.

- Display and section labels: `Avenir Next`, `Segoe UI`, sans-serif.
- Body and controls: `Inter`, `Avenir Next`, `Segoe UI`, sans-serif.
- Prices, IDs, timestamps, and status data:
  `SFMono-Regular`, `Cascadia Mono`, `Roboto Mono`, monospace.

The display face is used sparingly. Operational data is aligned and
tabular. Sentence case is used throughout.

### Layout

Desktop:

```text
┌────────────────────────────────────────────────────────────────────┐
│ Product / current page                         Session / Sign out   │
├───────────────┬────────────────────────────────────────────────────┤
│ Truth rail    │ Decision ledger                                    │
│ Paper mode    │ ┌ Pending proposal ──────────────────────────────┐ │
│ Broker        │ │ Exact order / expiry / exposure / actions       │ │
│ Daemon        │ └─────────────────────────────────────────────────┘ │
│ Breakers      │ Positions / activity / chat                        │
└───────────────┴────────────────────────────────────────────────────┘
```

Mobile:

```text
┌────────────────────────────┐
│ Page / session             │
├────────────────────────────┤
│ Truth rail (stacked)       │
├────────────────────────────┤
│ Pending decisions          │
├────────────────────────────┤
│ Positions / activity/chat  │
└────────────────────────────┘
```

The truth rail is the signature element. It is not decoration: it exposes the
assumptions required before any high-consequence action.

## Interaction contracts

### Session and requests

- Cookies remain HttpOnly; CSRF exists only in JavaScript module memory.
- A 401 redirects to `/login`.
- `recent_authentication_required` opens reauthentication, clears the entered
  secret before awaiting the request, and retries the original action once.
- Errors read the stable `error.code`, `error.message`, and `request_id`
  envelope. Provider text is never displayed.
- DOM content from broker, model, audit, or errors uses `textContent`, never
  `innerHTML`.

### Approval

Before approval, the confirmation surface shows:

- Alpaca broker;
- explicit paper mode;
- symbol, side, order type, quantity or notional, and limit price;
- proposal expiry;
- current position and resulting exposure based on the read-only server
  confirmation payload;
- the operator's non-empty reason.

The UI never calculates or guesses broker, mode, or exposure. Missing
confirmation data disables approval and explains what must be refreshed.

### Breaker reset

Reset is scoped to one asset class. It requires:

- currently observed positive generation;
- complete server health evidence;
- recent reauthentication;
- a non-empty operator reason.

There is no global reset shortcut.

### Panic

An explicit safe receipt is the only success state. An incomplete 503 receipt
renders:

- confirmed canceled broker IDs;
- unconfirmed local IDs;
- remaining or potentially open remote IDs;
- unsafe local fills, rules, and groups;
- unknown enumeration categories.

The critical unsafe banner persists for the page session until a later
explicitly safe receipt. Copy never says "everything halted."

## CSP and accessibility

- No inline script, style block, style attribute, or event handler.
- No `unsafe-inline`, remote asset, browser credential storage, or
  `X-API-Key`.
- Static JavaScript is split by page and shares one authenticated request
  module.
- Every control is keyboard reachable with a visible focus ring.
- Dialogs label their purpose, trap focus while open, close on Escape when that
  cannot trigger an action, and return focus to the invoking control.
- Status changes use polite live regions; unsafe panic state uses an assertive
  alert.
- Layout remains usable at 360 CSS pixels and respects reduced motion.

## Testing

- Parse every HTML page and reject inline scripts, styles, handlers, and style
  attributes.
- Search HTML/JavaScript for `localStorage`, `sessionStorage`, `X-API-Key`,
  `innerHTML`, and unsafe success copy.
- Exercise login clearing, session bootstrap, 401 redirect, reauthentication
  retry-once, reason validation, and stable error rendering.
- Verify approval confirmation contains exact server fields and disables itself
  when proof is incomplete.
- Verify panic success and incomplete receipts, including persistent unsafe
  state.
- Verify scoped reset uses the observed generation and cannot issue a global
  reset.
- Keep existing API, plan, backtest, security, and full-suite tests green.

## Self-review

The design has no placeholders or optional safety semantics. It stays within
Task 8: static assets, truthful action UX, and the minimal read-only response
data required for confirmation. It adds no live trading, auto-approval, remote
assets, or new execution path.
