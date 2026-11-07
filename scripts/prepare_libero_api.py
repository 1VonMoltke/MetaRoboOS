#!/usr/bin/env python3
"""Prepare a local original RoboRSI/LIBERO experiment using a model API."""

import argparse
import json
import re
import runpy
import sys
from pathlib import Path
from urllib.parse import urlsplit


def prepare(resources, evidence, model, base_url, key_env="OPENAI_API_KEY", gpu=None,
            context_window=128000, max_output_tokens=8192, case_timeout=7200):
    url = urlsplit(base_url)
    if not model.strip() or model.startswith("SET_") or url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Supply the actual model ID and a credential-free HTTP(S) API base URL")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
        raise ValueError("Invalid API key environment variable name")
    if max_output_tokens <= 0 or context_window <= max(max_output_tokens, 2048) + 8192:
        raise ValueError("Context window must exceed output/summary reserve plus 8192 overhead tokens")
    if gpu is not None and gpu < 0 or not 0 < case_timeout <= 86400:
        raise ValueError("Invalid GPU or case timeout")
    project = Path(__file__).absolute().parents[1]
    local = runpy.run_path(str(project / "scripts/metaos_local.py"))
    local["init_pool"](resources, evidence)
    read, write = local["read"], local["write"]
    model_path = resources / "model-api/resource.json"
    descriptor = read(model_path)
    descriptor["outputs"] = {"model_name": model, "endpoint": base_url.rstrip("/")}
    write(model_path, descriptor)
    system_path = resources / "libero/resource.json"
    system = read(system_path)
    spec = read(system["paths"]["spec"]["path"])
    if gpu is not None:
        for node in spec["components"].values():
            if node["component"] == "environment.libero_device":
                node["config"]["gpu_device"] = gpu
    write(resources / "libero/system.json", spec)
    system["paths"]["spec"]["path"] = "system.json"
    write(system_path, system)
    app_path = resources / "libero-harness/resource.json"
    app = read(app_path)
    app["paths"]["launcher"]["path"] = str(project / "scripts/run_libero_api.py")
    app["env"] = [key_env]
    settings = [
        'runtime.llm.openai_format="chat"',
        "runtime.llm.extra_body=null",
        'agent.reasoning_effort="none"',
        "runtime.llm.parallel_tool_calls=false",
        "runtime.llm.retry.max_retries=2",
        "agent.max_tokens=" + str(max_output_tokens),
        "agent.action_timeout_s=300",
        # ApiAgentLoop applies this to the whole episode, not one API request.
        # Keep the case deadline authoritative instead of cancelling a slower
        # API policy after 1800 seconds inside an otherwise longer case.
        "agent.planner_timeout_s=" + str(case_timeout),
        "runtime.context.context_window=" + str(context_window),
        "runtime.context.output_reserve=" + str(max_output_tokens),
        "runtime.context.overhead_reserve=8192",
        "runtime.context.summary_max_tokens=2048",
    ]
    app["command"] += ["--api-key-env", key_env, "--case-timeout", str(case_timeout)]
    for item in settings:
        app["command"].append("--agent-set=" + item)
    write(app_path, app)
    report = {"status": "prepared", "scope": "single_original_libero_task_api_planner",
        "resources": str(resources.absolute()), "spec": str((resources / "libero.json").absolute()),
        "model_name": model, "base_url": base_url.rstrip("/"), "api_key_env": key_env,
        "context_window": context_window, "max_output_tokens": max_output_tokens,
        "case_timeout": case_timeout, "model_contacted": False,
        "limitations": ["OpenAI Chat Completions compatible API with image and tool-call support required",
            "Original local tokenizer is retained for approximate context budgeting; provider token accounting may differ",
            "This is one original task, not a full benchmark result"]}
    write(resources / "api-experiment.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--gpu", type=int)
    parser.add_argument("--context-window", type=int, default=128000)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--case-timeout", type=int, default=7200)
    parser.add_argument("--evidence", type=Path, default=Path(__file__).absolute().parents[1] / "evidence/p1p2-latest.json")
    args = parser.parse_args()
    try:
        result = prepare(args.resources, args.evidence, args.model, args.base_url, args.api_key_env,
            args.gpu, args.context_window, args.max_output_tokens, args.case_timeout)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
