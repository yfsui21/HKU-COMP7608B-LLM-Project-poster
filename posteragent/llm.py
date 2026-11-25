"""LLM/VLM client with JSON coercion and a call trace.

Two backends behind one `call()`, dispatched on the model name, because the
proxy splits them: Claude models are only reachable over the Anthropic
Messages API and return 400 "does not support the chat interface" on the
OpenAI-compatible route; everything else is the reverse.

The trace matters for the project report: the proposal commits to logging
"the actions, failures, and costs".
"""
from __future__ import annotations

import base64
import io
import os
import json
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

import anthropic
from openai import OpenAI
from PIL import Image

from . import config

_NO_TEMPERATURE: set[str] = set()
_oai: OpenAI | None = None
_ant: "anthropic.Anthropic | None" = None
_lock = threading.Lock()


# Offline backend. When set, every call() goes here instead of a provider:
#     backend(agent, prompt, images, context) -> parsed JSON value
# `context` carries the structured inputs the agent formatted into its prompt,
# so a scripted backend does not have to parse prose. Real providers ignore it.
Backend = Callable[[str, str, Sequence[Image.Image], dict], Any]
_backend: Backend | None = None


def set_backend(fn: Backend | None) -> None:
    global _backend
    _backend = fn


def backend_name() -> str:
    if _backend is not None:
        return getattr(_backend, "name", "offline-stub")
    return config.TEXT_MODEL if config.TEXT_MODEL == config.VISION_MODEL else (
        f"{config.TEXT_MODEL}+{config.VISION_MODEL}")


def is_anthropic(model: str) -> bool:
    return model.lower().startswith("claude")


def client() -> OpenAI:
    global _oai
    with _lock:
        if _oai is None:
            # Explicit timeout: the default is 600s, and with retries a hung
            # vision call would block the UI for half an hour.
            _oai = OpenAI(timeout=config.REQUEST_TIMEOUT_S)
        return _oai


def anthropic_client() -> "anthropic.Anthropic":
    global _ant
    with _lock:
        if _ant is None:
            _ant = anthropic.Anthropic(timeout=config.REQUEST_TIMEOUT_S, max_retries=0)
        return _ant


@dataclass
class CallRecord:
    agent: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    seconds: float
    ok: bool
    note: str = ""


@dataclass
class Trace:
    """Collects every model call plus human-readable pipeline events."""

    calls: list[CallRecord] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    def log(self, msg: str) -> None:
        self.events.append(msg)

    @property
    def total_tokens(self) -> tuple[int, int]:
        return (
            sum(c.prompt_tokens for c in self.calls),
            sum(c.completion_tokens for c in self.calls),
        )

    def usage(self, start: int = 0) -> dict:
        """Calls, tokens and estimated cost from call index `start` on.

        Cost is only reported when per-million-token rates are configured
        (POSTER_INPUT_USD_PER_M / POSTER_OUTPUT_USD_PER_M); otherwise null,
        never a silent zero.
        """
        calls = self.calls[start:]
        pin = sum(c.prompt_tokens for c in calls)
        pout = sum(c.completion_tokens for c in calls)
        rates = [os.environ.get(f"POSTER_{k}_USD_PER_M") for k in ("INPUT", "OUTPUT")]
        offline = bool(calls) and all(c.model == "offline-stub" for c in calls)
        cost = None
        if offline or not calls:
            cost = 0.0
        elif all(rates):
            cost = round((pin * float(rates[0]) + pout * float(rates[1])) / 1e6, 4)
        by_agent: dict[str, int] = {}
        for c in calls:
            key = c.agent.split(":")[0]
            by_agent[key] = by_agent.get(key, 0) + 1
        return {
            "calls": len(calls),
            "failed_calls": sum(1 for c in calls if not c.ok),
            "prompt_tokens": pin,
            "completion_tokens": pout,
            "model_seconds": round(sum(c.seconds for c in calls), 2),
            "estimated_usd": cost,
            "calls_by_agent": by_agent,
        }

    def summary(self) -> str:
        pin, pout = self.total_tokens
        secs = sum(c.seconds for c in self.calls)
        fails = sum(1 for c in self.calls if not c.ok)
        return (
            f"{len(self.calls)} model calls ({fails} failed) | "
            f"{pin:,} in + {pout:,} out tokens | {secs:.1f}s in-model"
        )


def img_to_data_url(img: Image.Image, max_px: int = config.VLM_MAX_PX) -> str:
    """Downscale so the longest edge is <= max_px, then base64-encode as JPEG.

    JPEG, not PNG: a 1280px PNG of a text-dense poster runs to several MB and
    the upload alone can exceed the request timeout. The same image as JPEG is
    ~200KB and the call returns in seconds.
    """
    im = img.convert("RGB")
    longest = max(im.size)
    if longest > max_px:
        k = max_px / longest
        im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=82, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Any:
    """Pull the first JSON value out of a model response.

    strict=False is load-bearing: models routinely emit literal newlines and
    tabs inside string values when asked to copy passages out of a paper,
    which strict JSON rejects.
    """
    if not text:
        raise ValueError("empty response")
    candidates: list[str] = []
    m = _FENCE.search(text)
    if m:
        candidates.append(m.group(1))
    candidates.append(text)

    for cand in candidates:
        cand = cand.strip()
        try:
            return json.loads(cand, strict=False)
        except json.JSONDecodeError:
            pass
        # Fall back to brace/bracket matching so trailing prose is tolerated.
        for opener, closer in (("{", "}"), ("[", "]")):
            start = cand.find(opener)
            if start < 0:
                continue
            depth, in_str, esc = 0, False, False
            for i in range(start, len(cand)):
                ch = cand[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(cand[start : i + 1], strict=False)
                        except json.JSONDecodeError:
                            break
    raise ValueError(f"no JSON found in response: {text[:200]!r}")


def _jpeg_b64(img: Image.Image, max_px: int = config.VLM_MAX_PX) -> str:
    im = img.convert("RGB")
    longest = max(im.size)
    if longest > max_px:
        k = max_px / longest
        im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=82, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def _post_anthropic(
    model: str,
    system: str | None,
    prompt: str,
    images: Sequence[Image.Image],
    max_tokens: int,
    temperature: float,
    correction: tuple[str, str] | None,
) -> tuple[str, int, int]:
    """Anthropic Messages API. Returns (text, in_tokens, out_tokens)."""
    blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for img in images:
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": _jpeg_b64(img),
                },
            }
        )
    messages: list[dict[str, Any]] = [{"role": "user", "content": blocks}]
    if correction:
        messages.append({"role": "assistant", "content": correction[0]})
        messages.append({"role": "user", "content": correction[1]})

    kwargs: dict[str, Any] = {}
    if system:
        kwargs["system"] = system
    # Newer Claude models reject `temperature` outright ("deprecated for this
    # model"). Learned once per process rather than hardcoded, so a model that
    # still honours it keeps the lower-variance setting.
    if model not in _NO_TEMPERATURE:
        kwargs["temperature"] = temperature
    # Streamed, not a plain create(): this proxy drops a non-streaming Claude
    # request at ~60s of generation (504 "Upstream request timed out"), which
    # the parser agent's ~7k-token tree exceeds. Streaming keeps bytes moving
    # so the same call completes.
    try:
        with anthropic_client().messages.stream(
            model=model,
            max_tokens=max_tokens,
            # Thinking roughly doubles latency here for no gain on these tasks,
            # and its tokens count against max_tokens.
            thinking={"type": "disabled"},
            messages=messages,
            **kwargs,
        ) as stream:
            resp = stream.get_final_message()
    except Exception as e:  # noqa: BLE001
        if "temperature" in str(e) and model not in _NO_TEMPERATURE:
            _NO_TEMPERATURE.add(model)
            kwargs.pop("temperature", None)
            with anthropic_client().messages.stream(
                model=model,
                max_tokens=max_tokens,
                thinking={"type": "disabled"},
                messages=messages,
                **kwargs,
            ) as stream:
                resp = stream.get_final_message()
        else:
            raise
    # Never index content[0]: a thinking block can precede the text.
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return text, resp.usage.input_tokens, resp.usage.output_tokens


def call(
    agent: str,
    prompt: str,
    *,
    system: str | None = None,
    images: Sequence[Image.Image] = (),
    model: str | None = None,
    max_tokens: int = 4000,
    temperature: float = 0.2,
    want_json: bool = True,
    retries: int = 3,
    trace: Trace | None = None,
    context: dict | None = None,
) -> Any:
    """One model call. Returns parsed JSON when want_json, else raw text."""
    if _backend is not None:
        t0 = time.time()
        try:
            value = _backend(agent, prompt, images, context or {})
        except Exception as e:  # noqa: BLE001 - mirror provider failures
            if trace:
                trace.calls.append(CallRecord(agent, "offline-stub", 0, 0, time.time() - t0,
                                              ok=False, note=f"{type(e).__name__}: {e}"))
            raise RuntimeError(f"{agent}: offline backend failed") from e
        if trace:
            trace.calls.append(CallRecord(agent, "offline-stub", 0, 0, time.time() - t0, ok=True))
        return value
    model = model or (config.VISION_MODEL if images else config.TEXT_MODEL)
    anth = is_anthropic(model)

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for img in images:
        content.append({"type": "image_url", "image_url": {"url": img_to_data_url(img)}})

    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})
    base_messages = list(messages)

    # OpenAI-route JSON mode is the difference between "mostly valid JSON" and
    # valid JSON -- without it models emit things like an array of *unquoted*
    # sentences. The Anthropic route has no equivalent (and this proxy rejects
    # assistant prefill with a 500), so it leans on extract_json plus the
    # correction turn below.
    json_mode = want_json and not anth
    correction: tuple[str, str] | None = None

    _CORRECTION = (
        "That was not valid JSON. Every string must be double-quoted. "
        "Reply with the JSON value only, no prose and no code fences."
    )

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        t0 = time.time()
        try:
            if anth:
                text, tin, tout = _post_anthropic(
                    model, system, prompt, images, max_tokens, temperature, correction
                )
                rec = CallRecord(agent, model, tin, tout, time.time() - t0, ok=True)
            else:
                kwargs: dict[str, Any] = {}
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                resp = client().chat.completions.create(
                    model=model,
                    messages=messages,
                    max_completion_tokens=max_tokens,
                    temperature=temperature,
                    **kwargs,
                )
                text = resp.choices[0].message.content or ""
                usage = resp.usage
                rec = CallRecord(
                    agent=agent,
                    model=model,
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    seconds=time.time() - t0,
                    ok=True,
                )
            if not want_json:
                if trace:
                    trace.calls.append(rec)
                return text
            try:
                value = extract_json(text)
            except ValueError as e:
                rec.ok = False
                rec.note = f"unparseable JSON: {e}"
                if trace:
                    trace.calls.append(rec)
                last_err = e
                # Re-ask from the ORIGINAL turn with the failure shown, so the
                # correction does not accumulate across attempts.
                correction = (text[:2000], _CORRECTION)
                messages = base_messages + [
                    {"role": "assistant", "content": text[:2000]},
                    {"role": "user", "content": _CORRECTION},
                ]
                continue
            if trace:
                trace.calls.append(rec)
            return value
        except Exception as e:  # noqa: BLE001 - proxy raises a variety of types
            dt = time.time() - t0
            # Some models reject response_format; fall back rather than fail.
            if json_mode and "response_format" in str(e):
                json_mode = False
                continue
            if trace:
                trace.calls.append(
                    CallRecord(agent, model, 0, 0, dt, ok=False, note=f"{type(e).__name__}: {e}")
                )
            last_err = e
            if attempt < retries:
                # This proxy returns 500 "Upstream service unavailable" and 502
                # under load, and recovers on the order of seconds rather than
                # milliseconds. Back off exponentially with jitter; 1.5s is not
                # enough to outlast a capacity blip.
                time.sleep(4.0 * (2**attempt) * (0.75 + 0.5 * random.random()))

    raise RuntimeError(f"{agent}: model call failed after {retries + 1} attempts") from last_err
