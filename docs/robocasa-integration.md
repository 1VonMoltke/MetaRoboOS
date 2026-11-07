# RoboCasa365 Target50 接入

更新日期：2026-11-06。状态：**插件、独立安装、本地装配和协议验证已完成；未运行真实仿真任务或模型推理**。证据见 [robocasa-latest.json](../evidence/robocasa-latest.json)。

## 接入内容

- `plugins/robocasa-device/`：`metaos-robocasa-device==0.1.0`，通过 `roborsi.components` 注册 `environment.robocasa_device`。安装 wheel 后由现有 Registry 发现，不修改通用 runtime 的环境名称列表。负责自有 Docker 容器、reset/action/stop/finish/cancel、产物与清理。
- `plugins/robocasa-harness/`：`metaos-robocasa-harness==0.1.0`，原 CaseRunner 的独立网关客户端，持有租约、校验动作身份、下载并验证媒体/评分；支持外部 Agent RPC 或自有 Agent 子进程。
- 原 Agent 使用 `robots.robocasa.model_eval_agent:make_agent`、原 Target50 提示词、100 轮预算、3600 秒动作超时、原上下文策略和同任务 seed-0 ICL。模型通过 API 显式配置，密钥仅使用 `OPENAI_API_KEY`。原配置的 `xhigh` reasoning、32768 输出上限、图像/tool calling 不保证所有 API 都兼容，换模型时应显式调整 `agent.json` 并重新 build。
- 原生工具和评分仍由 RoboRSI RoboCasa benchmark 镜像负责。OS Python 不安装 MuJoCo、robosuite、RoboCasa、Torch 或模型客户端。感知使用原 toolkit action；原生不支持独立 BenchmarkQuery，因此不导出该能力。

任务按 **task_id + seed** 选择：50 个任务、340 个 Target50 cell；atomic seed 1–10，两个 composite split seed 1–5，seed 0 只作为 ICL 来源。保留 split 1800/3600 秒墙钟限制及 400 次工具调用 runaway cap，不套用 LIBERO 物理步预算。

策略 `/stop` 后允许 `/finish` 结算当前状态；操作 `/cancel` 不产生评分。单次网关命令遵守 contracts 的 3600 秒有效期上限，Harness 默认 3590 秒；整段任务预算独立管理。

## 文件与安装环境

```text
work/robocasa-integration-20261106/
  installation.json         # 安装与冻结输入索引
  os-deployment/            # 轻量 OS wheel 闭包、哈希锁和安装器
  os-env/                   # 独立 OS 安装环境与设备 entry point
  harness-deployment/       # 原评测/模型客户端 + 新 Harness 的离线闭包
  harness-env/              # 独立 Harness 环境
  assets/                   # 原 Agent/提示词/Target50/ICL 源码快照
  asset-inventory.json      # 快照 SHA-256 清单
  native-context/           # 原项目导出的最小 Docker build context
  fixture-resources/        # 假镜像 ID、无模型 endpoint 的协议测试资源
  fixture-bundle/           # 已 plan/build/verify 的协议装配样例
  verification.json         # 验证报告
  logs/                     # 安装与检查日志
```

`fixture-*` **不是可运行实验**：全零镜像 ID、`127.0.0.1:9` API 仅验证声明与构建。真实实验必须重新生成资源池并填写实际镜像和 API。最近真实实验仍为 `work/libero-api-task00-20261105-02/`，本次保留不变。

重新离线构建/安装插件（使用新输出目录，不启动仿真）：

```bash
/opt/miniconda3/bin/python -I -B scripts/bootstrap_robocasa.py \
  --baseline ../SenseNova-RoboRSI \
  --icl ../SenseNova-RoboRSI/work/model_eval_robocasa_27b/assets/robocasa365-icl \
  --output work/robocasa-new
```

两个 `*-deployment/` 复用哈希校验安装器 `metaos.harness/v1`，`role` 区分 OS/Harness Python 闭包；不包含原生镜像、GPU 驱动或完整实验资产。也可 `./metaos-local install <deployment> --target <新环境>` 后在资源描述中替换解释器路径。实验 Bundle 仍为本地路径引用锁。

## 准备真实资源与装配

本次只导出 Docker 构建上下文，**没有拉取或构建 RoboCasa 镜像**。后续先准备原项目固定的 base 镜像、厨房资产、RLDX-1 权重，再用 context 构建最终 benchmark service 镜像。base digest 和 Dockerfile 保存在 `native-context/`。base 镜像本身不能直接代替最终 benchmark 镜像。前置镜像已经具备时可执行：

```bash
docker build -t metaos-robocasa365:local \
  work/robocasa-integration-20261106/native-context
ROBOCASA_IMAGE_ID=$(docker image inspect metaos-robocasa365:local --format '{{.Id}}')
```

如果最终镜像本机已有，直接使用其实际 `sha256:...` image ID。默认权重路径 `/opt/rldx/RLDX-1-FT-RC365`；API planner **仍需 RLDX/VLA 及 GPU**。首次 reset 由原生服务启动自有 RLDX，也可在 SystemSpec 提供本机 `vla_endpoint`，外部 VLA 不归设备清理。

以下只生成配置、校验和构建，不调用模型/仿真：

```bash
python3 scripts/prepare_robocasa.py \
  --installation work/robocasa-integration-20261106/installation.json \
  --resources work/my-robocasa-resources \
  --image "$ROBOCASA_IMAGE_ID" --gpu 0 \
  --model ACTUAL_MODEL_ID --base-url http://MODEL_HOST:PORT/v1 \
  --task-id OpenDrawer --seed 1

./metaos-local plan work/my-robocasa-resources/robocasa.json \
  --resources work/my-robocasa-resources
./metaos-local build work/my-robocasa-resources/robocasa.json \
  --resources work/my-robocasa-resources --output work/my-robocasa-bundle
./metaos-local verify work/my-robocasa-bundle
```

生成三个组件 system、context、harness；API 设置位于 context 的 `source/agent.json`。准备脚本校验冻结资产和任务/seed。`plan/build` 不确认 Docker 镜像是否存在、GPU 容量或 API 可用性，不能据此声称真实运行通过。

后续明确开展任务时，设置环境密钥并使用现有 `metaos-local run`，进入 Manager → Worker → 容器 → Harness/原 Agent → 网关动作 → 原生评分与清理工作流。本次未执行该步骤。默认 case timeout 14400 秒，总运行 timeout 应更长，以覆盖启动和清理。

## 验证范围

9 项设备测试、16 项 Harness 测试通过；检查全部 340 个 cell 的原 ICL 映射。原共享 Benchmark HTTP 服务搭配 fixture，验证 HTTP reset、宽松工具参数、stop、finish、cancel、图片与评分校验；两次 fixture 会话仅一次评分，取消不评分。已安装插件 entry point、CLI、本地 plan/build/verify 通过，既有 LIBERO API Bundle 内容校验通过。

对新的安装目录运行同样验证（会创建 fixture 输出）：

```bash
work/robocasa-new/harness-env/bin/python -I -B scripts/verify_robocasa.py \
  --installation work/robocasa-new/installation.json
```

尚未验收：实际镜像/GPU/RLDX 启停、真实动作/相机、API Agent 兼容性、真实任务结果与全基准聚合。fixture 不是任务成功率证据。
