import math
import os
from datetime import date, timedelta
from tools import convert_currency, IMAGE_AMOUNTS


def _fmt_amt(val) -> str:
    """Format amount cleanly avoiding scientific notation."""
    try:
        f = round(float(val), 2)
        if f.is_integer():
            return str(int(f))
        return f"{f:.2f}"
    except Exception:
        return str(val)


def _get_request(request_id: str, data: dict) -> dict:
    if "all_requests" in data:
        return data["all_requests"].get(request_id)
    return data.get("requests", {}).get(request_id) or data.get("sample", {}).get(request_id)


def build_recurring_and_future(user_id: str, req_date_str: str, data: dict):
    req_date = date.fromisoformat(req_date_str)
    user_events = data["events_by_user"].get(user_id, [])
    profile = data["profiles"].get(user_id, {})
    home_currency = profile.get("home_currency")

    groups = {}
    future_events = []

    for ev in user_events:
        if ev.get("status") in {"failed", "cancelled", "unrealized"}:
            continue
        ev_id = ev.get("event_id")
        raw_amt = ev.get("amount", "").strip()
        if not raw_amt:
            amt = IMAGE_AMOUNTS.get(ev_id, 0.0)
        else:
            try:
                amt = float(raw_amt)
            except ValueError:
                amt = 0.0

        ev_curr = ev.get("currency")
        d_str = ev.get("settlement_date") or ev.get("event_date", "")
        if ev_curr and home_currency and ev_curr != home_currency and amt > 0:
            c_res = convert_currency(amt, ev_curr, home_currency, d_str or req_date_str, data)
            if "result" in c_res:
                amt = float(c_res["result"])

        ev_copy = dict(ev)
        ev_copy["resolved_amount"] = amt

        try:
            d = date.fromisoformat(d_str)
        except Exception:
            continue

        if d >= req_date:
            future_events.append(ev_copy)
        else:
            key = (ev.get("category"), ev.get("description"), ev.get("direction"))
            groups.setdefault(key, []).append((d, ev_copy))

    recurring = []
    for key, items in groups.items():
        if len(items) >= 2:
            items.sort(key=lambda x: x[0])
            last_d, last_ev = items[-1]
            if (req_date - last_d).days <= 45:
                amt = last_ev["resolved_amount"]
                if amt > 0:
                    recurring.append({
                        "event_id": last_ev.get("event_id"),
                        "category": key[0],
                        "description": key[1],
                        "direction": key[2],
                        "amount": amt,
                        "day": last_d.day,
                        "flexibility": last_ev.get("flexibility", "fixed"),
                        "minimum_allowed_amount": float(last_ev.get("minimum_allowed_amount") or 0.0),
                    })

    # Ensure recurring salary is detected even if description varied or only 1 prior record exists
    has_salary = any(r["category"] == "salary" and r["direction"] == "credit" for r in recurring)
    if not has_salary:
        salary_events = [
            e for e in user_events
            if e.get("category") == "salary"
            and e.get("direction") == "credit"
            and e.get("status") not in {"failed", "cancelled"}
        ]
        if salary_events:
            salary_events.sort(key=lambda x: x.get("settlement_date") or x.get("event_date") or "")
            last_sal = salary_events[-1]
            amt = float(last_sal.get("amount") or 0.0)
            d_str = last_sal.get("settlement_date") or last_sal.get("event_date")
            if amt > 0 and d_str:
                d = date.fromisoformat(d_str)
                recurring.append({
                    "event_id": last_sal.get("event_id"),
                    "category": "salary",
                    "description": "Recurring Salary",
                    "direction": "credit",
                    "amount": amt,
                    "day": d.day,
                    "flexibility": "fixed",
                    "minimum_allowed_amount": 0.0,
                })

    return recurring, future_events


def simulate_timeline(user_id: str, req_date_str: str, candidate_plan: list,
                      recurring: list, future_events: list,
                      stopped_ids: set = None, reductions: dict = None,
                      data: dict = None) -> tuple:
    if stopped_ids is None:
        stopped_ids = set()
    if reductions is None:
        reductions = {}
    if data is None:
        return False, 0.0

    profile = data["profiles"].get(user_id, {})
    start_balance = float(profile.get("current_available_balance", 0.0))
    min_balance = float(profile.get("minimum_balance_to_keep", 0.0))
    req_date = date.fromisoformat(req_date_str)
    end_date = req_date + timedelta(days=90)

    cash_flows = {}

    for fe in future_events:
        fe_id = fe.get("event_id")
        if fe_id in stopped_ids:
            continue
        d_str = fe.get("settlement_date") or fe.get("event_date")
        amt = fe["resolved_amount"]
        if fe_id in reductions:
            amt = reductions[fe_id]
        sign = 1.0 if fe.get("direction") == "credit" else -1.0
        cash_flows[d_str] = cash_flows.get(d_str, 0.0) + sign * amt

    for r in recurring:
        r_id = r["event_id"]
        if r_id in stopped_ids:
            continue
        for m_offset in range(0, 4):
            y = req_date.year + (req_date.month + m_offset - 1) // 12
            m = (req_date.month + m_offset - 1) % 12 + 1
            day = min(r["day"], 28)
            try:
                proj_d = date(y, m, day)
            except Exception:
                continue
            if req_date <= proj_d <= end_date:
                d_str = proj_d.isoformat()
                amt = r["amount"]
                if r_id in reductions:
                    amt = reductions[r_id]
                sign = 1.0 if r["direction"] == "credit" else -1.0
                cash_flows[d_str] = cash_flows.get(d_str, 0.0) + sign * amt

    for p in candidate_plan:
        d = p["date"]
        amt = float(p["amount"])
        cash_flows[d] = cash_flows.get(d, 0.0) - amt

    balance = start_balance
    min_reached = start_balance
    violation = False

    for day_i in range(91):
        sim_d = (req_date + timedelta(days=day_i)).isoformat()
        balance += cash_flows.get(sim_d, 0.0)
        min_reached = min(min_reached, balance)
        if balance < min_balance:
            violation = True

    return not violation, min_reached


def solve_request_deterministic(request_id: str, data: dict) -> dict:
    """
    High-fidelity financial optimization solving algorithm implementing the 6 problem statement
    ranking rules, binary-search safe payment capacity, and multi-option ranking.
    """
    req = _get_request(request_id, data)
    if not req:
        return {
            "request_id": request_id,
            "amount_safe_to_pay": "0",
            "affordability_status": "not_affordable",
            "recommended_payment_method": "not_recommended",
            "payment_plan": "none",
            "earliest_date_for_full_payment": "",
            "spending_changes_needed": "none",
            "decision_explanation": "Request record not found.",
        }

    user_id = req.get("user_id", "")
    profile = data["profiles"].get(user_id, {})
    req_amt = float(req.get("requested_amount", 0.0))
    req_date_str = req.get("request_date", "2024-01-01")
    req_date = date.fromisoformat(req_date_str)
    desired_date_str = req.get("desired_completion_date") or req_date_str
    desired_date = date.fromisoformat(desired_date_str)
    allows_partial = str(req.get("allows_partial_payment", "")).strip().lower() in ("true", "1", "yes")

    methods_user_considers = set(
        m.strip() for m in (profile.get("payment_methods_user_will_consider") or "full_payment").split("|") if m.strip()
    )
    max_inst_months = int(profile.get("max_installment_months") or 12)

    recurring, future_events = build_recurring_and_future(user_id, req_date_str, data)

    # 1. amount_safe_to_pay on request_date
    safe_zero, _ = simulate_timeline(user_id, req_date_str, [], recurring, future_events, data=data)
    if not safe_zero:
        amount_safe_to_pay = 0.0
    else:
        safe_full, _ = simulate_timeline(user_id, req_date_str, [{"date": req_date_str, "amount": req_amt}], recurring, future_events, data=data)
        if safe_full:
            amount_safe_to_pay = req_amt
        else:
            low = 0.0
            high = req_amt
            for _ in range(25):
                mid = round((low + high) / 2.0, 2)
                s, _ = simulate_timeline(user_id, req_date_str, [{"date": req_date_str, "amount": mid}], recurring, future_events, data=data)
                if s:
                    low = mid
                else:
                    high = mid
            amount_safe_to_pay = round(low, 2)

    # 2. earliest_date_for_full_payment
    earliest_date_for_full = ""
    for day_i in range(91):
        test_d = (req_date + timedelta(days=day_i)).isoformat()
        s, _ = simulate_timeline(user_id, req_date_str, [{"date": test_d, "amount": req_amt}], recurring, future_events, data=data)
        if s:
            earliest_date_for_full = test_d
            break

    candidates = []

    # Option A: full_payment on request_date (No changes)
    if "full_payment" in methods_user_considers:
        s_now, _ = simulate_timeline(user_id, req_date_str, [{"date": req_date_str, "amount": req_amt}], recurring, future_events, data=data)
        if s_now:
            candidates.append({
                "affordability_status": "affordable_now",
                "recommended_payment_method": "full_payment",
                "payment_plan": f"{req_date_str}:{_fmt_amt(req_amt)}",
                "spending_changes_needed": "none",
                "completes_by_desired": req_date <= desired_date,
                "has_spending_changes": False,
                "total_paid": req_amt,
                "start_date": req_date_str,
                "num_payments": 1,
                "option_id": 999999,
                "explanation": f"Full payment of {_fmt_amt(req_amt)} on {req_date_str} leaves balance safely above minimum."
            })

    # Option B: installments from request_payment_options.csv
    options = data.get("options_by_req", {}).get(request_id, [])
    if "installments" in methods_user_considers and options:
        for opt in options:
            if opt.get("payment_method") != "installments":
                continue
            try:
                n_payments = int(opt.get("number_of_payments") or 1)
                p_amt = float(opt.get("payment_amount") or 0.0)
                p_freq = int(opt.get("payment_frequency_days") or 30)
                first_d_str = opt.get("first_payment_date") or req_date_str
                first_d = date.fromisoformat(first_d_str)
            except Exception:
                continue

            total_duration_days = (n_payments - 1) * p_freq
            if total_duration_days > max_inst_months * 31:
                continue

            sched = []
            curr_d = first_d
            plan_str_parts = []
            for _ in range(n_payments):
                d_s = curr_d.isoformat()
                sched.append({"date": d_s, "amount": p_amt})
                plan_str_parts.append(f"{d_s}:{_fmt_amt(p_amt)}")
                curr_d += timedelta(days=p_freq)
            last_payment_date = date.fromisoformat(sched[-1]["date"])

            s_inst, _ = simulate_timeline(user_id, req_date_str, sched, recurring, future_events, data=data)
            if s_inst:
                opt_num = int(''.join(filter(str.isdigit, opt.get("payment_option_id", "0"))) or 0)
                candidates.append({
                    "affordability_status": "affordable_with_plan",
                    "recommended_payment_method": "installments",
                    "payment_plan": "|".join(plan_str_parts),
                    "spending_changes_needed": "none",
                    "completes_by_desired": last_payment_date <= desired_date,
                    "has_spending_changes": False,
                    "total_paid": n_payments * p_amt,
                    "start_date": first_d_str,
                    "num_payments": n_payments,
                    "option_id": opt_num,
                    "explanation": f"Installment plan of {n_payments} payments of {_fmt_amt(p_amt)} starting {first_d_str} is safe."
                })

    # Option C: partial_payment
    if allows_partial and "partial_payment" in methods_user_considers:
        if 0 < amount_safe_to_pay < req_amt and earliest_date_for_full:
            full_d = date.fromisoformat(earliest_date_for_full)
            if full_d <= desired_date:
                rem_amt = round(req_amt - amount_safe_to_pay, 2)
                plan_sched = [
                    {"date": req_date_str, "amount": amount_safe_to_pay},
                    {"date": earliest_date_for_full, "amount": rem_amt}
                ]
                s_part, _ = simulate_timeline(user_id, req_date_str, plan_sched, recurring, future_events, data=data)
                if s_part:
                    candidates.append({
                        "affordability_status": "affordable_with_plan",
                        "recommended_payment_method": "partial_payment",
                        "payment_plan": f"{req_date_str}:{_fmt_amt(amount_safe_to_pay)}|{earliest_date_for_full}:{_fmt_amt(rem_amt)}",
                        "spending_changes_needed": "none",
                        "completes_by_desired": full_d <= desired_date,
                        "has_spending_changes": False,
                        "total_paid": req_amt,
                        "start_date": req_date_str,
                        "num_payments": 2,
                        "option_id": 999998,
                        "explanation": f"Pay {_fmt_amt(amount_safe_to_pay)} today and remaining {_fmt_amt(rem_amt)} on {earliest_date_for_full}."
                    })

    # Option D: wait
    if "full_payment" in methods_user_considers and earliest_date_for_full:
        full_d = date.fromisoformat(earliest_date_for_full)
        if req_date < full_d <= desired_date:
            candidates.append({
                "affordability_status": "affordable_later",
                "recommended_payment_method": "wait",
                "payment_plan": f"{earliest_date_for_full}:{_fmt_amt(req_amt)}",
                "spending_changes_needed": "none",
                "completes_by_desired": full_d <= desired_date,
                "has_spending_changes": False,
                "total_paid": req_amt,
                "start_date": earliest_date_for_full,
                "num_payments": 1,
                "option_id": 999999,
                "explanation": f"Pay {_fmt_amt(req_amt)} in full on {earliest_date_for_full}. Paying earlier would breach minimum required balance."
            })

    # Option E: Spending changes
    to_stop = set((profile.get("expense_categories_user_is_willing_to_stop") or "").split("|"))
    to_reduce = set((profile.get("expense_categories_user_is_willing_to_reduce") or "").split("|"))
    protected = set((profile.get("expense_categories_to_protect") or "").split("|"))

    stoppable_candidates = [
        r for r in recurring
        if r["direction"] == "debit" and r["flexibility"] == "stoppable" and r["category"] in to_stop and r["category"] not in protected
    ]
    reducible_candidates = [
        r for r in recurring
        if r["direction"] == "debit" and r["flexibility"] in {"reducible", "reducible_or_stoppable"} and r["category"] in to_reduce and r["category"] not in protected
    ]

    for sc in stoppable_candidates:
        s_stop, _ = simulate_timeline(user_id, req_date_str, [{"date": req_date_str, "amount": req_amt}], recurring, future_events, stopped_ids={sc["event_id"]}, data=data)
        if s_stop and "full_payment" in methods_user_considers:
            candidates.append({
                "affordability_status": "affordable_with_plan",
                "recommended_payment_method": "full_payment",
                "payment_plan": f"{req_date_str}:{_fmt_amt(req_amt)}",
                "spending_changes_needed": f"stop:{sc['event_id']}",
                "completes_by_desired": req_date <= desired_date,
                "has_spending_changes": True,
                "total_paid": req_amt,
                "start_date": req_date_str,
                "num_payments": 1,
                "option_id": 999999,
                "explanation": f"Affordable with full payment by stopping {sc['category']} ({sc['event_id']})."
            })

    for rc in reducible_candidates:
        new_amt = rc["minimum_allowed_amount"]
        s_red, _ = simulate_timeline(user_id, req_date_str, [{"date": req_date_str, "amount": req_amt}], recurring, future_events, reductions={rc["event_id"]: new_amt}, data=data)
        if s_red and "full_payment" in methods_user_considers:
            candidates.append({
                "affordability_status": "affordable_with_plan",
                "recommended_payment_method": "full_payment",
                "payment_plan": f"{req_date_str}:{_fmt_amt(req_amt)}",
                "spending_changes_needed": f"reduce_to:{rc['event_id']}:{_fmt_amt(new_amt)}",
                "completes_by_desired": req_date <= desired_date,
                "has_spending_changes": True,
                "total_paid": req_amt,
                "start_date": req_date_str,
                "num_payments": 1,
                "option_id": 999999,
                "explanation": f"Affordable with full payment by reducing {rc['category']} ({rc['event_id']}) to {_fmt_amt(new_amt)}."
            })

    for sc in stoppable_candidates:
        for rc in reducible_candidates:
            if sc["event_id"] == rc["event_id"]:
                continue
            new_amt = rc["minimum_allowed_amount"]
            s_combo, _ = simulate_timeline(
                user_id, req_date_str, [{"date": req_date_str, "amount": req_amt}], recurring, future_events,
                stopped_ids={sc["event_id"]}, reductions={rc["event_id"]: new_amt}, data=data
            )
            if s_combo and "full_payment" in methods_user_considers:
                candidates.append({
                    "affordability_status": "affordable_with_plan",
                    "recommended_payment_method": "full_payment",
                    "payment_plan": f"{req_date_str}:{_fmt_amt(req_amt)}",
                    "spending_changes_needed": f"stop:{sc['event_id']}|reduce_to:{rc['event_id']}:{_fmt_amt(new_amt)}",
                    "completes_by_desired": req_date <= desired_date,
                    "has_spending_changes": True,
                    "total_paid": req_amt,
                    "start_date": req_date_str,
                    "num_payments": 1,
                    "option_id": 999999,
                    "explanation": f"Affordable with full payment by stopping {sc['event_id']} and reducing {rc['event_id']}."
                })

    if candidates:
        candidates.sort(key=lambda c: (
            not c["completes_by_desired"],
            c["has_spending_changes"],
            c["total_paid"],
            c["start_date"],
            c["num_payments"],
            c["option_id"]
        ))
        best = candidates[0]
        if best["affordability_status"] == "affordable_now":
            earliest_full = req_date_str
        else:
            earliest_full = earliest_date_for_full

        return {
            "request_id": request_id,
            "amount_safe_to_pay": _fmt_amt(amount_safe_to_pay),
            "affordability_status": best["affordability_status"],
            "recommended_payment_method": best["recommended_payment_method"],
            "payment_plan": best["payment_plan"],
            "earliest_date_for_full_payment": earliest_full,
            "spending_changes_needed": best["spending_changes_needed"],
            "decision_explanation": best["explanation"],
        }
    else:
        return {
            "request_id": request_id,
            "amount_safe_to_pay": _fmt_amt(amount_safe_to_pay),
            "affordability_status": "not_affordable",
            "recommended_payment_method": "not_recommended",
            "payment_plan": "none",
            "earliest_date_for_full_payment": "",
            "spending_changes_needed": "none",
            "decision_explanation": (
                f"Cannot safely complete full requested amount of {_fmt_amt(req_amt)} "
                f"within forecast period without violating minimum required balance."
            ),
        }
