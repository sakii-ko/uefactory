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

## [2026-07-09] T1.6 本地/远程统一执行器 + contact sheet + turntable — DONE

- 分支/commit:
  - 实现:`feat/m1-render` @ `d919e73`
- 做了什么:
  - `uef render job <job.yaml> [--host l40s|4090]` 接入统一远程执行入口;JobSpec 与 UE 侧脚本仍共用本地同一份 job JSON,远端仅负责推包、tmux 执行、回收、清理。
  - 每个 job 自动生成 `contact_sheet.png`、`index.html`、`turntable.mp4`;contact sheet 以 pass × view 网格展示 PNG/EXR 预览,turntable 使用 `ffmpeg` 合成 beauty_lit 视角序列。
  - `uef doctor` 增加 ffmpeg 检测;缺失为 WARN,不自动安装。
  - 远程 job 沿用 root 编排 + `uef` 非 root 用户运行 UE,并在本地校验后删除远端暂存目录;cleanup 写回 manifest。
  - UE 日志过滤补充精确规则:`LogCore: Warning: Unable to statfs(.../_mrq/... errno=2)` 归类为 MRQ 输出路径探测噪声,不吞其它 statfs warning。
- 本地验收:
  - 命令:`.venv/bin/uef render job examples/orbit8.yaml --timeout-sec 1800` → 退出码 0。
  - run:`out/renders/20260708T223645Z/builtin_cube`
  - 产物:
    - 六通道 × 8 帧:`beauty_lit`,`beauty_unlit`,`depth`,`normal`,`basecolor`,`object_mask` 均为 8。
    - `contact_sheet.png` 115713 bytes
    - `turntable.mp4` 28276 bytes
    - `index.html` 2802 bytes
  - manifest:`status=ok`,`render_kind=job`,`artifacts={contact_sheet.png,index.html,turntable.mp4}`。
  - `frame_luma=[49.046, 77.297, 34.791, 25.941, 15.01, 14.156, 12.701, 60.521]`。
- l40s 远程验收:
  - 预检:`UEF_DOCTOR_WRITE_TEST_MIB=8 .venv/bin/uef doctor --host l40s --json` → 退出码 0;`unreal_engine/gpu/vulkan` 均 OK,`disk` 为预期 WARN(远端 `/root/nas/bigdata1` 是 l40s 自己的 Ceph)。
  - 命令:`.venv/bin/uef render job examples/orbit8.yaml --host l40s --timeout-sec 2400` → 退出码 0。
  - run:`out/renders/20260708T224403Z/builtin_cube`
  - 产物结构与本地一致:
    - 六通道 × 8 帧均存在并通过本地 pass 校验。
    - `contact_sheet.png` 115713 bytes
    - `turntable.mp4` 28276 bytes
    - `index.html` 2802 bytes
  - manifest:
    - `status=ok`,`remote_host=l40s`,`remote_job_id=render_l40s_20260708T224403Z`,`run_user=uef`
    - `local_validation.status=ok`
    - `cleanup.status=ok`,`cleanup.verified=true`,`cleanup.verify_returncode=0`
    - `cleanup.removed_paths=["/root/nas/bigdata1/cjw/uef/jobs/render_l40s_20260708T224403Z"]`
    - `ue_summary.error_count=0`,`ue_summary.warning_count=0`;`mrq_remote_output_path_probe=264` 被记录为 filtered noise。
  - 无前台 SSH 挂住证据:CLI 只执行短 ssh/rsync 操作,远端渲染由 `tmux new-session -d -s uef_render_l40s_20260708T224403Z` 承载;本地随后仅轮询 `status.json`,完成后 rsync pull 并删除/复核远端 job 目录。
- 本地 vs l40s 初步一致性:
  ```text
  view  local_luma  l40s_luma  delta_pct
  0     49.046      49.046     0.00
  1     77.297      77.297     0.00
  2     34.791      34.791     0.00
  3     25.941      25.941     0.00
  4     15.010      15.010     0.00
  5     14.156      14.156     0.00
  6     12.701      12.701     0.00
  7     60.521      60.521     0.00
  ```
- 测试:
  - `tools/check.sh` → 退出码 0,summary:
    ```text
    All checks passed!
    46 files already formatted
    Success: no issues found in 39 source files
    collected 54 items / 1 deselected / 53 selected
    53 passed, 1 deselected in 0.53s
    ```
- 耗时/坑:
  - 首次 l40s run `out/renders/20260708T223926Z/builtin_cube` 实际产出六通道且 cleanup 成功,但远程脚本把 MRQ `_mrq` 输出路径 `statfs` 探测 warning 当作真实 warning,状态被判 failed;补精确噪声规则后复跑通过。
  - 4090 未跑:PLAN 已把 4090 作为机会性任务,本轮 T1.6 DoD 以 l40s 为远端必验节点。
  - 本轮未使用 `/root/nas/fastdata2`;产物和 DDC 在本机 `bigdata1`,远端临时产物在 l40s 自己的 `/root/nas/bigdata1/cjw/uef/jobs/...` 且已清理。
- 待决问题:无;T1.6 DoD 已达成,下一步按 PLAN 串行进入 T1.7 收尾。

## [2026-07-09] T1.7 M1 收尾 — DONE

- 分支/commit:
  - 禁用在线请求:`feat/m1-render` @ `10b9fe2`
  - README/配置样例:`feat/m1-render` @ `15a47a0`
- 做了什么:
  - 在 UE project config 层关闭 headless 在线请求源头:`[HTTP] bEnableHttp=False`,
    `bUseNullHttp=True`,同时关闭 StudioTelemetry 硬件/OS 数据发送。
  - 新增 `tests/test_ue_project_config.py`,把 HTTP/OnlineSubsystem/Analytics/StudioTelemetry
    的离线约束变成回归测试。
  - README 补「五分钟上手」:安装、`uef.toml`、doctor、本地六通道 job、HDRI job、l40s
    远程 job、产物查看路径。
  - `uef.toml.example` 补运行数据路径、doctor 写入测试、l40s 同路径陷阱和 `/root/nas/fastdata2`
    不放大数据的注释。
- 验收产物:
  - `tools/check.sh` → 退出码 0,summary:
    ```text
    All checks passed!
    47 files already formatted
    Success: no issues found in 40 source files
    collected 55 items / 1 deselected / 54 selected
    54 passed, 1 deselected in 0.52s
    ```
  - 本机 smoke:
    - 命令:`.venv/bin/uef render smoke --timeout-sec 1800` → 退出码 0。
    - run:`out/smoke/20260708T225415Z`
    - manifest:`status=ok`,`render_kind=scene`,`ue_summary.warning_count=0`,`ue_summary.error_count=0`,
      `mean_luma=36.866`。
    - 复查 `ue.log`:无 `LogHttp`、proxy、libcurl、HTTP request failed 匹配。
  - 本机六通道 job:
    - 命令:`.venv/bin/uef render job examples/orbit8.yaml --timeout-sec 1800` → 退出码 0。
    - run:`out/renders/20260708T225541Z/builtin_cube`
    - 六通道 × 8 帧均产出;`contact_sheet.png`、`turntable.mp4`、`index.html` 均存在。
    - manifest:`status=ok`,`ue_summary.warning_count=0`,`ue_summary.error_count=0`,
      `frame_luma=[49.046, 77.297, 34.791, 25.941, 15.01, 14.156, 12.701, 60.521]`。
    - 复查 `ue.log`/`ue_setup.log`:无 `LogHttp`、proxy、libcurl、HTTP request failed 匹配。
- M1 汇总:
  - T1.2 证明 MRQ headless 路线可行并保持确定性;T1.3 落地严格 JobSpec + orbit camera。
  - T1.4 完成 lit/unlit/depth/normal/basecolor/object_mask 六通道和通道级校验。
  - T1.5 完成 `three_point`/`hdri`/`none` 光照预设及最小 HDRI acquire。
  - T1.6 完成本地/远程统一入口、l40s 跑通、contact sheet、index.html、turntable mp4 和远端清理。
  - T1.7 关闭 UE 在线请求源头,补齐 README/配置样例,清点 QUESTIONS。
- 给 M2(资产 ingest)的建议:
  - 先用一个小型本地 glTF/FBX 样例贯通 import → catalog → thumbnail → JobSpec asset id 替换
    `builtin:cube`;不要一上来接 Objaverse 全量。
  - catalog 从第一版开始记录 source URI、license、license tier、original filename、content hash、
    import status、UE package path、thumbnail path;license 字段保持 NOT NULL。
  - 资产缓存、导入产物和缩略图默认继续放 `data/` 与 `out/`,不要使用 `/root/nas/fastdata2` 承载大缓存。
  - 复用 M1 的 render validator 作为 ingest smoke:每个新导入资产至少跑一帧 beauty_lit + object_mask,
    及时暴露尺度、材质、法线和 stencil 问题。
  - 先把失败资产保留结构化 failure record,不要静默跳过;M0/T0.6 的 fail-closed 模式应继续沿用。
- QUESTIONS 清点:`docs/QUESTIONS.md` 待批复为 `(暂无)`;Q1-Q4 均已归档并有 Owner/Planner 答复。
- 待决问题:无;M1 当前分支已可请求 review。

REVIEW REQUESTED: feat/m1-render 43d2163

## [2026-07-10] M1 正式审计、纠错与最终验收 — DONE

- 分支:`feat/m1-render`(正式提交 sha 见本条之后的 git 历史)。
- 说明:本条**追加修正**上方 T1.2–T1.7 的阶段性结论,不改写历史。正式 review 发现旧证据中
  有若干“产物存在但语义/生命周期不够严格”的假成功;全部修复并重新真实运行后才放行。
- 纠正的关键问题:
  - 空 OCIO transform 实际会把黄色 `OCIO INVALID` 字样写进图像;改为 pass-specific 有效 transform,
    并增加重复黄色覆盖层反例检测。
  - PNG/EXR 不能按配置自报格式:现在解码验证真实分辨率、通道、位深、pixel type;RGBA PNG
    原子规范为 RGB,EXR 如实记录 half-float RGBA。
  - 三位小数 luma 不是确定性证明:现在每帧记录 canonical decoded pixel SHA-256,
    `--verify-twice` 比较完整稳定 validation payload。
  - normal 是 **world-space normal**,不是 PLAN 旧文所称固定偏蓝的 tangent-space 外观;契约见 ADR-004。
  - HDRI 初版仅 SkyLight/共享场景不足以证明数据通道干净;改为官方 HDRIBackdrop 材质与
    beauty/data 两个 LevelSequence,环境只进入 beauty/unlit。
  - `none` 初版背景/地面语义不够严格;现在背景近黑且只有 emissive cube 可见,validator 会拒绝亮背景。
  - three-point 改为固定顺序、固定参数的持久 key/fill/rim level actors,解决 MRQ binding/注册顺序
    引起的 beauty FP16 非确定性。
  - runtime normalization/初始化/next-pass 异常始终写失败 manifest 并通知 executor finished,
    不再挂到外层 timeout;失败 manifest 的具体根因不会被 host orchestration 错误覆盖。
  - 本地 Ctrl-C/异常会终止 UE 进程组并清生成资产;cleanup 结果写 manifest,cleanup 失败附加到主异常。
  - 远端 runner 改为上传脚本后用短 tmux command 启动;旧 inline Python 超过 shell 命令长度的失败 run
    `out/renders/20260709T192138Z_0734e04f/` 已安全清理。
  - 远端 stop 使用 PID=PGID/session/start-ticks 身份验证,TERM→KILL 后等待退出;非终态且身份缺失、
    PID 消失或疑似复用时 fail closed,保留 remote tree,绝不误删目录或误杀无关进程。
- 本地确定性验收:
  - 命令:`.venv/bin/uef render job examples/orbit8.yaml --verify-twice --timeout-sec 1800`。
  - runs:
    - `out/renders/20260709T191746Z_b5cf85a4/builtin_cube`
    - `out/renders/20260709T191834Z_cb99c332/builtin_cube`
  - beauty_lit/beauty_unlit/depth/normal/basecolor/object_mask × 8,共 48 帧解码像素哈希逐帧完全一致。
- 光照与通道隔离验收:
  - HDRI beauty 背景:`out/renders/20260709T190912Z_31bd01b4/builtin_cube`。
  - HDRI 六通道:`out/renders/20260709T191403Z_5058d55e/builtin_cube`;beauty/unlit 有环境,
    depth/normal/basecolor/object_mask 与无 HDRI 干净基准逐帧哈希相同。
  - none:`out/renders/20260709T191654Z_2ef29f7b/builtin_cube`;黑背景、无发光地面、emissive cube 可见。
  - 执行代理亲眼检查以上 contact sheet,主体居中落地、8 视角完整、无 overlay、无数据通道 HDRI 污染。
- l40s 真实远程验收:
  - 命令:`.venv/bin/uef render job examples/orbit8.yaml --host l40s --timeout-sec 1800`。
  - run:`out/renders/20260709T192339Z_e408576b/builtin_cube`。
  - `status=ok`,`remote_host=l40s`,六通道 × 8 帧、contact sheet/index/4.000s MP4 全部通过本地复验。
  - 与本地 `20260709T191746Z_b5cf85a4` 的 48 帧 decoded SHA-256 **全部完全一致**;
    8 帧 luma 也完全一致,不是仅在 ±5% 容差内。
  - `cleanup.status=ok`,`cleanup.verified=true`;远端 job tree 删除后以 `test ! -e` 复核。
- 最终回归:
  - `tools/check.sh` → ruff check/format、mypy 全绿;`102 passed, 2 deselected in 17.50s`。
  - `.venv/bin/pytest -m ue -vv` → `2 passed, 102 deselected in 74.40s`。
  - 最终真实 UE run:`out/renders/20260709T194212Z_4313e2b8/builtin_cube`,与确定性基准
    六通道所有帧哈希一致;asset cleanup ok;turntable 4.000s;无遗留 UE 进程/RenderJobs 目录。
  - doctor:本机与 l40s UE 5.5.4/GPU/远程哨兵正常;只有 NAS/DDC 性能与本机缺 `vulkaninfo`
    的预期 WARN,不影响真实 Vulkan 渲染通过。
- 正式结论:`docs/reviews/2026-07-10-formal-m1-render.md` = APPROVE;M1 DoD 完成,
  当前主线切换到 M2 资产摄取。

## [2026-07-10] M2 资产 ingest + scene-level 扩展最终验收 — IMPLEMENTED / REVIEW PENDING

- 分支:`feat/m2-ingest`;本节只记录已经实际发生的实现、测试与运行事实。正式 M2 review、提交、
  merge 与 `v0.3.0` tag 尚未在本节书写时发生,不得由下述证据替代。
- 最终数据契约:
  - catalog schema v3:`assets/artifacts` 加 `scenes/scene_objects/scene_artifacts`;SQLite FK、WAL、
    versioned migration 与原子 finalize 均有正反例。
  - import manifest/artifact schema v2,quality ruleset `m2_static_mesh_v2`。完整 quality policy/check
    集合、bundle/content hash、normalization request、source structure 与 package inventory 进入
    skip key;仅有 `render_ok` 状态不能跳过。
  - glTF/GLB 严格解析 source graph/local transform 并做 canonical domain-separated SHA-256;
    非单位 quaternion、shear/perspective matrix、cycle/多 parent/非法索引 fail closed。FBX 明确
    `not_available/delegated`,不伪造层级证据。UE 输出 policy 为单 StaticMesh flatten,
    `ue_hierarchy_preserved=false`。
  - package 更新使用 candidate/backup:primary import → host quality → 独立 reload → 独立 finalize;
    finalize 不确定时 retry/inspect,失败恢复旧 package 或删除新 destination。
- pinned 模型获取:
  - 命令:`TMPDIR=$PWD/data/tmp uv run uef acquire models --json`。
  - 加入 Khronos `Box.glb` 后首次增量:`downloaded_files=1,reused_files=33`;立即复跑:
    `downloaded_files=0,reused_files=34`。
  - inventory:`11 models / 34 files / 60,003,947 bytes`,格式 `6 GLB + 5 FBX`;10 个 CC0-1.0
    textured 模型 + 1 个 CC-BY-4.0/Cesium attribution 的 untextured Box。所有 size/SHA、固定 commit
    URL 与 license whitelist 均复核。
- 独立审计发现并在正式验收前修复:
  - `textured` 要求原先未绑定 artifact/skip;现保存 exact quality policy,tag 改变会强制重导。
  - quality success 原先未锁定完整 check 集合;现只接受精确当前 12 项且 policy 匹配的报告。
  - glTF 整数值 JSON number 规范为严格 int;bool/分数/非有限拒绝;quaternion 与 TRS-decomposable
    matrix 按规范校验。Box source digest 保持
    `11953fabec8ba8a28add3bac5c31ad4eb554d0651fb0585996c68a15c2be27dc`。
  - 单独收集 scene tests 暴露 `render.job → ingest.__init__ → pipeline → render.job` 循环 import;
    source-evidence validator 改为函数内 lazy import,并加 fresh-interpreter 回归。
  - BlackMyth CLI 不再默认硬编码 `/home` 路径;`uef acquire blackmyth ROOT` 要求显式 root。
    9 个 SceneSpec 使用 `root_env: UEF_BLACKMYTH_ROOT` + 相对路径;未设置、相对 root、path escape
    与 symlink 均 fail closed。实测 Owner 路径扫描 `14 records / 0 quarantined`,9/9 YAML 可解析。
- 失败与安全重跑证据(均不是正式成功批次):
  - 预验收 `out/ingest_batches/20260710T120801Z_5a1c52c6/` 在独立审计发现上述 contract 缺口后
    主动停止。`khronos_boom_box` failure manifest 的根因是宿主 `KeyboardInterrupt`,UE rollback
    `status=ok`;该空 batch 无 manifest,未被计作成功。
  - 第二次预验收 `out/ingest_batches/20260710T122806Z_155980b1/` 中 Water Bottle 几何导入成功,
    但 Ceph DDC 随机产生 4 条精确的 `LogDerivedDataCache ... is very slow ...` warning;fail-closed
    正确触发 rollback,`ue_rollback.log` 约 128.608s 后成功。只为这条完整诊断增加
    `derived_data_cache_slow_io` 窄噪声规则;回放原日志得到 0 未过滤 warning/error、4 条单独计数,
    任意 DDC corrupt/其他 warning 仍失败。
  - 随后为停止该非正式 batch,宿主在 Shelf primary 后被终止,留下 pre-commit backup。正式首跑
    Shelf manifest 明确 `recovered_stale_transaction=true`,随后 reload/finalize/render 全通过;
    验收结束 `/Game/UEF/IngestTransactions` 无残留。
- 正式 11 资产 fresh acceptance:
  - 命令:
    `TMPDIR=$PWD/data/tmp uv run uef ingest batch examples/m2_assets.yaml --database data/catalog_m2_acceptance_release.db --timeout-sec 1800 --json`。
  - 首跑:`out/ingest_batches/20260710T124132Z_96b7fdcc/manifest.json`,约 25 分钟,
    `status=ok`,11/11 `render_ok`;report:
    `out/ingest_batches/20260710T124132Z_96b7fdcc/report/{contact_sheet.png,index.html}`。
  - 紧接着同命令复跑:`out/ingest_batches/20260710T130709Z_c465126e/manifest.json`,约 20 秒,
    `status=ok`,11/11 `skipped`,所有 result 的 ingest/thumbnail manifest 均为 null,没有启动 UE。
  - release DB:`integrity_check=ok`,`foreign_key_check` 0 rows,11 `render_ok`,66 artifacts。每资产
    恰好 1 import manifest + beauty/mask/raw-mask/render-manifest/contact-sheet 5 个 thumbnail artifacts;
    66/66 repo-relative path 存在且 catalog SHA-256 与磁盘一致。
  - 11/11 primary manifest 均 schema v2、quality v2 passed、transaction committed、
    `reload_validation=ok`,`finalize_validation=ok`;11/11 `ue.log` 同时含 Interchange start/completed,
    未过滤 warning/error 均为 0。UE package 路径全部存在并与 catalog mesh/material 统计一致。
  - `catalog stats`:11 assets、66 artifacts、`render_ok=11`,`khronos=6`,`polyhaven=5`,
    `CC0-1.0=10`,`CC-BY-4.0=1`;`catalog list/show/stats` 已在 release DB 真实执行。
  - Box:12 tris、1 material、0 textures;quality 的 texture requirement=false;source graph 精确为
    2 nodes / 1 root / 1 edge / depth 2 / 1 mesh definition/reference / 1 non-identity matrix;
    输出仍明确 flatten 为一个 StaticMesh。
- 模型视觉验收:
  - 执行代理打开总 contact sheet 和全部 11 个逐资产 8-view beauty/mask contact sheet。Avocado
    切面/外皮、Fish 鳍与贴图、Boom Box 天线/高光/cyan 控件、Corset 前后系带、Water Bottle
    标签、Shelf 正背/层板、Side Table 镂空、Picture Frame 正面/背架/薄侧面、School Desk 四腿、
    Vase 不规则瓶口、Box 红色无纹理材质均完整可辨;mask 逐视角匹配,无严重裁切/穿地。
  - 11 个 thumbnail validation 均 passed;最大 background contamination 全为 0。subject area
    minimum 范围 `[0.0233,0.3074]`,median 范围 `[0.0808,0.3217]`。
- Owner 追加的 scene-level / BlackMyth 开放场景:
  - 8 个场景全部为 catalog `render_ok`,合计 748 scene objects、72 scene artifacts;research-only
    `bm_lys_piandian_research` 未混入开放许可验收。
  - `bm_fantasy_diorama`:17 actors / 6 meshes / 10,216 tris / 2 mats / 0 tex,
    render `out/scene_thumbnails/20260710T113520Z_3e4aceb7/scene_bm_fantasy_diorama/manifest.json`。
  - `bm_player_home`:56 / 22 / 39,187 / 10 / 11,
    `out/scene_thumbnails/20260710T113624Z_3c8fb05a/scene_bm_player_home/manifest.json`。
  - `bm_cake_house`:335 / 127 / 35,550 / 10 / 13,
    `out/scene_thumbnails/20260710T113719Z_557db582/scene_bm_cake_house/manifest.json`。
  - `bm_old_church_ruins`:118 / 60 / 355,661 / 16 / 46,
    `out/scene_thumbnails/20260710T113816Z_3f2b99b0/scene_bm_old_church_ruins/manifest.json`。
  - `bm_thunderclap_temple`:19 / 14 / 437,840 / 1 / 3,
    `out/scene_thumbnails/20260710T113910Z_7988db88/scene_bm_thunderclap_temple/manifest.json`。
  - `bm_zelda_tilt_brush_forest`:28 / 14 / 477,968 / 10 / 7,
    `out/scene_thumbnails/20260710T114000Z_68b72794/scene_bm_zelda_tilt_brush_forest/manifest.json`。
  - `bm_zelda_temple_ruins`:47 / 25 / 751,016 / 18 / 13,
    `out/scene_thumbnails/20260710T114056Z_a9a2eaae/scene_bm_zelda_temple_ruins/manifest.json`。
  - `bm_rpg_lowpoly_arena`:128 / 55 / 36,551 / 18 / 49,
    `out/scene_thumbnails/20260710T113052Z_27ee86a9/scene_bm_rpg_lowpoly_arena/manifest.json`。
  - 执行代理打开上述 8 份最终 contact sheet:主体完整,scene mask/expected stencil coverage 通过;
    Tilt Brush 透明笔刷使用显式 coverage policy,RPG 旗帜 alpha 平面使用有界 contamination policy。
- 测试/环境:
  - DDC 修复后 `tools/check.sh`:Ruff check/format、Mypy 全绿;
    `641 passed,2 deselected in 118.35s`。新增版本一致性 targeted test `1 passed`,CLI
    `uef --version` 输出 `0.3.0`;最终全量计数将在正式 review 前再次记录。
  - doctor:UE 5.5.4、H100(约 79.18 GiB free)、存储空间均 OK;Ceph 写速和缺本机
    `vulkaninfo` 为已知 WARN,真实 UE import/render 已通过,不构成阻塞。
  - 当前没有遗留 UE/batch 进程或 ingest transaction。未使用 `/root/nas/fastdata2` 存放大数据。
- 正式 review 前最终门禁:`TMPDIR=$PWD/data/tmp tools/check.sh && git diff --check && uef --version`
  退出码 0;Ruff check/format、Mypy 全绿,`642 passed,2 deselected in 58.80s`,CLI 输出 `0.3.0`。

## [2026-07-10] M2 package-byte / concurrency 纠错与 release 复验 — REVIEW READY

- 说明:本节是对上方 “IMPLEMENTED / REVIEW PENDING” 阶段记录的**追加修正**。旧 fresh/scene
  运行仍是历史事实,但不能替代本节在最终代码上的重建、包字节闭包和并发审计。
- 独立审计发现并关闭的 release 问题:
  - portable SceneSpec 改为 `root_env + relative path` 后,旧 scene catalog generation 仍绑定旧 spec
    SHA；8 个开放场景全部重新 build/reload/finalize/thumbnail,不沿用失配 generation。
  - 旧 model skip/render 证据能确认 imported object/package inventory,但没有把当前磁盘上每个 UE
    package 文件的 bytes 绑定到 generation。现用 `ue_ingested_package_bundle_v1` 递归记录排序后的
    repo-relative path、size、file SHA-256 和 domain-separated bundle digest；拒绝 symlink、路径逃逸、
    空/非 regular 文件和采集期间增删改。import artifact 保存完整闭包,thumbnail 保存 bundle digest,
    skip/render/catalog commit 均重算当前磁盘。
  - finalize 原先会在 host 收集 evidence 后再次保存 destination,存在“验证后改写”窗口。现 finalize
    只清 transaction/backup,不重存 destination；宿主在不可逆 commit 后再完整重算 package bytes。
    不一致会写 durable failed manifest 并标明 committed/no rollback,绝不误报成功。
  - 同一 `asset_id` 的并发 batch/render 原先可能交错 generation。现 outer lease 覆盖 staging、
    existing/skip、UE transaction、catalog 与 thumbnail；model render 持锁到产物完成。其他
    thread/process busy 时返回明确单资产失败且不改 catalog。fork child 重置继承 guard/handle；
    relative `data_dir` 统一解析为 absolute。真实 thread/process/fork contention、late failure、
    KeyboardInterrupt release 与 catalog byte-for-byte unchanged 均有回归。
- 对应提交与独立复核:
  - `e95de22 fix(ingest): bind skips to package bytes`
  - `876a61a fix(ingest): serialize asset generations`
  - package/concurrency reviewer 最终结论 `APPROVE`,BLOCKER/MAJOR/MINOR 均无；补充复验
    related tests、真实 fork timeout、relative data path、Ruff、Mypy 与 `git diff --check` 全绿。

### 8 个 BlackMyth/open scene 在当前 SceneSpec 上重建

- 命令族(每个 YAML 各执行一次):
  - `UEF_BLACKMYTH_ROOT=/home/chijw/workspace/projs/blackmyth TMPDIR=$PWD/data/tmp uv run uef scene build examples/scenes/<scene>.yaml --database data/catalog.db --timeout-sec 1800 --json`
  - `UEF_BLACKMYTH_ROOT=/home/chijw/workspace/projs/blackmyth TMPDIR=$PWD/data/tmp uv run uef scene thumbnail <scene_id> --database data/catalog.db --timeout-sec 1800 --json`
- 当前 generation 的 build / render manifests:
  - `bm_fantasy_diorama`:
    `out/scene_builds/20260710T134109Z_382164d2/bm_fantasy_diorama/manifest.json` /
    `out/scene_thumbnails/20260710T134406Z_9a6c3e94/scene_bm_fantasy_diorama/manifest.json`
  - `bm_player_home`:
    `out/scene_builds/20260710T134517Z_e484c095/bm_player_home/manifest.json` /
    `out/scene_thumbnails/20260710T134635Z_622aca8d/scene_bm_player_home/manifest.json`
  - `bm_cake_house`:
    `out/scene_builds/20260710T134739Z_8b44de8c/bm_cake_house/manifest.json` /
    `out/scene_thumbnails/20260710T134919Z_6203475c/scene_bm_cake_house/manifest.json`
  - `bm_old_church_ruins`:
    `out/scene_builds/20260710T135016Z_7057250a/bm_old_church_ruins/manifest.json` /
    `out/scene_thumbnails/20260710T135231Z_85397dae/scene_bm_old_church_ruins/manifest.json`
  - `bm_thunderclap_temple`:
    `out/scene_builds/20260710T135332Z_799918ef/bm_thunderclap_temple/manifest.json` /
    `out/scene_thumbnails/20260710T135516Z_26a2b981/scene_bm_thunderclap_temple/manifest.json`
  - `bm_zelda_tilt_brush_forest`:
    `out/scene_builds/20260710T135608Z_841de62d/bm_zelda_tilt_brush_forest/manifest.json` /
    `out/scene_thumbnails/20260710T135753Z_e20ae7ca/scene_bm_zelda_tilt_brush_forest/manifest.json`
  - `bm_zelda_temple_ruins`:
    `out/scene_builds/20260710T135902Z_21f92851/bm_zelda_temple_ruins/manifest.json` /
    `out/scene_thumbnails/20260710T140738Z_52b262b6/scene_bm_zelda_temple_ruins/manifest.json`
  - `bm_rpg_lowpoly_arena`:
    `out/scene_builds/20260710T141100Z_3edb1316/bm_rpg_lowpoly_arena/manifest.json` /
    `out/scene_thumbnails/20260710T141342Z_d1da946f/scene_bm_rpg_lowpoly_arena/manifest.json`
- 严格 catalog/package/artifact 审计:`AUDIT_OK scenes=8 objects=748 artifacts=72`。合计 748 actors、
  323 meshes、2,143,989 triangles、85 materials、142 textures、566 UE package files；8 个当前
  SceneSpec SHA、source SHA、build inventory、artifact bytes/params 与磁盘 package 均一致。
- 执行代理打开全部 8 个总/逐 scene contact sheets 和代表性 selected thumbnail：构图完整、视角
  可区分、mask 与主体一致,无裁切/空帧/背景泄漏。fantasy 的 0 texture、Tilt Brush painterly forest、
  Zelda 白色笔触外观来自源素材；RPG 场景是 kit lineup,均未被误判为渲染故障。与 portable 改动前
  视觉基准相比,fantasy sheet bytes 相同,其余 7 个 normalized RMSE 仅
  `0.0000985–0.002314`,无可见回归。

### 最终 11 模型真实 UE fresh + immediate skip

- 获取预检:`TMPDIR=$PWD/data/tmp uv run uef acquire models --json` →
  `11 models / 34 files / 60,003,947 bytes / downloaded=0 / reused=34`。
- fresh 命令:
  `TMPDIR=$PWD/data/tmp uv run uef ingest batch examples/m2_assets.yaml --database data/catalog_m2_package_release.db --timeout-sec 1800 --json`。
- fresh batch:`out/ingest_batches/20260710T145337Z_d8eef9c2/manifest.json`,约 28m46s,
  `status=ok`,11/11 `render_ok`;report:
  `out/ingest_batches/20260710T145337Z_d8eef9c2/report/{contact_sheet.png,index.html}`。
- 同命令立即复跑:`out/ingest_batches/20260710T152241Z_4eaa416b/manifest.json`,11/11
  `skipped`,所有 ingest/thumbnail manifest 为 null,没有 UE log/进程启动。fresh 与 skip 的 11 张
  selected thumbnail、11 张 asset sheet 和总 contact sheet SHA-256 全部一致。
- release DB:`integrity_check=ok`,FK clean,11 assets / 66 artifacts / 全部 `render_ok`；来源
  Khronos=6、Poly Haven=5,许可 CC0-1.0=10、CC-BY-4.0=1。64 个 package files / 68,910,435
  bytes 对 11 个 package roots 构成完整闭包；manifest、import artifact 与当前磁盘重算三方一致。
  总模型统计为 52,458 triangles / 13 materials。
- 11/11 primary/reload/finalize 与 thumbnail setup/render 的未过滤 warning/error 均为 0；每个
  primary `ue.log` 都同时包含 Interchange start importing source / import completed。所有 source
  structure、quality checks、artifact hashes、176 个 beauty/mask frames 的物理格式、decoded pixel
  stats、stencil coverage、frame margin、background contamination 与 subject-area validation 均重新
  解码通过。
- 执行代理打开最新总 contact sheet 与全部 11 张 8-view beauty/mask asset sheets：11 个模型均
  居中、完整、落地；薄侧面视角符合几何，mask 逐帧贴合，无空帧、严重裁切、overlay 或背景泄漏。
- hygiene 审计发现并清理一组只属于 `out/m2_probes/finalize/...` 的孤立诊断包
  `/Game/UEF/Ingested/finalize_commit_probe`；最终 Ingested 根精确等于 DB 的 11 assets，
  `IngestTransactions`、`SceneTransactions`、`RenderJobs` 均为空。只读 SQLite 审计自身产生的空
  `-wal`/`-shm` sidecar 也在连接关闭并确认无占用后清理。

### 当前自动化门禁

- package/lock 修复后的全量 `TMPDIR=$PWD/data/tmp tools/check.sh`：Ruff check/format、Mypy 全绿，
  `675 passed,2 deselected in 72.45s`。
- `git diff --check`、`uef --version` 与 package/concurrency 独立复核均通过；版本为 `0.3.0`。
- release 文档落地后的全量门禁再次通过：100 files formatted、Mypy 91 source files 全绿，
  `675 passed,2 deselected in 139.12s`。
- 本节状态为 `REVIEW READY`；正式 M2 全范围 review、merge 与 tag 仍须由后续追加记录关闭。

## [2026-07-10] M2 正式 review #1:scene generation 闭包 — FIXED / RE-REVIEW PENDING

- review 对象:`feat/m2-ingest @ a1fe3ed`;结论 `REQUEST-CHANGES`,BLOCKER=0、MAJOR=2、
  MINOR/NIT=0。模型、许可、catalog、model package bytes/concurrency、8 场景现有证据与视觉均
  通过；两个问题都位于 scene generation 的持续约束。
- **MAJOR 1 已修复**:standalone `render_job(scene:<id>)` 原先未持 `scene_lock`,可能在 resolver
  验证旧 generation 后与 build 交错。现 scene lease 覆盖 resolver → UE setup/render → host
  artifacts；owning thread 可重入,其他 thread/process 非阻塞 busy；fork child 重置继承的 guard、
  registry 和 handles。scene thumbnail 的外层 lease 与内部 render 可安全嵌套。
- **MAJOR 2 已修复**:旧 scene package evidence 只 hash `inventory.assets` 推导的主文件,没有持续
  拒绝/绑定 root 内额外文件。新 `ue_scene_package_bundle_v1` 递归扫描完整 scene root，要求每个
  inventory `.uasset/.umap`，将同 basename 的 `.uexp/.ubulk/.uptnl` sidecar 纳入 digest，并拒绝
  未知额外文件、缺失/空/非 regular、file/dir symlink 与路径逃逸；两次 scan + 两次 fd hash
  检测采集中增删改。reload 后 evidence 在 finalize 不可逆提交后、catalog commit 前精确重算；
  post-commit mismatch 写 durable failure,明确 `rollback=not_attempted_after_commit`,不碰 catalog。
- 修复提交:`7da5c42 fix(scene): bind renders to complete package generations`，已推送远端。
- 新增反例覆盖:
  - same-thread reentrant、other-thread/process busy、fork guard reset/parent lease contention/release；
  - standalone render 在 busy 时 resolver/UE/out 均未触达；
  - extra known `.ubulk` 被完整记录且改 bytes 后拒绝，unknown extra、missing、empty、file/dir
    symlink、FIFO、path escape、两次 hash 之间 mutation 全部拒绝；
  - finalize 已 committed 后 package mismatch 不 rollback、不写 catalog,失败 manifest 记录不可逆边界。
- 自动化:
  - 首轮相关集:`76 passed`；scene/acquire/render 扩展集:`237 passed`。
  - 最终 `TMPDIR=$PWD/data/tmp tools/check.sh`:102 files formatted、Mypy 93 source files、Ruff 全绿，
    `691 passed,2 deselected in 82.64s`。
- 当前 8 场景兼容性复验:
  - immutable catalog 取当前 generation,新验证器逐 scene 双 scan/hash 全部通过；合计
    `8 scenes / 566 package files / 353,907,808 bytes`，manifest/catalog digest 不变。
  - 新 resolver 对 8/8 返回原 build/package digest 与完整 stencil inventory；因此无需为了约束增强
    重建已正确闭包的 generation。
- 真实 standalone scene render(直接验证 MAJOR 1 路径):
  - 命令:`UEF_BLACKMYTH_ROOT=/home/chijw/workspace/projs/blackmyth TMPDIR=$PWD/data/tmp uv run uef render job out/scene_thumbnail_jobs/20260710T134406Z_fb55c5e9/bm_fantasy_diorama.yaml --database data/catalog.db --timeout-sec 1800`。
  - run:`out/scene_thumbnails/20260710T160842Z_b5ab0f74/scene_bm_fantasy_diorama`；setup
    143.643s、render 42.127s,两个 UE phase 未过滤 warning/error 均为 0；16 个 beauty/mask
    decoded pixel hashes 与修复前 current generation 完全一致,contact sheet SHA-256 同为
    `64f8ccdaa735d07c8a62fe349308377348f47b1260563885f2be5be389a8dc7a`。
  - 执行代理亲眼复查新 contact sheet:8 视角主体完整、mask 对齐,无裁切/空帧/背景泄漏；渲染后
    lease 可立即重取,`RenderJobs` 无残留。
- 当前状态:两个 MAJOR 均有代码、反例、全量回归、现有 8 generation 字节复核和真实 UE/视觉
  证据；等待独立 reviewer 对 `7da5c42` 之后的文档基线复核，未在复核前合并/tag。

## [2026-07-10] M2 正式复审 — APPROVE

- 审查对象:`feat/m2-ingest @ d9dc14de9ba587ee44f650c3b5c72a5d61d00054`。
- 独立 reviewer 复核 7da5c42 的代码/反例和 d9dc14d 的证据文档；结论 `APPROVE`，
  BLOCKER/MAJOR/MINOR/NIT 均为 0。正式报告:
  `docs/reviews/2026-07-10-formal-m2-ingest.md`。
- reviewer 独立结果:changed-related `231 passed in 19.06s`；全量
  `691 passed,2 deselected`；Ruff/format/Mypy/`git diff --check` 全绿；8 场景 566 files /
  353,907,808 bytes 新闭包复算、standalone 16 帧/日志/cleanup/lease 与文档逐项一致。
- 决定:允许 `feat/m2-ingest` 以 `--no-ff` 合入 `main`；release bookkeeping commit 作为
  `v0.3.0` tag target。此条只关闭正式 review，实际 merge/tag/ref push 在后续 release 记录中登记。

## [2026-07-10] M2 merge 与 `v0.3.0` release — COMPLETE

- 正式放行依据:`docs/reviews/2026-07-10-formal-m2-ingest.md`,最终结论 `APPROVE`,未遗留
  BLOCKER/MAJOR/MINOR/NIT。功能分支最终提交为
  `3f46bda17f0788795bfcc3268014a65fbb9bd5de`。
- 实际合并:在同步后的 `main` 执行 `git merge --no-ff feat/m2-ingest`；合并提交为
  `f140f51a81a85fc8ea379b8e0b8e7501fb18a552`,无冲突。随后以本节所在的 release bookkeeping
  提交作为最终 `v0.3.0` tag target,不再修改 M2 功能代码。
- 发布验证基线:
  - 最终功能代码全量 `TMPDIR=$PWD/data/tmp tools/check.sh`:Ruff check/format、Mypy 全绿,
    `691 passed,2 deselected in 82.64s`;独立 reviewer 另行复跑同样得到 `691 passed,2 deselected`。
  - 11 模型 fresh/skip、11 张 contact sheet、64 package files / 68,910,435 bytes、66 artifacts
    与 catalog 三方一致；8 scene generations 共 566 package files / 353,907,808 bytes、72 artifacts,
    且全部 contact sheets 已由执行代理及独立 reviewer 视觉复核。
  - review 修复后的 standalone scene render 成功,16 帧像素 hash 与基线一致；渲染后 lease 可立即
    重取,`IngestTransactions`、`SceneTransactions`、`RenderJobs` 无残留。
- 发布引用约束:本 release bookkeeping commit 是 annotated tag `v0.3.0` 的 peeled target；发布后
  `origin/main`、本地 `main` 与 `v0.3.0^{}` 必须完全相同,CLI 版本必须为 `0.3.0`。提交后立即创建、
  推送并逐项核验这些引用；不以新的 post-tag 文档提交移动发布目标。
