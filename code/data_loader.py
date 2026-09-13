import csv
import os

DATASET_DIR = os.path.join(os.path.dirname(__file__), "..", "dataset")


def _load(filename: str):
    """Load a CSV file and return list of row dicts."""
    path = os.path.join(DATASET_DIR, filename)
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _group_by(rows: list, key: str) -> dict:
    """Group a list of dicts by a field value."""
    result = {}
    for row in rows:
        k = row[key]
        result.setdefault(k, []).append(row)
    return result


def load_all() -> dict:
    """
    Returns a dict of all dataset tables and indexed lookups.
    All values are lists of row dicts or mapped dictionaries.
    """
    requests = _load("requests.csv")
    profiles = _load("financial_profiles.csv")
    events = _load("financial_events.csv")
    rates = _load("exchange_rates.csv")
    options = _load("request_payment_options.csv")
    messages = _load("messages.csv")
    images = _load("images.csv")
    sample = _load("sample_requests.csv")  # 25 ground-truth requests (request_01..request_25)

    req_map = {r["request_id"]: r for r in requests}
    sample_map = {r["request_id"]: r for r in sample}

    # Combined map for transparent testing, evaluation and inference
    all_requests = {**sample_map, **req_map}

    return {
        "requests": req_map,            # 250 submission requests (request_26..request_275)
        "requests_list": requests,
        "sample": sample_map,           # 25 calibration samples (request_01..request_25)
        "sample_list": sample,
        "all_requests": all_requests,   # complete lookup by request_id
        "profiles": {r["user_id"]: r for r in profiles},
        "events_by_user": _group_by(events, "user_id"),
        "events_by_id": {r["event_id"]: r for r in events},
        "rates": rates,
        "options_by_req": _group_by(options, "request_id"),
        "msgs_by_user": _group_by(messages, "user_id"),
        "imgs_by_user": _group_by(images, "user_id"),
        "imgs_by_event": {r["related_event_id"]: r for r in images if r.get("related_event_id")},
    }
