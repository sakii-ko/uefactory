# UEFactory

UE 数据制造农场:Linux headless 环境下,持续收集 UE 可用资产,并通过 CLI(`uef`)
批量渲染所需内容(lit / unlit / depth / normal / mask 等)。目标形态类似 UnrealZoo。

## 从这里开始读(顺序)

1. `PLAN.md` —— 项目愿景、里程碑、**当前 Sprint 任务清单**(Coder 的工作来源)
2. `docs/CONVENTIONS.md` —— 代码 / git / 日志 / 测试规范(强制)
3. `docs/ENVIRONMENT.md` —— 本机环境事实(UE 路径、GPU、存储)
4. `docs/ARCHITECTURE.md` —— 系统设计
5. `docs/WORKLOG.md` —— Coder 的执行记录(追加式)
6. `docs/QUESTIONS.md` —— 决策请求通道
7. `docs/adr/` —— 架构决策记录;`docs/reviews/` —— review 报告

## 角色

- **Planner**(Claude):计划、规范、review、合并 main、打 tag。
- **Coder**(同事):在 feature 分支实现 PLAN.md 当前 Sprint 的任务,产出验收产物。
- **Owner**(用户):里程碑验收与开放性决策。
