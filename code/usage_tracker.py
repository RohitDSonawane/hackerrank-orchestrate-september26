import json
import os
import time
from datetime import datetime


class UsageTracker:
    def __init__(self):
        self.calls = []

    def log_call(self, response: dict, key_idx: int, batch_id: int = 0,
                 request_ids: list = None, is_tool_turn: bool = False):
        usage = response.get("usage", {}) if isinstance(response, dict) else {}
        self.calls.append({
            "timestamp": datetime.utcnow().isoformat(),
            "model": response.get("model", "unknown") if isinstance(response, dict) else "offline_fallback",
            "key_index": key_idx,  # index only — NEVER log the actual key
            "batch_id": batch_id,
            "request_ids": request_ids or [],
            "is_tool_turn": is_tool_turn,
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        })

    @property
    def total_calls(self):
        return len(self.calls)

    @property
    def total_input_tokens(self):
        return sum(c["input_tokens"] for c in self.calls)

    @property
    def total_output_tokens(self):
        return sum(c["output_tokens"] for c in self.calls)

    @property
    def total_tokens(self):
        return self.total_input_tokens + self.total_output_tokens

    def write_report(self, output_path: str, num_requests: int = 250):
        """Write evaluation/usage_report.md"""
        avg_per_req = self.total_tokens / max(num_requests, 1)
        tool_turns = sum(1 for c in self.calls if c["is_tool_turn"])

        lines = [
            "# Token Usage and Cost Report",
            "",
            f"**Generated:** {datetime.utcnow().isoformat()} UTC",
            f"**Run:** Final full-dataset run ({num_requests} requests)",
            "",
            "## Model",
            "",
            "| Provider | Model | Tier |",
            "|---|---|---|",
            "| OpenRouter | google/gemma-4-31b-it:free | free |",
            "",
            "## Summary",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Total API calls | {self.total_calls} |",
            f"| Tool-call turns | {tool_turns} |",
            f"| Final-answer turns | {self.total_calls - tool_turns} |",
            f"| Total input tokens | {self.total_input_tokens:,} |",
            f"| Total output tokens | {self.total_output_tokens:,} |",
            f"| Total tokens | {self.total_tokens:,} |",
            f"| Avg tokens per request | {avg_per_req:.0f} |",
            "| Estimated cost | $0.00 (free tier) |",
            "| Notional rate | $0.00 / 1K tokens (free) |",
            "",
            "## Per-Batch Breakdown",
            "",
            "| Batch | Calls | Input Tokens | Output Tokens |",
            "|---|---|---|---|",
        ]

        batches = {}
        for c in self.calls:
            b = c["batch_id"]
            batches.setdefault(b, {"calls": 0, "in": 0, "out": 0})
            batches[b]["calls"] += 1
            batches[b]["in"] += c["input_tokens"]
            batches[b]["out"] += c["output_tokens"]

        for b, stats in sorted(batches.items()):
            lines.append(f"| {b} | {stats['calls']} | {stats['in']:,} | {stats['out']:,} |")

        lines += [
            "",
            "## Notes",
            "",
            "- API keys are completely redacted from this report.",
            "- 'Tool-call turns' = turns where the model invoked get_context, convert_currency, or check_plan.",
            "- 'Final-answer turns' = turns where the model returned its structured JSON array.",
        ]

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"[usage_tracker] Report successfully written to {output_path}")
