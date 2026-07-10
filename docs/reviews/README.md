# Review 报告目录

执行代理在里程碑 DoD 证据齐全后产出一份 `<YYYY-MM-DD>-<branch>.md`,结构:

```markdown
# Review: <branch> @ <commit>
- 结论:APPROVE | APPROVE-WITH-FIXES | REQUEST-CHANGES
- 验证:我实际跑了哪些命令、看了哪些产物(不只读代码)
- 发现(按严重度):
  - [BLOCKER] ...(必须修,合并前)
  - [MAJOR] ...(必须修,可下个任务带上)
  - [MINOR/NIT] ...(建议)
- Credit:值得肯定的实现(具体到 commit)
```

REQUEST-CHANGES 的发现会同步为 PLAN.md 的 fix 任务并在同一工作流中修复、重验。
APPROVE 后才可 `merge --no-ff` 进 main 并按需打 tag。
