"""BrokerClient abstract interface. Implementations: MockBroker, AlpacaBroker (P2)."""

from __future__ import annotations

import abc
from typing import Optional

from .models import Account, OrderRequest, OrderResult, Position, Quote


class BrokerSubmissionRejected(RuntimeError):
    """Broker definitively rejected a request without accepting an order."""

    def __init__(self, stable_code: str, message: str = "") -> None:
        super().__init__(message or stable_code)
        self.stable_code = stable_code


class BrokerAcceptanceUnknown(RuntimeError):
    """A request may have been sent, but broker acceptance is unproven."""


class BrokerDataIntegrityError(ValueError):
    """Broker payload cannot be trusted as execution or reconciliation truth."""

    def __init__(
        self,
        message: str,
        *,
        broker_order_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.broker_order_id = broker_order_id


class BrokerClient(abc.ABC):
    """The single seam the rest of the system talks to for market/account/order I/O.

    Implementations MUST honor idempotency: ``submit_order`` with an
    already-seen ``idempotency_key`` must not create a second live order — it
    returns the existing order's status instead (checked via get_order_status).
    """

    reconciliation_key = "broker"

    @abc.abstractmethod
    def get_quote(self, ticker: str) -> Quote: ...

    @abc.abstractmethod
    def get_account(self) -> Account: ...

    @abc.abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abc.abstractmethod
    def submit_order(self, order: OrderRequest) -> OrderResult: ...

    @abc.abstractmethod
    def get_order_by_client_id(self, client_order_id: str) -> OrderResult | None: ...

    @abc.abstractmethod
    def get_open_orders(self) -> list[OrderResult]: ...

    @abc.abstractmethod
    def get_order_status(self, order_id: str) -> OrderResult: ...

    @abc.abstractmethod
    def cancel_order(self, order_id: str) -> OrderResult: ...
