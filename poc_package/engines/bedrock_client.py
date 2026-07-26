import boto3
import json
import time
import re
import os

from logger_config import get_logger
logger = get_logger(__name__)

MODEL_HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
REGION      = "us-east-1"

_client = None

_token_stats = {
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0,
    "requests": 0,
}

_langfuse = None


def _aws_env_config():
    access_key = os.getenv("AWS_ACCESS_KEY_ID")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    session_token = os.getenv("AWS_SESSION_TOKEN")
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or REGION
    return access_key, secret_key, session_token, region


def _init_langfuse():
    """Create the Langfuse client once, if configured."""
    global _langfuse
    if _langfuse is not None:
        return _langfuse

    try:
        from langfuse import Langfuse
    except Exception as e:
        logger.info(f"[langfuse] disabled (package unavailable): {e}")
        return None

    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    base_url   = os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")

    if not public_key or not secret_key:
        logger.info("[langfuse] disabled (missing LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY)")
        return None

    try:
        _langfuse = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=base_url,
        )
        logger.info(f"[langfuse] enabled | host={base_url}")
        return _langfuse
    except Exception as e:
        logger.warning(f"[langfuse] init failed, continuing without tracing: {e}")
        _langfuse = None
        return None

def get_client():
    global _client
    if _client is None:
        access_key, secret_key, session_token, region = _aws_env_config()
        if not access_key or not secret_key:
            raise RuntimeError(
                "Missing AWS credentials in .env. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY "
                "(and optionally AWS_SESSION_TOKEN / AWS_REGION)."
            )

        client_kwargs = {
            "service_name": "bedrock-runtime",
            "region_name": region,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
        }
        if session_token:
            client_kwargs["aws_session_token"] = session_token

        _client = boto3.client(**client_kwargs)
    return _client


def get_token_stats() -> dict:
    return _token_stats.copy()


def print_token_usage(input_tokens=0, output_tokens=0, total=0):
    _token_stats["input_tokens"]  += input_tokens     # ← increment FIRST
    _token_stats["output_tokens"] += output_tokens
    _token_stats["total_tokens"]  += total
    _token_stats["requests"]      += 1

    ... # existing box-drawing print() calls ...

    logger.info(                                       # ← THEN log, after increment
        f"bedrock call | input_tokens={input_tokens} output_tokens={output_tokens} "
        f"total={total} session_total={_token_stats['total_tokens']} requests={_token_stats['requests']}"
    )


def ask(prompt: str,
        system: str = "",
        model: str = None,
        max_tokens: int = 16000,
        retries: int = 3,
        verbose: bool = True) -> str:
    model = model or MODEL_HAIKU
    lf = _init_langfuse()
    body  = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    for attempt in range(retries):
        try:
            t0 = time.time()
            if lf:
                with lf.start_as_current_observation(
                    name="bedrock.ask",
                    as_type="generation",
                    # input={"prompt": prompt, "system": system},
                    # metadata={"max_tokens": max_tokens},
                    model=model,
                ) as generation:
                    resp = get_client().invoke_model(
                        modelId     = model,
                        body        = json.dumps(body),
                        # contentType = "application/json",
                        # accept      = "application/json",
                    )
                    logger.info(f"[bedrock] invoke_model network+generation time: {time.time()-t0:.3f}s")

                    t0 = time.time()
                    result = json.loads(resp["body"].read())
                    logger.info(f"[bedrock] response body read+json.loads: {time.time()-t0:.3f}s")

                    usage = result.get("usage", {})
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    total_tokens = input_tokens + output_tokens

                    generation.update(
                        output=result,
                        metadata={"max_tokens": max_tokens},
                        usage_details={
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "total_tokens": total_tokens,
                        },
                    )
            else:
                resp = get_client().invoke_model(
                    modelId     = model,
                    body        = json.dumps(body),
                    contentType = "application/json",
                    accept      = "application/json",
                )
                logger.info(f"[bedrock] invoke_model network+generation time: {time.time()-t0:.3f}s")

                t0 = time.time()
                result = json.loads(resp["body"].read())
                logger.info(f"[bedrock] response body read+json.loads: {time.time()-t0:.3f}s")

                usage = result.get("usage", {})
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                total_tokens = input_tokens + output_tokens

            try:
                if lf:
                    lf.flush()
            except Exception as e:
                logger.debug(f"[langfuse] flush failed: {e}")

            if verbose:
                print_token_usage(input_tokens, output_tokens, total_tokens)

            return result["content"][0]["text"]

        except Exception as e:
            err = str(e)
            if "ThrottlingException" in err and attempt < retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning(f"Bedrock throttled, waiting {wait}s before retry {attempt+2}/{retries}")
                time.sleep(wait)
            else:
                logger.error(f"Bedrock call failed: {err}", exc_info=True)
                raise


def ask_json(prompt: str,
             system: str = "",
             model: str = None,
             max_tokens: int = 16000,
             verbose: bool = True) -> dict:
    full_system = (system + "\n\n" if system else "") + \
                  "CRITICAL: Return ONLY valid JSON. No markdown fences, no explanation, no preamble. Use double quotes only, never apostrophes inside strings."

    t0 = time.time()
    raw = ask(prompt, system=full_system, model=model, max_tokens=max_tokens, verbose=verbose)
    logger.info(f"[ask_json] ask() total (network+generation+read): {time.time()-t0:.3f}s")

    t0 = time.time()
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
        parsed = json.loads(raw)
        logger.info(f"[ask_json] cleanup + json.loads parse: {time.time()-t0:.3f}s")
        return parsed
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse failed: {e} | raw (first 500 chars): {raw[:500]}")
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
