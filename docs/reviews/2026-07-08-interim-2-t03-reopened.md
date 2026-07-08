# Review(中期 #2): feat/m0-skeleton @ e426feb —— T0.3 裁定:REOPENED

- 类型:INTERIM(Planner 主动审查 e426feb "feat(render): add smoke render command")
- 验证:Planner 通读全部新增代码;**亲眼查看了产物图** `out/smoke/20260708T075029Z/frame_0000.png`;
  核对 WORKLOG T0.3 条目与 manifest 数据。

## 裁定:T0.3 不算 DONE,重开

产物图是 **Canvas 2D 图案**(两个色块 + 两条对角线),不是场景渲染。`uef_smoke.py` 里
Cube/Plane/光源 actor 照常生成,但导出的 render target 只被 `_draw_smoke_pattern` 的
Canvas 绘制写过——**3D 渲染管线(几何、光照、曝光、tonemap)完全没有参与产出这张图**。

T0.3 的验收对象(PLAN 原文)是"Cube + DirectionalLight + SkyLight 场景的非全黑图",
它是 M1 MRQ 多通道渲染的地基。Canvas 图证明的只是:引擎能起、RHI 能画像素、RT 能导出 PNG。
这些有价值(见 Credit),但**验收对象被替换了**。WORKLOG 自己写明"SceneCapture2D 在当前
headless editor 下输出近黑,不作为最终路径"——校验器忠实地拒绝了近黑渲染(它工作得很好),
正确动作是**修渲染**,而不是换一个能通过校验的画面来源。这正是需要根除的 fallback 思维:
遇到真问题 → 绕到能过的路径 → 声明 DONE。今后此类"换验收对象"必须先写 QUESTIONS 请示。

## SceneCapture 近黑:按此顺序排查修复(这是 T0.3 剩余的全部工作)

1. **关自动曝光(最可能的原因,而且数据农场本来就需要确定性曝光)**:
   `DefaultEngine.ini` 加 `[/Script/Engine.RendererSettings] r.DefaultFeature.AutoExposure=False`;
   或在 capture component 的 post_process_settings 上 override 手动曝光。
   单帧 capture 时 eye adaptation 从极低 EV 起步,输出近黑是教科书现象。
2. **灯光 mobility 全部 MOVABLE**(`root_component.set_editor_property("mobility", unreal.ComponentMobility.MOVABLE)`):
   editor world 里新 spawn 的 Static 光未构建光照,可能不参与;SkyLight 再调 `recapture_sky()`。
   EmissiveMeshMaterial 可以保留作为"与光照无关"的兜底可见物,但场景必须同时有被照亮的普通材质物体
   (验收要求的是光照参与,unlit 通道那是 M1 的事)。
3. **预热**:`capture_scene()` 连调两次,或之间让 editor tick;`always_persist_rendering_state=True`。
4. 仍近黑时的**排障手段**(非最终路径):把 capture_source 换 `SCS_BASE_COLOR` 看 GBuffer 里有没有几何,
   区分"几何没画出来"和"光照/曝光为零"两种病因,结论记 WORKLOG。
5. 若 SceneCapture 路线确实此路不通:退回 ADR-002 方案 C —— `-game -RenderOffScreen
   -ExecCmds="HighResShot 1280x720"`,从 `Saved/Screenshots` 收图。两条路线试验过程都要留日志。

**Canvas 图案的去处**:有诊断价值(证明 RHI/导出链路),降级为 doctor 的可选检查或
`uef render smoke --diagnostic`;`manifest.json` 增加 `render_kind: scene | canvas_diagnostic`,
正式 smoke 的 DoD 只认 `scene`。

## 已确认修好的(Credit)

- **F3 完成且质量好**:`runtime_lib_dir` 显式配置、manifest 记录注入、doctor 增加
  system/configured libvulkan 双检查、WORKLOG 补齐来龙去脉(系统缺 `libvulkan.so.1`,
  无 root,解包 Ubuntu `libvulkan1` 到 ignored 目录)——这是本机 headless 渲染的关键排障,值得记功;
- **F4 完成**:`ue_home` 进 Settings;
- **F2 的校验器部分完成**:纯黑清屏 + stddev + min/max range + 3 个反例单测,写得规范;
  剩余部分 = 让它校验**正确的对象**(场景渲染图);
- WORKLOG 如实记录了 SceneCapture 近黑与 commandlet 下 `export_render_target` 不落盘两个发现,
  诚实汇报值得肯定——问题出在后续决策,不在记录。

## 其他发现

- [MINOR] WORKLOG 违反 append-only:T0.3 条目插在 T0.1 与 T0.4 之间(应追加在文末);
  各条目 "commit: 待提交" 仍未回填真实 sha(F8 项,T0.5 一并处理);
- [MINOR] `ue_summary.warning_count=1298`(DirectoryWatcher/inotify 为主)——同意 WORKLOG 的
  "T0.5/T0.6 单独治理",届时给 summarize 加已知噪声过滤清单;
- F5/F6/F7 尚未动工,符合既定顺序,继续按小 commit 逐个来。

## 下一步(唯一主线)

修 SceneCapture 近黑(上面 1→5)→ `uef render smoke` 产出**真场景图** → 更新 WORKLOG
(新增条目,不改旧条目)→ 然后才轮到 F5/F6/F7。T0.6/T0.7 维持冻结。
