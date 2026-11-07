"""Host the original RoboCasa ICL Agent from explicit, pinned local inputs."""

import argparse
import os
from pathlib import Path


def load_config(path):
    from roborsi.agents.config import AgentConfig

    config = AgentConfig.load(str(path))
    if config.factory != "robots.robocasa.model_eval_agent:make_agent":
        raise ValueError("Expected the original RoboCasa Target50 Agent factory")
    if set(config.resources) != {"icl_package"}:
        raise ValueError("Expected the original sealed RoboCasa ICL package")
    llm = config.document.get("runtime", {}).get("llm", {})
    if not llm.get("model") or llm["model"].startswith(("YOUR_", "SET_")) or not llm.get("base_url"):
        raise ValueError("Configure an explicit vision/tool API model and base_url first")
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-config", type=Path, required=True)
    parser.add_argument("--case-dir", type=Path, required=True)
    parser.add_argument("--media-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    config = load_config(args.agent_config)
    args.case_dir.mkdir(parents=True, exist_ok=True)
    args.media_dir.mkdir(parents=True, exist_ok=True)
    os.environ["ROBORSI_MEDIA_DIR"] = str(args.media_dir.resolve())
    from roborsi.evaluation.services import AgentFacade

    agent = config.create_agent(args.case_dir.resolve())
    try:
        AgentFacade(agent).serve(transport="http", host="127.0.0.1", port=args.port, parent_watch=True)
    finally:
        agent.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
