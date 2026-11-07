"""Four-stage campaign providers discovered by the existing Harness SDK."""
from pathlib import Path

from metaos_harness.graph import Descriptor, Provider
from .campaign import Campaign
from .executor import LocalExecutor, input_identity

SEMANTICS = {"run_mode": "frozen", "episode_isolation": "process", "panel": "libero_pro_short"}


class ExecutorProvider(Provider):
    def plan(self, config):
        normalized = dict(config)
        for key in ["bundle", "composer", "composer_script", "verifier"]:
            if key in normalized:
                normalized[key] = str(Path(normalized[key]).absolute())
        plan = super().plan(normalized)
        plan["inputs"] = input_identity(normalized)
        return plan


def executor():
    return ExecutorProvider(Descriptor("executor.roborsi_local", "0.1.0", "metaos.roborsi.episode_executor/v1",
        config_schema={"type": "object", "additionalProperties": False,
            "required": ["bundle", "composer", "composer_script", "verifier"], "properties": {
                **{k: {"type": "string", "minLength": 1} for k in ["bundle", "composer", "composer_script", "verifier"]},
                "timeout_s": {"type": "integer", "minimum": 1, "maximum": 86400}}},
        semantics=SEMANTICS, implementation_distributions=("metaos-harness",)), LocalExecutor)


def campaign():
    return Provider(Descriptor("campaign.roborsi_libero", "0.1.0", "metaos.roborsi.campaign/v1",
        requires={"executor": "metaos.roborsi.episode_executor/v1"},
        config_schema={"type": "object", "additionalProperties": False, "properties": {
            "count": {"oneOf": [{"type": "integer", "minimum": 1, "maximum": 120}, {"const": "all"}]},
            "selection": {"enum": ["stratified", "first", "random"]},
            "selection_seed": {"type": "integer", "minimum": 0},
            "infra_retries": {"type": "integer", "minimum": 0, "maximum": 5}}},
        port_semantics={"executor": SEMANTICS}, implementation_distributions=("metaos-harness",)), Campaign)
