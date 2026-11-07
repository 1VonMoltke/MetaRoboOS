"""Sequential, resumable task campaigns with explicit valid/error denominators."""
import fcntl
from pathlib import Path
from typing import Protocol, runtime_checkable

from metaos_harness.graph import digest
from .executor import EpisodeCancelled
from .panel import select, validate
from .storage import append, atomic, journal, read

VALID = {"success", "task_failure"}


@runtime_checkable
class ExecutorAPI(Protocol):
    def identity(self): ...
    def panel(self): ...
    def validate_output(self, output): ...
    def recover(self, index, run): ...
    def execute(self, index, run, *, lock_fd): ...


class Campaign:
    def __init__(self, config, bindings, runtime):
        self.config, self.runtime = config, runtime
        self.executor = bindings["executor"]
        if not isinstance(self.executor, ExecutorAPI):
            raise TypeError("Executor does not implement the campaign episode contract")
        self.panel = validate(self.executor.panel())
        self.tasks = select(self.panel["tasks"], config.get("count", "all"),
            config.get("selection", "stratified"), config.get("selection_seed", 0))
        self.indices = [self.panel["tasks"].index(task) for task in self.tasks]

    def plan(self):
        return {"api": "metaos.roborsi-campaign-plan/v1", "executor": self.executor.identity(),
            "harness_fingerprint": self.runtime.get("fingerprint"), "config": self.config,
            "protocol": "one_seed_per_task_frozen_pass1", "tasks": self.tasks,
            "seed": self.panel["seed"], "episodes": len(self.tasks), "workers": 1}

    def _relative(self, index, attempt):
        slug = self.panel["tasks"][index].replace("/", "-")
        return Path("episodes") / f"{index:03d}-{slug}" / f"attempt-{attempt:02d}" / "run"

    def _states(self, rows):
        states = {index: [] for index in self.indices}
        for row in rows:
            index, attempt = row["index"], row["attempt"]
            if (index not in states or type(attempt) is not int or attempt < 1
                    or row["task"] != self.panel["tasks"][index] or row["seed"] != self.panel["seed"]
                    or row["run"] != str(self._relative(index, attempt))):
                raise ValueError("Journal episode does not belong to this campaign")
            previous = states[index]
            if row["event"] == "started":
                if attempt != len(previous) + 1 or (previous and previous[-1]["result"] is None):
                    raise ValueError("Journal contains duplicate or overlapping attempts")
                previous.append({"start": row, "result": None})
            elif row["event"] == "finished":
                if not previous or attempt != len(previous) or previous[-1]["result"] is not None:
                    raise ValueError("Journal finish has no unique preceding start")
                result = row["result"]
                if result.get("status") not in VALID | {"infrastructure_error", "implementation_error", "verification_error", "interrupted"} or type(result.get("cleanup_confirmed")) is not bool:
                    raise ValueError("Invalid terminal episode result")
                if result["status"] in VALID and not result["cleanup_confirmed"]:
                    raise ValueError("Valid episode cannot have unconfirmed cleanup")
                previous[-1]["result"] = result
            else:
                raise ValueError("Unknown journal event")
        return states

    def _summary(self, root, states, status=None):
        records, groups = [], {}
        counts = {"success": 0, "task_failure": 0, "infrastructure_error": 0,
                  "implementation_error": 0, "verification_error": 0, "interrupted": 0}
        for index in self.indices:
            attempts = states[index]
            for attempt in attempts:
                if attempt["result"]:
                    counts[attempt["result"]["status"]] += 1
            latest = attempts[-1]["result"] if attempts else None
            task = self.panel["tasks"][index]
            result_status = latest["status"] if latest else "pending"
            records.append({"task": task, "seed": self.panel["seed"], "status": result_status,
                "attempts": len(attempts), "run": attempts[-1]["start"]["run"] if attempts else None})
            family, dimension = task.split("/")[0].removeprefix("libero_").split("_", 1)
            for label in ["family:" + family, "perturbation:" + dimension]:
                group = groups.setdefault(label, {"planned": 0, "valid": 0, "success": 0})
                group["planned"] += 1
                group["valid"] += int(result_status in VALID)
                group["success"] += int(result_status == "success")
        valid = sum(r["status"] in VALID for r in records)
        success = sum(r["status"] == "success" for r in records)
        for group in groups.values():
            group["success_rate_valid"] = group["success"] / group["valid"] if group["valid"] else None
        report = {"api": "metaos.roborsi-campaign-summary/v1", "status": status or (
            "completed" if valid == len(self.tasks) else "partial"),
            "planned_tasks": len(self.tasks), "valid_episodes": valid, "success": success,
            "task_failure": valid - success, "unresolved_tasks": len(self.tasks) - valid,
            "success_rate_valid": success / valid if valid else None,
            "success_rate_planned": success / len(self.tasks), "coverage": valid / len(self.tasks),
            "attempt_counts": counts, "groups": groups, "tasks": records,
            "protocol": "one_seed_per_task_frozen_pass1", "workers": 1}
        atomic(root / "summary.json", report)
        return report

    def run(self, *, resume=False):
        root = Path(self.runtime["run_dir"]).absolute()
        self.executor.validate_output(root)
        preflight = getattr(self.executor, "preflight", None)
        if callable(preflight):
            preflight()
        if not resume and root.exists():
            raise FileExistsError("Use --resume for an existing campaign")
        if resume and not root.is_dir():
            raise FileNotFoundError("No campaign to resume")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (root / "campaign.lock").open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Campaign or its episode launcher is still running") from None
            manifest = self.plan()
            manifest["campaign_id"] = digest(manifest)
            path = root / "campaign.json"
            if resume:
                if read(path) != manifest:
                    raise ValueError("Resume configuration, code or episode bundle differs")
            else:
                atomic(path, manifest)
            log_path = root / "episodes.jsonl"
            rows = journal(log_path)
            states = self._states(rows)
            def finish(start, result, recovered=False):
                value = {k: start[k] for k in ["index", "attempt", "task", "seed", "run"]}
                append(log_path, rows, {**value, "event": "finished", "result": result, "recovered": recovered})
                return self._states(rows)
            try:
                for index in self.indices:
                    previous = states[index]
                    # An explicit resume grants a fresh bounded retry budget
                    # only to unfinished, cleanly stopped episodes.
                    limit = (len(previous) if resume else 0) + self.config.get("infra_retries", 1) + 1
                    if previous:
                        last = previous[-1]
                        if last["result"] is None:
                            result = self.executor.recover(index, root / last["start"]["run"])
                            states = finish(last["start"], result, recovered=True)
                        elif last["result"]["status"] in VALID:
                            audited = self.executor.recover(index, root / last["start"]["run"])
                            if (audited["status"] != last["result"]["status"] or
                                    audited.get("evidence_sha256") != last["result"].get("evidence_sha256")):
                                raise ValueError("A completed episode no longer verifies")
                    while True:
                        previous = states[index]
                        latest = previous[-1]["result"] if previous else None
                        if latest and (not latest["cleanup_confirmed"] or latest["status"] in {"verification_error", "implementation_error"}):
                            return self._summary(root, states, "blocked")
                        if latest and latest["status"] in VALID:
                            break
                        if len(previous) >= limit:
                            break
                        attempt = len(previous) + 1
                        start = {"event": "started", "index": index, "attempt": attempt,
                            "task": self.panel["tasks"][index], "seed": self.panel["seed"],
                            "run": str(self._relative(index, attempt))}
                        append(log_path, rows, start)
                        cancelled = False
                        try:
                            result = self.executor.execute(index, root / start["run"], lock_fd=lock.fileno())
                        except EpisodeCancelled as exc:
                            result, cancelled = exc.result, True
                        except KeyboardInterrupt:
                            raise
                        except Exception as exc:
                            result = {"status": "verification_error", "cleanup_confirmed": False,
                                      "reason": type(exc).__name__}
                        states = finish(start, result)
                        summary = self._summary(root, states)
                        print(f"{start['task']}: {result['status']} ({summary['valid_episodes']}/{len(self.tasks)} valid)", flush=True)
                        if cancelled:
                            return self._summary(root, states, "cancelled")
                return self._summary(root, states)
            except KeyboardInterrupt:
                return self._summary(root, self._states(rows), "cancelled")
