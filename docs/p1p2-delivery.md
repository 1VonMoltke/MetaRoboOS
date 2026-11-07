# P1 / P2：异构仿真接入与单机常驻系统

状态更新至 2026-11-06：基础设施、原生仿真与外部 API 模型的真实单任务闭环通过；原 27B 指定模型验收仍外部阻塞，原 P1/P2 目标保持未整体完成。开始日期：2026-11-04。承接 MetaOS.md 第 8 节与已完成 P0。

2026-11-04 用户确认远程显存不足，原 27B 服务无法启动。2026-11-05 改用原有 API 配置的 `deepseek-ai/DeepSeek-V4.1-Flash`，完成 `libero_spatial_task-t00-i00`、seed 0：原生 success / score=1.0，303 个物理步、64 个决策步，约 43 分钟，模型调用、视频与清理均已核验。见 [API 证据](../evidence/libero-api-latest.json)。`evidence/p1p2-latest.json` 保留原 27B 目标的 `goal_complete=false`，不混淆模型身份。

## 完成条件

- [x] P1：独立 LIBERO-Pro 插件、显式动作语义/转换、图像/状态与会话身份；通过新系统网关执行，保留原评分协议。
- [x] P1：缺少适配器、错误维度/单位/坐标系、旧会话与旧状态在执行前拒绝；Mock 与真实 LIBERO 两条路径均实测。
- [x] P1：27B Harness 复用原 Agent、ICL 与 CaseRunner，经系统客户端调用能力；独立续租，实际模型连接故障/超时/Agent 退出清理；确定性策略验证原生评分/视频/轨迹。
- [ ] P1：真实 27B 单任务闭环，取得实际模型响应与经新网关执行的动作、评分、媒体和清理证据。
- [x] 外部 API 模型单任务闭环：原 Agent/ICL → 新系统网关 → 真实 LIBERO-Pro → 原生成功评分/视频/trace → 清理。
- [x] P2：独立适配器进程、实例/资源目录、创建/启动/停止/受控重启、期望/实际状态、健康检查与命令任务 API。
- [x] P2：执行端拥有最终租约与命令检查；管理器丢失由执行端独立停止，接管前撤销旧控制权并对账；未知结果不自动重放。
- [x] P2：管理器 SIGKILL、适配器退出/阻塞、调用方退出、响应丢失、多调用方竞争、旧代次恢复、时序错误、权限边界均有实际证据。
- [x] 交付：独立 wheel、原生镜像/资产身份、预检、搬迁冷重建、诊断及敏感值过滤；P0 与原 modularize-demo 完整回归。
- [x] 文档：实际命令、逐项验收、原始证据索引、运行条件与未覆盖范围，与当前实现一致。

## 当前交付与证据

基础设施汇总见 [evidence/p1p2-latest.json](../evidence/p1p2-latest.json)，由 `scripts/verify_p1p2.py` 审核原始结果后写入，34 项审核通过，保留真实 27B 未完成状态。最新真实 API 实验另见 [API 索引](../evidence/libero-api-latest.json)。2026-11-06 按用户要求移除重复索引、旧版本和旧实验；下表当前资格验收证据保留，删除清单见 [cleanup-latest.json](../evidence/cleanup-latest.json)。汇总核对源码与 wheel 来源、报告哈希、媒体字节及资源清理。

| 验收层 | 结果与范围 | 原始证据 |
| --- | --- | --- |
| P1/P2 进程与语义矩阵 | 20 项通过，真实 HTTP/Manager/Worker/调用方进程 | `work/p1p2-final-03/p1p2-tests.json` |
| 原生 API 资格 | 当前冷部署 9 项通过；确定性 7D OSC，原 2000 物理步、原评分、91 个输出，非模型策略 | `work/p1p2-final-03/cold-native/report.json` |
| OS 冷交付 | 原生镜像归档与协议文件搬迁、删除临时输入、离线安装、完整冷执行与回归均通过 | `work/p1p2-final-03/acceptance-summary.json` 与 `logs/` |
| 独立 Harness | 搬迁冷安装通过，Harness 0.1.1，13 项原 CaseRunner/媒体/超时/续租/不重放、启动失败落盘与会话复用测试通过；普通符号链接 venv 实测通过 | `work/p1p2-harness-011/acceptance-summary.json`、`harness-tests.json` |
| Harness 交付拒绝路径 | 4 项通过：错误期望指纹、声明文件字节变化、清单变化、链接资产；均在创建目标环境前拒绝 | `work/p1p2-harness-011/delivery-negative-tests.json` |
| 原 Agent/真实 ICL | 实际原 Agent 与任务专用 ICL/tokenizer reset、原 HTTP 服务 reset、进程退出通过；没有 act/推理 | `work/p1p2-harness-011/original-inputs/report.json`、`original-service/report.json` |
| 原 Agent 模型故障 | 实际 CLI + 原 Agent + 新网关 + 真仿真；故意使用关闭的本机端口，得到未评分的 agent_error、取消/释放与另一个 caller 接管；实际过去的原生状态序号被拒绝；未联系远程模型 | `work/p1p2-harness-011-failure/report.json` |
| 原 Agent 退出/模型超时 | 实际 CLI 和原 Agent 到达本地模型请求后，分别强制退出所拥有的 Agent 与触发规划器超时；验证未评分结果、取消/释放、替代 caller 与旧状态拒绝，未执行模型推理 | `work/p1p2-harness-011-agent-exit/report.json`、`work/p1p2-harness-011-model-timeout/report.json` |
| 原 CaseRunner 真仿真 | 确定性策略经完整 Harness → 新网关通过，保留 2000 个物理步、原生评分、媒体、Runner trace 与清理 | `work/p1p2-harness-011-native/report.json` |
| 媒体可读性 | 两条完整原生链路共 129 张相机图像可读；各自 2001 帧 H.264 MP4 完整解码通过 | 两次原生运行目录的 `media-validation.json` |
| P0 与原 demo | 当前源码的 36 个系统测试、8 个 SDK-only 测试、交付拒绝路径和完整 demo 全通过；demo 为 7 个 SDK 测试与 10 类验收 | `work/p1p2-final-03/p0-regression/acceptance-summary.json` |
| 真实 27B 推理 | **未验收，外部阻塞**；不将 reset、端口故障或确定性动作当成模型闭环 | 无真实单任务推理通过记录 |
| 真实 API 模型单任务 | **success / score=1.0**；303/2000 物理步，42 次模型响应（26 规划、16 上下文压缩），195 张图像与 304 帧视频核验通过 | `work/libero-api-task00-20261105-02/report.json` |

实际原 ICL 为 `libero-icl-v18-persistent-memory-libero_spatial-task-00`，manifest SHA-256 为 `e01858b3917ab1e82aab93da2bb9e86f40fd81516236b11461e8a00049704382`；tokenizer SHA-256 为 `0997f410c57a1f4e53b09e4be8f4a172d90edd9564368fb0847030937229b9f3`。资产来自实际评测目录，不用 M5 的有意 fixtures 替代。

原生镜像固定为 `sha256:b3630a7ccb37db5a7397b63a0aeb2eae6835aeeaea99559a5d2d0905eb551212`；本轮冷部署配置 GPU 2，使用默认 Docker engine。其他已有容器与自定义 engine 未纳入清理范围。

## 已完成实现

- contracts/runtime 0.2.0；Mock、degrees Adapter、LIBERO Device、LIBERO Harness 各自独立发行。OS 不安装原 core/评测器/模型依赖；Harness 单独安装。
- Manager 管理认证、持久实例目录与路由；独立 Worker 打开完整 SystemSpec 图并持有原 Gateway 执行状态机，避免管理端与执行端各维护一套租约状态。
- Worker 以独立心跳锁接收管理器心跳；接管后立即启动心跳再落盘目录。已用阻塞超过 watchdog 预算的确定性测试覆盖慢准入与慢目录落盘两种误判。
- 明确的资源身份、进程 PID/启动 ticks/boot ID、workspace flock、管理器 generation/clock 与租约 epoch；受控停止、恢复、重启与对账。重启未知动作不重放。
- LIBERO 保留原 7D OSC、原工具查询、原会话与原评分预算；运维取消不产生有效任务评分。媒体通过权限检查后的分块 API 下载，核对会话、step、大小和哈希。
- 原 Harness 0.1.1 复用原 Agent、ICL 与 CaseRunner；独立续租，不依赖模型返回；明确传入实际模型名/API 地址，密钥留在环境中。保留普通符号链接 venv 的解释器身份，记录 `agent-runtime.json`；Agent 启动失败仍发布未评分结果与最终 trace，关闭的 Adapter 禁止跨会话复用。
- OS 与 Harness 分别提供冻结 wheel、资产/镜像身份、离线安装与搬迁校验；原始日志、SQLite、命令身份、score、trace 与视频保留供人工审查。

## 剩余完成条件

1. 原 27B 指定模型：在显存足够的远程机器恢复服务，按 [操作说明](p1p2-api.md) 补齐其独立验收。
2. 外部 API 模型闭环已完成；后续扩大任务与 seed 覆盖，保持模型身份、任务失败与基础设施失败分别记录。

以上完成之前，不能将 P1/P2 整体标为完成。两次确定性原生任务均正常结束为 `task_failure / score=0`，表示原任务未成功，基础设施和原评分链路有效；不能当作模型任务成功率。当前任务 API 是动作命令 API，不是完整 Agent-job 调度；本阶段也不宣称全基准模型性能、跨节点调度、长期生产 SLO、恶意插件沙箱、硬实时停止或真实硬件安全资格。

## 审核复跑

```bash
/opt/miniconda3/bin/python -I -B scripts/verify_p1p2.py \
  --infrastructure work/p1p2-final-03 --harness work/p1p2-harness-011 \
  --native-harness work/p1p2-harness-011-native \
  --failure-harness work/p1p2-harness-011-failure \
  --agent-exit-harness work/p1p2-harness-011-agent-exit \
  --model-timeout-harness work/p1p2-harness-011-model-timeout \
  --output evidence/p1p2-latest.json
```

该命令审核保留的基础设施原始结果，不重新执行仿真，也不会把原 27B 待验收改为完成。API 单任务的评分、媒体与模型记录可运行 `work/p1p2-harness-011/cold/bin/python -B work/libero-api-task00-20261105-02/audit.py` 重新核验，不调用模型。新建实验见 [API 实验说明](libero-api-experiment.md)。

## 实施顺序

1. 执行端进程与最终授权：复用 P0 Gateway，置于独立 worker；公共管理器仅做认证、路由和生命周期，避免两套执行状态机。
2. 持久实例目录与恢复：记录所拥有的进程身份、内容锁、期望状态和实际状态；同一目录只允许一个管理器；重新接管必须先停止/对账，不能盲目重启。
3. LIBERO 原生服务桥接、显式能力、会话/图像与评分；先跑确定性动作，再接原 27B Harness。
4. 资产与镜像交付、完整故障矩阵、冷重建、原回归及完成审计。

## 实现约束

- 不清理或重构原仓库的既有修改；原生仿真、Agent 与 ICL 优先复用已验证实现。
- 单机本地进程/容器边界；资源准入与强制隔离分别报告，不宣称真实硬件安全、硬实时或恶意插件沙箱。
- 命令任务与 Agent 推理会话分别记录；仿真失败、模型/Agent 错误、基础设施故障分别统计。
- 当前 P0 运行环境仍可用于回归；新版本从独立 wheel 安装，禁止临时修改 site-packages 或借用源码搜索路径。
- 不必将本阶段扩成 Factory、UI、跨节点调度；P3–P5 不在本目标完成结论内。
