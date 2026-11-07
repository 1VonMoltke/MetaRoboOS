# 模块、Harness 工作区与产物保留

更新日期：2026-11-07。项目根目录：`/data3/xzx/MetaRoboOS/`。

## 项目结构

```text
MetaRoboOS/
├── MetaOS.md             # 架构目标与阶段范围
├── README.md             # 当前能力与操作入口
├── metaos-local          # 本地 install/init/plan/build/verify/run
├── packages/             # contracts、runtime、轻量 Harness SDK
├── plugins/              # 设备、原 Agent/Harness 及 RoboRSI 细粒度组件
├── configs/              # SystemSpec 示例
├── assets/               # 静态协议文件
├── scripts/              # 安装、装配、运行、验收脚本
├── tests/                # 各层测试源码
├── docs/                 # 架构、接口和操作文档
├── evidence/             # 当前证据索引、最近一次测试和清理清单
├── modularize-demo/      # 早期模块装配示例源码及冻结输入
└── work/                 # 最新实验、实际依赖与当前资格验收证据
```

## 当前可运行模块与资产

| 内容 | 真实位置（相对项目根目录） |
| --- | --- |
| OS 安装环境 | `work/p1p2-final-03/cold/` |
| 构建/CPU 测试环境 | `work/p1p2-final-03/producer/` |
| OS 离线部署包、wheel、镜像归档 | `work/p1p2-final-03/relocated/deployment/` |
| 原 Agent/Harness 安装环境 | `work/p1p2-harness-011/cold/` |
| Harness 离线部署包、wheel、ICL、tokenizer、任务 | `work/p1p2-harness-011/relocated/harness/` |
| LIBERO 已加载镜像 | 默认 Docker engine，数据目录 `/var/lib/docker` |
| API 凭据来源 | 旁边 SenseNova-RoboRSI 仓库的 `work/libero-pro-pilot/api.env`，值不写入 Bundle |

`packages/`、`plugins/` 是开发源码；本次实验从已安装的环境加载模块。API 模型无需本地模型权重。冻结 vendor 输入保留供离线构建使用。

## 原 RoboRSI 细粒度 Harness 工作区

2026-11-06 新增 `work/roborsi-modular/`，对应 `../RoboRSI`，与下文 SenseNova-RoboRSI 的原 Agent/ICL 实验分别记录。状态以 [RoboRSI 单任务证据](../evidence/roborsi-modular-latest.json) 为准；构建成功和测试通过本身不代表真实仿真任务已完成。

| 内容 | 位置 |
| --- | --- |
| 干净上游快照与注入补丁清单 | `work/roborsi-modular/baseline-v2/roborsi/metaos_source.json` |
| 安装记录及四个 wheel | `work/roborsi-modular/installation.json`、`wheels/` |
| 隔离 Harness / 原生环境 | `work/roborsi-modular/harness-env/` |
| 隔离 OS 环境 | `work/roborsi-modular/os-env/` |
| 当前内层组件图、原生配置、资产和外层资源声明 | `work/roborsi-modular/assembly-retirement-fix/` |
| 当前外层内容锁定 Bundle | `work/roborsi-modular/bundle-retirement-fix/` |
| 已完成并验收的单任务输出 | `work/roborsi-modular/runs/task03/`：334 物理步、29 工具调用，原生 score=0.0，链路和清理验收通过 |
| 16 项小型定向检查 | `graph-tests.log`、`component-tests.log`、`device-tests.log`（均在该工作区根目录） |

LIBERO-PRO、BDDL/init、PyRoKi 依赖环境和 Panda 资产仍通过明确的本地路径引用，Bundle 对实际文件字节做内容锁定；感知权重从已有 cache 物化为普通文件。复制时需要遵循 Bundle 的路径依赖，不能把本机引用包冒充完整离线跨机器发行包。完整操作见 [RoboRSI 接入](roborsi-modular.md)。

`runs/task00/` 保留轨道视角 PNG 被误标为 JPEG 的首轮中断及清理证据，没有有效最终任务评分。`runs/task01/` 已产生真实评分与视频，但 Worker 收尾超时、清理未确认，独立审计 `audit.json` 如实保留失败项。`runs/task02/` 在初始化阶段取消，以补充分离状态锁与进程清理锁的修复；无最终评分，清理确认通过。较早的 `assembly-final/` / `bundle/`、`assembly-mime-fix/` / `bundle-mime-fix/`、`assembly-cleanup-fix/` / `bundle-cleanup-fix/` 是修复前的构建记录；不再引用它们启动新运行。当前可运行产物以证据索引和上表为准。

## RoboRSI 选定条数测评工作区

2026-11-07 新增 `work/roborsi-campaign/`：独立 `harness-env/` 显式继承既有 RoboRSI Harness 环境，新增 campaign wheel；`panel-120.json` 固定任务白名单与 seed/预算，`assembly/` 和 `episode-bundle/` 提供共用的单 episode 运行入口。`campaign-10/`、`campaign-20/`、`campaign-120/` 是三个调度图；使用 `./metaos-campaign run` 执行，真实输出写入该工作区 `runs/`。

当前只完成 18 项定向检查及组装验证，未启动真实批量测评，`runs/` 没有相应任务结果。模块与运行命令见 [选定条数测评](roborsi-campaign.md)，就绪状态见 [构建验证记录](../evidence/roborsi-campaign-latest.json)。该记录与前述单任务真实仿真证据分别保存。

## SenseNova-RoboRSI 原 Agent/ICL 端到端 Harness

工作区：`work/libero-api-task00-20261105-02/`。

```text
libero-api-task00-20261105-02/
├── resources/                 # 本次资源声明
│   ├── libero/                # OS SystemSpec 与解释器路径
│   ├── libero-assets/         # 任务、ICL、tokenizer 引用
│   ├── model-api/             # 公开 API 模型设置
│   ├── libero-harness/        # 应用启动命令、绑定与环境变量名
│   └── libero.json            # 选择并连接四个组件的装配入口
├── bundle/                    # 锁定组合身份，引用本地安装环境与资产
├── run/
│   ├── run.json               # 应用退出码、进程与清理结果
│   ├── managed/               # Manager/Worker 与原生容器记录
│   └── results/metaos-7c835a51a785/single-task/
│       ├── agent/             # 配置、上下文身份与模型调用轨迹
│       ├── gateway-commands.jsonl
│       ├── result.json        # 原 CaseRunner 任务结果
│       ├── native/            # score.json 与 episode.mp4
│       └── media/             # 相机观测
└── report.json                # 评分、产物哈希、媒体与清理核验
```

`resources/` 定义如何装配，`bundle/` 固定输入身份，`run/` 保存执行记录。Bundle 使用本地路径引用，不能只复制 `run/` 就获得独立可部署的完整环境。

该实验使用 `deepseek-ai/DeepSeek-V4.1-Flash`，2026-11-05 完成真实任务 `libero_spatial_task-t00-i00`、seed 0：原生 success / score=1.0，303 个物理步、64 个决策步，约 43 分钟，清理确认通过。复跑见 [API 实验说明](libero-api-experiment.md)，证据见 [最新 API 索引](../evidence/libero-api-latest.json)。

## 本次清理后的保留规则

2026-11-06 新增 `work/robocasa-integration-20261106/`，保存 RoboCasa 的 OS/Harness 独立安装环境、离线包、原输入快照、原生镜像构建上下文与协议 fixture 验证。它不含真实任务结果；`fixture-resources/` 和 `fixture-bundle/` 不能作为真实实验直接运行。新增模块位于 `plugins/robocasa-device/`、`plugins/robocasa-harness/`，见 [接入说明](robocasa-integration.md)。下面清理清单记录该次清理时的历史状态，不限制此后新增工作区。

该次清理只保留上述一次原 Agent/ICL 真实实验；后续新增的 RoboRSI 细粒度工作区不在历史清理清单内。另保留运行依赖和每类当前资格验证证据：

- `work/p1p2-final-03/`：当前 OS 安装/部署及 P0/P1/P2 回归，包含最近一次 demo 验收。
- `work/p1p2-harness-011/`：当前 Harness 安装/部署、输入与交付验证。
- `work/p1p2-harness-011-native/`：当前确定性原生协议、媒体和评分验证。
- `work/p1p2-harness-011-failure/`：当前模型连接失败语义验证。
- `work/p1p2-harness-011-agent-exit/`：当前 Agent 退出语义验证。
- `work/p1p2-harness-011-model-timeout/`：当前模型超时语义验证。

后四项是不同故障/协议场景的当前证据，不是额外保留的旧版本实验。清理清单见 [cleanup-latest.json](../evidence/cleanup-latest.json)。更早版本、重复构建、旧资源池、旧人工运行、旧 API 尝试及生成的源码 build 目录已移除；源码、冻结输入、共享 Docker 镜像与旁边的 SenseNova-RoboRSI 仓库保留。

后续运行仍使用新的输出目录。再次清理前，应根据最新 Bundle 的路径声明及证据索引确定依赖，避免删除其仍引用的 Python、部署包或资产。原 27B 模型验收与全基准覆盖仍未完成；当前单任务 API 结果单独记录。
