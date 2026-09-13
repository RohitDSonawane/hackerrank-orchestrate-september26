import csv
import os
import sys

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CODE_DIR)

from data_loader import load_all
from agent_loop import deterministic_solve_single
from validator import validate_row

DATASET_DIR = os.path.join(CODE_DIR, "..", "dataset")
OUTPUT_FILE = os.path.join(CODE_DIR, "..", "output.csv")


def load_csv(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def score_against_sample():
    data = load_all()
    samples = data["sample"]
    sample_ids = list(samples.keys())

    print(f"\nEvaluating solver against {len(sample_ids)} calibration samples...")
    predictions = {}
    validation_failures = 0

    for sid in sample_ids:
        pred = deterministic_solve_single(sid, data)
        viols = validate_row(pred, data)
        if viols:
            validation_failures += 1
        predictions[sid] = pred

    n = len(sample_ids)
    exact_status = sum(1 for sid in sample_ids if predictions[sid]["affordability_status"] == samples[sid]["affordability_status"])
    exact_method = sum(1 for sid in sample_ids if predictions[sid]["recommended_payment_method"] == samples[sid]["recommended_payment_method"])

    print(f"\n==========================================")
    print(f"=== Ground Truth Calibration Results ({n} Samples) ===")
    print(f"==========================================")
    print(f"  Contract Validation Pass Rate: {n - validation_failures}/{n} ({100*(n - validation_failures)/n:.1f}%)")
    print(f"  affordability_status Match:    {exact_status}/{n} ({100*exact_status/n:.1f}%)")
    print(f"  recommended_payment_method:    {exact_method}/{n} ({100*exact_method/n:.1f}%)")

    print("\nSample Comparisons (First 5):")
    for sid in sample_ids[:5]:
        pred = predictions[sid]
        gt = samples[sid]
        status_match = "[MATCH]" if pred["affordability_status"] == gt["affordability_status"] else "[MISMATCH]"
        print(f"  [{sid}] {status_match} Pred: {pred['affordability_status']} ({pred['recommended_payment_method']}) | GT: {gt['affordability_status']} ({gt['recommended_payment_method']})")


def score_output_file():
    sample_path = os.path.join(DATASET_DIR, "sample_requests.csv")
    samples = {r["request_id"]: r for r in load_csv(sample_path)}
    output = {r["request_id"]: r for r in load_csv(OUTPUT_FILE)}

    if not output:
        print(f"[evaluator] Output file not found: {OUTPUT_FILE}")
        return

    common = sorted(list(set(samples.keys()) & set(output.keys())))
    if not common:
        print("[evaluator] Output file contains 250 competition test requests (request_26..request_275).")
        print("Running calibration evaluation against sample ground truth dataset:")
        score_against_sample()
        return

    n = len(common)
    exact_status = sum(1 for r in common if output[r]["affordability_status"] == samples[r]["affordability_status"])
    exact_method = sum(1 for r in common if output[r]["recommended_payment_method"] == samples[r]["recommended_payment_method"])
    exact_plan = sum(1 for r in common if output[r]["payment_plan"] == samples[r]["payment_plan"])
    exact_changes = sum(1 for r in common if output[r]["spending_changes_needed"] == samples[r]["spending_changes_needed"])

    print(f"\n==========================================")
    print(f"=== Self-Evaluation ({n} Overlapping Requests) ===")
    print(f"==========================================")
    print(f"  affordability_status:       {exact_status}/{n} ({100*exact_status/n:.1f}%)")
    print(f"  recommended_payment_method: {exact_method}/{n} ({100*exact_method/n:.1f}%)")
    print(f"  payment_plan:               {exact_plan}/{n} ({100*exact_plan/n:.1f}%)")
    print(f"  spending_changes_needed:    {exact_changes}/{n} ({100*exact_changes/n:.1f}%)")


if __name__ == "__main__":
    score_output_file()
