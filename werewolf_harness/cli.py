"""Command line.

    python -m werewolf_harness.cli play   --variant 12p --seed 8 --model mock
    python -m werewolf_harness.cli play   --variant 12p --seed 8 --model <gateway model> --guard L1,L2,L3 --out game.json
    python -m werewolf_harness.cli replay game.json          # rebuild the world from the journal, compare outcomes
    python -m werewolf_harness.cli random --variant 12p --seeds 20   # scripted seats, no model: exercises the rules

Models other than "mock" need LLM_API_KEY (and LLM_BASE_URL for a relay).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .arena import Record, Terminal, replay, run
from .envs import VARIANTS, Werewolf
from .players import (BudgetExhausted, GatewayExhausted, GuardStack, LLMPlayer, MockClient,
                      OpenAICompatClient, RandomPlayer, TokenBudget, TransportBudget)


def _client(model: str, seed: int):
    if model == "mock":
        return MockClient(seed=seed)
    from .players.providers.claude_cli import ClaudeCLIClient, is_cli_model
    if is_cli_model(model):
        return ClaudeCLIClient(model)
    key = os.getenv("LLM_API_KEY", "")
    base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    return OpenAICompatClient(model, api_key=key, base_url=base, tool_mode=os.getenv("LLM_TOOL_MODE", "native"))


def cmd_play(a: argparse.Namespace) -> int:
    env = Werewolf(a.variant, playbook_mode=a.playbook)
    n = VARIANTS[a.variant].num_players
    guard = GuardStack(tuple(x for x in a.guard.split(",") if x), evidence_forced=a.evidence)
    budget = TransportBudget()
    tokens = TokenBudget(max_tokens=a.max_tokens, max_usd=a.max_usd)
    players = {s: LLMPlayer(_client(a.model, a.seed * 100 + s), guard=guard, transport_budget=budget,
                            token_budget=tokens, playbook=a.playbook != "none",
                            temperature=a.temperature) for s in range(1, n + 1)}
    t0 = time.time()
    crashed = None
    try:
        ep = run(env, players, a.seed)
    except GatewayExhausted as exc:
        crashed = str(exc)
        print(f"!! 网关不再作答：{exc}", file=sys.stderr)
        return 2
    except BudgetExhausted as exc:
        crashed = str(exc)
        print(f"!! 预算用尽：{exc}", file=sys.stderr)
        return 3
    dur = time.time() - t0
    truth = env.ground_truth()
    print(f"== {a.variant} seed={a.seed} model={a.model} guard={guard.label()} -> {ep.outcome['winner']} in round {ep.outcome['round']} "
          f"({len(ep.journal)} turns, {dur:.0f}s)")
    h = ep.health
    print(f"   roles: {truth['roles']}")
    print(f"   健康度：{h.forced}/{h.turns} 回合兜底（{h.forced_rate:.1%}）"
          + ("  ✓ 可用" if h.usable else "  !! 超过 5%，这一批不能用"))
    print(f"   用量：本地估算 {tokens.total_tokens} tokens / 上限 {tokens.max_tokens}"
          + (f"，约 ${tokens.usd:.2f}" if any(tokens.price_per_1m) else "")
          + (f"；网关自报是本地口径的 {tokens.drift()}×" if tokens.drift() else ""))
    if a.out:
        log = {
            "variant": a.variant, "seed": a.seed, "model": a.model, "guard": guard.label(), "playbook": a.playbook,
            "outcome": ep.outcome, "ground_truth": truth, "duration_s": dur, "crashed": crashed,
            "health": ep.health.to_dict(), "usage": tokens.to_dict(),
            "journal": [r.__dict__ for r in ep.journal],
            "turns": [t.to_dict() for p in players.values() for t in p.history],
            "night": env.night_records(), "pack_chat": env.pack_chat_log(),
        }
        with open(a.out, "w") as f:
            json.dump(log, f, ensure_ascii=False, indent=1, default=str)
        print(f"   wrote {a.out}")
    return 0


def cmd_replay(a: argparse.Namespace) -> int:
    log = json.load(open(a.file))
    env = Werewolf(log["variant"], playbook_mode=log.get("playbook", "static"))
    journal = [Record(**r) for r in log["journal"]]
    end = replay(env, log["seed"], journal, upto=a.upto if a.upto is not None else None)
    if a.upto is not None:
        print(f"after {a.upto} steps -> next turn {end}")
        return 0
    same = isinstance(end, Terminal) and json.loads(json.dumps(end.outcome)) == log["outcome"]   # JSON turns int keys into str
    print("replay:", "identical ✓" if same else f"DIVERGED: {end}")
    return 0 if same else 1


def cmd_random(a: argparse.Namespace) -> int:
    n = VARIANTS[a.variant].num_players
    wins, forced, turns = {}, 0, 0
    for seed in range(1, a.seeds + 1):
        env = Werewolf(a.variant, playbook_mode=a.playbook)
        ep = run(env, {s: RandomPlayer(seed * 100 + s) for s in range(1, n + 1)}, seed)
        end = replay(Werewolf(a.variant, playbook_mode=a.playbook), seed, ep.journal)
        assert isinstance(end, Terminal) and end.outcome == ep.outcome, f"seed {seed} diverged"
        wins[ep.outcome["winner"]] = wins.get(ep.outcome["winner"], 0) + 1
    print(f"{a.seeds} games, all replay-consistent; winners: {wins}")
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    """The online service (needs Postgres + Redis: `make dev`)."""
    import uvicorn
    uvicorn.run("werewolf_harness.app.api.main:app", host=a.host, port=a.port)
    return 0


def cmd_serve_legacy(a: argparse.Namespace) -> int:
    """The original single-process server: standard library only, games live in memory."""
    from .server import serve
    serve(a.host, a.port)
    return 0


def cmd_providers(a: argparse.Namespace) -> int:
    """Say which providers are reachable, and prove it with a real tool call.

    A documented endpoint is a claim; a returned tool_call is a fact. This is
    what moves a row in china.py from the first to the second, and it is the
    only place that spends money -- one short call per model.
    """
    from .players.providers import CANDIDATES, PROVIDERS, profile
    from .players.providers.base import ProviderError
    from .players.providers.openai_compat import OpenAICompatClient
    from .server.http import read_env

    env = read_env()
    tool = [{"type": "function", "function": {"name": "vote", "description": "Cast your vote.",
             "parameters": {"type": "object", "properties": {
                 "target_id": {"type": "integer", "description": "a living player", "enum": [2, 3, 4, 5]},
                 "reason": {"type": "string", "description": "why"}},
                 "required": ["target_id"]}}}]
    msgs = [{"role": "user", "content": "投 5 号出局。调用工具，不要用文字回答。"}]

    for key, prov in PROVIDERS.items():
        token = env.get(prov.env_key)
        models = [c for c in CANDIDATES if c.provider == key]
        if not token:
            print(f"\n{prov.label:22s} 没有 key（.env 里设 {prov.env_key}）")
            print(f"{'':22s} 注册：{prov.console}")
            if prov.free:
                print(f"{'':22s} 免费档：{prov.free}")
            continue
        print(f"\n{prov.label:22s} {prov.base_url}")
        for c in models:
            if a.model and c.model != a.model:
                continue
            p = profile(c.model)
            cl = OpenAICompatClient(c.model, api_key=token, base_url=prov.base_url,
                                    tool_choice=p.get("tool_choice", "required"),
                                    extra_body=p.get("extra_body"),
                                    temperature=p.get("temperature"))
            t0 = time.time()
            try:
                r = cl.chat(msgs, tools=tool, max_tokens=200, timeout=a.timeout)
                dt = time.time() - t0
                if r.tool_calls:
                    print(f"  ✓ {c.model:32s} {dt:5.1f}s  工具调用 {r.tool_calls[0].name}"
                          f"{r.tool_calls[0].arguments}")
                else:
                    print(f"  ! {c.model:32s} {dt:5.1f}s  通了但没返回工具调用"
                          f"（finish={r.finish_reason}）— 多半是 tool_choice 或厂商参数的问题")
            except ProviderError as exc:
                print(f"  ✗ {c.model:32s} {time.time() - t0:5.1f}s  {str(exc)[:150]}")
    print("\n没有 key 的行只是还没注册，不是坏了。表在 players/providers/china.py。")
    return 0


def cmd_resume(a: argparse.Namespace) -> int:
    """Pick a saved game up where it stopped, and keep it in the running server."""
    import json
    import urllib.request
    log = json.load(open(a.file, encoding="utf-8"))
    req = urllib.request.Request(f"http://{a.host}:{a.port}/api/games/resume",
                                 data=json.dumps({"log": log, "max_tokens": a.max_tokens}).encode(),
                                 method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        g = json.loads(r.read())
    # A game summary carries its own `error` field (usually null), so the presence
    # of the key says nothing -- only a failure with no game in it is a failure.
    if "id" not in g:
        print("接不上：", g.get("error") or g)
        return 1
    print(f"{g['id']} 从 {len(log.get('journal') or [])} 个回合处接着跑 "
          f"（{g['variant']} · {g['playbook']} · seed {g['seed']}）")
    print(f"http://{a.host}:{a.port}/#/g/{g['id']}?seat=0")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="werewolf_harness")
    sub = p.add_subparsers(dest="cmd", required=True)
    pl = sub.add_parser("play")
    pl.add_argument("--variant", default="12p", choices=sorted(VARIANTS))
    pl.add_argument("--seed", type=int, default=1)
    pl.add_argument("--model", default="mock")
    pl.add_argument("--guard", default="L1,L2,L3", help="comma-separated layers, or empty")
    pl.add_argument("--evidence", action="store_true")
    pl.add_argument("--playbook", default="static", choices=["none", "static", "dynamic"])
    pl.add_argument("--temperature", type=float, default=0.7)
    pl.add_argument("--max-tokens", type=int, default=6_000_000, dest="max_tokens",
                    help="本地估算的每局 token 上限，超了停局")
    pl.add_argument("--max-usd", type=float, default=None, dest="max_usd")
    pl.add_argument("--out", default=None)
    rp = sub.add_parser("replay")
    rp.add_argument("file")
    rp.add_argument("--upto", type=int, default=None)
    rd = sub.add_parser("random")
    rd.add_argument("--variant", default="12p", choices=sorted(VARIANTS))
    rd.add_argument("--seeds", type=int, default=10)
    rd.add_argument("--playbook", default="static", choices=["none", "static", "dynamic"])
    sv = sub.add_parser("serve", help="在线服务（Postgres + Redis），用浏览器坐进去玩")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8402)
    sl = sub.add_parser("serve-legacy", help="旧的单进程服务（标准库，不持久化）")
    sl.add_argument("--host", default="127.0.0.1")
    sl.add_argument("--port", type=int, default=8402)
    rs = sub.add_parser("resume", help="从存下来的日志接着跑一局被中断的游戏")
    rs.add_argument("file")
    rs.add_argument("--host", default="127.0.0.1")
    rs.add_argument("--port", type=int, default=8402)
    rs.add_argument("--max-tokens", type=int, default=None, dest="max_tokens")
    pv = sub.add_parser("providers", help="探活：哪几家有 key、通不通、能不能返回工具调用")
    pv.add_argument("--model", default=None, help="只探这一个模型")
    pv.add_argument("--timeout", type=float, default=60.0)
    a = p.parse_args(argv)
    return {"play": cmd_play, "replay": cmd_replay, "random": cmd_random,
            "serve": cmd_serve, "serve-legacy": cmd_serve_legacy, "providers": cmd_providers, "resume": cmd_resume}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
