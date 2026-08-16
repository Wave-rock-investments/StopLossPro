# Sterling_Room — Payment Provider Decision

This is a decision for you, not something to choose autonomously — per
explicit instruction, and consistent with the standing 2026-08-16 "build
the abstraction first" decision. This document lists exactly what each
realistic option requires, so the decision can be made with full
information. `PAYMENT_PROVIDER=manual` (`ManualPaymentProvider`) stays
the active default regardless, and stays fully usable for testing and
for manual-fulfillment launches no matter which path is chosen.

## The abstraction, briefly

Every provider implements `app/payments.py::PaymentProvider`:
`create_payment()`, `verify_payment()`, `get_payment_status()`, optionally
`process_webhook()` and `refund()`. `subscriptions.py`'s
`confirm_payment()` is the *only* function that ever activates a
subscription, and it's provider-agnostic — it doesn't matter whether a
`Payment` row got to `CONFIRMED` via an admin click (manual) or a
provider webhook (a real processor). Adding a provider means writing one
new class in `app/payments.py` and wiring its webhook (if any) to a new
route that calls `confirm_payment()` — it does not touch
`app/subscriptions.py`, `app/bot.py`, or `app/admin.py`.

## Option A — Stay manual-only for launch

**What's required from you:** nothing beyond what already exists. You
confirm payment receipt yourself (bank transfer, crypto wallet, cash,
whatever you already use) and click "Confirm" in `/admin/payments`.

**Tradeoffs:** zero integration work, zero processor fees, but every
payment needs a human to notice it and confirm it — no instant
activation, and it doesn't scale past a level where you can personally
track incoming payments. Fine for an initial pilot; a real constraint
past a certain subscriber count.

## Option B — Stripe

**What's required from you:**
- A Stripe account (business details, bank account for payouts, tax
  info — Stripe's own onboarding, not something this codebase can do
  for you).
- A Stripe API secret key (`sk_live_...` for production,
  `sk_test_...` for staging) — goes in the secret manager, never
  committed.
- Stripe Products + Prices configured for each plan (or created
  dynamically via the API — a design choice for whoever implements
  `StripePaymentProvider`).
- A webhook signing secret (`whsec_...`) for verifying
  `checkout.session.completed` / `invoice.paid` events.
- A decision on Checkout (Stripe-hosted payment page, less code, less
  control over UX) vs. Payment Intents (more control, more compliance
  surface you own — e.g. PCI scope).
- Recurring billing requires Stripe Subscriptions or a
  Checkout-in-recurring-mode setup, mapped onto Sterling_Room's own
  `Plan`/`Subscription` model (Stripe's subscription lifecycle and
  Sterling_Room's need to be reconciled explicitly, not assumed to match
  1:1).

**Engineering work implied (not started):** `StripePaymentProvider`
class, a `POST /payments/webhook/stripe` route with signature
verification, a mapping from Stripe price IDs to Sterling_Room `Plan`
rows.

## Option C — PayPal

Similar shape to Stripe: a PayPal Business account, REST API
credentials (client ID + secret), webhook configuration and signature
verification, and a decision on PayPal Subscriptions vs. one-off
payments repeated manually per billing cycle.

## Option D — Crypto payment processor (e.g. a hosted crypto checkout)

**What's required from you:** an account with the chosen processor,
its API key, and its webhook/callback verification mechanism (varies by
processor — some use HMAC signatures, some use on-chain confirmation
polling). Crypto-native processors often have simpler compliance
requirements than card processors but different reliability
characteristics (confirmation times, chain reorgs) that
`verify_payment()`'s implementation would need to account for
explicitly (e.g. requiring N confirmations before treating a payment as
final).

## What to send back, whichever you choose

1. Which option (A/B/C/D, or something not listed here).
2. If B/C/D: the account/API credentials, once the account itself is
   set up (do not send raw secret values in chat — use the production
   secret manager once a host exists, or a clearly-marked placeholder
   here and the real value only at deploy time).
3. Whether recurring billing should auto-renew (processor-driven) or
   continue as Sterling_Room's current model (each renewal is its own
   `Payment`, confirmed the same way as the first payment) — this
   changes how `subscriptions.py::renew_subscription` would need to be
   invoked (webhook-driven vs. the current PENDING_PAYMENT → confirm
   flow).
