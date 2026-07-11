# Formal Slice Review:M3 acquisition runtime controls

- 结论:**APPROVE**
- 日期:2026-07-11
- 最终审查提交:`2eeacc9`
- 审查 patch SHA-256:`734f9dd53c80e8bd91481d65869aa46630357c7889c73c65d013cf75cdca305a`
- 范围:PolyHaven model adapter 的 retry/backoff/`Retry-After`、逐 hop rate limit、UTC-day
  item/byte quota、disk/free-space gate、Range/retransmission durable accounting、schema v3 runtime
  receipt 与 no-op state anchor。
- 明确不在本次批准范围:failure journal、permanent revision quarantine/rotation、跨 run scheduling、
  HDRI/PBR provider adapter、Objaverse 与 24h supervisor 验收。

## 验证结果

1. 自动化与静态门禁
   - 全 acquire tests:`222 passed`。
   - 全项目最终回归:`890 passed,2 deselected`。
   - focused claims/crash/framing/schema:`21 passed`;第二位独立审计的 crash/quota/Range/oversize
     cohort:`17 passed`。
   - Ruff format/check、Mypy、py_compile、`git diff --check` 全绿。
2. 真实兼容性
   - 三份 schema v2 finalized run 全部可重放:Barber Shop Chair=`render_ok`、ArmChair=`skipped`、
     Barrel=`render_ok`；state/catalog/run/spec/batch 共 11 个文件重放前后 SHA-256 不变。
   - `data/catalog_m3_polyhaven.db`:`integrity_check=ok`,foreign-key violations=0。
   - 真实官方 listing 演练:
     `out/acquire/polyhaven/20260711T075932Z_4053a0b1/manifest.json`；521 discovered、1 request、
     0 payload、518 unseen revisions deferred,CLI status=`deferred`。schema v3 no-op receipt digest 与
     `state.noop_run_receipts` anchor 精确一致。
3. 独立结论
   - 两位独立 reviewer 均对同一 patch/commit 给出 `APPROVE`；最终分级
     BLOCKER/MAJOR/MINOR/NIT 全为 0。

## Review 发现与关闭情况

- **[BLOCKER,已关闭]** transient/integrity 交替曾重置 retry budget。现按 failure category 累计,
  交替序列也在有限总尝试内收敛。
- **[MAJOR,已关闭]** redirect hop、UTC 跨日 reservation、schema v2 no-op downgrade 与 schema v3
  no-op runtime 重算存在漏计/降级空间。现每个 hop 独立取 token；跨日新 reservation 要求 restart；
  schema v2 no-op 拒绝,新的 no-op digest 必须锚定 state。
- **[BLOCKER,已关闭]** oversized body、ledger close、marker unlink 之间存在多处 crash debt 窗口。
  conditional 1-byte probe 在有 quota+disk headroom 时预留；`spec+1` marker 与 atomic close 可恢复；
  close 前失败、close 后未 unlink、同日与跨日重启均零网络幂等结算。
- **[MAJOR,已关闭]** probe 曾让 daily bytes、max storage 或 free-space 的精确边界误拒合法 body。
  无 headroom 时 probe=0,并强制唯一、canonical、精确 `Content-Length`；缺失/重复长度、错误长度与
  `Transfer-Encoding + Content-Length` 全部在读取 0 body bytes 时 fail closed。
- **[BLOCKER,已关闭]** body 已从 transport 返回但 file write/flush 失败时,重试可复用旧 reservation
  免费重传。open ledger 现持久记录 `body_bytes_claimed`;每次网络请求前预 claim 预计 body,只释放
  transport 明确未交付的部分。lost-write 的第二次请求会先扩 quota 或被拒绝；正常 206 Range resume
  仍复用已持久 partial,Range 200 在读取前追加 offset claim。
- **[MAJOR,已关闭]** destination replace 后 crash + `--force`、完整 partial、EOF probe read error 曾
  误退款旧 probe。force 现在先结清旧 reservation 再创建新 transfer；所有未持久确认 EOF 的完整
  partial 都保守消费 probe。
- **[MINOR,已关闭]** Python numeric equality 曾把 JSON `2` 与 `2.0` 当作同一 canonical config。
  runtime config 改用 canonical payload digest 比较,run/state/quota schema version 要求 exact integer。

## 结论与边界

- 本切片未解决项:BLOCKER / MAJOR / MINOR / NIT 均为 0；可作为后续 resource adapters 的共享
  runtime substrate。
- T3.0 仍未整体关闭。下一切片必须实现 durable failure journal 与 permanent failure rotation,
  证明坏 revision 可审计地让路且不会饿死 unseen 队列。
