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

### Q1 [2026-07-08] M3 是否收录 `nc`/research-only 资产
- 背景:`docs/ASSET_ACQUISITION.md` 建议把 CC-BY-NC / research-only 资产收进库,但隔离标记为 `nc`,未来商用时可一键过滤。该选择会影响 M3 的数据源白名单、catalog license tier 和默认查询过滤。
- 备选方案:A 收录并隔离标记;B M3 只收 `open` 与 `ue-only`,暂不碰 `nc`。
- 我的倾向:A,因为当前 Owner 假设 A2 是研究/内部数据生产,且隔离标记能控制未来风险。
- 阻塞哪些任务:不阻塞 M0/M1/M2;阻塞 M3 bulk/source whitelist 的最终口径。

### Q2 [2026-07-08] Fab/Epic 内容是否建立半人工认领通道
- 背景:Fab / Quixel / Epic 免费内容质量高且 UE 内许可匹配,但没有稳定公开下载 API,自动化抓取有 ToS 风险。方案是 Owner 定期人工认领/下载到 drop 目录,管线只负责自动 ingest。
- 备选方案:A 建立半人工 drop 目录通道;B 暂不接 Fab/Epic,只做公开 API/数据集来源。
- 我的倾向:A,每月少量人工动作换高质量 `ue-only` 资产,性价比高。
- 阻塞哪些任务:不阻塞 M0/M1/M2;影响 M3/M3.5 的 C 腿资产供给。

### Q3 [2026-07-08] AIGC 3D 生成模型许可是否需要法务级核验
- 背景:TRELLIS 等开源模型许可相对清晰,Hunyuan 等社区许可可能有地域/规模条款。AIGC 腿用于定向补品类,生成资产需记录 model/version/prompt/seed。
- 备选方案:A 研究用途先从宽,逐模型记录许可证并保留可追溯;B 接入前逐模型做法务级核验。
- 我的倾向:A,先限定研究/内部用途并保留追溯;若进入商用或外发数据再升级核验。
- 阻塞哪些任务:不阻塞 M0-M3 v1;影响 M4 之后 AIGC 生产型作业是否解冻。

### Q4 [2026-07-08] Objaverse-XL 走全量还是精选
- 背景:Objaverse-XL 是千万级资产,原始文件可能数十 TB,NAS 容量够但去重/质量门禁成本高。`docs/ASSET_ACQUISITION.md` 建议先用 Objaverse 1.0 LVIS 子集打通全链路。
- 备选方案:A 后续全量灌库;B 先精选/分批,以 LVIS/品类缺口驱动扩容。
- 我的倾向:B,先用 LVIS 子集验证 ingest、质量门禁、去重和渲染闭环,再按缺口扩展。
- 阻塞哪些任务:不阻塞 M0/M1/M2;影响 M3 v2 之后的存储和调度规模。

## 已归档

(暂无)
