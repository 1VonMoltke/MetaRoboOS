# MetaRoboOS 开发者 Agent 入口

## 任务路由

处理下列任务时，先读取对应 Skill；跨层任务按需组合，无需一次载入所有参考资料。

| 任务 | Skill |
| --- | --- |
| 理解架构、模块边界、接口归属、设计扩展点 | [metaos-architecture](.agents/skills/metaos-architecture/SKILL.md) |
| 新增/替换设备、Adapter、Harness 业务组件，注册和验证 provider | [metaos-component-development](.agents/skills/metaos-component-development/SKILL.md) |
| 选择组件、生成工作区、组装 Harness、运行/续跑实验 | [metaos-harness-assembly](.agents/skills/metaos-harness-assembly/SKILL.md) |

Skills 随仓库维护在 `.agents/skills/`。支持 Skill 调用的 Agent 可按名称选择；其他 Agent 可直接读取上面的 `SKILL.md`，无需全局安装副本。命令默认从仓库根目录执行，Markdown 相对链接按文件位置解析。

## 共同约定

- 当前实现以 `packages/*/src`、`plugins/*/src`、CLI 和测试为准。`docs/harness-architecture.md` 含目标设计；区分已实现命令和提案。状态判断需核对原始运行证据及时间。
- OS 与 Harness 的 entry point、描述类型和生命周期签名不同；先定位层级再开发。业务组件不必单独运行一个进程。
- `work/` 保存本机安装环境、锁定输入和运行产物；`evidence/` 保存验收索引。构建/实验使用新目录，变更依赖使用独立环境，避免修改活动任务依赖的已安装包或资产。
- plan/build 不启动模型或设备；运行按用户任务范围执行。凭据经声明的环境变量传递，不写入 Skill、配置、Bundle 或日志。仅清理本次拥有的资源。
- 区分构建完成、后台提交、实际运行、原生任务成功和清理核验。交付时提供相关命令、产物和验证结果；未实际运行的部分明确说明。

修改公共接口、CLI 或组件注册方式时，同步检查相关 Skill 引用和示例。保持详细定义在源码/专门文档，Skill 主要负责选择、步骤和完成标准。
