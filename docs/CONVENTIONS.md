# 工程规范(CONVENTIONS)

> 对 Coder 有强制力。Review 时按本文逐条对照;不符合即打回。
> 想改规范?在 `docs/QUESTIONS.md` 提出,由 Planner 修订本文件,不要先斩后奏。

## 1. 工具链

- Python **3.11+**(本机 3.13),虚拟环境 `.venv`(`python3 -m venv .venv`),包管理 `pip install -e ".[dev]"`,一切依赖声明在 `pyproject.toml`,禁止游离的 `pip install`。
- CLI 框架:**typer**;配置:env(`UEF_*`)> `uef.toml` > 默认值;数据库:**SQLite**(标准库 `sqlite3`,暂不引 ORM)。
- lint/format:**ruff**(line-length 100);类型:**mypy**(渐进,新代码必须有类型标注);测试:**pytest**。
- UE 侧脚本:UE 内置 Python(3.11),放 `ue/UEFBase/Content/Python/`,文件名前缀 `uef_`。

## 2. 代码风格

- 模块小而深:对外暴露少量函数,复杂度藏在模块内;跨模块只 import 对方的公开接口(`__init__.py` 导出)。
- 所有路径用 `pathlib.Path`;所有时间用 UTC(`datetime.now(timezone.utc)`),文件名时间戳格式 `YYYYMMDDTHHMMSSZ`。
- 子进程一律走 `subprocess.run/Popen` 显式参数列表(不用 shell=True),必须设 timeout,stdout/stderr 必须落盘。
- 错误处理:能定位的失败要 fail fast 并带上下文(哪个资产、哪条命令、日志在哪);禁止裸 `except:`;禁止吞异常后返回 None。
- 注释:只写代码本身表达不了的约束(如"UE 在 -RenderOffscreen 下此参数必须为…"),不写叙事性注释。
- 禁止:硬编码绝对路径(进 config)、print 调试残留(用 logger)、TODO 不带任务号。

## 3. 日志规范(核心可验证性要求)

每一次 CLI 调用都必须留下可追溯证据:

- `logs/<ts>_<command>.log`:DEBUG 级全量,首行记录:argv、cwd、git commit(`git rev-parse --short HEAD`)、uefactory 版本。
- 终端:INFO 级,人类可读;`--verbose` 提升到 DEBUG。
- 格式:`%(asctime)s %(levelname)s %(name)s: %(message)s`(asctime 为 UTC ISO)。
- UE 子进程:完整 stdout/stderr 另存 `<job_out_dir>/ue.log`;运行结束后自动生成摘要(Error/Warning 各前 20 条 + 总数)追加到主日志。
- 长任务(>30s)要有进度输出(每 N 项或每 30s 一行)。

## 4. 测试与验收产物(Definition of Done)

任何任务没有"证据"就不算完成。**DoD 的验收对象不可自行替换**:如果规定的验收对象做不出来
(如"场景渲染图"做不出来就换"Canvas 图"),不允许换成另一个能通过校验的东西然后标 DONE——
正确动作是把任务标 BLOCKED、把障碍写进 QUESTIONS,等 Planner 裁定(降级验收、换方案、或加时)。
每个任务的 DoD 至少包含:

1. **pytest**:纯逻辑测试(默认跑);需要引擎/网络的测试标记 `@pytest.mark.ue` / `@pytest.mark.net`(默认跳过,`-m ue` 显式跑)。
2. **产物**:渲染类任务必须有输出图 + `manifest.json`(含输入参数、引擎版本、耗时、校验值);批量图必须附 contact sheet(缩略图拼图)。
3. **反例校验**:关键断言要防"假成功"——例:渲染图必须过非全黑检查;导入必须校验三角形数 > 0。
4. **WORKLOG 记录**:产物路径 + 关键命令 + 耗时;别人(Planner)不问你任何问题就能复现。

`tools/check.sh` 必须在每次请求 review 前全绿。

## 5. Git 规范(严格执行)

**身份**(2026-07-08 Owner 定):**作者字段即角色标识**——
- Planner 的提交:`sakii-ko <chijw2004@outlook.com>`(repo 级 git config 已按此配置);
- Coder 的提交:`chijw`(Coder 自己的既有身份;在本 checkout 提交时注意确认 `git config user.name`
  或环境变量生效的是自己的身份,别被 repo 级配置带偏)。
blame/credit 直接按作者统计(`git shortlog -sne`)。Role trailer 不强制(历史提交里出现的无需清理)。
AI 参与的提交保留 `Co-Authored-By: Claude ... <noreply@anthropic.com>` 尾注。

**分支**:
- `main`:只有 Planner 在 review 通过后合入(`merge --no-ff`),Coder 永远不直接 push main。
- 工作分支:`feat/m<里程碑>-<slug>`、`fix/<slug>`、`docs/<slug>`。一个 Sprint 一个主分支即可(如 `feat/m0-skeleton`)。

**提交**(Conventional Commits):
- 格式:`<type>(<scope>): <subject>`;type ∈ feat/fix/docs/test/refactor/chore/perf;scope ∈ cli/render/ingest/catalog/acquire/farm/ue/core/infra。
- subject 用英文祈使句 ≤72 字符;body 可中文,写"为什么"而不是"做了什么"。
- 粒度:一个提交一件事,能通过 `tools/check.sh`(至少不引入新的红);禁止 `wip`、`fix fix` 式提交进入 review。
- 大文件:**任何 >5MB 的二进制不进 git**(渲染输出、下载资产、.uasset 一律走 `out/`、`data/`,已 gitignore)。误提交立即报告,不要自己 rebase 已推分支。

**tag**:里程碑验收后由 Planner 打 `vX.Y.0`;中间可打 `m0-t3-done` 类轻量 tag 标记任务节点。

**远端与 push 纪律**:
- origin = `git@github.com:sakii-ko/uefactory.git`,使用专用 key `~/.ssh/id_github`。
  本 repo 已配置 `core.sshCommand`(repo 级);若在别处 clone,先执行:
  `git config core.sshCommand "ssh -i ~/.ssh/id_github -o IdentitiesOnly=yes"`。
- **每次 commit 后立即 push 当前分支**(`git push origin HEAD`,分支首推加 `-u`)——远端即备份,
  也是 Planner review 的依据;请求 review 时远端必须与本地一致(WORKLOG 里的 sha 以远端为准)。
- push 失败(网络等)不阻塞继续开发,但要在 WORKLOG 标注并尽快补推。
- `main` 与 tag 只由 Planner push;**禁止 force-push 任何已共享分支**(需要改历史先在 QUESTIONS 说明)。

## 6. 目录结构(固定,新目录需 Planner 批准)

```
uefactory/
├── PLAN.md                  # Planner 维护:计划与当前 Sprint
├── README.md
├── pyproject.toml
├── uef.toml.example         # 配置样例(真实 uef.toml 不入库)
├── src/uefactory/
│   ├── cli/                 # typer 入口:main.py + 每个子命令一个文件
│   ├── core/                # config.py / log.py / paths.py
│   ├── render/              # ue_runner.py / jobspec.py / passes.py
│   ├── ingest/              # 导入器
│   ├── catalog/             # SQLite schema + 查询
│   ├── acquire/             # 各资产源抓取器(每源一个模块)
│   └── farm/                # 队列与 worker
├── ue/
│   ├── UEFBase/             # UE 工程(仅 .uproject/Config/Content/Python 入库)
│   └── scripts/             # 在引擎外部使用的 UE 相关辅助脚本
├── tests/
├── tools/                   # check.sh 等
├── docs/                    # 本目录:规范/架构/环境/WORKLOG/QUESTIONS/adr/reviews
├── logs/    (gitignore)
├── out/     (gitignore)     # 渲染输出
└── data/    (gitignore)     # catalog.db、下载缓存
```

## 7. 远程操作纪律(强制,背景见 ADR-003)

- 一切 ssh / rsync / scp 只能经 `core/remote.py` 发起;review 中发现业务代码裸调 ssh 直接打回。
- 连接必须复用:ControlMaster 选项由 `remote.py` 统一注入;多个远程操作能合并成一条批量命令的必须合并。**手工调试时也一样**——你在终端手敲 ssh 探测,请一次带全所有命令。
- 预计 >60s 的远程任务必须进远端 tmux(`uef_<job_id>` 会话),通过远端状态 JSON 轮询(≥30s 间隔),不许保持前台 ssh 等待。
- 破坏性操作(`rsync --delete`、`rm -rf`)只允许指向含 `.uef_node` 哨兵的目录,代码里强制校验,无例外。
- 共享机器(4090)上:不 kill/renice 任何非本项目进程,不碰他人文件,不占满磁盘(写前检查剩余空间,doctor 有 WARN 就先清理再干活)。
- 远端一切皆可丢:任何只存在于远端的状态都必须能从本机数据重建;产物落地以"已 rsync 回本机 NAS"为准。

## 8. UE 侧专项规范

- UE Python 脚本必须可独立重跑(幂等):重复执行不产生重复 actor/资产;入口函数 `main()`,顶层不放副作用代码。
- 与 CLI 的参数传递:通过 `-ExecutePythonScript` 传参困难时,统一用 env var `UEF_JOB_FILE=<json路径>`,UE 内脚本读该 JSON——不要用位置参数字符串拼接。
- 引擎命令行必备:`-unattended -nosplash -nosound -stdout -FullStdOutLogOutput`;渲染加 `-RenderOffscreen`;所有调用都经 `render/ue_runner.py`,禁止散落的 subprocess 调 UE。
- Content 命名:`/Game/UEF/<域>/...`(如 `/Game/UEF/Ingested/<asset_id>/`),资产 id 全小写下划线。
