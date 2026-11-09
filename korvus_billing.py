#!/usr/bin/env python3
"""
==============================================================================
 KORVUS BILLING  ·  Stripe subscriptions   (by BlackCrownVxJ.LLC)
==============================================================================
 Turns the free/pro tier into a real paid membership using Stripe Checkout.
 Checkout is STRIPE-HOSTED: members enter their card on Stripe's page, not
 ours, so raw card data never touches this server (keeps us out of PCI scope).

   /upgrade  --"Go Pro"-->  /api/billing/checkout  -->  Stripe Checkout page
        ^                                                      | pays
        |  success_url / cancel_url                            v
        +-----------------------  Stripe webhook  -->  /api/billing/webhook
                                  (signature-verified)        |
                                                              v
                                          auth.apply_subscription() -> tier=pro

 SETUP (all via .env, no code change, same pattern as the quotes providers):
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
 and this module never sees or stores a card number; Stripe handles all of it.
 Until STRIPE_SECRET_KEY + STRIPE_PRICE_ID are set (and `stripe` is installed),
 billing is simply "disabled" and the upgrade page shows a graceful notice.
==============================================================================
"""
import os
import datetime as dt
from dotenv import load_dotenv
import korvus_auth as auth
import korvus_notify as notify

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
        print("  [billing] the 'stripe' package isn't installed. Run: pip install stripe")
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


# ----------------------------------------------------------------------------
# ACCOUNT DELETION GATE  (no surprise bills on an account that no longer exists)
# ----------------------------------------------------------------------------
# Any of these statuses can still produce a charge (or resume into one), so an
# account carrying one of them can never be self-deleted. Only 'canceled' and
# 'incomplete_expired' are terminal in Stripe and safe.
BILLABLE_STATUSES = {"active", "trialing", "past_due", "unpaid", "incomplete", "paused"}


def deletion_block(user):
    """Decide whether this account may be deleted right now.
    Returns (blocked: bool, reason: str, ends_at: iso|None).

    Checks Stripe LIVE, never just the local copy (a missed webhook must not
    let a paying subscription outlive its account). Fails CLOSED: if the user
    has ever had a Stripe customer and we cannot confirm their subscriptions,
    deletion is blocked until we can."""
    user = user or {}
    cid = user.get("stripe_customer_id")
    if not cid:
        # Checkout always creates + stores the customer first, so no customer
        # id means Stripe has nothing it could ever bill for this account.
        return False, "", None
    stripe = _stripe()
    if not stripe:
        return (True, "We can't reach billing to confirm you have no active "
                      "subscription, so deletion is paused for your protection. "
                      "Please try again later or contact support.", None)
    try:
        subs = stripe.Subscription.list(customer=cid, status="all", limit=100)
        live = [x for x in subs.auto_paging_iter() if x.get("status") in BILLABLE_STATUSES]
    except Exception as e:
        print(f"  [billing] deletion check failed for {user.get('username')}: {e}")
        return (True, "We couldn't confirm your subscription status with Stripe "
                      "just now, so deletion is paused for your protection. "
                      "Please try again in a few minutes.", None)
    if not live:
        return False, "", None

    sub = live[0]
    status = sub.get("status")
    cpe = sub.get("current_period_end")
    if cpe is None:
        try:
            cpe = sub["items"]["data"][0].get("current_period_end")
        except (KeyError, IndexError, TypeError):
            cpe = None
    ends = _iso(sub.get("cancel_at") or cpe)
    if status in ("active", "trialing") and sub.get("cancel_at_period_end"):
        return (True, "Your Operator membership is already canceled and won't "
                      "renew. It stays active until the end of the period you "
                      "paid for, and you can delete your account once it ends.", ends)
    if status in ("active", "trialing"):
        return (True, "You have an active Operator subscription. Cancel it in "
                      "Manage billing first so nothing renews, then come back "
                      "here once it has ended.", ends)
    return (True, f"Your subscription is in '{status.replace('_', ' ')}' status, "
                  "which can still produce a charge. Open Manage billing to "
                  "settle or cancel it first.", ends)


def remove_payment_methods(user):
    """Detach every saved card from the Stripe customer, so even a mistake
    elsewhere could never charge a deleted account. Invoices and receipts
    stay in Stripe for the business's records. Best-effort; returns count."""
    cid = (user or {}).get("stripe_customer_id")
    stripe = _stripe()
    if not (cid and stripe):
        return 0
    n = 0
    try:
        for pm in stripe.PaymentMethod.list(customer=cid, limit=100).auto_paging_iter():
            try:
                stripe.PaymentMethod.detach(pm["id"])
                n += 1
            except Exception as e:
                print(f"  [billing] detach {pm.get('id')} failed: {e}")
        stripe.Customer.modify(cid, metadata={"korvus_account": "deleted"})
    except Exception as e:
        print(f"  [billing] payment-method cleanup failed for {user.get('username')}: {e}")
    return n


def _iso(ts):
    try:
        return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).isoformat()
    except Exception:
        return None


def handle_webhook(payload: bytes, sig_header: str):
    """Verify + process a Stripe webhook. Returns (http_status, message).
    The signature check is mandatory: unsigned / forged events are rejected."""
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

    def _notify_transition(res):
        """Ping Discord only when the tier actually changed. Overlapping Stripe
        events (checkout.session.completed + customer.subscription.created for
        the same purchase) are naturally deduplicated: whichever lands first
        flips the tier, the second sees no transition and stays silent."""
        if not res:
            return
        if res["old_tier"] != "pro" and res["new_tier"] == "pro":
            notify.member_went_pro(res["username"], res["email"],
                                   res["tv_username"], res["status"])
        elif res["old_tier"] == "pro" and res["new_tier"] != "pro":
            notify.member_pro_ended(res["username"], res["email"],
                                    res["tv_username"], res["status"])

    try:
        if typ == "checkout.session.completed":
            customer = obj.get("customer")
            username = obj.get("client_reference_id")
            sub_id   = obj.get("subscription")
            if username and customer:
                auth.set_stripe_customer(username, customer)   # link on first purchase
            res = auth.apply_subscription(customer, "active", sub_id, None)
            _notify_transition(res)
            print(f"  [billing] checkout completed -> pro: {(res or {}).get('username') or username}")
        elif typ in ("customer.subscription.created",
                     "customer.subscription.updated",
                     "customer.subscription.deleted"):
            customer = obj.get("customer")
            status   = "canceled" if typ.endswith("deleted") else obj.get("status")
            sub_id   = obj.get("id")
            # Newer Stripe API versions moved current_period_end off the
            # subscription object onto its line items, so fall back to the item.
            cpe_ts = obj.get("current_period_end")
            if cpe_ts is None:
                try:
                    cpe_ts = obj["items"]["data"][0].get("current_period_end")
                except (KeyError, IndexError, TypeError):
                    cpe_ts = None
            cpe      = _iso(cpe_ts)
            res      = auth.apply_subscription(customer, status, sub_id, cpe)
            _notify_transition(res)
            print(f"  [billing] {typ} -> {status} for {(res or {}).get('username') or customer}")
    except Exception as e:
        # log, but still 200 so Stripe doesn't retry forever on an internal hiccup
        print(f"  [billing] webhook handling error: {e}")
    return 200, "ok"
