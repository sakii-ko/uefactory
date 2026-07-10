# ADR-004:渲染数据通道契约与跨节点确定性

- 状态:Accepted(2026-07-10)
- 背景:M1 初版曾出现多种“文件存在但数据错误”的假成功:空 OCIO 配置把黄色
  `OCIO INVALID` 覆盖进图片;PNG 实际 RGBA 却宣称 RGB;HDRI 穹顶写入数据通道;
  三位小数 luma 相同却无法证明像素相同;动态生成灯光的注册顺序还会使 FP16 beauty
  在重复运行间变化。里程碑需要把视觉结果、数据语义和确定性都变成可执行契约。

## 决定

1. **按物理文件验真。** 校验器解码每一帧并检查真实扩展名、分辨率、通道数、像素类型与位深;
   PNG 在验收前原子规范为 RGB。manifest 不得只复述 JobSpec 声称的格式。
2. **确定性以解码像素为准。** 每帧记录 canonical decoded pixel SHA-256;
   `--verify-twice` 比较所有 pass 的稳定校验载荷。luma 仅用于可读统计,不再充当相等证明。
3. **数据 pass 语义固定。** `depth` 是 WorldDepth half-float EXR;`normal` 是**世界空间**
   normal 编码,orbit 下 R/G 随视角变化;`basecolor` 是材质基色 RGB PNG;
   `object_mask` 是 `CustomStencil / 255` 标量 half-float EXR。normal 不是切线空间法线贴图,
   因此不要求固定“偏蓝”均值。
4. **HDRI 与数据通道隔离。** HDRIBackdrop 只存在于 beauty LevelSequence;
   depth/normal/basecolor/object_mask 使用不含 backdrop 的 data LevelSequence。禁止依赖
   “不投影/不写 stencil”等属性来推断穹顶不会写 SceneDepth/GBuffer。
5. **确定性灯光是持久 level actor。** three-point 的 key/fill/rim 使用固定参数、固定 spawn 顺序,
   保存进每个 job 的临时 level;不在 MRQ 启动后以不稳定 binding/注册顺序临时创建。
6. **通道级反例门禁。** depth 必须有有效梯度;normal 必须具有合理编码范围和跨 orbit 变化;
   mask 必须有背景/主体分离且主体 bbox 与 beauty 可见区域一致;lit/unlit 不得相同;
   `none` 背景必须保持暗且发光主体可见;已知 OCIO 黄色覆盖层必须被检测。
7. **HDRI beauty/data 双序列是 JobSpec 内部实现细节。** CLI、远程执行器和最终目录结构保持
   单一 JobSpec/统一入口;manifest 仍按逻辑 pass 记录结果。

## 证据

- 本地同 job 两次运行共 48 帧解码像素哈希逐帧一致:
  `out/renders/20260709T191746Z_b5cf85a4/` 与
  `out/renders/20260709T191834Z_cb99c332/`。
- 同一 job 本地与 l40s 的六通道 × 8 帧哈希完全一致:
  `out/renders/20260709T191746Z_b5cf85a4/` 与
  `out/renders/20260709T192339Z_e408576b/`。
- HDRI 六通道 run `out/renders/20260709T191403Z_5058d55e/` 中,data pass 与无 HDRI
  基准逐帧相同,beauty/unlit 保留环境背景。

## 后果

- (+)“能打开文件”不再等价于“通道正确”,格式谎报、覆盖层、环境污染和近似统计碰撞会 fail closed。
- (+)本地与远程 GPU 的一致性可用完整像素证据复核,为 M2 真实资产缩略图和 M4 farm 提供稳定基线。
- (−)manifest 体积增加,每帧需完整解码一次;相较 UE 渲染耗时可忽略。
- (−)世界空间 normal 的消费者必须按本契约解码;若未来需要 tangent/view-space normal,
  必须新增明确 pass 名与 ADR,不能悄悄改变 `normal` 语义。
