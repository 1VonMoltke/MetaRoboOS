"""Host the unchanged installed 27B policy and a verified sealed ICL binding."""

import argparse
import importlib.metadata
import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

from metaos_runtime.composition import Registry
from roborsi.contracts import ComponentContext, ComponentSpec
from roborsi.evaluation.services import AgentFacade


def model_overrides(model, base_url, settings=()):
    """Select the actual service explicitly; credentials stay in its environment."""
    parsed = urlsplit(base_url)
    if not model.strip() or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("An explicit model and credential-free HTTP(S) model origin are required")
    for item in settings:
        key, separator, value = item.partition("=")
        if not separator or any(part in {"api_key", "authorization", "password", "secret", "access_token"} for part in key.lower().split(".")):
            raise ValueError("Agent settings must use key=JSON; provide credentials through OPENAI_API_KEY")
        document = json.loads(value)
        def check(value):
            if isinstance(value, dict):
                for name, child in value.items():
                    if name.lower() in {"api_key", "authorization", "password", "secret", "access_token"}:
                        raise ValueError("Provide credentials through OPENAI_API_KEY")
                    check(child)
            elif isinstance(value, list):
                for child in value:
                    check(child)
        check(document)
    return [*settings, 'runtime.llm.provider="openai"', "runtime.llm.model=" + json.dumps(model),
        "runtime.llm.base_url=" + json.dumps(base_url)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--case-dir", type=Path, required=True)
    parser.add_argument("--media-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-base-url", required=True)
    parser.add_argument("--set", action="append", default=[])
    args = parser.parse_args(argv)
    overrides = model_overrides(args.model, args.model_base_url, args.set)
    args.case_dir.mkdir(parents=True, exist_ok=True)
    args.media_dir.mkdir(parents=True, exist_ok=True)
    os.environ["ROBORSI_MEDIA_DIR"] = str(args.media_dir.resolve())
    distribution = importlib.metadata.distribution("roborsi-agent-27b")
    mapping = Path(distribution.locate_file("roborsi/integrations/presets/context_maps/libero_pro_init0.json"))
    registry = Registry()
    context_spec = ComponentSpec(component="context.sealed_icl", config={"map_path": str(mapping), "package_root": str(args.packages.resolve())})
    context = ComponentContext(args.case_dir / "context", root=args.case_dir.resolve())
    context_provider = registry.provider(context_spec.component)
    context_plan = context_provider.plan(context_spec, context)
    with ExitStack() as stack:
        binding = stack.enter_context(context_provider.open(context_provider.prepare(context_plan, context), context))
        agent_spec = ComponentSpec(component="agent.roborsi_27b", config={"preset": "sensenova_roborsi_27b_no_tools",
            "tokenizer_file": str(args.tokenizer.resolve()), "overrides": overrides})
        agent_context = ComponentContext(args.case_dir.resolve(), root=args.case_dir.resolve(), bindings={"context": binding})
        provider = registry.provider(agent_spec.component)
        plan = provider.plan(agent_spec, agent_context)
        (args.case_dir / "agent-plan.json").write_text(json.dumps(plan.model_dump(mode="json"), indent=2))
        agent = stack.enter_context(provider.open(provider.prepare(plan, agent_context), agent_context))
        (args.case_dir / "agent-runtime.json").write_text(json.dumps({"python_executable": sys.executable,
            "prefix": sys.prefix, "base_prefix": sys.base_prefix,
            "harness_version": importlib.metadata.version("metaos-libero-harness"),
            "agent_version": distribution.version}, indent=2))
        AgentFacade(agent.value).serve(transport="http", host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
