# 问题与决策请求(QUESTIONS)

> Coder 遇到 PLAN/CONVENTIONS 没覆盖的决策点时写在这里,然后**继续做不被阻塞的任务**。
> Planner 会在每轮 review 时批复(直接在条目下写 `ANSWER:`);重大问题升级给 Owner。
> 已解决的条目移到文末「已归档」。

## 条目模板

```markdown
### Q<编号> [YYYY-MM-DD] <一句话问题>
- 背景:
- 备选方案:A / B(各自代价)
- 我的倾向:
- 阻塞哪些任务:(无则写"不阻塞")
```

---

## 待批复

(暂无)

## 已归档

### Q1 [2026-07-08] M3 是否收录 `nc`/research-only 资产
- 备选方案:A 收录并隔离标记;B M3 只收 `open` 与 `ue-only`,暂不碰 `nc`。
- ANSWER(Owner 2026-07-08,原话"所有资产都需要"):**A**。能收尽收,`nc` 档收录并隔离标记,
  license 追溯为硬约束不变。

### Q2 [2026-07-08] Fab/Epic 内容是否建立半人工认领通道
- 备选方案:A 建立半人工 drop 目录通道;B 暂不接 Fab/Epic。
- ANSWER(Owner 2026-07-08):**A**。建立 drop 目录通道;认领节奏与所需 Windows/Launcher
  环境细节在 M3 C 腿动工前与 Owner 对齐。

### Q3 [2026-07-08] AIGC 3D 生成模型许可是否需要法务级核验
- 备选方案:A 研究用途从宽 + 全程留痕;B 逐模型法务级核验。
- ANSWER(Owner 2026-07-08):**A**。研究/内部用途从宽,catalog 记录 model/version/prompt/seed;
  商用或外发数据前升级核验。

### Q4 [2026-07-08] Objaverse-XL 走全量还是精选
- 备选方案:A 全量灌库;B 先精选/分批。
- ANSWER(Owner 2026-07-08):**终点是 A(全量),路径按 B 执行**——先 LVIS 子集打通
  ingest/门禁/去重/渲染闭环,验证后把 XL 全量灌库排为 M3 v2 的正式任务(不再是可选项),
  存储与调度规模按全量设计。
