---
name: metaos-harness-assembly
description: 在 MetaRoboOS 中选择和绑定已有组件、生成工作区、构建内容锁并启动或续跑 Harness；适用于本地进程装配、RoboRSI 单任务和 LIBERO campaign。
---

# MetaRoboOS Harness 组装运行

先判断用户需要业务图、外层工作区，还是运行已有 Bundle。阅读 [工作流与命令](references/workflows.md) 中相应分支；不要为复用已有组件而默认新增 provider。

## 组装顺序

1. 明确目标链路、任务/seed/预算、模型接口、环境资产、解释器和资源。原 RoboRSI 三角色与 SenseNova 单 Agent 使用不同 prepare 脚本；从 [架构地图](../metaos-architecture/references/current-architecture.md) 确认。
2. 核查目标环境中的组件发现结果和配置 Schema，再编写显式端口绑定。新接口未实现时先按 [组件开发](../metaos-component-development/SKILL.md) 完成适配。
3. 建立新工作区；先 plan，再构建业务图与外层 Bundle，最后 verify。build/run 不隐式安装缺失依赖。更改模型、组件、任务白名单或预算后重建受影响的锁。
4. 对需要执行的任务，使用独立输出目录启动。使用的 GPU、端口和服务须与已有运行相容；仅关闭本次拥有的进程，保留共享服务。凭据通过资源 `env` 名单传递，不进入声明和锁。
5. 按用户需要报告构建完成、后台作业提交、实际执行或最终判分，分别提供相应证据。

## 判断完成的依据

- 构建：CLI 成功、输出 Bundle/lock 存在，并且 verify 通过。plan 成功不证明模型/仿真能运行。
- 后台启动：保存命令、PID/进程身份、日志和输出位置，确认进程存活。若后台作业还在准备/构建，明确说明尚未开始仿真；不要把提交当成测评完成。
- 用户要求“不用监控”时，完成一次启动确认后返回。后续用户询问状态时再检查，无需建立长期轮询。
- 任务结果：检查 `run.json`、真实模型 trace、原生 score/视频和清理证据。应用 exit=0 不是任务成功；任务失败也不等于基础设施失败。
- 多任务进度：用 campaign 的 summary 和 started/finished 事件，并结合活进程核查。`pending` 可能包含已开始但尚未 finished 的任务；`status=partial` 在进行中也可能正常。

## 常见问题

| 现象 | 下一步 |
| --- | --- |
| 找不到 provider | 核对解释器、entry point、非 editable 安装及 ID，避免只检查源码目录 |
| 缺端口、语义不符 | 修正声明/adapter；保留契约校验 |
| frozen lock 漂移 | 找出变更输入，重新安装/构建新 Bundle；不要手改 lock |
| 大型环境校验耗时 | 查看构建进程与日志；先区分磁盘扫描和模型请求，不重复启动同一作业 |
| 模型 400/401/无 tool_calls | 检查实际端点、鉴权、图像 MIME、tools/parser 与输出预算；单独做最小兼容性检查 |
| 任务中断或清理未确认 | 保留证据，检查本次资源归属；按具体执行器的恢复规则处理，不自动重放未知动作 |
