# werewolf_harness — Werewolf on the Arena protocol

A 12-player Werewolf table (also 8p / 15p) rebuilt on a five-method environment
contract, with LLM seats behind a one-method player contract. The old harness
(engine + agent loop + server + evaluation) is in git history before this rewrite;
this tree keeps only what the framework needs and what the game needs.

```
arena/      the framework: protocol.py (Environment / Player / ActionSpec / ToolSpec), driver.py (run, replay)
envs/       werewolf.py — the whole game: rules, phases, visibility, legality, rules text
players/    scripted.py (RandomPlayer), llm/ (LLMPlayer: prompt, ReAct loop, info tools, beliefs),
            guard/ (L1 isolate, L2 filter, L3 verify, E evidence), providers/ (OpenAI-compatible, mock),
            recovery.py (retries, terminal refusal, transport budget), strategies/ (wolf playbook, pulled context)
tests/      79 tests: rules pinned to rules_text, isolation, driver/replay, prompt contract, LLM loop, guard
cli.py      play / replay / random
```

## Run

```
.venv/bin/python -m werewolf_harness.cli random --seeds 20              # scripted seats, no model
.venv/bin/python -m werewolf_harness.cli play --seed 8 --model mock --out game.json
.venv/bin/python -m werewolf_harness.cli replay game.json               # world == f(seed, journal)
LLM_API_KEY=... LLM_BASE_URL=https://relay/v1 .venv/bin/python -m werewolf_harness.cli play --model claude-sonnet-4-6 --guard L1,L2,L3
pytest werewolf_harness/tests -q
```

## The contract

```python
class Environment(Protocol):
    def reset(self, seed) -> Turn | Terminal          # deal, build state; first actor
    def observe(self, actor) -> dict                  # the only information exit (G1)
    def action_space(self, actor, task) -> ActionSpec # what may be submitted now; rendered AND checked (G2)
    def step(self, actor, task, action) -> Turn | Terminal   # the only state write; says who is next
    def rules_text(self) -> str                       # rendered from the Variant, pinned by tests (G3)

class Player(Protocol):
    def act(self, view, task, spec, feedback) -> action
```

`run()` in `arena/driver.py` is 55 lines and contains no game word (a test greps
for it). Every seat -- random script, `LLMPlayer`, a browser later -- implements
`act`. The journal `[(actor, task, action)]` rebuilds the world exactly (G4);
`cli replay` proves it on any saved game.

## The game (envs/werewolf.py)

- Seats 1..N, roles dealt from `Variant.role_counts` with `random.Random(seed)`; that and the
  fallback target order are the engine's only randomness.
- Night order: seer → guard → wolves (seat order, majority kill, first-named breaks ties) → witch.
  Pack chat and pack planning (static: once; dynamic: every night) precede the night for 12p/15p.
- Day: round-1 sheriff election (candidates do not vote; one candidate takes the badge; a tie leaves
  it unclaimed), speech in seat order rotated to the sheriff, vote with a 1.5 sheriff ballot, tie
  exiles nobody. Interrupts in order: hunter shot → badge → last words.
- Rule parameters on the `Variant` (`guard_self`, `guard_witch_overlap_kills`, `witch_self_save`,
  `hunter_shoots_on`) are each an engine branch **and** a sentence in `rules_text()`; `tests/test_rules.py`
  plays every sentence out.
- Deliberate choices where the old engine and its own rules text disagreed: the witch cannot save
  herself; a hunter-shot sheriff still passes the badge; a wolf who skips does not cancel the pack's
  knife; the guard stops the knife, not the poison; a self-detonation is public as "detonated".
- Terminal actions are `ToolSpec`s whose legal arguments (`choices`) are computed from state. The
  environment never sees an illegal action: the driver checks the same object it rendered.

## The LLM seat (players/llm)

`LLMPlayer.act` builds the system message (rules from the view, private identity block, L1 preamble,
output contract naming exactly the tools this turn offers) and the situation (public record, this
round's speeches -- others' fenced and filtered by the guard -- own beliefs, the tagged task line),
then runs up to 8 ReAct steps. Information tools (`query_history`, `query_votes`, `query_deaths`,
`read_belief`, `update_belief`, `check_ability`, and for wolves the playbook tools) read the view and
the seat's `BeliefState`; a terminal call is checked against the `ActionSpec` and the guard's
output-side checks (say/do consistency, directive compliance, evidence, forensics) and returned.
Malformed or unknown calls are bounded and retried with the reason; a turn that cannot be salvaged
returns the environment's fallback and records `fallback_used`. `TransportBudget` abandons a game
whose model stops answering; a balance/key refusal raises `GatewayExhausted` at once.

Prompt neutrality is a contract, not a style: see `PROMPTS.md`, enforced by `tests/test_prompts.py`.

## Not carried over

Server / dashboard, MCP, the attack corpus and the evaluation kit stayed behind in git history
(`git log -- werewolf_harness/server`). `experiments/run_arms.py` and `experiments/analyse_memory.py`
import the old modules and no longer run.

## 自己坐进去玩

```
.venv/bin/python -m werewolf_harness.cli serve          # http://127.0.0.1:8402
```

浏览器打开就是大厅：选牌型、模型、防护层、你坐哪个位置、每回合给你多久，开桌即入座。
先用 `mock`（离线、秒回、不花钱）走一遍流程，熟了再换 `gemini-3-flash`（一步约 2 秒，一整局十几分钟）
或 `gpt-5.4`（一步 5–60 秒，一整局一两个小时）。真模型要 `.env` 里的 `LLM_BASE_URL` 和分组 key。

真人座位就是一个 `Player` 实现：拿到的 view 和 AI 拿到的是同一个 `observe()` 切出来的，
能提交的东西是同一个 `action_space()` 算出来的，页面把那张单子渲染成按钮而已。超时不卡桌——
到点用环境自己的 fallback 并在记录里标成兜底。提交带回合序号，重发和手抖只落一次。

接口（无框架，标准库 HTTP）：

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/meta` | 可选的牌型、模型、防护层 |
| `GET` | `/api/games` | 桌子列表（含每桌健康度） |
| `POST` | `/api/games` | 开桌 `{variant, model, guard, human_seats, turn_seconds}` |
| `POST` | `/api/games/{id}/start` | 开局 |
| `GET` | `/api/games/{id}?seat=N` | 这个座位的 view + 它这一回合能提交什么 |
| `POST` | `/api/games/{id}/act` | `{seat, seq, action}`；重复的 `seq` 原样返回第一次的结果 |
| `GET` | `/api/games/{id}/events?after=N` | SSE，每一步一条 |
