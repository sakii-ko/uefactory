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

## 远程渲染节点(2026-07-08 Planner 实测)

通过 `~/.ssh/config` 别名连接。**三条纪律(Owner 要求,强制)**:
1. 不频繁建 ssh 连接——一切远程操作走 ControlMaster 复用 + 单次批量命令;
2. 长任务必须跑在远端 tmux 里,本机轮询状态文件,不保持前台 ssh;
3. 远端存储与本机**完全不相通**,同名路径 ≠ 同一数据(见 l40s 陷阱)。

### `ssh 4090`(主机名 abc,user lyf;ssh config 已配 ControlMaster)
- **8× RTX 4090 24GB**(实测 1 卡被他人占 ~9GB——这是共享机器,礼貌使用,严禁影响他人任务)
- 96 核 / 503GiB 内存;tmux、rsync、Vulkan ICD 齐;Python 3.10
- 存储 ⚠️ 非常紧张:`/` 938G(剩 438G)、`/home` 6T(**97% 满,剩 209G**)、`/data1` 8.7T(**100% 满**)
- **无 NAS 挂载**→ 数据全靠 rsync;工作目录 `/home/lyf/uef/`,产物拉回后立即清理暂存
- 未发现 UE 安装 → 需一次性 provision(引擎 zip ~数十 GB,WAN 传输,断点续传)

### `ssh l40s`(容器 cci-…,user root)
- **1× L40S 46GB**(全空闲);128 核 / 1TiB 内存;tmux、rsync、Vulkan ICD 齐;Python 3.11
- ⚠️⚠️ **同路径陷阱**:它也有 `/root/nas/bigdata1`(其中甚至也有 `cjw/` 目录),但那是**另一个 CephFS**(298T,内容不同)——本机的 repo/UE 在上面**不可见**。任何脚本禁止假设"路径相同 = 数据相同",rsync/删除前必须校验 `.uef_node` 哨兵文件。
- 可写:`/`(50G overlay,**容器重建即丢**)、`/root/nas/bigdata1/`(它自己的 NAS,剩 173T,持久)
- 只读:`/anc-init`;`/root/public`(NFS,有 datasets/models/script,日后可翻看)
- 容器已运行约 7 天但随时可能重建 → 持久数据只放它的 NAS:工作目录 `/root/nas/bigdata1/cjw/uef/`
- 未发现 UE 安装 → 需一次性 provision

### 其它别名(未探测)
`duan`(走 socks5 代理)、`jz2`、`serv` —— 需要时再探测,不主动连。
