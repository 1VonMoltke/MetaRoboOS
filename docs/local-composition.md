# 本地资源池装配与运行

RoboCasa365 的独立资源池生成与相同 `plan/build/verify/run` 接口见 [RoboCasa 接入](robocasa-integration.md)。本次仅完成协议与构建验证，尚未运行真实 RoboCasa 任务。

更新日期：2026-11-06。入口：项目根目录的 `./metaos-local`，只依赖 Python 3.10+ 标准库，使用 Linux 进程管理。

这是已实现的本地引用装配 v1：读取本地清单，检查依赖绑定，固定文件/已安装环境的内容，生成组合 Bundle，再协调现有 Manager、Worker 与 Harness 运行。不访问包平台，不执行 docker pull 或下载模型。`install` 可调用已有离线发行包的安装器，从本地 wheel 安装；build/run 本身不安装依赖。

它复用 OS/Harness 的已有安装器、已安装环境和原生镜像；尚未实现完整架构设计中的任意 Agent、Model、Context、Toolset 内部端口装配。本阶段的 Harness 可以作为具有明确输入的 application 资源注册。该边界不改变 P1/P2 真实 27B 尚待验收的状态。

## 1. 用现有资源直接体验

在当前工作区执行，资源池与 build/run 输出目录均需使用新路径：

```bash
cd /data3/xzx/MetaRoboOS

# 根据 evidence/p1p2-latest.json 注册已验收的本机环境，生成可编辑示例
./metaos-local init --resources work/my-resources

# 可选：只检查依赖、模板和现有 OS plan，不打开设备
./metaos-local plan work/my-resources/mock.json --resources work/my-resources

# 构建并锁定本地输入
./metaos-local build work/my-resources/mock.json \
  --resources work/my-resources --output work/my-mock-build

# 一条命令启动系统、执行动作验证并清理
./metaos-local run work/my-mock-build --output work/my-mock-run --timeout 60
```

Mock 示例执行真实 Manager/Worker HTTP 链路上的正常动作、取消和租约到期检查。查看 `work/my-mock-run/actions.json`，三个 case 应通过；`run.json` 应为 `application_completed` 且 `cleanup_confirmed=true`。

`init` 是当前工作区的资源注册快捷方式，不是下载或安装命令。换机器时可手工编写相同资源清单，或先用已有离线部署包的安装器创建运行环境，再登记实际路径。命令行不要求把本地资源复制到一个大目录，资源清单可以引用已有绝对路径。

若手头是尚未安装的本地 OS/Harness 发行包，可先使用统一的本地安装入口，再把输出的 Python 路径填入资源清单：

```bash
# OS 发行包：含原生镜像归档时可增加 --load-images
./metaos-local install /path/to/os-deployment \
  --target /path/to/new-os-env --python /opt/miniconda3/bin/python --load-images

# Harness 发行包：同样使用本地 wheels/requirements.lock
./metaos-local install /path/to/harness-deployment \
  --target /path/to/new-harness-env --python /opt/miniconda3/bin/python
```

支持已有 `metaos.deployment/v1` 和 `metaos.harness/v1` 发行目录，在执行包内安装器前先验证清单、文件字节和 installer 本身。可传 `--expect-fingerprint` 使用已知外部指纹。安装目标必须是新目录，Python/平台需匹配发行包；安装失败不会自动覆盖或删除目标。该入口不接受一个裸 wheel 目录或任意 tar 自动推断安装方式。

## 2. 本地资源池

```text
my-resources/
  mock/resource.json              # OS Python + SystemSpec
  mock-check/resource.json        # 已安装应用入口
  libero/resource.json            # LIBERO OS 配置
  libero-assets/resource.json     # 原 ICL、tokenizer、task
  libero-harness/resource.json    # 原 Agent/Harness 启动方式
  model-api/resource.json         # 已存在模型 API 的公开设置
  mock.json                       # Mock 组合声明
  libero.json                     # LIBERO 组合声明
```

每个资源通过一个 `resource.json` 描述自己。实际资源可以位于其他挂载盘：

| kind | 用途 | 启动行为 |
| --- | --- | --- |
| `system` | 已安装 OS Python、SystemSpec 和项目根 | 调用现有 credentials/manage；必须且只能有一个 |
| `application` | 已安装 Harness 或实验应用 | 按 argv 启动前台应用；必须且只能有一个 |
| `service` | 本地模型等常驻前台服务 | 分配本机端口，启动并检查 HTTP readiness |
| `asset` | 模型权重、ICL、tokenizer、公开设置等 | 只提供内容与输出，不启动进程 |

路径相对于该 `resource.json` 目录解析，也允许绝对路径。`file` 记录文件哈希，`tree` 记录整个普通文件目录的清单与哈希，`python` 记录实际解释器、venv 和安装包字节。仅 OS 的 `root` 可使用存在性目录 `directory`，其被声明的系统输入由现有 OS plan/lock 继续校验。普通模型和上下文目录必须使用 `tree`。

现有 Docker 镜像放在 Docker 引擎中，由 SystemSpec 的不可变 image ID 定位；仅把 tar 放进目录不会自动变成可运行环境。已有离线包可先运行 `install.py --load-images`，或者手工 `docker load --input image.tar`，再绑定对应设备插件和配置。单有镜像而没有对应 DeviceAPI 适配器，也不能自动提供 OS 能力。

## 3. 组合声明

一个简短 JSON 文件选择资源并显式绑定端口：

```json
{
  "api": "metaos.local-harness/v1",
  "components": {
    "system": {"resource": "libero"},
    "context": {"resource": "libero-assets"},
    "model": {"resource": "model-api"},
    "harness": {
      "resource": "libero-harness",
      "bindings": {
        "system": "system",
        "assets": "context",
        "model": "model"
      }
    }
  }
}
```

清单的 `provides` 与 `requires` 使用精确契约标识匹配。缺绑定、不兼容标识、依赖环、未连接到应用的多余资源、未知命令模板均会拒绝。v1 检查的是声明契约标识与 OS 既有 plan；不会凭接口同名推断模型架构、模态或动作语义兼容。

启动命令必须是 argv 数组，不调用 shell。支持的模板包括：

- `{self.paths.python}`：本资源声明的路径。
- `{system.endpoint}`、`{system.ready_file}`：绑定 OS 的就绪信息。
- `{model.outputs.endpoint}`：依赖资源的声明输出。
- `{weights.paths.root}`：依赖资源的资产目录。
- `{self.port}`：本次 service 分配的本机端口。
- `{run.directory}`、`{run.credentials}`、`{run.store}`：本次工作区位置。

只能引用自身及依赖图中的资源。输出可以引用本资源路径或已绑定资源；不会进行 shell 展开。`env` 只声明要转交给该进程的环境变量名字，其值不写入 Bundle 或运行记录。资源发布者与命令本身属于受信本地代码，v1 不提供恶意插件沙箱。

## 4. 使用现有模型 API 运行 LIBERO

真实 API 单任务已经通过：`deepseek-ai/DeepSeek-V4.1-Flash`，`libero_spatial_task-t00-i00`、seed 0，原生 success / score=1.0。最新工作区为 `work/libero-api-task00-20261105-02/`，证据见 [API 索引](../evidence/libero-api-latest.json)。

新增 API 实验请使用 `scripts/prepare_libero_api.py` 生成资源池，再 build/run，完整命令见 [API 实验说明](libero-api-experiment.md)。它显式设置 chat 协议、清除原 27B serving 专用参数、设置上下文/输出预算，并让整个 episode 的规划期限与 case 期限一致。只改 `init` 默认清单中的模型名和 URL，不足以保证不同服务兼容。

模型仍作为 `model.chat/v1` 的 asset 注册，输出公开模型名与 endpoint；密钥只通过声明的环境变量传入。已存在的外部 API 不由 Runner 启动或关闭。真实实验要求 Docker、对应镜像、可用渲染 GPU，以及支持图像和工具调用的模型 API。

当前原 27B 服务仍因远程显存不足未启动；API 单任务成功与原 27B 指定模型验收分别记录。

## 5. 使用本地模型权重和服务

模型权重是资产，推理框架是另一个已安装组件。先准备兼容的服务环境，例如用户自己的 vLLM venv；工具不会替用户安装框架或推断显存参数。

权重资源清单，例如 `my-resources/weights/resource.json`：

```json
{
  "api": "metaos.local-resource/v1",
  "kind": "asset",
  "provides": ["model.weights/v1"],
  "paths": {
    "root": {"path": "/data/models/my-model", "mode": "tree"}
  }
}
```

服务清单，例如 `my-resources/model-local/resource.json`；下面是配置模板，要求对应版本框架已安装且支持所选模型，并非已完成真实模型运行验收：

```json
{
  "api": "metaos.local-resource/v1",
  "kind": "service",
  "provides": ["model.chat/v1"],
  "requires": {"weights": "model.weights/v1"},
  "paths": {
    "python": {"path": "/data/venvs/vllm/bin/python", "mode": "python"}
  },
  "env": ["CUDA_VISIBLE_DEVICES"],
  "command": [
    "{self.paths.python}", "-I", "-B", "-m", "vllm.entrypoints.openai.api_server",
    "--model", "{weights.paths.root}", "--served-model-name", "local-model",
    "--host", "127.0.0.1", "--port", "{self.port}"
  ],
  "health_url": "http://127.0.0.1:{self.port}/v1/models",
  "endpoint": "http://127.0.0.1:{self.port}/v1",
  "outputs": {
    "model_name": "local-model",
    "endpoint": "http://127.0.0.1:{self.port}/v1"
  },
  "startup_timeout_s": 600
}
```

在组合声明中增加权重节点，替换模型节点，其他绑定保持不变：

```json
{
  "weights": {"resource": "weights"},
  "model": {"resource": "model-local", "bindings": {"weights": "weights"}}
}
```

重新 build/run 后，工具先启动模型服务并等待就绪，再启动依赖它的 Harness；实验结束时关闭本次拥有的模型服务。兼容性、显存与所需的引擎启动参数仍需用户明确配置。已有外部模型 API 作为 asset 注册，不会被工具停止。

## 6. 内容锁、运行结果和限制

- v1 Bundle 是本地路径引用锁，**不是可搬迁离线发行包**。不复制大型镜像/权重，也不会复制 venv。
- build 与 run 前校验文件、权重目录及已安装 Python 内容；权重越大，哈希检查越耗时。文件、目录清单或系统输入变化后，旧 Bundle 拒绝运行，需要重新 build。
- 资源描述与组合声明在 build 时嵌入 Bundle。修改源清单不会悄悄改变已有 Bundle 的组件选择，需重新 build。
- 常规 build/run 不自动更新或安装依赖，不重试设备动作。缺少已安装环境或 DeviceAPI 插件时直接报告；需要先显式 install 本地发行包。
- `run.json` 的 `application_completed` 仅表示应用退出码为 0。任务是否成功、是否真实模型推理，需要检查原 Harness 的 `result.json` 和 trace；不会由通用 Runner 推断。
- Ctrl-C、应用非零退出、服务失联或总超时触发清理。注册的应用需遵守前台生命周期，并在终止时清理自己另起 session 的子进程。LIBERO 本地启动桥已将 SIGTERM 转为可执行 finally 清理的中断。
- 仅清理本次直接拥有的进程/服务；现有 OS Manager 负责自己的 Worker 和原生容器。停止未确认时不声称资源已安全释放。
- 当前只有单系统、单前台应用与多个有依赖关系的服务/资产；无多实验调度、恢复、远程软件目录、任意业务组件编译或 GPU 显存调度。

结果保存在独立输出目录：

```text
my-libero-run/
  run.json                 # 组合身份、进程、应用退出码和清理结果
  credentials.json         # 本次私有凭据
  system.log
  harness.log
  managed/                 # 原 OS 的实例目录与停止证据
  results/                 # 原 CaseRunner 的 case / agent workspace / trace / 媒体
```

## 7. 当前验收

最新索引见 [local-composition-latest.json](../evidence/local-composition-latest.json)。本地装配测试覆盖 12 项：真实 Manager/Worker 的正常、取消与租约到期，依赖/模板拒绝，内容与 Bundle 篡改拒绝，HTTP 服务就绪与退出，应用失败、超时、缺失凭据，以及离线安装入口。最近一次测试结果保存在 `evidence/local-tests-latest.json`。

真实 API 端到端实验已成功，原生 score=1.0，模型响应、网关动作、评分、视频及清理证据保存在 `work/libero-api-task00-20261105-02/`。通用 Runner 的 `application_completed` 仍仅表示应用退出码为零；模型参与和任务成功以原始 trace 与 result 为准。

2026-11-06 已按用户要求清理旧实验、重复构建与旧测试工作区；最新实验的依赖和当前资格验收证据保留，见 [目录与保留规则](workspace-layout.md)。运行默认仍创建新目录，不自动覆盖或删除旧运行。

本地入口是 [完整 Harness 架构](harness-architecture.md) 的第一步：先让已有资源在明确的绑定与生命周期下运行，再推进细粒度业务端口和远程分发。
