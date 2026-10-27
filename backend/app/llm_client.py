"""
LLM client (llm_client.py) — replaces cloud_reviewer.py.

One OpenAI-compatible client for three ways of running Nemotron:
  CP_LLM_PROVIDER=ollama  (default, fully local)  base URL http://localhost:11434/v1
  CP_LLM_PROVIDER=nvidia  (NVIDIA API catalog)    base URL https://integrate.api.nvidia.com/v1
  CP_LLM_PROVIDER=custom  (e.g. Nebius)           set CP_LLM_BASE_URL yourself

Required: CP_LLM_MODEL (the exact model id from your provider's catalog).
For cloud providers: CP_LLM_API_KEY.
Failures raise LLMError. Nothing silently falls back to a hard-coded design.
"""

import json
import os
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from timing import timed

PRESET_URLS = {
    "ollama": "http://localhost:11434/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
}


MIN_CALL_S = 10      # don't start a model call with less time than this left in the budget
BACKOFF_CAP_S = 8    # longest pause between retries
BUSY_WAIT_S = 40     # most total waiting for a busy (429/503) provider during one generation call
HEDGE_AFTER_S = float(os.getenv("CP_LLM_HEDGE_S", "35"))  # no answer by then: send a parallel duplicate request


class LLMError(RuntimeError):
    pass


class LLMBudgetError(LLMError):
    """The call was stopped (or not started) because the generation's time budget ran out."""


def _status(e):
    return getattr(e, "status_code", None) or getattr(getattr(e, "response", None), "status_code", None)


def _is_timeout(e):
    return type(e).__name__ in ("APITimeoutError", "TimeoutException", "ReadTimeout", "ConnectTimeout",
                                "TimeoutError")


def _is_retryable(e):
    status = _status(e)
    if status is not None:
        return status == 429 or status >= 500
    text = str(e)
    return type(e).__name__ == "APIConnectionError" or "ResourceExhausted" in text


class LLMClient:
    def __init__(self):
        self.provider = os.getenv("CP_LLM_PROVIDER", "ollama").lower()
        self.base_url = os.getenv("CP_LLM_BASE_URL") or PRESET_URLS.get(self.provider, "")
        self.model = os.getenv("CP_LLM_MODEL", "")
        self.api_key = os.getenv("CP_LLM_API_KEY") or ("ollama" if self.provider == "ollama" else "")
        # Per-call cap in seconds. A generation also has an overall budget (see planner), which can cut it shorter.
        self.timeout = float(os.getenv("CP_LLM_TIMEOUT", "90"))
        # Busy/overloaded responses (429, 5xx) and dropped connections are retried with a short, capped back-off.
        self.retries = int(os.getenv("CP_LLM_RETRIES", "2"))
        self.max_tokens = int(os.getenv("CP_LLM_MAX_TOKENS", "4096"))
        # Some Nemotron variants switch reasoning on/off with a system line; set it here if needed.
        self.system_prefix = os.getenv("CP_LLM_SYSTEM_PREFIX", "")
        # Thinking is off unless CP_LLM_THINKING says otherwise: Nemotron reasoning models then answer directly
        # (much faster, no token budget spent thinking). CP_LLM_THINKING=on keeps the model's default reasoning.
        thinking = os.getenv("CP_LLM_THINKING", "").strip().lower()
        self.thinking_off = thinking in ("", "off", "false", "0", "no")
        self._client = None

    def info(self):
        local = self.provider == "ollama" or "localhost" in self.base_url or "127.0.0.1" in self.base_url
        return {"provider": self.provider, "base_url": self.base_url, "model": self.model or None,
                "thinking": "off" if self.thinking_off else "default",
                "runs_locally": local, "configured": not self.config_problems()}

    def config_problems(self):
        problems = []
        if not self.base_url:
            problems.append("CP_LLM_BASE_URL is not set")
        if not self.model:
            problems.append("CP_LLM_MODEL is not set (use the exact model id from your provider)")
        if not self.api_key:
            problems.append("CP_LLM_API_KEY is not set")
        return problems

    def chat(self, messages, temperature=0.2, label="call", deadline=None, timeout=None, max_tokens=None):
        """label names the pipeline stage for the [timing] log, e.g. 'netlist round 2'.
        deadline (time.monotonic() value), timeout (seconds) and max_tokens can only shorten the call.
        max_tokens applies when thinking is off: a direct answer never needs more, and a runaway reply then
        fails in seconds instead of minutes. With thinking on, CP_LLM_MAX_TOKENS alone applies."""
        size = sum(len(m.get("content") or "") for m in messages)
        limit = min(self.max_tokens, max_tokens) if max_tokens and self.thinking_off else self.max_tokens
        with timed(f"llm {label} ({size} prompt chars)"):
            return self._chat(messages, temperature, deadline, timeout, limit)

    def _openai(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as e:
                raise LLMError("The 'openai' package is not installed (pip install -r requirements.txt)") from e
            # Retries are done in _chat so they can respect the generation's time budget.
            self._client = OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=self.timeout, max_retries=0)
        return self._client

    @staticmethod
    def _create_hedged(client, limit, **kwargs):
        """One completion with a hedge: when the provider has not answered after HEDGE_AFTER_S (an overloaded
        endpoint sometimes leaves a request hanging for minutes), the same request is sent once more in parallel
        and the first answer wins. A quick error (e.g. 503) is raised as usual so the retry logic sees it."""
        started = time.monotonic()
        pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="llm")
        try:
            pending = {pool.submit(client.chat.completions.create, timeout=limit, **kwargs)}
            done, pending = wait(pending, timeout=min(HEDGE_AFTER_S, limit))
            left = limit - (time.monotonic() - started)
            if not done and left > MIN_CALL_S:
                print(f"[timing] llm no answer after {HEDGE_AFTER_S:.0f}s; sending a parallel request", flush=True)
                pending.add(pool.submit(client.chat.completions.create, timeout=left, **kwargs))
            error = None
            while True:
                for f in done:
                    if f.exception() is None:
                        return f.result()
                    error = error or f.exception()
                if not pending:
                    raise error
                left = limit - (time.monotonic() - started)
                if left <= 0:
                    raise TimeoutError(f"no answer within {limit:.0f}s")
                done, pending = wait(pending, timeout=left, return_when=FIRST_COMPLETED)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)  # a slower duplicate is left to finish on its own

    def _chat(self, messages, temperature, deadline, timeout, max_tokens):
        problems = self.config_problems()
        if problems:
            raise LLMError("LLM not configured: " + "; ".join(problems))
        client = self._openai()
        if self.system_prefix and messages and messages[0]["role"] == "system":
            messages = [{"role": "system", "content": self.system_prefix + "\n" + messages[0]["content"]}] + messages[1:]
        extra = {"chat_template_kwargs": {"enable_thinking": False}} if self.thinking_off else None
        attempt, timed_out_before, waited = 0, False, 0.0
        while True:
            limit = min(self.timeout, timeout or self.timeout)
            if deadline is not None:
                limit = min(limit, deadline - time.monotonic())
                if limit < MIN_CALL_S:
                    raise LLMBudgetError("The time budget for this generation is used up")
            try:
                resp = self._create_hedged(client, limit, model=self.model, messages=messages,
                                           temperature=temperature, max_tokens=max_tokens, extra_body=extra)
                break
            except Exception as e:
                text = str(e)
                if _is_timeout(e):
                    out_of_budget = deadline is not None and deadline - time.monotonic() < MIN_CALL_S
                    if not out_of_budget and not timed_out_before:  # independent of the busy (503) retries
                        timed_out_before, attempt = True, attempt + 1  # a hung request is retried once, never twice
                        print(f"[timing] llm retry {attempt}/{self.retries} after a {limit:.0f}s timeout", flush=True)
                        continue
                    error = LLMBudgetError if out_of_budget else LLMError
                    raise error(f"{self.provider} did not answer within {limit:.0f}s") from e
                pause = min(BACKOFF_CAP_S, 2.0 * 2 ** attempt)  # 2 s, 4 s, 8 s ... never more than 8 s
                # Inside a generation (deadline set) a busy provider gets up to BUSY_WAIT_S of total waiting,
                # still bounded by the budget; elsewhere CP_LLM_RETRIES is the limit.
                patient = deadline is not None and waited + pause <= BUSY_WAIT_S
                if _is_retryable(e) and (attempt < self.retries or patient):
                    attempt, waited = attempt + 1, waited + pause
                    print(f"[timing] llm retry {attempt} after {pause:.0f}s: {text[:120]}", flush=True)
                    time.sleep(pause)
                    continue
                if "ResourceExhausted" in text or "503" in text or "429" in text:
                    raise LLMError(f"{self.provider} is busy or rate-limited right now (tried {attempt + 1} times). "
                                   f"Wait a few minutes and retry. Details: {text}") from e
                raise LLMError(f"{self.provider} request to {self.base_url} failed: {text}") from e
        content = (resp.choices[0].message.content or "").strip()
        if not content:
            raise LLMError("The model returned an empty response")
        return content


def extract_json(text):
    """Pull one JSON object out of a model reply (handles <think> blocks and ``` fences)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("The reply contains no JSON object")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        # Text after the object can contain a stray "}" (notes, a second fence): take the complete object that
        # starts at the first "{" and ignore what follows. A broken object is still reported as invalid JSON.
        try:
            return json.JSONDecoder().raw_decode(text, start)[0]
        except json.JSONDecodeError:
            pass
        # Small slips that cost a whole repair round: a missing comma between lines, a trailing comma.
        fixed = re.sub(r",\s*([}\]])", r"\1", text[start:end + 1])
        fixed = re.sub(r'([}\]"\d]|true|false|null)(\s*\n\s*)(["{\[])', r"\1,\2\3", fixed)
        try:
            return json.loads(fixed, strict=False)  # strict=False: tolerate a raw tab/newline inside a string
        except json.JSONDecodeError:
            pass
        raise ValueError(f"The reply is not valid JSON: {e.msg} at line {e.lineno} column {e.colno}") from e
