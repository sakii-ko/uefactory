# Formal Slice Review: M3 durable acquisition failure journal

- 结论：**APPROVE**
- 日期：2026-07-11
- 审查提交：`e9726476c4b856128dcb11654c8661cd175ea295`
- 审查提交摘要 SHA-256：`0efdc635403e43c004f6ac7e1b53d567a86827002a300bcfa15d2aba9e163edf`
- 范围：PolyHaven model adapter 的 durable failure journal、per-revision cross-run scheduling、
  permanent/quality quarantine、integrity threshold、operator release、stale-run/crash recovery、schema v4
  run/finalization receipts、failure report 与 resolution-isolated storage。
- 明确不在本次批准范围：HDRI/PBR provider adapter、Objaverse、dedupe、daily supervisor 与 24h 验收。

## 最终验证

1. 自动化与静态门禁
   - all acquire：`283 passed`。
   - 全项目：`951 passed, 2 deselected`。
   - Ruff format/check、Mypy、`compileall`、`git diff --check` 全绿；commit hooks 全绿。
2. 真实 provider 与历史兼容
   - official 521-model listing schema v4 no-payload run：
     `out/acquire/polyhaven/20260711T104158Z_12c6465e/manifest.json`；1 HTTP request、0 body、0 attempted、
     518 unseen quota-deferred，CLI status=`deferred`。
   - 新 failure journal 为 0 events；human `--status all` 前后文件 SHA-256 均为
     `19d440f64a9a5549d0b871ce40450cb243d7b513b23c789acdb6451058761414`，证明 report read-only。
   - 3 个 schema v2 finalized、1 个 schema v3 noop、1 个 schema v4 noop 共 5 个真实 run 严格重放；
     13 个 watched files 总摘要
     `e637436aad969439502ecee836498aca2f035d5ec94b1e9b545bcf5ed66eab4f` 前后一致。
3. 独立审查
   - failure journal / cross-run / path / crash adversarial reviewer：`APPROVE`。
   - CLI、runtime config、schema v2/v3 compatibility reviewer：`APPROVE`。
   - finalization receipt、state anchor 与 transaction consistency reviewer：`APPROVE`。
   - 最终 BLOCKER / MAJOR / MINOR / NIT 均为 0。

## 已建立的 durable contract

- journal 为严格 schema、连续 sequence、deterministic event ID、previous-event hash chain 与 payload/head
  SHA-256；failed event 固化当次 failure policy，replay 可重算 disposition、integrity threshold、
  Retry-After 与 exact next-eligible deadline。
- permanent/path/license/quality 立即 quarantine；transient/downstream/interrupted 与 quota/disk 分别进入
  backoff/deferred；integrity 只按连续 integrity streak 达阈值隔离。operator 只能用 exact asset revision
  生成 audited release，`--force` 不绕过 quarantine。
- schema v4 receipt 同时绑定 journal prefix、event refs、当前 run/policy/attempt ordinal、success/failure/retry
  cohort、PolyHaven source/revision/asset closure、state transition 与 finalize-only state anchor。schema v2/v3
  保留原 full-state hash 语义，loader 不注入会改变历史 payload 的新 optional maps。
- 每个 selected revision 在 I/O 前持久化 active attempt；item failure 先落 journal、后清 marker。stale startup
  可恢复真正的 process interruption，但已观察的本地异常不会被误记为 provider failure。mixed batch 中坏
  revision 让出队列，成功 revision 仍可 prepare。
- downstream cohort 先在内存构造全部 failure/resolution events，全部合法后一次原子 journal write；
  journal→state/manifest crash 使用 deterministic refs 重放。旧 resolution/release 不得清除更新 active
  failure，terminal state 在 selection 中始终优先。
- source lock、run root、state/manifest/journal 与 report/finalize 均逐组件拒绝 symlink；same revision 的
  package storage 再按 resolution 分目录，1k closure/partial 不会污染 2k retry。

## 结论与下一步

T3.0 persistent acquisition control plane 的 DoD 已达成，可作为其他 provider adapter 的共享 substrate。
下一主线是 T3.1b：用同一 discover/files/revision/runtime/receipt 契约接入 PolyHaven HDRI 与 PBR texture set，
替换 M1 非增量单文件 helper；M3 整体仍需 T3.2–T3.4 与真实 24h evidence 后才可发布。
