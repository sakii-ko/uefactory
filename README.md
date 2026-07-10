# UEFactory

UE 数据制造农场:Linux headless 环境下,持续收集 UE 可用资产,并通过 CLI(`uef`)
批量渲染所需内容(lit / unlit / depth / normal / mask 等)。目标形态类似 UnrealZoo。

## 从这里开始读(顺序)

1. `PLAN.md` —— 项目愿景、里程碑、**当前 Sprint 任务清单**
2. `docs/CONVENTIONS.md` —— 代码 / git / 日志 / 测试规范(强制)
3. `docs/ENVIRONMENT.md` —— 本机环境事实(UE 路径、GPU、存储)
4. `docs/ARCHITECTURE.md` —— 系统设计
5. `docs/WORKLOG.md` —— 实际执行记录(追加式)
6. `docs/QUESTIONS.md` —— 决策请求通道
7. `docs/adr/` —— 架构决策记录;`docs/reviews/` —— review 报告

## 五分钟上手

以下命令假设你在 repo 根目录,并使用已有的 UE 5.5.4 Linux 预编译引擎。

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp uef.toml.example uef.toml
```

编辑 `uef.toml` 后先跑本机健康检查。快速自检可临时降低写入测试规模:

```bash
UEF_DOCTOR_WRITE_TEST_MIB=8 .venv/bin/uef doctor
```

本地渲染一个 8 视角、6 通道的内置 cube job:

```bash
.venv/bin/uef render job examples/orbit8.yaml --timeout-sec 1800
```

若要验证 HDRI 光照,先下载一个 CC0 小样例,再跑 HDRI job:

```bash
.venv/bin/uef acquire hdri --asset-id studio_small_03 --resolution 1k
.venv/bin/uef render job examples/orbit8_hdri.yaml --timeout-sec 1800
```

远程 l40s 渲染使用同一个 JobSpec:

```bash
UEF_DOCTOR_WRITE_TEST_MIB=8 .venv/bin/uef doctor --host l40s
.venv/bin/uef render job examples/orbit8.yaml --host l40s --timeout-sec 2400
```

成功后查看 `out/renders/<run_id>/builtin_cube/`:每个 pass 下有 `frame_*.png` 或
`frame_*.exr`,根目录有 `manifest.json`、`contact_sheet.png`、`turntable.mp4` 和
`index.html`。`out/`、`data/`、`logs/` 都是 gitignored 运行产物目录。

存储约束:默认把数据、DDC、输出放在本机 `/root/nas/bigdata1` 下。不要把引擎、DDC、
渲染输出、资产缓存等大目录放到 `/root/nas/fastdata2`;那块盘只允许细碎小数据或 Owner
明确批准的用途。

## 工作方式

- **执行代理**(Codex):直接负责计划、实现、测试、真实运行、可视化审阅、review 与里程碑收口,
  不再做 Planner → Coder/Executor 交接。
- **Owner**(用户):定义终极目标;只在权限、外部资源或会实质改变目标的开放性选择上介入。
- 计划、证据与决策分别沉淀在 `PLAN.md`、`docs/WORKLOG.md`、`docs/reviews/` 和
  `docs/adr/`;任何任务仍须满足 `docs/CONVENTIONS.md` 的 DoD。
