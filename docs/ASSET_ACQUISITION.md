# 资产持续获取战略(M2/M3 设计基础)

> Planner 起草 2026-07-08。回答一个问题:**不靠手搓,资产从哪儿源源不断地来?**
> 答案是五条供给腿 + 一个增殖乘数 + 一套让"持续"成立的基建。M3 动工前本文细化成任务。

## 0. 总原则

1. **license 是一等公民**:每个资产入库必须带 license + 来源 URL + 抓取时间 + 内容 hash
   (catalog 已定 NOT NULL)。策略分三档:
   - `open`(CC0/CC-BY/自产):无限制使用;
   - `nc`(CC-BY-NC / research-only):**收但隔离标记**,按 Owner 假设 A2(研究用途)可用,
     未来商用时一键过滤剔除;
   - `ue-only`(Epic/Fab 内容许可):只能在 UE 内使用——**而我们恰好只在 UE 里渲染**,完全兼容;
   - 来路不明/禁转载(TurboSquid 免费区之类)→ 不收。
2. **拉推结合**:push(每日配额定量爬新资产)+ pull(渲染作业缺什么品类,按需补什么)。
3. **质量门禁前置**:垃圾资产比没有资产更糟(拖慢渲染、污染数据)。入库先过自动检查
   (三角形数区间、有 UV、有贴图、包围盒合理、非损坏),再渲缩略图做去重与质量分。

## 1. 五条供给腿(按启动顺序)

### 腿 A:存量开源数据集批量灌库(M3 第一枪,性价比最高)
一次性 bulk 导入 + 偶尔版本更新,直接把库存从 0 拉到 10万+:

| 数据集 | 规模 | 许可 | 备注 |
|---|---|---|---|
| **Objaverse 1.0** | ~80 万 (glb) | 逐对象 CC(需过滤) | 主力;先导入 LVIS 标注子集(~4.7 万,质量较好) |
| **Objaverse-XL** | ~1000 万 | 逐对象 | 二期;去重/质量门禁压力大 |
| Google Scanned Objects | ~1 千 | CC-BY 4.0 | 扫描级质量,小而精 |
| Smithsonian Open Access | 数千 | CC0 | 文物扫描 |
| ABO (Amazon) | ~8 千 3D | CC-BY-NC | `nc` 档,电商产品类 |
| 3D-FUTURE / 3D-FRONT | 万级家具+室内布局 | research | `nc` 档;室内场景合成的原料 |
| PolyHaven | 数百模型 + **HDRI/材质** | CC0 | HDRI 是 M1 光照预设的刚需,最优先接 |
| ambientCG | 千级 PBR 材质 | CC0 | 材质库,喂给"变体增殖" |

### 腿 B:API 化持续抓取(M3 主体,"持续"二字的来源)
- **PolyHaven API**:干净、CC0、无鉴权,第一个 adapter 用它打样;
- **Sketchfab API**:搜索 + 下载(OAuth),按 `downloadable + CC 许可` 过滤,增量爬
  (记 cursor,断点续传,尊重 rate limit);量大,与 Objaverse 高度重叠 → 靠 hash 去重;
- **Smithsonian / ambientCG** 均有可编程接口。
- 形态:每源一个 `acquire/<source>.py` adapter,统一契约:`list_new(since) → fetch(id) →
  license_check → normalize → ingest`,由调度器(cron/`uef acquire --daily`)驱动。

### 腿 C:UE 生态内容(质量天花板,license 为 `ue-only`)
- **Fab / Quixel Megascans**:扫描级 3D 资产 + 表面材质,对 UE 用户基本免费;
  **没有官方公开下载 API**,自动化违 ToS 风险高 → 定位为**半人工通道**:Owner 定期在
  Fab 认领免费包/每月免费资产,下载到约定的 drop 目录(可在 Windows 机或 in-editor Fab 插件),
  管线只负责从 drop 目录自动 ingest。每月十几分钟人工,换顶级质量,值。
- **Epic 官方免费内容**:City Sample(整城!)、Paragon 角色、Infinity Blade 系列、
  各 Feature Pack——全部 `ue-only` 许可,体量和质量都极高,一次性人工认领 + drop 目录 ingest。
- 结论:C 腿不追求自动化,追求"人工动作最小化 + 入库自动化"。

### 腿 D:程序化生成(无限供给、license 全干净、参数可控)
- **Infinigen / Infinigen-Indoors**(开源,Blender 底座):无限自然物 + 室内场景,
  带完整参数与随机种子 → 可复现、可按分布采样;headless Blender 跑在 CPU 富余的本机(192 核!),
  导出 FBX/USD → 走标准 ingest。
- **Blender geometry nodes** 参数化生成器(headless 脚本化):家具/建筑构件/岩石等品类,
  一个生成器 = 一个品类的无限变体。
- **UE PCG framework**:场景级组装(把 catalog 里的资产摆成房间/街区),资产×布局的乘法。
- 战略地位:这是数据农场最强的一条腿——别人给不了的分布控制、标注完备性、无版权风险,全在这。

### 腿 E:AIGC 3D 生成(定向补货,快速成熟中)
- 开源 image/text→3D:**TRELLIS**(MIT)、Hunyuan3D-2 等(许可需逐个核验)——
  出 glb + PBR 贴图,单件质量已到"道具级可用";
- 用途:**定向补品类**——"渲染作业需要 500 把不同的椅子,库里只有 80" → 生成任务补齐;
- 算力:跑在 4090 节点空闲时段(24G 显存够),与渲染作业共用 farm 队列错峰;
- 生成物 license 归我们(以模型许可为准,入库记 `generated:<model>@<version>+prompt+seed`,可复现)。

## 2. 增殖乘数:一个资产变一百个(M3.5)
库存量不是数据量。同一 mesh × 材质替换(ambientCG/Megascans 材质库)× 颜色抖动 ×
破损/贴花 × 缩放抖动 = 组合爆炸,而这正是渲染数据farm相对静态数据集的核心优势。
实现上是 catalog 里的"虚拟资产"(base_asset + variant_params),渲染时现场应用,不占存储。
**10 万实体资产 × 增殖 ≈ 千万级可渲染变体。**

## 3. 让"持续"成立的基建(M3 验收的真正对象)
1. **增量同步**:每源记 `last_cursor`,只拉新;失败重试带退避;每日配额限流。
2. **去重**:内容 hash(精确)+ 渲染缩略图感知哈希/embedding(近似,跨源查重)。
3. **质量门禁**:自动检查 → 隔离区(quarantine)→ 抽样人工复核 → 放行;门禁规则版本化,
   规则升级可对存量重新评级。
4. **规范化**:一切来源 → glTF/USD 中间格式 → 统一米制、Z-up、pivot 落地 → UE Interchange
   导入 → 绑定标准母材质(master material)。**规范化质量决定下游一切**,M2 的核心。
5. **可观测**:`uef catalog stats` + 每日 HTML 报告:各源新增/拒收/去重数、license 分布、
   品类分布(缺口一目了然,反过来驱动 pull 式补货)。

## 4. Owner 已拍板(2026-07-08,"所有资产都需要"——能收尽收)
- Q1 = A:`nc` 档收录 + 隔离标记(license 追溯硬约束不变);
- Q2 = A:建 Fab/Epic drop 目录半人工通道(细节 M3 C 腿动工前对齐);
- Q3 = A:AIGC 研究用途从宽 + model/version/prompt/seed 全程留痕;
- Q4 = 终点全量、路径分批:LVIS 子集打通闭环后,**XL 全量灌库为 M3 v2 正式任务**,
  存储/去重/门禁按千万级规模设计。
详见 `docs/QUESTIONS.md` 已归档 Q1–Q4。

## 5. 落地顺序(并入里程碑;按 Q1–Q4 批复更新)
- **M2**(不变):本地文件 ingest + 规范化 + catalog + 缩略图 —— 一切腿的公共地基;
- **M3 v1**:PolyHaven adapter(HDRI+模型+材质,打样)→ Objaverse LVIS 子集灌库
  → 质量门禁 + 去重 v1 → 每日增量调度;
- **M3 v2**:**Objaverse-XL 全量灌库(正式任务)**、Sketchfab adapter、GSO/Smithsonian bulk、
  drop 目录半人工通道(C 腿);
- **M3.5**:变体增殖(材质库 × mesh);
- **M4 之后**:Infinigen(D 腿)与 AIGC(E 腿)作为 farm 的"生产型作业"接入队列。
