"""Provider-agnostic model interface.

One interface, two tool-calling implementations (native function calling and a
JSON-prompt fallback), so the ReAct loop never learns which gateway or model it
is talking to.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class ProviderError(Exception):
    """Anything the gateway or the transport got wrong. Carries a hint that is
    safe to show a user (see `explain` in `openai_compat`)."""

    def __init__(self, message: str, *, hint: str | None = None, status: int | None = None):
        super().__init__(message)
        self.hint = hint
        self.status = status


@dataclass
class ToolCall:
    name: str
    arguments: dict
    id: str = "call_0"
    raw_arguments: str = ""
    malformed: bool = False


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    finish_reason: str = "stop"
    model: str = ""
    raw: dict | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMClient(ABC):
    """Every client returns tool calls the same way, whatever the wire format."""

    name: str = "unknown"
    tool_mode: str = "native"

    @abstractmethod
    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 800,
        timeout: float = 30.0,
    ) -> LLMResponse:
        ...


# ------------------------------------------------------- JSON fallback

JSON_TOOL_INSTRUCTIONS = """\
TOOL PROTOCOL (strict)

This model is driven without native function calling, so every step must be a
single JSON object and nothing else -- no prose before or after:

{{"thought": "<one sentence of reasoning>", "action": "<tool name>", "args": {{...}}}}

"thought" is REQUIRED on every step, never empty. When the tool you are calling
also takes a "thought" argument, fill that one too: say who you suspect, on what
evidence, and what this move is meant to achieve.

Available tools:
{tools}

Example:
{{"thought": "I should check what player 5 said last round.", "action": "query_history", "args": {{"player_id": 5, "round": 2}}}}

Example with a terminal action:
{{"thought": "7 dodged the vote on 3 twice; I want him out.", "action": "vote", "args": {{"target_id": 7, "thought": "7 dodged the vote on 3 twice -- that is the hardest read I have."}}}}
"""

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_action(text: str) -> ToolCall:
    """Recover a tool call from free text.

    Tolerant on the outside (code fences, stray prose) and strict on the
    inside: a malformed call is flagged rather than guessed at, so the loop can
    retry with the parse error attached instead of acting on a hallucination.
    """
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\n?|```$", "", candidate).strip()
    match = _JSON_BLOCK.search(candidate)
    if not match:
        return ToolCall(name="", arguments={}, raw_arguments=text, malformed=True)
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        try:  # a single trailing comma is the most common failure
            data = json.loads(re.sub(r",\s*([}\]])", r"\1", match.group(0)))
        except json.JSONDecodeError:
            return ToolCall(name="", arguments={}, raw_arguments=text, malformed=True)
    if not isinstance(data, dict):
        return ToolCall(name="", arguments={}, raw_arguments=text, malformed=True)
    action = data.get("action") or data.get("tool") or data.get("name")
    args = data.get("args") or data.get("arguments") or {}
    return ToolCall(
        name=action if isinstance(action, str) else "",
        arguments=args if isinstance(args, dict) else {},
        raw_arguments=match.group(0),
        malformed=not isinstance(action, str) or not action,
    )


def describe_tools_for_prompt(tool_schemas: list[dict]) -> str:
    lines = []
    for schema in tool_schemas:
        fn = schema["function"]
        params = fn.get("parameters", {}).get("properties", {})
        required = set(fn.get("parameters", {}).get("required", []))
        args = ", ".join(
            f"{p}: {spec.get('type')}" + ("" if p in required else " (optional)")
            for p, spec in params.items()
        )
        lines.append(f"- {fn['name']}({args}) -- {fn.get('description', '')}")
    return "\n".join(lines)
