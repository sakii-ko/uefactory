# 系统架构(ARCHITECTURE)

> 设计蓝图,随里程碑推进细化。改动需对应 ADR。

## 1. 组件总览

```
                        ┌─────────────────────────────────────────┐
  外部资产源             │                uef CLI                  │
  PolyHaven ─┐          │  doctor / ingest / catalog / render /   │
  Objaverse ─┼─────►    │        acquire / farm                   │
  Sketchfab ─┘          └───────┬───────────────┬─────────────────┘
  本地文件 ──────►               │               │
                        ┌───────▼──────┐  ┌─────▼───────────────┐
                        │  Acquire     │  │  Farm(队列/worker) │
                        │  抓取+许可证  │  │  SQLite 队列        │
                        └───────┬──────┘  └─────┬───────────────┘
                        ┌───────▼──────┐  ┌─────▼───────────────┐
                        │  Ingest      │  │  Render             │
                        │  格式归一 →   │  │  JobSpec(YAML) →    │
                        │  UE 导入      │  │  ue_runner → MRQ    │
                        └───────┬──────┘  └─────┬───────────────┘
                        ┌───────▼─────────────── ▼──────────────┐
                        │  Catalog(SQLite)+ 文件仓(data/)     │
                        │  资产元数据 · 许可证 · 渲染产物索引     │
                        └───────────────┬───────────────────────┘
                                        ▼
                        ┌────────────────────────────────────────┐
                        │  UE 5.5.4 (headless, Vulkan,           │
                        │  -RenderOffscreen)+ UEFBase 工程       │
                        │  Content/Python/uef_*.py 在引擎内执行   │
                        └────────────────────────────────────────┘
```

关键边界:**Python 世界(CLI/管线)与 UE 世界(引擎内 Python)只通过两样东西通信——
命令行参数 + JSON 作业文件(env `UEF_JOB_FILE`),以及输出目录里的产物 + manifest。**
不做常驻 RPC(M5 之前)。

## 2. 数据流(一个资产的生命周期)

1. **acquire**:抓取器下载原始文件到 `data/raw/<source>/<source_id>/`,写入 catalog(状态 `raw`,含 license、来源 URL、校验和)。
2. **ingest**:格式归一(必要时 gltf→fbx 等转换)→ 启动 UE headless 执行导入脚本 → 产出 `/Game/UEF/Ingested/<asset_id>/` 下的 .uasset(存于 UEFBase 工程 Content,不入 git)→ catalog 状态 `imported`,记录三角形数/材质数/包体大小。
3. **render**:JobSpec 展开成 UE 作业 JSON → `ue_runner` 启动引擎 → 引擎内脚本摆场景(资产 + 光照预设 + 相机 rig)→ MRQ 渲染各 pass → 输出 `out/renders/<job_id>/<asset_id>/<pass>/frame_%04d.png` + `manifest.json` → 校验(非全黑、张数齐)→ catalog 记录渲染产物。
4. **farm**:以上 2、3 的批量化:SQLite 队列表,worker 进程领任务、心跳、超时重试。

## 3. JobSpec 草案(M1 定稿)

```yaml
job: render
assets: [chair_001, "tag:furniture"]      # id 或 catalog 查询
camera:
  rig: orbit          # orbit | fixed | random
  views: 8            # 环拍视角数
  elevation_deg: [15, 45]
  fov: 60
  resolution: [1024, 1024]
lighting:
  preset: hdri        # hdri | three_point | unlit | none(纯黑测发光)
  hdri: studio_small_03
passes: [beauty_lit, beauty_unlit, depth, normal, basecolor, object_mask]
output:
  dir: out/renders/{job_id}
  format: png16       # mask/depth 需要 16bit
```

## 4. Catalog schema 草案(M2 定稿)

```sql
CREATE TABLE assets (
  asset_id    TEXT PRIMARY KEY,        -- 全小写下划线
  name        TEXT NOT NULL,
  source      TEXT NOT NULL,           -- local | polyhaven | objaverse | ...
  source_id   TEXT,
  source_url  TEXT,
  license     TEXT NOT NULL,           -- SPDX 风格:CC0-1.0 / CC-BY-4.0;硬约束非空
  status      TEXT NOT NULL,           -- raw | imported | render_ok | failed
  tags        TEXT,                    -- JSON array
  tri_count   INTEGER,
  material_count INTEGER,
  sha256      TEXT,
  created_at  TEXT NOT NULL,           -- UTC ISO
  updated_at  TEXT NOT NULL
);
CREATE TABLE artifacts (               -- 渲染/缩略图等产物索引
  artifact_id TEXT PRIMARY KEY,
  asset_id    TEXT REFERENCES assets(asset_id),
  kind        TEXT NOT NULL,           -- thumbnail | render_pass | contact_sheet
  path        TEXT NOT NULL,
  params_json TEXT,                    -- 生成参数(可复现)
  created_at  TEXT NOT NULL
);
```

## 5. 渲染 pass 的实现思路(M1 细化)

| pass | 方案 |
|---|---|
| beauty_lit | MRQ Deferred Rendering 默认输出 |
| beauty_unlit | viewmode unlit(MRQ Additional Post Process / ShowFlag.Lighting=0) |
| depth / normal / basecolor | MRQ 的 GBuffer 通道(Deferred Rendering 的 Additional Render Passes) |
| object_mask | Custom Stencil:导入时给每资产分配 stencil id,MRQ Stencil Layer 输出 |

已知备选:若 MRQ 通道在 headless 下有坑,退路是 SceneCapture2D + 后处理材质(记 ADR 再切换)。

## 6. 远程渲染节点(ADR-003)

```
 本机(数据主库:catalog + data/ + out/)            远端节点(算力 + 暂存)
 ┌─────────────────────────────┐                 ┌──────────────────────────┐
 │ uef render --host 4090      │  rsync push     │ work_dir/(.uef_node 哨兵)│
 │  └ RemoteHost(core/remote)  │ ──────────────► │  ├ engine/(一次 provision)│
 │     · ControlMaster 复用     │  tmux 派发      │  ├ jobs/<job_id>/        │
 │     · 状态轮询 ≥30s          │ ──────────────► │  │   job.json / status.json│
 │                             │  rsync pull     │  │   out/(渲染输出)      │
 │ out/renders/<job_id>/ ◄──── │ ◄────────────── │  └ (渲后清理 jobs/)      │
 └─────────────────────────────┘                 └──────────────────────────┘
```

- 节点 profile 在 `uef.toml [hosts.<name>]`:ssh_alias、work_dir、engine_dir、gpu 数、暂存配额。
- 渲染执行器只有一个抽象:`Executor(local | remote(host))`,JobSpec 与 UE 侧脚本完全不感知本地/远程差异——差异全部封装在"作业包推送 / 引擎路径 / 产物回收"三处。
- 远端渲染输出永远写节点本地(或其自有存储),完成后一次性 rsync 回本机;禁止跨 WAN 写帧序列。

## 7. 性能与资源纪律

- 引擎冷启动贵(秒~分钟级):farm 的 worker 按"一次引擎启动处理一批资产"设计(引擎内循环),而不是每资产一进程;但 M1 先做正确,再做快。
- DDC 共享:所有 worker 用同一 DDC 路径,避免重复编 shader。
- 显存:doctor 渲前检查;worker 启动错峰(串行 warmup)。
- NAS:大量小文件写入(帧序列)先落本地 scratch 再 mv 到 NAS(若找到本地盘;M0 doctor 的结论决定)。
