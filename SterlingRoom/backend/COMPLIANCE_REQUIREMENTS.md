# Sterling_Room — Compliance & Business-Model Gate

**This is not a coding task, and this document is not legal advice.**
Nothing below should be read as a claim that Sterling_Room is legally
compliant, that any disclaimer text makes it compliant, or that
Anthropic/Claude has assessed its regulatory status. This document does
one thing: lists the specific questions that need a real answer — from
the business owner, and in most cases from a licensed lawyer in the
relevant jurisdiction(s) — before Sterling_Room accepts a single real
payment from a paying customer. Engineering work (rate limiting, security
hardening, testing, deployment prep) can and did proceed in parallel; this
gate is independent of all of it and does not close because the
engineering work is done.

## Why this gate exists

Sterling_Room's product, as built, sends explicit trading calls (BUY
BTCUSD, entry range, stop loss, TP1/2/3) to paying subscribers over
Telegram, across Forex/Commodities/Crypto instruments. Depending on the
jurisdictions involved and exactly how the service is described and
delivered, this can implicate securities/commodities/financial-services
regulation, investment-adviser or broker-dealer registration regimes,
marketing and solicitation rules, and consumer-protection law (refunds,
cancellation rights, clear pricing). Which of those actually apply — and
what's required to comply — cannot be determined from the code. It
depends on facts only the business owner has.

## Open questions — answer these before accepting real payments

1. **Target customer jurisdictions.** Which countries/states will
   Sterling_Room actually market to and accept subscribers from? "Anywhere"
   is not free — it multiplies the number of regimes to check, not
   simplifies it. A geo-restriction (blocking sign-up from unsupported
   jurisdictions) may end up being part of the answer, not just a legal
   footnote.

2. **Business entity jurisdiction.** What legal entity will operate
   Sterling_Room (LLC, corporation, sole proprietorship — and where
   incorporated/registered)? Regulatory exposure and tax treatment both
   depend on this, and it affects which jurisdiction's rules govern the
   Terms of Service.

3. **Exact instruments/products covered.** The three categories mentioned
   (Forex, Commodities, Crypto) are regulated very differently in most
   jurisdictions — crypto in particular is a fast-moving regulatory area.
   Confirm the exact instrument list, since "we also added indices" or "we
   also cover options" later changes the analysis.

4. **Individualized vs. general recommendations.** Are calls the same for
   every subscriber (general market commentary), or ever tailored to an
   individual's account, risk tolerance, or financial situation?
   Individualized advice is treated very differently — and much more
   strictly — than general/one-to-many commentary in most regulatory
   frameworks.

5. **Educational/analytical/advisory/research/signal-based framing.** How
   is the service actually described to customers, in marketing and in the
   product itself — "signals," "trade ideas," "educational content,"
   "research," "advice"? The words used matter regulatorily, not just as
   marketing copy, and the actual product behavior (explicit BUY/SELL calls
   with entries and stops) needs to match whatever framing is chosen —
   calling something "educational" while delivering actionable individual
   trade instructions is itself a compliance risk, not a workaround.

6. **Regulatory registration needs.** Does this activity require
   registration as an investment adviser, commodity trading advisor,
   introducing broker, or similar, in the target jurisdictions? This is
   the single most consequential open question and the one most likely to
   require a lawyer's answer, not an engineering one.

7. **Applicable financial-services rules.** Beyond registration: consumer
   financial protection law, unfair/deceptive practices rules, and
   sector-specific rules for forex/commodities/crypto marketing in each
   target jurisdiction.

8. **Required disclosures.** Risk-of-loss disclosures, past-performance
   disclaimers ("past results do not guarantee future returns"), and
   any jurisdiction-mandated disclosure language for trading-signal or
   advisory services.

9. **Terms of Service.** Governing law, limitation of liability, dispute
   resolution, acceptable use, account termination — see the template
   note below.

10. **Refund policy.** Subscription cancellation and refund terms —
    Sterling_Room's `Subscription` state machine already supports
    `CANCELLED`/`REVOKED` cleanly (see `app/subscriptions.py`), but the
    actual refund *policy* (pro-rated? none after N days? none once calls
    were delivered?) is a business decision this document does not make.

11. **Privacy policy.** What subscriber data is collected (Telegram user
    ID, username, payment records, support tickets — see `app/models.py`)
    and how it's used/retained/shared needs a policy that matches what the
    code actually does, not a generic template that overpromises or
    underdiscloses.

12. **Risk disclosures.** Trading-specific risk warnings appropriate to
    the instruments covered (§3) and the jurisdictions targeted (§1).

13. **Marketing restrictions.** Some jurisdictions restrict how trading
    signal/advisory services can advertise (e.g. prohibitions on
    guaranteed-return language, required disclaimers on social media
    promotion). Whatever channels are used to market Sterling_Room need to
    be checked against this before launch, not after.

14. **Recordkeeping requirements.** Some regulatory regimes for
    advisory-adjacent services impose specific recordkeeping/retention
    obligations (which may exceed or differ from the operational backup
    retention in `DEPLOYMENT.md` §3). Confirm whether any apply and whether
    the current audit trail (`AuditEvent`, `CallEvent`, `Payment` history —
    all already immutable/append-only in this schema) satisfies them.

## What Sterling_Room's engineering already gives you

Not a substitute for legal review, but relevant inputs to it:

- Every call, subscription, and payment event is append-only and
  auditable (`app/models.py`: `AuditEvent`, `CallEvent`,
  `Subscription`/`Payment` history) — useful for recordkeeping (§14)
  regardless of which regime applies.
- No subscription is ever activated on an unverified client-side claim —
  only `subscriptions.py::confirm_payment()` (admin-confirmed or, in
  future, a verified provider webhook) can activate one. Relevant to any
  future dispute-resolution or consumer-protection review.
- Telegram-side data collected is minimal: Telegram user ID, username,
  and what's needed to operate the subscription (see `TelegramUser`,
  `Subscriber` in `app/models.py`) — worth listing explicitly in a real
  privacy policy rather than guessing at scope.

## Document templates — status

Per master-prompt §14, at minimum the following documents are needed
before real payments: **Terms of Service, Privacy Policy, Risk
Disclosure, Subscription/Refund Policy, Contact/Support info.** None are
drafted in this repository. Producing boilerplate templates here — before
the fourteen questions above are answered — risks producing legally
inaccurate or contradictory language (e.g. a generic ToS that claims a
jurisdiction or entity structure that turns out to be wrong), which is
worse than no document at all. Once the business owner has answered
§1–§7, template drafts for the remaining documents can be prepared for
professional legal review; they should be treated as **drafts requiring
review**, never as final compliant documents, regardless of source.

## Launch gate

**Do not accept real payments from paying customers until:**

1. Questions 1–8 above have real, specific answers from the business
   owner.
2. A licensed lawyer in the relevant jurisdiction(s) (per §1–§2) has
   reviewed the business model and confirmed what registration/compliance
   steps, if any, are required — and those steps are actually completed,
   not just identified.
3. Terms of Service, Privacy Policy, Risk Disclosure, and a Refund Policy
   are drafted, reviewed, and published somewhere a subscriber can read
   them before paying.

`PAYMENT_PROVIDER=manual` staying the default, and no real payment
processor being wired in (`PAYMENT_PROVIDER_DECISION.md`), already means
Sterling_Room cannot process a real card/crypto payment today without
further engineering work *and* this gate being cleared. That is
intentional, not an oversight.
