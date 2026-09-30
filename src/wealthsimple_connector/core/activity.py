"""Small allowlist for persisted Wealthsimple activity feed rows."""

from datetime import datetime, timezone
import hashlib

from wealthsimple_connector.core.portfolio import account_nickname, number, reference, text


# Bump when saved activity rows or their coverage record change shape; the next sync
# then reads all history again. 3: coverage gaps are kept per account. 4: kind=spending
# holds purchases and refunds (debit refunds were counted as spending, card refunds missed).
ACTIVITY_SCHEMA = 4
INVESTMENT_ACCOUNTS = {"tfsa", "fhsa", "rrsp", "non_registered",
                       "non_registered_crypto"}

# The vocabulary below was read from a real activity feed (type, subType, status and
# amountSign only). A value not listed here stays "other" or "unknown", never a purchase.
CARD_SUBTYPES = {"PURCHASE": "purchase", "REFUND": "refund", "PAYMENT": "card_payment"}
# Pre-authorized debits and bill payments: real outflows, but bills, not card purchases.
BILL_SUBTYPES = {"AFT", "BILL_PAY"}
TRANSFER_TYPES = {"DEPOSIT", "WITHDRAWAL", "INTERNAL_TRANSFER", "P2P_PAYMENT",
                  "INSTITUTIONAL_TRANSFER_INTENT"}
# For trades the sign is always positive, so it says nothing about which way cash moved.
TRADE_TYPES = {"DIY_BUY", "DIY_SELL", "CRYPTO_BUY", "CRYPTO_SELL", "FUNDS_CONVERSION"}
OUTCOMES = {"settled": "done", "completed": "done", "accepted": "done", "posted": "done",
            "filled": "done", "authorized": "pending", "pending": "pending",
            "rejected": "not_done", "cancelled": "not_done", "expired": "not_done",
            "failed": "not_done", "declined": "not_done", "reversed": "not_done"}


def activity_direction(activity_type, amount_sign, amount=None):
    """out (money left the account), in, or unknown."""
    if activity_type in TRADE_TYPES or (isinstance(amount, str) and amount.startswith("-")):
        return "unknown"  # a signed amount contradicts amount_sign; do not guess
    return {"negative": "out", "positive": "in"}.get(amount_sign, "unknown")


def activity_category(account_type, activity_type, sub_type, amount_sign, amount=None):
    """purchase, refund, card_payment, bill_payment, transfer or other."""
    if account_type == "ca_cash_msb" and activity_type == "SPEND":
        # A debit card refund is also SPEND; only the direction tells them apart.
        return {"out": "purchase", "in": "refund"}.get(
            activity_direction(activity_type, amount_sign, amount), "other")
    if account_type == "ca_credit_card" and activity_type == "CREDIT_CARD":
        return CARD_SUBTYPES.get(sub_type, "other")
    if activity_type == "CREDIT_CARD_PAYMENT":
        return "card_payment"  # the chequing side of a card payment, not spending
    if activity_type == "WITHDRAWAL" and sub_type in BILL_SUBTYPES:
        return "bill_payment"
    return "transfer" if activity_type in TRANSFER_TYPES else "other"


def activity_outcome(status):
    """done, pending (may still change) or not_done (no money moved); unknown otherwise."""
    return OUTCOMES.get(status.lower(), "unknown") if isinstance(status, str) else "unknown"


def activity_kind(account_type, activity_type, sub_type):
    """spending: debit and card purchases and their refunds; investment: investment accounts.
    A debit row whose direction is unclear stays in spending, so a total can say it is
    unconfirmed instead of silently leaving the row out."""
    if ((account_type == "ca_cash_msb" and activity_type == "SPEND")
            or (account_type == "ca_credit_card" and activity_type == "CREDIT_CARD"
                and sub_type in {"PURCHASE", "REFUND"})):
        return "spending"
    return "investment" if account_type in INVESTMENT_ACCOUNTS else "other"


def normalize_activity(node, observed_at, account_type=None):
    """Return a deduplicatable row, or None if identity/time is missing."""
    account_id = node.get("accountId")
    canonical_id = node.get("canonicalId")
    occurred_at = node.get("occurredAt")
    if (not isinstance(account_id, str) or not account_id
            or not isinstance(canonical_id, str) or not canonical_id
            or not isinstance(occurred_at, str)):
        return None
    try:
        occurred = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if occurred.tzinfo is None:
        return None

    key = hashlib.sha256((account_id + "\0" + canonical_id).encode()).hexdigest()
    activity_type = text(node.get("type"), 64)
    sub_type = text(node.get("subType"), 64)
    amount = number(node.get("amount"))
    amount_sign = text(node.get("amountSign"), 16)
    return {
        "activity_ref": key,
        "account_ref": reference(account_id),
        "account_type": text(account_type, 64),
        "account_nickname": account_nickname({"id": account_id, "type": account_type}),
        "source": "wealthsimple_activity_feed",
        "observed_at": observed_at,
        "occurred_at": occurred_at,
        # Upstream strings can differ in offset and precision; queries use this instead.
        "occurred_ts": occurred.astimezone(timezone.utc),
        "kind": activity_kind(account_type, activity_type, sub_type),
        "type": activity_type,
        "sub_type": sub_type,
        "status": text(node.get("status"), 64),
        "amount": amount,
        "amount_sign": amount_sign,
        "currency": text(node.get("currency"), 8),
        "merchant": text(node.get("spendMerchant"), 256),
        "aft_originator_name": text(node.get("aftOriginatorName"), 256),
        "aft_transaction_category": text(node.get("aftTransactionCategory"), 64),
        "aft_transaction_type": text(node.get("aftTransactionType"), 64),
        "asset_symbol": text(node.get("assetSymbol"), 40),
        "asset_quantity": number(node.get("assetQuantity")),
        "fees": number(node.get("fees")),
        "realized_pnl": number(node.get("realizedPnl")),
    }
