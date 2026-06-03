#!/usr/bin/env python3
"""
==============================================================================
 KORVUS BILLING  ·  Stripe subscriptions   (by BlackCrownVxJ.LLC)
==============================================================================
 Turns the free/pro tier into a real paid membership using Stripe Checkout.
 Checkout is STRIPE-HOSTED — members enter their card on Stripe's page, not
 ours, so raw card data never touches this server (keeps us out of PCI scope).

   /upgrade  --"Go Pro"-->  /api/billing/checkout  -->  Stripe Checkout page
        ^                                                      | pays
        |  success_url / cancel_url                            v
        +-----------------------  Stripe webhook  -->  /api/billing/webhook
                                  (signature-verified)        |
                                                              v
                                          auth.apply_subscription() -> tier=pro

 SETUP (all via .env — no code change, same pattern as the quotes providers):
   pip install stripe
   STRIPE_SECRET_KEY=sk_live_...        (sk_test_... while testing)
   STRIPE_PRICE_ID=price_...            a RECURRING price on your Pro product
   STRIPE_WEBHOOK_SECRET=whsec_...      from the webhook endpoint you create
   STRIPE_PRICE_DISPLAY=$29 / month     (optional; shown on the upgrade page)
   SITE_URL=https://korvus.industries   (already used by auth)

 In the Stripe Dashboard:
   1. Create a Product + recurring Price -> copy the price id to STRIPE_PRICE_ID
   2. Developers > Webhooks > Add endpoint:  https://<site>/api/billing/webhook
        events: checkout.session.completed, customer.subscription.created,
                customer.subscription.updated, customer.subscription.deleted
      -> copy the signing secret into STRIPE_WEBHOOK_SECRET

 IMPORTANT: secrets live ONLY in .env on the server. Nothing is hardcoded here,
 and this module never sees or stores a card number — Stripe handles all of it.
 Until STRIPE_SECRET_KEY + STRIPE_PRICE_ID are set (and `stripe` is installed),
 billing is simply "disabled" and the upgrade page shows a graceful notice.
==============================================================================
"""
import os
import datetime as dt
from dotenv import load_dotenv
import korvus_auth as auth

load_dotenv()

STRIPE_SECRET_KEY     = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_PRICE_ID       = os.getenv("STRIPE_PRICE_ID", "").strip()
STRIPE_PRICE_ID_YEARLY = os.getenv("STRIPE_PRICE_ID_YEARLY", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_PRICE_DISPLAY  = os.getenv("STRIPE_PRICE_DISPLAY", "").strip()
STRIPE_PRICE_DISPLAY_YEARLY = os.getenv("STRIPE_PRICE_DISPLAY_YEARLY", "").strip()
SITE_URL              = os.getenv("SITE_URL", "https://korvus.industries").rstrip("/")


def _stripe():
    """Lazy import + configure. Returns the stripe module, or None if unavailable."""
    if not STRIPE_SECRET_KEY:
        return None
    try:
        import stripe
    except Exception:
        print("  [billing] the 'stripe' package isn't installed — run: pip install stripe")
        return None
    stripe.api_key = STRIPE_SECRET_KEY
    return stripe


def billing_enabled() -> bool:
    """True only when the secret key + price are set AND the library is importable."""
    return bool(STRIPE_SECRET_KEY and STRIPE_PRICE_ID and _stripe())


def yearly_enabled() -> bool:
    """True when an annual price is configured (and billing is otherwise live)."""
    return bool(STRIPE_SECRET_KEY and STRIPE_PRICE_ID_YEARLY and _stripe())


def price_display() -> str:
    return STRIPE_PRICE_DISPLAY or ""


def price_display_yearly() -> str:
    return STRIPE_PRICE_DISPLAY_YEARLY or ""


def _ensure_customer(stripe, user) -> str:
    """Return the user's Stripe customer id, creating + storing it on first use."""
    cid = user.get("stripe_customer_id")
    if cid:
        return cid
    cust = stripe.Customer.create(
        email=user.get("email"),
        metadata={"username": user.get("username", "")},
    )
    auth.set_stripe_customer(user.get("username"), cust.id)
    return cust.id


def create_checkout_url(user, interval="month"):
    """Create a subscription Checkout Session for this user, monthly or yearly.
    Returns (url, error); url is None on failure."""
    stripe = _stripe()
    if not stripe:
        return None, "Memberships aren't open yet."
    if interval == "year":
        if not STRIPE_PRICE_ID_YEARLY:
            return None, "The annual plan isn't available yet."
        price_id = STRIPE_PRICE_ID_YEARLY
    else:
        if not STRIPE_PRICE_ID:
            return None, "Memberships aren't open yet."
        price_id = STRIPE_PRICE_ID
    try:
        cid = _ensure_customer(stripe, user)
        sess = stripe.checkout.Session.create(
            mode="subscription",
            customer=cid,
            client_reference_id=user.get("username"),
            line_items=[{"price": price_id, "quantity": 1}],
            allow_promotion_codes=True,
            success_url=f"{SITE_URL}/upgrade?status=success",
            cancel_url=f"{SITE_URL}/upgrade?status=cancel",
        )
        return sess.url, None
    except Exception as e:
        print(f"  [billing] checkout error: {e}")
        return None, "Could not start checkout. Please try again."


def create_portal_url(user):
    """Create a Stripe Billing Portal session so the user can manage / cancel.
    Returns (url, error)."""
    stripe = _stripe()
    cid = user.get("stripe_customer_id")
    if not stripe:
        return None, "Billing isn't configured yet."
    if not cid:
        return None, "No billing account is linked to this user yet."
    try:
        ps = stripe.billing_portal.Session.create(
            customer=cid, return_url=f"{SITE_URL}/upgrade")
        return ps.url, None
    except Exception as e:
        print(f"  [billing] portal error: {e}")
        return None, "Could not open the billing portal."


def _iso(ts):
    try:
        return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).isoformat()
    except Exception:
        return None


def handle_webhook(payload: bytes, sig_header: str):
    """Verify + process a Stripe webhook. Returns (http_status, message).
    The signature check is mandatory — unsigned / forged events are rejected."""
    stripe = _stripe()
    if not stripe:
        return 503, "billing disabled"
    if not STRIPE_WEBHOOK_SECRET:
        return 500, "webhook secret not set"
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        print(f"  [billing] webhook verify failed: {e}")
        return 400, "bad signature"

    typ = event["type"]
    obj = event["data"]["object"]
    try:
        if typ == "checkout.session.completed":
            customer = obj.get("customer")
            username = obj.get("client_reference_id")
            sub_id   = obj.get("subscription")
            if username and customer:
                auth.set_stripe_customer(username, customer)   # link on first purchase
            updated = auth.apply_subscription(customer, "active", sub_id, None)
            print(f"  [billing] checkout completed -> pro: {updated or username}")
        elif typ in ("customer.subscription.created",
                     "customer.subscription.updated",
                     "customer.subscription.deleted"):
            customer = obj.get("customer")
            status   = "canceled" if typ.endswith("deleted") else obj.get("status")
            sub_id   = obj.get("id")
            cpe      = _iso(obj.get("current_period_end"))
            updated  = auth.apply_subscription(customer, status, sub_id, cpe)
            print(f"  [billing] {typ} -> {status} for {updated or customer}")
    except Exception as e:
        # log, but still 200 so Stripe doesn't retry forever on an internal hiccup
        print(f"  [billing] webhook handling error: {e}")
    return 200, "ok"
