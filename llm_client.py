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

PRESET_URLS = {
    "ollama": "http://localhost:11434/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
}


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self):
        self.provider = os.getenv("CP_LLM_PROVIDER", "ollama").lower()
        self.base_url = os.getenv("CP_LLM_BASE_URL") or PRESET_URLS.get(self.provider, "")
        self.model = os.getenv("CP_LLM_MODEL", "")
        self.api_key = os.getenv("CP_LLM_API_KEY") or ("ollama" if self.provider == "ollama" else "")
        self.timeout = float(os.getenv("CP_LLM_TIMEOUT", "180"))
        self.max_tokens = int(os.getenv("CP_LLM_MAX_TOKENS", "4096"))
        # Some Nemotron variants switch reasoning on/off with a system line; set it here if needed.
        self.system_prefix = os.getenv("CP_LLM_SYSTEM_PREFIX", "")

    def info(self):
        local = self.provider == "ollama" or "localhost" in self.base_url or "127.0.0.1" in self.base_url
        return {"provider": self.provider, "base_url": self.base_url, "model": self.model or None,
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

    def chat(self, messages, temperature=0.2):
        problems = self.config_problems()
        if problems:
            raise LLMError("LLM not configured: " + "; ".join(problems))
        try:
            from openai import OpenAI
        except ImportError as e:
            raise LLMError("The 'openai' package is not installed (pip install -r requirements.txt)") from e
        if self.system_prefix and messages and messages[0]["role"] == "system":
            messages = [{"role": "system", "content": self.system_prefix + "\n" + messages[0]["content"]}] + messages[1:]
        try:
            client = OpenAI(base_url=self.base_url, api_key=self.api_key, timeout=self.timeout)
            resp = client.chat.completions.create(model=self.model, messages=messages,
                                                  temperature=temperature, max_tokens=self.max_tokens)
        except Exception as e:
            raise LLMError(f"{self.provider} request to {self.base_url} failed: {e}") from e
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
        raise ValueError(f"The reply is not valid JSON: {e.msg} at line {e.lineno} column {e.colno}") from e
