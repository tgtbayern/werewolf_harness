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
import threading
import time

from .base import LLMClient, LLMResponse, ProviderError, ToolCall

_SELF = re.compile(r"You are player (\d+), role: (\w+)")
_ROUND = re.compile(r"=== (?:ROUND|NIGHT) (\d+)")
_WOLVES = re.compile(r"Your fellow wolves: \[([\d, ]*)\]")
_TASK = re.compile(r"YOUR TASK \[(\w+)\]")


class MockClient(LLMClient):
    name = "mock"
    tool_mode = "native"

    def __init__(self, seed: int = 0, info_rate: float = 0.5, latency_ms: int = 2, sleep_ms: int = 0):
        self.seed = seed
        self.info_rate = info_rate
        self.latency_ms = latency_ms
        self.sleep_ms = sleep_ms          # really wait this long per call: a stand-in for a model's latency

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=800, timeout=30.0) -> LLMResponse:
        if self.sleep_ms:
            time.sleep(self.sleep_ms / 1000)
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


class ChaosMockClient(MockClient):
    """A mock that behaves like a real model in the ways that make games hard to reproduce.

    Its choices come from an unseeded generator, so two runs of the same seed play
    different games (as a model at temperature > 0 does); it sleeps a random time,
    so seats finish in a different order every run; and it sometimes fails --
    a retryable 503, a malformed reply, an empty reply. Only a recording can
    replay a game played against it, which is what it exists to test.
    """

    name = "mock-chaos"

    def __init__(self, seed: int = 0, error_rate: float = 0.08, malformed_rate: float = 0.08,
                 max_sleep_ms: int = 8, rng: random.Random | None = None):
        super().__init__(seed=seed)
        self.name = "mock-chaos"
        self.error_rate = error_rate
        self.malformed_rate = malformed_rate
        self.max_sleep_ms = max_sleep_ms
        self._rng = rng or random.Random()             # deliberately not derived from the game seed
        self._lock = threading.Lock()

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=800, timeout=30.0) -> LLMResponse:
        with self._lock:
            roll, nap, salt = self._rng.random(), self._rng.random(), self._rng.randrange(1 << 30)
        time.sleep(nap * self.max_sleep_ms / 1000)
        if roll < self.error_rate:
            raise ProviderError("mock-chaos: upstream 503", status=503, hint="模拟的上游故障")
        if roll < self.error_rate + self.malformed_rate:
            return LLMResponse(text="嗯……我想想", latency_ms=int(nap * self.max_sleep_ms), model=self.name)
        # a fresh draw every call: the base mock keys its choices on its seed
        r = MockClient(seed=salt, info_rate=self.info_rate).chat(messages, tools, temperature, max_tokens, timeout)
        r.latency_ms = int(nap * self.max_sleep_ms)
        r.model = self.name
        if r.tool_calls and tools is None:
            r.tool_calls = []
        if not r.tool_calls and tools is None:
            r.text = f"记一下：第 {salt % 7} 号可疑。"
        return r
