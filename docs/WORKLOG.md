# 工作日志(WORKLOG)

> Coder 维护,**只追加不改写**历史条目。每完成一个任务(或被卡住)追加一条。
> 这是 review 的入口:Planner 只认这里登记过的工作。

## 条目模板

```markdown
## [YYYY-MM-DD] T<任务号> <标题> — <状态: DONE | BLOCKED | PARTIAL>
- 分支/commit:feat/m0-skeleton @ abc1234
- 做了什么:(2~5 行,写关键决定和原因,不逐条复述代码)
- 验收产物:
  - 命令:`uef render smoke` → 退出码 0
  - 图:out/smoke/20260709T.../frame_0000.png(均值亮度 87/255)
  - 日志:logs/20260709T..._render.log
  - 测试:`tools/check.sh` 全绿(粘贴末尾 summary)
- 耗时/坑:(如 shader 首编 43min;HighResShot 在 xx 参数下全黑,改用 yy)
- 待决问题:(没有就写"无";有就同步写进 QUESTIONS.md)
```

请求 review 时在末尾追加一行:`REVIEW REQUESTED: <branch> <commit-sha>`

---

(暂无条目 —— M0 开工后从这里开始)
