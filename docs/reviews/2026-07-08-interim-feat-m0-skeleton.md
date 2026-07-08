# Review(中期): feat/m0-skeleton @ 2368513(T0.1/T0.2/T0.4 已交,T0.3 进行中)

- 类型:**INTERIM**(未收到 REVIEW REQUESTED,Planner 主动抽查;不构成合并结论)
- 验证:Planner 实跑 `tools/check.sh` → 全绿(5 passed, 1 deselected `ue`),与 WORKLOG 声称一致;
  通读全部 src/tests/tools/ue 代码;未跑 `uef render smoke`(T0.3 尚未完成)。
- 总体评价:**架子搭得好**,ue_runner/config/log 三个核心模块干净、职责清楚。以下发现按严重度排序,
  **F2/F3 必须在 T0.3 标记 DONE 之前修掉**,其余并入 T0.5 收尾。

## 发现

### [WITHDRAWN] F1 提交身份 —— 撤回,不是问题
Owner 澄清(2026-07-08):`chijw` 就是 Coder 的正式身份,`sakii-ko` 是 Planner 的身份。
现有提交完全合规,**无需任何改写**。CONVENTIONS §5 已相应修订:作者字段即角色标识
(sakii-ko=Planner,chijw=Coder),Role trailer 不再强制。此条为 Planner 误判,记 Planner 账上。

### [BLOCKER] F2 冒烟渲染的防假成功断言被自己的清屏色打穿
`uef_smoke.py` 把 render target 清成 `(0.03, 0.05, 0.08)`,该底色本身的平均亮度就远超
`_validate_image` 的 `mean_luma > 5` 阈值——**即使场景完全没渲出来,校验也会通过**,断言形同虚设
(正是 PLAN 风险 §5 点名的"假成功")。
**修复**:清屏色改纯黑 `(0,0,0)`;校验在均值之外增加**非均匀性断言**(如 `ImageStat.Stat(L).stddev > 阈值`,
或 min/max 差值),均匀图一律 FAIL;并给 `_validate_image` 补两个纯逻辑 pytest(合成全黑图、合成均匀灰图都必须被拒),不需要引擎。

### [BLOCKER] F3 无出处的静默 fallback:`runtime_deps` 注入 LD_LIBRARY_PATH
`smoke.py:59,79` 检查 `data/runtime_deps/extracted/.../libvulkan.so.1`,存在就悄悄改引擎的
`LD_LIBRARY_PATH`。WORKLOG/QUESTIONS 均无一字记载:这个目录哪来的?为什么需要?系统缺 `libvulkan.so.1`?
这是典型的"静默 fallback 掩埋环境问题":装了就悄悄生效,没装就悄悄不生效,两种运行环境的差异不留痕迹。
**修复**:(a)在 WORKLOG 写清来龙去脉;(b)若确实需要,变成显式配置 `Settings.runtime_lib_dir`(uef.toml 可设),
manifest 里记录是否注入;(c)doctor 增加 `libvulkan.so.1` 可解析检查(ldconfig -p 或显式路径),缺失给 WARN 并提示;
(d)若不需要,删掉这两段。**默认路线是 (c)+(b),不允许保留 if-exists 静默分支。**

### [MAJOR] F4 硬编码绝对路径 `/root/nas/bigdata1/cjw/UE5Home`(smoke.py:57)
违反 CONVENTIONS §2。T0.2 里同样的 HOME 技巧是写在 WORKLOG 命令里的,可以;进了业务代码就必须走配置。
**修复**:加 `Settings.ue_home`(默认可以是这个值,写在 config.py 的默认区),smoke.py 只用 settings。

### [MAJOR] F5 doctor 写速测试:失败被静默吞掉 + finally 有真 bug
`_write_speed_mbps`(doctor.py:302)两个问题:
1. 路径不可写时返回 `None`,`check_disk` 对 None **不产生任何 WARN**——不可写的候选盘应当是 FAIL 级发现,现在被 fallback 抹平;
2. `tmp_path = Path()` 哨兵值是 `Path('.')`,**truthy**;若 `mkstemp` 抛异常(正是不可写时),
   `finally` 会执行 `Path('.').unlink(missing_ok=True)` → `IsADirectoryError` 直接炸掉整个 doctor
   (本机就有现成触发器:只读的 `/anc-init`)。
**修复**:哨兵改 `tmp_path: Path | None = None`;写失败返回明确的失败标记并让 check_disk 产出 FAIL/WARN;
给"候选路径只读"补一个 pytest(tmp_path chmod 0555 即可模拟)。

### [MAJOR] F6 配置出逃:`UEF_DOCTOR_WRITE_TEST_MIB` 绕过 core/config(doctor.py:303)
环境变量直接 `os.environ.get`,违反"配置统一走 config.py"。**修复**:挪进 `DoctorConfig.write_test_mib`。

### [MAJOR] F7 模块边界:doctor.py(429 行)囤积了通用系统探测工具
`_mounts / _mount_for_path / _is_network_fs / _write_speed_mbps / _is_candidate_local_mount`
是 T0.6 远程 doctor 在远端也要用的通用能力,放在 `cli/doctor.py` 里下一步只能复制粘贴。
**修复**:抽到 `core/sysinfo.py`,doctor 只留检查编排与呈现。(趋势管控:cli/ 下文件只做参数解析+调用+输出。)

### [MINOR] F8 杂项(并入 T0.5 一次清掉)
- `check_gpu` 消息硬编码 "8 GiB",实际阈值可配(doctor.py:160);
- `_engine_version` 吞错返回 `{"error": "unavailable"}`——引擎刚跑完,读不到版本应该 raise(smoke.py:203);
- `render_smoke` 约 110 行,失败 manifest 有两条重复路径——抽 `_finalize_manifest(status, ...)` 一个出口;
- `uef_smoke.py:53` `time.sleep(0.5)` 是脆弱的同步方式——至少加注释说明为什么、多久会不够;
- `summarize_ue_log` 里 `"LogPython: Error:" in line` 与 `"Error:" in line` 重复(ue_runner.py:108);
- `_settings_from_context` 在 doctor.py 与 render.py 重复——挪 `cli/_common.py`;
- `forbid_main_commit.sh` 把 Planner 在 main 上的 docs 提交也拦了——加环境变量逃生口
  (如 `UEF_ALLOW_MAIN=1`),本次 Planner 用了 `--no-verify`,以后不想再用;
- WORKLOG 三个条目的 "commit: 待提交" 已过时,补真实 sha(以远端为准)。

## 偏差备案(不算错,但要补记录)
- T0.3 实现走了 SceneCapture2D + RenderTarget 导出,而非 ADR-002 的方案 C(HighResShot)。
  这实际是 ADR-002 里的方案 B 变体,用于冒烟**可以接受**(可控、可导出),但请在 WORKLOG 写明
  选择原因与踩过的坑;Planner 会在 T0.3 DONE 后修订 ADR-002 备注。

## Credit(记入贡献)
- `ue_runner.py` 质量很高:进程组 SIGTERM→SIGKILL 递进、超时透传、日志摘要,一次到位(2368513 前的 WIP);
- T0.2 的 DDC 排查扎实:HOME + `-LocalDataCachePath` 双管齐下,首开 2:24 → 二开 29.7s,证据齐全(2368513);
- config.py 的 env > toml > 默认 优先级实现干净且有测试覆盖(7d933da);
- 日志基建完全符合规范(argv/cwd/git sha/版本首行入日志)。

## 对 Coder 的下一步(顺序执行,勿扩散)
1. ~~F1~~ 已撤回,无需处理;
2. 修 F2/F3(T0.3 的 DONE 前置条件),然后完成 T0.3 并按模板登记 WORKLOG;
3. F4–F7 作为独立小 commit 修掉(每个一个 commit,好 review);
4. F8 并入 T0.5 收尾;
5. 全部完成后走正常 REVIEW REQUESTED 流程。
**T0.6/T0.7(远程节点)在上述完成并通过正式 review 之前不要开工。**
