# UEFactory — UE 数据制造农场 · 总计划

> 本文件是项目的**单一事实来源**:做什么、现在做到哪、下一步做什么。
> 由当前执行代理统一维护并直接实施;不再拆分 Planner → Coder/Executor 交接。
> 最后更新:2026-07-10(第 12 次) · 当前阶段:**M2 `v0.3.0` 发布完成,M3 待启动** · 已完成:**M0 `v0.1.0`;M1 `v0.2.0`;M2 已通过正式审计、合入 `main` 并标记 `v0.3.0`**
> **当前主线:M3 持续获取——PolyHaven adapter 打样 → Objaverse LVIS 灌库 →
> 质量门禁/去重 → 每日增量调度与 24h 无人值守验收。**
> 规则不变:DoD 验收对象不可替换;实现、测试、真实运行、可视化审阅和 review 必须形成闭环。
---

## 1. 愿景与范围

**一句话**:在 Linux headless 环境下,通过一个 CLI(`uef`)持续地收集 UE 可用资产、
并按需批量渲染出各种内容(有光照 / 无光照 / 深度 / 法线 / 分割 mask 等),
最终形态类似 UnrealZoo:用命令行即可指定"我要什么资产、什么视角、什么光照、什么通道"。

**目标(In scope)**
1. 资产摄取管线:本地文件(FBX/glTF/OBJ + PBR 贴图)与公开资产源(PolyHaven、Objaverse 等)→ 导入 UE → 入 catalog(含许可证记录)。
2. Headless 渲染服务:无显示器、无 X server,纯 `-RenderOffscreen` + Vulkan,支持多渲染模式(lit / unlit / depth / normal / basecolor / object mask)、相机环拍(orbit)、光照预设(HDRI / 三点光 / 无光);**可在本机或远程 GPU 节点(`4090`/`l40s`)执行**,数据主库始终在本机 NAS(ADR-003)。
3. CLI 编排:`uef ingest / catalog / render / acquire / farm / doctor`,作业用 YAML JobSpec 描述。
4. 持续获取:定时增量抓取 + 断点续传 + 速率限制 + 许可证过滤。
5. 每一步都产出**可验证的证据**:结构化日志、manifest、缩略图 contact sheet、pytest。

**非目标(Out of scope,现阶段)**
- 实时交互 / gym 接口(UnrealCV 式 TCP 控制)——放到 M5 之后再议。
- 多机分布式——先做好单机多 worker。
- Windows 支持。

## 2. 工作协议(必读)

| 角色 | 谁 | 职责 |
|---|---|---|
| 执行代理 | Codex(当前会话) | 统一负责计划、设计、实现、测试、真实运行、可视化审阅、review 与里程碑收口 |
| Owner | 用户 | 定义终极目标;仅在权限、外部资源或会实质改变目标的开放性选择上介入 |

项目状态以文件与 git 为准:

- `PLAN.md`(本文件)—— 当前目标、任务顺序与 DoD;执行中同步维护。
- `docs/WORKLOG.md` —— **只追加**实际命令、结果、产物路径、失败修正和耗时。
- `docs/QUESTIONS.md` —— 只有真正需要 Owner 拍板或提供外部条件时才登记阻塞。
- `docs/reviews/` —— 每个里程碑的独立审计结论;发现的问题先修复再放行。
- `docs/adr/` —— 跨里程碑的不变量与重大技术选型。

**流程**:在 `feat/m<里程碑>-<slug>` 上直接规划并实现 → 自动化测试 → 真实 UE/远程运行
→ 亲眼审阅图像/报告 → 更新 WORKLOG 与 ADR → 正式 review → Conventional Commits →
合入 `main` 并打 tag。除非遇到真实外部阻塞,不再用 Planner/Coder 信号交接停顿。
详细 git、代码与证据规范见 `docs/CONVENTIONS.md`。

## 3. 里程碑路线图

| 里程碑 | 交付物 | 验收标准(摘要) |
|---|---|---|
| **M0 骨架与冒烟渲染** | `uef` CLI 骨架、`uef doctor`、headless 渲出第一张非全黑图 | pytest 全绿;`out/smoke/` 有 PNG + manifest + 日志 |
| **M1 渲染服务 v1** | JobSpec(YAML)→ MRQ 渲染:六 pass、orbit 相机、三种光照、contact sheet/MP4;本地/远程同一入口 | **已通过正式审计**:六通道 × 8 视角;本地重复运行及本地↔l40s 共 48 帧解码像素哈希完全一致;HDRI/none 视觉验收与失败清理通过 |
| **M2 资产摄取** | 本地 FBX/glTF 导入 UE + SQLite catalog + 缩略图;Owner 追加外部 scene-level 兼容 | **已通过正式审计并发布 `v0.3.0`**:11 个杂源模型一键入库,catalog 可查,缩略图正确;8 个开放场景可持久构建/重载/渲染 |
| **M3 持续获取** | 按 `docs/ASSET_ACQUISITION.md` 五腿战略:PolyHaven adapter 打样 → Objaverse LVIS 灌库 → 质量门禁/去重 → 每日增量调度 | 无人值守跑 24h;license 三档(open/nc/ue-only)全程可追溯;catalog stats 报告可读 |
| **M4 农场化** | 作业队列、**多节点池调度**(本机 + 4090 + l40s)、失败重试、HTML 统计报告 | 100 资产 × 全通道批渲无人值守完成(跨节点),报告可读 |
| **M5 UnrealZoo 化(后议)** | 交互控制 / 场景组合 / gym 接口 | 待 Owner 定义 |

每个里程碑完成 = DoD 证据齐全 + 正式 review 通过 + 可追溯提交/合并 + tag `vX.Y.0`。

## 4. 已完成 Sprint:M2 任务清单(资产摄取)

> M0 已验收并标记 `v0.1.0`;M1 已合入 `main` 并标记 `v0.2.0`。M1 的修正版证据、正式审计和渲染数据契约见
> `docs/WORKLOG.md`、`docs/reviews/2026-07-10-formal-m1-render.md` 与 ADR-004。
> M2 的验收对象是**真实本地 FBX/glTF/GLB 资产**,不是内置 cube 或伪造 catalog 行。

### T2.1 IngestSpec 与至少十资产样例集(当前 11 个) `#ingest` `#acquire`

- [x] 定义严格的 ingest manifest:本地路径、稳定 `asset_id`、名称、source/source URL、SPDX 风格
  license、tags;未知/缺失字段 fail fast,license 不允许为空。
- [x] 准备至少 10 个可再分发的开放许可样例,覆盖 FBX 与 glTF/GLB、带/不带贴图、不同尺度与层级;
  原始大文件放 `data/raw/`,git 仅记录小型 manifest、来源与许可证证据。
- [x] 下载必须可重入,校验 size + SHA-256,临时文件原子改名;失败不得伪装为已获取。
- **DoD(已达成)**:一条命令可准备/校验样例集;当前 11 个条目(6 GLB + 5 FBX、34 files、
  60,003,947 bytes)均有来源、license、内容 hash;重复执行 `downloaded=0,reused=34`。

### T2.2 SQLite catalog v1 `#catalog`

- [x] 用标准库 `sqlite3` 落地 versioned schema 与迁移:assets、artifacts,外键开启;路径存相对路径;
  asset/license/status/source/hash/timestamp 约束入库级强制。
- [x] CLI 至少支持 `catalog init/list/show/stats`;查询支持 id、status、source、license、tag。
- [x] upsert 必须事务化;同 hash 重复资产可检测,失败导入保留结构化 error 而不污染 imported 状态。
- **DoD(已达成)**:schema v3 空库初始化幂等;11 资产可查;非法 license/状态、重复 id/hash 与
  事务回滚均有反例测试;release DB `integrity_check=ok`,FK clean。

### T2.3 UE 5.5.4 headless 导入 `#ingest` `#ue`

- [x] `uef ingest asset|batch` 生成 JSON 作业,经统一 `run_ue` 调 UE Python;优先使用 Interchange,
  必要时按格式使用受控 fallback,不得依赖 Editor GUI。
- [x] 输出到 `/Game/UEF/Ingested/<asset_id>/`,写 import manifest:package paths、mesh 数、三角形数、
  material/texture 数、bounds、引擎版本、日志摘要与耗时。
- [x] 进程失败、warning/error、零 mesh/零三角形、缺 package 都 fail closed;重复导入幂等且不会叠资产。
- **DoD(已达成)**:11/11 经真实 UE 5.5.4 headless 导入;主日志均观察到 Interchange
  start/completed 且零未过滤 error/warning;每个 package 均由独立 UE 进程重载并提交事务，
  finalize 前后完整 path/size/file SHA-256 闭包一致。

### T2.4 规范化与质量门禁 `#ingest`

- [x] 统一厘米/米换算、Z-up、pivot 落地与可配置缩放;保留源变换信息以便追溯。当前精确边界为
  source conversion 委托 UE importer、package pivot 保留、render actor 应用可配置 scale 与
  bounds bottom-center framing;glTF/GLB 保存 canonical graph/TRS,FBX 明确 delegated/unavailable。
- [x] 自动门禁至少覆盖:有限非零 bounds、三角形数 > 0、合理尺度、材质槽/纹理引用可解析、无 NaN;
  失败进入 `failed`/quarantine 并记录规则版本与原因。
- [x] 不把“导入命令退出 0”等同于质量通过;为每条门禁制作失败 fixture。
- **DoD(已达成)**:11 个验收资产全部通过 `m2_static_mesh_v2`;quality policy、完整 check 集合与
  source-structure digest 进入 skip key;Box 作为 0-texture/hierarchical 反例通过。

### T2.5 Catalog ↔ render 与缩略图闭环 `#catalog` `#render`

- [x] JobSpec 支持 catalog `asset_id`,UE setup 加载已导入 StaticMesh,自动按 bounds 构图、落地并赋 stencil;
  `builtin:cube` 仅保留回归用途。
- [x] 每个 imported 资产生成至少一张标准 beauty 缩略图和 object mask;缩略图/manifest 作为 artifacts 入 catalog。
- [x] 复用 M1 的解码像素、格式、mask/bounds 校验;图像必须由执行代理逐张或 contact sheet 亲眼审阅。
- **DoD(已达成)**:11 资产各有 8-view beauty/mask 与 5 个 thumbnail artifacts;均非黑、主体完整、
  不穿地/严重裁切,mask 一致;执行代理已逐 contact sheet 检查。

### T2.6 至少十资产一键端到端验收(当前 11 个) `#ingest` `#catalog` `#ue`

- [x] 一条 batch 命令完成 manifest 校验 → catalog raw → UE import → 门禁 → thumbnail → catalog imported。
- [x] 中途失败可安全重跑,已成功项不重复工作;最终生成 JSON + HTML 汇总和 11 资产 contact sheet。
- [x] 跑纯逻辑全量测试、真实 UE 集成测试,并至少随机独立重载 3 个 package 验证持久化。
- **DoD(已达成)**:最终 fresh batch 11/11 `render_ok`,立即重跑 11/11 `skipped` 且不启动 UE；
  `catalog list/show/stats` 可查,11 组缩略图视觉正确；64 个 UE package files / 68,910,435 bytes
  的完整闭包、66 artifacts、manifest、数据库和磁盘三方一致。

### T2.6A Owner 追加:scene-level / BlackMyth 兼容 `#scene` `#blackmyth`

- [x] 外部 BlackMyth 目录只读扫描;license 分层/隔离,source root 显式传入,示例不硬编码本机路径。
- [x] SceneSpec → persistent UE level → 独立 reload/finalize → schema v3 catalog generation,
  保留逐 actor mesh/transform/bounds inventory。
- [x] 8 个开放许可场景全部 `render_ok`:748 个 scene objects、72 个 scene artifacts;
  逐 scene contact sheet 已检查。research-only 样例不冒充开放发布资产。
- **DoD(已达成)**:`UEF_BLACKMYTH_ROOT=/home/chijw/workspace/projs/blackmyth` 实测扫描
  14 records、0 quarantine;9/9 YAML 可解析;8 个开放场景在当前 portable SceneSpec SHA 上重新
  build/reload/finalize/thumbnail,严格审计为 748 objects / 72 artifacts 且逐张视觉通过。

### T2.7 收尾与正式 review

- [x] README 增加 ingest/catalog 五分钟路径;ARCHITECTURE 将 M2 schema/数据流由草案改为实装契约。
- [x] package/CLI/lock 版本统一为 `0.3.0`,并有版本漂移回归测试。
- [x] 正式 review #1 的两个 scene MAJOR 已关闭:standalone scene render 持完整 generation lease；
  scene package evidence 覆盖 root 下完整 regular-file tree 并在 finalize 后精确复验。
- [x] WORKLOG 追加所有真实命令、耗时、失败修正、产物路径;新增 M2 正式 review,无高/中未解决项。
- [x] Conventional Commits 推送分支,以 `--no-ff` 合入 `main`,打 annotated tag `v0.3.0` 并推送远端。

**任务顺序**:T2.1 → T2.2 → T2.3 → T2.4 → T2.5 → T2.6 → T2.6A → T2.7。
实现时允许为测试并行准备样例和 catalog,但不允许用未经过前序门禁的结果冒充后序 DoD。
实现分支:`feat/m2-ingest`;最终提交 `3f46bda`,合并提交 `f140f51`。下一 Sprint 在新的 M3 功能分支启动。

## 5. 风险与已知约束

1. **GPU 显存占用会动态变化**:2026-07-10 doctor 时 H100 约 79GiB 可用,但历史上曾只剩 ~12GiB。
   UE/ingest 重任务前必须 doctor;若 OOM,记录现场并升级给 Owner。**禁止 kill 任何不是我们启动的进程。**
2. **全盘皆 NAS(CephFS)**:repo、home 都在 NAS 上。UE 的 DDC/shader 编译对 IO 极敏感——T0.1 doctor 必须找出本地盘;若真没有本地盘,DDC 放 NAS 并把首次编译耗时如实记录,后续再议。
3. **无 docker**:一切原生跑,依赖装进 venv,系统级依赖(如 vulkan-tools)先记录缺什么、写进 QUESTIONS,不擅自 `apt install`(无 root 也未必装得上)。
4. **许可证合规**:M3 起,任何抓取的资产必须记录 license 与来源 URL,默认白名单 CC0/CC-BY;这是硬约束,catalog schema 里 license 字段 NOT NULL。
5. **headless 常见坑**:渲出全黑图(光照/EV/自动曝光问题)、`-RenderOffscreen` 下 swapchain 报错、首帧 GC。所以每个渲染产物都要过"非全黑"断言,UE log 全量落盘。
6. **远端同路径陷阱**:l40s 的 `/root/nas/bigdata1` 是另一个文件系统,内容与本机不同。任何远程脚本禁止假设路径相同即数据相同;`--delete`/`rm -rf` 必须先验 `.uef_node` 哨兵(ADR-003)。
7. **4090 是共享机器且存储近满**(`/home` 97%、`/data1` 100%):严禁影响他人进程/文件;我们只用自己的工作目录,渲后即清;引擎 + 暂存总占用给出硬上限(建议 ≤150GB)并在 doctor 里监控。
8. **WAN 带宽未知**:引擎 provision(几十 GB)与批量产物回传可能很慢;一切大传输走 `rsync --partial` 断点续传 + 远端 tmux,首次实测带宽记入 WORKLOG,作为 M4 调度参数。
9. **l40s 是容器,随时可能重建**:持久数据只放它自己的 NAS;每次任务前 doctor 校验哨兵还在,不在就自动重新 `node init + provision`(幂等设计的意义)。

## 6. 当前假设(Owner 可推翻)

- A1:引擎用已就位的 **UE 5.5.4 预编译 Linux 版**,不自己编引擎(ADR-001)。
- A2:资产用途按"研究/内部数据生产"处理,商用合规问题出现时再升级。
- A3:资产供给按 `docs/ASSET_ACQUISITION.md` 五腿战略执行(存量数据集 / API 抓取 / UE 生态半人工 /
  程序化生成 / AIGC 定向补货 + 变体增殖);接入顺序 PolyHaven → Objaverse LVIS → Sketchfab。
  该文档 §4 有四个待 Owner 拍板项(nc 档收不收、Fab 人工通道、AIGC 许可尺度、Objaverse-XL 范围)。
- A4:渲染主力管线 M1 起用 MovieRenderQueue(ADR-002)。
