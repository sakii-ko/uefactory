# UEFactory — UE 数据制造农场 · 总计划

> 本文件是项目的**单一事实来源**:做什么、现在做到哪、下一步做什么。
> 由 Planner 维护;Coder(同事)只执行「当前 Sprint」清单中的任务,不自行扩大范围。
> 最后更新:2026-07-09(第 6 次) · 当前阶段:**M1 渲染服务 v1** · 上一里程碑:**M0 已由 Owner 验收,tag `v0.1.0`**
> **当前主线:T1.2(MRQ headless spike)→ T1.3 JobSpec → T1.4 多通道 → T1.5 光照预设
> → T1.6 统一执行器 + contact sheet + turntable 视频 → T1.7 收尾 → M1 验收。
> T1.1(4090)降级为机会性任务,不阻塞 M1(Owner 2026-07-09 指示)。**
> 规则不变:DoD 验收对象不可自行替换;一次只推进一个任务;通知走 docs/SIGNALS.md 信号。
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
**互相通知走信号机制**(2026-07-08 起,Owner 不再中转):协议见 `docs/SIGNALS.md`——
push 完发 `tools/signal.sh planner REVIEW_REQUESTED`,收到 `REVIEW_DONE` 信号后 pull 并读 review。

## 3. 里程碑路线图

| 里程碑 | 交付物 | 验收标准(摘要) |
|---|---|---|
| **M0 骨架与冒烟渲染** | `uef` CLI 骨架、`uef doctor`、headless 渲出第一张非全黑图 | pytest 全绿;`out/smoke/` 有 PNG + manifest + 日志 |
| **M1 渲染服务 v1** | JobSpec(YAML)→ MRQ 渲染:多 pass、orbit 相机、光照预设、contact sheet;本地/远程节点同一入口 | 同一资产渲出 lit/unlit/depth/normal 四通道 × 8 视角,并在**至少一个远程节点(l40s)**跑通;4090 期间可用则一并跑通 |
| **M2 资产摄取** | 本地 FBX/glTF 导入 UE + SQLite catalog + 缩略图 | 10 个杂源模型一键入库,catalog 可查,缩略图正确 |
| **M3 持续获取** | 按 `docs/ASSET_ACQUISITION.md` 五腿战略:PolyHaven adapter 打样 → Objaverse LVIS 灌库 → 质量门禁/去重 → 每日增量调度 | 无人值守跑 24h;license 三档(open/nc/ue-only)全程可追溯;catalog stats 报告可读 |
| **M4 农场化** | 作业队列、**多节点池调度**(本机 + 4090 + l40s)、失败重试、HTML 统计报告 | 100 资产 × 全通道批渲无人值守完成(跨节点),报告可读 |
| **M5 UnrealZoo 化(后议)** | 交互控制 / 场景组合 / gym 接口 | 待 Owner 定义 |

每个里程碑完成 = Planner review 通过 + Owner 验收 + tag `vX.Y.0`。

## 4. 当前 Sprint:M1 任务清单(渲染服务 v1)

> M0(T0.1–T0.7)已全部完成并验收,tag `v0.1.0`;过程与证据见 WORKLOG 与 docs/reviews/,此处不再保留细节。
> M1 目标(里程碑表):JobSpec(YAML)→ 多 pass、orbit 相机、光照预设、contact sheet;
> 本地/远程同一入口;验收标准:**同一资产渲出 lit/unlit/depth/normal 四通道 × 8 视角,且在 4090 节点跑通**。
> 继承不变量:**确定性渲染**(同 job 两次运行输出一致;破坏此性质的变更需先记 ADR)。

### T1.1 4090 节点上线 `#remote`(**机会性任务,不阻塞 M1**;Owner 2026-07-09 指示)
- [ ] 每个工作日**至多一次**轻量探测(单条 ssh echo,不 hammer);不通只记一行 WORKLOG,继续主线;
- [ ] 探测通了才升级动作:`node init` → `provision`(tmux + `--partial`,落点 `/home/lyf/uef/engine/`;
  传输挂后台,不占串行槽)→ doctor → 远程 smoke;共享机器纪律与 150GB 硬上限不变;
- [ ] GPU 挑选策略(8 卡选空闲)与实测带宽记 WORKLOG。
- **DoD(仅当节点可用时适用)**:4090 冒烟图过校验 + 确定性;渲染不影响该机其他用户(nvidia-smi 前后对照)。
- **M1 验收不依赖本任务**;若 M1 结束时仍不可用,顺延 M2 并在 WORKLOG 记录探测历史。

### T1.2 MRQ headless 可行性 spike `#render`(M1 最大技术风险,先消解)
- [ ] 最小验证:UEFBase 里建一个 LevelSequence(相机固定或简单位移)+ MRQ 配置,headless
  (`-game -RenderOffscreen` 或 editor `-ExecutePythonScript` 驱动 MRQ,以实测为准)渲出 8 帧 PNG 序列;
- [ ] 确定性验证:同配置两跑,逐帧 luma 一致;
- [ ] 结论写 WORKLOG:可行路径、必需命令行 flags、坑(首帧 GC、TAA 抖动等);**若 MRQ 不可行**,
  停下发 BLOCKED 信号 + QUESTIONS 里给备选评估(SceneCapture 多 pass 方案),等 Planner 裁定(ADR-002 约定)。
- **DoD**:8 帧序列 + 两跑一致证据 + WORKLOG spike 报告。**不做任何超出 spike 的封装**——先证明,再工程化。

### T1.3 JobSpec v1 + orbit 相机 `#render`
- [ ] JobSpec YAML schema 落地(ARCHITECTURE §3 草案):`assets/camera(rig=orbit,views,elevation_deg,fov,resolution)/lighting(preset)/passes/output`;
  dataclass + 显式校验(未知字段、缺字段、非法值一律 raise,错误信息带字段路径);schema 单测(合法/非法例);
- [ ] `uef render job <job.yaml>`:先支持 `passes: [beauty_lit]` 单通道,orbit N 视角(等距方位角 × 指定仰角),
  走 T1.2 验证过的执行路径;输出 `out/renders/<job_id>/<asset>/<pass>/frame_%04d.png` + manifest v2(含 job 全文、每帧校验值);
- [ ] 场景仍用冒烟的 Cube 场景充当"资产占位"(真资产等 M2 ingest;JobSpec 的 assets 字段先允许 `builtin:cube`)。
- **DoD**:`uef render job examples/orbit8.yaml` 渲出 8 视角 beauty;非法 YAML 有可读报错;确定性两跑一致。

### T1.4 多通道 passes `#render`(M1 核心交付)
- [ ] 在 MRQ 上实现:`depth` / `normal` / `basecolor`(GBuffer 通道)+ `object_mask`(custom stencil)+
  `beauty_unlit`(unlit 视图模式);depth/mask 用 16bit(EXR 或 png16,选型记 WORKLOG);
- [ ] **通道级防假成功断言**(校验器扩展):depth 必须有梯度(非常数)、normal 均值应偏蓝(切线空间外观合理性)、
  mask 的唯一值数量 = 场景物体数 + 1、unlit 与 lit 不得逐像素相同;各断言配反例单测;
- [ ] manifest 记录每通道格式/位深/校验值。
- **DoD**:同一 job 渲出 lit/unlit/depth/normal/basecolor/mask 六通道 × 8 视角,全部过通道级校验;确定性保持。

### T1.5 光照预设 `#render`
- [ ] `lighting.preset ∈ {hdri, three_point, none}`:hdri 需样例 HDRI(脚本从 PolyHaven 下载 1–2 张 CC0 到
  `data/hdri/`,不入 git,下载器即 M3 acquire 的最小雏形);three_point 为参数化三点光;none 为全黑底(测发光材质);
- [ ] 预设由 UE 侧脚本按 job JSON 构建,幂等(重复构建不叠灯)。
- **DoD**:同场景 × 3 预设 × beauty 通道图,视觉可辨(hdri 有环境反射/背景,none 接近全黑但发光体可见);luma 断言按预设定制。

### T1.6 本地/远程统一执行器 + contact sheet + turntable `#render` `#remote`
- [ ] `Executor(local | remote(host))` 抽象落地(ARCHITECTURE §6):`uef render job x.yaml [--host l40s|4090]`,
  JobSpec 与 UE 侧脚本完全不感知本地/远程;远程沿用作业包推送/tmux/回收/清理机制;
- [ ] contact sheet:每 job 自动生成缩略图拼图 PNG(视角 × 通道网格)+ 简单 index.html;
- [ ] turntable:orbit 帧序列合成 mp4(ffmpeg;doctor 增加 ffmpeg 检测,缺失则 QUESTIONS,不自装);
- [ ] 跨节点一致性初查:同 job 本地 vs l40s vs 4090 的 luma 偏差表记 WORKLOG(容差暂定 ±5%,超出开 M2 前置调查任务)。
- **DoD**:同一 job 在本地 + l40s 跑通且产物结构一致(4090 届时可用则加验);contact sheet + turntable 视频可看(M1 验收演示物)。

### T1.7 收尾
- [ ] LogHttp/在线请求源头禁用(M0 遗留);README「五分钟上手」+ `uef.toml.example` 补全;
- [ ] WORKLOG 汇总 M1 + 给 M2(资产 ingest)的建议;QUESTIONS 清点;
- [ ] 文末 `REVIEW REQUESTED: <branch> <sha>` + 发 REVIEW_REQUESTED 信号。

**任务顺序**:主线严格 T1.2 → T1.3 → T1.4 → T1.5 → T1.6 → T1.7 串行;T1.1 为机会性任务,
在任务间隙做轻量探测,通了再做重活(后台传输不占串行槽)。
每任务完成即在 WORKLOG 登记并可发 INFO 信号,**中途发现 PLAN 未覆盖的决策点一律 BLOCKED + QUESTIONS**。
分支:`feat/m1-render`(从 main 切)。预计 4–6 个工作日(MRQ spike 为主要不确定项)。

## 5. 风险与已知约束

1. **GPU 显存被占**:H100 上有一个常驻进程占 69GiB,渲染只有 ~12GiB 余量。UE 一般够用,但 doctor 必须每次检查;若 OOM,记录现场并在 QUESTIONS 里升级给 Owner(是否协调让出显存)。**禁止 kill 任何不是我们启动的进程。**
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
