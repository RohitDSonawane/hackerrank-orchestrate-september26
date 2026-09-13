# Few-Shot Examples — Buy or Wait?

Condensed from `dataset/sample_requests.csv`. These illustrate the expected reasoning style and output format. Use them to calibrate your decisions.

---

## Example 1: Full Payment (affordable_now)

**Context:** User has ZAR 58,481 balance, minimum ZAR 18,000. Requesting ZAR 25,256 for a laptop today.

**Reasoning:**
- Called `get_context(request_01)` — balance 58,481; recurring rent ~5,148/mo, utilities ~1,475/mo, education ~1,821/mo, debt ~3,487/mo. No messages, no images.
- Events recurring: rent, utilities, education, debt payment (all appear monthly). Delivery subscription is stoppable but not needed here.
- Called `check_plan` with all recurring events included, `candidate_plan = [{"date": "2024-03-03", "amount": 25256}]`
- Result: `is_safe = true`, `min_balance_reached = 18,000+` — passes.
- User accepts `full_payment`. No spending changes needed.

**Output:**
```json
{
  "request_id": "request_01",
  "amount_safe_to_pay": 25256,
  "affordability_status": "affordable_now",
  "recommended_payment_method": "full_payment",
  "payment_plan": "2024-03-03:25256",
  "earliest_date_for_full_payment": "2024-03-03",
  "spending_changes_needed": "none",
  "decision_explanation": "Pay ZAR 25,256 today. This leaves at least ZAR 18,000 available over the next 90 days."
}
```

---

## Example 2: Installments (affordable_with_plan)

**Context:** User has IDR 60,383,889 balance, minimum IDR 29,158,400. Requesting IDR 46,018,000 for travel by Oct 10.

**Reasoning:**
- Paying full amount today would dip below minimum. Called `check_plan` with full payment — `is_safe = false`.
- Checked installment options. Option with 3 payments of IDR 15,952,906.67 starting Aug 8, monthly — called `check_plan` with this schedule — `is_safe = true`.
- User accepts installments, max_installment_months covers 3.
- `amount_safe_to_pay` = IDR 17,229,139.20 (what they can pay today before spending changes — checked with `check_plan` for a single today payment)
- `earliest_date_for_full_payment` = Sep 15 (next salary settlement makes full payment safe)

**Output:**
```json
{
  "request_id": "request_02",
  "amount_safe_to_pay": 17229139.2,
  "affordability_status": "affordable_with_plan",
  "recommended_payment_method": "installments",
  "payment_plan": "2025-08-08:15952906.67|2025-09-07:15952906.67|2025-10-07:15952906.67",
  "earliest_date_for_full_payment": "2025-09-15",
  "spending_changes_needed": "none",
  "decision_explanation": "Use 3 installments of IDR 15,952,906.67, starting 8 August 2025. This leaves at least IDR 29,158,400 available."
}
```

---

## Example 3: Wait (affordable_later)

**Context:** User requesting IDR 5,491,000 for education by Nov 15. Balance too low now.

**Reasoning:**
- `check_plan` with today payment — fails, would breach minimum.
- Scanned forward: next salary settles Nov 15. Called `check_plan` with `candidate_plan = [{"date": "2019-11-15", "amount": 5491000}]` — passes.
- Nov 15 is on or before desired_completion_date (Nov 15) — viable.
- User accepts `full_payment`. `wait` is eligible.

**Output:**
```json
{
  "request_id": "request_03",
  "amount_safe_to_pay": 873000,
  "affordability_status": "affordable_later",
  "recommended_payment_method": "wait",
  "payment_plan": "2019-11-15:5491000",
  "earliest_date_for_full_payment": "2019-11-15",
  "spending_changes_needed": "none",
  "decision_explanation": "Pay IDR 5,491,000 in full on 15 November 2019. Paying earlier would take the balance below the IDR 2,668,700 minimum."
}
```

---

## Example 4: Spending Change Required (affordable_with_plan + full_payment)

**Context:** User requesting EUR 620.40 for investment by Jan 14. Has a family streaming plan that's stoppable.

**Reasoning:**
- `check_plan` without spending changes — fails by small margin.
- Streaming subscription (event_476) is in `expense_categories_user_is_willing_to_stop`, `flexibility = stoppable`.
- `check_plan` with `excluded_event_ids = ["event_476"]` and today full payment — passes.
- User accepts `full_payment`. Spending change: `stop:event_476`.
- `amount_safe_to_pay`: 603.30 (without stopping the subscription — checked separately).

**Output:**
```json
{
  "request_id": "request_06",
  "amount_safe_to_pay": 603.3,
  "affordability_status": "affordable_with_plan",
  "recommended_payment_method": "full_payment",
  "payment_plan": "2026-01-03:620.40",
  "earliest_date_for_full_payment": "2026-01-15",
  "spending_changes_needed": "stop:event_476",
  "decision_explanation": "Stop the family streaming plan, then pay EUR 620.40 today. This leaves at least EUR 800 available."
}
```

---

## Example 5: Not Affordable (not_affordable + not_recommended)

**Context:** User requesting ZAR 15,488 for debt repayment. Balance insufficient even after checking all options.

**Reasoning:**
- `check_plan` with all options — all fail within the 90-day forecast window.
- No eligible spending changes available.
- No installment options fit within budget.
- `amount_safe_to_pay` = ZAR 737 (single today payment that just passes safety check).
- `earliest_date_for_full_payment` = empty (never safe within 90 days).

**Output:**
```json
{
  "request_id": "request_05",
  "amount_safe_to_pay": 737,
  "affordability_status": "not_affordable",
  "recommended_payment_method": "not_recommended",
  "payment_plan": "none",
  "earliest_date_for_full_payment": "",
  "spending_changes_needed": "none",
  "decision_explanation": "Do not make this payment by 12 January 2026. None of the available options keeps the ZAR 13,100 minimum protected."
}
```

---

## Key Patterns to Remember

| Pattern | What to do |
|---|---|
| Blank event amount | Find `event_id` in `images.csv -> related_event_id`, read from image, pass as `event_amount_overrides` |
| Message mentions salary change | Extract new salary amount and effective date — update your recurring income projection |
| Pending debit | Include as reserved expense — conservative |
| Pending credit | Exclude until settled — conservative |
| linked_event_id present | Identify the lifecycle (pending -> settled) — use only the settled/latest version |
| Message says event is cancelled | Pass that event_id to `excluded_event_ids` in `check_plan` |
