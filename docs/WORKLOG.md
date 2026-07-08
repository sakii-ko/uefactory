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
