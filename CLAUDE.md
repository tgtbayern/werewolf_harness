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
| C 流式发言 | ✅ | 草稿 draft/commit/retract 协议，取消上游调用。模拟：首字 p50 2.1 s vs 整段 13.3 s；50 局 2402 条草稿 0 条悬空。**真实 sonnet 5.5**：首字 p50 2.32 s / p95 4.84 s vs 整段 8.37 s / 13.40 s（30 段）。ADR 0009 |
| D 故障注入 | ✅ | `faults/`、`make faults`（约 3 分钟）/`make faults-quick`（约 80 s，接进 CI）。7 场景 × 7 不变量 49/49 ✓，兜底 0；发现并处理 9 个恢复缺陷（worker 遇 Redis 断连退出、WS 停在旧节点、死 worker 任务等满期限、任务静默丢失、在线座位不重试等），回归测试 `tests/integration/test_fault_fixes.py`。ADR 0013、`faults/report.md` |
| E 引擎版本 | ✅ | `ENGINE_VERSION`、`RULE_CHANGES` 规则注册表、v2 新规则"平票 PK"；`scripts/compat_check.py`、`make compat`；34 局真实日志 v1 全一致，v2 下 4 局在第 N 步分叉。ADR 0010 |
| F 缩短整局时长 | ✅ | 开放回合并行决定 + 按序提交、笔记移出关键路径、`claude -p` 精简启动。每回合 14 s → 3.4 s，整局 13–17 分钟 → 约 3 分钟，每次调用 token 9.1k → 4.4k |
| G 模型级联 | ✅ | `players/llm/cascade.py`、ADR 0014。**真实配对**（haiku 5.5 / sonnet 5.5，10 种子 × 3 臂）：成本 0.34× 强模型（现行价 20 倍价差；3 倍价差时 0.64×），合法率非劣效；升级 44.8%，因出错升级仅 1.1%。`docs/cascade_real.md` |
| H 过载保护 | ✅ | 压力信号 + 滞回、准入（201/202 排队/503）、按局公平排队。模拟：过载时进行中对局吞吐保持 92%（无保护 46%）。ADR 0011，`scripts/bench_overload.py` |
| I 容器化 + CI | ✅ | Mac M3 Pro 官方 arm64 镜像实测 `make smoke` 全过（含 Grafana）；冷启动 116 s，`up` 25.9 s，镜像 341 MB 解包 / 72 MB 压缩；故障切换正常。`docs/deploy.md` |
| J 十万局数据库 | ✅ | 大厅首页 77.7 → 0.10 ms，LLM 看板 5.9 s → 68 ms，恢复扫描 3.4 → 1.4 s，归档后每局 107 KB → 3.1 KB。`docs/db_scale.md`、ADR 0012 |
| K 简历与面试材料 | ✅ | `~/Documents/求职与实习/狼人杀项目_简历条目.md`（新写，中英两版）、`狼人杀项目_面试准备清单.md`（数字卡、红线、Q3/Q4/Q51–Q53 更新，新增 Q54–Q63）。原清单备份在会话 scratchpad |

另外修了：投票意图正则（`players/guard/verify.py` `_VOTE_INTENT`，误拦 46 → 0，代价是"集中票出5号"这类说法也识别不了）；驱动层没读 `last_forced` 导致兜底统计为 0。

## 待办

第 0–6 步已在 2026-10-15 的本地会话里做完（见上表）。剩下：

1. **用户自己**：在 GitHub 建仓库，`git remote add origin … && git push -u origin master`，CI 才会跑；确认 CI 绿。
2. Claude 项目「找工」里的 `狼人杀项目_升级计划.md` 末尾状态表在本机没有副本，需要用户把上表同步过去。
3. 可选：级联续跑到 20 对种子（同一条命令把 `--seeds` 改成 `2001-2020`，约 2 小时）；注入攻击实验（还没有任何真实数字）。

## 注意事项

- 改引擎里会影响 `step()` 的规则，必须走 `RULE_CHANGES` 加新版本（见 ADR 0010），并跑 `make compat`。
- 改提示词会让 `tests/regressions/` 的录像对不上 —— 这是预期的，确认是有意改动后用 `make_regression` 重新生成。
- 并行回合：引擎 `open_turns()` 声明可同时决定的回合（封票投票、复盘轮、夜间预言家/守卫/首狼），驱动层提交前比对视图和合法动作指纹，不一致就作废重问。改 `observe()` 时注意别让开放回合的视图在提交前变化（`tests/test_parallel_turns.py` 有性质测试）。
- 云端会话里的 worktree 分支已全部合并进 master，不需要再管。
- 故障注入用的模型 `faults-mock` 只有设置了 `FAULTS_CONTROL` 才会出现；`CLAIM_IDLE_MS`（默认 15 s）是 worker 认领死任务的阈值，worker 会给自己在跑的任务续时间。
