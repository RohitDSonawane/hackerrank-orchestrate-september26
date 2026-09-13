from datetime import date, timedelta
import json
import os

REQUIRED_COLUMNS = [
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
]

VALID_STATUS = {
    "affordable_now",
    "affordable_with_plan",
    "affordable_later",
    "not_affordable",
}

VALID_METHOD = {
    "full_payment",
    "partial_payment",
    "installments",
    "wait",
    "not_recommended",
}

VALID_PAIRS = {
    ("affordable_now", "full_payment"),
    ("affordable_with_plan", "full_payment"),
    ("affordable_with_plan", "partial_payment"),
    ("affordable_with_plan", "installments"),
    ("affordable_later", "wait"),
    ("not_affordable", "not_recommended"),
}


def _get_request(request_id: str, data: dict) -> dict:
    if "all_requests" in data:
        return data["all_requests"].get(request_id, {})
    return data.get("requests", {}).get(request_id) or data.get("sample", {}).get(request_id, {})


def _parse_plan(plan_str: str) -> list:
    """Parse 'YYYY-MM-DD:amount|...' into [(date_str, float), ...]"""
    result = []
    if not plan_str or plan_str == "none":
        return result
    for entry in plan_str.split("|"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 2:
            raise ValueError(f"Invalid plan component: {entry}")
        d, a = parts[0].strip(), parts[1].strip()
        result.append((d, float(a)))
    return result


def _find_matching_option(payments: list, options: list) -> bool:
    """Check if payments match any payment option's schedule."""
    if not options:
        return False
    for opt in options:
        if opt.get("payment_method") != "installments":
            continue
        try:
            n = int(opt["number_of_payments"])
        except (ValueError, KeyError):
            continue
        if len(payments) != n:
            continue

        start = opt.get("first_payment_date", "")
        freq = int(opt.get("payment_frequency_days") or 30)
        amt = float(opt.get("payment_amount", 0))

        expected_dates = []
        try:
            d = date.fromisoformat(start)
            for _ in range(n):
                expected_dates.append(d.isoformat())
                d += timedelta(days=freq)
        except Exception:
            continue

        actual_dates = [p[0] for p in payments]
        actual_amounts = [p[1] for p in payments]

        if actual_dates == expected_dates and all(abs(a - amt) < 0.05 for a in actual_amounts):
            return True
    return False


def validate_row(row: dict, data: dict) -> list:
    """
    Returns list of violation strings. Empty list = row is 100% valid.
    """
    violations = []
    req_id = row.get("request_id", "?")

    # 1. All required columns present
    for col in REQUIRED_COLUMNS:
        if col not in row or row[col] is None:
            violations.append(f"Missing column: {col}")
    if violations:
        return violations

    req = _get_request(req_id, data)
    user_id = req.get("user_id", "")
    profile = data["profiles"].get(user_id, {})
    options = data.get("options_by_req", {}).get(req_id, [])

    try:
        amt_safe = float(row["amount_safe_to_pay"])
        amt_requested = float(req.get("requested_amount", 0))
    except (ValueError, TypeError):
        violations.append("amount_safe_to_pay is not a valid number")
        return violations

    # 2. amount_safe_to_pay bounds [0, requested_amount]
    if not (-0.01 <= amt_safe <= amt_requested + 0.01):
        violations.append(
            f"amount_safe_to_pay {amt_safe} not in range [0, {amt_requested}]"
        )

    # 3. Enum values
    status = row.get("affordability_status", "")
    method = row.get("recommended_payment_method", "")
    if status not in VALID_STATUS:
        violations.append(f"Invalid affordability_status: {status!r}")
    if method not in VALID_METHOD:
        violations.append(f"Invalid recommended_payment_method: {method!r}")

    # 4. Valid (status, method) pair
    if status in VALID_STATUS and method in VALID_METHOD:
        if (status, method) not in VALID_PAIRS:
            violations.append(f"Invalid (status, method) pair: ({status!r}, {method!r})")

    # 5. payment_plan rules
    plan_str = str(row.get("payment_plan", "")).strip()
    if method == "not_recommended":
        if plan_str != "none":
            violations.append(f"not_recommended requires payment_plan to be 'none', got {plan_str!r}")
    elif plan_str == "none":
        violations.append(f"Affordable status {status!r} cannot have payment_plan 'none'")
    else:
        try:
            payments = _parse_plan(plan_str)
        except Exception as e:
            violations.append(f"payment_plan parse error: {e}")
            payments = []

        if payments:
            dates = [p[0] for p in payments]
            if dates != sorted(dates):
                violations.append("payment_plan entries are not in chronological order")

            plan_total = sum(p[1] for p in payments)

            if method == "partial_payment":
                if len(payments) != 2:
                    violations.append(f"partial_payment requires exactly 2 installments, got {len(payments)}")
                if abs(plan_total - amt_requested) > 0.05:
                    violations.append(
                        f"partial_payment plan total {plan_total} != requested_amount {amt_requested}"
                    )
            elif method == "installments":
                if not _find_matching_option(payments, options):
                    violations.append(
                        "installments plan does not match any available option in request_payment_options.csv"
                    )

    # 6. earliest_date_for_full_payment rules
    edfp = str(row.get("earliest_date_for_full_payment", "")).strip()
    req_date = req.get("request_date", "")

    if status == "affordable_now" and edfp != req_date:
        violations.append(
            f"affordable_now requires earliest_date_for_full_payment == request_date ({req_date}), got {edfp!r}"
        )
    if status == "not_affordable" and edfp not in ("", None):
        violations.append(
            f"not_affordable requires empty earliest_date_for_full_payment, got {edfp!r}"
        )

    # 7. spending_changes_needed rules
    sched = str(row.get("spending_changes_needed", "")).strip()
    if sched and sched != "none":
        evt_by_id = data.get("events_by_id", {})
        to_stop = set((profile.get("expense_categories_user_is_willing_to_stop") or "").split("|"))
        to_reduce = set((profile.get("expense_categories_user_is_willing_to_reduce") or "").split("|"))
        protected = set((profile.get("expense_categories_to_protect") or "").split("|"))

        changes = [c.strip() for c in sched.split("|") if c.strip()]
        if len(changes) > 3:
            violations.append(f"Too many spending changes: {len(changes)} (maximum 3 allowed)")

        for change in changes:
            if change.startswith("stop:"):
                ev_id = change.split(":")[1]
                ev = evt_by_id.get(ev_id, {})
                if ev.get("flexibility") != "stoppable":
                    violations.append(f"stop:{ev_id} event flexibility is not 'stoppable' (got {ev.get('flexibility')})")
                if ev.get("category") in protected:
                    violations.append(f"stop:{ev_id} targets protected category {ev.get('category')}")
                if ev.get("category") not in to_stop:
                    violations.append(f"stop:{ev_id} category not in willing_to_stop")
            elif change.startswith("reduce_to:"):
                parts = change.split(":")
                if len(parts) == 3:
                    ev_id = parts[1]
                    ev = evt_by_id.get(ev_id, {})
                    if ev.get("flexibility") != "reducible":
                        violations.append(f"reduce_to:{ev_id} event flexibility is not 'reducible'")
                    if ev.get("category") in protected:
                        violations.append(f"reduce_to:{ev_id} targets protected category")
                    if ev.get("category") not in to_reduce:
                        violations.append(f"reduce_to:{ev_id} category not in willing_to_reduce")

    return violations


def fallback_row(request_id: str, data: dict) -> dict:
    """
    Produce the safest deterministic row without LLM involvement.
    Guaranteed 100% schema-compliant.
    """
    req = _get_request(request_id, data)
    user_id = req.get("user_id", "")
    profile = data["profiles"].get(user_id, {})

    from tools import check_plan
    sim = check_plan(
        request_id=request_id,
        included_event_ids=[],
        excluded_event_ids=[],
        event_amount_overrides={},
        candidate_plan=[],
        data=data,
    )

    min_bal = float(profile.get("minimum_balance_to_keep", 0.0))
    start_bal = float(profile.get("current_available_balance", 0.0))
    min_reached = sim.get("min_balance_reached", start_bal)
    safe_headroom = max(0.0, min_reached - min_bal)
    requested = float(req.get("requested_amount", 0.0))
    safe_to_pay = round(min(safe_headroom, requested), 2)

    return {
        "request_id": request_id,
        "amount_safe_to_pay": safe_to_pay,
        "affordability_status": "not_affordable",
        "recommended_payment_method": "not_recommended",
        "payment_plan": "none",
        "earliest_date_for_full_payment": "",
        "spending_changes_needed": "none",
        "decision_explanation": (
            f"Fallback: Unable to verify safe completion within available options. "
            f"Preserved minimum balance requirement with safe headroom estimated at {safe_to_pay}."
        ),
    }
