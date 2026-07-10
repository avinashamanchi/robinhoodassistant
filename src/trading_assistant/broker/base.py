"""BrokerClient abstract interface. Implementations: MockBroker, AlpacaBroker (P2)."""

from __future__ import annotations

import abc
from typing import Optional

from .models import Account, OrderRequest, OrderResult, Position, Quote


class BrokerClient(abc.ABC):
    """The single seam the rest of the system talks to for market/account/order I/O.

    Implementations MUST honor idempotency: ``submit_order`` with an
    already-seen ``idempotency_key`` must not create a second live order — it
    returns the existing order's status instead (checked via get_order_status).
    """

    @abc.abstractmethod
    def get_quote(self, ticker: str) -> Quote: ...

    @abc.abstractmethod
    def get_account(self) -> Account: ...

    @abc.abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abc.abstractmethod
    def submit_order(self, order: OrderRequest) -> OrderResult: ...

    @abc.abstractmethod
    def get_order_status(self, order_id: str) -> OrderResult: ...

    @abc.abstractmethod
    def cancel_order(self, order_id: str) -> OrderResult: ...
