# ADR-002:生产渲染走 MovieRenderQueue,M0 冒烟允许 HighResShot

- 状态:Accepted(2026-07-08, Planner)
- 背景:需要多 pass(lit/unlit/depth/normal/basecolor/mask)、确定性帧输出、16bit 格式。候选:
  - A. MovieRenderQueue(MRQ):官方离线渲染管线,原生支持 GBuffer 通道、stencil layer、高位深输出、Python 脚本化;headless 需 `-game` 模式配合。
  - B. SceneCapture2D + 后处理材质:灵活但每个 pass 都要自己搭材质,色彩管理/抗锯齿要自己管。
  - C. HighResShot 截屏:最简单,只有 beauty,无法出通道。
- 决定:M1 起生产管线用 **A(MRQ)**;**M0 冒烟测试允许用 C**,目的只是验证"headless 引擎能出一张非全黑的图",不代表最终管线。B 作为 MRQ 在 headless 下遇到硬坑时的退路。
- 后果:M1 需要验证 MRQ 在 `-game -RenderOffscreen` 下各通道可用性,验证结论(哪些 flag 必需)必须写进 WORKLOG;若 MRQ 不可行,切 B 需新 ADR。
