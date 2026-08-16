"""Direct tests of the PaymentProvider abstraction (app/payments.py) —
independent of app/subscriptions.py's higher-level usage in
test_subscriptions.py. This closes the "payment-provider abstraction
tests" category explicitly called out for Phase 4-8 verification."""
import pytest

from app.payments import ManualPaymentProvider, PaymentIntent, get_provider


def test_get_provider_returns_manual_by_default():
    provider = get_provider()
    assert isinstance(provider, ManualPaymentProvider)
    assert provider.name == "manual"


def test_get_provider_unknown_name_raises_not_fabricated():
    """No live provider exists yet — get_provider() must fail loudly for
    anything but 'manual', never silently pretend a provider exists."""
    with pytest.raises(NotImplementedError):
        get_provider("stripe")


def test_manual_create_payment_returns_intent_with_no_checkout_url():
    provider = ManualPaymentProvider()
    intent = provider.create_payment(subscriber_id="sub-1", plan_id="MONTHLY", amount=49.0, currency="USD")
    assert isinstance(intent, PaymentIntent)
    assert intent.provider == "manual"
    assert intent.provider_payment_id.startswith("manual-")
    assert intent.amount == 49.0
    assert intent.currency == "USD"
    assert intent.checkout_url is None  # no processor, no redirect


def test_manual_create_payment_ids_are_unique():
    provider = ManualPaymentProvider()
    ids = {provider.create_payment(subscriber_id="s", plan_id="p", amount=1, currency="USD").provider_payment_id
           for _ in range(20)}
    assert len(ids) == 20


def test_manual_verify_payment_never_auto_confirms():
    """The whole point of ManualPaymentProvider: nothing in this class can
    move a payment to CONFIRMED on its own. Only an admin action
    (subscriptions.confirm_payment, called from app/admin.py) can."""
    provider = ManualPaymentProvider()
    with pytest.raises(NotImplementedError):
        provider.verify_payment("manual-anything")


def test_manual_get_payment_status_is_always_pending():
    provider = ManualPaymentProvider()
    assert provider.get_payment_status("manual-anything") == "PENDING"


def test_manual_process_webhook_not_supported():
    provider = ManualPaymentProvider()
    with pytest.raises(NotImplementedError):
        provider.process_webhook(b"{}", {})


def test_manual_refund_not_supported():
    provider = ManualPaymentProvider()
    with pytest.raises(NotImplementedError):
        provider.refund("manual-anything")
