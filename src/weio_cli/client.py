"""Thin client for the Weio OpenAI-compatible gateway."""
from __future__ import annotations

import json
from urllib.parse import urlencode
from typing import Iterator, Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    raise SystemExit("weio-cli requires httpx. Install with: pip install httpx")

from . import __version__


class WeioError(RuntimeError):
    pass


class WeioAuthError(WeioError):
    """Explicit authentication/authorization refusal by an account endpoint."""
    pass


class WeioClient:
    def __init__(self, api_key: str, base: str, *, model: str = "auto", timeout: float = 180.0):
        if not api_key:
            raise WeioError("No API key. Run `weio login` or pass --key / set WEIO_API_KEY.")
        self.api_key = api_key
        self.base = base.rstrip("/")
        self.model = model
        self.timeout = timeout
        # CLI-V2: per-session token tally so the agent can print an honest cost line.
        self.last_usage: dict = {}
        self.session_tokens: int = 0
        # 1.0: native tool-calling capability probe result, cached after the first
        # check (None = not probed yet). Defaults to False on any error so an older
        # gateway (no `tool_calling` field, or unreachable) always gets the safe,
        # already-published fence-based agent path.
        self._tool_calling_supported: Optional[bool] = None

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"weio-cli/{__version__}",
        }

    def _record_usage(self, data: dict) -> None:
        """Validate optional gateway usage without leaking malformed response values."""
        usage = data.get("usage")
        if usage is None:
            usage = {}
        if not isinstance(usage, dict):
            raise WeioError("Unexpected usage shape.")
        total = usage.get("total_tokens", 0)
        maximum = (1 << 63) - 1
        if (type(total) is not int or not 0 <= total <= maximum
                or type(self.session_tokens) is not int
                or not 0 <= self.session_tokens <= maximum
                or total > maximum - self.session_tokens):
            raise WeioError("Unexpected usage shape.")
        # Commit both fields only after every validation succeeds.
        self.last_usage = usage
        self.session_tokens += total

    def _public_url(self, path: str) -> str:
        """Build a tenant-portal public API URL from the configured API base."""
        base = self.base.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        base = base.rstrip("/")
        if base in ("https://api.weio.ai", "https://weio.ai", "https://www.weio.ai"):
            base = "https://app.weio.ai"
        return f"{base}/api/public/{path.lstrip('/')}"

    def chat(self, messages: list[dict], *, max_tokens: int = 2048,
             temperature: float = 0.7, model: Optional[str] = None) -> str:
        """Non-streaming completion. Returns the assistant text."""
        payload = {
            "model": model or self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        try:
            r = httpx.post(f"{self.base}/chat/completions", headers=self._headers(),
                           json=payload, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise WeioError("Network error talking to the gateway.") from e
        if r.status_code == 401:
            raise WeioError("Authentication failed (401). Check your API key.")
        if r.status_code == 402 or r.status_code == 429:
            raise WeioError(
                f"Daily token limit reached ({r.status_code}). Tip: a lighter model uses "
                f"far fewer tokens — try `/model low` (or `weio config model low`), or upgrade "
                f"your plan at https://weio.ai/pricing.")
        if r.status_code >= 400:
            raise WeioError(f"Gateway error {r.status_code}.")
        try:
            data = r.json()
            if not isinstance(data, dict) or "error" in data:
                raise WeioError("Gateway returned an error response.")
            self._record_usage(data)
            message = data["choices"][0]["message"]
            content = message["content"]
            if message.get("tool_calls"):
                raise WeioError("Native tool calls cannot be run in fence mode.")
            if not isinstance(content, str):
                raise WeioError("Gateway returned no assistant text.")
            return content
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
            raise WeioError("Unexpected response shape.") from e

    def probe_tool_calling(self) -> bool:
        """One-time capability probe of the gateway's discovery endpoint (GET the API
        base, which is also the `/v1` info route). Cached on this client instance --
        callers can call this every step for free after the first. Any failure (older
        gateway with no `tool_calling` field, network error, non-200) resolves to
        False so the agent always falls back to the fence-based path."""
        if self._tool_calling_supported is not None:
            return self._tool_calling_supported
        try:
            r = httpx.get(self.base, headers=self._headers(), timeout=10.0)
            doc = r.json()
            self._tool_calling_supported = r.status_code == 200 and isinstance(doc, dict) and doc.get("tool_calling") is True
        except (httpx.HTTPError, json.JSONDecodeError, TypeError, AttributeError):
            self._tool_calling_supported = False
        return self._tool_calling_supported

    def chat_with_tools(self, messages: list[dict], *, tools: Optional[list] = None,
                        tool_choice=None, max_tokens: int = 2048, temperature: float = 0.7,
                        model: Optional[str] = None) -> dict:
        """Native tool-calling completion (non-streaming). Returns
        {"content": str|None, "tool_calls": list|None, "finish_reason": str} --
        the OpenAI-shape message fields the agent needs to keep driving its loop."""
        payload = {
            "model": model or self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        try:
            r = httpx.post(f"{self.base}/chat/completions", headers=self._headers(),
                           json=payload, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise WeioError("Network error talking to the gateway.") from e
        if r.status_code == 401:
            raise WeioError("Authentication failed (401). Check your API key.")
        if r.status_code == 402 or r.status_code == 429:
            raise WeioError(
                f"Daily token limit reached ({r.status_code}). Tip: a lighter model uses "
                f"far fewer tokens — try `/model low` (or `weio config model low`), or upgrade "
                f"your plan at https://weio.ai/pricing.")
        if r.status_code >= 400:
            raise WeioError(f"Gateway error {r.status_code}.")
        try:
            data = r.json()
            if not isinstance(data, dict) or "error" in data:
                raise WeioError("Gateway returned an error response.")
            self._record_usage(data)
            message = data["choices"][0]["message"]
            finish_reason = data["choices"][0].get("finish_reason") or "stop"
            if not isinstance(message, dict):
                raise WeioError("Unexpected response shape.")
            content, calls = message.get("content"), message.get("tool_calls")
            if content is not None and not isinstance(content, str):
                raise WeioError("Unexpected response shape.")
            if calls is not None:
                if not isinstance(calls, list) or not calls:
                    raise WeioError("Unexpected tool-call shape.")
                seen = set()
                for call in calls:
                    if not isinstance(call, dict) or call.get("type") != "function":
                        raise WeioError("Unexpected tool-call shape.")
                    ident, fn = call.get("id"), call.get("function")
                    if not isinstance(ident, str) or not ident or ident in seen or not isinstance(fn, dict) or not isinstance(fn.get("name"), str) or not fn["name"] or not isinstance(fn.get("arguments"), str):
                        raise WeioError("Unexpected tool-call shape.")
                    seen.add(ident)
            if content is None and not calls:
                raise WeioError("Gateway returned an empty assistant response.")
        except (KeyError, IndexError, TypeError, AttributeError, json.JSONDecodeError) as e:
            raise WeioError("Unexpected response shape.") from e
        return {
            "content": message.get("content"),
            "tool_calls": message.get("tool_calls"),
            "finish_reason": finish_reason,
        }

    @staticmethod
    def _sse_events(lines):
        data = []
        for line in lines:
            if line == "":
                if data:
                    yield "\n".join(data)
                    data = []
            elif line.startswith("data:"):
                value = line[5:]
                data.append(value[1:] if value.startswith(" ") else value)
        if data:
            yield "\n".join(data)

    def chat_stream(self, messages: list[dict], *, max_tokens: int = 2048,
                    temperature: float = 0.7, model: Optional[str] = None) -> Iterator[str]:
        """Yield text deltas; malformed or empty streams fail without retry."""
        payload = {
            "model": model or self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        try:
            with httpx.stream("POST", f"{self.base}/chat/completions",
                              headers=self._headers(), json=payload, timeout=self.timeout) as r:
                if r.status_code >= 400:
                    raise WeioError(f"Gateway error {r.status_code}.")

                ctype = r.headers.get("content-type", "").lower()
                # Not all gateways honor stream=true. If the response isn't SSE,
                # read the whole JSON body and yield its content in one shot.
                if "text/event-stream" not in ctype:
                    body = r.read().decode("utf-8", "replace")
                    try:
                        data = json.loads(body)
                        if not isinstance(data, dict) or "error" in data:
                            raise WeioError("Gateway returned an error response.")
                        content = data["choices"][0]["message"]["content"]
                        if not isinstance(content, str) or not content:
                            raise WeioError("Gateway returned no assistant text.")
                        yield content
                    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as e:
                        raise WeioError("Unexpected response shape.") from e
                    return

                saw_delta = False
                for event in self._sse_events(r.iter_lines()):
                    if event == "[DONE]":
                        break
                    try:
                        doc = json.loads(event)
                        if not isinstance(doc, dict) or "error" in doc or not isinstance(doc.get("choices"), list):
                            raise ValueError("invalid event")
                        if not doc["choices"]:
                            continue
                        delta = doc["choices"][0].get("delta", {}).get("content")
                        if delta is not None and not isinstance(delta, str):
                            raise ValueError("invalid delta")
                        if delta:
                            saw_delta = True
                            yield delta
                    except (KeyError, IndexError, TypeError, AttributeError, ValueError, json.JSONDecodeError) as e:
                        raise WeioError("Malformed event-stream completion.") from e
                if not saw_delta:
                    # The first call may already have incurred cost; never retry it silently.
                    raise WeioError("Gateway stream returned no assistant text.")
        except httpx.HTTPError as e:
            raise WeioError("Network error talking to the gateway.") from e

    def models(self) -> list[dict]:
        """Fetch public model descriptions from the server-owned registry."""
        try:
            r = httpx.get(f"{self.base}/models", headers=self._headers(), timeout=15.0)
        except httpx.HTTPError as e:
            raise WeioError("Network error talking to the gateway.") from e
        if r.status_code >= 400:
            raise WeioError(f"Gateway error {r.status_code}.")
        try:
            doc = r.json()
            if not isinstance(doc, dict) or "error" in doc or doc.get("object") != "list" or not isinstance(doc.get("data"), list):
                raise ValueError("invalid registry")
            return [{"id": item["id"], "description": item.get("description", ""),
                     "availability": item.get("availability", "")}
                    for item in doc["data"] if isinstance(item, dict) and isinstance(item.get("id"), str)
                    and isinstance(item.get("description", ""), str) and isinstance(item.get("availability", ""), str)]
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
            raise WeioError("Unexpected model registry response.") from e

    def usage(self) -> dict:
        """Fetch account usage + limits."""
        try:
            r = httpx.get(f"{self.base}/usage", headers=self._headers(), timeout=15.0)
        except httpx.HTTPError as e:
            raise WeioError("Network error talking to the gateway.") from e
        if r.status_code in (401, 403):
            raise WeioAuthError(f"Authentication refused ({r.status_code}). Check your API key.")
        if r.status_code >= 400:
            raise WeioError(f"Gateway error {r.status_code}.")
        try:
            payload = r.json()
            if not isinstance(payload, dict) or 'error' in payload or payload.get('success') is False:
                raise WeioError("Unexpected account response shape.")
            return payload
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            raise WeioError("Unexpected response shape.") from e

    def whoami(self) -> dict:
        """Cheap authenticated identity ping for `weio whoami` / re-auth diffing.

        Reuses GET /usage (same account/tier payload `weio usage` already relies
        on, plus `org_name` when the account belongs to an org) rather than
        standing up a separate endpoint for the same data.
        """
        return self.usage()

    def ledger_entries(self, *, state: str | None = None, kind: str | None = None,
                       person: str | None = None, department: str | None = None,
                       limit: int = 15) -> dict:
        """Fetch unified org work ledger entries."""
        params = {
            "state": state or "",
            "kind": kind or "",
            "person": person or "",
            "department": department or "",
            "limit": str(limit),
        }
        url = self._public_url("ledger") + "?" + urlencode(params)
        try:
            r = httpx.get(url, headers=self._headers(), timeout=20.0)
        except httpx.HTTPError as e:
            raise WeioError("Network error talking to the gateway.") from e
        if r.status_code == 401:
            raise WeioError("Authentication failed (401). Check your API key.")
        if r.status_code >= 400:
            raise WeioError(f"Gateway error {r.status_code}.")
        try:
            payload = r.json()
        except json.JSONDecodeError as e:
            raise WeioError("Unexpected response shape.") from e
        if not payload.get("success", True):
            raise WeioError("Ledger request failed.")
        return payload.get("data") or {}

    def ledger_rollup(self) -> dict:
        """Fetch unified org work ledger rollup totals."""
        try:
            r = httpx.get(self._public_url("ledger/rollup"), headers=self._headers(), timeout=20.0)
        except httpx.HTTPError as e:
            raise WeioError("Network error talking to the gateway.") from e
        if r.status_code == 401:
            raise WeioError("Authentication failed (401). Check your API key.")
        if r.status_code >= 400:
            raise WeioError(f"Gateway error {r.status_code}.")
        try:
            payload = r.json()
        except json.JSONDecodeError as e:
            raise WeioError("Unexpected response shape.") from e
        if not payload.get("success", True):
            raise WeioError("Ledger request failed.")
        return payload.get("data") or {}

    def ping(self) -> bool:
        """Check the gateway is reachable and the key is valid (GET /models)."""
        try:
            r = httpx.get(f"{self.base}/models", headers=self._headers(), timeout=15.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def list_agents(self) -> list[dict]:
        """List the account's saved/deployed agents."""
        try:
            r = httpx.get(f"{self.base}/agents", headers=self._headers(), timeout=20.0)
        except httpx.HTTPError as e:
            raise WeioError("Network error talking to the gateway.") from e
        if r.status_code == 401:
            raise WeioError("Authentication failed (401). Check your API key.")
        if r.status_code >= 400:
            raise WeioError(f"Gateway error {r.status_code}.")
        try:
            return r.json().get("data", [])
        except json.JSONDecodeError as e:
            raise WeioError("Unexpected response shape.") from e

    def get_agent(self, name_or_id: str) -> dict | None:
        """Resolve an agent by name (case-insensitive) or id."""
        for a in self.list_agents():
            if a.get("id") == name_or_id or (a.get("name", "").lower() == str(name_or_id).lower()):
                return a
        return None

    def run_agent(self, name_or_id: str, input_text: str = "") -> dict:
        """Run a hosted agent server-side; returns the completed run record."""
        try:
            r = httpx.post(f"{self.base}/agents/{name_or_id}/run", headers=self._headers(),
                           json={"input": input_text}, timeout=self.timeout)
        except httpx.HTTPError as e:
            raise WeioError("Network error talking to the gateway.") from e
        if r.status_code == 401:
            raise WeioError("Authentication failed (401). Check your API key.")
        if r.status_code == 404:
            raise WeioError(f"No agent named or with id '{name_or_id}'.")
        if r.status_code >= 400:
            raise WeioError(f"Gateway error {r.status_code}.")
        try:
            return r.json()
        except json.JSONDecodeError as e:
            raise WeioError("Unexpected response shape.") from e
