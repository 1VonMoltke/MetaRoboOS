# P0：不依赖评测器的最小具身执行闭环

状态：已实现并完成完整验收（2026-11-04）。范围：单机、CPU、本机 HTTP、独立协作式 Mock 设备与显式授权。

本页保留 P0 历史验收范围；P1/P2 的新增实现、当前证据与真实 27B 阻塞项见 [P1/P2 交付记录](p1p2-delivery.md)。

## 本轮交付

- [x] 最小资源、能力、命令、状态、控制租约契约与行为测试。
- [x] 不要求 Agent/Environment/Episode 的 SystemSpec 通用组件图与执行入口。
- [x] 独立 SDK 插件、异步动作、反馈、取消、执行网关、认证和租约代次检查。
- [x] 离线独立安装、部署搬迁、故障验收、原 modularize-demo 回归与原始证据。

## 设计决策

- 使用 roborsi-sdk 的公开生命周期与能力契约；SystemSpec 与已有 Episode AssemblySpec 分开，不改写原评测入口。
- OS 环境不安装 roborsi core 或评测器，证明其启动不依赖 benchmark/case。
- metaos-contracts、metaos-runtime 与 metaos-mock-arm 独立发行；运行时不导入具体 Mock 实现。
- 客户端预配置随机 Bearer 凭据，执行网关确定调用方，不信任请求中的 owner 字段。
- 控制租约有有效期与单调代次；调用方退出后由网关独立检查租约，按期停止在途动作。
- 命令身份去重与结果查询优先于重试。客户端失去提交响应时报告 outcome_unknown，不自动重新执行。
- 插件在网关内协作执行；本轮不宣称对恶意/阻塞插件的强制隔离或真实设备停止资格。
- 多节点调度、管理器崩溃后的物理状态对账、真实硬件与 Factory 不属于本次 P0 交付。

## 必须验证

正常执行；并发争用；旧代次拒绝；过期/非法/陈旧状态拒绝；取消与停止确认；执行失败；丢失响应后查询且不重复执行；独立调用方退出后租约到期停止；插件移除；离线冷重建；版本/锁/产物篡改拒绝；清理与原 demo 回归。

## 本次结果

完整证据索引：[evidence/p0-latest.json](../evidence/p0-latest.json)。当前保留的最近一次完整 P0 回归位于 `work/p1p2-final-03/p0-regression/`；下表证据均相对该目录。2026-11-06 已按用户要求删除更早的独立验收产物；当前回归的日志、测试结果、SQLite、事件、wheel、部署物及冷环境仍保留。

| 验收层 | 实际结果 | 证据 |
| --- | --- | --- |
| SDK-only | 8 项通过，无 runtime/core/评测器依赖 | `sdk-only-tests.json`、`logs/tests-sdk-only.log` |
| 系统测试 | 36 项通过：11 装配、8 契约、17 真实服务测试 | `producer-tests.json`、`logs/tests-producer.log` |
| 交付验收 | 14 类检查通过 | `acceptance-summary.json` 的 checks 与 commands |
| 离线闭包 | 16 个发行包，含 SDK 和三个新包；不含 core、示例评测包、Torch、NumPy | `relocated/p0-deployment/deployment.json` |
| 冷执行 | normal succeeded；cancel cancelled；expire cancelled，停止确认成立 | `cold-actions.json`、`cold-runtime/events.jsonl` |
| 原 demo | SDK-only 7 项测试、10 类验收通过；基线 9 步，新模型 5 步，两者 score=1.0 | `demo-regression/acceptance-summary.json`、对应 logs |
| 清理 | 17 个测试网关与冷网关正常退出，控制租约清空、句柄关闭、ready 移除 | 每个场景的 process.json / shutdown.json，及 cold-process.json |

14 类交付检查包括安装包边界、两类测试环境、两节点通用图规划、运行前锁漂移拒绝、离线搬迁冷安装、冷执行及清理、内容篡改拒绝、缺失 wheel 拒绝、未知 schema 拒绝、错误身份/已存在安装目标拒绝、安装后代码篡改与恢复、插件卸载恢复、进程清理。每条记录对应实际命令与退出码。

故障测试使用真实本机 HTTP、服务子进程和调用方子进程：调用方 `os._exit(0)` 后由网关停止动作；转发代理在服务端接受命令后丢弃响应，客户端观察 OutcomeUnknown，再查原 ID，设备执行计数保持 1。停止未确认则保留 outcome_unknown 并锁定资源，不将取消请求伪装为停止成功。

验证过程中修复了嵌套 NaN 在 JSON 序列化时被替换为空值而漏过校验的问题；故障注入改为首次 tick 触发，避免短动作先完成而漏测故障。最终结果来自重新构建的 wheel 与全新环境。服务启动也拒绝已有 ready 文件，关闭时先停止新控制并等待有界 HTTP 请求结束，再关闭记录存储。

## 交付文件与复跑

- [README](../README.md)：目录、离线复跑、人工启动与操作命令。
- [接口与扩展说明](p0-api.md)：SystemSpec、DeviceAPI、鉴权、租约、命令与故障语义。
- `packages/metaos-contracts`、`packages/metaos-runtime`、`plugins/mock-arm`：三个独立发行包。
- `configs/p0-system.yaml`、`configs/p0-monitor.yaml`：单设备与两节点装配。
- `scripts/bootstrap_p0.py`：完整构建/验收入口；可指定不存在的 `--output`。
- `scripts/exercise_p0.py`：人工对正在运行的服务执行三类动作。

```bash
cd /data3/xzx/MetaRoboOS
/opt/miniconda3/bin/python -I -B scripts/bootstrap_p0.py
```

仅在所有环节及原 demo 回归通过后，脚本才更新最新证据。脚本默认保留每次输出；当前工作区已按用户要求清理较早产物，仅保留最新依赖与证据，见 [保留规则](workspace-layout.md)。原 demo 回归使用其冻结 M5 wheel 作为基线，不能据此声称当前原仓库全部未提交修改都经过了全仓测试。

## 未交付范围

- 没有完成 P1/M6 异构本体适配、真实 LIBERO/27B 评测或真实机器人资格。
- 设备插件仍在网关进程内协作执行，没有阻塞/恶意插件隔离、硬实时或物理安全保证；10 ms 为循环目标间隔，不是停止时延保证。
- 支持调用方退出、正常服务关闭与响应丢失；不支持网关 SIGKILL 后的物理状态恢复/对账，也没有 workspace resume。
- 新入口只交付 package/config-only 部署，不支持外部资产、容器镜像或跨平台迁移。原 M5 的资产能力未自动迁移到新入口。
- 本机凭据预配置及 HTTP 权限检查不等于多用户主机隔离；同一 owner 共享完整凭据文件时，不能把其中调用方视为互不信任的安全主体。
- 未实现完整实例目录、长期运行治理、分布式调度、Factory 或全栈应用生态。

## 下一步如何开始

先执行 README 的人工验收：查看资源与能力，分别完成正常动作、取消、租约过期，最后 Ctrl-C 并核对停止与清理记录。认可该接口与故障语义后进入 P1，按三个可验收增量推进：

1. **第二设备与原始语义**：选定已有仿真环境/设备，核对实际动作/观测空间、单位、坐标系和停止能力；独立打包，不先改通用核心。
2. **显式 Adapter 与拒绝路径**：分别保留原始能力和转换能力，声明转换契约及限制；缺 Adapter、错误维度/单位/坐标系时，在动作执行前拒绝。
3. **同工作流替换及资格报告**：只安装新包和替换 SystemSpec，完成两设备的正常/取消/争用/失联与冷重建；保持 P0 和原 demo 回归通过。

P1 的验收重点是异构语义与可替换执行，而不是再拆目录。之后在 P2 引入独立设备适配进程、实例管理与管理器重启对账，再让 Factory 基于已经稳定的系统 API 生成 Harness。
