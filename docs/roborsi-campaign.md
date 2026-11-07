# 选定条数的 RoboRSI / LIBERO-PRO 测评

新增 `plugins/roborsi-campaign/`，通过 `metaos.harness.components` 注册两个可组合的 provider：

| Provider | 责任 |
| --- | --- |
| `campaign.roborsi_libero` | 选择任务、逐任务调度、断点续跑、错误分类与汇总 |
| `executor.roborsi_local` | 为每次 episode 启动现有 MetaRoboOS Bundle，并调用独立评分与清理核验 |

两者通过 `metaos.roborsi.episode_executor/v1` 端口连接。声明示例在 `configs/roborsi-libero.campaign.yaml`。单任务内部仍使用此前的 Planner / Engineer / Reviewer 等 11 个组件；本模块不调用原 `roborsi eval-suite` 替代组件化链路。

## 当前可用入口

工作区 `work/roborsi-campaign/` 已准备以下配置，未启动真实批量测评：

```text
work/roborsi-campaign/
├── harness-env/          # 独立安装 campaign 包，显式继承既有 Harness 依赖
├── wheels/               # 非 editable 的 campaign wheel
├── panel-120.json        # 允许执行的完整 120 条任务及固定 seed/预算
├── assembly/             # 内层单任务图、原生配置及外层资源
├── episode-bundle/       # 所有任务共用的外层 Bundle
├── campaign-10/          # 选择 10 条的调度组件图与内容锁
├── campaign-20/          # 选择 20 条
├── campaign-120/         # 选择全部 120 条
├── runs/                 # 真实测评运行的输出位置，目前没有批量运行结果
└── tests-installed.log   # 18 项定向检查；含明确标记的进程协议 fixture
```

在项目根目录运行。预先在环境中提供 `OPENAI_API_KEY`，密钥不写入声明或测评 manifest：

```bash
# 先查看将测评的任务，不调用模型或仿真
./metaos-campaign plan --bundle work/roborsi-campaign/campaign-10

# 选择以下任意一个入口启动真实测评
./metaos-campaign run --bundle work/roborsi-campaign/campaign-10 \
  --output work/roborsi-campaign/runs/eval-10

./metaos-campaign run --bundle work/roborsi-campaign/campaign-20 \
  --output work/roborsi-campaign/runs/eval-20

./metaos-campaign run --bundle work/roborsi-campaign/campaign-120 \
  --output work/roborsi-campaign/runs/eval-120

# 对同一配置继续运行，不重复已获得有效原生判分的任务
./metaos-campaign run --bundle work/roborsi-campaign/campaign-10 \
  --output work/roborsi-campaign/runs/eval-10 --resume
```

当前协议是 **frozen、每条任务一个固定 seed、每次运行一个独立 episode、顺序执行**。默认 seed=0、tool_budget=24，与此前单任务的预算一致。它没有实现多 GPU 并发或原生 pass@K 的成功后停止规则。若要使用原 RoboRSI PRO 完整基线脚本的预算 80，在准备 panel 时显式设置 `--tool-budget 80` 后重新构建。

当前设备配置使用 GPU 3、专用 PyRoKi 端口 15559。每次 episode 都启动并清理其拥有的 OS、仿真和 PyRoKi 进程；只同时启动一个此类 campaign，或为其他 campaign 配置独立资源并重建。

## 选择任意条数

支持 `1..120` 或 `all`。整套 episode Bundle 只构建一次；改变选取条数时，只需构建轻量调度图：

```bash
./metaos-campaign build \
  --episode-bundle work/roborsi-campaign/episode-bundle \
  --count 15 --output work/roborsi-campaign/campaign-15

./metaos-campaign build \
  --episode-bundle work/roborsi-campaign/episode-bundle \
  --count all --output work/roborsi-campaign/campaign-all

# 可复现的随机抽取
./metaos-campaign build \
  --episode-bundle work/roborsi-campaign/episode-bundle \
  --count 20 --selection random --selection-seed 42 \
  --output work/roborsi-campaign/campaign-random20
```

默认 `stratified` 按任务 ID、扰动、任务族交错选取，让小子集覆盖多个 suite；不是简单取同一 suite 的前 10 条。`first` 按锁定 panel 的顺序选择；`random` 按独立的 selection_seed 抽样，该种子不改变仿真 seed。实际任务列表会完整写入 `campaign.json`。

## 输出、汇总与续跑

```text
runs/eval-10/
├── campaign.json          # 不可变任务列表、seed、预算、组件与执行器身份
├── campaign.lock          # 防止并发调度；存活的 launcher 也继承此锁
├── episodes.jsonl         # 带序号及哈希链的 started/finished 日志
├── summary.json           # 进度、成功率、错误计数及分组结果
└── episodes/
    └── 000-libero_spatial_task-0/
        └── attempt-01/
            ├── launcher.json
            ├── launcher.log
            ├── audit.json       # 本条任务的独立证据
            ├── audit.log
            └── run/             # 与此前 task03 相同的完整 OS/Harness 产物
                ├── run.json
                ├── managed/
                └── results/     # 三角色文档、原生评分、视频和命令记录
```

汇总同时提供 `success_rate_valid = success / valid_episodes`、`coverage = valid_episodes / planned_tasks` 和 `success_rate_planned = success / planned_tasks`，避免把未执行或基础设施中断隐含算为任务失败。按 spatial / object / goal 和 task / object / swap / lan 分组统计。

- `success`、`task_failure`：只有独立验收通过且有原生判分才进入有效分母。任务失败不会自动重跑。真实策略在首次可达运动前失败、物理步数为零时，也可成为有效任务失败；其余角色、评分、媒体和清理检查仍保留。
- `infrastructure_error`：排除出有效分母。默认在确认清理完成后额外重试一次，始终创建新的 episode。耗尽重试后继续其他任务，并将整体标为 partial；显式 `--resume` 可为这些未完成任务再提供同样的有限重试预算。
- `implementation_error`、`verification_error`：不计入任务失败，停止调度，保留错误与证据。
- 清理未确认：立即停止后续调度，不自动重放未知动作，也不启动下一个环境。

续跑会校验组件内容锁、完整任务列表、执行器工具字节、episode Bundle 和日志哈希链；已完成任务会重新核验证据。若上次在 episode 完成后、写入 finished 记录前中断，会先恢复该次结果，避免再次执行。未完成的最后一行日志会保留为 `episodes.partial-*.bin` 后恢复；已封存日志的内容损坏会拒绝续跑。

CLI 完整完成时 exit=0，即使全部任务失败；partial/blocked 时 exit=2；取消时 exit=130。成功率始终以 `summary.json` 中的原生任务结果为准。

## 从新目录安装与构建

无需修改原 `work/roborsi-modular/harness-env/`。建立显式继承它的独立 venv，安装新 wheel：

```bash
work/roborsi-modular/harness-env/bin/python -m venv \
  --system-site-packages work/roborsi-campaign/harness-env

work/roborsi-campaign/harness-env/bin/python -m pip install --no-index --no-deps \
  work/roborsi-campaign/wheels/metaos_roborsi_campaign-0.1.0-py3-none-any.whl

./metaos-campaign panel --count all --seed 0 --tool-budget 24 \
  --output work/roborsi-campaign/panel-120.json

python scripts/prepare_roborsi.py \
  --harness-python work/roborsi-campaign/harness-env/bin/python \
  --os-python work/roborsi-modular/os-env/bin/python \
  --libero-root /data3/xzx/roborsi-repro/LIBERO-PRO \
  --bddl /data3/xzx/roborsi-repro/LIBERO-PRO-assets/bddl_files \
  --init /data3/xzx/roborsi-repro/LIBERO-PRO-assets/init_files \
  --pyroki-python /data3/xzx/roborsi-repro/venv-pyroki/bin/python \
  --pyroki-port 15559 --gpu 3 \
  --panel work/roborsi-campaign/panel-120.json \
  --output work/roborsi-campaign/assembly \
  --run-root work/roborsi-campaign/runs

./metaos-local build work/roborsi-campaign/assembly/local.json \
  --resources work/roborsi-campaign/assembly/resources \
  --output work/roborsi-campaign/episode-bundle
```

以上创建命令要求目标不存在；重新构建时使用新的目录名。这里仍是本机路径引用的内容锁定构建，独立 venv 明确依赖原 Harness 环境；不是完整的跨机器离线发行包。

设备白名单包含锁定 panel 的所有任务。调度器仅通过受校验的环境变量 `METAOS_ROBORSI_EPISODE_INDEX` 选择其中一项；episode runner 校验索引并生成单任务 experiment，再交给现有 SDK 运行。环境变量不能覆盖模型、任务白名单或预算。

当前交付验证包含定向 fixture 检查、安装字节及 10/20/120 条组装计划验证；没有运行真实的 10/20/120 条 campaign。模块就绪状态见 `evidence/roborsi-campaign-latest.json`，它与单任务的真实仿真证据 `evidence/roborsi-modular-latest.json` 分别记录。
