# Formal Slice Review:M3 PolyHaven models hardened control plane

- 结论:**APPROVE**
- 日期:2026-07-11
- 最终审查提交:`79ea635`
- 范围:PolyHaven official model listing/files → revisioned acquisition state → verified package closure →
  generated IngestSpec → UE/package/catalog/thumbnail → evidence-derived terminal receipt 与历史重放。
- 明确不在本次批准范围:后续 retry/rate/quota integration、HDRI/PBR adapters、Objaverse、daily/24h。

## 验证结果

1. 自动化与静态门禁
   - acquire tests:`139 passed`。
   - Ruff format/check、Mypy 54 source files、py_compile、`git diff --check` 全绿。
   - SQLite `integrity_check=ok`,foreign-key check 为空；无 `commit_intent.json` 残留。
2. 真实 terminal replay
   - Barber fresh run:`out/acquire/polyhaven/20260711T050110Z_6ad80fda/manifest.json`,terminal
     `render_ok`。
   - ArmChair reuse run:`out/acquire/polyhaven/20260711T050808Z_d8ce797d/manifest.json`,terminal
     `skipped`。
   - 两个 run 的 public finalizer 与 private receipt verifier 均重放成功；重放前后 state/catalog/
     run/spec hashes 不变。
3. 真实图像
   - Barber:
     `out/thumbnails/20260711T050521Z_308e8d1e/polyhaven_barbershopchair_01_f111fa76f5cc/contact_sheet.png`。
   - ArmChair:
     `out/thumbnails/20260710T170709Z_423cb378/polyhaven_armchair_01_8a04a102d4a1/contact_sheet.png`。
   - 执行代理与独立 reviewer 均打开检查:8 视角完整居中、无裁切、mask 对齐。

## Review 发现与关闭情况

- **[BLOCKER,已关闭]** thumbnail sanitization 曾错误要求 1 个 subjob；标准 beauty/data 是 2 个。
  current verifier 使用 `expected_subjobs=2`,两组真实 evidence 均通过。
- **[MAJOR,已关闭]** Range resume 曾把完整文件大小记作 transferred bytes。现 reused/complete
  partial 为 0、206 只计响应 bytes、server 200 fallback 计完整响应，均有反例。
- **[MAJOR,已关闭]** prepared manifest 的 request/listing/counts 可在 prepare/finalize 间漂移。
  现 domain-separated prepared receipt 锚进 selected state item；strict shape/accounting/CAS 覆盖完整
  acquisition projection 与 generated spec。
- **[MAJOR,已关闭]** terminal replay 曾不重验 staged raw、完整 source/license provenance 与 artifact
  rows/params。现从 batch/spec/state 重新派生 canonical raw/catalog/import/thumbnail receipt，并复验当前
  UE package bytes。
- **[MAJOR,已关闭]** 较早 finalized run 会被无关的后续全局 state 更新误判 stale。现 commit-time
  snapshot 与 historical replay 分离；历史 run 依赖 per-item anchor/receipt,无关 state progress 可重放。
- **[MAJOR,已关闭]** selected state 的 date/timestamps/paths/files 与 finalized manifest 的
  finalization/evidence/cohort 曾只做部分绑定。现 acquisition projection 全量对账；finalization exact
  shape、committed_at、batch、terminal evidence 与 terminal→nonterminal downgrade 均有攻击回归。

## 结论与边界

- 本切片未解决项:BLOCKER / MAJOR / MINOR / NIT 均为 0。
- PolyHaven models hardened substrate 可作为 T3.1a 真实三模型验收基础。
- T3.0 总 DoD 仍需 retry/backoff/`Retry-After`、全局 rate、item/byte/disk quota、failure journal 与
  restart scheduling；本批准不提前放行 M3 里程碑。
