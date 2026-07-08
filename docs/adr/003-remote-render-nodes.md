# ADR-003:远程渲染节点策略——数据主库在本机 NAS,节点只当算力

- 状态:Accepted(2026-07-08, Planner;依据 Owner 指示 + 实测)
- 背景:本机 H100 显存被常驻进程占用且非图形定位卡;可经 ssh 使用 `4090`(8× RTX 4090)与 `l40s`(1× L40S 46GB)。实测:两节点存储与本机完全不相通(l40s 存在同路径不同数据的 CephFS 陷阱);4090 存储近满;l40s 是可能被重建的容器。Owner 要求避免频繁 ssh、用 tmux 等持久化方式。
- 决定:
  1. **单一数据主库**:catalog、原始资产、最终渲染产物只存本机 NAS(repo 的 `data/`、`out/`)。远端一律视为可丢弃的"算力 + 暂存",任何远端数据丢失都必须可从本机重建。
  2. **连接纪律**:所有 ssh/rsync 由 `src/uefactory/core/remote.py` 统一发起,强制携带
     `-o ControlMaster=auto -o ControlPath=~/.ssh/uef_cm_%r@%h-%p -o ControlPersist=900 -o BatchMode=yes`;
     多个探测/操作合并为单次批量命令;禁止业务代码裸调 ssh。
  3. **长任务**:预计 >60s 的远程任务必须 `tmux new-session -d -s uef_<job_id>` 执行;任务脚本周期性写远端状态 JSON(阶段/进度/心跳),本机按需轮询(间隔 ≥30s),不依赖保持 ssh 前台存活。
  4. **数据面**:rsync(`-z --partial`)推作业包 → 远端渲染(输出写远端本地/暂存)→ rsync 拉回产物 → 清理远端暂存(引擎与哨兵保留)。**安全阀**:远端工作目录必须含 `.uef_node` 哨兵文件(记录 host 名 + 初始化时间);任何带 `--delete` 的 rsync 与任何 `rm -rf` 只允许作用于已验证哨兵的目录。
  5. **引擎 provision**:每节点一次性传输 UE 5.5.4(优先传本机已有的 zip + 远端解压,断点续传);落点:4090 → `/home/lyf/uef/engine/`,l40s → `/root/nas/bigdata1/cjw/uef/engine/`(其自有 NAS,容器重建可幸存)。幂等:已存在且版本校验通过则跳过。
- 后果:
  - (+)节点可随时增删(未来 `duan`/`jz2` 也能纳入);容器重建、磁盘清理都不伤数据;
  - (−)WAN 带宽成为成本:作业包必须最小化(只传该作业需要的资产),产物压缩回传;
  - farm(M4)按"节点池"抽象:每节点 profile(GPU 数、显存、暂存配额、并发上限)进 `uef.toml [hosts.*]`。