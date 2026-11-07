# P1/P2 接口与操作

更新日期：2026-11-06。实际通过项见 [交付记录](p1p2-delivery.md)。外部 API 模型已完成真实单任务闭环，实际配置与复跑命令见 [API 实验说明](libero-api-experiment.md)，目录见 [工作区说明](workspace-layout.md)。原 27B 服务仍因远程 GPU 显存不足未能启动，本页原 27B 命令保留作其恢复后的验收入口。

## 运行边界

```text
独立 Harness 环境
  原 Agent/ICL ←→ 模型 API
       ↓ 原 CaseRunner
  GatewayAdapter + Client + 独立 LeaseKeeper
       ↓ 公开本机 HTTP + caller ACL
OS 环境
  Manager：认证、路由、实例/资源目录、进程生命周期
       ↓ 私有 Worker RPC + manager generation
  独立 Worker：SystemSpec 图 + Gateway + 最终租约/命令检查
       ↓ DeviceAPI
  Mock / 显式 degrees Adapter / LIBERO-Pro 原生容器
```

`serve` 是兼容的 P0 单进程入口。P2 使用 `manage`，每个实例拥有独立进程；一个实例内的依赖图仍可包含多个协作组件。进程隔离不能推导出真实硬件的独立安全停止保证。

## 启动与人工检查

先完整运行 README 中的 P1/P2 bootstrap。最近一次证据索引保存在 `evidence/p1p2-latest.json`；若索引标注未完成，必须检查其中具体的通过范围。

CPU 检查使用 producer 环境，它已安装 Mock 和 degrees Adapter：

```bash
cd /data3/xzx/MetaRoboOS
P2_PY=$(/opt/miniconda3/bin/python -I -B -c 'import json; print(json.load(open("evidence/p1p2-latest.json"))["artifacts"]["producer_python"])')
P2_SESSION=$(mktemp -d "$PWD/work/p2-manual-XXXXXX")
"$P2_PY" -I -B -m metaos_runtime plugins
"$P2_PY" -I -B -m metaos_runtime plan --spec configs/managed-degrees.yaml --root "$PWD"
"$P2_PY" -I -B -m metaos_runtime credentials --output "$P2_SESSION/credentials.json"
"$P2_PY" -I -B -m metaos_runtime manage \
  --spec configs/managed-degrees.yaml --root "$PWD" --workspace "$P2_SESSION/managed" \
  --credentials "$P2_SESSION/credentials.json" --ready-file "$P2_SESSION/ready.json"
```

另一终端将 `P2_SESSION` 设为本次目录，并使用同一 `P2_PY`：

```bash
P2_ENDPOINT=$(/opt/miniconda3/bin/python -I -B -c 'import json,sys; print(json.load(open(sys.argv[1]))["endpoint"])' "$P2_SESSION/ready.json")
"$P2_PY" -I -B -m metaos_runtime client --endpoint "$P2_ENDPOINT" \
  --credentials "$P2_SESSION/credentials.json" instances
"$P2_PY" -I -B -m metaos_runtime client --endpoint "$P2_ENDPOINT" \
  --credentials "$P2_SESSION/credentials.json" resources
"$P2_PY" -I -B - "$P2_ENDPOINT" "$P2_SESSION/credentials.json" <<'PY'
import sys
from metaos_runtime.client import Client, LeaseKeeper
c = Client(sys.argv[1], sys.argv[2])
with LeaseKeeper(c, "robots/degrees-arm", 10.) as keeper:
    request = c.build_command(keeper.check(), "manipulation.move_joint_degrees/v1",
        {"positions": [90., -90.], "duration_s": 0.2})
    c.submit(request)
    result = c.wait(request["command_id"])
    assert result["status"] == "succeeded", result
    print(result)
print(c.state("robots/degrees-arm"))
PY
```

状态和反馈应为度。单位/坐标系不匹配会得到 `semantic_mismatch`，维度错误为 `invalid_action`。管理器正常关闭后，检查 `managed/shutdown.json`、实例的 `worker-shutdown.json`，以及 ready 文件和所拥有进程已退出。

管理器被 SIGKILL 后，Worker 的心跳 watchdog 独立撤销旧控制权。使用原目录恢复：

```bash
"$P2_PY" -I -B -m metaos_runtime manage --resume \
  --workspace "$P2_SESSION/managed" --credentials "$P2_SESSION/credentials.json" \
  --ready-file "$P2_SESSION/ready.json"
```

恢复后必须重新读取 endpoint、申请新租约、使用新时钟域和当前状态构造命令。不要重放旧动作。Worker 死亡或停止未确认时，资源保留故障；只有操作员核实状态后，才能明确 `restart --acknowledge` 和 `reconcile --acknowledge`。

## 新增 API

保留 P0 的资源、状态、租约、命令和取消接口，新增：

| 接口 | 行为 |
| --- | --- |
| `GET /v1/instances` | 实例代次、期望/实际状态、进程身份、内容锁与最后健康状态 |
| `POST /v1/instances` | 管理员提交 `id, spec`，校验并保留导出资源身份后启动 |
| `POST /v1/instances/{id}/start` | 启动已登记实例；故障实例需要受控恢复 |
| `POST /v1/instances/{id}/stop` | 撤销控制、请求停止、清理所拥有进程/原生容器 |
| `POST /v1/instances/{id}/restart` | 要求 `acknowledge=true`，停止后创建新 Worker 代次 |
| `POST /v1/instances/{id}/reconcile` | 要求 `acknowledge=true`，接管并验证停止/状态后清除资源故障 |
| `GET /v1/health` | `ready/degraded` 与实例状态 |
| `GET /v1/tasks` | 当前调用方的动作命令历史，不是 Agent-job API |
| `GET /v1/artifacts?resource_id=...` | 资源 ACL 内的不可变媒体/原生输出元数据 |
| `GET /v1/artifact-chunk?...` | 按 artifact ID、offset、length 读取，单块最大 1 MiB |

生命周期操作需 `admin` scope。`Client.create_instance/instance` 提供对应方法；CLI 支持 `instances/tasks/health/instance`。故障确认是运行时操作要求，不等于每次开发都需人工审批。

SystemSpec 的 `resources: {导出别名: 资源ID}` 在常驻模式下必填，必须与插件实际导出一致。已登记实例持续保留资源身份，停止不会让其他实例抢占同一身份。workspace 用 flock 保证单一管理器，PID 身份同时包含启动 ticks 和 boot ID，避免误杀复用 PID。

## LIBERO-Pro 能力

`configs/managed-libero.yaml` 指定不可变镜像摘要、GPU 和项目内的 `assets/libero/protocol.yaml`。镜像已包含原生仿真依赖；Host 仍需要 Docker、兼容的 NVIDIA 驱动/runtime/GPU。bootstrap 的 `--gpu` 会写入本次部署配置，默认示例配置为 GPU 7。

| 能力 | 语义 |
| --- | --- |
| `simulation.reset/v1` | 原任务/seed 和全新 episode ID，返回初始真实相机与状态 |
| `simulation.libero_action/v1` | 原生 7D OSC action/chunk 或原支持的具名 primitive |
| `simulation.libero_query/v1` | 不推进场景的原生工具查询，保留 query ID |
| `simulation.finish/v1` | 仅原生 terminal/truncated 后调用原评分并收集输出 |
| `simulation.cancel_episode/v1` | 运维终止，停止并释放，不产生有效任务评分 |

原 Adapter 不支持策略请求的零步提前停止，所以没有宣称支持 `end_episode`。不能使用取消或缩短 2000 个原物理步预算替代原评分。OSC 能力使用 `libero_native_action/v1` 和 `libero_world_osc`，不暗中转换为两个 Mock 关节。

命令最大有效期由能力声明：Mock 默认为 60 秒，原生能力显式为 300 秒；当前验收客户端使用 290 秒。执行预算与租约 TTL 是不同机制；`LeaseKeeper` 在模型等待时独立续租，失租后不自动重获控制权。

## 独立 Harness 交付

Harness 安装原 `roborsi`、27B Agent、ICL 与模型依赖，和轻量 OS 环境分开。构建时使用已冻结的原 wheel 快照与实际评测资产，不能使用 M5 的示例 tokenizer/ICL fixtures：

```bash
/opt/miniconda3/bin/python -I -B scripts/bootstrap_harness.py \
  --baseline-bundle /data3/xzx/SenseNova-RoboRSI/work/modularity/m5/native-deployment-qualified-v2 \
  --metaos-wheels work/p1p2-final-03/wheels \
  --packages /data3/xzx/SenseNova-RoboRSI/work/model_eval_remote_27b/assets/libero-three/pro/packages \
  --tokenizer /data3/xzx/SenseNova-RoboRSI/work/model_eval_remote_27b/assets/libero-three/tokenizer.json \
  --task work/p1p2-development/native-task.json --output work/harness-new-acceptance --symlinks
```

脚本从当前源码构建 Harness wheel，复用已验收的 runtime/contracts wheel，并冻结 wheel 哈希、真实资产、测试与安装器，搬迁后删除临时输入目录，离线安装并执行 Harness 测试、原 Agent reset 和 HTTP 服务 reset。reset 不调用模型。搬迁后的 `harness.json` 含依赖/文件身份；`install.py --target 新路径 --expect-fingerprint 外部指纹` 可重新安装。指纹提供完整性，不是发布者签名。`--symlinks` 使用普通 venv 的解释器符号链接，安装器也支持此参数；省略时复制解释器。当前 Harness 为 0.1.1，13 项测试通过。CLI 保留所指定 venv 的解释器路径，并在 case 的 `agent/agent-runtime.json` 中记录 executable、prefix 与包版本。

## 模型恢复后的真实单任务

先使用证据索引中的 `os_python/system_deployment` 启动该冷部署的 `manage`，记录本次 `P2_ENDPOINT/P2_SESSION`。原生镜像可通过 `bundle verify --bundle ... --installed --native` 预检。然后在独立 Harness 终端读取：

```bash
HARNESS_PY=$(/opt/miniconda3/bin/python -I -B -c 'import json; print(json.load(open("evidence/p1p2-latest.json"))["artifacts"]["harness_python"])')
HARNESS_BUNDLE=$(/opt/miniconda3/bin/python -I -B -c 'import json; print(json.load(open("evidence/p1p2-latest.json"))["artifacts"]["harness_deployment"])')
TASK_ID=$(/opt/miniconda3/bin/python -I -B -c 'import json,sys; print(json.load(open(sys.argv[1]))["task_id"])' "$HARNESS_BUNDLE/task.json")
```

在本地加载自己的服务环境文件，确保 `OPENAI_API_KEY` 已导出，并将实际模型名/API base URL 分别设为 `MODEL_NAME/MODEL_BASE_URL`。不要将密钥放入命令行、SystemSpec、部署包或对话。

```bash
"${HARNESS_PY%/*}/metaos-libero-harness" \
  --endpoint "$P2_ENDPOINT" --credentials "$P2_SESSION/credentials.json" \
  --task-id "$TASK_ID" --agent-python "$HARNESS_PY" \
  --packages "$HARNESS_BUNDLE/assets/packages" --tokenizer "$HARNESS_BUNDLE/assets/tokenizer.json" \
  --model "${MODEL_NAME:?set actual model name}" --model-base-url "${MODEL_BASE_URL:?set actual API base URL}" \
  --agent-version reviewed-original-27b --store "$P2_SESSION/results" \
  --case-timeout 7200 --operation-timeout 290
```

`--agent-version` 是调用方提供的来源标签；真正的已安装代码/资产身份见 Harness manifest、原 `agent-plan.json` 和 `context-package.json`，不能用标签冒充代码哈希。原 Agent 的推理/上下文逻辑不变；`--agent-set key=JSON` 仅用于显式记录的非敏感配置调整。命令显式选择模型和地址，避免继承 preset 的其他模型。

Agent 服务启动失败会在已 claim 的 case 中落盘 `infrastructure_error`、错误阶段 `agent.start` 与最终 trace；已关闭的 GatewayAdapter 不能用于新会话。

验收需同时检查真实模型响应/工具调用、`gateway-commands.jsonl`、原生 score JSON/MP4、原 CaseRunner 结果和 trace、Agent 进程退出与租约释放。`task_failure` 表示正常完成但原任务未成功，与 `agent_error/infrastructure_error` 分开；返回一个分数文件不等于真实 27B 已参与。

## 本地故障与媒体复验

不依赖远程模型的实际 CLI 故障验证可分别选择 `connection`、`agent_exit`、`model_timeout`。后两种等待原 Agent 发起本地模型请求，再强制退出本次拥有的 Agent 或触发规划器超时；本地服务不返回模型输出。每次指定不存在的新输出目录：

```bash
work/p1p2-harness-011/cold/bin/python -I -B scripts/exercise_harness_failure.py \
  --system-python work/p1p2-final-03/cold/bin/python \
  --harness-python work/p1p2-harness-011/cold/bin/python \
  --harness-bundle work/p1p2-harness-011/relocated/harness \
  --spec work/p1p2-final-03/relocated/deployment/system.json \
  --root work/p1p2-final-03/relocated/deployment/project \
  --failure-mode model_timeout --output work/harness-timeout-new
```

故障报告检查未评分结果、原生取消、租约释放、其他 caller 接管与过去状态拒绝。完整原生 Harness 运行后的媒体可单独复验，要求 Pillow、ffprobe 和 ffmpeg：

```bash
work/p1p2-harness-011/cold/bin/python -I -B scripts/verify_native_media.py \
  --run work/p1p2-harness-011-native
```

该检查核对原始 artifact 哈希和大小，完整读取图片、解码 MP4，并写入该运行目录的 `media-validation.json`。完整基础设施证据审核命令见 [交付记录](p1p2-delivery.md)。

## 未覆盖范围

本阶段没有承诺恶意插件沙箱、真实硬件安全、跨节点调度、完整 Agent-job 调度、全基准性能、长期生产稳定性或 Factory。进程/容器被强制移除时，未知结果仍须保留为未知，不能据此写成物理停止成功。
