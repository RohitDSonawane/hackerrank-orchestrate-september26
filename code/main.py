import csv
import json
import os
import sys
import time
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv

load_dotenv()

# Add code/ directory to path
CODE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(CODE_DIR)
sys.path.insert(0, CODE_DIR)

from data_loader import load_all
from agent_loop import run_batch, KeyPool
from validator import validate_row, fallback_row, REQUIRED_COLUMNS
from usage_tracker import UsageTracker

OUTPUT_PATH = os.path.join(REPO_ROOT, "output.csv")
USAGE_REPORT_PATH = os.path.join(CODE_DIR, "evaluation", "usage_report.md")


def load_prompt(filename: str) -> str:
    path = os.path.join(CODE_DIR, "prompts", filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def main():
    print("=" * 60)
    print("Buy or Wait? — Financial Advisory Agent Pipeline")
    print(f"Repository Root: {REPO_ROOT}")
    print("=" * 60)

    # 1. Load data
    print("\n[Step 1/5] Loading datasets...")
    data = load_all()
    requests_list = data["requests_list"]
    total_requests = len(requests_list)
    print(f"Loaded {total_requests} requests to process.")

    # 2. Setup agent loop components
    print("\n[Step 2/5] Initializing agent and prompt templates...")
    key_pool = KeyPool()
    tracker = UsageTracker()
    system_prompt = load_prompt("system_prompt.md")
    few_shot = load_prompt("few_shot_examples.md")

    if key_pool.has_keys:
        print(f"OpenRouter key pool configured with {len(key_pool.keys)} active key(s).")
    else:
        print("No OPENROUTER API keys set; utilizing deterministic financial simulation solver.")

    # 3. Process batches
    print("\n[Step 3/5] Processing requests in batches...")
    batch_size = 20
    all_results = {}
    failing_rows = []

    request_ids_all = [r["request_id"] for r in requests_list]

    for i in range(0, total_requests, batch_size):
        batch_ids = request_ids_all[i : i + batch_size]
        batch_num = (i // batch_size) + 1
        total_batches = (total_requests + batch_size - 1) // batch_size
        print(f"  -> Batch {batch_num}/{total_batches}: processing {len(batch_ids)} requests ({batch_ids[0]}..{batch_ids[-1]})...")

        batch_output = run_batch(
            request_ids=batch_ids,
            data=data,
            key_pool=key_pool,
            tracker=tracker,
            system_prompt=system_prompt,
            few_shot=few_shot,
            budget_remaining=100,
        )

        # Map and validate
        output_map = {r.get("request_id"): r for r in batch_output if isinstance(r, dict)}

        for req_id in batch_ids:
            row = output_map.get(req_id)
            if not row:
                row = fallback_row(req_id, data)
                all_results[req_id] = row
                failing_rows.append((req_id, ["Missing from model batch response"]))
                continue

            violations = validate_row(row, data)
            if violations:
                failing_rows.append((req_id, violations))
                all_results[req_id] = row
            else:
                all_results[req_id] = row

    # 4. Repair pass for failing rows
    print(f"\n[Step 4/5] Validation check: {len(failing_rows)} rows flagged for repair...")
    if failing_rows and key_pool.has_keys:
        print("Running targeted repair pass on failing rows...")
        repair_ids = [req_id for req_id, _ in failing_rows]
        repair_msg = "Please correct these specific contract errors:\n"
        for req_id, viols in failing_rows[:10]:
            repair_msg += f"- {req_id}: {'; '.join(viols)}\n"

        repaired = run_batch(
            request_ids=repair_ids,
            data=data,
            key_pool=key_pool,
            tracker=tracker,
            system_prompt=system_prompt,
            few_shot=few_shot,
            budget_remaining=50,
            extra_user_message=repair_msg,
        )

        repaired_map = {r.get("request_id"): r for r in repaired if isinstance(r, dict)}
        for req_id in repair_ids:
            r_row = repaired_map.get(req_id)
            if r_row and not validate_row(r_row, data):
                all_results[req_id] = r_row
            else:
                all_results[req_id] = fallback_row(req_id, data)
    elif failing_rows:
        print("Applying safe deterministic fallbacks to flagged rows...")
        for req_id, _ in failing_rows:
            all_results[req_id] = fallback_row(req_id, data)

    # Final sanity check: Ensure all 250 requests are present
    for r in requests_list:
        rid = r["request_id"]
        if rid not in all_results:
            all_results[rid] = fallback_row(rid, data)

    # 5. Write output.csv
    print(f"\n[Step 5/5] Writing final output to {OUTPUT_PATH}...")
    with open(OUTPUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REQUIRED_COLUMNS)
        writer.writeheader()
        for r in requests_list:
            rid = r["request_id"]
            row = all_results[rid]
            # Ensure only required columns are written and amounts are clean numbers
            clean_row = {col: row.get(col, "") for col in REQUIRED_COLUMNS}
            try:
                val = round(float(clean_row["amount_safe_to_pay"]), 2)
                clean_row["amount_safe_to_pay"] = str(int(val)) if val.is_integer() else f"{val:.2f}"
            except Exception:
                pass
            writer.writerow(clean_row)

    # Write usage report
    tracker.write_report(USAGE_REPORT_PATH, num_requests=total_requests)

    # Final verification stats
    total_written = len(all_results)
    print("\n" + "=" * 60)
    print("PIPELINE COMPLETED SUCCESSFULLY")
    print(f"Total rows written to output.csv: {total_written}")
    print(f"Output path: {OUTPUT_PATH}")
    print(f"Usage report: {USAGE_REPORT_PATH}")
    print("=" * 60)


if __name__ == "__main__":
    main()
