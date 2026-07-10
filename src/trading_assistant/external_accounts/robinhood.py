"""Robinhood READ-ONLY data source via robin_stocks.

Only read functions are called — build_holdings, profile loads, and the *get*
(never *order_*) order/dividend history. There is no code path here that places,
modifies, or cancels anything. See the package docstring for the hard non-goals.

Robinhood periodically changes their device-verification flow and breaks
robin_stocks; auth failures raise a readable ExternalAuthError telling you to
update the library rather than a raw stack trace. Pin is compatible (>=3.4,<4).
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal

from .base import ExternalAccountSummary, ExternalAuthError, ExternalPosition

log = logging.getLogger(__name__)

_UPDATE_HINT = (
    "Robinhood login failed. Robinhood periodically changes their "
    "device-verification flow and breaks robin_stocks — update it "
    "(`uv pip install -U robin_stocks`) and try again. Original cause hidden."
)


class RobinhoodSource:
    source_name = "robinhood"

    def __init__(
        self, username: str, password: str, totp_secret: str, token_path: str
    ) -> None:
        self._username = username
        self._password = password
        self._totp_secret = totp_secret
        self._token_path = token_path
        self._logged_in = False

    # ── auth ───────────────────────────────────────────────────
    def _login(self) -> None:
        if self._logged_in:
            return
        try:
            import pyotp
            import robin_stocks.robinhood as rh
        except ImportError as exc:
            raise ExternalAuthError(
                "robin_stocks/pyotp not installed — `uv pip install -e '.[external]'`"
            ) from exc

        code = pyotp.TOTP(self._totp_secret).now() if self._totp_secret else None
        try:
            self._do_login(rh, code)
        except Exception as exc:  # noqa: BLE001 — deliberately opaque, readable message
            log.warning("robinhood auth failed: %s", type(exc).__name__)
            raise ExternalAuthError(_UPDATE_HINT) from exc

        self._secure_token()
        self._logged_in = True

    def _do_login(self, rh, code) -> None:
        directory = os.path.dirname(self._token_path) or "."
        name = os.path.basename(self._token_path)
        try:
            rh.login(
                self._username,
                self._password,
                mfa_code=code,
                store_session=True,
                pickle_path=directory,
                pickle_name=name,
            )
        except TypeError:
            # Older robin_stocks without pickle_path/pickle_name kwargs.
            rh.login(self._username, self._password, mfa_code=code, store_session=True)

    def _secure_token(self) -> None:
        try:
            if os.path.exists(self._token_path):
                os.chmod(self._token_path, 0o600)
        except OSError:
            log.warning("could not chmod 0600 the Robinhood token file")

    # ── read-only fetches ──────────────────────────────────────
    def get_positions(self) -> list[ExternalPosition]:
        self._login()
        import robin_stocks.robinhood as rh

        holdings = rh.account.build_holdings() or {}
        out: list[ExternalPosition] = []
        for ticker, h in holdings.items():
            out.append(
                ExternalPosition(
                    ticker=ticker,
                    quantity=Decimal(str(h.get("quantity", "0") or "0")),
                    avg_cost=Decimal(str(h.get("average_buy_price", "0") or "0")),
                    current_price=Decimal(str(h.get("price", "0") or "0")),
                    source="robinhood",
                )
            )
        return out

    def get_account_summary(self) -> ExternalAccountSummary:
        self._login()
        import robin_stocks.robinhood as rh

        acct = rh.profiles.load_account_profile() or {}
        port = rh.profiles.load_portfolio_profile() or {}
        equity = port.get("equity") or port.get("market_value") or "0"
        return ExternalAccountSummary(
            total_equity=Decimal(str(equity or "0")),
            cash=Decimal(str(acct.get("cash", "0") or "0")),
            buying_power=Decimal(str(acct.get("buying_power", "0") or "0")),
            source="robinhood",
        )

    def get_order_history(self, days: int = 30) -> list[dict]:
        self._login()
        import robin_stocks.robinhood as rh

        orders = rh.orders.get_all_stock_orders() or []
        return [
            {
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "state": o.get("state"),
                "quantity": o.get("quantity"),
                "created_at": o.get("created_at"),
                "source": "robinhood",
            }
            for o in orders
        ]

    def get_dividends(self, days: int = 90) -> list[dict]:
        self._login()
        import robin_stocks.robinhood as rh

        divs = rh.account.get_dividends() or []
        return [
            {"amount": d.get("amount"), "state": d.get("state"), "source": "robinhood"}
            for d in divs
        ]
