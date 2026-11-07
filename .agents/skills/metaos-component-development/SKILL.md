---
name: metaos-component-development
description: 在 MetaRoboOS 中新增或替换 OS 设备插件、Adapter 或 Harness 业务 provider，定义接口、注册发行包并验证模块可发现和可组装时使用。
---

# MetaRoboOS 新组件开发

先根据用户目标选择组件层级。边界不清时阅读 [架构地图](../metaos-architecture/references/current-architecture.md)，不要默认所有组件使用同一个 SDK。

## 开发路线

| 类型 | 必需参考 | 最小交付 |
| --- | --- | --- |
| OS 设备、仿真或显式 Adapter | [OS 插件接口](references/os-plugin.md) | SDK provider、DeviceAPI 或能力消费者、SystemSpec、安装与能力测试 |
| 模型、角色、上下文、工具、结果或业务编排 | [Harness provider 指南](references/harness-provider.md) | Descriptor、factory/业务对象、entry point、HarnessSpec、端口/行为测试 |
| 仅新增资产或启动已有服务/应用 | [组装运行 Skill](../metaos-harness-assembly/SKILL.md) | `resource.json` 与显式绑定；通常无需增加 Python provider |

1. 写清职责、输入输出、依赖端口、配置、语义与资源所有权。复用现有契约时，检查调用方如何使用返回值，不只对照方法名。
2. 在 `plugins/` 新建独立发行包，或按目标修改已有包。注册唯一 entry point，保持 ID 与 descriptor 一致；正常扩展无需修改 Registry 白名单。
3. `describe/plan` 保持无设备、模型调用和进程启动；在运行阶段打开资源。部分打开失败的资源要在 provider 内自行清理，已成功返回的句柄由 Host/OS 逆序关闭。
4. 增加能独立使用的最小声明和与行为相关的检查。源码与发行包身份分开验证：构建非 editable wheel，安装到新环境，再进行发现、plan/build/verify。
5. 交付源码/声明、安装环境、执行命令和结果；明确仅完成 CPU/fixture 验证还是已进行真实设备/模型验证。

## 验证范围

- 正确绑定能调用真实依赖；不兼容契约、缺端口、非法配置会被拒绝。涉及动作时检查维度、单位、坐标系和控制器。
- `plan` 不打开资源；正常退出和部分打开失败能关闭已拥有资源。自建进程的关闭逻辑需要覆盖它们自己的子进程。
- 已安装 entry point 可被发现；源码改动不自动改变旧 Bundle；重新安装后旧锁漂移会被拒绝。
- 按变更范围运行相应测试；不要为文档改动重跑完整 GPU 基准。新契约不在 Harness 的 `CONTRACT_PROTOCOLS` 表时，需由该插件的接口测试证明行为，Host 不会自动检查任意方法集合。

与已有实验共享机器时，在独立目录/venv 中完成开发验证。替换工作区中运行任务依赖的包会使锁漂移，因此不要在活动环境中试装新 wheel。
