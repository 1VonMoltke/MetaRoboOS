# modularize-demo：独立 Model 插件与模块化装配

这个目录以 SenseNova-RoboRSI 的 M5 发行物为 baseline，在原仓库之外构建、安装
`model.manual_counter`，将它绑定给现有的 Agent，并沿用原评测与部署工作流。
不修改 RoboRSI core，也不使用 editable 安装、`PYTHONPATH` 或原仓库源码注入。

这是 CPU 上运行的确定性计数示例，不是 LLM、LIBERO 仿真或真实 27B 性能评测。

## 组件组合

```text
environment.example_workflow  <── 原评测运行时 ──>  agent.example_counter
                                                       │
                                             ┌─────────┴──────────┐
                                             │                    │
                                    model.manual_counter  context.example_memory
                                    （本目录独立插件）       （原示例组件）
```

| 角色 | baseline 装配 | manual 装配 |
| --- | --- | --- |
| Environment | `environment.example_workflow` | 相同 |
| Agent | `agent.example_counter` | 相同 |
| Context | `context.example_memory` | 相同 |
| Model | `model.example_counter`，每次 +1 | `model.manual_counter`，每次 +2 |
| 任务 | 从 0 达到 4、5 | 相同 |
| 预期动作步数 | 4、5，共 9 步 | 2、3，共 5 步 |
| 预期聚合分数 | 1.0 | 1.0 |

计数环境的成功条件是 `position >= goal`，因此目标 5 在每次 +2 时以 position=6 成功。
两份 Assembly 只改变 Model 的 ID 和配置；Agent 的 `model` 绑定别名不变。

## 一条命令完成安装、运行与验收

当前冻结依赖面向 **CPython 3.11 / Linux x86_64**。启动 Python 需要标准
`venv` / `ensurepip`，以及 `pip`、`setuptools>=67`、`wheel`。本机解释器已具备这些工具。
运行不需要网络、Docker、GPU、模型权重或 API key。

```bash
cd /data3/xzx/MetaRoboOS/modularize-demo
/opt/miniconda3/bin/python -I -B scripts/bootstrap.py
```

脚本每次创建新的 `work/acceptance-<随机值>/`，不覆盖之前的运行。
也可指定一个尚不存在的目录：

```bash
/opt/miniconda3/bin/python -I -B scripts/bootstrap.py \
  --output /data3/xzx/manual-counter-review-001
```

脚本会：

1. 离线构建独立插件 wheel，将冻结依赖复制到本次 wheelhouse。
2. 新建 producer venv，安装 core、SDK、原示例组件和新插件。
3. 另建 **仅 SDK + 新插件** 的 venv，执行 7 项测试，确认没有安装 core 或 torch。
4. 通过 entry point 发现插件，分别规划、锁定并运行 baseline/manual 两份装配。
5. 对比原生评分文件：两份装配都成功，步数分别为 9 和 5，非 Model 组件和 core 锁一致。
6. 检查非法配置、不兼容版本和装配锁漂移是否被拒绝。
7. 导出 manual 部署物，搬迁部署目录，删除本次复制的原输入项目，然后在全新 venv 冷安装运行。
8. 验证部署身份和装配锁未变、模块从新 venv 导入、评分仍为 1.0。
9. 篡改部署副本中的 Context 文件，确认完整性校验拒绝。
10. 卸载新插件，确认它消失、原插件保留、缺失模型无法装配；重新装回并校验。
11. 检查运行进程已退出、资源租约为空。

成功后终端输出 baseline_steps=9、manual_steps=5、cold_score=1.0。
每条实际验收命令的退出码、stdout 和 stderr 都记录在本次输出目录。

## 文件与产物

```text
plugins/manual-counter/
  pyproject.toml                         独立包元数据与 entry point
  src/roborsi_manual_counter/__init__.py   Model 与 SDK provider
  tests/test_manual_counter.py            SDK-only 生命周期与配置测试
configs/assemblies/baseline.yaml          原 Model 的对照装配
configs/assemblies/manual.yaml            新 Model 的装配
configs/runs/local.yaml                  独立的执行/评测设置
assets/counter-memory.txt                声明并冻结的 Context 输入
vendor/wheels/                          从 M5 产物复制的 26 个 wheel 与来源记录
vendor/source.json                      冻结来源、平台、wheel SHA-256
scripts/bootstrap.py                    离线构建与新环境安装
scripts/verify_demo.py                  装配、运行、迁移和失败门禁验收
evidence/latest.json                    最近一次成功验收摘要
work/acceptance-*/
  producer/                            可供人工操作的安装环境
  sdk-only/                            不含 core 的插件测试环境
  wheels/                              上述依赖 + 新插件 wheel
  baseline.lock.json                   对照装配锁
  manual.lock.json                     新模型装配锁
  project-baseline/results/             对照任务评分与轨迹
  relocated/manual-deployment/          可独立搬走的完整部署物
  cold/                                全新冷安装环境
  cold-run/project/results/             冷重建后的评分与轨迹
  logs/                                各实际命令的完整日志
  acceptance-summary.json              本次验收摘要
```

`project-manual/` 在迁移验收中会被删除，以确认部署物不依赖原项目输入。
其评分摘要、清理检查与执行日志保留在验收记录中。用户源码及 `assets/` 不会被删除。

## 人工检查和重新组装

完成一键运行后，在本目录中读取最近一次成功运行位置：

```bash
RUN=$(/opt/miniconda3/bin/python -I -B -c \
  'import json; print(json.load(open("evidence/latest.json"))["work"])')
PY="$RUN/producer/bin/python"

"$PY" -I -B -m roborsi.components list
"$PY" -I -B -m roborsi.components describe model.manual_counter
```

新建一份可编辑的人工验收项目：

```bash
mkdir "$RUN/human-project"
cp -a assets "$RUN/human-project/assets"
cp configs/assemblies/manual.yaml "$RUN/human-project/assembly.yaml"
cp configs/runs/local.yaml "$RUN/human-project/run.yaml"

"$PY" -I -B -m roborsi.assembly \
  --assembly "$RUN/human-project/assembly.yaml" \
  --run-spec "$RUN/human-project/run.yaml" \
  --root "$RUN/human-project" \
  --dry-run --lock "$RUN/human.lock.json"

"$PY" -I -B -m roborsi.assembly \
  --assembly "$RUN/human-project/assembly.yaml" \
  --run-spec "$RUN/human-project/run.yaml" \
  --root "$RUN/human-project" \
  --expect-lock "$RUN/human.lock.json"
```

修改 `human-project/assembly.yaml` 中的 `increment` 或 `goals` 后，旧锁应拒绝漂移；
审核后生成新锁，再使用新的 `run_id` 或新项目目录运行，保留历史结果。

新插件只提供 `model.counter_action/v1` 能力，与原 Agent 的 Model 依赖端口匹配。
`generate(request)` 接收 observation/context，返回 `{"increment": 2}`。
默认 increment=2，也可以配置为 1。它不推理、不读取目标、不调用外部模型。
`describe / plan / prepare / open` 均直接使用公共 SDK 实现，没有继承 core 的 BaseProvider。

修改插件实现后重跑 `bootstrap.py`，会重新构建 wheel 并生成独立的新环境与部署物；
不需要修改 core 的插件注册代码。

## 搬迁部署物

可把整个 `relocated/manual-deployment/` 复制到满足相同平台前提的另一台机器：

```bash
python3.11 -I -B /transfer/manual-deployment/install.py \
  --target /runtime/manual-counter

/runtime/manual-counter/bin/python -I -B -m roborsi.deployment verify \
  --bundle /transfer/manual-deployment --installed

/runtime/manual-counter/bin/python -I -B -m roborsi.deployment run \
  --bundle /transfer/manual-deployment --workspace /runs/manual-counter
```

安装目标与运行 workspace 必须尚不存在，并位于部署物之外。
冷安装只需要 Python 标准库与 venv/ensurepip，不需要构建工具、源码目录或原 producer 环境。

## 依赖来源与验收边界

冻结依赖来自已经验收的 M5 counter 部署物，源部署身份记录在 `vendor/source.json`。
原有第三方 wheels 中包含 installed-RECORD 重打包产物，其逐文件来源和摘要完整保留在
`vendor/wheels/wheel-provenance.json`，不将它们冒充上游索引的原始 wheel。
新插件 wheel 记录为 `local-source-wheel`。

本示例验证外部插件注册、SDK 边界、声明式装配、原工作流接入与可迁移部署。
它不证明新机器人本体、不同动作空间或真实设备的兼容性，也不宣称完成 M6。
