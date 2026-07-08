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
- 分支/commit: feat/m0-skeleton @ 待提交
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

## [2026-07-08] T0.4 工程质量基建 — DONE
- 分支/commit: feat/m0-skeleton @ 待提交
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
- 分支/commit: feat/m0-skeleton @ 待提交
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
