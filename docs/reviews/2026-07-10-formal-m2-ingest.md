# Formal Review:M2 资产摄取与 persistent scene levels (`feat/m2-ingest`)

- 结论:**APPROVE**
- 日期:2026-07-10
- 最终审查提交:`d9dc14de9ba587ee44f650c3b5c72a5d61d00054`
- 范围:开放模型 acquire → strict staging/source evidence → UE 5.5.4 transactional import →
  package-byte evidence → SQLite catalog → thumbnail/report；以及 BlackMyth read-only compatibility →
  portable SceneSpec → persistent level build/reload/finalize → scene render/artifacts。
- 审计方式:完整 diff/源码/反例审查 + 自动化测试 + immutable SQLite/磁盘字节复算 + fresh/skip
  真实 UE 批次 + standalone scene render + 执行代理与独立 reviewer 亲眼审阅 contact sheets。

## 验证结果

1. 自动化门禁
   - `tools/check.sh`:Ruff check/format、Mypy 93 source files 全绿。
   - pytest:`691 passed,2 deselected in 82.64s`。
   - reviewer 独立定向复验:`231 passed in 19.06s`；独立全量同为
     `691 passed,2 deselected`。
   - `git diff --check` 与 `uef --version` 通过；版本为 `0.3.0`。
2. 11 模型 release acceptance
   - fresh:`out/ingest_batches/20260710T145337Z_d8eef9c2/manifest.json`，11/11
     `render_ok`。
   - immediate rerun:`out/ingest_batches/20260710T152241Z_4eaa416b/manifest.json`，
     11/11 `skipped`，所有 ingest/thumbnail manifest 为 null 且没有启动 UE。
   - `data/catalog_m2_package_release.db`:integrity/FK clean，11 assets / 66 artifacts；66/66
     artifact hashes 与磁盘一致。
   - 11 package roots 的完整闭包为 64 files / 68,910,435 bytes；逐文件 path/size/SHA-256、
     bundle digest、manifest 与 catalog artifact 三方一致。
   - 55 个 UE phase logs 的未过滤 warning/error 均为 0；11 个 primary log 都含真实
     Interchange start/completed。
3. 8 persistent scenes
   - 当前 portable SceneSpec generation 全部 `render_ok`:748 scene objects、72 scene artifacts、
     566 package files / 353,907,808 bytes；spec/source/build/inventory/package/artifact evidence
     与 catalog/磁盘一致。
   - research-only `bm_lys_piandian_research` 未混入 8 个开放许可验收场景。
   - 修复后 standalone run:
     `out/scene_thumbnails/20260710T160842Z_b5ab0f74/scene_bm_fantasy_diorama`；setup/render
     均 0 未过滤 warning/error，16 帧 decoded hashes 与既有 current generation 完全一致，
     contact sheet SHA-256 为
     `64f8ccdaa735d07c8a62fe349308377348f47b1260563885f2be5be389a8dc7a`。
4. 可视化与卫生
   - 最新 11 个模型的总览和全部逐资产 8-view beauty/mask sheets 已亲眼检查；主体完整、mask
     对齐，无空帧、严重裁切、overlay 或背景泄漏。
   - 8 个 scene sheets 与修复后的 standalone sheet 已亲眼检查；构图/视角/stencil coverage 正常。
   - `IngestTransactions`、`SceneTransactions`、`RenderJobs` 均为空；Ingested roots 精确等于
     release DB 的 11 assets，无 release DB sidecar 或诊断 package 残留。

## Review 发现与关闭情况

- **[MAJOR,已关闭]** model skip/render 曾只信旧 package evidence，未持续绑定当前 package
  bytes。现 model root 使用完整双扫描/双 hash 闭包，finalize 后不可逆复验，import/thumbnail/
  render/catalog generation 全部绑定 bundle digest。
- **[MAJOR,已关闭]** 同一 model `asset_id` 的 batch/render 可能交错。现跨 thread/process lease
  覆盖完整 generation；fork child 清继承状态，busy/late failure/KeyboardInterrupt 均有反例。
- **[MAJOR,已关闭]** standalone scene render 原先未持 scene lease，resolver 后可与 build
  交错。`7da5c42` 后 lease 覆盖 resolver → UE setup/render → host artifacts；owning thread
  可重入，其他 thread/process busy，fork-safe。
- **[MAJOR,已关闭]** scene evidence 原先只列 inventory 主 package，未持续强制完整 root。
  现要求全部 `.uasset/.umap`，记录已知 `.uexp/.ubulk/.uptnl` sidecar，拒绝未知/缺失/空/
  非 regular/symlink/path escape，以双扫描/双 hash 检测 TOCTOU；finalize 后、catalog commit 前
  精确复验，committed mismatch 明确不回滚、不写 catalog。

## 未决项与决定

- BLOCKER / MAJOR / MINOR / NIT 未解决项均为 0。
- 本机 Ceph/DDC 性能和缺 `vulkaninfo` 仍是已知环境 WARN；真实 H100 UE import/render 成功，
  不构成 M2 阻塞。
- 允许将 `feat/m2-ingest` 以 `--no-ff` 合入 `main`，并在 release bookkeeping 后标记
  `v0.3.0`。
