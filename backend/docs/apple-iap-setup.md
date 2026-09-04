# Apple In-App Purchase setup

What has to exist outside this repo before an iOS build can sell anything, in
the order it is needed. The code is written and compiles; none of it can be
exercised until steps 1 and 2 are done.

Why iOS sells differently at all: App Store Guideline 3.1.1 forbids sending
users to an outside payment page for digital content. The carve-out that
currently permits it covers only the United States storefront, exists because of
the Epic injunction, and is under Supreme Court review. Aura ships to every
storefront, so the Dodo web checkout is switched off on iOS entirely and
StoreKit sells there instead. Every other surface is unchanged.

## 1. Paid Applications agreement

App Store Connect → Business (formerly Agreements, Tax, and Banking).

Nothing below can be created until this is **Active**. It needs a bank account
and completed tax forms, and Apple validates both, so start it first. This is
the long pole in the whole iOS launch.

## 2. Subscription products

App Store Connect → your app → Subscriptions.

Create **one subscription group** holding all four products, so users can move
between tiers instead of holding two subscriptions at once. Set Pro above
Companion in the group's level order, which is what makes Companion → Pro an
immediate upgrade and Pro → Companion a downgrade at period end.

| Product ID | Tier | Duration | Current web price |
|---|---|---|---|
| `com.varundevs.aura.companion.monthly` | Companion | 1 month | $19.99 |
| `com.varundevs.aura.companion.yearly` | Companion | 1 year | $191 |
| `com.varundevs.aura.pro.monthly` | Pro | 1 month | $34.99 |
| `com.varundevs.aura.pro.yearly` | Pro | 1 year | $335 |

The product IDs are not configuration. They are compiled into
`lib/data/services/store_purchase_service.dart` and defaulted in
`backend/src/config/settings.py`, and Apple treats them as immutable once
created, so they must match exactly and character-for-character.

Pick the nearest Apple price point to each web price. They will not match
exactly and do not need to.

## 3. Server notification endpoint

App Store Connect → your app → General → App Information → App Store Server
Notifications. Set **Version 2** and point both URLs at:

```
https://juno-backend-620715294422.us-central1.run.app/billing/apple/notifications
```

Use the sandbox URL field for sandbox. There is no shared secret to configure:
Apple signs every payload, and the backend verifies it against the Apple Root CA
G3 certificate committed at `backend/src/resources/apple`. That is also why
there is nothing here to rotate.

Use "Request a Test Notification" once the URL is set. The endpoint answers
`{"status":"ok"}` to a TEST notification and logs it.

## 4. Backend configuration

One setting is required, in Cloud Run env:

| Setting | Value |
|---|---|
| `APPLE_IAP_APP_APPLE_ID` | the numeric App Store id, from App Store Connect → App Information |
| `APPLE_IAP_ENVIRONMENT` | `Production`, or `Sandbox` for a sandbox-only revision |

Until `APPLE_IAP_APP_APPLE_ID` is set, `settings.apple_iap_configured` is false
and both Apple routes answer 503 rather than trusting a payload they cannot
fully verify. That is deliberate: Apple's verifier checks the app id, and a
half-verified purchase must never reach an entitlement.

Everything else has a working default: the bundle id and the four product ids.

## 5. Sandbox testing

App Store Connect → Users and Access → Sandbox → Test Accounts. Then on the
iPhone, Settings → Developer → Sandbox Apple Account.

Exercise all of these, and confirm the entitlement document actually changes in
Firestore rather than trusting the UI:

- purchase Companion monthly, then Pro monthly (an upgrade inside the group)
- cancel, and confirm access runs to the period end rather than stopping
- let a renewal fail, and confirm grace period rather than immediate expiry
- delete and reinstall the app, then Restore Purchases
- purchase on iOS while a web subscription is active on the same account, and
  confirm neither seller's later events revoke the other's access

That last one is the case the `source` field on the entitlement document exists
for. See `source_may_apply_non_activating` in `backend/src/services/entitlement.py`.

Sandbox subscriptions renew on an accelerated clock, so a one-month product
renews every few minutes. That is how to see `DID_RENEW` without waiting.

## What is not built

- No use of the App Store Server API to query subscription status on demand. The
  backend learns about changes from notifications and from the transaction the
  app posts. If a notification is missed and the user never reopens the app, the
  entitlement can lag. Adding the API would need a `.p8` key, an issuer id, and
  a key id.
- The client posts its transaction on purchase and restore. If Apple sends a
  notification for a subscription the backend has never seen, it answers 500 so
  Apple redelivers; Apple retries for days, and the mapping lands as soon as the
  app is opened.
