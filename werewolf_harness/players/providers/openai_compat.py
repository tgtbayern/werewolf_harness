"""OpenAI-compatible gateway client.

The whole experiment reaches every model through one relay endpoint, so there
is one client and no per-vendor branching. The `openai` SDK is used when it is
installed; otherwise the same requests go out over `urllib`, which keeps the
harness runnable with zero third-party dependencies.

Two gateway-specific hazards are handled here rather than left to blow up
mid-experiment:

* **token group / model mismatch.** Relays bind a token to a channel group; a
  model outside that group fails with "no available channel". The raw message
  is useless to a user, so `explain()` turns each known failure into an
  actionable line, surfaced by the config page.
* **no native tool calling.** Support is per-model and not guaranteed to be
  passed through. `tool_mode="json_prompt"` runs the identical loop with the
  tool schema described in the prompt and the reply parsed as JSON, so a model
  that cannot do function calling still produces comparable runs. Which mode a
  model uses is recorded in every game log.
"""

from __future__ import annotations

import json
import time
import http.client
import urllib.error
import urllib.request

from .base import (
    JSON_TOOL_INSTRUCTIONS,
    LLMClient,
    LLMResponse,
    ProviderError,
    ToolCall,
    describe_tools_for_prompt,
    parse_json_action,
)


def _as_string_enums(tools: list[dict]) -> list[dict]:
    """Rewrite every non-string `enum` as a string one, for Google's schema.

    The whitelist is unchanged, only its encoding: the model answers "5" where
    it would have answered 5, and a ToolSpec's own check accepts a digit string
    for an int and normalises it back. Legality still lives in the ToolSpec --
    this only decides how the same set is spelled on the wire.
    """
    def fix(node):
        if isinstance(node, list):
            return [fix(x) for x in node]
        if not isinstance(node, dict):
            return node
        out = {k: fix(v) for k, v in node.items()}
        if isinstance(out.get("enum"), list) and out.get("type") != "string":
            out["type"] = "string"
            out["enum"] = [str(x) for x in out["enum"]]
        return out
    return [fix(t) for t in tools]


class OpenAICompatClient(LLMClient):
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        tool_mode: str = "native",
        display_name: str | None = None,
        group: str | None = None,
        extra_headers: dict | None = None,
        tool_choice: str = "required",
        schema_dialect: str | None = None,
        extra_body: dict | None = None,
        temperature: float | None = None,
    ):
        if not api_key:
            raise ProviderError(
                "no API key configured",
                hint="Set one on the config page, or export LLM_API_KEY.",
            )
        self.model = model
        self.name = display_name or model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.tool_mode = tool_mode
        self.group = group
        self.extra_headers = extra_headers or {}
        self.tool_choice = tool_choice
        # Google's function-declaration schema only allows `enum` on a STRING
        # field, so an integer whitelist ("vote for one of 2,3,4") is a 400 --
        # every turn whose legal set is seat numbers. Derived from the model
        # name rather than passed in, because it is a property of the upstream
        # API, not a choice any caller should have to remember.
        self.schema_dialect = schema_dialect or ("google" if model.startswith("gemini") else "openai")
        # Vendor-specific body fields. Several Chinese providers need one to
        # behave like the others at all -- GLM drops tool_calls silently without
        # `tool_stream`, glm-5.3 rejects a disabled `thinking` outright. Kept as
        # data on the profile so the client stays one code path.
        self.extra_body = dict(extra_body or {})
        # Some vendors accept exactly one temperature and reject every other value
        # with a 400 -- and which value depends on whether thinking is on. When a
        # profile pins one, it wins over whatever the caller asks for.
        self.temperature = temperature

    # ---- transport -------------------------------------------------------

    def _post(self, path: str, payload: dict, timeout: float) -> dict:
        url = f"{self.base_url}{path}"
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            **self.extra_headers,
        }
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:600]
            raise ProviderError(
                f"HTTP {exc.code} from {url}: {detail}",
                hint=explain(detail, exc.code, self.group, self.model),
                status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise ProviderError(
                f"cannot reach {url}: {exc.reason}",
                hint="Check the base_url (it usually must end in /v1) and the network.",
            ) from exc
        except TimeoutError as exc:
            raise ProviderError(
                f"timed out after {timeout}s",
                hint="The upstream model is slow or the relay is queueing; "
                     "raise the timeout or pick a faster model.",
            ) from exc
        except (ConnectionError, OSError, http.client.HTTPException) as exc:
            # RemoteDisconnected, ConnectionReset, BadStatusLine: the relay dropped us mid-request.
            raise ProviderError(f"connection dropped by {url}: {type(exc).__name__}: {exc}",
                                hint="The relay closed the connection; retried automatically.") from exc

    # ---- chat ------------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 800,
        timeout: float = 30.0,
    ) -> LLMResponse:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if self.temperature is not None else temperature,
            "max_tokens": max_tokens,
            **self.extra_body,
        }
        if tools and self.tool_mode == "native":
            payload["tools"] = _as_string_enums(tools) if self.schema_dialect == "google" else tools
            # "required", not "auto": the loop's contract is one tool call per step anyway, and at least
            # one relay answers "auto" with finish_reason=tool_calls and an EMPTY tool_calls list on the
            # step after a tool result -- the call is lost and the turn reads as malformed.
            payload["tool_choice"] = self.tool_choice
        elif tools:
            payload["messages"] = _with_json_protocol(messages, tools)

        started = time.time()
        data = self._post("/chat/completions", payload, timeout)
        latency_ms = int((time.time() - started) * 1000)

        try:
            choice = data["choices"][0]
        except (KeyError, IndexError) as exc:
            raise ProviderError(
                f"malformed response: {json.dumps(data)[:300]}",
                hint="The relay returned no choices; the model name may be wrong.",
            ) from exc

        message = choice.get("message", {}) or {}
        usage = data.get("usage", {}) or {}
        response = LLMResponse(
            text=(message.get("content") or "").strip(),
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            latency_ms=latency_ms,
            finish_reason=choice.get("finish_reason", "stop"),
            model=data.get("model", self.model),
            raw=data,
        )

        if self.tool_mode == "native":
            for call in message.get("tool_calls") or []:
                fn = call.get("function", {})
                raw_args = fn.get("arguments", "") or "{}"
                try:
                    args = json.loads(raw_args)
                    malformed = not isinstance(args, dict)
                except json.JSONDecodeError:
                    args, malformed = {}, True
                response.tool_calls.append(
                    ToolCall(
                        name=fn.get("name", ""),
                        arguments=args if isinstance(args, dict) else {},
                        id=call.get("id", "call_0"),
                        raw_arguments=raw_args,
                        malformed=malformed,
                    )
                )
        elif response.text:
            call = parse_json_action(response.text)
            if not call.malformed:
                response.tool_calls.append(call)
            else:
                response.tool_calls.append(call)  # loop reports the parse error

        if self.tool_mode == "native" and tools and not response.tool_calls and not response.text:
            # The relay sometimes answers finish_reason=tool_calls with an empty message and a
            # prompt_tokens count a tenth of the real prompt: an upstream channel dropped the request.
            # That is weather, not a model reply -- surface it as a retryable transport failure.
            raise ProviderError("empty completion from gateway (finish_reason=%s, prompt_tokens=%s)"
                                % (response.finish_reason, response.prompt_tokens), status=502,
                                hint="the relay lost the request upstream; retried automatically")
        return response

    # ---- probe -----------------------------------------------------------

    def list_models(self, timeout: float = 15.0) -> list[str]:
        req = urllib.request.Request(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001 -- listing is best-effort
            raise ProviderError(f"cannot list models: {exc}") from exc
        return [m.get("id", "") for m in data.get("data", [])]


def _with_json_protocol(messages: list[dict], tools: list[dict]) -> list[dict]:
    """Describe the tools in the system message for JSON-fallback mode."""
    block = JSON_TOOL_INSTRUCTIONS.format(tools=describe_tools_for_prompt(tools))
    out = [dict(m) for m in messages]
    for msg in out:
        if msg.get("role") == "system":
            msg["content"] = f"{msg['content']}\n\n{block}"
            return out
    return [{"role": "system", "content": block}] + out


_KNOWN_FAILURES = [
    (
        ("no available channel", "无可用渠道", "无可用分组"),
        "The token's channel group does not match this model. Check the group "
        "on the relay dashboard, or use a token issued for the model's group "
        "(each model config can carry its own provider).",
    ),
    (
        ("invalid token", "无效的令牌", "令牌验证失败", "incorrect api key"),
        "The token was rejected. Re-copy it in full, including the sk- prefix.",
    ),
    (
        ("insufficient", "额度", "quota", "balance"),
        "The token is out of quota on the relay.",
    ),
    (
        ("model not found", "模型不存在", "does not exist"),
        "The relay does not know this model name. Copy it from the model list "
        "rather than typing it -- names are not guessable.",
    ),
    (
        ("rate limit", "429", "too many requests"),
        "Rate limited. Lower the runner's concurrency setting.",
    ),
]


def explain(detail: str, status: int | None, group: str | None, model: str) -> str:
    """Turn a relay error into something a user can act on.

    Passing the upstream error straight through is the lazy option and the
    useless one: the three most common failures here are all configuration
    mistakes with specific fixes.
    """
    low = (detail or "").lower()
    for needles, advice in _KNOWN_FAILURES:
        if any(n in low for n in needles):
            if "channel group" in advice and group:
                return f"{advice} (this model is configured for group {group!r})"
            return advice
    if status == 401:
        return "Unauthorised: the token is missing, truncated or expired."
    if status == 404:
        return f"Endpoint or model {model!r} not found; check base_url ends in /v1."
    if status and status >= 500:
        return "The relay or the upstream provider failed; retry, or try another model."
    return "Unrecognised gateway error; the raw response is in the details field."
