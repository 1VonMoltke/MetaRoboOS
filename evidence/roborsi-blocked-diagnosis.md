# RoboRSI 第二条阻塞诊断与修复

日期：2026-11-07。对象：`work/roborsi-campaign-27b/runs/eval-20` 第 2 条 `libero_object_task/0`，attempt-01。原始运行证据保留不变。

## 能确认的故障链

1. `look(camera=head)` 完成后执行 `visual_pick_place(source=cream cheese, target=basket, placement=container)`。
2. 原生进程被终止时，栈停在 `detect_object/robotwin/policy.py` 的模型加载 `model.to(_DEVICE).eval()`，经 LIBERO 的 `vlm_sam` 定位路径进入。并非远程 27B 推理栈。没有足够证据证明 OOM、模型死锁或底层驱动故障。
3. OS 事件中，该命令 accepted 到 lease_released 约 333 秒，没有终态。因此不是已经证实的 650 秒命令超时。触发 Harness 退出的首个异常未保存在旧日志中，不能进一步断言是传输超时或租约故障。
4. gateway.release 只等 2 秒，设备取消原先可能等待 TERM 5 秒再 KILL 5 秒。原生 SIGTERM 处理器抛 KeyboardInterrupt，但模型加载/释放可能延迟退出。这一时间预算不匹配可导致 `stop_unconfirmed`。
5. native-process.json 最终记录 closed=true、exit_code=-9、reason=lease_released、forced=true；但 worker 清理未确认，外层清理也未确认。原生进程退出不等于完整清理已核验。
6. Host 的 close 回调异常覆盖了执行异常，harness-error.json 只留下 APIError，campaign 因 cleanup_confirmed=false 保守阻塞。

主要证据位于原 attempt-01/run 下：`harness.log`、`results/harness-error.json`、`managed/instances/main/runtime/events.jsonl`、`managed/instances/main/runtime/components/libero/native.log` 和 `native-process.json`、`run.json`。

## 修改

- RoboRSI 设备取消：TERM 最多等 0.75 秒，再 KILL 最多等 0.75 秒；仍必须获得进程退出确认才写关闭回执。普通最终判分继续允许自然退出。此修复针对设备取消等待，不承诺任意慢磁盘、锁竞争或 OS 调度下都在 2 秒内完成。
- Harness wire client：命令查询遇短暂网络错误或 worker_unavailable 时，在原等待预算内继续查询同一 command_id；提交响应不确定时同样只查询该 ID；不重复提交动作。权限等永久错误、租约失效仍终止。
- Host：执行/打开失败时仍逆序关闭所有已打开组件，保留原始异常；附加结构化 cleanup_errors。正常运行后清理失败仍抛异常。
- 错误产物：保留原始错误类型、错误码、命令 ID（已知时）、显式 cause 的类型/错误码及清理错误；不写请求正文或凭据。租约续期错误保留原始 cause。

不放宽 campaign 对未确认清理的阻塞判定。旧实验依赖和 Bundle 未替换，旧 campaign 未重启，修复尚未经过真实 GPU/LIBERO 复测。

## 验证和使用

非 editable wheel：`work/roborsi-robustness-check/wheels/`。独立验证环境：`work/roborsi-robustness-check/env/`；只读复用旧 OS 环境的依赖路径，修改后的两个发行包实际从新环境加载。原依赖环境未安装或卸载任何包。

测试记录：[`tests.json`](../work/roborsi-robustness-check/tests.json)。覆盖 Harness 图生命周期、清理异常、部分打开失败、命令查询故障/不重复提交、真实忽略 SIGTERM 子进程、正常最终判分退出，以及 campaign 重试/恢复与执行器回归。均为 CPU/fixture 测试。

部署时在新工作区安装这两个 wheel，重新生成 Harness 锁和外层 Bundle，先运行选定失败任务进行真实单条验证，再建立新的 campaign；直接运行旧锁定工作区不会使用本修复。旧 cleanup_unconfirmed 记录保持原样，不能手改成已确认后续跑。
