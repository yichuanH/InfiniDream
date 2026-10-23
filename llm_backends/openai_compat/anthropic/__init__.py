"""Drop-in replacement for the `anthropic` SDK that routes every `messages.create`
to an OpenAI-compatible chat-completions endpoint (vLLM, SGLang, Ollama, OpenRouter...).

Activated by putting this package's parent directory FIRST on PYTHONPATH — the
`llm_backends/openai_compat/bin/python` wrapper does exactly that — so the 17 pipeline
scripts that `import anthropic` keep working unchanged.

Configuration (environment):
  LLM_OPENAI_BASE_URL   e.g. http://localhost:8000/v1          (required)
  LLM_OPENAI_API_KEY    any string for local servers            (default "EMPTY")
  LLM_OPENAI_MODEL      served model name, must accept images   (required)
                        e.g. Qwen/Qwen3-VL-8B-Instruct
  LLM_OPENAI_TIMEOUT    seconds per request                     (default 900)
  LLM_OPENAI_RETRIES    retries on 5xx / timeouts / rate limits (default 3)
  SUB_LOG               optional JSONL call log

Only the subset of the Anthropic API this repo uses is implemented: `Anthropic(...)`,
`client.messages.create(model=, system=, messages=, max_tokens=)` with text / base64
image / text-document blocks, and `messages.stream(...)` (non-streaming under the hood).
"""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

__version__ = "0.0.0-openai-compat-shim"


class AnthropicError(Exception):
    pass


class APIError(AnthropicError):
    def __init__(self, message: str = "", *args, **kwargs):
        super().__init__(message)
        self.message = message
        self.status_code = kwargs.get("status_code")


class APIStatusError(APIError):
    pass


class APIConnectionError(APIError):
    pass


class APITimeoutError(APIConnectionError):
    pass


class RateLimitError(APIStatusError):
    pass


class InternalServerError(APIStatusError):
    pass


class OverloadedError(APIStatusError):
    pass


class BadRequestError(APIStatusError):
    pass


class AuthenticationError(APIStatusError):
    pass


def _cfg(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or not str(value).strip():
        raise AuthenticationError(
            f"{name} is not set — the openai_compat backend needs LLM_OPENAI_BASE_URL and "
            f"LLM_OPENAI_MODEL (see config.sh)")
    return str(value).strip()


def _system_text(system) -> str:
    if not system:
        return ""
    if isinstance(system, str):
        return system
    return "\n\n".join(b.get("text", "") for b in system if isinstance(b, dict))


def _content_parts(content) -> tuple[list, int]:
    """Anthropic content blocks -> OpenAI content parts. Returns (parts, n_images)."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}], 0
    parts, n_images = [], 0
    for block in content:
        if isinstance(block, str):
            parts.append({"type": "text", "text": block})
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append({"type": "text", "text": block.get("text", "")})
        elif kind == "image":
            src = block.get("source", {})
            if src.get("type") == "base64":
                url = f"data:{src.get('media_type', 'image/png')};base64,{src['data']}"
            elif src.get("type") == "url":
                url = src["url"]
            elif src.get("type") == "file" and src.get("path"):
                import base64
                import mimetypes
                mime = mimetypes.guess_type(src["path"])[0] or "image/png"
                data = base64.b64encode(open(src["path"], "rb").read()).decode()
                url = f"data:{mime};base64,{data}"
            else:
                raise BadRequestError(f"unsupported image source: {src.get('type')}")
            parts.append({"type": "image_url", "image_url": {"url": url}})
            n_images += 1
        elif kind == "document":
            src = block.get("source", {})
            if src.get("type") == "text":
                parts.append({"type": "text", "text": src.get("data", "")})
    return parts, n_images


def _classify(status: int | None, text: str) -> type:
    if status in (401, 403):
        return AuthenticationError
    if status == 429:
        return RateLimitError
    if status == 400:
        return BadRequestError
    if status and status >= 500:
        return InternalServerError
    low = text.lower()
    if "timed out" in low or "timeout" in low:
        return APITimeoutError
    return APIConnectionError


def _log(entry: dict) -> None:
    path = os.environ.get("SUB_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _create(model: str, messages, system=None, max_tokens=None, temperature=None,
            **_ignored) -> SimpleNamespace:
    from openai import OpenAI  # the real OpenAI SDK (pinned in requirements.txt)
    import openai as _openai

    base_url = _cfg("LLM_OPENAI_BASE_URL")
    served = _cfg("LLM_OPENAI_MODEL")
    client = OpenAI(api_key=os.environ.get("LLM_OPENAI_API_KEY", "EMPTY") or "EMPTY",
                    base_url=base_url,
                    timeout=float(os.environ.get("LLM_OPENAI_TIMEOUT", "900")),
                    max_retries=0)
    chat = []
    sys_text = _system_text(system)
    if sys_text:
        chat.append({"role": "system", "content": sys_text})
    n_images = 0
    for m in messages:
        parts, n = _content_parts(m.get("content", ""))
        n_images += n
        # pure-text turns as plain strings: friendlier to servers without vision parsing
        if all(p["type"] == "text" for p in parts):
            chat.append({"role": m.get("role", "user"),
                         "content": "\n\n".join(p["text"] for p in parts)})
        else:
            chat.append({"role": m.get("role", "user"), "content": parts})
    kwargs = {"model": served, "messages": chat}
    if max_tokens:
        kwargs["max_tokens"] = int(max_tokens)
    if temperature is not None:
        kwargs["temperature"] = float(temperature)

    retries = int(os.environ.get("LLM_OPENAI_RETRIES", "3"))
    last, err_cls = "", APIConnectionError
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            r = client.chat.completions.create(**kwargs)
            choice = r.choices[0]
            text = choice.message.content or ""
            if isinstance(text, list):  # some servers return parts
                text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
            u = r.usage
            usage = SimpleNamespace(
                input_tokens=getattr(u, "prompt_tokens", 0) or 0,
                output_tokens=getattr(u, "completion_tokens", 0) or 0,
                cache_read_input_tokens=0, cache_creation_input_tokens=0)
            _log({"t": time.strftime("%Y-%m-%d %H:%M:%S"), "backend": "openai_compat",
                  "model": model, "served_model": served, "images": n_images,
                  "in_tok": usage.input_tokens, "out_tok": usage.output_tokens,
                  "secs": round(time.time() - t0, 1), "cwd": os.getcwd()})
            block = SimpleNamespace(type="text", text=text)
            return SimpleNamespace(id=getattr(r, "id", ""), type="message", role="assistant",
                                   model=model, content=[block],
                                   stop_reason="end_turn" if choice.finish_reason != "length"
                                   else "max_tokens", stop_sequence=None, usage=usage)
        except _openai.APIStatusError as e:
            last, err_cls = f"{e.status_code}: {str(e)[:400]}", _classify(e.status_code, str(e))
            if err_cls in (AuthenticationError, BadRequestError):
                break
        except Exception as e:  # connection / timeout
            last, err_cls = f"{type(e).__name__}: {str(e)[:400]}", _classify(None, str(e))
        if attempt < retries:
            delay = 15 * (2 ** attempt)
            print(f"[openai-compat] {err_cls.__name__}: {last[:160]} — retry in {delay}s "
                  f"({attempt + 1}/{retries})", flush=True)
            time.sleep(delay)
    raise err_cls(f"openai-compatible backend failed: {last}")


class _Stream:
    def __init__(self, kwargs):
        self._kwargs = kwargs
        self._msg = None

    def __enter__(self):
        self._msg = _create(**self._kwargs)
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._msg

    def get_final_text(self):
        return self._msg.content[0].text

    @property
    def text_stream(self):
        yield self._msg.content[0].text

    def __iter__(self):
        return iter(())


class _Messages:
    def create(self, **kwargs):
        if kwargs.get("stream"):
            raise BadRequestError("stream=True is not supported by the openai_compat shim")
        return _create(**kwargs)

    def stream(self, **kwargs):
        return _Stream(kwargs)


class Anthropic:
    """Accepts and ignores api_key / base_url: the endpoint comes from LLM_OPENAI_*."""

    def __init__(self, api_key=None, base_url=None, **_ignored):
        self.api_key = api_key
        self.base_url = base_url
        self.messages = _Messages()

    def with_options(self, **_ignored):
        return self
