# RoboRSI 细粒度接入实施记录

目标：将 `../RoboRSI` 的 Planner / Engineer / Reviewer、模型、上下文、工具、技能、环境、评分、结果及编排拆成可声明组装的组件。完成一次真实 LIBERO 单任务；不执行大规模测试或 campaign。

## 基线与边界

- 上游基线：`da3e855adf0b4b9a2824139f4e924d31d3141e5b`，原始 checkout 保持不变。
- RoboRSI Python >=3.12；OS 使用独立 Python 3.11，避免同名 `roborsi` 包冲突。
- 原技能需要 JOINT_POSITION（7 个关节增量 + gripper）、深度、相机参数及 MuJoCo 内部对象。现有 OSC LIBERO Device 不满足该契约。
- 新增并行的兼容 Device。原技能在设备侧原生环境执行；角色只通过 OS Gateway 发起工具调用。
- frozen 模式，无跨运行能力写回；原生判分仅在所有轮次之后执行。单次任务失败也必须如实保留，不能以应用退出码代替判分。

## 交付检查

- [完成] 干净上游快照、19 处最小依赖注入 seam、原始/适配字节清单；原生 LIBERO reset / 相机 / Joint 控制预检通过。
- [完成] 轻量 Harness SDK：四阶段生命周期、entry point 发现、端口/版本/语义检查、DAG、运行时 Protocol 检查、逆序清理和内容锁。
- [完成] 12 种业务 provider，默认图实例化 11 个；角色、模型、上下文、工具、技能包、环境、评分、结果与编排通过显式绑定组合。
- [完成] 兼容 LIBERO Device / 原生线程执行器 / 工具桥接，复用现有 Manager、Worker、Gateway、租约与命令身份；新增服务只清理本次拥有的进程。
- [完成] 双 Python 环境、非 editable wheel、源码快照安装器与 prepare；感知资产从已有 cache 物化并锁定。离线 Conda 缓存发现已修复。
- [完成] 9 项 SDK 检查 + 4 项业务注入/协议/技能组合/MIME 检查 + 3 项真实子进程生命周期检查，全部通过；未运行大规模回归。
- [完成] `task03` 完成真实 Planner / Engineer / Reviewer、一次 reset、334 个物理步、29 次工具调用、一次最终原生判分和视频生成。原生结果为 `task_failure / success=false / score=0.0 / outcome=budget_exceeded`，如实保留；本次证明组装后的执行链路完成，不代表任务策略成功或论文指标复现。
- [完成] 最终判分后，原生服务正常退出，Device 在独立进程清理锁下等待退出再完成命令，状态读取和租约续期不被阻塞。原生进程 exit=0、未强杀；Worker 句柄关闭、租约为零，外层 `application_completed / cleanup_confirmed=true`。独立验收 26 项全部通过，包括真实视频解码、产物哈希和进程身份检查，见 `evidence/roborsi-modular-latest.json`。

## 当前交付入口

- 操作及扩展协议：`docs/roborsi-modular.md`。
- 声明：`configs/roborsi-libero.harness.yaml`、`configs/roborsi-libero.experiment.yaml`。
- 本机已构建产物：`work/roborsi-modular/assembly-retirement-fix/`、`work/roborsi-modular/bundle-retirement-fix/`。
- 本次运行：`work/roborsi-modular/runs/task03/`；独立证据中的 Bundle fingerprint 为 `1f131b7fb7f2a126b677d12b8d7447570ce4a3e3eba7365f6b654718054c1790`。
- 安装身份和小型检查：`work/roborsi-modular/installation.json`、`installed-byte-audit.json` 及三个定向测试日志。四个 wheel 的 803 个包内文件与实际安装一致。

调试历史保留：`task00` 在 PNG/JPEG MIME 错标处中断，无最终评分；`task01` 已评分但清理未确认，独立审计如实记录失败；`task02` 在初始化阶段取消，用于补充分离状态锁的修复，无最终评分。它们不作为最终验收运行，也不以修改历史结果的方式补过验收。

阶段五的大规模 campaign 不在本次实施范围。

后续追加（2026-11-07）：用户另行要求增加选定条数测评模块，已提供 [campaign 调度组件](roborsi-campaign.md) 和 10 / 20 / 120 条构建；完成定向及组装检查，未运行真实批量测评。此增量不改变上面单任务验收的原始结果与范围。
