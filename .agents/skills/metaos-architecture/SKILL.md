---
name: metaos-architecture
description: 解析 MetaRoboOS 当前架构、组件边界、接口归属和实现状态；在理解项目、选择扩展位置或设计模块拆分时使用。
---

# MetaRoboOS 架构导航

把用户目标定位到当前代码中的扩展点，给出可复查的接口和数据路径。先阅读 [当前架构地图](references/current-architecture.md)，再按任务选择源码；无需通读所有历史交付文档。

## 定位工作

1. 判断涉及 OS 设备/能力、Harness 业务组件、外层进程/资产装配，还是任务调度。跨层任务分别列出需要改动的接口。
2. 查相应 Registry、契约和一个真实 provider；以源码和 CLI `--help` 核对文档。`work/` 中的已安装包可能落后于 `src/`，比较版本与内容，不能把源码可导入当作已安装。
3. 用一条调用链说明输入、输出、绑定、进程归属和结果位置。组件粒度由可替换职责决定，一个组件不必是一个进程或发行包。
4. 将发现标为“已实现”“设计目标”或“本机运行观察”。涉及当前任务状态时，核对原始 run/score/audit、进程身份及时间；历史 `latest` 索引不保证反映正在进行的实验。

## 必须区分的概念

- OS 的 `roborsi.components` 与 Harness 的 `metaos.harness.components` 使用不同的描述类型和生命周期签名。
- `metaos.harness-spec/v1` 组装同进程业务对象；`metaos.local-harness/v1` 协调 OS、应用、服务和资产；`metaos.system/v1` 导出设备能力。
- 本仓库给开发者 Agent 的这些 Skills，与 `skills.roborsi_libero` 管理的机器人策略 `SKILL.md / policy.py` 属于不同用途。
- [Harness 架构设计](../../../docs/harness-architecture.md) 中完整 `metaos install/build/run`、远程 Catalog、通用依赖求解仍是目标；实际命令见架构地图。

## 交付检查

回答应能说明：改哪一层、入口文件在哪、绑定哪个契约、需要什么环境、如何验证。提议新接口时标明新增范围，不把同名契约当作动作维度、单位、坐标系和模型模态已兼容的证据。

需要实施组件时使用 [新组件开发](../metaos-component-development/SKILL.md)；需要生成工作区或执行实验时使用 [Harness 组装运行](../metaos-harness-assembly/SKILL.md)。
