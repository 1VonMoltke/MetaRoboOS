#!/usr/bin/env python3
"""Verify installed original Agent + sealed ICL through reset, without inference."""

import argparse
import hashlib
import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path

from roborsi.contracts import ComponentContext, ComponentSpec
from roborsi.evaluation.messages import Task
from metaos_runtime.composition import Registry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "media").mkdir()
    os.environ["ROBORSI_MEDIA_DIR"] = str(output / "media")
    import importlib.metadata as metadata
    agent_dist = metadata.distribution("roborsi-agent-27b")
    mapping = Path(agent_dist.locate_file("roborsi/integrations/presets/context_maps/libero_pro_init0.json"))
    context_spec = ComponentSpec(component="context.sealed_icl", config={"map_path": str(mapping), "package_root": str(args.packages.resolve())})
    agent_spec = ComponentSpec(component="agent.roborsi_27b", config={"preset": "sensenova_roborsi_27b_no_tools",
        "tokenizer_file": str(args.tokenizer.resolve())})
    registry = Registry()
    context = ComponentContext(output / "context", root=output)
    provider = registry.provider(context_spec.component)
    plan = provider.plan(context_spec, context)
    report = {"status": "running", "scope": "original_27b_reset_only", "model_inference": False,
        "context_inputs": [row.model_dump(mode="json") for row in plan.inputs], "python": sys.executable}
    try:
        with ExitStack() as stack:
            handle = stack.enter_context(provider.open(provider.prepare(plan, context), context))
            agent_context = ComponentContext(output / "agent", root=output, bindings={"context": handle})
            agent_context.ensure_workspace()
            agent_provider = registry.provider(agent_spec.component)
            agent_plan = agent_provider.plan(agent_spec, agent_context)
            report["agent_plan"] = agent_plan.model_dump(mode="json")
            agent = stack.enter_context(agent_provider.open(agent_provider.prepare(agent_plan, agent_context), agent_context))
            task = Task.from_dict(json.loads(args.task.read_text()))
            agent.value.reset(task)
            context_record = json.loads((output / "agent/context-package.json").read_text())
            selected = handle.value.select(task.task_id)
            assert context_record["manifest_sha256"] == selected.manifest_sha256
            report.update(status="passed", task_id=task.task_id, selected_context=context_record,
                tokenizer_sha256=hashlib.sha256(args.tokenizer.read_bytes()).hexdigest())
            plan.verify_inputs()
            agent_plan.verify_inputs()
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"status": report["status"], "model_inference": False, "report": str(output / "report.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
