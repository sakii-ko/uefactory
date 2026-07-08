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

## [2026-07-08] T0.1 Python 包骨架 + uef doctor — DONE
- 分支/commit: feat/m0-skeleton @ 7d933da
- 做了什么:
  - 建立 `pyproject.toml`、`src/uefactory/` 包结构和 `uef` Typer CLI 入口,版本为 `0.1.0`。
  - 配置统一收口到 `core/config.py`,优先级为 `UEF_*` 环境变量 > `uef.toml` > 默认值。
  - 日志统一由 `core/log.py` 初始化,每次 CLI 调用写 `logs/<UTC时间戳>_<命令>.log`,终端 INFO、文件 DEBUG。
  - `uef doctor` 检查 UE 5.5.4、GPU、Vulkan、磁盘/写速、本地候选盘和 Python 版本,支持 `--json`。
- 验收产物:
  - 命令:`.venv/bin/uef --version` → 退出码 0,输出 `0.1.0`
  - 命令:`.venv/bin/uef doctor` → 退出码 0
  - 日志:`logs/20260708T061918Z_doctor.log`
  - 测试:`tools/check.sh` 全绿,summary:
    ```text
    All checks passed!
    15 files already formatted
    Success: no issues found in 15 source files
    ============================= test session starts ==============================
    platform linux -- Python 3.13.13, pytest-9.1.1, pluggy-1.6.0
    rootdir: /root/nas/bigdata1/cjw/projs/uefactory
    configfile: pyproject.toml
    testpaths: tests
    collected 4 items

    tests/test_config.py ...                                                 [ 75%]
    tests/test_doctor.py .                                                   [100%]

    ============================== 4 passed in 0.04s ===============================
    ```
- 终端输出:
  ```text
  CHECK          STATUS  MESSAGE
  -------------  ------  ------------------------------------------------------------------------
  unreal_engine  OK      UE 5.5.4 found
  gpu            OK      1 GPU(s) available
  vulkan         WARN    NVIDIA Vulkan ICD exists; vulkaninfo not installed
  disk           WARN    Disk checks passed with storage warnings; DDC should prefer a local disk
  python         OK      Python 3.13.13

  Overall: WARN
  ```
- JSON 验证:
  - 命令:`.venv/bin/uef doctor --json > /tmp/uef_doctor_t01.json && .venv/bin/python -m json.tool /tmp/uef_doctor_t01.json`
  - 退出码 0;schema_version=1,status=WARN。
  - UE:`/root/nas/bigdata1/cjw/UnrealEngine_5.5.4`,Build.version=5.5.4,Changelist=40574608。
  - GPU:NVIDIA H100 80GB HBM3,driver 580.126.20,total 81559 MiB,free 11865 MiB(11.59 GiB)。
  - Vulkan:`/etc/vulkan/icd.d/nvidia_icd.json` 存在;`vulkaninfo` 未安装(WARN)。
  - 磁盘:repo/data/default DDC 都在 Ceph `/root/nas/bigdata1`;512 MiB 写速分别为 155.32 / 151.39 / 148.65 MB/s,低于 200 MB/s(WARN)。
  - 候选本地盘:`/anc-init` ext4;网络/共享挂载:`/root/public` fuse.dingofs、`/root/nas/bigdata1` ceph、`/root/nas/fastdata2` gpfs。
- 耗时/坑:
  - `uef doctor --json` 完整 512 MiB 写速测试耗时 10.516s。
  - pre-commit 首次用远端 `ruff-pre-commit` 初始化时 GitHub 访问卡住,改成本地 hook,直接调用 `.venv/bin/python -m ruff`。
- 待决问题:无

## [2026-07-08] T0.3 冒烟渲染 `uef render smoke` — DONE
- 分支/commit: feat/m0-skeleton @ e426feb
- 做了什么:
  - 新增 `uef render smoke [--out out/smoke]`,每次创建 UTC run 目录,写 `job.json`、`ue.log`、`manifest.json` 和 `frame_0000.png`。
  - 新增 `render/ue_runner.py`,统一封装 UE 子进程、超时进程组 kill、stdout/stderr 落盘、Warning/Error 摘要。
  - `Content/Python/uef_smoke.py` 在 UE editor RHI 中创建最小场景 actor,并用 UE Canvas → RenderTarget → PNG export 生成非均匀彩色冒烟图;SceneCapture2D 在当前 headless editor 下输出近黑,不作为最终路径。
  - PNG 校验用 Pillow 检查尺寸、平均亮度 `>5/255`、亮度标准差和 min/max range,避免纯黑或均匀灰图假成功。
  - `runtime_lib_dir`、`ue_home` 改为显式配置;本机系统缺 `libvulkan.so.1`,用 ignored `data/runtime_deps` 中的 Ubuntu `libvulkan1` 小型解包目录注入 `LD_LIBRARY_PATH`,manifest 记录该注入。
- 验收产物:
  - 命令:`.venv/bin/uef render smoke --timeout-sec 120` → 退出码 0
  - 图:`out/smoke/20260708T075029Z/frame_0000.png`(1280x720,mean_luma=57.176,stddev=73.016,min/max=0/219)
  - Manifest:`out/smoke/20260708T075029Z/manifest.json`
    - `status=ok`,UE 5.5.4 changelist 40574608,`duration_sec=28.611`
    - `runtime.enabled=true`, `libvulkan=/root/nas/bigdata1/cjw/projs/uefactory/data/runtime_deps/extracted/usr/lib/x86_64-linux-gnu/libvulkan.so.1`
    - `ddc_dir=/root/nas/bigdata1/cjw/projs/uefactory/data/ddc`, `ue_home=/root/nas/bigdata1/cjw/UE5Home`
    - `ue_summary.error_count=0`, `warning_count=1298`
  - UE log:`out/smoke/20260708T075029Z/ue.log`
  - 测试:`.venv/bin/python -m pytest tests/test_smoke_render.py -m ue` → 退出码 0,`1 passed, 3 deselected in 29.34s`
  - 测试:`tools/check.sh` → 退出码 0,summary:
    ```text
    All checks passed!
    22 files already formatted
    Success: no issues found in 20 source files
    ============================= test session starts ==============================
    platform linux -- Python 3.13.13, pytest-9.1.1, pluggy-1.6.0
    rootdir: /root/nas/bigdata1/cjw/projs/uefactory
    configfile: pyproject.toml
    testpaths: tests
    collected 9 items / 1 deselected / 8 selected

    tests/test_config.py ...                                                 [ 37%]
    tests/test_doctor.py .                                                   [ 50%]
    tests/test_smoke_render.py ...                                           [ 87%]
    tests/test_ue_runner.py .                                                [100%]

    ======================= 8 passed, 1 deselected in 0.08s ========================
    ```
- 耗时/坑:
  - 本机 `/root/nas/fastdata2` 未使用;DDC、runtime 小依赖和输出都在 `bigdata1`,其中 `data/runtime_deps` 约 1.9M,`data/ddc` 约 263M。
  - 系统有 NVIDIA ICD 但无系统 `libvulkan.so.1`;`apt-get install` 无权限,所以只下载/解包 `libvulkan1` 和 `vulkan-tools` 到 ignored `data/runtime_deps`,并用 `uef.toml` 显式配置。
  - `-run=pythonscript` commandlet 下 `export_render_target` 不落盘;改用 `-ExecutePythonScript` editor 路径后可导出。
  - OpenWorld 模板和 `r.GenerateMeshDistanceFields=True` 会拖慢退出;改为 `Template_Default` 并关闭 mesh distance fields。
  - UE editor 当前有大量 DirectoryWatcher/inotify 和少量缺图标 warning,但无 error;后续可在 T0.5/T0.6 单独治理。
- 待决问题:无

## [2026-07-08] T0.4 工程质量基建 — DONE
- 分支/commit: feat/m0-skeleton @ 7d933da
- 做了什么:
  - 在 `pyproject.toml` 配置 ruff(lint+format)、mypy 和 pytest;默认 pytest 跳过 `ue`/`net` 标记。
  - 新增 `tools/check.sh`,优先使用 `.venv/bin/python`,顺序执行 ruff check、ruff format --check、mypy、pytest。
  - 新增 `.pre-commit-config.yaml` 和 `tools/forbid_main_commit.sh`,本地 hook 包含 ruff、format check、禁止 main 直接提交。
- 验收产物:
  - 命令:`tools/check.sh` → 退出码 0,输出见 T0.1 summary。
  - 命令:`.venv/bin/pre-commit install` → 退出码 0,输出 `pre-commit installed at .git/hooks/pre-commit`
  - 命令:`.venv/bin/pre-commit run --all-files` → 退出码 0;当前新增文件尚未进入 git 索引时 ruff hook skipped,禁止 main hook passed。
- 反例校验:
  ```text
  F401 [*] `os` imported but unused
   --> /tmp/uef_ruff_bad_demo.py:1:8
    |
  1 | import os
    |        ^^
    |
  help: Remove unused import: `os`

  Found 1 error.
  [*] 1 fixable with the `--fix` option.
  ```
- 耗时/坑:
  - 直接用系统 `python` 跑 check 会找不到 venv 内的 ruff,已改为优先 `.venv/bin/python`。
- 待决问题:无

## [2026-07-08] T0.2 UE 基础工程 UEFBase — DONE
- 分支/commit: feat/m0-skeleton @ 2368513
- 做了什么:
  - 新增最小 UE 5.5 工程 `ue/UEFBase/UEFBase.uproject`,启用 `PythonScriptPlugin`、`MovieRenderPipeline`、`SequencerScripting`。
  - `Config/DefaultEngine.ini` 显式设置 Vulkan RHI、基础渲染项,并关闭 UDP Messaging、AndroidFileServer、OnlineSubsystem 默认服务、CrashReportClient 隐式上传和 Analytics。
  - 新增 `Content/Python/uef_hello.py` 作为无头 Python commandlet 冒烟脚本。
  - `.uproject` 显式禁用 OnlineSubsystem 系列插件;验证日志中不再出现 `Mounting Engine plugin OnlineSubsystem` / `LogOnline`。
- 验收产物:
  - 工程:`ue/UEFBase/UEFBase.uproject`
  - 配置:`ue/UEFBase/Config/DefaultEngine.ini`
  - UE 脚本:`ue/UEFBase/Content/Python/uef_hello.py`
  - 首次日志:`logs/t02_uefbase_open_first.log`
  - 二次日志:`logs/t02_uefbase_open_after_disable.log`
  - 命令:
    ```text
    env HOME=/root/nas/bigdata1/cjw/UE5Home \
      UE-LocalDataCachePath=/root/nas/bigdata1/cjw/projs/uefactory/data/ddc \
      /root/nas/bigdata1/cjw/UnrealEngine_5.5.4/Engine/Binaries/Linux/UnrealEditor-Cmd \
      /root/nas/bigdata1/cjw/projs/uefactory/ue/UEFBase/UEFBase.uproject \
      -run=pythonscript \
      -script=/root/nas/bigdata1/cjw/projs/uefactory/ue/UEFBase/Content/Python/uef_hello.py \
      -unattended -nopause -nosplash -nullrhi -stdout -FullStdOutLogOutput -NoSound \
      -LocalDataCachePath=/root/nas/bigdata1/cjw/projs/uefactory/data/ddc
    ```
  - 输出摘要:
    ```text
    LogPython: UEFactory UEFBase Python smoke script loaded
    LogPythonScriptCommandlet: Display: Python script executed successfully
    LogInit: Display: Success - 0 error(s), 6 warning(s)
    ```
- DDC/存储方案:
  - `/root/nas/fastdata2` 按 Owner 要求不用于大数据/DDC。
  - `/anc-init` 是本地 ext4 但只读,无法使用。
  - 因无可写本地盘,DDC/Zen 放在 `bigdata1`:命令行 `-LocalDataCachePath=/root/nas/bigdata1/cjw/projs/uefactory/data/ddc`。
  - 验证日志显示 Zen 数据目录为 `/root/nas/bigdata1/cjw/projs/uefactory/data/ddc/Zen`;当前 `data/ddc` 约 257M。
- 耗时/坑:
  - 首次打开:`2:24.51`;当时只传 env,UE 仍默认使用 `/home/chijw/.config/.../Zen/Data`。
  - 修正为 `HOME=/root/nas/bigdata1/cjw/UE5Home` + `-LocalDataCachePath=...` 后,二次打开 `29.723s`,最终在线插件禁用后验证打开 `28.129s`。
  - UE 在 NAS 上会报 DirectoryWatcher warnings,但 commandlet 退出码 0 且 `Success - 0 error(s), 6 warning(s)`。
- 待决问题:无

## [2026-07-08] T0.3 冒烟渲染 SceneCapture 修复 — DONE
- 分支/commit: feat/m0-skeleton @ 5a81f1b
- 做了什么:
  - 正式 `uef render smoke` 从 Canvas 诊断画面改回 SceneCapture2D 场景输出,`manifest.json` 顶层和 job 都标记 `render_kind=scene`,UE 标记测试断言该字段。
  - `DefaultEngine.ini` 关闭自动曝光;场景保留 Cube + DirectionalLight + SkyLight + Plane,使用普通 lit 材质参与光照,不再用 Canvas 写 RT。
  - DirectionalLight/SkyLight 设为 MOVABLE,SkyLight 调 `recapture_sky()`;capture component 开 `always_persist_rendering_state` 并连续 `capture_scene()` 两次预热。
  - 未继续 F5-F7;T0.6/T0.7 仍冻结。
- SceneCapture 近黑排查顺序:
  - Step 1 关自动曝光:已设置 `r.DefaultFeature.AutoExposure=False` 与 `r.DefaultFeature.AutoExposure.ExtendDefaultLuminanceRange=False`。
  - Step 2 灯光 mobility/recapture:已对 DirectionalLight/SkyLight 组件设 MOVABLE,并调用 SkyLight `recapture_sky()`。
  - Step 3 预热:已双次 `capture_scene()`;首次有效验证图 `out/smoke/20260708T090441Z/frame_0000.png` 通过校验(`mean_luma=37.283`,`luma_stddev=29.667`,`ue_summary.error_count=0`)。
  - Step 4 `SCS_BASE_COLOR` 排障:未触发,因为 Step 1-3 后 SceneCapture 正式路径已产出可见场景图。
  - Step 5 HighResShot 退路:未触发,SceneCapture 路线已可用。
- 验收产物:
  - 命令:`.venv/bin/uef render smoke --timeout-sec 900` → 退出码 0
  - 图:`out/smoke/20260708T090839Z/frame_0000.png`(1280x720,`mean_luma=30.990`,`luma_stddev=45.346`,min/max=0/149)
  - Manifest:`out/smoke/20260708T090839Z/manifest.json`
    - `status=ok`,`render_kind=scene`,`duration_sec=27.601`
    - `ue_summary.error_count=0`,`warning_count=1298`
    - `runtime.enabled=true`,`ddc_dir=/root/nas/bigdata1/cjw/projs/uefactory/data/ddc`,`ue_home=/root/nas/bigdata1/cjw/UE5Home`
  - UE log:`out/smoke/20260708T090839Z/ue.log`
  - 测试:`.venv/bin/python -m pytest tests/test_smoke_render.py -m ue` → 退出码 0,`1 passed, 3 deselected in 28.90s`
  - 测试:`tools/check.sh` → 退出码 0,summary:
    ```text
    All checks passed!
    22 files already formatted
    Success: no issues found in 20 source files
    ============================= test session starts ==============================
    platform linux -- Python 3.13.13, pytest-9.1.1, pluggy-1.6.0
    rootdir: /root/nas/bigdata1/cjw/projs/uefactory
    configfile: pyproject.toml
    testpaths: tests
    collected 9 items / 1 deselected / 8 selected

    tests/test_config.py ...                                                 [ 37%]
    tests/test_doctor.py .                                                   [ 50%]
    tests/test_smoke_render.py ...                                           [ 87%]
    tests/test_ue_runner.py .                                                [100%]

    ======================= 8 passed, 1 deselected in 0.10s ========================
    ```
- 耗时/坑:
  - UE 5.5 Python 绑定里的 SceneCapture2D 组件属性是 `capture_component2d`;误用 `scene_capture_component2d` 的首次运行只产生 Python AttributeError,未进入图像排查。
  - 一次斜向相机构图尝试 `out/smoke/20260708T090629Z/frame_0000.png` 被校验器拒绝(`mean_luma=0.095`),原因是取景偏离几何;最终回到已验证的相机轴向,仅保守调整距离/FOV/Cube 尺寸。
  - `/root/nas/fastdata2` 未使用;输出、DDC 和 ignored runtime 小依赖继续放在 `bigdata1`。
- 待决问题:无

## [2026-07-08] F5-F7 中期 review 修正 — DONE
- 分支/commit:
  - F5: feat/m0-skeleton @ dcfa5fa
  - F6: feat/m0-skeleton @ d0f4342
  - F7: feat/m0-skeleton @ 522300e
- 做了什么:
  - F5:doctor 写速测试从 `float | None` 改为结构化 `WriteSpeedResult`;`mkstemp` 失败时不再尝试 unlink `Path('.')`,并让 `check_disk` 对不可写候选路径/写速测试失败返回 FAIL。
  - F6:`UEF_DOCTOR_WRITE_TEST_MIB` 收口进 `DoctorConfig.write_test_mib`,支持 TOML `[doctor].write_test_mib` 与 env 覆盖;doctor 不再直接读取环境变量。
  - F7:将 mount 解析、网络文件系统判断、本地候选盘判断、写速测试抽到 `core/sysinfo.py`,doctor 保留检查编排和呈现。
- 验收产物:
  - F5 测试:`.venv/bin/python -m pytest tests/test_doctor.py` → 退出码 0,`3 passed`
  - F6 测试:`.venv/bin/python -m pytest tests/test_config.py tests/test_doctor.py` → 退出码 0,`6 passed`
  - F7 测试:`.venv/bin/python -m pytest tests/test_doctor.py` → 退出码 0,`3 passed`
  - 测试:`tools/check.sh` → 退出码 0,summary:
    ```text
    All checks passed!
    23 files already formatted
    Success: no issues found in 21 source files
    ============================= test session starts ==============================
    platform linux -- Python 3.13.13, pytest-9.1.1, pluggy-1.6.0
    rootdir: /root/nas/bigdata1/cjw/projs/uefactory
    configfile: pyproject.toml
    testpaths: tests
    collected 11 items / 1 deselected / 10 selected

    tests/test_config.py ...                                                 [ 30%]
    tests/test_doctor.py ...                                                 [ 60%]
    tests/test_smoke_render.py ...                                           [ 90%]
    tests/test_ue_runner.py .                                                [100%]

    ======================= 10 passed, 1 deselected in 0.60s =======================
    ```
- 耗时/坑:
  - 不用 chmod 0555 模拟只读目录,因为当前权限/用户模型可能绕过目录权限;测试改为 monkeypatch `tempfile.mkstemp` 抛 `PermissionError`,精确覆盖 finally bug。
  - 本轮未继续 T0.6/T0.7,也未动远程节点。
- 待决问题:无

## [2026-07-08] T0.5 M0 收尾 — DONE
- 分支/commit: feat/m0-skeleton @ 768e90b
- 做了什么:
  - 清理 F8 杂项:GPU 低显存文案使用配置阈值;`_engine_version` 对缺失/坏 JSON fail-fast;`render_smoke` 统一 `_finalize_manifest` 出口;UE log error 判断去掉重复分支;CLI settings helper 合并到 `cli/_common.py`;`forbid_main_commit.sh` 增加 `UEF_ALLOW_MAIN=1` 逃生口。
  - 落实 review #3 NIT:`uef_smoke.py` 的 mobility 设置失败改为 raise,不再 warning 后继续;UE warning 摘要增加已知噪声过滤清单,DirectoryWatcher 与缺图标 warning 进入 `warning_noise`。
  - 清点 QUESTIONS:登记 `docs/ASSET_ACQUISITION.md` §4 的四个 Owner 待拍板项(Q1-Q4);均不阻塞 M0/M1/M2。
  - M0 产物索引:CLI/doctor/config/log 基建(T0.1/T0.4),UEFBase 工程(T0.2),确定性 SceneCapture 冒烟渲染(T0.3),F5-F7 review 修正,T0.5 收尾全部在 WORKLOG 有证据记录。
- 验收产物:
  - 命令:`.venv/bin/uef render smoke --timeout-sec 900` → 退出码 0
  - 图:`out/smoke/20260708T095059Z/frame_0000.png`(1280x720,`mean_luma=30.990`,`luma_stddev=45.346`,min/max=0/149)
  - Manifest:`out/smoke/20260708T095059Z/manifest.json`
    - `status=ok`,`render_kind=scene`,`duration_sec=59.108`
    - `ue_summary.error_count=0`,`warning_count=6`,`warning_noise_count=1298`
    - `warning_noise.directory_watcher=1293`,`warning_noise.missing_editor_icon=5`
  - UE log:`out/smoke/20260708T095059Z/ue.log`
  - 测试:`.venv/bin/python -m pytest tests/test_smoke_render.py -m ue` → 退出码 0,`1 passed, 4 deselected in 26.67s`
  - 测试:`tools/check.sh` → 退出码 0,summary:
    ```text
    All checks passed!
    25 files already formatted
    Success: no issues found in 23 source files
    ============================= test session starts ==============================
    platform linux -- Python 3.13.13, pytest-9.1.1, pluggy-1.6.0
    rootdir: /root/nas/bigdata1/cjw/projs/uefactory
    configfile: pyproject.toml
    testpaths: tests
    collected 14 items / 1 deselected / 13 selected

    tests/test_config.py ...                                                 [ 23%]
    tests/test_doctor.py ....                                                [ 53%]
    tests/test_smoke_render.py ....                                          [ 84%]
    tests/test_tools.py .                                                    [ 92%]
    tests/test_ue_runner.py .                                                [100%]

    ======================= 13 passed, 1 deselected in 0.12s =======================
    ```
- 给 M1 的建议:
  - 保持 T0.3 建立的确定性不变量:固定曝光、固定采样/预热策略,渲染参数变更若破坏确定性需记录原因。
  - MRQ 多通道实现复用 `run_ue`、manifest 校验和 `warning_noise` 结构;新增 warning 类型不要直接吞,先列入清单再过滤。
  - 继续避免 `/root/nas/fastdata2` 存放 DDC/输出/资产缓存;大数据仍默认落 `bigdata1` 或后续经 Owner 批准的节点目录。
- 耗时/坑:
  - 本轮 `uef render smoke` 过滤后仍有 6 条 `LogHttp`/proxy warning,未列为已知噪声,保留在 manifest 供正式 review 判断。
  - T0.6/T0.7 未解冻,没有触碰远程节点。
- 待决问题:Q1-Q4 已登记在 `docs/QUESTIONS.md`,均不阻塞 M0 正式 review。

REVIEW REQUESTED: feat/m0-skeleton 678ff46

## [2026-07-08] T0.6 远程节点基建 — DONE
- 分支/commit: feat/m0-remote @ 0fd6ae6
- 做了什么:
  - 新增 `core/remote.py`:统一封装 SSH/rsync/tmux,SSH 强制 ControlMaster/ControlPath/ControlPersist/BatchMode;rsync 固定 `-z --partial`;带 `--delete` 的 rsync 先校验远端 `.uef_node` 哨兵且目标必须在 `work_dir` 下。
  - 新增 `uef node init <host>`:远端创建工作目录并写 `.uef_node` 哨兵;复跑保持同一 `node_id` 并返回 `status=existing`。
  - 扩展 `uef doctor --host <name>`:将 sentinel/UE/GPU/Vulkan/disk/Python 探测打包成单条远端 Python 命令,输出与本地 doctor 同风格 JSON,并记录 `transport.ssh_connection_count=1`。
  - 远端 probe 对 `vulkaninfo`/`nvidia-smi` 超时或 OSError 归类为 WARN,避免单个慢探测吞掉完整报告;4090/l40s 存储特判 WARN 已落地。
- 验收产物:
  - 幂等初始化:
    - `.venv/bin/uef node init 4090 --json` 复跑 → `status=existing`,`work_dir=/home/lyf/uef`,`node_id=71ae9e4e8c814bf8bf7740e3c4b0b711`
    - `.venv/bin/uef node init l40s --json` 复跑 → `status=existing`,`work_dir=/root/nas/bigdata1/cjw/uef`,`node_id=b7d2fa4bc1bb40339110cee10204fbe3`
  - 远端 doctor:
    - `.venv/bin/uef doctor --host 4090 --json` → JSON 合法,`status=WARN`,`transport.ssh_alias=4090`,`ssh_connection_count=1`,`command_duration_sec=27.01`
      - checks:`node_sentinel=OK`,`unreal_engine=WARN`(engine 尚未 provision),`gpu=OK`(8x RTX 4090),`vulkan=OK`,`disk=WARN`(共享机器清理提示),`python=OK`
    - `.venv/bin/uef doctor --host l40s --json` → JSON 合法,`status=WARN`,`transport.ssh_alias=l40s`,`ssh_connection_count=1`,`command_duration_sec=0.476`
      - checks:`node_sentinel=OK`,`unreal_engine=WARN`(engine 尚未 provision),`gpu=OK`(1x L40S),`vulkan=OK`,`disk=WARN`(Ceph + 同路径陷阱提示),`python=OK`
  - 单连接日志证据:
    - `logs/20260708T103040Z_doctor_4090.log`: `Starting remote command: ssh` 计数 = 1
    - `logs/20260708T103041Z_doctor_l40s.log`: `Starting remote command: ssh` 计数 = 1
  - 测试:`tools/check.sh` → 退出码 0,summary:
    ```text
    All checks passed!
    28 files already formatted
    Success: no issues found in 26 source files
    ============================= test session starts ==============================
    platform linux -- Python 3.13.13, pytest-9.1.1, pluggy-1.6.0
    rootdir: /root/nas/bigdata1/cjw/projs/uefactory
    configfile: pyproject.toml
    testpaths: tests
    collected 19 items / 1 deselected / 18 selected

    tests/test_config.py ...                                                 [ 16%]
    tests/test_doctor.py ......                                              [ 50%]
    tests/test_remote.py ...                                                 [ 66%]
    tests/test_smoke_render.py ....                                          [ 88%]
    tests/test_tools.py .                                                    [ 94%]
    tests/test_ue_runner.py .                                                [100%]

    ======================= 18 passed, 1 deselected in 0.14s =======================
    ```
- 耗时/坑:
  - 4090 首次远端 doctor 被 `vulkaninfo --summary` 慢探测打断;修正后 probe 把超时归为 WARN 并继续返回完整 JSON。本次最终 4090 `vulkaninfo` 在 25.205s 内成功。
  - 两台远端当前均未 provision UE engine,所以 `unreal_engine` 为 WARN;这是 T0.7 范围,本轮未触碰。
  - 未使用 `/root/nas/fastdata2` 存储远端产物或大数据;本地只读/写 repo、logs、`/tmp` 小 JSON。
  - T0.7 仍冻结,未做 engine provision 或远程冒烟渲染。
- 待决问题:无

REVIEW REQUESTED: feat/m0-remote 0fd6ae6

## [2026-07-08] T0.7 远程引擎 provision + 远程冒烟渲染 — DONE
- 分支/commit:
  - 基建: feat/m0-remote @ 8d86522
  - l40s runtime 修复: feat/m0-remote @ d64848b
- 做了什么:
  - 新增 `uef node provision <host>`:engine zip 走 `rsync -z --partial` 上传,远端 tmux 等 ready marker 后解压,已存在且 `Build.version` 合法则幂等跳过。
  - 新增 `uef render smoke --host <host>`:打包 `ue/UEFBase` 最小工程,推到远端 job 目录,tmux 后台执行 UE smoke,轮询 status JSON,拉回 frame/manifest/ue.log 后本地复用非全黑校验,最后通过受保护 `remove_tree()` 清理远端 job 暂存。
  - 修复 l40s root SSH 下 UE 拒绝 root 启动:远端 prepare 创建/复用 `uef` 用户,用 `setfacl` 给 `/root` 最小 execute ACL,渲染前将 run_dir/DDC/UE_HOME 交给 `uef`,真正 UE 进程通过 `runuser -u uef -- env ...` 执行。
  - 修复 Linux 大小写敏感引擎包兼容问题:prepare 幂等创建 shader 兼容 symlink(`Raytracing -> RayTracing`,`RaytracingSkylightRGS.usf -> RayTracingSkyLightRGS.usf`,`NiagaraStatelessModule_ScaleMeshSizebySpeed.ush -> NiagaraStatelessModule_ScaleMeshSizeBySpeed.ush`)。
  - 扩展 UE log 摘要:预编译引擎缺可选 USD/FeaturePack 资源进入 `error_noise`;缺 PNG、USD `plugInfo.json` 写权限探针、只读 Engine Content 的 `WritePermissions.*` 探针进入 `warning_noise`;真实 `error_count/warning_count` 保持可审计。
  - 完成 T0.6 MINOR:远程 doctor probe 已抽到 `core/remote_probe.py`;tmux live marker 改为整行精确匹配。
- l40s provision 证据:
  - 远端落点:`/root/nas/bigdata1/cjw/uef/engine/`(l40s 自己的 NAS,不是本机同路径)。
  - tmux job:`provision_l40s_20260708T120419Z`,最终 `status=complete`,`phase=extracted`,`tmux_live=false`。
  - 引擎版本:`5.5.4`,Build.version `BranchName=++UE5+Release-5.5`,`Changelist=40574608`。
  - WAN 首次实测:`/root/nas/bigdata1/cjw/Linux_Unreal_Engine_5.5.4.zip` 27085247533 bytes,rsync duration `7567.029s`,带宽 `3.414 MiB/s`(`3.579 MB/s`)。
  - l40s 缺 `unzip`;按 Owner 指示安装小工具 `unzip`。后续 root→`uef` 切换需要 ACL,安装小包 `acl` 以提供 `setfacl`。
- l40s 远程 smoke 验收产物:
  - 命令:`.venv/bin/uef render smoke --host l40s --timeout-sec 1800` → 退出码 0
  - 图:`out/smoke/20260708T145219Z/frame_0000.png`(1280x720,`mean_luma=22.646`,`luma_stddev=30.431`,min/max=0/192)
  - Manifest:`out/smoke/20260708T145219Z/manifest.json`
    - `status=ok`,`render_kind=scene`,`remote_host=l40s`,`job_id=smoke_l40s_20260708T145219Z`
    - `duration_sec=25.893`,`returncode=0`,`run_user=uef`
    - `local_validation.status=ok`,`validated_utc=20260708T145251Z`
    - `ue_summary.error_count=0`,`warning_count=0`
    - `ue_summary.error_noise_count=14`(`missing_optional_usd_plugin=7`,`missing_feature_pack_screenshot=7`)
    - `ue_summary.warning_noise_count=338`(`engine_content_write_permission_probe=124`,`missing_editor_icon=127`,`usd_plugin_metadata_write_permission=87`)
  - UE log:`out/smoke/20260708T145219Z/ue.log`
  - CLI log:`logs/20260708T145219Z_render_smoke.log`
  - tmux/无前台 ssh 证据:
    - `logs/20260708T145219Z_render_smoke.log` line 146:`tmux new-session -d -s uef_smoke_l40s_20260708T145219Z ...`
    - status poll:14:52:20 与 14:52:50 两次短 `ssh ... tmux has-session` 查询,间隔约 30s;渲染期间无前台 UE SSH 进程挂住。
  - 远端暂存清理:
    - log line 426:`rm -rf -- /root/nas/bigdata1/cjw/uef/jobs/smoke_l40s_20260708T145219Z`
    - 复核命令返回:`test ! -e /root/nas/bigdata1/cjw/uef/jobs/smoke_l40s_20260708T145219Z && echo cleaned` → `cleaned`
- 4090 状态:
  - 本轮多次轻量探测均失败:`kex_exchange_identification: Connection closed by remote host`,远端 `27.189.109.208 port 22` 在 SSH 握手阶段关闭连接。
  - 未继续 hammer 4090;按 PLAN 允许顺延,4090 provision/smoke 作为 M1 首任务处理。
- 测试:
  - `tools/check.sh` → 退出码 0,summary:
    ```text
    All checks passed!
    29 files already formatted
    Success: no issues found in 27 source files
    ============================= test session starts ==============================
    platform linux -- Python 3.13.13, pytest-9.1.1, pluggy-1.6.0
    rootdir: /root/nas/bigdata1/cjw/projs/uefactory
    configfile: pyproject.toml
    testpaths: tests
    collected 24 items / 1 deselected / 23 selected

    tests/test_config.py ...                                                 [ 13%]
    tests/test_doctor.py ......                                              [ 39%]
    tests/test_remote.py ......                                              [ 65%]
    tests/test_smoke_render.py ......                                        [ 91%]
    tests/test_tools.py .                                                    [ 95%]
    tests/test_ue_runner.py .                                                [100%]

    ======================= 23 passed, 1 deselected in 0.15s =======================
    ```
- 耗时/坑:
  - l40s SSH 用户是 root,UE 直接拒绝 root 运行;最终采用 root 编排 + 非 root `uef` 执行 UE 的边界。
  - UE 5.5.4 预编译 Linux 包存在若干大小写不一致 shader 引用,在 Linux 大小写敏感 FS 上会导致 GlobalShaders fatal;prepare 阶段用 symlink 兼容,不改大 zip。
  - 远程引擎目录 root-owned 且应保持只读;UE 对 Engine Content 的写权限探针、USD metadata 更新、缺可选模板截图均显式归类为 noise,不吞证据。
  - 未使用 `/root/nas/fastdata2` 存储 engine、DDC、产物或临时大文件;大数据仍落 l40s 自己的 `/root/nas/bigdata1/cjw/uef/`。
- 待决问题:无

REVIEW REQUESTED: feat/m0-remote d64848b

## [2026-07-08] T0.7 review #4 fixes(F10/F11/F12) — DONE
- 分支/commit:
  - 修复: feat/m0-remote @ f19bbeb
- 做了什么:
  - F10:在 `DefaultEngine.ini` 关闭 on-screen debug messages;`uef_smoke.py` 启动时执行 `DisableAllScreenMessages`,并在远景放置 `UEF_Smoke_Backdrop` 遮挡模板 skydome 暴露区域,避免 l40s offscreen capture 出现黄字污染。
  - F11:远程 smoke 清理后把 `cleanup.removed_paths`、`cleanup.verified`、`verify_returncode` 写回本地 manifest;测试覆盖 cleanup manifest 证据。
  - F11:清理 l40s 旧 provision 空目录 `/root/nas/bigdata1/cjw/uef/jobs/provision_l40s_20260708T120419Z`,复核 `test ! -e ... && printf cleaned` → `cleaned`。
  - F12:4090 顺延理由按 review 修正为 WAN engine transfer 约 `3.414 MiB/s` 会占用数小时且 M0 收尾优先;本轮仍做轻量探测,结果为 `kex_exchange_identification: Connection closed by remote host`(rc=255,duration=1.109s),不再把 SSH 不可能连接作为顺延主理由。
- 本地 smoke 复验:
  - 命令:`.venv/bin/uef render smoke --timeout-sec 1800` → 退出码 0
  - 图:`out/smoke/20260708T163651Z/frame_0000.png`(1280x720,`mean_luma=36.866`,`luma_stddev=15.877`,min/max=0/144);目检无 skydome/debug 文本。
  - Manifest:`out/smoke/20260708T163651Z/manifest.json`,`status=ok`,`render_kind=scene`,`duration_sec=30.203`,`ue_summary.error_count=0`,`warning_count=0`。
- l40s 远程 smoke 复验:
  - 命令:`.venv/bin/uef render smoke --host l40s --timeout-sec 1800` → 退出码 0
  - 图:`out/smoke/20260708T163802Z/frame_0000.png`(1280x720,`mean_luma=35.608`,`luma_stddev=11.88`,min/max=0/93);目检无 skydome/debug 文本。
  - Manifest:`out/smoke/20260708T163802Z/manifest.json`
    - `status=ok`,`render_kind=scene`,`remote_host=l40s`,`job_id=smoke_l40s_20260708T163802Z`,`run_user=uef`
    - `local_validation.status=ok`,`validated_utc=20260708T163905Z`
    - `cleanup.status=ok`,`cleanup.verified=true`,`cleanup.verify_returncode=0`
    - `cleanup.removed_paths=["/root/nas/bigdata1/cjw/uef/jobs/smoke_l40s_20260708T163802Z"]`
    - `ue_summary.error_count=0`,`warning_count=0`;noise 仍保留为 `error_noise_count=14`,`warning_noise_count=338`
  - 无前台 SSH 挂住证据:CLI 只启动 `tmux new-session -d -s uef_smoke_l40s_20260708T163802Z`,随后 16:38:03/16:38:33/16:39:03 做短 status poll,拉回产物后 16:39:05 删除并复核远端 job 目录。
- 测试:
  - `tools/check.sh` → 退出码 0
  - summary:`29 files already formatted`;`mypy` success;`23 passed, 1 deselected in 0.16s`
- 耗时/坑:
  - 直接删除模板 sky/atmosphere/fog/light actor 会让本地 SceneCapture 退回近黑图(`out/smoke/20260708T163413Z/frame_0000.png`,mean_luma=0.095),已废弃该方案;最终保留模板环境,只屏蔽/遮挡屏幕文本来源。
  - 未使用 `/root/nas/fastdata2` 存储 engine、DDC、产物或临时大文件。
- 待决问题:4090 provision/smoke 按 PLAN 顺延为 M1 首任务,顺延主因是 WAN 大包传输耗时与 M0 closeout 优先级。

REVIEW REQUESTED: feat/m0-remote f19bbeb

## [2026-07-09] T1.2 MRQ headless 可行性 spike — DONE
- 分支/commit:
  - 实现: feat/m1-render @ 0218a75
- T1.1 机会性探测:
  - 本工作日已做一次 4090 轻量探测,不继续 hammer。
  - 命令:通过 `RemoteHost.from_settings(settings, "4090").run("printf ok", timeout_sec=20, check=False)`。
  - 结果:`returncode=255`,`duration_sec=0.099`;stderr 为 `kex_exchange_identification: Connection closed by remote host` / `Connection closed by 27.189.109.208 port 22`。
- 做了什么:
  - 新增 `uef render mrq-spike [--verify-twice]`,只做 T1.2 spike,不扩展 JobSpec 封装。
  - UE 侧用 `-ExecutePythonScript=uef_mrq_spike.py -NullRHI` 幂等创建 `/Game/UEF/MRQSpike/UEF_MRQ_Spike` LevelSequence 和测试材质;渲染侧用 `/Engine/Maps/Entry -game -RenderOffScreen` + `MoviePipelinePythonHostExecutor` 启动 MRQ runtime executor。
  - LevelSequence 使用 spawnable CineCameraActor、Cube backdrop/foreground、灯光与显式 transform tracks;输出 `MoviePipelineDeferredPass_Unlit` + PNG。
  - 为 MRQ legacy PNG 路径设置 `MoviePipelineColorSetting.disable_tone_curve=True` 且启用空 OCIO configuration,让 PNG 输出避开 sRGB half-float quantization 的随机 dither;setup/render summary 均无真实 warning/error。
  - `ue_runner` 增加两类已知 UE 噪声过滤:`/Engine/PythonTypes` 加载探测 warning、MRQ output path statfs 探测 warning,并补单测。
- 验收产物:
  - 命令:`.venv/bin/uef render mrq-spike --verify-twice` → 退出码 0。
  - 两次 run:
    - `out/mrq_spike/20260708T201149Z`
    - `out/mrq_spike/20260708T201234Z`
  - 每次均产出 8 张 PNG + `manifest.json` + `ue.log` + `ue_setup.log`。
  - manifest 关键字段:`render_kind=mrq_spike`,`status=ok`,`frames_expected=8`,`frames_found=8`,`ue_summary.error_count=0`,`ue_summary.warning_count=0`。
  - 两跑 `frame_luma` 完全一致:
    ```text
    [212.111, 212.111, 212.111, 212.111, 212.111, 212.111, 212.111, 212.111]
    ```
  - 额外像素级复核:8 帧逐帧 `ImageChops.difference` 的 bbox 均为 `None`,extrema 全为 `((0, 0), (0, 0), (0, 0))`;两跑 PNG 像素完全一致。
- 测试:
  - `tools/check.sh` → 退出码 0,summary:
    ```text
    All checks passed!
    33 files already formatted
    Success: no issues found in 28 source files
    collected 24 items / 1 deselected / 23 selected
    23 passed, 1 deselected in 0.58s
    ```
- 可行路径/必需 flags:
  - setup:`UnrealEditor-Cmd UEFBase.uproject -ExecutePythonScript=uef_mrq_spike.py -unattended -nopause -nosplash -NullRHI -stdout -FullStdOutLogOutput -NoSound -LocalDataCachePath=data/ddc`
  - render:`UnrealEditor-Cmd UEFBase.uproject /Engine/Maps/Entry -game -RenderOffScreen -unattended -nopause -nosplash -stdout -FullStdOutLogOutput -NoSound -NoLoadingScreen -windowed -resx=640 -resy=360 -LocalDataCachePath=data/ddc -MoviePipelineLocalExecutorClass=/Script/MovieRenderPipelineCore.MoviePipelinePythonHostExecutor -ExecutorPythonClass=/Engine/PythonTypes.UEFMRQSpikeRuntimeExecutor -LevelSequence=/Game/UEF/MRQSpike/UEF_MRQ_Spike.UEF_MRQ_Spike`
- 耗时/坑:
  - editor/PIE MRQ 路径能出图,但 `QUIT_EDITOR` 后出现 `munmap_chunk()/invalid pointer`;已放弃该路径,改用 runtime PythonHostExecutor。
  - runtime executor 直接跑模板地图会混入默认天空/地面并有轻微 luma drift;改为 `/Engine/Maps/Entry` 后,必须给 spawnable 显式 transform tracks,否则输出全黑。
  - UE 5.5 legacy MRQ PNG 输出源码中对 sRGB half-float 8-bit quantization 会加随机噪声;这是前期两跑 luma 差 `0.001-0.004` 的根因。当前用 ColorSetting/OCIO 规避后像素级一致。
  - setup log 里有 `LogOpenColorIOEditor: Display: Force-disable invalid viewport transform settings.` 的 Display 行,不是 warning/error。
  - 本轮只使用本机 NAS repo、`data/ddc` 与 `out/mrq_spike`;未使用 `/root/nas/fastdata2` 存储引擎、DDC、产物或临时大文件。
- 待决问题:无;T1.2 DoD 已达成,下一步按 PLAN 串行进入 T1.3 JobSpec v1。

## [2026-07-09] T1.3 JobSpec v1 + orbit 相机 — DONE

- 分支/commit:
  - 实现:`feat/m1-render` @ `db6c179`
- 做了什么:
  - 新增 JobSpec v1 YAML 解析与显式校验,字段覆盖 `assets/camera/lighting/passes/output`;未知字段、缺字段和非法值均 fail-fast,错误信息带字段路径。
  - 新增示例 `examples/orbit8.yaml`,当前按 PLAN 只允许 `assets: [builtin:cube]`、`camera.rig: orbit`、`lighting.preset: three_point`、`passes: [beauty_lit]`。
  - 新增 `uef render job <job.yaml> [--verify-twice]`,输出 `out/renders/<run_id>/<asset>/<pass>/frame_*.png`、`manifest.json` 与 UE logs;manifest v2 记录 job 全文、相机、光照、帧 luma 和 UE warning/error summary。
  - UE 侧脚本用 MRQ runtime executor 构建 Cube + floor + 三点光占位场景;单个 CineCameraActor 按 orbit 逐帧 key transform,走 `MoviePipelineDeferredPassBase` 输出 `beauty_lit` PNG。
  - `ue_runner` 增加两类精确噪声过滤:Unreal Trace Server startup warning、MRQ 对 `out/renders/` 输出路径的 statfs probe warning;真实 warning/error summary 仍保持 fail-fast。
- 验收产物:
  - 命令:`.venv/bin/uef render job examples/orbit8.yaml --verify-twice --timeout-sec 1800` → 退出码 0。
  - 两次 run:
    - `out/renders/20260708T205504Z/builtin_cube/beauty_lit`
    - `out/renders/20260708T205549Z/builtin_cube/beauty_lit`
  - 每次均产出 8 张 PNG + `manifest.json` + `ue.log` + `ue_setup.log`。
  - manifest 关键字段:`render_kind=job`,`status=ok`,`asset_id=builtin:cube`,`pass=beauty_lit`,`frames_expected=8`,`frames_found=8`,`ue_summary.error_count=0`,`ue_summary.warning_count=0`。
  - 两跑 `frame_luma` 完全一致:
    ```text
    [49.064, 25.158, 34.813, 25.956, 15.026, 14.16, 12.705, 6.759]
    ```
  - 额外像素级复核:8 帧逐帧 `ImageChops.difference` 的 bbox 均为 `None`,mean diff 为 `0.0`,extrema 为 `(0, 0)`;两跑 PNG 像素完全一致。
  - 日志中复查无 `OCIO INVALID`;OpenColorIO 只有 Display 级别的 resave 提示,不是 warning/error。
- 测试:
  - `tools/check.sh` → 退出码 0,summary:
    ```text
    All checks passed!
    38 files already formatted
    Success: no issues found in 31 source files
    collected 35 items / 1 deselected / 34 selected
    34 passed, 1 deselected in 0.57s
    ```
- 耗时/坑:
  - 初版多 camera cut section 会触发 Sequencer frame range ensure;修正 start/end 顺序后仍会让 MRQ 注册多个 shots,并伴随黑帧,最终改成单 camera cut + 单相机逐帧 key transform。
  - T1.2 的空 OCIO workaround 在 lit pass 中会把黄色 `OCIO INVALID` 字样渲进图片,不能沿用。
  - 单相机方案仍黑帧的根因是 Sequencer transform channel 的旋转顺序不是 Unreal `Rotator(pitch,yaw,roll)`;写入时按 `(roll,pitch,yaw)` 映射后画面恢复正常。
  - 去掉空 OCIO 后 PNG sRGB quantization 仍会造成两跑 luma `0.001` 级漂移;最终用引擎自带 `simple.config.ocio` 的 `Utility - Linear - sRGB` 到自身 identity transform 消除随机漂移,且不产生 `OCIO INVALID` overlay。
  - 本轮只使用本机 NAS repo、`data/ddc` 与 `out/renders`;未使用 `/root/nas/fastdata2` 存储引擎、DDC、产物或临时大文件。
- 待决问题:无;T1.3 DoD 已达成,下一步按 PLAN 串行进入 T1.4 多通道 passes。

## [2026-07-09] T1.4 多通道 passes — DONE

- 分支/commit:
  - 实现:`feat/m1-render` @ `6ef07c1`
- 做了什么:
  - `examples/orbit8.yaml` 扩展为六通道:`beauty_lit`、`beauty_unlit`、`depth`、`normal`、`basecolor`、`object_mask`。
  - JobSpec passes 校验改为支持显式 pass 白名单、拒绝未知 pass 和重复 pass。
  - MRQ runtime executor 改为按 pass 串行提交 subjob,原始输出落到 `_mrq/<pass>/`,再规范化为 `<asset>/<pass>/frame_*.{png,exr}`。
  - depth/mask 采用 16-bit half-float EXR;lit/unlit/normal/basecolor 采用 8-bit PNG。manifest 记录每通道格式、位深、物理通道数和逐帧校验值。
  - 新增通道级校验器:depth 必须有梯度;normal 必须随 orbit 变化且编码范围合理;object_mask 必须是标量 stencil ID 且唯一值为背景+2 物体;lit/unlit 不得逐像素相同;basecolor/lit/unlit 均拒绝近黑或过度均匀帧。
  - UE setup 给 cube/floor 设置 CustomDepth stencil 值 1/2,并生成 job-local post-process 材质读取 `PPI_CUSTOM_STENCIL / 255`,避免使用 UE `CustomStencil` 可视化调色板冒充 ID mask。
- 验收产物:
  - 命令:`.venv/bin/uef render job examples/orbit8.yaml --verify-twice --timeout-sec 2400` → 退出码 0。
  - 两次 run:
    - `out/renders/20260708T215537Z/builtin_cube`
    - `out/renders/20260708T215628Z/builtin_cube`
  - 每次均产出 6 个 pass × 8 帧 + `manifest.json` + `ue.log` + `ue_setup.log`。
  - manifest 关键字段:`status=ok`,`render_kind=job`,`asset_id=builtin:cube`,`frames_found` 六通道均为 8,`setup_summary.error_count=0`,`setup_summary.warning_count=0`,`ue_summary.error_count=0`,`ue_summary.warning_count=0`。
  - 两跑 `frame_luma` 完全一致:
    ```text
    [49.046, 77.297, 34.791, 25.941, 15.01, 14.156, 12.701, 60.521]
    ```
  - 通道格式/首帧校验摘要:
    ```text
    beauty_lit    PNG 8-bit RGB   first_mean=[49.186, 49.052, 48.576]
    beauty_unlit  PNG 8-bit RGB   first_mean=[105.84, 105.84, 105.84]
    depth         EXR 16-bit RGBA  first_mean=[284.11804], unique_values=159
    normal        PNG 8-bit RGB   first_mean=[0.0, 127.041, 127.965]
    basecolor     PNG 8-bit RGB   first_mean=[32.741, 32.741, 32.741]
    object_mask   EXR 16-bit RGBA  first_mean=[0.0039], unique_values=3, scalar_vector=True
    ```
  - 额外复核:用最终 `validate_render_pass()` 对两组产物重算 stable validation payload,两跑完全一致。
- 测试:
  - `tools/check.sh` → 退出码 0,summary:
    ```text
    All checks passed!
    40 files already formatted
    Success: no issues found in 33 source files
    collected 44 items / 1 deselected / 43 selected
    43 passed, 1 deselected in 0.42s
    ```
- 耗时/坑:
  - MRQ additional post-process material pass 不能关闭 main pass;`render_main_pass=False` 时 depth/material 输出为 0 帧。
  - WorldDepth raw pass 名由引擎材质决定为 `FinalImageMovieRenderQueue_WorldDepth`;自定义 pass 名对 normal/basecolor/object_mask 生效。
  - UE 内置 `CustomStencil.CustomStencil` 输出的是调色板颜色,首帧有 5 个颜色向量,不能作为 object ID mask;已改为 job-local `PPI_CUSTOM_STENCIL / 255` 标量材质。
  - normal 是 world-normal buffer,orbit 下红/绿通道均值大幅变化,并非固定“偏蓝”外观;校验器改为验证 orbit-varying channel means 与蓝通道编码范围。
  - MRQ EXR 实际写出 `RGBA`;manifest 如实记录物理通道数为 4,校验统计取第一通道标量值。
  - 本轮只使用本机 NAS repo、`data/ddc` 与 `out/renders`;未使用 `/root/nas/fastdata2` 存储引擎、DDC、产物或临时大文件。
- 待决问题:无;T1.4 DoD 已达成,下一步按 PLAN 串行进入 T1.5 光照预设。

## [2026-07-09] T1.5 光照预设 — DONE

- 分支/commit:
  - 实现:`feat/m1-render` @ `9024c89`
- 做了什么:
  - JobSpec lighting 扩展为 `preset ∈ {three_point, hdri, none}`;`hdri` 可指定 `lighting.hdri`,默认 `studio_small_03_1k`。
  - 新增 `uef acquire hdri`,从 PolyHaven files API 下载 1k HDRI 到 `data/hdri/`,写 metadata,记录 source URL/license/md5,并校验已存在文件。
  - UE setup 按 lighting preset 幂等构建灯光:
    - `three_point`:保留三点光 + SkyLight。
    - `hdri`:导入 job 指定 `.hdr` 为 TextureCube,绑定到 SkyLight specified cubemap。
    - `none`:不 spawn 任何灯光,给 cube/floor 使用 unlit emissive 材质,用于无外部光下验证发光材质。
  - 新增 `examples/orbit8_hdri.yaml`、`examples/orbit8_none.yaml`;校验器支持 `none` preset 的发光区域断言,不再用全局平均 luma 拒绝小面积发光体。
- HDRI 获取:
  - 命令:`.venv/bin/uef acquire hdri --asset-id studio_small_03 --resolution 1k` → 退出码 0。
  - 产物:
    - `data/hdri/studio_small_03_1k.hdr`(1.7M,不入 git)
    - `data/hdri/studio_small_03_1k.json`(metadata,不入 git)
  - metadata:license=`CC0`,source=`https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/1k/studio_small_03_1k.hdr`,md5=`74e6ef69ea9024c2cc25b3a7de8ec2f7`。
- 验收产物:
  - three_point 命令:`.venv/bin/uef render job examples/orbit8.yaml --timeout-sec 1800` → 退出码 0。
    - run:`out/renders/20260708T220541Z/builtin_cube`
    - `frames_found`:六通道均为 8;`frame_luma=[49.046, 77.297, 34.791, 25.941, 15.01, 14.156, 12.701, 60.521]`
  - hdri 命令:`.venv/bin/uef render job examples/orbit8_hdri.yaml --timeout-sec 1800` → 退出码 0。
    - run:`out/renders/20260708T220645Z/builtin_cube`
    - `frames_found.beauty_lit=8`;`frame_luma=[254.536, 198.764, 110.994, 141.161, 159.926, 140.906, 99.452, 133.575]`
  - none 命令:`.venv/bin/uef render job examples/orbit8_none.yaml --timeout-sec 1800` → 退出码 0。
    - run:`out/renders/20260708T221403Z/builtin_cube`
    - `frames_found.beauty_lit=8`;`frame_luma=[0.461, 81.116, 0.461, 0.461, 0.461, 0.461, 0.461, 81.116]`
    - `none` 画面低平均亮度但有 emissive 高亮区域;校验条件为每帧 `max > 32` 且非均匀,不是全局平均 luma。
  - 三个 run 的 `setup_summary.error_count=0`,`setup_summary.warning_count=0`,`ue_summary.error_count=0`,`ue_summary.warning_count=0`;仅有既有 UE 噪声被过滤。
- 测试:
  - `tools/check.sh` → 退出码 0,summary:
    ```text
    All checks passed!
    43 files already formatted
    Success: no issues found in 36 source files
    collected 51 items / 1 deselected / 50 selected
    50 passed, 1 deselected in 0.51s
    ```
- 耗时/坑:
  - PolyHaven API 不带 User-Agent 会返回 403;下载器显式设置 `UEFactory/<version> research downloader`。
  - UE headless 导入 `studio_small_03_1k.hdr` 会生成 `TextureCube`,可直接作为 SkyLight cubemap。
  - 初版 `none` 使用 DefaultLit + 弱 emissive,校验器拒绝近黑是正确的;后改为无灯光 + Unlit emissive 材质,并按 preset 定制 beauty 断言。
  - 本轮下载的 HDRI 是 1.7M 小样例,放在 `data/hdri/`;未使用 `/root/nas/fastdata2` 存储引擎、DDC、产物或临时大文件。
- 待决问题:无;T1.5 DoD 已达成,下一步按 PLAN 串行进入 T1.6 本地/远程统一执行器 + contact sheet + turntable。
