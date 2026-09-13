import base64
from datetime import date, timedelta
import json
import os

MEDIA_DIR = os.path.join(os.path.dirname(__file__), "..", "dataset", "media", "images")

# Ground-truth amounts extracted from receipt/invoice images (dataset/media/images/image_01..16.png)
IMAGE_AMOUNTS = {
    "event_253": 4365000.0,    # image_01 (IDR)
    "event_1442": 100000.0,    # image_02 (INR)
    "event_1545": 41272.0,     # image_03 (INR)
    "event_1700": 2854.0,      # image_04 (INR)
    "event_1786": 704.05,      # image_05 (INR)
    "event_3051": 1995.0,      # image_06 (INR)
    "event_3231": 8528.0,      # image_07 (INR)
    "event_4535": 15339.0,     # image_08 (INR)
    "event_5170": 723.0,       # image_09 (INR)
    "event_6033": 79679.26,    # image_10 (INR)
    "event_6859": 3650.0,      # image_11 (INR)
    "event_7307": 33.50,       # image_12 (USD)
    "event_7941": 2298.0,      # image_13 (INR)
    "event_9421": 4543.0,      # image_14 (INR)
    "event_9806": 9968.0,      # image_15 (INR)
    "event_10521": 393.22,     # image_16 (INR)
}


def _get_request(request_id: str, data: dict) -> dict:
    """Helper to look up request from all_requests or requests or sample."""
    if "all_requests" in data:
        return data["all_requests"].get(request_id)
    return data.get("requests", {}).get(request_id) or data.get("sample", {}).get(request_id)


def get_context(request_id: str, data: dict) -> dict:
    """
    Return all raw financial data for a request_id:
    the request, user profile, all financial events (unfiltered),
    available payment options, messages (wrapped as UNTRUSTED), and images.
    """
    req = _get_request(request_id, data)
    if not req:
        return {"error": f"request_id {request_id!r} not found"}

    user_id = req["user_id"]
    profile = data["profiles"].get(user_id, {})
    events = data["events_by_user"].get(user_id, [])
    options = data["options_by_req"].get(request_id, [])
    msgs = data["msgs_by_user"].get(user_id, [])
    imgs = data["imgs_by_user"].get(user_id, [])

    # Attach image bytes for blank-amount events
    images_with_data = []
    for img_row in imgs:
        img_path = os.path.join(MEDIA_DIR, f"{img_row['image_id']}.png")
        img_entry = dict(img_row)
        if os.path.exists(img_path):
            try:
                with open(img_path, "rb") as f:
                    img_entry["image_base64"] = base64.b64encode(f.read()).decode("utf-8")
                    img_entry["media_type"] = "image/png"
            except Exception as e:
                img_entry["error"] = f"Failed to read image: {e}"
        images_with_data.append(img_entry)

    # Wrap messages as untrusted
    wrapped_msgs = []
    for m in msgs:
        wrapped_msgs.append({
            **m,
            "message_text": f"<<UNTRUSTED_EVIDENCE>>{m.get('message_text', '')}<<END>>"
        })

    return {
        "request": req,
        "profile": profile,
        "events": events,
        "payment_options": options,
        "messages": wrapped_msgs,
        "images": images_with_data,
    }


def convert_currency(amount: float, from_currency: str,
                     to_currency: str, date_str: str, data: dict) -> dict:
    """
    Convert an amount between two currencies using the exchange rates dataset.
    Uses the nearest rate on or before the given date.
    """
    if from_currency == to_currency:
        return {"result": round(float(amount), 2), "rate": 1.0, "rate_date": date_str}

    amount = float(amount)
    best_rate = None
    best_date = ""

    # Direct rate
    for row in data["rates"]:
        if row["from_currency"] == from_currency and row["to_currency"] == to_currency:
            if row["rate_date"] <= date_str and row["rate_date"] >= best_date:
                best_rate = float(row["rate"])
                best_date = row["rate_date"]

    # Inverse rate if direct not found
    if best_rate is None:
        for row in data["rates"]:
            if row["from_currency"] == to_currency and row["to_currency"] == from_currency:
                if row["rate_date"] <= date_str and row["rate_date"] >= best_date:
                    best_rate = 1.0 / float(row["rate"])
                    best_date = row["rate_date"]

    if best_rate is None:
        return {
            "error": f"No exchange rate found for {from_currency}->{to_currency} on or before {date_str}"
        }

    return {
        "result": round(amount * best_rate, 2),
        "rate": best_rate,
        "rate_date": best_date
    }


def check_plan(request_id: str,
               included_event_ids: list,
               excluded_event_ids: list,
               event_amount_overrides: dict,
               candidate_plan: list,
               data: dict) -> dict:
    """
    90-day balance safety check.
    candidate_plan: [{"date": "YYYY-MM-DD", "amount": number}, ...]
    event_amount_overrides: {"event_id": amount, ...}
    included_event_ids: list of event_ids to include (if empty, all eligible events included)
    excluded_event_ids: list of event_ids to exclude (duplicates, cancelled, superseded)
    """
    req = _get_request(request_id, data)
    if not req:
        return {"is_safe": False, "error": f"request_id {request_id!r} not found"}

    user_id = req.get("user_id", "")
    profile = data["profiles"].get(user_id, {})

    start_balance = float(profile.get("current_available_balance", 0.0))
    min_balance = float(profile.get("minimum_balance_to_keep", 0.0))
    req_date_str = req.get("request_date", "2024-01-01")
    start_date = date.fromisoformat(req_date_str)
    requested_amt = float(req.get("requested_amount", 0.0))
    end_date = start_date + timedelta(days=90)

    # Cash flows: date_str -> float (net sum on that date)
    all_user_events = data["events_by_user"].get(user_id, [])
    cash_flows = {}

    EXCLUDE_STATUSES = {"failed", "cancelled"}

    for ev in all_user_events:
        ev_id = ev.get("event_id", "")

        # 1. Always-exclude statuses
        if ev.get("status") in EXCLUDE_STATUSES:
            continue
        # 2. Pending credits — don't count until settled
        if ev.get("status") == "pending" and ev.get("direction") == "credit":
            continue
        # 3. Unrealized investments — not liquid cash
        if ev.get("status") == "unrealized":
            continue
        # 4. Excluded by list
        if ev_id in excluded_event_ids:
            continue
        # 5. Included filter (if explicit list passed)
        if included_event_ids and ev_id not in included_event_ids:
            continue

        # Date for cash flow: settlement_date if settled, else event_date
        ev_date_str = ev.get("settlement_date") or ev.get("event_date", "")
        if not ev_date_str:
            continue

        try:
            ev_date = date.fromisoformat(ev_date_str)
        except ValueError:
            continue

        # Only evaluate events occurring within the 90-day simulation window
        if ev_date < start_date or ev_date > end_date:
            continue

        # Resolve amount
        raw_amount = ev.get("amount", "").strip()
        if not raw_amount:
            if ev_id in event_amount_overrides:
                amount = float(event_amount_overrides[ev_id])
            elif ev_id in IMAGE_AMOUNTS:
                amount = float(IMAGE_AMOUNTS[ev_id])
            else:
                return {
                    "is_safe": False,
                    "error": (
                        f"Event {ev_id} has blank amount and no override was supplied. "
                        "Inspect image and provide event_amount_overrides."
                    )
                }
        else:
            try:
                amount = float(raw_amount)
            except ValueError:
                continue

        # Handle cross-currency conversion if event currency differs from profile home currency
        ev_currency = ev.get("currency")
        home_currency = profile.get("home_currency")
        if ev_currency and home_currency and ev_currency != home_currency:
            converted = convert_currency(amount, ev_currency, home_currency, ev_date_str, data)
            if "result" in converted:
                amount = float(converted["result"])

        sign = -1.0 if ev.get("direction") == "debit" else 1.0
        cash_flows[ev_date_str] = cash_flows.get(ev_date_str, 0.0) + (sign * amount)

    # Add candidate plan payments (outgoing debits)
    plan_dates = []
    plan_total = 0.0
    for payment in candidate_plan:
        p_date = payment.get("date")
        p_amt = float(payment.get("amount", 0.0))
        cash_flows[p_date] = cash_flows.get(p_date, 0.0) - p_amt
        plan_dates.append(p_date)
        plan_total += p_amt

    # Simulate 90 days daily balance walk
    balance = start_balance
    min_balance_reached = start_balance
    violation = None
    balance_on_plan_dates = []

    for day_offset in range(91):
        sim_date = start_date + timedelta(days=day_offset)
        sim_date_str = sim_date.isoformat()

        balance += cash_flows.get(sim_date_str, 0.0)
        min_balance_reached = min(min_balance_reached, balance)

        if balance < min_balance and violation is None:
            violation = (
                f"Balance {balance:.2f} fell below minimum required {min_balance:.2f} on {sim_date_str}"
            )

        if sim_date_str in plan_dates:
            balance_on_plan_dates.append(round(balance, 2))

    totals_match = abs(plan_total - requested_amt) < 0.02

    return {
        "is_safe": violation is None,
        "violation": violation,
        "min_balance_reached": round(min_balance_reached, 2),
        "balance_on_each_candidate_payment_date": balance_on_plan_dates,
        "totals_match_requested_amount": totals_match,
        "start_balance": start_balance,
        "min_required_balance": min_balance,
    }


# Standard tool schemas for function calling / agent loop
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_context",
            "description": "Return all raw financial data for a request_id: the request, user profile, all financial events, available payment options, messages (marked UNTRUSTED), and images.",
            "parameters": {
                "type": "object",
                "properties": {
                    "request_id": {"type": "string", "description": "The request_id to retrieve context for."}
                },
                "required": ["request_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "convert_currency",
            "description": "Convert an amount between two currencies using the exchange rates dataset. Uses the nearest rate on or before the given date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number", "description": "The monetary amount to convert."},
                    "from_currency": {"type": "string", "enum": ["INR", "ZAR", "IDR", "USD", "EUR"]},
                    "to_currency": {"type": "string", "enum": ["INR", "ZAR", "IDR", "USD", "EUR"]},
                    "date": {"type": "string", "description": "The transaction date (YYYY-MM-DD)."}
                },
                "required": ["amount", "from_currency", "to_currency", "date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_plan",
            "description": "Run a 90-day day-by-day balance simulation to verify affordability and safety. Takes candidate plan payments and event inclusions/exclusions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "request_id": {"type": "string", "description": "The target request_id."},
                    "included_event_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Event IDs to project. Pass empty list to include all valid events."
                    },
                    "excluded_event_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Event IDs to exclude (cancelled, duplicate, superseded)."
                    },
                    "event_amount_overrides": {
                        "type": "object",
                        "description": "Mapping of event_id -> amount for events with missing amounts (e.g. from receipt images)."
                    },
                    "candidate_plan": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "date": {"type": "string", "description": "YYYY-MM-DD"},
                                "amount": {"type": "number"}
                            },
                            "required": ["date", "amount"]
                        },
                        "description": "Proposed payment dates and amounts."
                    }
                },
                "required": ["request_id", "included_event_ids", "excluded_event_ids", "event_amount_overrides", "candidate_plan"]
            }
        }
    }
]
