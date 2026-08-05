# RISK DISCLOSURE — version 1.0

**[PLACEHOLDER — LAWYER REVIEW REQUIRED]**

This file is a versioned container, not legal advice and not final wording.
Do not ship it to customers as-is.

The technical framework around it is complete: acceptance is recorded per user
per document per version in `consent_records`, with a timestamp and the app
version. Bumping the version string in `services.REQUIRED_CONSENTS` will
re-prompt every customer at next sign-in.

## Points counsel should address

- The product is a POSITION-SIZING AND RISK-MANAGEMENT CALCULATOR
- It is NOT investment advice, NOT financial advice, NOT a trading signal service
- It does NOT guarantee profit, prevent loss, or assure any trading outcome
- Trading leveraged instruments carries substantial risk of loss
- Calculations are based on user-supplied inputs; the user is responsible for
  verifying every value before placing any trade
- The MT5 order-placement feature executes trades the USER initiates and
  confirms; the software makes no autonomous trading decisions
- Whether the seller requires any financial-services registration in the
  jurisdictions sold into
