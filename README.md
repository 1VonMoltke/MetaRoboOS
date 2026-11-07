# MetaRoboOS

## Agent 辅助开发

开发者 Agent 的项目入口为 [AGENTS.md](AGENTS.md)。仓库内提供三个可组合的 Skill：

- [架构导航](.agents/skills/metaos-architecture/SKILL.md)：定位 OS、Harness、外层资源和测评调度的边界与真实接口。
- [新组件开发](.agents/skills/metaos-component-development/SKILL.md)：开发设备/业务 provider、注册安装，并通过可复制的 CPU 示例验证组装。
- [Harness 组装运行](.agents/skills/metaos-harness-assembly/SKILL.md)：生成工作区、构建内容锁、启动及续跑单任务/campaign。

支持 Skill 调用的 Agent 可使用 `$metaos-architecture`、`$metaos-component-development`、`$metaos-harness-assembly`；也可让 Agent 直接读取对应文件。Skill 包含按需读取的参考资料，不需要把整个架构文档粘贴进提示词。

## 项目状态

以 SenseNova-RoboRSI 的独立 SDK 与插件生命周期为 baseline，逐步构建具身系统服务层和 Agent Factory。更新至 **2026-11-07**：P0 与 P1/P2 基础设施验收通过；已使用本地装配入口、原 Agent/ICL 和外部 API 模型 `deepseek-ai/DeepSeek-V4.1-Flash`，完成真实 LIBERO-Pro 单任务 `libero_spatial_task-t00-i00`、seed 0，原生结果 **success / score=1.0**，303 个物理步，清理确认通过。原 27B 服务仍因远程显存不足未完成指定模型验收。

最近一次真实实验见 [API 评测证据](evidence/libero-api-latest.json) 和 [运行报告](work/libero-api-task00-20261105-02/report.json)。模块位置、Harness 工作区及历史产物保留规则见 [目录与工作区说明](docs/workspace-layout.md)。

2026-11-06 新增 [RoboCasa365 Target50 接入](docs/robocasa-integration.md)：独立设备插件、原 Agent/ICL Harness、离线安装和本地装配脚本已完成，协议与构建验证通过。本次未拉取/构建 RoboCasa 镜像，未运行真实任务或模型推理；见 [接入证据](evidence/robocasa-latest.json)。

2026-11-06 新增 [原 RoboRSI 细粒度 Harness](docs/roborsi-modular.md)：针对 `../RoboRSI`，新增独立 Harness SDK、12 种可发现的组件 provider（默认图选用 11 个）、原 JOINT_POSITION LIBERO 兼容设备、技能包组合与双层声明式构建。真实单任务 `libero_goal_task/0`、seed 0 已完成三角色执行、334 个物理步和 29 次工具调用，原生结果为 **task_failure / score=0.0**；26 项链路、视频和清理验收全部通过。安装和运行方式见接入文档，实施状态见 [交付记录](docs/roborsi-implementation.md)，单任务验收见 [运行证据](evidence/roborsi-modular-latest.json)。未执行大规模测试或 campaign。

2026-11-07 新增 [选定条数的 LIBERO-PRO 测评模块](docs/roborsi-campaign.md)：`campaign.roborsi_libero` 与 `executor.roborsi_local` 组合，支持选择 1–120 条或全部任务、固定随机抽样、逐任务隔离执行、断点续跑及分组汇总。入口为 `./metaos-campaign`，已提供 10 / 20 / 120 条构建。18 项定向检查通过；本次未启动真实批量测评，模块就绪记录见 [campaign 构建验证](evidence/roborsi-campaign-latest.json)。

架构目标见 [MetaOS.md](MetaOS.md)，当前验收边界见 [P1/P2 交付记录](docs/p1p2-delivery.md)，常驻服务与 Harness 的运行方法见 [P1/P2 接口与操作](docs/p1p2-api.md)。P0 的历史范围见 [P0 交付报告](docs/p0-delivery.md) 与 [P0 接口说明](docs/p0-api.md)。原评测式插件示例保留在 [modularize-demo](modularize-demo/README.md)。

统一安装、构建和实验运行的后续架构见 [Harness 架构设计](docs/harness-architecture.md)。完整 `metaos install/build/run` 和远程包目录仍为目标设计。现已增加 [本地资源装配 v1](docs/local-composition.md)：通过 `./metaos-local install/init/plan/build/run` 离线安装已有发行包、引用本机环境与资产、协调现有 OS/Harness。原 RoboRSI 接入进一步使用内层 `metaos.harness-spec/v1` 编译显式业务组件图，再交给外层本地运行器；两者尚不自动求解任意 wheel 依赖或提供跨机器发行包。

## 当前结构

```text
packages/
  metaos-contracts/       资源、能力、状态、命令、租约、SystemSpec、DeviceAPI
  metaos-runtime/         插件发现、依赖图、锁、网关、HTTP API、CLI、离线部署
  metaos-harness/         轻量业务端口、组件发现、Harness 图编译与 Host
plugins/
  mock-arm/              独立设备插件与显式依赖设备的 monitor 示例
  joint-degrees/         显式绑定设备的角度转换 Adapter
  libero-device/         原生 LIBERO-Pro 容器、动作/会话/媒体/评分桥接
  libero-harness/        原 Agent/ICL 与 CaseRunner 的独立客户端应用
  robocasa-device/       原生 RoboCasa365 Target50 容器与网关能力桥接
  robocasa-harness/      原 RoboCasa ICL Agent 与 CaseRunner 的独立客户端
  roborsi-components/    原 RoboRSI 角色、模型、上下文、技能、工具与编排
  roborsi-libero-device/ 原 RoboRSI 的 8D JOINT_POSITION LIBERO 原生执行器
  roborsi-campaign/      选定条数的测评调度、独立 episode 执行及续跑汇总
configs/
  p0-system.yaml         单设备系统
  p0-monitor.yaml        设备 + monitor 两节点依赖图
  managed-mock.yaml      声明资源身份的独立执行进程
  managed-degrees.yaml   Mock + 显式 degrees Adapter
  managed-libero.yaml    原生 LIBERO-Pro、镜像与协议文件
  roborsi-libero.harness.yaml     原 RoboRSI 细粒度组件绑定
  roborsi-libero.experiment.yaml  单任务、seed 与工具预算
scripts/
  bootstrap_p0.py        离线构建、独立安装、冷重建及原 demo 回归
  run_p0_tests.py        已安装包的测试与逐项结果记录
  verify_p0.py           部署搬迁、篡改、插件移除与进程清理验收
  exercise_p0.py         对正在运行的服务执行正常、取消、过期操作
  bootstrap_p1p2.py      故障矩阵、原生镜像/文件搬迁、冷运行与 P0 回归
  bootstrap_harness.py   独立 Harness、真实 ICL/tokenizer 的离线搬迁验收
  exercise_libero.py     原生 7D 动作、错误语义、完整评分/视频验收
  exercise_libero_harness.py  原 CaseRunner + 新网关的真实仿真验收
  exercise_harness_failure.py  原 Agent 连接故障、退出与模型超时验收
  verify_native_media.py  下载图片与原生 MP4 完整解码检查
  verify_p1p2.py         当前源码、交付物和原始证据汇总审核
  metaos_local.py        本地资源池、离线安装与组合 build/run
  bootstrap_roborsi.py   原 RoboRSI 快照、双环境隔离与 wheel 安装
  prepare_roborsi.py     内层组件图与外层 OS/服务/资产装配
  verify_roborsi_run.py  单任务角色、网关、评分、媒体与清理核验
tests/p0/                契约、装配、真实 HTTP/子进程故障测试
tests/p1p2/              独立进程、恢复、fencing、阻塞隔离、显式转换
tests/harness/           原 CaseRunner 协议、媒体、模型超时与续租测试
tests/local/             本地组合、进程生命周期与离线安装测试
evidence/p0-latest.json   P0 验收及原始证据索引
evidence/p1p2-latest.json  P1/P2 基础设施证据与真实 27B 待验收状态
evidence/local-composition-latest.json  本地资源装配验收
evidence/libero-api-latest.json  最近一次真实 API 单任务评测
evidence/cleanup-latest.json     历史产物清理清单
modularize-demo/         原 Model 替换、装配与评测链路回归
work/                    最近一次真实实验、所需安装环境与当前资格验收证据
```

运行依赖方向为：`metaos-runtime → metaos-contracts → roborsi-sdk`；设备插件依赖 `metaos-contracts + roborsi-sdk`，通过 `roborsi.components` entry point 接入。contracts/runtime 为 0.2.0，Mock、degrees Adapter、LIBERO Device 为 0.1.0，独立 Harness 为 0.1.1。runtime 不导入具体设备、原评测器或 Harness；原 Agent 依赖安装在单独的 Harness 环境。OS 环境不安装 `roborsi` core、评测器、Torch 或 NumPy。

当前常驻路径是：`Harness/Client → 公共 Manager → 独立 Worker/Gateway → DeviceAPI → 原生服务`。Worker 持有最终租约、代次与命令状态；管理器恢复时接管原 Worker、撤销旧控制权并对账，未知动作不会自动重放。`serve` 保留 P0 单进程入口；新增 `manage` 才是 P2 常驻模式。

## P1/P2 重建与验收

```bash
cd /data3/xzx/MetaRoboOS
/opt/miniconda3/bin/python -I -B scripts/bootstrap_p1p2.py \
  --output work/p1p2-new-acceptance --native --gpu 2
```

输出目录必须不存在。`--native` 要求 Docker、NVIDIA runtime、可用 GPU 及配置中固定摘要的原生镜像；它会导出镜像归档、搬迁部署物、删除本次临时输入项目、离线重建并运行原协议的 2000 个物理步，再完整回归 P0 与 demo。省略 `--native` 时只验收 CPU Adapter 路径。脚本中的确定性动作不调用模型，输出始终保留 `goal_complete=false`，直到另行取得真实 27B 闭环证据。

独立 Harness 的离线构建、模型服务恢复后的单任务命令和人工验收步骤见 [P1/P2 操作说明](docs/p1p2-api.md)。

## P0 一键重建与验收

冻结依赖闭包当前面向 **CPython 3.11 / Linux x86_64**。启动 Python 需要 pip、setuptools ≥ 67、wheel 和 venv；结束后的部署物只需要同平台 Python 与 venv/ensurepip，安装不访问包索引。

```bash
cd /data3/xzx/MetaRoboOS
/opt/miniconda3/bin/python -I -B scripts/bootstrap_p0.py
```

脚本创建新的 `work/p0-acceptance-*`，不覆盖已有运行。可用 `--output /data3/新目录` 指定不存在的目录。每一步保留 stdout/stderr；全部通过后才更新 `evidence/p0-latest.json`。依赖来自 `modularize-demo/vendor` 的冻结快照，先验证原始哈希，再构建三个新包；来源记录保留在部署物的 `wheel-provenance.json`，不把快照冒充上游原始 wheel。

完整验收包括 SDK-only 安装、装配与执行测试、部署搬迁、删除本次临时输入项目、全新环境离线安装、实际动作、篡改拒绝、插件卸载恢复，以及原 demo 的完整回归。每次保留占用磁盘的独立环境，按需人工归档。

## 人工运行一个系统

完成一次完整验收后，在终端 A 执行：

```bash
cd /data3/xzx/MetaRoboOS
P0_PY=$(/opt/miniconda3/bin/python -I -B -c 'import json; print(json.load(open("evidence/p0-latest.json"))["artifacts"]["cold_python"])')
P0_BUNDLE=$(/opt/miniconda3/bin/python -I -B -c 'import json; print(json.load(open("evidence/p0-latest.json"))["artifacts"]["deployment"])')
P0_SESSION=$(mktemp -d "$PWD/work/p0-manual-XXXXXX")
printf '本次目录：%s\n' "$P0_SESSION"
"$P0_PY" -I -B -m metaos_runtime bundle verify --bundle "$P0_BUNDLE" --installed
"$P0_PY" -I -B -m metaos_runtime credentials --output "$P0_SESSION/credentials.json"
"$P0_PY" -I -B -m metaos_runtime serve \
  --spec "$P0_BUNDLE/system.json" --root "$P0_BUNDLE/project" \
  --lock "$P0_BUNDLE/system.lock.json" --workspace "$P0_SESSION/runtime" \
  --credentials "$P0_SESSION/credentials.json" --ready-file "$P0_SESSION/ready.json"
```

服务在前台运行，打印本机 endpoint。终端 B 切换到项目目录，令 `P0_SESSION` 为终端 A 打印的本次目录，重新读取 `P0_PY`，然后执行：

```bash
P0_PY=$(/opt/miniconda3/bin/python -I -B -c 'import json; print(json.load(open("evidence/p0-latest.json"))["artifacts"]["cold_python"])')
"$P0_PY" -I -B scripts/exercise_p0.py \
  --ready-file "$P0_SESSION/ready.json" --credentials "$P0_SESSION/credentials.json" \
  --mode all --output "$P0_SESSION/actions.json"
```

预期依次出现 `PASS normal`、`PASS cancel`、`PASS expire`。`actions.json` 包含请求、租约代次、接收记录、最终结果与设备状态；`runtime/events.jsonl` 包含动作/租约事件，`runtime/state.sqlite3` 保留命令身份及结果。也可用 `--mode normal`、`cancel` 或 `expire` 单独检查。

终端 A 按 Ctrl-C，检查 `runtime/shutdown.json` 的 `handles_closed=true`、`active_leases=0`，且 `ready.json` 已删除。P0 `serve` 要求新的 workspace；P2 的 `manage --resume` 支持受控恢复，见新操作说明。

## 新插件如何接入

1. 新建独立发行包，依赖 SDK 与 contracts，实现 `describe / plan / prepare / open` 生命周期并注册 entry point。
2. 设备导出实现 `DeviceAPI`；普通组件通过 descriptor 声明能力与依赖端口，SystemSpec 用 `bindings` 明确连接。
3. 非 editable 安装到新 runtime 环境，运行 `plugins`、`plan`，确认能力、版本、配置和依赖校验。
4. 常驻模式在 SystemSpec 中声明导出资源身份，使用 `manage` 与相同 Client 接口执行；将新增 wheel 放入 wheelhouse，再 `bundle export`、冷安装与故障验收。

不需要修改 runtime 注册表。现有 `tool.mock_arm_monitor` 演示依赖端口绑定，`embodiment.joint_degrees` 演示显式动作/状态转换；具体 entry point 以 `plugins` 输出为准。P1/P2 部署支持项目根内声明的文件输入和原生镜像归档；未声明的绝对路径仍拒绝，凭据单独配置。

## 当前验收边界与后续

真实 API 模型的单任务闭环已完成，包含模型响应、网关动作、原生评分、视频和退出清理证据；复跑见 [API 实验说明](docs/libero-api-experiment.md)。指定的原 27B 服务仍待恢复，其验收单独保留。后续推进更多任务/seed、第二仿真环境、真实设备资格、算力调度、长期运行观察与受约束 Factory。当前任务 API 管理的是动作命令，不是完整的 Agent 作业调度器。

系统仍只提供单机、本机 HTTP 与协作插件边界。Worker 进程隔离和原生容器限制不代表恶意插件沙箱、硬实时停止或物理安全保证；强制结束进程不能证明真实硬件已经停止。当前没有多节点调度、GPU 资源池调度或生产设备资格。生成的集中凭据文件用于本机验收，向实际调用方分发时只应给其自身 token；共享同一文件的进程不构成彼此隔离的安全主体。
