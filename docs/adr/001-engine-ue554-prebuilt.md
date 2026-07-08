# ADR-001:引擎采用 UE 5.5.4 预编译 Linux 版

- 状态:Accepted(2026-07-08, Planner)
- 背景:机器上已就位解压好的 UE 5.5.4 预编译 Linux 版(含 UnrealEditor-Cmd),且同机有历史 headless 渲染成功记录;自编引擎(源码版)成本高(编译数小时、NAS IO 慢),当前需求(headless 渲染 + Python 脚本 + MRQ)预编译版全部覆盖。
- 决定:M0–M4 全部使用 `/root/nas/bigdata1/cjw/UnrealEngine_5.5.4`,不编译引擎源码。
- 后果:
  - (+)零引擎构建成本,立即可用;
  - (−)不能改引擎源码;若未来需要引擎级定制(如自定义 GBuffer 输出),先评估插件方案,不行再开新 ADR 讨论源码版;
  - 引擎升级(5.6+)是独立决策,需新 ADR。
