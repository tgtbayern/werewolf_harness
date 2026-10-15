# werewolf_harness — 交接说明（给本地 Claude Code）

这个文件是从云端会话交接过来的完整上下文。Claude Code 打开本仓库会自动读它。
**先读完这个文件，再动手。** 用户是 Yixiao（Eason），USC CS 硕士 2026 年 12 月毕业，前腾讯 QQ 浏览器实习，Java 后端出身，正在准备 2027 届秋招（国内）和美国 new grad。本项目是他简历上的主项目。

## 用户的偏好（必须遵守）

- 跟用户说话用**中文**，简洁、直接；不要自造术语，不要堆细节，讲机制不讲琐碎。
- 用户要你**自己连续做完**，不要每一步都停下来问；能并行就并行。只有真正需要他决定的事才问。
- 写简历 / 面试材料的规则：正式、简洁；第一条是背景；分条带标签；**不用括号**；不写"修复"（自己的项目）；不写"难点在于…""改为…代价…"这类句式；讲机制不讲细节；不编造数字，所有数字都要来自真实测量。
- 安全方向（提示注入）不是主线，主线是**工程能力**。不上线（没有服务器），但要做到"拿到机器就能部署"。

## 仓库与环境（这台 Mac）

- 仓库根：`/Users/li/jobProjects/werewolf_harness`（git 根，包目录是 `werewolf_harness/`）。还没推到 GitHub —— 需要用户自己建仓库并 `git remote add origin … && git push -u origin master`（CI 在 `.github/workflows/ci.yml`）。
- Python venv：`~/jobProjects/.venv-werewolf`（uv 建的，Python 3.12）。`PY=~/jobProjects/.venv-werewolf/bin/python`
- Postgres：brew postgresql@14，`pg_ctl -D /opt/homebrew/var/postgresql@14 start`，库 `wh`（用户 li）。Redis：`redis-server --daemonize yes`。
- `.env` 在仓库根（已在 `.git/info/exclude` 里）：`DATABASE_URL=postgresql+asyncpg://li@localhost:5432/wh`、`REDIS_URL`、`JWT_SECRET`、`AGENT_EXECUTOR=local`、`CLAUDE_CLI_CONCURRENCY=4`。
- 服务：`nohup $PY -m werewolf_harness.cli serve --port 8402 > /tmp/werewolf-server.log 2>&1 &`，浏览器开 http://127.0.0.1:8402 。重启时按端口杀进程：`kill $(lsof -ti tcp:8402 -sTCP:LISTEN)`（**不要用 pkill -f**，会误杀自己的 shell）。服务启动时会自动跑数据库迁移。
- 真实模型用本机的 `claude -p`（订阅额度），模型名 `claude-cli:haiku` / `claude-cli:sonnet` / `claude-cli:opus`。跑实验时设 `CLAUDE_CLI_CONCURRENCY=8`。`claude -p` 不能放在公开服务后面用。
- 实验结果放 `runs/`（不进 git）。
- 测试：`cd /Users/li/jobProjects/werewolf_harness && $PY -m pytest werewolf_harness/tests -q -p no:warnings`。集成测试要 `TEST_DATABASE_URL`（如 `postgresql+asyncpg://li@localhost:5432/wh_test`，先 `createdb wh_test`）和 `TEST_REDIS_URL=redis://127.0.0.1:6379/15`，不设就自动跳过。云端最后一次：**523 个测试全过**，`make lint` 干净。

## 项目是什么

LLM 多 Agent 狼人杀的实时对局服务（"Werewolf Arena"）。核心机制：
- world = f(seed, journal)：引擎 `envs/werewolf.py` 确定性；日志优先提交（先写 Postgres 日志 + 座位记忆同一事务，再推进内存世界，再发事件）。
- 每局一个 asyncio actor（`app/runtime/actor.py`），单写者；Redis 租约 + Postgres fencing token 防双主；崩溃后其他节点按日志重放接管。
- 模型回合交给执行器：本进程线程池（按局公平排队）或 Redis Streams worker（至少一次投递 + 按版本号幂等）。
- LLM 网关 `app/llm/gateway.py`：预算 → 熔断 → 令牌桶 → 调用 → 分类 → 降级链。
- 座位：`players/llm/loop.py` 有界 ReAct 循环，三道闸（schema、白名单、语义），守卫 L1/L2/L3（`players/guard/`），合法动作由引擎按状态计算（"改环境而不是靠提示词"）。
- 文档：`werewolf_harness/docs/architecture.md`、`docs/adr/0001–0014`、`docs/numbers.md`。

## 升级计划进度（项目文档里有一份 `claude/狼人杀项目_升级计划.md`）

| 阶段 | 状态 | 关键结果 |
|---|---|---|
| A 真实评测基线 | ✅ | `scripts/eval_baseline.py`，报告 `docs/baseline_A.md`。30 局 haiku：好人胜率 63.3% [45.5, 78.1]；每局 563k token（CLI 计）；每回合墙钟 p50 6.4 s；整局 203 s；兜底 0/1650；预言家公开验人与引擎一致 29/29。发现 L3 言行一致正则误判（46/46 全是误拦，浪费 16.8% token）→ 已修 |
| B 录制回放 | ✅ | `players/providers/recorded.py`、`cli play --record`、`cli replay-llm`、`scripts/make_regression.py`、`tests/regressions/`。真实局回放：150 次调用 0.14 s，0 次真实调用（录制用了 266 s）。ADR 0008 |
| C 流式发言 | ✅ | 草稿 draft/commit/retract 协议，取消上游调用。模拟：首字 p50 2.1 s vs 整段 13.3 s；50 局注入崩溃/超时，2402 条草稿 0 条悬空。ADR 0009。真实模型还没测：`BENCH_MODEL=claude-cli:sonnet $PY -m werewolf_harness.scripts.bench_streaming --games 0 --crashes 0 --speeches 30` |
| D 故障注入 | ❌ **未开始** | 见下文"待办 1" |
| E 引擎版本 | ✅ | `ENGINE_VERSION`、`RULE_CHANGES` 规则注册表、v2 新规则"平票 PK"；`scripts/compat_check.py`、`make compat`；34 局真实日志 v1 全一致，v2 下 4 局在第 N 步分叉。ADR 0010 |
| F 缩短整局时长 | ✅ | 开放回合并行决定 + 按序提交、笔记移出关键路径、`claude -p` 精简启动。每回合 14 s → 3.4 s，整局 13–17 分钟 → 约 3 分钟，每次调用 token 9.1k → 4.4k |
| G 模型级联 | ✅ 代码 / ⏳ 真实实验 | `players/llm/cascade.py`、`scripts/paired_cascade.py`、ADR 0014、`docs/cascade_mock.md`（模拟：成本 0.56–0.73× 强模型，合法率不差）。真实对比见"待办 3" |
| H 过载保护 | ✅ | 压力信号 + 滞回、准入（201/202 排队/503）、按局公平排队。模拟：过载时进行中对局吞吐保持 92%（无保护 46%）。ADR 0011，`scripts/bench_overload.py` |
| I 容器化 + CI | ✅（沙箱里用替身镜像跑通） | `make smoke`（起整套、走一局、查指标和回放）；Grafana 和官方镜像没实测，Apple Silicon 没实测 → "待办 4" |
| J 十万局数据库 | ✅ | 大厅首页 77.7 → 0.10 ms，LLM 看板 5.9 s → 68 ms，恢复扫描 3.4 → 1.4 s，归档后每局 107 KB → 3.1 KB。`docs/db_scale.md`、ADR 0012 |
| K 简历与面试材料 | ❌ 未开始 | 见"待办 5" |

另外修了：投票意图正则（`players/guard/verify.py` `_VOTE_INTENT`，误拦 46 → 0，代价是"集中票出5号"这类说法也识别不了）；驱动层没读 `last_forced` 导致兜底统计为 0。

## 待办（按顺序，自己连续做完）

**0. 先确认 Mac 上正在跑的批次。** `runs/rec/run.sh` 在重跑基线的 30 个种子（1001–1030，修正后的投票检查，`--record` 录制，每局跑完 `replay-llm` 校验）。看 `runs/rec/DONE` / `STOPPED`、`grep -h "==" runs/rec/*.log`。跑完后：
   - `$PY -m werewolf_harness.scripts.eval_baseline 'runs/rec/g*.json' --compare before='runs/base/g*.json' --out runs/rec/report.md --json runs/rec/metrics.json`（参数以脚本 `--help` 为准），对比修正前后：token、强制收尾率、被拦率、耗时。
   - 把结果写进 `werewolf_harness/docs/baseline_A.md` 的新一节（修正后的对比），并更新 guard 审计那几行旧数字。
   - 挑 2–3 局有代表性的真实局做成回归用例：`$PY -m werewolf_harness.scripts.make_regression runs/rec/g10xx.json --name real_g10xx --upto 40`。

**1. D 故障注入（还没做）。** 规格：
   - 新建 `werewolf_harness/faults/`，一条命令 `make faults`（及 `$PY -m werewolf_harness.faults run [--scenario X] [--games N]`），本地起真实多进程（uvicorn API 节点、Redis Streams worker、本地 Postgres/Redis），注入：① worker 回合中/流式草稿中被 kill -9；② API 节点（局的 owner）被 kill -9，另一节点靠租约过期 + fencing 接管；③ Redis 重启（无持久化，靠重新提问恢复）；④ Postgres 提交中重启；⑤ 模型报错/超时暴增窗口、慢接口窗口；⑥ 自写 asyncio TCP 故障代理做延迟/丢包/断连（没有 toxiproxy）；⑦ 任务重复投递、被 fence 掉的旧 owner 继续写。
   - 座位用确定性且免费的来源：mock / ChaosMock / B 的录像回放。
   - 每个场景后自动检查不变量：日志 seq 从 0 连续无重复、fencing token 单调；已提交回合没有再次调用模型；订阅的真实 SSE/WebSocket 客户端看到的每条草稿要么等于日志正文要么被撤回；每局要么打完要么明确报错，不会卡死；日志重放结果与实时一致（`scripts/verify_replay.py`）；座位记忆行与日志 seq 对齐。
   - 输出 场景 × 不变量 的 ✓/✗ 矩阵和耗时到 `faults/report.md` + JSON，有 ✗ 就非零退出；全量 < 5 分钟，`make faults-quick` < 90 s 并接进 CI 预留的位置。
   - 发现真 bug 就修并加回归测试。写 ADR `docs/adr/0013-fault-injection.md`（中文）。参考已有 `scripts/chaos_demo.py`、ADR 0002/0004/0005/0008/0009。

**2. C 的真实模型测量。** 跑上面 bench_streaming 的真实模型命令（sonnet 或 haiku），把首字延迟写进 `docs/numbers.md`。

**3. G 级联的真实配对实验**（等第 0 步的批次跑完再开，避免抢额度）：
   ```
   CLAUDE_CLI_CONCURRENCY=8 $PY -m werewolf_harness.scripts.paired_cascade --seeds 2001-2010 --cheap claude-cli:haiku --strong claude-cli:sonnet --arms strong,cheap,cascade --cascade-mode both --variant 8p --parallel 8 --record --out runs/cascade/
   ```
   估计约 2.5 小时、1700 万 token（CLI 计）。支持断点续跑。碰到订阅限额就停，等重置后继续。结果写进 `docs/cascade_real.md`（成本比、升级率、合法率/兜底率/被拦率的配对差和置信区间、胜率仅作方向）。价格按 Haiku $1/$5、Sonnet $3/$15 每百万 token —— 先上网核实当前价格。

**4. I 在 Mac 上实测。** `cd werewolf_harness && make smoke`（需要 Docker Desktop；用官方镜像，arm64）。记录镜像大小、冷启动时间、是否全过，更新 `docs/deploy.md`。

**5. K 简历与面试材料。** 用以上所有真实数字更新：
   - 项目文档 `claude/狼人杀项目_简历条目.md`（Claude 项目「找工」里；本地没有就在 `werewolf_harness/resume/` 下写一份）—— 遵守上面的简历规则；
   - `claude/狼人杀项目_面试准备清单.md`：新增流式一致性、故障注入、录制回放、引擎版本、级联、背压、十万局数据库的问题和第一人称口述答案。
   - 简历条目里只放最硬的 3–4 个数字（例如整局时长 4×、过载吞吐保持、回放秒级零调用、草稿零悬空），每个数字要能在 docs 里找到出处。

**6. 收尾。** 更新 `claude/狼人杀项目_升级计划.md` 末尾的状态表；提醒用户建 GitHub 仓库并 push（CI 才会跑）。

## 注意事项

- 改引擎里会影响 `step()` 的规则，必须走 `RULE_CHANGES` 加新版本（见 ADR 0010），并跑 `make compat`。
- 改提示词会让 `tests/regressions/` 的录像对不上 —— 这是预期的，确认是有意改动后用 `make_regression` 重新生成。
- 并行回合：引擎 `open_turns()` 声明可同时决定的回合（封票投票、复盘轮、夜间预言家/守卫/首狼），驱动层提交前比对视图和合法动作指纹，不一致就作废重问。改 `observe()` 时注意别让开放回合的视图在提交前变化（`tests/test_parallel_turns.py` 有性质测试）。
- 云端会话里的 worktree 分支已全部合并进 master，不需要再管。
