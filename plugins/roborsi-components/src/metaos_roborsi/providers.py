"""One independently discoverable provider per replaceable component."""
from metaos_harness.graph import Descriptor, Provider
from . import components as c


def schema(properties=None, required=()):
    return {"type": "object", "additionalProperties": False, "properties": properties or {}, "required": list(required)}


CONTRACTS = {name: "metaos.roborsi." + name + "/v1" for name in [
    "planner", "engineer", "reviewer", "model", "context", "skills", "tools", "results", "episode", "evaluator", "orchestrator"]}


def make(name, provides, requires, factory, config_schema=None, semantics=None, port_semantics=None):
    return Provider(Descriptor(name, "0.1.0", CONTRACTS[provides],
        {port: CONTRACTS[kind] for port, kind in requires.items()}, config_schema or schema(), semantics or {},
        port_semantics or {}, ("metaos-roborsi-baseline", "metaos-harness")), factory)


def model():
    return make("model.roborsi", "model", {}, c.Model, schema({
        "role_sessions": {"enum": ["persistent", "stateless"]},
        "transport": {"enum": ["responses", "chat_completions"]},
        "base_url": {"type": "string", "pattern": "^https?://[^@?#]+$"}}, ["role_sessions", "transport"]))


def skills():
    return make("skills.roborsi_libero", "skills", {}, c.Skills,
                semantics={"namespace": "libero", "frozen": True, "controller": "JOINT_POSITION", "action_dim": 8})


def composed_skills():
    semantics = {"namespace": "libero", "frozen": True, "controller": "JOINT_POSITION", "action_dim": 8}
    return make("skills.compose", "skills", {"base": "skills", "extension": "skills"}, c.ComposedSkills,
                schema({"overrides": {"type": "array", "uniqueItems": True, "items": {"type": "string"}}}),
                semantics, {"base": semantics, "extension": semantics})


def context():
    return make("context.roborsi", "context", {"skills": "skills"}, c.Context,
                semantics={"memory": "original_read_only", "compaction": "original"})


def environment():
    return make("environment.libero_roborsi", "episode", {}, c.Environment,
                schema({"resource_id": {"type": "string", "pattern": "^environments/"}}, ["resource_id"]),
                {"transport": "metaos.system/v1", "controller": "JOINT_POSITION", "action_dim": 8, "native_skills": True})


def toolset():
    return make("toolset.roborsi_libero", "tools", {"skills": "skills", "environment": "episode"}, c.Toolset,
                semantics={"dispatch": "os_gateway_skill_boundary", "schema_identity": "sha256"},
                port_semantics={"skills": {"namespace": "libero", "frozen": True, "controller": "JOINT_POSITION", "action_dim": 8},
                                "environment": {"controller": "JOINT_POSITION", "action_dim": 8, "transport": "metaos.system/v1"}})


ROLE_CONFIG = schema({"model": {"type": "string", "minLength": 1}}, ["model"])


def planner():
    def open(config, bindings, runtime):
        from roborsi.agents.roles.planner import Planner
        return Planner(model=config["model"], services=bindings)
    return make("planner.roborsi", "planner", {"model": "model", "context": "context", "tools": "tools"}, open, ROLE_CONFIG)


def engineer():
    def open(config, bindings, runtime):
        from roborsi.agents.roles.engineer import Engineer
        return Engineer(model=config["model"], services=bindings)
    return make("engineer.roborsi", "engineer", {"model": "model", "context": "context", "tools": "tools"}, open, ROLE_CONFIG)


def reviewer():
    def open(config, bindings, runtime):
        from roborsi.agents.roles.reviewer import Reviewer
        return Reviewer(model=config["model"], allow_evolution=False, services=bindings)
    return make("reviewer.roborsi", "reviewer", {"model": "model", "context": "context"}, open, ROLE_CONFIG)


def evaluator():
    return make("evaluator.libero_roborsi", "evaluator", {"environment": "episode"}, c.Evaluator,
                semantics={"authority": "final_native_predicate", "timing": "after_all_rounds"})


def results():
    return make("results.roborsi", "results", {}, c.Results)


def orchestrator():
    return make("orchestrator.roborsi", "orchestrator", {
        "planner": "planner", "engineer": "engineer", "reviewer": "reviewer", "tools": "tools",
        "environment": "episode", "evaluator": "evaluator", "results": "results"}, c.Orchestrator,
        schema({"max_rounds": {"type": "integer", "minimum": 1, "maximum": 6}}, ["max_rounds"]),
        {"mode": "frozen", "reset_between_rounds": False, "budget": "shared"})
