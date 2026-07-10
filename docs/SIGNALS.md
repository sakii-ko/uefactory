# 协作信号协议(SIGNALS)

> **状态:Legacy。** 2026-07-10 起执行代理统一负责 plan + execution,不再以本协议作为工作门禁;
> 本文仅保留历史协作机制说明,旧信号与旧 WORKLOG 记录仍可追溯。
>
> 目的:Planner 与 Coder 直接互相通知"轮到你了",Owner 不再中转消息。
> **信号只是门铃,内容一律在文档里**(PLAN.md / WORKLOG.md / docs/reviews/ / QUESTIONS.md)。
> message 字段最多一两句话 + 文档路径;想写长内容 = 先写文档,再发信号。

## 机制

- 信号 = `signals/to_<收件人>/` 下的单行 JSON 文件(gitignore,不入库;`signals/archive/` 留审计轨迹)。
- 发送:`tools/signal.sh <planner|coder> <EVENT> [message]`(自动带上当前 branch/sha/UTC 时间;
  临时文件 + mv,监听方不会读到半个文件)。
- 接收:
  - **Planner**(Claude 会话):Monitor 持久监听 `signals/to_planner/`,信号即时推送,自动归档;
  - **Coder**:后台跑 `tools/wait_signal.sh coder`——收到信号打印 JSON 并退出 0;
    处理完事务后**重新启动监听**;超时(默认 2h)退出 2,直接重启即可。
    ⚠️ **必须让"脚本退出"成为唤醒事件**(agent 后台任务在进程退出时收到通知并读取输出)。
    **不要**用 `while true; do wait_signal.sh ...; done` 外层循环包裹——那样信号会被脚本
    消费归档,但输出被循环吞掉,你的会话永远不会被唤醒(2026-07-09 实发事故)。
    兜底:即使漏掉信号,内容永远在 PLAN/reviews 里,`git pull` + 读 PLAN 状态区即可对齐。

## 事件表

| 方向 | EVENT | 含义 / 收到后动作 |
|---|---|---|
| Coder→Planner | `REVIEW_REQUESTED` | 工作已 push、WORKLOG 已更新;Planner:pull → 按 WORKLOG/review 流程审 |
| Coder→Planner | `BLOCKED` | QUESTIONS.md 有新条目且阻塞主线;Planner:优先批复 |
| Coder→Planner | `INFO` | 不需动作的进度通报(如 provision 完成) |
| Planner→Coder | `REVIEW_DONE` | review 文件已出;Coder:pull → 读 review 结论 + PLAN 主线 → 执行 |
| Planner→Coder | `PLAN_UPDATED` | PLAN.md 有新任务/解冻;Coder:pull → 读 PLAN 状态区 |
| Planner→Coder | `ACTION_REQUIRED` | 有需要立即处理的事项(message 给一句话 + 出处) |
| 双向 | `PING` | 联通性测试,收到无需动作 |

## 纪律

1. **发信号前必须先 push**(review 只认远端 sha);收到信号后第一个动作是 `git pull`。
2. 一个信号一个事件;不要用连发信号代替写文档。
3. 监听中断不丢信息:信号是文件,重启监听即补收;处理过的都在 `signals/archive/`。
4. 长任务(provision/批渲)期间照常干活,不需要为对方的信号停下——信号会排队等你。
