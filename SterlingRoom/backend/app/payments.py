"""Payment abstraction (master-prompt §13) — interface only.

Per the explicit 2026-08-16 decision ("Not decided yet — build the
abstraction first"), THERE IS NO LIVE PAYMENT PROVIDER WIRED IN YET. This
module defines the interface every provider must implement, plus a
ManualPaymentProvider that mirrors StopLossPro Pro's existing pattern in
this same repo (Working/backend: `Licence.activation_note` — cash/crypto,
manually verified by an admin, no processor integration) as the only
currently-usable implementation. That keeps Sterling_Room launchable for a
manually-verified pilot without blocking on a provider decision, while
keeping the real integration (Stripe, PayPal, etc.) a drop-in swap later —
same "narrow interface, swap the implementation" hedge this repo's
PROJECT_STATUS.md §8 already uses for licensing-vendor risk.

Do NOT wire a Stripe/PayPal/etc. SDK call in here speculatively — that would
mean fabricating an integration against a provider nobody has chosen or
configured, which is explicitly against master-prompt §10 ("never fabricate
... integrations").
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class PaymentIntent:
    provider: str
    provider_payment_id: str
    amount: float
    currency: str
    checkout_url: str | None = None  # None for manual/offline flows


@dataclass
class PaymentVerification:
    verified: bool
    provider_payment_id: str
    detail: str = ""


class PaymentProvider(ABC):
    """Every provider implementation lives behind this interface.
    Payment confirmation must come from the provider, never from a client
    claim (master-prompt §13) — create_payment() starts a payment;
    verify_payment() is the only thing allowed to confirm one.
    """

    @abstractmethod
    def create_payment(self, *, subscriber_id: str, plan_id: str, amount: float, currency: str) -> PaymentIntent: ...

    @abstractmethod
    def verify_payment(self, provider_payment_id: str) -> PaymentVerification: ...

    @abstractmethod
    def get_payment_status(self, provider_payment_id: str) -> str: ...

    def process_webhook(self, payload: bytes, headers: dict) -> PaymentVerification:
        """Providers with real webhooks (Stripe, PayPal) override this.
        Not abstract because ManualPaymentProvider legitimately has none."""
        raise NotImplementedError(f"{type(self).__name__} does not support webhooks")

    def refund(self, provider_payment_id: str) -> bool:
        raise NotImplementedError(f"{type(self).__name__} does not support automated refunds")


class ManualPaymentProvider(PaymentProvider):
    """Cash/crypto, manually verified by an admin — same pattern as
    StopLossPro Pro's `Licence.activation_note` in this same repo. No
    processor call is ever made; create_payment() just allocates a reference
    id for the admin to reconcile against, and verify_payment() is meant to
    be called from the admin console once a human has actually checked the
    payment arrived (never auto-verified)."""

    name = "manual"

    def create_payment(self, *, subscriber_id: str, plan_id: str, amount: float, currency: str) -> PaymentIntent:
        return PaymentIntent(
            provider=self.name,
            provider_payment_id=f"manual-{uuid.uuid4().hex[:12]}",
            amount=amount,
            currency=currency,
            checkout_url=None,
        )

    def verify_payment(self, provider_payment_id: str) -> PaymentVerification:
        # Deliberately does not auto-confirm. An admin action (not built in
        # this pass — see the Sterling_Room audit's "admin dashboard" phase)
        # is what actually flips a Payment row to CONFIRMED.
        raise NotImplementedError(
            "ManualPaymentProvider.verify_payment is intentionally not automatic — "
            "confirm via the admin console once a human has checked the payment arrived."
        )

    def get_payment_status(self, provider_payment_id: str) -> str:
        return "PENDING"


def get_provider(name: str = "manual") -> PaymentProvider:
    if name == "manual":
        return ManualPaymentProvider()
    raise NotImplementedError(
        f"No PaymentProvider implementation for {name!r} yet — only 'manual' exists. "
        f"Wire a real provider here once one is chosen (see app/payments.py module docstring)."
    )
