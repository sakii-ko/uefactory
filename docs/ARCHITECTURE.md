# 系统架构（ARCHITECTURE）

> 当前实现基线，而非未来功能清单。改变关键边界需对应 ADR。

## 1. 组件与信任边界

```text
 Khronos / Poly Haven ── acquire models ──► data/m2_samples/
                                                   │
 本地 FBX / glTF / GLB ── strict IngestSpec ───────┤
                                                   ▼
                                        safe staging + hashes
                                        data/raw/local/<asset_id>/
                                                   │
                                                   ▼
 uef CLI ── JSON job + UEF_JOB_FILE ──► UE 5.5.4 / AssetImportTask
                                                   │
                          ┌────────────────────────┼─────────────────────┐
                          ▼                        ▼                     ▼
                 /Game/UEF/Ingested/       out/ manifests        SQLite catalog v3
                          │                        │                     │
                          └──────── catalog asset/scene resolver ───────┘
                                                   │
                                                   ▼
                                      MRQ headless multi-pass render
                                                   │
                                                   ▼
                                      out/ frames + visual reports

 BlackMyth/external library ── read-only scan ── SceneSpec(root_env + relative path)
                                                   │
                                                   ▼
                                      persistent multi-object UE level
                                      /Game/UEF/Scenes/<scene_id>/
```

Python 宿主与 UE 内置 Python 不共享进程内状态。稳定通信面只有：

1. 命令行参数和环境变量 `UEF_JOB_FILE` 指向的严格 JSON job；
2. UE package、日志和原子写入的 JSON manifest；
3. 宿主对 manifest、package、图像和 hash 的二次验真。

M5 之前不引入常驻 RPC。`farm`/worker 是 M4 的计划能力，不应被视为当前 ingest batch 的
执行模型；当前 batch 在宿主侧逐资产执行，并为每个资产启动相互独立的 UE 验证进程。

## 2. 模型资产生命周期

### 2.1 获取与清单

`uef acquire models` 只接受代码中固定的 HTTPS host、URL、字节数和 SHA-256，许可不在批准的
开放集合时 fail closed。文件先写临时路径，精确校验后原子替换，最终形成
`data/m2_samples/<asset_id>/` 和总清单 `data/m2_samples/inventory.json`。

M2 固定样本是 11 个模型（6 GLB、5 FBX），共 34 个下载文件、60,003,947 bytes。10 个带
纹理样本使用 CC0-1.0；`khronos_box` 是 CC-BY-4.0，要求保留 Cesium/Khronos attribution。

### 2.2 安全 staging 与 TOCTOU

`uef ingest batch` 先严格解析 IngestSpec，再把主文件和精确声明的依赖复制到
`data/raw/local/<asset_id>/`：

- 禁止 symlink、父目录逃逸、绝对依赖、远程/data URI 和未声明的 glTF 外部依赖；
- `bundle_sha256` 对规范相对路径、长度和文件内容敏感；
- `content_sha256` 对文件内容 multiset 敏感，不把改名误判成不同内容；
- 复制前后核对两个 hash 和源结构证据，临时目录完整后才原子 rename；
- UE import、独立 reload 和 finalize 各阶段前后再次核对 source/bundle/content hash，源字节或
  结构在运行中变化即中止并回滚。

这条链路保留输入字节，不先把 glTF 转换成 FBX，也不把“格式归一化”写成一个并不存在的
宿主转换步骤。

### 2.3 源结构证据与输出语义

glTF/GLB 有规范 JSON scene graph，因此宿主在导入前记录 canonical source evidence：scene
root、node/child edge、mesh reference、最大深度以及每个 node 的 matrix 或 TRS local
transform。非法索引、重复 parent、cycle、非有限 transform、matrix 与 TRS 并存、不可分解的
matrix 等情况 fail closed；证据使用 domain-separated canonical SHA-256。

M2 v1 没有独立 FBX scene-graph parser。FBX 的 `source_structure.status` 必须明确为
`not_available`，policy 是 `fbx_not_available_delegated_to_unreal_importer_v1`；系统不会伪造
node 数或 transform 作为“已观察”证据。

M2 逻辑资产输出契约是恰好一个 StaticMesh：

```text
ue_output_policy       = flatten_to_single_static_mesh_v1
ue_hierarchy_preserved = false
```

源 graph/transform 证据用于 provenance 与重跑门禁，不等于 UE package 保留了源 hierarchy。
`khronos_box` 是当前 hierarchical/untextured 反例：源图为 2 nodes、1 root、1 child edge、
max depth 2、1 mesh reference 和 1 non-identity matrix；导入结果是一个 12-triangle StaticMesh，
`texture_count=0`。

### 2.4 UE 导入与多进程 package 事务

UE 侧唯一导入入口是 `unreal.AssetImportTask` +
`AssetToolsHelpers.get_asset_tools().import_asset_tasks()`，manifest 的稳定 backend 标识为
`asset_tools_auto`。输入格式的 unit/up-axis/handedness 转换委托给引擎 importer；M2 fresh
acceptance 的 11 份 UE 5.5.4 日志实际都走 `LogInterchangeEngine`，但实现不把某个具体
Interchange pipeline 类写死为 API 契约。

导入不是直接覆盖最终目录，而是可恢复的多 UE 进程事务：

```text
primary process
  import -> /Game/UEF/IngestTransactions/<asset_id>/candidate
  validate/save candidate
  old destination -> backup (if present)
  candidate -> /Game/UEF/Ingested/<asset_id>
  state = pending_host_validation
         │
         ▼
host quality gate (m2_static_mesh_v2)
         │
         ▼
independent reload process
  reload package and require exact object/mesh/material/texture payload
         │
         ▼
host package evidence
  recursively hash the complete destination tree
  bind sorted repo-relative path + size + SHA-256 into one bundle digest
         │
         ▼
independent finalize process
  recheck host-approved payload, delete transaction/backup, state = committed
  (does not resave the approved destination)
         │
         ▼
post-commit host validation
  rehash the complete package tree and require byte-for-byte equality
```

任何 reversible 阶段失败都会恢复 backup 或删除新 destination。finalize 支持一次独立重试；
若结果跨过不可逆 commit 点仍不明确，则再启 UE inspect process，以 package payload 和 transaction
目录状态判定 `pre_commit`、`committed` 或 `in_doubt`，不会猜测成功。不可逆 commit 后若 package
字节复验不一致，宿主会写 durable `failed` manifest 并明确记录 `committed/no rollback`，绝不把
已经无法安全回滚的状态伪装成成功。

`ue_package_bundle` 使用 `ue_ingested_package_bundle_v1`：递归覆盖
`ue/UEFBase/Content/UEF/Ingested/<asset_id>/` 下全部常规文件，每项记录排序后的 repo-relative
POSIX path、正整数 size 与文件 SHA-256，再以 domain-separated canonical JSON 计算
`package_bundle_sha256`。收集器拒绝 symlink、路径逃逸、空/非 regular 文件，要求每个 imported
object 的 `.uasset` 都在闭包中，并以两次目录扫描和两次文件 hash 检测采集期间的增删改。import
artifact 保存完整闭包；thumbnail generation 保存闭包 digest；skip、model render 和 catalog commit
都会重算当前磁盘字节，不能靠旧 manifest 自证。

同一个 `asset_id` 的 staging、import/reload/finalize、catalog 更新、thumbnail 和 model render 由
`data/locks/assets/<asset_id>.lock` 的跨进程 `flock` 串行化。锁在 owning thread 内可重入，但其他
thread/process 只会得到明确 busy；busy 结果不得改写 catalog。fork child 会丢弃继承的 registry
与 file handle，避免把父进程 guard 或 lease 误当成自己的锁。builtin 不持 generation lock；
scene render 使用下文独立的 scene lease，不进入 model asset lock。

### 2.5 “归一化”的实际边界

不要把下面三层混为一谈：

- package import：source conversion 委托引擎 importer，package pivot 保留，package
  `uniform_scale=1.0`；
- IngestSpec request：`source_*: auto` 与 `uniform_scale` 被记录和校验，但不重写 package 几何；
- catalog render：在临时 render actor 上应用可配置 uniform scale，用导入 bounds 将对象的
  bottom-center 平移到原点，并按缩放后 bounds/FOV 自动计算 camera radius 与 floor 尺寸。

因此当前实现保证 package preservation 加可复现取景，不声称已经完成通用的模型单位、pivot 或
hierarchy 烘焙归一化。

## 3. 导入质量与产物契约

成功的模型导入必须同时满足：

- import manifest `schema_version=2`；
- catalog `import_manifest` artifact params `schema_version=2`；
- `quality.ruleset_version=m2_static_mesh_v2` 且全部 checks 为 `passed`；
- 单 StaticMesh、LOD/triangle/vertex 均为正；
- bounds 有限、有序、size 与 max-min 一致、至少两个非退化轴、缩放后最大 extent 合法；
- 每个 material slot 有稳定 material reference；
- tagged `textured` 资产有正 texture count 与可解析的 used-texture references；未标记 textured
  的 Box 明确允许 `texture_count=0`；
- source-structure payload 与 canonical digest 精确匹配，且不得声称 hierarchy preserved；
- primary、独立 reload 和 finalize 的完整 asset payload 一致，transaction 为 `committed`；
- 完整 package tree 的 path/size/file SHA-256 与 bundle digest 在 finalize 前后精确一致；
- UE 日志的有效 warning/error 计数为零；已分类的 directory-watcher 等噪声单独记录。

FBX 在引擎导入后按 `fbx_filename_pbr_v2` 连接 base color、normal、roughness、metallic，记录
OpenGL normal green-channel flip 和 glass override 证据。glTF/GLB 材质交给引擎 importer，
不套用 FBX 文件名后处理。

batch manifest 自身目前是 schema v1；它聚合每个资产的 import/thumbnail manifest 路径和
状态。这里的 schema v2 特指 durable import manifest 与对应 catalog artifact contract。

## 4. 缩略图、报告与幂等重跑

`ingest batch` 默认启用 catalog thumbnail：8 个 orbit 视角、512×512、three-point lighting，
输出 `beauty_lit` PNG 与 `object_mask` half-float EXR。校验器要求 mask 中有 bounded subject、
beauty 黑背景与 stencil 一致、主体占比达到阈值，再选择主体面积最大的视角生成
`thumbnail.png` 和 `subject_mask.png`。

每个 `render_ok` 模型在 catalog 中有一个 import manifest artifact 和五个同 generation
thumbnail artifacts：beauty、PNG mask、raw EXR mask、render manifest、contact sheet。
batch 完成后从 hash-valid artifact group 生成总 contact sheet、每资产 sheet 和离线
`index.html`；即使 all-skip 重跑，也会从当前 catalog 证据重新生成本批报告。

只有以下证据全都精确有效时才返回 `skipped`：staged hashes、当前 source structure、schema v2
import manifest/artifact、当前 quality policy/ruleset、实际 UE package inventory，以及完整且
hash-valid 的 thumbnail artifact generation。catalog 中一个 `render_ok` 字符串本身不足以跳过。

2026-07-10 fresh acceptance 证据：

- `out/ingest_batches/20260710T145337Z_d8eef9c2/manifest.json`：11/11 `render_ok`；
- 同批 `report/contact_sheet.png` 与 `report/index.html`：11 个模型视觉报告；
- `out/ingest_batches/20260710T152241Z_4eaa416b/manifest.json`：立即重跑 11/11 `skipped`，
  没有启动 UE；
- `data/catalog_m2_package_release.db`：11 个 `render_ok`、66 artifacts，SQLite integrity/FK clean；
- 11 个 package roots 的完整闭包为 64 files / 68,910,435 bytes，fresh manifest、catalog artifact
  与当前磁盘重算结果三方相同；
- `out/ingest/20260710T152035Z_ed073d49/khronos_box/manifest.json`：Box schema v2、
  source graph digest、0 source textures、quality v2、committed transaction 与最终 package bytes。

## 5. Catalog schema v3

Catalog 使用标准库 SQLite、每操作一个连接，启用 `foreign_keys=ON`、WAL 和 busy timeout。
迁移与写操作使用 `BEGIN IMMEDIATE`；schema 升级按 `PRAGMA user_version` 串行，失败完整 rollback。

当前五张业务表：

| 表 | 责任 |
|---|---|
| `assets` | 模型 provenance、license/tier、raw/package 路径、hash、状态和 mesh 统计 |
| `artifacts` | import/thumbnail/render 产物的 repo-relative path、params JSON、SHA-256 |
| `scenes` | SceneSpec/source/build generation、persistent map、总 inventory/bounds、状态 |
| `scene_objects` | build generation 中逐 actor 的 class、mesh、transform、bounds 和统计 |
| `scene_artifacts` | scene build/reload/finalize/thumbnail 同 generation 的带 hash 产物 |

模型状态是 `raw | imported | render_ok | failed`；场景状态是
`raw | built | render_ok | failed | quarantined`。license、tier、状态组合、lower_snake_case id、
repo-relative path、UE package namespace、hash 格式和状态所需字段均同时受 Python 校验与 SQL
CHECK/FK 约束。`finalize_import`、`finalize_render` 与 scene generation replacement 都在单个
SQLite write transaction 内更新 parent record 和 artifact set，避免半提交 catalog。

## 6. Persistent scene level

`uef acquire blackmyth <root>` 只读扫描 `asset-library/manifests` 与 self-contained derived GLB，
拒绝 symlink/path escape，按 manifest license 分类 open、NC 与 quarantined；它不会修改或复制
外部库。SceneSpec 的 `source.path` 在设置 `root_env` 时必须是规范相对路径，例如：

```yaml
source:
  path: asset-library/derived/f3266f252ea98fcc/f3266f252ea98fcc.glb
  root_env: UEF_BLACKMYTH_ROOT
```

运行时显式设置 `UEF_BLACKMYTH_ROOT=/home/chijw/workspace/projs/blackmyth`。resolver 要求实际文件
位于该 canonical root 内，并在 build 前后校验 source SHA-256。`scene build` 通过 UE scene import
生成 `/Game/UEF/Scenes/<scene_id>/L_<scene_id>` 持久 level，独立进程 reload/finalize 后记录 exact
package bundle、actor/component inventory、逐 mesh transform/bounds 与 build generation digest。

`scene thumbnail` 只接受当前 generation 的完整 build artifact group 和未变化的 source/map；
它直接加载 persistent level，不把 scene 合成一个临时 model，不自动添加 floor。每个静态网格
component 分配稳定 stencil，camera target/radius 来自 scene world bounds，最终将 build 与五个
render artifacts 原子更新到同一 scene generation。

scene package evidence 递归覆盖
`ue/UEFBase/Content/UEF/Scenes/<scene_id>/` 的完整 regular-file tree。inventory 中每个 object 的
`.uasset/.umap` 是必需子集；同 basename 的 `.uexp/.ubulk/.uptnl` sidecar 也进入证据，其他未知
文件、缺失/空/非 regular 文件、file/dir symlink 与路径逃逸均 fail closed。收集器执行两次目录
扫描和两次安全文件 hash；reload 后的 approved evidence 必须与 finalize 不可逆提交后、catalog
commit 前的重算结果完全相同。standalone `render job scene:<id>` 也会在 resolver 前取得
`data/locks/scenes/<scene_id>.lock` 并持有到 UE setup/render 与 host artifacts 完成，避免验证旧
generation 却加载新 package。scene lease 只允许 owning thread 重入，其他 thread/process busy；
fork child 清空继承 registry/handle 后重新竞争。

当前开放许可验收集有 8 个 level；research-only/NC SceneSpec 不属于开放集，不能因文件可读取
就改写许可层级。

## 7. Render JobSpec 与 pass 契约

一个 JobSpec 当前只接受一个逻辑目标：`builtin:cube`、catalog `asset_id` 或
`scene:<scene_id>`。

```yaml
job: render
assets: [khronos_avocado]
camera:
  rig: orbit
  views: 8
  elevation_deg: 20
  fov: 45
  resolution: [512, 512]
lighting:
  preset: three_point
passes: [beauty_lit, beauty_unlit, depth, normal, basecolor, object_mask]
output:
  dir: out/renders
```

物理输出契约：beauty/unlit/normal/basecolor 是 8-bit RGB PNG；depth/object_mask 是 half-float
RGBA EXR（统计语义取第一通道）。render manifest schema v3 记录实际格式、分辨率、每帧统计与
canonical decoded pixel SHA-256；`--verify-twice` 比较完整稳定载荷，不比较近似 luma。

| pass | 语义 |
|---|---|
| `beauty_lit` | MRQ Deferred Rendering 的有光照输出 |
| `beauty_unlit` | 关闭 lighting 的材质颜色输出 |
| `depth` | WorldDepth half-float EXR，必须有有效前后景梯度 |
| `normal` | 世界空间 normal 编码，orbit 时 R/G 随视角变化 |
| `basecolor` | 材质 base color RGB PNG |
| `object_mask` | `CustomStencil / 255` 标量 half-float EXR |

HDRI beauty 与 data pass 使用两个 LevelSequence：HDRIBackdrop 只存在于 beauty sequence，
不会写入 depth/normal/basecolor/object_mask。三点光是固定顺序和参数的 persistent level actor。
完整确定性与反例门禁见 [ADR-004](adr/004-render-data-contract-and-determinism.md)。

## 8. 远程节点边界

本机 NAS 是 catalog、raw、package 输入和最终输出的单一主库；4090/l40s 节点只提供算力与
可丢弃暂存。remote executor 通过 ControlMaster 复用 SSH，将最小作业包 rsync 到有
`.uef_node` 哨兵的工作目录，在 tmux 中运行并按状态 JSON 轮询，完成后拉回产物。远端同名
路径不代表共享文件系统，任何清理必须先验证哨兵和进程身份。详见
[ADR-003](adr/003-remote-render-nodes.md)。

## 9. 性能与资源纪律

- UE 冷启动昂贵，但 M2 的 primary/reload/finalize 进程隔离是 package durability 证据，不能为
  省启动时间合并；未来 farm 可在不削弱该事务边界的前提下优化调度。
- 所有 UE 进程使用共享 DDC 和 `-ddc=InstalledNoZenLocalFallback`，避免 Zen 服务生命周期噪声。
- `doctor` 在执行前检查 UE、GPU/Vulkan、磁盘空间和写速；WARN 必须保留，不能伪装成 OK。
- `TMPDIR`、DDC、下载和输出放 approved bulk storage；不默认使用 `/root/nas/fastdata2`。
