# 当前架构与源码入口

以下路径相对仓库根目录；命令也从仓库根目录执行。参考文档记录历史范围，实际接口以链接的源码为准。

## 层次与选择

| 目标 | 接口/注册方式 | 首先阅读 |
| --- | --- | --- |
| 新设备、仿真、单位转换、设备能力消费者 | `metaos.system/v1`；`roborsi.components` | [contracts](../../../../packages/metaos-contracts/src/metaos_contracts/__init__.py)、[OS Registry/resolve/open_system](../../../../packages/metaos-runtime/src/metaos_runtime/composition.py) |
| Planner、模型、上下文、工具、结果、编排等业务替换 | `metaos.harness-spec/v1`；`metaos.harness.components` | [Descriptor/Protocol/Provider/Host](../../../../packages/metaos-harness/src/metaos_harness/graph.py)、[业务 providers](../../../../plugins/roborsi-components/src/metaos_roborsi/providers.py) |
| 启动 OS、独立 Python 应用、服务，绑定模型与资产 | `metaos.local-harness/v1` 与 `metaos.local-resource/v1`；本地文件资源池 | [本地 composer](../../../../scripts/metaos_local.py)、[本地装配说明](../../../../docs/local-composition.md) |
| 选择 N 条任务、隔离执行、续跑与汇总 | Harness providers `campaign.roborsi_libero`、`executor.roborsi_local` | [campaign CLI](../../../../plugins/roborsi-campaign/src/metaos_roborsi_campaign/cli.py)、[campaign 文档](../../../../docs/roborsi-campaign.md) |

## 运行链路与隔离

```text
Harness Host -> Orchestrator -> 角色 / Model / Context / Toolset
Toolset -> Client / LeaseKeeper -> Manager -> Worker / Gateway -> DeviceAPI
DeviceAPI -> 原生仿真、技能执行进程 -> 原生评分与媒体
外层 metaos-local 负责本次 OS / 应用 / 服务进程的启动和退出
```

- `packages/metaos-runtime/` 管理租约、命令、进程和 HTTP；不导入具体评测器和重模型依赖。`packages/metaos-contracts/` 定义 DeviceAPI、SystemSpec、命令/状态契约，依赖独立 `roborsi-sdk`。
- 原 RoboRSI 三角色链路的 Harness/原生 Python 3.12 与轻量 OS Python 隔离；PyRoKi 又是独立环境。原生技能需实际场景对象，在原生侧执行，Python 对象不经 HTTP 传递。
- `plugins/libero-harness/` 的 SenseNova 单 Agent/ICL 链路与 `plugins/roborsi-components/` 的原 RoboRSI 三角色链路分别维护。阅读 [RoboRSI 接入](../../../../docs/roborsi-modular.md) 后再选择适配脚本。
- JOINT_POSITION 8D 与 OSC_POSE 7D 是不同控制协议；转换需要明确的 adapter 和测试。`plugins/joint-degrees/` 演示显式单位转换和共享控制域归属。

## 已实现入口

`OS_PY` 指安装 `metaos-runtime` 的解释器；`HARNESS_PY` 指安装 `metaos-harness` 及目标业务 provider 的解释器。两者不要混用。

```bash
"$OS_PY" -I -B -m metaos_runtime plugins
"$OS_PY" -I -B -m metaos_runtime plan --spec configs/managed-mock.yaml --root "$PWD"
"$HARNESS_PY" -I -B -m metaos_harness.cli components
"$HARNESS_PY" -I -B -m metaos_harness.cli plan --spec configs/roborsi-libero.harness.yaml
./metaos-local --help
./metaos-campaign --help
```

读取 [runtime CLI](../../../../packages/metaos-runtime/src/metaos_runtime/cli.py) 和 [Harness CLI](../../../../packages/metaos-harness/src/metaos_harness/cli.py) 获取实际参数。当前 Harness CLI 的 `run` 有 LIBERO experiment/result 限制；通用 `Host` 可以组装其他契约，自定义应用需要自己的入口。

`./metaos-campaign` 是依赖 `work/roborsi-campaign/harness-env` 的本仓库快捷入口，不是自动创建环境的安装器。缺环境时按对应文档准备，不能宣称任意克隆都可直接运行。

## 声明、内容锁与可复现范围

| 层 | 构建输入 | 关键输出 |
| --- | --- | --- |
| OS | SystemSpec、已安装 OS 插件、计划声明的文件/镜像输入 | `metaos.lock/v1`；离线部署目录通过 `metaos_runtime bundle export` 生成 |
| Harness | 组件图、JSON Schema、端口契约与语义、已安装实现包 | `harness.json`、`harness.lock.json`、`plan.json` |
| 本地装配 | 资源池、组合声明、已安装 Python、文件/目录、OS plan | `bundle.json`、`system.lock.json`；本机路径引用锁 |
| 单次运行 | Bundle、任务/seed/预算、环境凭据 | `run.json`、Harness result、原生 score/视频、清理证据 |

源码目录、wheel、已安装环境和 Bundle 是四个不同对象。源码修改后应构建 wheel、安装到目标隔离环境，再重建受影响的锁。不能手改 lock 让旧构建“通过”。本地 Bundle 不复制整个 venv/资产，也不是跨机器离线发行包。

`work/` 是本机环境与运行产物，`evidence/` 是验证索引，`configs/` 是声明示例；它们不应反向成为包核心逻辑的依赖。可复用示例优先放源码目录并说明所需环境。

## 按问题继续阅读

- 设备接入：[Mock + Monitor](../../../../plugins/mock-arm/src/metaos_mock_arm/__init__.py)、[SystemSpec 示例](../../../../configs/p0-monitor.yaml)。
- 业务契约：[Graph/Host 测试](../../../../tests/test_harness_graph.py)、[角色与模型实现](../../../../plugins/roborsi-components/src/metaos_roborsi/components.py)。
- LIBERO 细粒度组合：[默认图](../../../../configs/roborsi-libero.harness.yaml)、[prepare 脚本](../../../../scripts/prepare_roborsi.py)。
- 运行结论：[单任务核验器](../../../../scripts/verify_roborsi_run.py)、[工作区布局](../../../../docs/workspace-layout.md)。`application_completed` 表示应用退出码为零，原生 score 表示任务结果，audit 表示证据/清理核验，三者分别判断。
- 长期设计：[MetaOS.md](../../../../MetaOS.md)、[Harness 架构设计](../../../../docs/harness-architecture.md)。不要从提案复制尚未实现的命令。
