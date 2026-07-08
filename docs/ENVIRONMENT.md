# 机器环境事实(ENVIRONMENT)

> Planner 于 2026-07-08 实测。Coder 开工前读一遍,别重复踩坑;发现与实际不符就更新本文件(单独 commit)。

## 硬件 / 系统
- Linux 5.15.0-126-generic,zsh;**192 核 CPU,2015 GiB 内存**。
- GPU:**NVIDIA H100 80GB HBM3**(driver 580.126.20, CUDA 13.0)。
  - ⚠️ 常驻进程 `miniconda3/envs/wm/bin/python3`(非本项目)占用 **~69 GiB** 显存,渲染可用余量约 12 GiB。**不得动这个进程。**
- Vulkan:`/etc/vulkan/icd.d/nvidia_icd.json` 存在;`vulkaninfo` 未安装(想装需走 QUESTIONS)。
- **无 docker**;git 2.x 可用;系统 Python 3.13.13。

## 存储
- `/root/nas/bigdata1`:CephFS NAS,577T 总 / **358T 空闲**。repo 就在这里。
- ⚠️ `/home/chijw/workspace` **同样挂在这个 CephFS 上**——机器可能没有可写的本地盘,T0.1 doctor 需实测确认(遍历非网络挂载点 + 写速测试)。
- DDC(UE DerivedDataCache)对 IO 敏感:若无本地盘,首次 shader 编译会很慢,耗时如实记录。

## Unreal Engine(已就位,不要重新下载)
- **UE 5.5.4 预编译 Linux 版**:`/root/nas/bigdata1/cjw/UnrealEngine_5.5.4/`
  - 可执行:`Engine/Binaries/Linux/UnrealEditor-Cmd`(已确认存在)
  - 另有 `/root/nas/bigdata1/cjw/UE5Home/UnrealEngine/`(内含 UnrealTrace),及原始包 `Linux_Unreal_Engine_5.5.4.zip`。
- 历史实验(**宝贵参考,先读日志再动手**):`/root/nas/bigdata1/cjw/UE5Projects/`
  - `BlankTest/`、`RealisticRender/`:以前的工程;
  - `v2_render.log`:一次渲染的完整日志,结尾正常退出("Daemon is exiting without errors")——说明本机 headless 渲染链路曾经跑通;
  - `render_out/`、`realistic_out*/`、`duan_pt_out/`:历史输出。
  - **只读参考,不要修改该目录任何内容。**

## 项目路径约定
- repo:`/root/nas/bigdata1/cjw/projs/uefactory`(git,main 分支)
- 默认 env:`UEF_UE_ROOT=/root/nas/bigdata1/cjw/UnrealEngine_5.5.4`
- 数据/输出:repo 下 `data/`、`out/`、`logs/`(均已 gitignore)
