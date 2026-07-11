# Formal Slice Review:M3 PolyHaven HDRI/PBR resources

- 结论:**APPROVE（catalog/resource scope）**
- 日期:2026-07-11
- 实现分支:`feat/m3-acquire`
- 范围:Poly Haven official HDRI/texture listing → revisioned source bytes → strict CPU validation →
  schema v5 typed resource cohort → durable replay；并记录本阶段 1440² scene showcase evidence。
- 边界:当前 dirty-tree provisional MP4 因主体过小已被后续 object-mask framing gate 明确拒绝，不作为
  本报告对 catalog/resource 代码给出 `APPROVE` 的依据；最终 clean-tree scene archive 单独追加证据。

## Reviewer 结论

1. Catalog/schema 审查
   - 最终结论:`APPROVE`，BLOCKER/MAJOR/MINOR/NIT=0。
   - broader catalog/resource regression=`107 passed`；最后的 schema v5 migration 与 terminal-control
     hardening targeted suite=`22 passed`；Ruff 与 Mypy 通过。
2. Poly Haven resource sync 审查
   - 最终结论:`APPROVE`，BLOCKER/MAJOR/MINOR/NIT=0。
   - focused sync=`23 passed`，related acquisition/catalog=`107 passed`；Ruff、Mypy 与 diff check 通过。

两路 reviewer 均为只读复核；最终确认的不变量包括 listing-bound identity、source revision、完整
file/artifact cohort、catalog publication lineage、restart/replay、path/symlink/TOCTOU、quota/failure
journal 和 human CLI control-byte escaping。

## 真实 HDRI/PBR replay

### Studio Small 03 HDRI

- run:`out/acquire/polyhaven-resources/hdri/20260711T125901Z_c3984c13/manifest.json`。
- official listing 979 records；本次 exact source-id replay 为 1 request、0 body bytes、1 reused file、
  0 downloads，状态 `ready` / item `skipped`。
- 重新验证 1,686,299 bytes；Radiance HDR 为 1024×512、linear、modern RLE RGBE、`-Y +X`，
  SHA-256=`30933d55e45f0795daf49f3cbefbe0e5ebcb821ee04fb0a2818c02ffc3938817`。
- catalog resource:
  `polyhaven_hdri_studio_small_03_d9548efaac6fb667e616ea809ef5144d`，profile
  `radiance_hdr_v1`，license=`CC0-1.0/open`，attribution=`Greg Zaal`。
- canonical source 与 `data/hdri/studio_small_03_1k.hdr` 兼容 alias 内容 hash 相同、inode 不同；
  alias 是独立 regular-file copy，不是 hardlink。

### Aerial Asphalt 01 PBR

- run:`out/acquire/polyhaven-resources/pbr_texture_set/20260711T125915Z_7c894939/manifest.json`。
- official listing 779 records；本次 exact source-id replay 为 1 request、0 body bytes、3 reused files、
  0 downloads，状态 `ready` / item `skipped`。
- 重新验证 5,477,198 bytes；三张 1024×1024 PNG 为 Diffuse、DirectX normal 与 ARM，physical size
  30,000×30,000 mm。ARM channel contract 为 R=ambient occlusion、G=roughness、B=metallic。
- file SHA-256:Diffuse=`2aaa24b429b395afc4309afd431edcee52bd640265f3adb5638d0d228ff33992`，
  normal=`aea5813095e3bd62c178a5f5f1f463930ff970c6c2914cb75c0225d02128c92e`，
  ARM=`235c81726fdff13093c98ebe6bfd7c5f6720f572d540721a6455a0ed8b9bc6dc`。
- catalog resource:
  `polyhaven_pbr_aerial_asphalt_01_dd67d209d4cd82d8275e9032b5ce648a`，profile
  `ue_pbr_png_v1`，license=`CC0-1.0/open`，attribution=`Rob Tuytel`。

真实 `data/catalog.db` 已迁移为 schema v5；`resource-stats` 为 2 resources / 4 files / 5 artifacts /
0 bindings，2/2 `ready`、`polyhaven`、`CC0-1.0/open`。SQLite publication 不是由 ready 字符串自证：
每个 cohort 的 bundle/content digest、唯一 primary file 和 hash-bound source/validation artifacts 都由
`finalize_resource` 在一个 transaction 内提交并在 replay 时重新验真。

## 1440² scene showcase evidence

- source render:
  `out/showcase_source_renders/20260711T134429Z_727167e6/scene_bm_player_home/manifest.json`。
- `scene:bm_player_home`:56 actors、22 renderable StaticMesh actors/components、39,187 triangles、
  10 materials、11 textures；CC-BY-4.0/open，attribution=`Sander Vander Meiren - Stylised Sky Player
  Home Diorama`。
- source output:72 `beauty_lit` PNG + 72 `object_mask` EXR，1440×1440；setup/render 均为 0
  unfiltered warnings/errors，render manifest schema v3 status=`ok`。
- provisional archive:
  `out/showcases/m3_t3_1b/20260711T141026Z_scene_bm_player_home/showcase.mp4`；H.264/yuv420p、
  1440×1440、24 fps、72 frames、3.000 s、1,814,801 bytes，SHA-256=
  `56e2be69bb29881a668b48fd686127fc265b00bc7e52e7eff6329586eb485c60`。
- 独立视觉审阅量化 foreground=`4.2493%–5.3900%`、mean=`4.9058%`，bbox area 最低约 `8.20%`；
  因主体过小，该 provisional MP4 不再是可放行的阶段结果。showcase gate 已升级为实际解码 object mask，
  每帧要求 foreground>=10%、bbox>=18%、margin>=3%；旧 source run 在编码前以
  `foreground=0.047325 < 0.100000` 被确定性拒绝。
- `camera.distance_multiplier` 已进入 JobSpec/manifest/geometry，当前 showcase spec 设为 `0.6`；实现提交
  `e4dde5e`，focused=`79 passed`、broader render regressions=`123 passed`、Ruff/Mypy/diff-check 全绿。
  后续从新的真实 UE source run 生成 clean-tree archive，并在 WORKLOG 追加最终路径，不改写本条历史证据。

## 人物类别状态

本阶段人物数据明确为 `not_available`：当前没有 accepted/deliverable person sample，也没有人物类
showcase video。已检查的候选均不得替代该状态：

- `Character Fight` mixed-skeletal 候选捕获 404 strict warnings（375 zero-scale physics、22 skinned
  hierarchy、7 empty-bound navigation），已原子 rollback：
  `out/scene_builds/20260711T082245Z_df6d6c7b/bm_character_fight_diorama/manifest.json`。
- Feudal Japanese House 有 12 skinned meshes / 444,275 triangles，但 UE 只生成 SkeletalMesh、零
  StaticMesh，已 rollback：
  `out/scene_builds/20260710T165310Z_083516c0/bm_feudal_japanese_house/manifest.json`。
- `bm_genshin_environment_base` 虽 build 成功，但 12 primitives 中 11 个 alpha blend 导致 stencil
  coverage=`1/12=0.083333`，保留 build-only quarantine：
  `out/scene_thumbnails/20260710T164331Z_1a9eb59b/scene_bm_genshin_environment_base/manifest.json`。

下一阶段必须先建立显式 SkeletalMesh/Skeleton/pose-or-animation ingestion、人物 mask 与 license gate，
再把人物数据从 `not_available` 升级为 accepted；本报告不通过降低 static scene gate 来提前宣称完成。

## 审查中关闭的问题

- schema v5 migration 现能识别 v4 中已经由完整 evidence 发布、随后转为 failed/quarantined 的 lineage；
  `published_once` 由 SQL trigger 保证 append-only，不能借状态回退重写历史 cohort。
- published resource 的 metadata/files/artifacts 不可变；唯一合法升级是 `verified -> ready`，且只追加
  profile 必需的 ready proofs。
- HDRI compatibility alias 使用独立 copy，catalog commit 后的 alias crash window 由 durable intent
  reconciliation 恢复；历史 alias 不会覆盖较新 revision。
- human catalog 输出会转义全部 Unicode `Cc`（包括 C0、C1 与 DEL）控制字符；JSON 输出保持结构化。

## 结论

- Poly Haven HDRI/PBR adapter、schema v5 resource catalog 与真实 exact replay 获准进入提交收口。
- 当前 scene-level 流水线已能稳定交付 1440²、72-view、mask-aligned 的源帧；旧 MP4 编码有效但构图
  不达新门禁，最终 clean-tree stage archive 必须由 0.6 镜头倍率的新真实 UE run 重新生成。
- 人物类别仍是明确缺口，不得计入本切片完成度。
