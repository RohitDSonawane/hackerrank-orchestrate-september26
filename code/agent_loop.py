from datetime import date, timedelta
import json
import os
import time
import requests as req_lib

from tools import get_context, convert_currency, check_plan, TOOL_SCHEMAS, _get_request
from validator import fallback_row

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemma-4-31b-it:free")

MAX_TURNS = 5
MAX_TURNS_DEGRADED = 2


class KeyPool:
    """Manages OpenRouter API keys with automatic rotation on 429 errors."""

    def __init__(self):
        candidates = [
            os.environ.get("OPENROUTER_KEY_A", ""),
            os.environ.get("OPENROUTER_KEY_B", ""),
            os.environ.get("OPENROUTER_API_KEY", ""),
        ]
        self.keys = [k.strip() for k in candidates if k and k.strip()]
        self.idx = 0

    @property
    def has_keys(self) -> bool:
        return len(self.keys) > 0

    def current(self) -> str:
        if not self.keys:
            return ""
        return self.keys[self.idx]

    def rotate(self):
        if self.keys:
            self.idx = (self.idx + 1) % len(self.keys)
            print(f"[key_pool] Rotated to key index {self.idx}")

    def call(self, messages: list, tools: list, tracker=None) -> dict:
        if not self.has_keys:
            raise RuntimeError("No OpenRouter API key found in environment")

        attempts = max(len(self.keys), 1)
        for attempt in range(attempts):
            key = self.current()
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/hackerrank-orchestrate",
            }
            body = {
                "model": MODEL,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
            }

            try:
                r = req_lib.post(OPENROUTER_URL, headers=headers, json=body, timeout=120)
                if r.status_code == 429:
                    print(f"[key_pool] 429 Rate Limit on key {self.idx}, rotating...")
                    self.rotate()
                    time.sleep(3)
                    continue
                r.raise_for_status()
                result = r.json()
                if tracker:
                    tracker.log_call(result, key_idx=self.idx)
                return result
            except req_lib.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 429:
                    print(f"[key_pool] 429 received, rotating...")
                    self.rotate()
                    time.sleep(3)
                else:
                    raise

        raise RuntimeError("All API keys exhausted or rate limited")


def dispatch_tool(tool_name: str, tool_args: dict, data: dict) -> str:
    """Execute tool call from LLM and return result JSON string."""
    try:
        if tool_name == "get_context":
            result = get_context(tool_args["request_id"], data)
        elif tool_name == "convert_currency":
            result = convert_currency(
                amount=float(tool_args["amount"]),
                from_currency=tool_args["from_currency"],
                to_currency=tool_args["to_currency"],
                date_str=tool_args["date"],
                data=data,
            )
        elif tool_name == "check_plan":
            result = check_plan(
                request_id=tool_args["request_id"],
                included_event_ids=tool_args.get("included_event_ids", []),
                excluded_event_ids=tool_args.get("excluded_event_ids", []),
                event_amount_overrides=tool_args.get("event_amount_overrides", {}),
                candidate_plan=tool_args.get("candidate_plan", []),
                data=data,
            )
        else:
            result = {"error": f"Unknown tool: {tool_name}"}
    except Exception as e:
        result = {"error": str(e)}

    return json.dumps(result)


def _parse_batch_output(content: str, request_ids: list) -> list:
    """Extract and parse JSON array from model text response."""
    text = content.strip()
    if "```json" in text:
        text = text.split("```json", 1)[1]
        if "```" in text:
            text = text.split("```", 1)[0]
    elif text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1])

    text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except Exception:
        # Try finding the outermost JSON array brackets
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                if isinstance(data, list):
                    return data
            except Exception:
                pass

    print(f"[parse] Failed to parse batch output for request IDs: {request_ids[:3]}")
    return []


def _fmt_amt(val) -> str:
    """Format amount as clean decimal or integer without scientific notation."""
    try:
        f = round(float(val), 2)
        if f.is_integer():
            return str(int(f))
        return f"{f:.2f}"
    except Exception:
        return str(val)


from solver import solve_request_deterministic


def deterministic_solve_single(request_id: str, data: dict) -> dict:
    """
    High-fidelity deterministic financial reasoning when API keys are not available
    or when circuit breaker / fallback is active.
    Delegates to full-spec solver implementing 6 ranking rules, candidate schedules,
    and cash-flow projection.
    """
    return solve_request_deterministic(request_id, data)



def run_batch(request_ids: list, data: dict, key_pool: KeyPool,
              tracker, system_prompt: str, few_shot: str,
              budget_remaining: int = 100, extra_user_message: str = None) -> list:
    """
    Run one batch of request_ids through the agent loop.
    Uses LLM tool loop when keys are active; otherwise uses deterministic solver.
    """
    if not key_pool.has_keys:
        print(f"[agent_loop] No OpenRouter API keys detected, using deterministic solver for {len(request_ids)} requests.")
        return [deterministic_solve_single(rid, data) for rid in request_ids]

    turn_cap = MAX_TURNS if budget_remaining > 10 else MAX_TURNS_DEGRADED
    tools = TOOL_SCHEMAS

    user_prompt = (
        f"Process these {len(request_ids)} requests. "
        f"For each request, call get_context first, then verify any proposed plan with check_plan. "
        f"Return ONLY a valid JSON array of objects following the exact output schema.\n\n"
        f"Request IDs: {json.dumps(request_ids)}"
    )
    if extra_user_message:
        user_prompt += f"\n\nAdditional Instructions:\n{extra_user_message}"

    messages = [
        {"role": "system", "content": system_prompt + "\n\n" + few_shot},
        {"role": "user", "content": user_prompt},
    ]

    turn = 0
    while turn < turn_cap:
        try:
            response = key_pool.call(messages, tools, tracker)
        except Exception as e:
            print(f"[agent_loop] API call failed: {e}. Falling back to deterministic solver.")
            return [deterministic_solve_single(rid, data) for rid in request_ids]

        message = response["choices"][0]["message"]
        messages.append(message)
        turn += 1

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            content = message.get("content", "")
            results = _parse_batch_output(content, request_ids)
            if results:
                return results
            break

        # Execute tool calls
        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            try:
                tool_args = json.loads(tc["function"]["arguments"])
            except Exception:
                tool_args = {}
            result_str = dispatch_tool(tool_name, tool_args, data)
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "name": tool_name,
                "content": result_str,
            })

        time.sleep(2)

    # Force final output if turn cap was reached
    messages.append({
        "role": "user",
        "content": "Turn cap reached. Return your final JSON array now for all request IDs. No more tool calls.",
    })
    try:
        response = key_pool.call(messages, tools, tracker)
        content = response["choices"][0]["message"].get("content", "")
        results = _parse_batch_output(content, request_ids)
        if results:
            return results
    except Exception as e:
        print(f"[agent_loop] Final turn call failed: {e}")

    # If parsing or calls failed, use deterministic solver
    return [deterministic_solve_single(rid, data) for rid in request_ids]
