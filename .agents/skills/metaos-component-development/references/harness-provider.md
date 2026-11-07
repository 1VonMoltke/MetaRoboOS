# Harness provider：业务组件

## 接口与约束

依据 [graph.py](../../../../packages/metaos-harness/src/metaos_harness/graph.py) 和 [现有 providers](../../../../plugins/roborsi-components/src/metaos_roborsi/providers.py)：

```python
from metaos_harness import Descriptor, Provider

def provider():
    return Provider(Descriptor(
        id="results.my_provider", version="0.1.0",
        provides="metaos.roborsi.results/v1",
        requires={},
        config_schema={"type": "object", "additionalProperties": False},
    ), factory)

# factory(config, bindings, runtime) -> 业务对象
```

以上展示接口形状，`factory` 需由开发者实现。`requires` 是端口名到契约字符串的映射，`provides` 是单个契约字符串。`semantics` 声明提供者语义，`port_semantics` 声明依赖所需语义；编译器精确匹配契约和所需语义键。

生命周期签名为 `describe()`、`plan(config)`、`prepare(plan)`、`open(prepared, bindings, runtime)`。SDK `Provider` 可实现通用检查，factory 只在 Host 打开图时调用。`runtime` 是调用者提供的字典，使用什么键需要由入口约定；不要假设所有入口都有 endpoint、凭据或 run_dir。

Host 按拓扑创建对象，将显式 bindings 转成对象，并在退出/后续节点失败时逆序调用已返回对象的 `close()`。factory 自己创建一半便抛异常的资源不在 Host 栈里，需要自身清理。

## 契约与兼容性

现有 `metaos.roborsi.*` Protocol 在 graph.py 中。替换模型需实现 `ModelAPI.call / role / review`；替换 Results 需满足调用方要求的 Workspace 行为与结果格式，不只实现 `workspace / event / finish` 三个名字。

依赖旧业务实现时，通过 `implementation_distributions` 声明需要一起锁定的额外发行包；同时在 `pyproject.toml` 声明实际安装依赖。内容锁不会自动推断任意动态 import 的依赖闭包。

新能力可定义自己的版本化契约；当前 Host 只对 `CONTRACT_PROTOCOLS` 内的契约做方法存在性检查，检查也不能证明参数/返回语义正确。为自定义契约补充行为检查或明确注册对应 Protocol。

## 可执行最小示例

[assets/harness-plugin](../assets/harness-plugin/pyproject.toml) 是可复制的 CPU 示例，提供 `counter.example` 与消费它的 `report.example`。它演示两个 provider、显式绑定和 Host 运行，不依赖远程 API、仿真或 RoboRSI 原始包，也不自动安装到活动环境。

下面在仓库根目录使用；`BUILD_PY` 需包含 setuptools、wheel；`HARNESS_PY` 已非 editable 安装 `metaos-harness` 及依赖。`PLUGIN_PY` 仅继承模板环境，仍依赖该模板存在，不是离线发行包。

```bash
SKILL_ROOT="$PWD/.agents/skills/metaos-component-development"
"$BUILD_PY" -c 'import setuptools, wheel'
mkdir -p work
DEV_WORK=$(mktemp -d "$PWD/work/component-skill-XXXXXX")
cp -R "$SKILL_ROOT/assets/harness-plugin" "$DEV_WORK/plugin"
"$HARNESS_PY" -m venv --system-site-packages "$DEV_WORK/env"
PLUGIN_PY="$DEV_WORK/env/bin/python"
"$BUILD_PY" -m pip wheel --no-deps --no-build-isolation --no-index \
  "$DEV_WORK/plugin" --wheel-dir "$DEV_WORK/wheels"
"$PLUGIN_PY" -m pip install --no-index --no-deps \
  "$DEV_WORK/wheels/metaos_example_components-0.1.0-py3-none-any.whl"
"$PLUGIN_PY" -I -B -m metaos_harness.cli components
"$PLUGIN_PY" -I -B -m metaos_harness.cli plan --spec "$DEV_WORK/plugin/example.harness.yaml"
"$PLUGIN_PY" -I -B -m metaos_harness.cli build \
  --spec "$DEV_WORK/plugin/example.harness.yaml" --output "$DEV_WORK/bundle"
"$PLUGIN_PY" -I -B -m metaos_harness.cli verify --bundle "$DEV_WORK/bundle"
"$PLUGIN_PY" -I -B "$DEV_WORK/plugin/smoke.py" --bundle "$DEV_WORK/bundle"
```

按用户职责改名、实现与增加检查后，将可复用包放到 `plugins/`，声明放到 `configs/`。不要用示例计数器的测试替代真实组件的设备/模型行为验证。

Harness CLI 的 `run` 当前绑定 LIBERO experiment schema 和 RoboRSI 结果格式。此示例用 `Host` 运行自定义契约；接入完整原 RoboRSI 图时使用现有角色契约和 [组装流程](../../metaos-harness-assembly/references/workflows.md)。
