#!/usr/bin/env python3
"""Qualify installed RoboCasa integration with protocol fixtures, never simulation."""

import argparse
import base64
import hashlib
import json
import runpy
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch


CHILD = r'''
import json,sys
from pathlib import Path
from metaos_robocasa_device import RoboCasaDevice,RESET,ACTION,END,FINISH,ABORT
class Device(RoboCasaDevice):
    def launch(self):
        self.endpoint=sys.argv[1]
        self.native_token='fixture-only'
    def remove_container(self): pass
device=Device({'image':'sha256:'+'0'*64,'gpu_device':0},Path(sys.argv[2]))
def execute(cap,**args):
    op=device.start(cap,args)
    assert op.done.wait(10)
    op.tick(0.)
try:
    assert len(device.tasks)==340
    execute(RESET,task_id='OpenDrawer',seed=2,episode_id='fixture-scored')
    assert device.last_reply['payload']['task']['seed']==2
    execute(ACTION,episode_id='fixture-scored',action={'primitive':'move_to'})
    execute(END,episode_id='fixture-scored',reason='policy_stop')
    execute(FINISH,episode_id='fixture-scored')
    result=device.last_reply['payload']
    assert result['score']==0. and result['task']['seed']==2
    records=device.list_artifacts()
    assert any(x['ref']=='native/score.json' for x in records)
    assert any(x['kind']=='image' for x in records)
    for info in records:
        assert device.read_artifact(info['artifact_id'],0,info['size_bytes']) is not None
    execute(RESET,task_id='OpenDrawer',seed=1,episode_id='fixture-cancelled')
    execute(ABORT,episode_id='fixture-cancelled')
    assert device.last_reply['payload']['stopped']
    print(json.dumps({'status':'passed','catalog_cells':340,'verified_artifacts':len(records),'native_task_tested':False}))
finally:
    device.close()
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", type=Path, required=True)
    args = parser.parse_args()
    project = Path(__file__).absolute().parents[1]
    installation = json.loads(args.installation.read_text())
    output = Path(installation["output"])
    os_python = installation["deployments"]["os"]["python"]
    checks = []

    def run(name, command):
        result = subprocess.run([str(x) for x in command], capture_output=True, text=True, timeout=600)
        (output / "logs" / (name + ".log")).write_text(result.stdout + result.stderr)
        if result.returncode:
            raise RuntimeError(name + " failed; inspect logs/" + name + ".log")
        checks.append({"name": name, "status": "passed"})
        return result.stdout

    run("device-tests", [os_python, "-I", "-B", project / "scripts/run_p0_tests.py", "--tests", project / "tests/robocasa/device", "--output", output / "device-tests.json"])
    run("harness-tests", [sys.executable, "-I", "-B", project / "scripts/run_p0_tests.py", "--tests", project / "tests/robocasa/harness", "--output", output / "harness-tests.json"])
    run("harness-help", [sys.executable, "-I", "-B", "-m", "metaos_robocasa_harness", "--help"])
    run("agent-help", [sys.executable, "-I", "-B", "-m", "metaos_robocasa_harness.agent_service", "--help"])
    # Explicit fixture image/model, not represented as an executable experiment.
    prepare = runpy.run_path(str(project / "scripts/prepare_robocasa.py"))["prepare"]
    resources = output / "fixture-resources"
    prepare(args.installation, resources, "sha256:" + "0" * 64, "protocol-fixture-no-model", "http://127.0.0.1:9/v1")
    from metaos_robocasa_harness.agent_service import load_config
    from roborsi.agents.config import AgentConfig

    config = load_config(resources / "context/source/agent.json")
    assert config.document["agent"]["max_turns"] == 100
    assert config.document["agent"]["action_timeout_s"] == 3600
    class Delegate:
        evaluation_initial_context = ()
        def reset(self, task): self.task = task
        def close(self): pass
    delegate = Delegate()
    case = output / "fixture-agent-config"
    case.mkdir()
    with patch.object(AgentConfig, "embodied_agent", return_value=delegate):
        agent = config.create_agent(case)
    from robots.robocasa.benchmark_service import RoboCasa365Benchmark, make_task
    from robots.robocasa.model_eval_agent import load_task_context

    provider = RoboCasa365Benchmark(None)
    catalog = provider.tasks()
    assert len(catalog) == 340 and len({task.task_id for task in catalog}) == 50
    for task in catalog:
        load_task_context(config.resources["icl_package"], task)
    agent.reset(make_task("OpenDrawer", 1))
    assert delegate.task == make_task("OpenDrawer", 1)
    assert delegate.evaluation_initial_context and delegate.planner_timeout_s == 2700
    agent.close()
    checks.append({"name": "original_catalog_config_and_all_340_icl_bindings", "status": "passed", "api_called": False})
    from roborsi.evaluation.benchmark_http import BenchmarkApplication, BenchmarkHttpServer
    from roborsi.evaluation.media import CaseMediaChannel
    from roborsi.evaluation.messages import Observation, StartResult, StepResult, NativeResult

    counts = {"starts": 0, "actions": 0, "stops": 0, "scores": 0, "closes": 0}
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=")
    class Adapter:
        def __init__(self, directory):
            self.directory, self.index = directory, 0
            self.media = CaseMediaChannel(directory / "media")
        def observation(self):
            return Observation(benchmark="robocasa365", episode_id=self.episode, step_index=self.index,
                media=(self.media.write(self.episode, self.index, png, "image/png"),))
        def start(self, task, episode):
            counts["starts"] += 1
            self.task, self.episode = task, episode
            return StartResult(task=task, observation=self.observation())
        def step(self, action):
            counts["actions"] += 1
            self.index += 1
            return StepResult(observation=self.observation(), terminated=False, truncated=False)
        def stop(self, request):
            counts["stops"] += 1
            return StepResult(observation=self.observation(), terminated=False, truncated=True, termination_reason="policy_stop")
        def finish(self):
            counts["scores"] += 1
            path = self.directory / "native/score.json"
            path.parent.mkdir()
            path.write_text('{"fixture_only":true,"score":0.0}')
            return NativeResult(task=self.task, episode_id=self.episode, success=False, score=0.,
                source="native/score.json", source_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        def close(self): counts["closes"] += 1

    provider.create_adapter = lambda directory, artifacts: Adapter(directory)
    with tempfile.TemporaryDirectory(prefix="metaos-robocasa-protocol-") as temporary:
        app = BenchmarkApplication(provider, Path(temporary) / "native")
        http = BenchmarkHttpServer(("127.0.0.1", 0), app, token="fixture-only")
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            protocol = run("shared-http-protocol", [os_python, "-I", "-B", "-c", CHILD,
                "http://127.0.0.1:" + str(http.server_port), Path(temporary) / "device"])
        finally:
            app.close()
            http.shutdown()
            http.server_close()
            thread.join(3)
    assert counts == {"starts": 2, "actions": 1, "stops": 1, "scores": 1, "closes": 2}, counts
    run("composition-plan", [project / "metaos-local", "plan", resources / "robocasa.json", "--resources", resources])
    run("composition-build", [project / "metaos-local", "build", resources / "robocasa.json", "--resources", resources, "--output", output / "fixture-bundle"])
    run("composition-verify", [project / "metaos-local", "verify", output / "fixture-bundle"])
    # No invocation of metaos-local run, Docker or a planner/model endpoint.
    run("libero-existing-bundle-regression", [project / "metaos-local", "verify", project / "work/libero-api-task00-20261105-02/bundle"])
    report = {"status": "protocol_and_composition_passed", "native_task_tested": False, "model_inference": False,
        "docker_started": False, "native_image_qualified": False,
        "installation": str(args.installation.absolute()), "checks": checks,
        "protocol_fixture": json.loads(protocol.strip().splitlines()[-1]), "fixture_counts": counts,
        "device_tests": json.loads((output / "device-tests.json").read_text()), "harness_tests": json.loads((output / "harness-tests.json").read_text()),
        "remaining_acceptance": ["Build/load the pinned native RoboCasa benchmark image", "Real GPU/RLDX reset/action and teardown qualification", "Real API Agent task score/media/trace qualification (intentionally not performed)"]}
    (output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k not in {"device_tests", "harness_tests"}}, indent=2))


if __name__ == "__main__":
    main()
