# 已实现的组装与运行工作流

所有命令从仓库根目录执行。路径/解释器由本机实际安装决定，下面使用变量的地方需先设置；不要把其他机器的绝对路径当作产品默认值。

## A. CPU Mock：外层工作区入门与装配验证

条件：`evidence/p1p2-latest.json` 的 artifacts 指向仍存在的已安装 OS 环境。缺失时先按 [本地装配文档](../../../../docs/local-composition.md) 安装已有发行包或手工登记资源池；`init` 不会安装环境。

```bash
mkdir -p work
ASSEMBLY_WORK=$(mktemp -d "$PWD/work/assembly-skill-XXXXXX")
./metaos-local init --resources "$ASSEMBLY_WORK/resources"
./metaos-local plan "$ASSEMBLY_WORK/resources/mock.json" --resources "$ASSEMBLY_WORK/resources"
./metaos-local build "$ASSEMBLY_WORK/resources/mock.json" \
  --resources "$ASSEMBLY_WORK/resources" --output "$ASSEMBLY_WORK/bundle"
./metaos-local verify "$ASSEMBLY_WORK/bundle"
# 执行已获授权的 CPU Mock 动作、取消和租约到期验证；无需 GPU/API。
./metaos-local run "$ASSEMBLY_WORK/bundle" --output "$ASSEMBLY_WORK/run" --timeout 180
```

检查 `run/actions.json` 的 normal/cancel/expire 和 `run/run.json` 的 `application_completed`、`cleanup_confirmed`。这验证 Manager/Worker 生命周期，不证明任何真实模型或仿真任务成功。

总超时包含进程初始化；冷缓存和繁忙磁盘会消耗启动预算。若出现 HTTP 超时，查看 system/harness/worker 日志并区分客户端请求时限与总时限。旧运行清理未确认时，先核对其拥有的进程；使用新目录复跑，保留失败记录。

Mock 的 SQLite 状态写入也受文件系统延迟影响。定位问题时可将新 run 输出放到低延迟本地盘；`/tmp` 仅适合临时验证，结果随后归档。增大外层 `--timeout` 不会增大 Client 的请求时限。遇到 `OutcomeUnknown`，先按 command_id 对账终态与停止确认，不自动重复提交。

## B. 仅组装 Harness 业务图

`HARNESS_PY` 是安装了目标 providers 的解释器，`HARNESS_SPEC` 是所选 YAML，`HARNESS_BUNDLE` 是尚不存在的输出目录。

```bash
"$HARNESS_PY" -I -B -m metaos_harness.cli components
"$HARNESS_PY" -I -B -m metaos_harness.cli plan --spec "$HARNESS_SPEC"
"$HARNESS_PY" -I -B -m metaos_harness.cli build --spec "$HARNESS_SPEC" --output "$HARNESS_BUNDLE"
"$HARNESS_PY" -I -B -m metaos_harness.cli verify --bundle "$HARNESS_BUNDLE"
```

参考 [默认业务图](../../../../configs/roborsi-libero.harness.yaml)。别名只用小写字母、数字和下划线且以字母开头；节点包含 `component/version/config/bindings`。所有节点须连到 `entrypoint`，依赖绑定必须与 descriptor.requires 一致；版本、配置、端口契约与声明语义需匹配。

自定义契约使用 `Host` 或自己的应用入口。当前 `metaos_harness.cli run` 的 Experiment schema 和结果提取是 LIBERO/RoboRSI 专用，不是任意业务图的通用 runner。

## C. 原 RoboRSI：单任务 LIBERO

依据 [RoboRSI 接入](../../../../docs/roborsi-modular.md) 和 [prepare_roborsi.py](../../../../scripts/prepare_roborsi.py)。与 SenseNova 单 Agent 的 `prepare_libero_api.py` 分开。

先准备：

| 变量 | 含义 |
| --- | --- |
| `HARNESS_PY` / `OS_PY` | 原 RoboRSI Harness/原生依赖环境与轻量 OS 环境的 Python |
| `HARNESS_SPEC` / `EXPERIMENT_SPEC` | 业务图与单任务配置；后者含 atomic、sim_task、seed、tool_budget |
| `LIBERO_ROOT` / `BDDL_DIR` / `INIT_DIR` | LIBERO 代码与任务资产 |
| `PYROKI_PY` / `PYROKI_PORT` / `GPU_ID` | 独立运动学环境、专用端口、本次可用 GPU |
| `ROBORSI_WORK` | 新工作区根；assembly、episode-bundle 尚不存在 |

```bash
python scripts/prepare_roborsi.py \
  --harness-python "$HARNESS_PY" --os-python "$OS_PY" \
  --spec "$HARNESS_SPEC" --experiment "$EXPERIMENT_SPEC" \
  --libero-root "$LIBERO_ROOT" --bddl "$BDDL_DIR" --init "$INIT_DIR" \
  --pyroki-python "$PYROKI_PY" --pyroki-port "$PYROKI_PORT" --gpu "$GPU_ID" \
  --output "$ROBORSI_WORK/assembly" --run-root "$ROBORSI_WORK/runs"
./metaos-local build "$ROBORSI_WORK/assembly/local.json" \
  --resources "$ROBORSI_WORK/assembly/resources" --output "$ROBORSI_WORK/episode-bundle"
./metaos-local verify "$ROBORSI_WORK/episode-bundle"
# 先在当前终端提供 OPENAI_API_KEY，勿写入配置。
./metaos-local run "$ROBORSI_WORK/episode-bundle" \
  --output "$ROBORSI_WORK/runs/task00" --timeout 14400
python scripts/verify_roborsi_run.py --run "$ROBORSI_WORK/runs/task00" \
  --output "$ROBORSI_WORK/task00-audit.json"
```

prepare 读取现有感知模型与机器人 cache，不隐式下载；可以用其 `--hf-cache/--robot-cache` 参数指定。run 输出必须位于 prepare 声明的 run-root 下。

模型调整应改 `model.config.base_url` 和各角色 `config.model`。当前 OpenAI 兼容模式使用 `openai/<served-model-id>`；模型 provider 会设置 base URL，单独修改 shell 的 `OPENAI_BASE_URL` 不能覆盖锁定配置。prepare 将 Engineer 模型写入原生感知配置。先检查 `/v1/models`、图像输入与标准 function tool_calls；服务鉴权与 SSH 隧道由实际环境决定。

输出 token 限制、reasoning、transport 是否可配需读 [模型实现](../../../../plugins/roborsi-components/src/metaos_roborsi/components.py) 及安装后的原实现；不要在 Schema 中添加不支持的参数。修改运行语义后重新构建。

## D. LIBERO campaign：选定条数与续跑

依据 [campaign 文档](../../../../docs/roborsi-campaign.md)。确认 campaign 模块已安装；快捷入口 `./metaos-campaign` 指向当前仓库的独立 campaign 环境。需要换解释器时使用安装后的模块：`"$HARNESS_PY" -I -B -m metaos_roborsi_campaign.cli ...`。

1. 使用 `panel --count all --seed 0 --tool-budget 24 --output "$ROBORSI_WORK/panel.json"` 建立 120 条白名单。预算按实验目标选择，24 是现有示例，原 PRO 基线脚本使用 80。
2. 按 C 构建，但在 prepare 命令中增加 `--panel "$ROBORSI_WORK/panel.json"`；此时 panel 取代单任务配置。`HARNESS_PY` 也必须装有 campaign 包。不能将未准备 panel 的单任务 Bundle 当成全量入口。
3. 选择条数并查看清单：

```bash
./metaos-campaign build --episode-bundle "$ROBORSI_WORK/episode-bundle" \
  --count 20 --output "$ROBORSI_WORK/campaign-20"
./metaos-campaign plan --bundle "$ROBORSI_WORK/campaign-20"
./metaos-campaign run --bundle "$ROBORSI_WORK/campaign-20" \
  --output "$ROBORSI_WORK/runs/eval-20"
# 中断后，同一配置/输出且无同目录存活任务时：
./metaos-campaign run --bundle "$ROBORSI_WORK/campaign-20" \
  --output "$ROBORSI_WORK/runs/eval-20" --resume
```

`--count` 支持 1..120 或 `all`；默认 stratified 交错选取；随机抽样使用 build 的 `--selection random --selection-seed N`。协议是每任务一个固定 seed、顺序独立 episode，不是 pass@K 或多 GPU 并发。

`tool_budget` 沿用原角色循环口径，并非严格的工具函数调用总数；一条回复可能包含多个工具调用。统计中分别保留有效任务成功率和覆盖率；以 summary 的原生有效判分为准。基础设施错误不直接计为任务失败。

## E. 自定义外层资源与工作区

已有组件之外的应用或服务，按 [本地装配文档](../../../../docs/local-composition.md) 写资源池：

- 每个 `resource.json` 的 api 为 `metaos.local-resource/v1`；kind 为 system/application/service/asset。v1 要求一个 system、一个 application，其余节点都成为 application 的依赖。
- `requires` 是端口到契约，`provides` 是契约列表，`paths` 使用 file/tree/python；directory 仅限 OS root。资源路径相对自身 resource.json，Bundle 保存本机内容引用。
- 命令使用 argv 数组和显式模板，如 `{self.paths.python}`、`{system.endpoint}`、`{run.directory}`，不执行 shell 展开。
- `env` 仅列要传入的变量名；新启动的进程不会自动获得任意父环境变量。已有远程 API 通常作为 asset 输出公开端点，本地 service 则由 runner 拥有其生命周期。

预期布局：assembly/resources 和内层 harness 属于锁定输入；episode-bundle/campaign-N 保存构建；runs 下每次实验使用新目录。日志放 Bundle 外，避免自身运行改变被锁定的资产树。
