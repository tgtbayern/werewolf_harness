"""Offline scripted client. Exercises the ReAct loop with no key and no network.

It reads the tool schemas it is handed (the same ones a real model gets) and
picks a legal call: sometimes an information tool, then a terminal. Terminals
are recognised by the "ENDS YOUR TURN" marker every environment terminal
carries in its description. Runs labelled ``model="mock"`` are never findings.
"""
from __future__ import annotations

import json
import random
import re

from .base import LLMClient, LLMResponse, ToolCall

_SELF = re.compile(r"You are player (\d+), role: (\w+)")
_ROUND = re.compile(r"=== (?:ROUND|NIGHT) (\d+)")
_WOLVES = re.compile(r"Your fellow wolves: \[([\d, ]*)\]")
_TASK = re.compile(r"YOUR TASK \[(\w+)\]")


class MockClient(LLMClient):
    name = "mock"
    tool_mode = "native"

    def __init__(self, seed: int = 0, info_rate: float = 0.5, latency_ms: int = 2):
        self.seed = seed
        self.info_rate = info_rate
        self.latency_ms = latency_ms

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=800, timeout=30.0) -> LLMResponse:
        system = messages[0]["content"] if messages else ""
        prompt = "\n".join(m.get("content") or "" for m in messages)
        me = int(_SELF.search(system).group(1)) if _SELF.search(system) else 0
        rnd = int(_ROUND.search(prompt).group(1)) if _ROUND.search(prompt) else 1
        step = sum(1 for m in messages if m.get("role") == "assistant")
        rng = random.Random(f"{self.seed}:{me}:{rnd}:{step}")
        tools = tools or []
        terminals = [t for t in tools if "ENDS YOUR TURN" in t["function"].get("description", "")]
        infos = [t for t in tools if t not in terminals]
        forced = "out of steps" in (messages[-1].get("content") or "")
        if infos and not forced and step < 2 and rng.random() < self.info_rate:
            pick = rng.choice(infos)
        elif terminals:
            pick = rng.choice(terminals)
        else:
            return LLMResponse(text="I have nothing to add.", latency_ms=self.latency_ms, model=self.name)
        fn = pick["function"]
        args = self._args(fn, rng, me, system, rnd)
        call = ToolCall(name=fn["name"], arguments=args, id=f"call_{step}", raw_arguments=json.dumps(args))
        return LLMResponse(text="", tool_calls=[call], prompt_tokens=len(prompt) // 4, completion_tokens=20,
                           latency_ms=self.latency_ms, model=self.name)

    def _args(self, fn: dict, rng: random.Random, me: int, system: str, rnd: int) -> dict:
        props = fn.get("parameters", {}).get("properties", {})
        required = set(fn.get("parameters", {}).get("required", []))
        out: dict = {}
        for k, p in props.items():
            if k not in required and rng.random() < 0.3:
                continue
            if "enum" in p:
                if not p["enum"]:
                    continue
                out[k] = rng.choice(p["enum"])
            elif p["type"] == "integer":
                out[k] = max(1, rnd - 1) if k == "round" else rng.randint(1, 12)
            elif p["type"] == "number":
                out[k] = round(rng.uniform(p.get("minimum", 0.0), p.get("maximum", 1.0)), 2)
            elif p["type"] == "string":
                out[k] = f"player {me} says something about {fn['name']} in round {rnd}"
            elif p["type"] == "array":
                out[k] = []
            elif p["type"] == "object":
                m = _WOLVES.search(system)
                wolves = [me] + ([int(x) for x in m.group(1).split(",") if x.strip()] if m else [])
                out[k] = {str(w): "shenshui" for w in wolves}
        return out
