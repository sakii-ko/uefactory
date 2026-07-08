# Review(中期 #3): feat/m0-skeleton @ 7d2bdc8 —— T0.3 裁定:DONE

- 类型:INTERIM(复审 reopened 的 T0.3;5a81f1b + 7d2bdc8)
- 验证(Planner 亲测,不止读代码):
  - **亲眼查看产物图**:`out/smoke/20260708T090839Z/frame_0000.png` 是真场景渲染
    (棋盘格地面、Cube、透视、投影、着色渐变——几何/光照/阴影/tonemap 全参与);
  - **独立实跑复现**:`.venv/bin/uef render smoke --timeout-sec 900` → 退出码 0,
    产物 `out/smoke/20260708T091652Z/`,图像与 Coder 的运行视觉一致且
    **mean_luma 完全相同(30.990)——确定性渲染成立**(关自动曝光的直接收益);
  - `tools/check.sh` 全绿(8 passed, 1 deselected);
  - WORKLOG:reopened 条目按 append-only 追加文末;"待提交" 已全部回填真实 sha;
  - `DefaultEngine.ini` 确认 `r.DefaultFeature.AutoExposure=False`;
  - manifest `render_kind=scene`,UE 侧测试有断言。

## 结论

T0.3 **DONE**。处方(MOVABLE / recapture_sky / 双次预热 / always_persist_rendering_state /
关自动曝光)全部落实且附带说明性注释。**确定性输出(两次运行 mean_luma 逐位一致)是
本次意外收获的宝贵性质,M1 起视为必须保持的不变量**:固定曝光、固定采样的渲染配置
变更若破坏确定性,需 ADR 说明。

## Credit

- 排障记录质量高:发现 UE 5.5 Python 绑定的组件属性名是 `capture_component2d`
  (误用 `scene_capture_component2d` 只报 AttributeError),这类知识点正是 WORKLOG 的价值;
- **校验器抓住了一次真实失败**(斜向构图偏离几何 → mean_luma=0.095 被拒),
  并如实记录——反例防线从"理论上会红"变成"实际红过",这是本 Sprint 质量文化的转折点;
- 未越界:F5–F7 未动,T0.6/T0.7 未动,fastdata2 未用,完全按主线走。

## 遗留小注(并入 T0.5,不阻塞)

- [NIT] `_set_movable` 的 getattr 链 + 裸 `except Exception`:在 UE 脚本边界可容忍
  (API 表面不稳定),但既然调用方明确知道 actor 类型,可各自直接设对应组件;至少把
  `except Exception` 收窄为 `except Exception as exc` 且失败即 raise(现在是 log_warning
  继续跑——若 mobility 没设上,后面就是又一张近黑图,应 fail fast);
- [NIT] 构图偏功能性(Cube 底部出画)——冒烟无所谓,M1 相机 rig 会系统解决;
- warning_count=1298(DirectoryWatcher 噪声)治理仍挂在 T0.5/T0.6。

## 解冻与下一步

1. **F5 → F6 → F7 解冻**,逐个小 commit(见 review #1);
2. 然后 T0.5 收尾(F8 + 本文两个 NIT + WORKLOG 汇总)→ `REVIEW REQUESTED`;
3. 正式 review 通过合入 main 后,解冻 T0.6/T0.7;
4. ADR-002 已由 Planner 修订:M0 冒烟实际路线为 SceneCapture2D(方案 B 变体)+ 关自动曝光,
   HighResShot 退路未启用;MRQ 仍是 M1 生产管线。
