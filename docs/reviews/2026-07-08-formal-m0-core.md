# Review(正式): M0 核心(T0.1–T0.5) feat/m0-skeleton @ 678ff46

- 结论:**APPROVE** —— merge --no-ff 进 main,打轻量 tag `m0-core-done`。
  (`v0.1.0` 保留给 M0 完整验收:T0.6/T0.7 完成 + Owner 验收之后,按 CONVENTIONS tag 规则。)
- 验证(Planner 亲测,全量重验而非增量):
  - `tools/check.sh` 全绿:13 passed, 1 deselected(ruff + format + mypy 23 文件 + pytest);
  - 独立实跑 `uef render smoke`:成功,`render_kind=scene`,**mean_luma=30.99 第三次逐位一致**
    (确定性不变量三连验证);本次运行 0 条真 warning,1298 条噪声被正确归类计数;
  - 实跑 `uef doctor`(表格 + `--json`,配置化写测生效);
  - 逐条核对 review #1 F1–F8、review #3 两个 NIT:全部关闭(F1 撤回);
  - WORKLOG:append-only 合规、sha 全部回填、每任务证据齐全;QUESTIONS 登记 Q1–Q4。

## 各任务终判

| 任务 | 判定 | 备注 |
|---|---|---|
| T0.1 CLI 骨架 + doctor | DONE | config 三层优先级、日志基建、JSON schema 测试齐 |
| T0.2 UEFBase 工程 | DONE | DDC 方案有实测数据(2:24 → 29.7s),在线子系统禁用 |
| T0.3 冒烟渲染 | DONE(经一次 REOPEN) | 真场景渲染 + 确定性输出;校验器有实战拦截记录 |
| T0.4 质量基建 | DONE | check.sh / pre-commit(含 UEF_ALLOW_MAIN 逃生口 + hook 自身的测试) |
| T0.5 收尾 | DONE | F8 全清;warning 噪声治理为显式清单 + 计数保留,不吞 |

## 值得记录的工程资产(超出任务要求的部分)

1. **确定性渲染不变量**:三次独立运行输出逐位一致——M1 起任何破坏此性质的变更需 ADR;
2. **反 fallback 范式落地**:`WriteSpeedResult`(error ≠ skipped)、mobility fail-fast、
   噪声过滤显式清单化——三个样板,后续 review 以此为基准;
3. `core/sysinfo.py` 的探测集接口,即 T0.6 远程 doctor 的现成载荷。

## 遗留(不阻塞合并,登记去向)

- LogHttp/proxy 瞬态 warning(Coder 运行有 6 条、Planner 复跑 0 条):M1 任务加一条
  "headless 下禁用 HTTP/在线请求于源头"(比过滤更正确);
- `uef.toml.example` 与 README 的使用说明尚薄:T0.6 动工时顺带补;
- Q1–Q4 待 Owner 拍板(均不阻塞 T0.6/T0.7)。

## Credit 汇总(M0 核心)

Coder(chijw)独立完成 M0 核心全部实现,共 11 个实现/文档 commit;经历 1 次 REOPEN 后
的修复质量与过程纪律(不越界、单任务串行、证据完备)显著提升,并沉淀了 3 个可复用的
工程范式。Planner 误判 1 次(F1,已撤回记账)。

## 解冻与下一步

T0.6(远程基建 + 远程 doctor)**解冻,开新分支 `feat/m0-remote`**;完成并过 review 后
T0.7。仍然一次一个任务。T0.6 开工前先读:PLAN §T0.6、ADR-003、ENVIRONMENT「远程渲染节点」、
CONVENTIONS §7(远程纪律)。
