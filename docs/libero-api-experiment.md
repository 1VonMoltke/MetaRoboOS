# 使用模型 API 装配并评测原 RoboRSI Agent

更新日期：2026-11-06。本指南运行原 Agent、真实 LIBERO-Pro 与原生单任务评分，规划模型来自用户的 API，不启动或下载本地 27B 权重。最近一次真实实验已成功，见第 5 节；模块与工作区位置见 [目录说明](workspace-layout.md)。

运行链路：

```text
原 RoboRSI Agent + 真实 ICL + 原生动作/图像工具
                ↕ 图像、工具调用与模型响应
       用户的模型 API（Chat Completions 兼容）
                ↓ 设备能力调用
Client → Manager → Worker / Gateway → LIBERO-Pro Docker
                ↓
      原 CaseRunner / 原生评分 / trace / 视频
```

这是当前原策略的无 VLA、无 SAM3 运行条件，工具仍包含原生动作与观测查询。“无本地模型权重”不免除仿真渲染所需的本地 Docker/NVIDIA 环境。包名 `roborsi-agent-27b` 表示复用的原策略发行包，实际推理模型由传入的 API 地址和模型名决定。

## 1. 前提

- 本机已有已验收的 OS、LIBERO 镜像、原 Agent/Harness 和 ICL/tokenizer；准备脚本默认从 `evidence/p1p2-latest.json` 获取实际路径。
- 模型 API 支持 OpenAI Chat Completions 兼容协议、图像输入和工具调用，模型名使用服务实际提供的 ID。
- 使用服务支持的上下文和输出预算。原 ICL 较长，不能假设小上下文模型可直接容纳。
- 密钥放在当前终端的环境变量中；不写入命令参数、资源文件或 Bundle。

当前桥接固定使用 Chat Completions。仅提供原生 Responses、Anthropic 等其他协议的接口，需先增加对应接入；不能只替换 URL 宣称兼容。

## 2. 准备资源项目

以下为真实命令，替换模型名、API URL 和 GPU 编号。输出资源目录必须不存在：

```bash
cd /data3/xzx/MetaRoboOS

python3 scripts/prepare_libero_api.py \
  --resources work/my-api-resources \
  --model YOUR_VISION_TOOL_MODEL \
  --base-url https://your-api-provider.example/v1 \
  --api-key-env OPENAI_API_KEY \
  --gpu 2 \
  --context-window 128000 \
  --max-output-tokens 8192 \
  --case-timeout 7200
```

`--context-window` 与 `--max-output-tokens` 需按实际 API 能力设置，示例数字不是自动检测结果。默认保留原本地 tokenizer 作为上下文预算估算器，跨模型时与服务实际 token 统计可能有差异。原任务的物理步预算和原生评分协议不改动。

脚本会：

1. 注册现有 OS、Agent、ICL 与 tokenizer 路径。
2. 复制一份 OS SystemSpec 到本次资源池并设置 GPU，不修改冻结交付物。
3. 绑定外部模型 API，密钥仅保留环境变量名字。
4. 清除原 27B 的 `extra_body` 中专用 serving 参数，使用 chat 格式，关闭默认并行工具调用。
5. 显式记录模型输出、上下文、超时和重试配置，保留原 Agent/工具/ICL 与 CaseRunner。

`agent.planner_timeout_s` 约束整个 episode 的规划过程，不是单次模型请求；准备脚本将它与 `--case-timeout` 设为一致，避免较慢的 API 策略被更短的内部规划期限提前取消。

默认任务来自当前已验收的 `task.json`，为 `libero_spatial_task-t00-i00`、seed 0。它是完整单任务评测，不是全套 LIBERO-Pro 基准。

## 3. 构建和运行

```bash
./metaos-local build work/my-api-resources/libero.json \
  --resources work/my-api-resources --output work/my-api-build

# 使用隐藏输入设置密钥，也可以从自己的本地环境文件加载
read -rsp 'API key: ' OPENAI_API_KEY
export OPENAI_API_KEY
printf '\n'

# 为 case 结束后的评分与清理留出额外时间
./metaos-local run work/my-api-build \
  --output work/my-api-run --timeout 7500
```

准备和 build 不调用模型；run 会实际调用所配置的 API。若使用自定义密钥变量名，例如 `MY_MODEL_KEY`，准备时传 `--api-key-env MY_MODEL_KEY`，然后在运行终端设置该变量；应用桥会在自己的进程内映射为原 Agent 所需的 `OPENAI_API_KEY`。

需要改变模型名、URL、预算或组件声明时，修改资源文件并输出到一个新的 build 目录。已有 build 保留原内容身份。再次实验使用新的 run 目录。

## 4. 判断运行和评测结果

```text
work/my-api-run/
  run.json                         # 应用退出码、组合身份、清理结果
  system.log / harness.log
  managed/                         # Worker、网关与原生容器的运行记录
  results/.../
    result.json                    # 原 CaseRunner 的任务结果
    gateway-commands.jsonl          # 真正提交给 OS 的 reset/action/query/finish
    agent/                         # 原 Agent 配置、ICL 身份、runtime 与推理记录
    ...                            # trace、原生评分和媒体，按结果中的引用定位
```

检查三层证据：

- **系统执行**：`run.json` 中应用退出码正常、`cleanup_confirmed=true`。
- **模型参与**：Agent trace 中有实际模型响应/工具调用，并且 `gateway-commands.jsonl` 有其驱动的动作。
- **任务评测**：原 `result.json` 的 status、score、原生评分引用、媒体和最终 trace。

`success` 表示原任务成功；`task_failure` 表示正常结束但任务失败；两者都可证明评测正常完成。`agent_error`、`infrastructure_error`、未评分的取消和未知结果不能算一次正常任务评测。通用 Runner 的 `application_completed` 只表示应用退出码为零，不替代原任务结果。

## 5. 当前验证范围

最近一次成功实验位于 `work/libero-api-task00-20261105-02/`，原始 `run/`、资源声明和 Bundle 均保留：

| 项目 | 实际结果 |
| --- | --- |
| 完成时间 | 2026-11-05 17:32，北京时间 |
| 模型 | `deepseek-ai/DeepSeek-V4.1-Flash`，复用原 API 配置 |
| 任务 / seed | `libero_spatial_task-t00-i00` / `0` |
| 原生结果 | `success` / `score=1.0`，`native_success_at_1x` |
| 步数 | 303 个物理步、64 个决策步；上限仍为 2000，原生成功后提前终止 |
| 模型响应 | 42 次：26 次规划、16 次上下文压缩 |
| 耗时 | 整体约 43 分钟 |
| 产物核验 | 263 个产物、195 张图像、304 帧 H.264 视频完整解码通过 |
| 清理 | 进程退出、租约释放、所拥有仿真容器移除；`cleanup_confirmed=true` |

证据：[最新索引](../evidence/libero-api-latest.json)、[核验报告](../work/libero-api-task00-20261105-02/report.json)。准备脚本的两项测试结果保留在该实验的 `recipe-validation.json`。本结果只覆盖此任务与 seed，不是全基准或原 27B 模型的验收。

使用已保留的 Bundle 复跑，无需重新下载或 build；输出目录必须不存在：

```bash
cd /data3/xzx/MetaRoboOS
./metaos-local verify work/libero-api-task00-20261105-02/bundle

# 只在本机终端加载既有凭据，不打印密钥
set -a
source ../SenseNova-RoboRSI/work/libero-pro-pilot/api.env
set +a

./metaos-local run work/libero-api-task00-20261105-02/bundle \
  --output work/libero-api-next-run --timeout 7500
```

Bundle 引用本机已安装的 OS/Harness 和资产，依赖目录需保留；具体路径见 [工作区说明](workspace-layout.md)。验证历史产物可运行 `work/p1p2-harness-011/cold/bin/python -B work/libero-api-task00-20261105-02/audit.py`，不启动仿真或模型。
