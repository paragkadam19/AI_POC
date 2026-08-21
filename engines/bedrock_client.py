import boto3
import json
import time
import re
import os
from dotenv import load_dotenv
import urllib3

from logger_config import get_logger
logger = get_logger(__name__)

from botocore.config import Config

config = Config(
    read_timeout=400,      # 5 minutes, adjust to your workload
    connect_timeout=50,
    retries={"max_attempts": 3, "mode": "adaptive"}
)

MODEL_HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
MODEL_TITAN_EMBED = "amazon.titan-embed-text-v2:0"
REGION      = "ap-south-1"

_client = None

_token_stats = {
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0,
    "requests": 0,
}

_langfuse = None

if os.getenv("DISABLE_SSL_VERIFICATION", "0").lower() in {"1", "true", "yes"}:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()

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

    logger.info(f"[langfuse] bootstrap | python={os.environ.get('VIRTUAL_ENV') or ''} | disable={os.getenv('DISABLE_LANGFUSE', '')}")

    if os.getenv("DISABLE_LANGFUSE", "").lower() in {"1", "true", "yes"}:
        logger.info("[langfuse] disabled via DISABLE_LANGFUSE")
        return None

    try:
        from langfuse import Langfuse
    except Exception as e:
        logger.info(f"[langfuse] disabled (package unavailable): {e}")
        return None

    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    base_url   = os.getenv("LANGFUSE_BASE_URL", "https://us.cloud.langfuse.com/")

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

        client_kwargs = {
            "service_name": "bedrock-runtime",
            "region_name": region,
        }
        # Only pass explicit keys when they're actually set (e.g. local dev
        # without an instance role attached). On EC2, leave these unset —
        # boto3's default credential chain then resolves the instance role
        # automatically via IMDS. The previous version *required* static
        # keys and raised if they were missing, which made it impossible
        # to use the IAM role this deployment is built around.
        if access_key and secret_key:
            client_kwargs["aws_access_key_id"] = access_key
            client_kwargs["aws_secret_access_key"] = secret_key
            if session_token:
                client_kwargs["aws_session_token"] = session_token

        # Same corporate-proxy workaround as main.py's DISABLE_SSL_VERIFICATION,
        # applied here too since boto3 has its own HTTP stack — the requests-
        # library monkeypatch in main.py doesn't cover Bedrock calls at all.
        # Off by default; only set the env var for local dev behind that proxy.
        disable_verify = os.getenv("DISABLE_SSL_VERIFICATION", "0").lower() in {"1", "true", "yes"}

        _client = boto3.client(**client_kwargs, verify=False, config=config)
    return _client


def get_token_stats() -> dict:
    return _token_stats.copy()


def print_token_usage(input_tokens=0, output_tokens=0, total=0):
    _token_stats["input_tokens"]  += input_tokens     # ← increment FIRST
    _token_stats["output_tokens"] += output_tokens
    _token_stats["total_tokens"]  += total
    _token_stats["requests"]      += 1

    logger.info(
        "\n"
        + "=" * 72
        + "\nBEDROCK TOKEN USAGE\n"
        + f"  input_tokens : {input_tokens}\n"
        + f"  output_tokens: {output_tokens}\n"
        + f"  total_tokens : {total}\n"
        + f"  session_total : {_token_stats['total_tokens']}\n"
        + f"  requests      : {_token_stats['requests']}\n"
        + "=" * 72
    )


def _consume_message_stream(resp, verbose: bool = True) -> dict:
    """
    Consumes an EventStream returned by invoke_model_with_response_stream
    for Anthropic Messages-API models, and reassembles it into the same
    shape a non-streaming invoke_model() call would have returned:
        {"content": [{"type": "text", "text": "..."}], "usage": {...}, "stop_reason": "..."}
    """
    text_parts = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    stop_reason = None

    for event in resp["body"]:
        chunk = event.get("chunk")
        if not chunk:
            continue

        chunk_data = json.loads(chunk["bytes"])
        event_type = chunk_data.get("type")

        if event_type == "message_start":
            msg_usage = chunk_data.get("message", {}).get("usage", {})
            usage["input_tokens"] = msg_usage.get("input_tokens", usage["input_tokens"])

        elif event_type == "content_block_delta":
            delta = chunk_data.get("delta", {})
            if delta.get("type") == "text_delta":
                text_piece = delta.get("text", "")
                text_parts.append(text_piece)
                if verbose:
                    print(text_piece, end="", flush=True)

        elif event_type == "message_delta":
            delta_usage = chunk_data.get("usage", {})
            if "output_tokens" in delta_usage:
                usage["output_tokens"] = delta_usage["output_tokens"]
            stop_reason = chunk_data.get("delta", {}).get("stop_reason", stop_reason)

        elif event_type == "message_stop":
            pass

    if verbose:
        print()  # newline after streamed text

    return {
        "content": [{"type": "text", "text": "".join(text_parts)}],
        "usage": usage,
        "stop_reason": stop_reason,
    }


def ask(prompt: str,
        system: str = "",
        model: str = None,
        max_tokens: int = 50000,
        temperature: float = 0.1,
        retries: int = 3,
        verbose: bool = True) -> str:
    model = model or MODEL_HAIKU
    lf = _init_langfuse()
    body  = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "temperature": temperature,
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
            logger.info(
                "\n"
                + "=" * 72
                + "\nBEDROCK REQUEST\n"
                + f"MODEL: {model}\n"
                + "SYSTEM:\n"
                + f"{system or '(empty)'}\n"
                + "PROMPT:\n"
                + f"{prompt or '(empty)'}\n"
                + "=" * 72
            )
            if lf:
                with lf.start_as_current_observation(
                    name="bedrock.ask",
                    as_type="generation",
                    # input={"prompt": prompt, "system": system},
                    # metadata={"max_tokens": max_tokens},
                    model=model,
                ) as generation:
                    resp = get_client().invoke_model_with_response_stream(
                        modelId     = model,
                        body        = json.dumps(body),
                    )
                    logger.info(f"[bedrock] invoke_model call setup time: {time.time()-t0:.3f}s")

                    t0 = time.time()
                    result = _consume_message_stream(resp, verbose=False)
                    logger.info(f"[bedrock] stream consume (network+generation): {time.time()-t0:.3f}s")
                    logger.info(
                        "\n"
                        + "=" * 72
                        + "\nBEDROCK RESPONSE\n"
                        + json.dumps(result, indent=2, ensure_ascii=False)[:4000]
                        + "\n"
                        + "=" * 72
                    )

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
                resp = get_client().invoke_model_with_response_stream(
                    modelId     = model,
                    body        = json.dumps(body),
                )
                logger.info(f"[bedrock] invoke_model call setup time: {time.time()-t0:.3f}s")

                t0 = time.time()
                result = _consume_message_stream(resp, verbose=False)
                logger.info(f"[bedrock] stream consume (network+generation): {time.time()-t0:.3f}s")
                logger.info(
                    "\n"
                    + "=" * 72
                    + "\nBEDROCK RESPONSE\n"
                    + json.dumps(result, indent=2, ensure_ascii=False)[:4000]
                    + "\n"
                    + "=" * 72
                )

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
             max_tokens: int = 50000,
             temperature: float = 0.1,
             verbose: bool = True) -> dict:
    full_system = (system + "\n\n" if system else "") + \
                  "CRITICAL: Return ONLY valid JSON. No markdown fences, no explanation, no preamble. Use double quotes only. Keep the JSON compact."

    def _clean_raw(text: str) -> str:
        text = (text or "").strip()
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```\s*$', '', text)
        text = text.strip()
        start = len(text)
        if text.find('{') != -1:
            start = min(start, text.find('{'))
        if text.find('[') != -1:
            start = min(start, text.find('['))
        end = max(text.rfind('}'), text.rfind(']')) + 1
        if 0 <= start < end:
            text = text[start:end]
        return text.strip()

    def _repair_common_json_issues(text: str) -> str:
        text = text.strip()
        # Remove trailing commas before closing containers.
        text = re.sub(r",\s*([}\]])", r"\1", text)
        # Normalize single-line control characters inside strings only as a
        # last resort for model output that inserted stray CR/LF pairs.
        text = text.replace("\r\n", "\n")
        return text

    def _attempt_parse(text: str):
        cleaned = _repair_common_json_issues(_clean_raw(text))
        return json.loads(cleaned), cleaned

    def _repair_json(bad_json: str, error_msg: str) -> str:
        repair_prompt = f"""
Fix the following invalid JSON and return ONLY valid JSON.
Do not add markdown or commentary.

JSON error:
{error_msg}

Broken JSON:
{bad_json}
"""
        return ask(repair_prompt, system=full_system, model=model, max_tokens=50000, verbose=False)

    t0 = time.time()
    raw = ask(
        prompt,
        system=full_system,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        verbose=verbose,
    )
    logger.info(f"[ask_json] ask() total (network+generation+read): {time.time()-t0:.3f}s")
    logger.info(
        "\n"
        + "=" * 72
        + "\nASK_JSON RAW TEXT\n"
        + f"{raw or '(empty)'}\n"
        + "=" * 72
    )

    try:
        t_parse = time.time()
        parsed, cleaned = _attempt_parse(raw)
        logger.info(f"[ask_json] cleanup + json.loads parse: {time.time()-t_parse:.3f}s")
        logger.info(
            "\n"
            + "=" * 72
            + "\nASK_JSON PARSED JSON\n"
            + json.dumps(parsed, indent=2, ensure_ascii=False)
            + "\n"
            + "=" * 72
        )
        return parsed
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse failed: {e} | raw (first 500 chars): {raw[:500]}")
        raw = _repair_json(_clean_raw(raw), str(e))
        t_parse = time.time()
        try:
            parsed, cleaned = _attempt_parse(raw)
            logger.info(f"[ask_json] repair + json.loads parse: {time.time()-t_parse:.3f}s")
            return parsed
        except json.JSONDecodeError as e2:
            logger.error(f"JSON repair failed: {e2} | repaired raw (first 500 chars): {raw[:500]}")
            raise ValueError(
                "Model returned invalid JSON twice. "
                "Try rerunning with a smaller prompt or lower output scope."
            ) from e2


def embed_text(text: str, model: str = None) -> list:
    """
    Generate a semantic embedding using Amazon Titan embeddings on Bedrock.
    Returns a plain list[float].

    NOTE: Embedding models are single-shot (no token-by-token generation),
    so this uses invoke_model (non-streaming), NOT
    invoke_model_with_response_stream. Titan embedding models don't
    benefit from — and in some cases don't support — the streaming API.
    """
    model = model or MODEL_TITAN_EMBED
    body = {"inputText": text or ""}
    resp = get_client().invoke_model(
        modelId=model,
        body=json.dumps(body),
        #contentType="application/json",
        #accept="application/json",
    )
    result = json.loads(resp["body"].read())
    embedding = result.get("embedding") or result.get("embeddings")
    if isinstance(embedding, list):
        return embedding
    raise ValueError("Bedrock embedding response did not contain an embedding vector.")


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
