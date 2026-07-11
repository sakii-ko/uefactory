# UEFactory

UE 数据制造农场：在 Linux headless 环境中获取和编目可追溯资产，通过 UE 5.5.4
导入真实 FBX/glTF/GLB，并批量生成 lit、unlit、depth、normal、basecolor、object mask
等可验证渲染结果。目标形态类似 UnrealZoo。

## 从这里开始读

1. [`PLAN.md`](PLAN.md) —— 项目愿景、里程碑和当前 Sprint
2. [`docs/CONVENTIONS.md`](docs/CONVENTIONS.md) —— 代码、git、日志和测试规范
3. [`docs/ENVIRONMENT.md`](docs/ENVIRONMENT.md) —— 本机 UE、GPU 和存储事实
4. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) —— 当前系统设计与数据契约
5. [`docs/WORKLOG.md`](docs/WORKLOG.md) —— 可复现执行记录
6. [`docs/QUESTIONS.md`](docs/QUESTIONS.md) —— 需要 Owner 决策的问题
7. [`docs/adr/`](docs/adr/) 和 [`docs/reviews/`](docs/reviews/) —— 决策与正式 review

## 五分钟上手

以下命令假设位于 repo 根目录，并使用已经就位的 UE 5.5.4 Linux 预编译引擎。
运行时临时文件统一放在项目的 bulk-data 目录，避免使用系统临时盘。

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp uef.toml.example uef.toml
mkdir -p data/tmp
```

编辑 `uef.toml` 后先做本机健康检查。快速自检可临时降低写入测试规模：

```bash
TMPDIR=$PWD/data/tmp UEF_DOCTOR_WRITE_TEST_MIB=8 .venv/bin/uef doctor
```

本地渲染一个 8 视角、6 通道的内置 cube job：

```bash
TMPDIR=$PWD/data/tmp .venv/bin/uef render job examples/orbit8.yaml --timeout-sec 1800
```

若要验证 HDRI 光照，先通过统一 resource sync 获取、CPU 验证并原子发布一个 CC0 小样例，
再跑 HDRI job：

```bash
TMPDIR=$PWD/data/tmp .venv/bin/uef acquire polyhaven-resources \
  --kind hdri --source-id studio_small_03 --resolution 1k \
  --database data/catalog.db --json
TMPDIR=$PWD/data/tmp .venv/bin/uef render job \
  examples/orbit8_hdri.yaml --timeout-sec 1800
```

`uef acquire hdri` 仍保留为单 HDRI 兼容入口；新流程应优先使用
`uef acquire polyhaven-resources`，因为它同时维护 revisioned source bytes、validation evidence、
failure journal、run receipt 与 schema v5 catalog cohort。

远程 l40s 使用相同 JobSpec：

```bash
TMPDIR=$PWD/data/tmp UEF_DOCTOR_WRITE_TEST_MIB=8 .venv/bin/uef doctor --host l40s
TMPDIR=$PWD/data/tmp .venv/bin/uef render job \
  examples/orbit8.yaml --host l40s --timeout-sec 2400
```

成功后查看 `out/renders/<run_id>/builtin_cube/`：各 pass 下是 `frame_*.png` 或
`frame_*.exr`，根目录包含 `manifest.json`、`contact_sheet.png`、`turntable.mp4` 和
`index.html`。

阶段性最高质量结果先用专用 JobSpec 生成高分辨率 beauty/mask cohort，再从经过验真的 render run
生成独立 MP4 归档：

```bash
UEF_BLACKMYTH_ROOT=/home/chijw/workspace/projs/blackmyth \
TMPDIR=$PWD/data/tmp .venv/bin/uef render job \
  examples/showcases/scene_player_home_1080p.yaml \
  --database data/catalog.db --timeout-sec 3600
TMPDIR=$PWD/data/tmp .venv/bin/uef render showcase \
  out/showcase_source_renders/<run_id>/scene_bm_player_home \
  --stage m3_t3_1b
```

该命令只接受成功的 schema v3 render manifest、连续且 hash 匹配的至少 72 帧
`beauty_lit` PNG、逐帧对应的 `object_mask` EXR，以及短边至少 1080 的偶数分辨率。单主体每帧
foreground 必须 >=10%、bbox area >=18%、最小边缘余量 >=3%；静止、过小或裁切的 cohort 会在编码前
拒绝。它会在
`out/showcases/<stage>/<timestamp>_<asset>/` 原子写入 H.264/yuv420p/24 fps/faststart MP4 和
包含归档 source manifest、beauty/mask 逐帧证据、license、git 与 ffprobe/hash 的 `manifest.json`。

## 导入 M2 模型集

固定清单 [`examples/m2_assets.yaml`](examples/m2_assets.yaml) 包含 11 个开放许可模型：
6 个 GLB 和 5 个 FBX。获取器先按固定 URL、字节数和 SHA-256 验证 34 个模型/依赖文件，
再写入 `data/m2_samples/` 与 `data/m2_samples/inventory.json`。

```bash
TMPDIR=$PWD/data/tmp .venv/bin/uef acquire models --json
TMPDIR=$PWD/data/tmp .venv/bin/uef ingest batch examples/m2_assets.yaml \
  --database data/catalog_m2.db --timeout-sec 1800 --json
```

`ingest batch` 默认同时渲染每个资产的 8 视角 beauty/mask 缩略图，并生成：

- `out/ingest_batches/<run_id>/manifest.json`
- `out/ingest_batches/<run_id>/report/contact_sheet.png`
- `out/ingest_batches/<run_id>/report/index.html`
- `out/ingest_batches/<run_id>/report/asset_sheets/<asset_id>.png`

紧接着原样重跑同一命令应得到 11 个 `skipped`。跳过不是只看 catalog 状态：bundle/content
哈希、manifest/artifact v2、`m2_static_mesh_v2` 质量证据、源结构证据、完整 UE package
path/size/file SHA-256 闭包和缩略图产物组都必须仍然精确匹配；任一证据过期或被篡改都会重新
导入或重新渲染。同一 `asset_id` 的导入、catalog、缩略图与模型渲染还会由跨进程 lease
串行化，冲突任务不会覆盖已成功 generation。若只想调试导入，可显式加 `--no-thumbnails`。

常用 catalog 查询：

```bash
TMPDIR=$PWD/data/tmp .venv/bin/uef catalog stats --database data/catalog_m2.db --json
TMPDIR=$PWD/data/tmp .venv/bin/uef catalog list \
  --database data/catalog_m2.db --status render_ok
TMPDIR=$PWD/data/tmp .venv/bin/uef catalog show khronos_box \
  --database data/catalog_m2.db --json
```

2026-07-10 的最终 M2 fresh acceptance 位于
`out/ingest_batches/20260710T145337Z_d8eef9c2/manifest.json`，已验证 11/11 `render_ok`；立即重跑
`out/ingest_batches/20260710T152241Z_4eaa416b/manifest.json` 为 11/11 `skipped` 且没有启动 UE。
`data/catalog_m2_package_release.db` 为 11 assets / 66 artifacts，11 个 UE package roots 的完整
字节闭包为 64 files / 68,910,435 bytes。获取清单为 34 个文件、60,003,947 bytes。11 份 UE
5.5.4 fresh import 日志都记录了实际 `LogInterchangeEngine` 导入，但稳定入口契约仍是
`AssetImportTask` 自动选择引擎 importer，而不是把 Interchange 写死为宿主 API。无纹理的
CC-BY-4.0 `khronos_box` 记录
`texture_count=0`，其 GLB 源图有 2 个 node、1 条 child edge、深度 2 和 1 个非 identity
local transform；UE v1 输出明确是单 StaticMesh 扁平化，未声称保存源 hierarchy。

## 构建外部场景 level

BlackMyth 兼容层只读扫描外部库，不复制或修改源目录；SceneSpec 用 `source.root_env` 加相对
路径绑定机器上的 approved root。先显式设置根目录，再扫描、校验、构建和渲染：

```bash
export UEF_BLACKMYTH_ROOT=/home/chijw/workspace/projs/blackmyth

TMPDIR=$PWD/data/tmp .venv/bin/uef acquire blackmyth \
  "$UEF_BLACKMYTH_ROOT" --json
TMPDIR=$PWD/data/tmp .venv/bin/uef scene validate \
  examples/scenes/bm_fantasy_diorama.yaml --json
TMPDIR=$PWD/data/tmp .venv/bin/uef scene build \
  examples/scenes/bm_fantasy_diorama.yaml --database data/catalog.db \
  --timeout-sec 1800 --json
TMPDIR=$PWD/data/tmp .venv/bin/uef scene thumbnail bm_fantasy_diorama \
  --database data/catalog.db --timeout-sec 1800 --json
```

当前 8 个开放许可 level 样例位于 [`examples/scenes/`](examples/scenes/)：fantasy diorama、
player home、cake house、old church ruins、thunderclap temple、两个 Zelda/Tilt Brush 场景和
RPG low-poly arena。`bm_lys_piandian_research.yaml` 是明确的 research-only/NC 样例，不属于这
8 个开放许可验收场景。构建生成持久 map 与逐 actor inventory，缩略图流程使用 scene bounds
取景、禁止自动 floor，并将 build 与 render 产物按同一 generation 写入 schema v5 catalog。

## 同步 Poly Haven HDRI/PBR resources

HDRI 与 PBR texture set 是独立 catalog resource，不伪装成 StaticMesh asset。下面两条命令分别
同步当前已验收的 Studio Small 03 HDRI 与 Aerial Asphalt 01 PBR cohort：

```bash
TMPDIR=$PWD/data/tmp .venv/bin/uef acquire polyhaven-resources \
  --kind hdri --source-id studio_small_03 --resolution 1k \
  --database data/catalog.db --json
TMPDIR=$PWD/data/tmp .venv/bin/uef acquire polyhaven-resources \
  --kind pbr_texture_set --source-id aerial_asphalt_01 --resolution 1k \
  --database data/catalog.db --json
```

查询完整 resource cohort 与统计：

```bash
TMPDIR=$PWD/data/tmp .venv/bin/uef catalog resource-stats \
  --database data/catalog.db --json
TMPDIR=$PWD/data/tmp .venv/bin/uef catalog resources \
  --database data/catalog.db --status ready --license-tier open
TMPDIR=$PWD/data/tmp .venv/bin/uef catalog resource-show \
  polyhaven_pbr_aerial_asphalt_01_dd67d209d4cd82d8275e9032b5ce648a \
  --database data/catalog.db --json
```

resource id 绑定 provider source revision、profile 与 resolution；provider revision 变化会得到新 id，
不会覆盖已经发布的 source bytes 或 evidence。PBR `ue_pbr_png_v1` 当前固定为 sRGB Diffuse、
DirectX normal 与 data-space ARM（R=AO、G=roughness、B=metallic）。

## 数据与存储纪律

`out/`、`data/`、`logs/` 和 UE 生成的 package 都是 gitignored 运行产物。默认将下载资产、
raw staging、catalog、DDC 和输出放在 `/root/nas/bigdata1` 下。不要把引擎、DDC、渲染输出或
资产缓存等大目录放到 `/root/nas/fastdata2`，除非 Owner 明确批准。

## 工作方式

- 执行代理直接负责计划、实现、测试、真实 UE 运行、可视化审阅、review 与里程碑收口。
- Owner 定义终极目标，只在权限、外部资源或会实质改变目标的开放性选择上介入。
- 计划、证据与决策分别沉淀在 `PLAN.md`、`docs/WORKLOG.md`、`docs/reviews/` 和
  `docs/adr/`；任何任务仍须满足 `docs/CONVENTIONS.md` 的 DoD。
