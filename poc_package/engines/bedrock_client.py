import boto3
import json
import time
import re

MODEL_HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
REGION      = "us-east-1"

_client = None

_token_stats = {
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0,
    "requests": 0,
}

def get_client():
    global _client
    if _client is None:
        _client = boto3.client("bedrock-runtime", region_name=REGION)
    return _client


def get_token_stats() -> dict:
    return _token_stats.copy()


def print_token_usage(input_tokens: int = 0, output_tokens: int = 0, total: int = 0):
    _token_stats["input_tokens"] += input_tokens
    _token_stats["output_tokens"] += output_tokens
    _token_stats["total_tokens"] += total
    _token_stats["requests"] += 1

    CONTEXT_WINDOW = 100000

    used_pct = (_token_stats["total_tokens"] / CONTEXT_WINDOW) * 100
    remaining = CONTEXT_WINDOW - _token_stats["total_tokens"]

    bar_width = 30
    filled = int((used_pct / 100) * bar_width)
    bar = "█" * filled + "░" * (bar_width - filled)

    if used_pct < 50:
        status = "✅ GOOD"
        color_code = "\033[92m"
    elif used_pct < 80:
        status = "⚠️  CAUTION"
        color_code = "\033[93m"
    else:
        status = "🔴 CRITICAL"
        color_code = "\033[91m"

    reset_code = "\033[0m"

    print(f"\n{color_code}{'═' * 70}{reset_code}")
    print(f"{color_code}  TOKEN USAGE REPORT{reset_code}")
    print(f"{color_code}{'═' * 70}{reset_code}")
    print(f"\n  This Request:")
    print(f"    Input:   {input_tokens:>7,} tokens")
    print(f"    Output:  {output_tokens:>7,} tokens")
    print(f"    Total:   {total:>7,} tokens")
    print(f"\n  Session Total (Requests: {_token_stats['requests']}):")
    print(f"    Input:   {_token_stats['input_tokens']:>7,} tokens")
    print(f"    Output:  {_token_stats['output_tokens']:>7,} tokens")
    print(f"    Total:   {_token_stats['total_tokens']:>7,} tokens  ({used_pct:.1f}%)")
    print(f"\n  Capacity:")
    print(f"    {status}")
    print(f"    [{bar}]")
    print(f"    Used: {_token_stats['total_tokens']:,} / {CONTEXT_WINDOW:,}")
    print(f"    Remaining: {remaining:,} tokens")
    print(f"\n{color_code}{'═' * 70}{reset_code}\n")


def ask(prompt: str,
        system: str = "",
        model: str = None,
        max_tokens: int = 16000,
        retries: int = 3,
        verbose: bool = True) -> str:
    model = model or MODEL_HAIKU
    body  = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system

    for attempt in range(retries):
        try:
            resp = get_client().invoke_model(
                modelId     = model,
                body        = json.dumps(body),
                contentType = "application/json",
                accept      = "application/json",
            )
            result = json.loads(resp["body"].read())

            usage = result.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
            total_tokens = input_tokens + output_tokens

            if verbose:
                print_token_usage(input_tokens, output_tokens, total_tokens)

            return result["content"][0]["text"]

        except Exception as e:
            err = str(e)
            if "ThrottlingException" in err and attempt < retries - 1:
                wait = 2 ** (attempt + 1)
                print(f"  [throttled] waiting {wait}s before retry {attempt+2}/{retries}...")
                time.sleep(wait)
            else:
                raise


def ask_json(prompt: str,
             system: str = "",
             model: str = None,
             max_tokens: int = 16000,
             verbose: bool = True) -> dict:
    full_system = (system + "\n\n" if system else "") + \
                  "CRITICAL: Return ONLY valid JSON. No markdown fences, no explanation, no preamble. Use double quotes only, never apostrophes inside strings."

    raw = ask(prompt, system=full_system, model=model, max_tokens=max_tokens, verbose=verbose)

    raw = raw.strip()
    raw = re.sub(r'^```(?:json)?\s*', '', raw)
    raw = re.sub(r'\s*```\s*$', '', raw)
    raw = raw.strip()

    start = len(raw)
    if raw.find('{') != -1:
        start = min(start, raw.find('{'))
    if raw.find('[') != -1:
        start = min(start, raw.find('['))
    end = max(raw.rfind('}'), raw.rfind(']')) + 1
    if 0 <= start < end:
        raw = raw[start:end]

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"\n  [DEBUG] JSON parse failed: {e}")
        print(f"  [DEBUG] Raw response (first 500 chars):\n{raw[:500]}")
        raise


def print_separator(title: str = "", char: str = "─", width: int = 65):
    if title:
        pad = (width - len(title) - 2) // 2
        print(f"\n{'─'*pad} {title} {'─'*pad}")
    else:
        print("─" * width)


def reset_token_stats():
    global _token_stats
    _token_stats = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "requests": 0,
    }
    print("\n  [reset] Token statistics cleared\n")