# Review: T0.7 引擎 provision + 远程冒烟 feat/m0-remote @ d64848b

- 结论:**APPROVE-WITH-FIXES**(F10 必修 + F11/F12 记录修正,完成并经 Planner 复核后
  合并 feat/m0-remote 进 main,M0 进入 Owner 验收)
- 验证(Planner 亲测):
  - `tools/check.sh` 全绿(23 passed);
  - **亲眼查看远程渲回的图**:真场景渲染成立(棋盘地面/Cube/投影),但见 F10;
  - 日志核对:tmux 派发 + 30s 间隔轮询可见,无前台 ssh 等待;
  - **单次批量 ssh 现场核查 l40s**:smoke 作业目录已清理、引擎 5.5 就位、`uef` 非 root
    用户存在、哨兵完好、无 tmux 残留——清理行为属实(但 manifest 未记录,见 F11);
  - **独立探测 4090**:`ssh 4090 'echo ok'` 当前成功——同事遇到的 kex 失败为瞬态(见 F12)。

## 发现

### [MAJOR] F10 屏幕调试文字污染渲染产物(合并前必修)
远程渲回的 frame 上平铺着 UE 屏幕调试信息("YOUR SCENE CONTAINS A SKYDOME MESH…")。
对数据制造管线,调试文字混入产物 = 数据污染,现有校验器(亮度/方差)对此不设防。
**修复**:`DefaultEngine.ini` 加 `[/Script/Engine.Engine] bEnableOnScreenDebugMessages=False`
(或等效启动参数,以实测为准);**本地 + 远程各重跑一次 smoke**,远程图不得再有任何
屏幕文字;两张图与各自 luma 记入 WORKLOG。若去掉文字后本地/远程 luma 仍差距大
(本次 30.99 vs 22.65)与棋盘格尺度差异仍在,不阻塞 M0,但开 M1 任务
"跨节点渲染一致性"(数据集最终需要跨节点可比)。

### [MINOR] F11 清理与 provision 证据缺口
- manifest `cleanup` 为 null,而汇报称"复核为 cleaned"——行为属实(Planner 已现场核实),
  但**证据必须进 manifest**:记录 removed 路径清单 + verified 标志;
- l40s 上残留空目录 `jobs/provision_l40s_20260708T120419Z`,顺带清理,provision 的
  manifest/WORKLOG 同样要有清理记录。

### [RECORD] F12 4090 顺延理由需修正
Planner 复测 4090 ssh 当前**可连通**,"kex_exchange_identification 被远端关闭"为瞬态
(共享机器,真实存在但已消失)。顺延到 M1 的决定**维持**(理由改为:WAN 3.4 MiB/s 下
引擎传输需数小时,与 M0 收尾错峰),但 WORKLOG 补一条修正记录,避免后人按错误结论排障。

## Credit
- **UE 拒绝以 root 运行**是真门槛,`uef` 非 root 用户 + acl 的解法干净且已验证;
- WAN 实测带宽 3.414 MiB/s 已记录(M4 调度关键参数);provision 全程 tmux + `--partial`;
- T0.6 的两个 MINOR 已兑现(`core/remote_probe.py` 抽出,doctor.py 回落 ~340 行);
- 连接纪律保持:租约复用、批量命令、30s 轮询,全链路日志可证。

## 下一步
F10 + F11(小 commit,可合一)→ Planner 复核(将**独立重跑远程 smoke** 作为复核动作)
→ 合并 feat/m0-remote 进 main → **M0 完整验收(Owner)** → tag `v0.1.0` → M1 kickoff
(首任务:4090 provision + 远程冒烟)。

---

## F10/F11/F12 复核(2026-07-09,Planner):**全部通过,T0.7 关闭,M0 交付完毕**
- 亲眼查验本地/远程复验图(20260708T163651Z / 163802Z):无任何屏幕调试文字,
  两端构图一致、亮度收敛(36.87 vs 35.61,此前差 8+);
- **Planner 独立重跑 l40s 远程 smoke(20260708T164626Z):成功,mean_luma=35.608
  与 Coder 运行逐位一致——远程渲染确定性同样成立**;cleanup 证据入 manifest
  (removed_paths + verified + returncode),`check.sh` 23 passed;
- F10 解法组合合理:ini 关闭 + 启动时 `DisableAllScreenMessages` + 背景板遮挡,且
  WORKLOG 记录了"删模板环境导致近黑"的失败路径(校验器再次实战拦截,20260708T163413Z);
- F12 表述已修正,4090 顺延主因改为传输时长(注:Planner 与 Coder 的 4090 探测结果
  不一致——时通时断,进一步佐证瞬态;M1 首任务处理);
- 本轮流程为**信号机制首次全闭环**:ACTION_REQUIRED → 修复 push → REVIEW_REQUESTED
  → 本复核,Owner 全程未中转。
- 后续动作:合并 feat/m0-remote 进 main;M0 进入 Owner 验收,通过后打 `v0.1.0`。
