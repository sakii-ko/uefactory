# Formal Review:M1 渲染服务 v1 (`feat/m1-render`)

- 结论:**APPROVE**
- 日期:2026-07-10
- 范围:JobSpec → UE 5.5.4 MRQ 六通道 → 本地/远程执行 → 通道校验 →
  contact sheet/index/turntable → 失败生命周期与清理。
- 审计方式:源码/测试审查 + 反例测试 + 本地真实 UE 两次确定性渲染 + HDRI/none 实跑 +
  l40s 真实远程渲染 + 执行代理亲眼审阅 contact sheet;不是只读代码或只检查文件存在。

## 验证结果

1. `tools/check.sh`
   - Ruff check/format:通过。
   - mypy:通过(42 source files)。
   - pytest:`102 passed, 2 deselected in 17.50s`。
2. `.venv/bin/pytest -m ue -vv`
   - `2 passed, 102 deselected in 74.40s`。
   - 最新六通道 run:`out/renders/20260709T194212Z_4313e2b8/builtin_cube`。
3. 本地重复确定性
   - `out/renders/20260709T191746Z_b5cf85a4/builtin_cube`
   - `out/renders/20260709T191834Z_cb99c332/builtin_cube`
   - 六 pass × 8 views = 48 帧 decoded pixel SHA-256 逐帧一致。
4. 跨节点确定性
   - l40s:`out/renders/20260709T192339Z_e408576b/builtin_cube`。
   - 与本地基准 48 帧 decoded pixel SHA-256 全部一致;manifest local validation 与远程 cleanup
     都为 ok,远端目录删除后复核不存在。
5. 光照/通道语义
   - HDRI 六通道:`out/renders/20260709T191403Z_5058d55e/builtin_cube`。
     beauty/unlit 保留环境;四个 data pass 与无 HDRI 基准逐帧一致。
   - none:`out/renders/20260709T191654Z_2ef29f7b/builtin_cube`。
     黑背景、emissive cube 可见,无发光地面。
   - contact sheets 均已按原分辨率亲眼审阅:主体完整落地、8 视角正确、无 `OCIO INVALID`/
     编辑器覆盖层、无 HDRI 穹顶写入数据 pass。
6. 产物与卫生
   - PNG 实际 RGB/8-bit;depth/mask 实际 half-float RGBA EXR;manifest 路径相对且记录引擎版本。
   - turntable 用 ffprobe 验证 4.000s;contact sheet 为 RGB PNG。
   - 最终无本机 UE 进程、无 `Content/UEF/RenderJobs/<run_id>` 遗留;远程成功路径 cleanup verified。

## Review 发现与关闭情况

- **[BLOCKER,已关闭]** 远端 runner 在 Popen 与 PID status 写入之间硬崩时,旧 stop 逻辑可能把
  “tmux 已死”误当作 UE 已停并删除 job tree。修复后非终态必须完整验证
  PID=PGID/session/start-ticks,TERM→KILL 并确认组退出;缺/坏身份和 PID 复用全部 fail closed。
  新增 dead tmux + missing PID、真实 PID reuse、terminal without PID 反例;远程联合回归 27 项通过。
- **[MAJOR,已关闭]** 本地 setup/render 异常曾忽略生成资产 cleanup 失败。现在 cleanup 写入 manifest,
  失败升级状态并作为 note 附加到主异常;KeyboardInterrupt 路径有回归测试。
- **[MAJOR,已关闭]** UE runtime 的具体失败 manifest 曾可能被 `UERunnerError` 覆盖。现在合并 host error,
  保留原始 `error` 与 runtime detail;非零退出反例已覆盖。
- **[BLOCKER,已关闭]** HDRIBackdrop 污染 depth/normal/basecolor/mask。改为 beauty/data 双 sequence,
  并以真实 HDRI run 对无 HDRI 数据 hash 证明隔离。
- **[BLOCKER,已关闭]** 空 OCIO transform 造成黄色错误覆盖层、格式/alpha 声明与物理文件不符、
  rounded luma 弱确定性等假成功。现已由有效 transform、物理解码校验和 decoded pixel hash 关闭。
- **[MAJOR,已关闭]** three-point 动态 binding 顺序导致 beauty FP16 不稳定。固定持久灯光 actor 顺序后,
  本地双跑及本地↔l40s 全像素一致。

## 未决项

- 无阻塞或中等级未解决项。
- 本机 doctor 的 NAS/DDC 写速与缺 `vulkaninfo` 是已知 WARN;真实 H100/l40s Vulkan 渲染均成功。
- 4090 按 Owner 2026-07-09 指示为机会性节点,不属于 M1 必验条件;本次已在 l40s 完成远程 DoD。
- M2 真实导入资产的远程打包尚未实现,属于 M2/M4 数据面任务,不影响 M1 内置 cube 的验收。

## 决策与下一步

- M1 渲染通道/确定性契约固化为 ADR-004。
- 允许将 `feat/m1-render` 以 `--no-ff` 合入 `main` 并标记 M1 tag。
- 当前 Sprint 切换到 M2:真实 FBX/glTF/GLB → UE headless ingest → SQLite catalog →
  标准缩略图的十资产端到端闭环。
