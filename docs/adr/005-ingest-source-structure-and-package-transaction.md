# ADR-005：M2 导入保留源证据，UE 输出采用单 StaticMesh 事务

- 状态：Accepted（2026-07-10）
- 背景：M2 需要在 headless UE 5.5.4 中导入真实 FBX/glTF/GLB，并证明重复导入不会把旧 package
  破坏或把错误结果写进 catalog。源文件可能有 hierarchy/local transform，但 M2 数据消费者的
  逻辑资产契约是单 StaticMesh。系统没有独立 FBX scene-graph parser，也不能把 importer 的
  隐式行为包装成“已经保存 hierarchy”或“已经完成通用归一化”。

## 决定

1. 原始模型和精确依赖先安全 stage；bundle/content hash、源文件 hash 与 source-structure digest
   在复制前后及 UE 多阶段执行期间持续核对。宿主不做 glTF→FBX 预转换。
2. glTF/GLB 的规范 JSON graph 由宿主 fail-closed 解析并 canonical hash；FBX 明确记录
   `not_available/delegated`，不猜 node、hierarchy 或 local transform。
3. M2 v1 UE 输出 policy 固定为 `flatten_to_single_static_mesh_v1`，并永久记录
   `ue_hierarchy_preserved=false`。源 graph 是 provenance，不是 package hierarchy 承诺。
4. UE 导入入口固定为 `AssetImportTask` 自动 importer。unit/up-axis/handedness 转换委托引擎，
   package pivot 和 package scale 保留；IngestSpec 的 uniform scale 只在 render actor 上应用，
   取景时再按 bounds 做 bottom-center 对齐。
5. package 更新走 candidate/backup transaction：primary UE process 导入并提升 candidate，宿主
   quality gate 后由独立 UE process reload 验真，再由独立 finalize process 删除 transaction。
   失败恢复 backup；commit 点不明确时用独立 inspect process 判定，不猜测结果。
6. durable import manifest 与 catalog import artifact 同步升级到 schema v2；有效成功还必须通过
   `m2_static_mesh_v2`、source provenance、package inventory 与 committed transaction 门禁。

## 后果

- （+）源 hierarchy/transform 是否可观察、UE 输出是否保留 hierarchy 两个问题不再混淆。
- （+）源字节变化、证据伪造、半完成 package 替换或仅修改 catalog 状态都不能形成可跳过成功。
- （+）未来若增加 FBX parser、多 mesh 输出或 hierarchy-preserving scene import，可以通过新
  policy/schema 并存，不会静默改变 M2 消费者语义。
- （−）每个资产至少需要 primary、reload 和 finalize 三次 UE 冷启动；这是当前 durability
  contract 的成本。
- （−）当前 requested uniform scale 不烘焙到 package，package 级单位/pivot 归一化若成为需求，
  必须另立 policy 和迁移，不能沿用现有字段夸大实现。

## 验收证据

- 2026-07-10 fresh batch：11 个开放许可模型（6 GLB、5 FBX）全部导入并渲染成功；11 份
  UE 5.5.4 import log 实际记录 `LogInterchangeEngine`。
- 同一 batch 紧接着重跑，11 个资产均通过完整证据门禁返回 `skipped`。
- CC-BY-4.0 `khronos_box` 证明 untextured + hierarchical source 路径：2-node source graph、
  1 non-identity local transform、`texture_count=0`，输出仍明确为一个 StaticMesh 且
  `ue_hierarchy_preserved=false`。
