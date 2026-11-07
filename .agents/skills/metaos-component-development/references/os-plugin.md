# OS 插件：设备与显式 Adapter

## 真实接口

源码依据：[Mock/Monitor provider](../../../../plugins/mock-arm/src/metaos_mock_arm/__init__.py)、[单位转换 Adapter](../../../../plugins/joint-degrees/src/metaos_joint_degrees/__init__.py)、[OS 组装器](../../../../packages/metaos-runtime/src/metaos_runtime/composition.py)。

provider 工厂通过 `roborsi.components` 返回实现以下方法的对象。这里使用 `roborsi.contracts` 的类型，与 Harness 的 `Descriptor/Provider` 不同。

| 方法 | 返回/行为 |
| --- | --- |
| `describe(self)` | `ComponentDescriptor`：ID、kind、版本、`provides`、`dependencies`、配置 Schema |
| `plan(self, spec, context)` | `ComponentPlan`；先 `context.check()`、核对 ID/配置；声明实际文件等输入，供内容锁核验 |
| `prepare(self, plan, context)` | 核对 descriptor、`plan.verify_inputs()`，返回 `PreparedComponent(plan)` |
| `open(self, prepared, context)` | 从 `prepared.plan.config` 和 `context.binding(port).value` 创建对象，返回 `ComponentHandle(id, value, cleanup=...)` |

声明依赖使用 `DependencyPort(name=..., capability=Capability(contract=...))`，SystemSpec 用 `bindings` 显式指定提供者。依赖由组装器先打开，消费者先关闭；消费者不重复关闭依赖所有者的设备。

```toml
[project.entry-points."roborsi.components"]
"embodiment.my_device" = "my_device:provider"
```

将包放到 `plugins/` 不会自动注册到已安装环境。根据目标解释器和离线 wheelhouse 构建/安装，参考已有插件 `pyproject.toml`；OS 环境维持轻量依赖，重型仿真/推理放入独立运行环境。

## 设备契约与导出

导出设备实现 [DeviceAPI](../../../../packages/metaos-contracts/src/metaos_contracts/__init__.py)：

- `describe_resource()` 返回 `ResourceDescriptor` 和动作能力的参数、单位及 frame。
- `read_state(clock_id)` 返回 `StateSample`，使用单调时钟及相应 `clock_id`。
- `start(capability, arguments)` 校验动作后返回 `DeviceOperation`，其 `tick(elapsed_s)` 返回 `TickResult`。
- `stop(reason)` 返回真实的停止确认；`close()` 清理本次资源。未知执行结果由命令查询对账，不自动重放动作。

普通能力消费者（例如 Monitor）不必实现 DeviceAPI，也不进入 `exports`。Adapter 需要保留底层设备的 `control_domain`；同一控制域不能同时以原设备和 Adapter 导出两份控制权。

SystemSpec 可从 [managed-mock.yaml](../../../../configs/managed-mock.yaml) 改起：

```yaml
schema_version: 1
api: metaos.system/v1
components:
  device:
    component: embodiment.my_device
    version: '==0.1.0'
    config: {resource_id: robots/my-device}
exports: [device]
resources: {device: robots/my-device}
```

上面是接入模板，需先实现并安装该 provider。`resources` 是 managed 模式中导出别名到资源 ID 的声明，必须覆盖所有 exports，并与设备实际资源身份一致。

## 开发验证

`OS_PY` 使用安装了新 wheel 的独立 OS 解释器；`SYSTEM_SPEC` 指本次声明。

```bash
"$OS_PY" -I -B -m metaos_runtime plugins
"$OS_PY" -I -B -m metaos_runtime plan --spec "$SYSTEM_SPEC" --root "$PWD"
```

设备验证参考 [P0 测试](../../../../tests/p0/test_system.py)、[组合测试](../../../../tests/p0/test_composition.py)；Adapter 还需覆盖语义不兼容与控制域重复导出。独立进程/故障语义参考 [P1/P2 测试](../../../../tests/p1p2/test_semantics.py)。

需要运行时按 [组装运行](../../metaos-harness-assembly/SKILL.md) 创建新工作区；优先 CPU Mock 的 Manager/Worker 验证，再执行任务确实需要的真实设备或仿真检查。
