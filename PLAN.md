# UEFactory — UE 数据制造农场 · 总计划

> 本文件是项目的**单一事实来源**:做什么、现在做到哪、下一步做什么。
> 由 Planner 维护;Coder(同事)只执行「当前 Sprint」清单中的任务,不自行扩大范围。
> 最后更新:2026-07-08 · 当前阶段:**M0(骨架与冒烟渲染)** · 状态:待 Coder 开工

---

## 1. 愿景与范围

**一句话**:在 Linux headless 环境下,通过一个 CLI(`uef`)持续地收集 UE 可用资产、
并按需批量渲染出各种内容(有光照 / 无光照 / 深度 / 法线 / 分割 mask 等),
最终形态类似 UnrealZoo:用命令行即可指定"我要什么资产、什么视角、什么光照、什么通道"。

**目标(In scope)**
1. 资产摄取管线:本地文件(FBX/glTF/OBJ + PBR 贴图)与公开资产源(PolyHaven、Objaverse 等)→ 导入 UE → 入 catalog(含许可证记录)。
2. Headless 渲染服务:无显示器、无 X server,纯 `-RenderOffscreen` + Vulkan,支持多渲染模式(lit / unlit / depth / normal / basecolor / object mask)、相机环拍(orbit)、光照预设(HDRI / 三点光 / 无光)。
3. CLI 编排:`uef ingest / catalog / render / acquire / farm / doctor`,作业用 YAML JobSpec 描述。
4. 持续获取:定时增量抓取 + 断点续传 + 速率限制 + 许可证过滤。
5. 每一步都产出**可验证的证据**:结构化日志、manifest、缩略图 contact sheet、pytest。

**非目标(Out of scope,现阶段)**
- 实时交互 / gym 接口(UnrealCV 式 TCP 控制)——放到 M5 之后再议。
- 多机分布式——先做好单机多 worker。
- Windows 支持。

## 2. 协作协议(必读)

| 角色 | 谁 | 职责 |
|---|---|---|
| Planner | Claude(本会话) | 写计划、定规范、review 代码、必要时补代码 |
| Coder | 同事 | 按当前 Sprint 清单实现,产出验收产物 |
| Owner | 用户 | 验收里程碑、拍板开放性决策 |

**通信全部走 md 文件 + git,不走口头**:
- `PLAN.md`(本文件)—— Planner 写任务;Coder 只读。
- `docs/WORKLOG.md` —— Coder 每完成一个任务**追加**一条记录(模板见该文件),必须附验收产物路径。
- `docs/QUESTIONS.md` —— Coder 遇到计划没覆盖的决策点,**写问题、停下来或先做别的任务**,不要擅自做重大设计决定。
- `docs/reviews/` —— Planner 的 review 报告;review 发现的问题会变成 PLAN.md 里的 fix 任务。
- `docs/adr/` —— 架构决策记录;重大技术选型必须先有 ADR。

**流程**:Coder 从 `main` 切 `feat/m0-<slug>` 分支 → 按任务提交(Conventional Commits)→
任务全部 DoD 达成后在 WORKLOG 登记并请求 review → Planner review → 通过后由 Planner 合入 `main` 并打 tag。
详细 git / 代码规范见 `docs/CONVENTIONS.md`,**开工前先读完它**。

## 3. 里程碑路线图

| 里程碑 | 交付物 | 验收标准(摘要) |
|---|---|---|
| **M0 骨架与冒烟渲染** | `uef` CLI 骨架、`uef doctor`、headless 渲出第一张非全黑图 | pytest 全绿;`out/smoke/` 有 PNG + manifest + 日志 |
| **M1 渲染服务 v1** | JobSpec(YAML)→ MRQ 渲染:多 pass、orbit 相机、光照预设、contact sheet | 同一资产渲出 lit/unlit/depth/normal 四通道 × 8 视角 |
| **M2 资产摄取** | 本地 FBX/glTF 导入 UE + SQLite catalog + 缩略图 | 10 个杂源模型一键入库,catalog 可查,缩略图正确 |
| **M3 持续获取** | PolyHaven / Objaverse 抓取器、许可证过滤、增量调度 | 无人值守跑 24h,只入 CC0/CC-BY,断点续传可用 |
| **M4 农场化** | 作业队列、多 worker、失败重试、HTML 统计报告 | 100 资产 × 全通道批渲无人值守完成,报告可读 |
| **M5 UnrealZoo 化(后议)** | 交互控制 / 场景组合 / gym 接口 | 待 Owner 定义 |

每个里程碑完成 = Planner review 通过 + Owner 验收 + tag `vX.Y.0`。

## 4. 当前 Sprint:M0 任务清单

> 环境事实(UE 路径、GPU 状况等)见 `docs/ENVIRONMENT.md`,不要重新踩坑。
> 历史参考:`/root/nas/bigdata1/cjw/UE5Projects/` 有以前跑通过的渲染实验
> (`RealisticRender/`、`v2_render.log`),**先翻一遍日志,把已验证可用的命令行参数抄过来**。

### T0.1 Python 包骨架 + `uef doctor` `#skeleton`
- [ ] 按 `docs/CONVENTIONS.md` §6 的目录结构建 `pyproject.toml` + `src/uefactory/`;CLI 用 **typer**;`python3 -m venv .venv && pip install -e ".[dev]"` 可装。
- [ ] `uef --version` 输出版本;`uef doctor` 依次检查并输出**人类可读表格 + `--json` 机器格式**:
  - UE 安装:`UEF_UE_ROOT`(默认 `/root/nas/bigdata1/cjw/UnrealEngine_5.5.4`)下 `Engine/Binaries/Linux/UnrealEditor-Cmd` 存在且可执行,并读出版本(`Engine/Build/Build.version`);
  - GPU:`nvidia-smi --query-gpu=...` 取名称/总显存/**当前空闲显存**(<8GiB 给 WARN);
  - Vulkan:`/etc/vulkan/icd.d/nvidia_icd.json` 存在;若装了 `vulkaninfo` 则跑 summary;
  - 磁盘:repo 所在盘、`$UEF_DATA_DIR`、候选 DDC 路径各自的剩余空间 + 简易写速测试(dd 512MB),NAS(<200MB/s)给 WARN 并提示 DDC 应放本地盘;探测是否存在真正的本地盘(列出非网络挂载点);
  - Python / 依赖版本。
- [ ] 所有配置走 `src/uefactory/core/config.py`(env var `UEF_*` > 配置文件 `uef.toml` > 默认值),路径不许硬编码在业务代码里。
- [ ] 日志基建 `core/log.py`:每次 CLI 运行写 `logs/<UTC时间戳>_<命令>.log`(DEBUG 级),终端只出 INFO;格式见 CONVENTIONS §3。
- **DoD**:`uef doctor` 在本机通过(允许有 WARN);`uef doctor --json | python -m json.tool` 合法;pytest 覆盖 config 优先级与 doctor 的 JSON schema;WORKLOG 附终端输出全文。

### T0.2 UE 基础工程 `ue/UEFBase` `#ue`
- [ ] 建最小 UE 5.5 工程 `ue/UEFBase/UEFBase.uproject`(Blank,无 starter content),启用插件:**PythonScriptPlugin、MovieRenderPipeline(MRQ)、SequencerScripting**。
- [ ] `Config/DefaultEngine.ini` 显式设定:Vulkan RHI、关闭不需要的(在线子系统、崩溃上报弹窗等);把 DDC 路径指到 doctor 探测出的最快盘(通过 ini 或 `UE-LocalDataCachePath` env,方案写进 WORKLOG)。
- [ ] git 只提交 `.uproject` + `Config/` + `Content/Python/`(UE 内脚本);`Content/` 其余、`Saved/`、`Intermediate/`、`DerivedDataCache/` 全部 gitignore(已在根 .gitignore 预置,确认生效)。
- [ ] 首次用 `UnrealEditor-Cmd UEFBase.uproject -run=pythonscript -script="print('hello')" -unattended -nosplash` 之类验证工程能被引擎无头打开(首次会编 shader,可能要很久——把耗时记进 WORKLOG)。
- **DoD**:无头打开成功,退出码 0;UE 全量日志存 `logs/`;WORKLOG 记录首次/二次打开耗时(验证 DDC 生效)。

### T0.3 冒烟渲染 `uef render smoke` `#render`
- [ ] 新增子命令 `uef render smoke [--out out/smoke]`:
  1. 生成场景:UE 启动时执行 `Content/Python/uef_smoke.py` —— 用 UE Python API 在空 Level 里摆一个 Cube(引擎自带 BasicShapes)+ 一盏 DirectionalLight + SkyLight,相机对准;
  2. 渲染:M0 允许走最简单可行路径 —— `-game -RenderOffscreen -ExecCmds="HighResShot 1280x720"` 后从 `Saved/Screenshots/` 收图(MRQ 留给 M1,见 ADR-002);具体参数以踩通为准,过程记 WORKLOG;
  3. 产出:`out/smoke/<UTC时间戳>/frame_0000.png` + `manifest.json`(引擎版本、命令行、耗时、图的尺寸/均值亮度)+ `ue.log`(UE 完整 stdout/stderr);
  4. 校验:图存在、可被 Pillow 打开、**平均亮度 > 5/255(防全黑)**,不满足则命令以非 0 退出并在日志末尾给出 UE log 中的 Error/Warning 摘要。
- [ ] UE 子进程封装进 `src/uefactory/render/ue_runner.py`:超时 kill(默认 30min)、退出码透传、日志落盘、从 UE log 提取 `Error:`/`Warning:` 行数摘要——这个模块以后所有 UE 调用都复用。
- **DoD**:`uef render smoke` 端到端成功;`pytest tests/test_smoke_render.py -m ue`(标记 `ue` 的测试需要引擎,CI 可跳)通过;WORKLOG 附:渲出的 PNG 路径、manifest 内容、耗时。**这是 M0 的核心验收物。**

### T0.4 工程质量基建 `#quality`
- [ ] ruff(lint + format)、mypy(先宽松:`ignore_missing_imports`)、pytest 配置进 `pyproject.toml`;
- [ ] `tools/check.sh`:ruff → mypy → pytest(默认跳过 `-m ue`),一条命令全绿;
- [ ] pre-commit hook(本地 `.pre-commit-config.yaml`):ruff + 禁止直接提交到 main。
- **DoD**:`tools/check.sh` 全绿输出贴 WORKLOG;故意改坏一处能被 ruff 拦下(演示一次)。

### T0.5 收尾
- [ ] WORKLOG 汇总 M0:每任务的产物索引、遇到的坑、给 M1 的建议;
- [ ] `docs/QUESTIONS.md` 里列出所有待 Planner/Owner 决策的问题;
- [ ] 在 feature 分支上请求 review(WORKLOG 末尾写 `REVIEW REQUESTED: <branch> <commit>`)。

**任务顺序**:T0.1 → T0.2 → T0.3 必须串行;T0.4 可穿插。预计 2~4 个工作日(首次 shader 编译不可控)。

## 5. 风险与已知约束

1. **GPU 显存被占**:H100 上有一个常驻进程占 69GiB,渲染只有 ~12GiB 余量。UE 一般够用,但 doctor 必须每次检查;若 OOM,记录现场并在 QUESTIONS 里升级给 Owner(是否协调让出显存)。**禁止 kill 任何不是我们启动的进程。**
2. **全盘皆 NAS(CephFS)**:repo、home 都在 NAS 上。UE 的 DDC/shader 编译对 IO 极敏感——T0.1 doctor 必须找出本地盘;若真没有本地盘,DDC 放 NAS 并把首次编译耗时如实记录,后续再议。
3. **无 docker**:一切原生跑,依赖装进 venv,系统级依赖(如 vulkan-tools)先记录缺什么、写进 QUESTIONS,不擅自 `apt install`(无 root 也未必装得上)。
4. **许可证合规**:M3 起,任何抓取的资产必须记录 license 与来源 URL,默认白名单 CC0/CC-BY;这是硬约束,catalog schema 里 license 字段 NOT NULL。
5. **headless 常见坑**:渲出全黑图(光照/EV/自动曝光问题)、`-RenderOffscreen` 下 swapchain 报错、首帧 GC。所以每个渲染产物都要过"非全黑"断言,UE log 全量落盘。

## 6. 当前假设(Owner 可推翻)

- A1:引擎用已就位的 **UE 5.5.4 预编译 Linux 版**,不自己编引擎(ADR-001)。
- A2:资产用途按"研究/内部数据生产"处理,商用合规问题出现时再升级。
- A3:优先接入的外部资产源顺序:PolyHaven(CC0,API 友好)→ Objaverse → Sketchfab(需 API key)。
- A4:渲染主力管线 M1 起用 MovieRenderQueue(ADR-002)。
