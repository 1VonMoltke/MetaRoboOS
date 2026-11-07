"""The canonical 120 short tasks, separate from seeds and execution attempts."""
import random

from jsonschema import Draft202012Validator

API = "metaos.roborsi-panel/v1"
INDEX_ENV = "METAOS_ROBORSI_EPISODE_INDEX"
FAMILIES = ("spatial", "object", "goal")
DIMENSIONS = ("task", "object", "swap", "lan")
# Round-robin over suites gives small prefixes coverage across perturbations.
TASKS = tuple(f"libero_{family}_{dimension}/{index}" for index in range(10)
              for dimension in DIMENSIONS for family in FAMILIES)
SCHEMA = {"type": "object", "additionalProperties": False,
    "required": ["api", "tasks", "seed", "tool_budget", "atomic", "instruction"],
    "properties": {"api": {"const": API}, "tasks": {"type": "array", "minItems": 1,
        "maxItems": 120, "uniqueItems": True, "items": {"enum": list(TASKS)}},
        "seed": {"type": "integer", "minimum": 0},
        "tool_budget": {"type": "integer", "minimum": 1, "maximum": 100},
        "atomic": {"const": "libero_pick_place"}, "instruction": {"type": "string"}}}


def validate(panel):
    Draft202012Validator(SCHEMA).validate(panel)
    return panel


def select(tasks, count, selection="stratified", selection_seed=0):
    if count == "all":
        count = len(tasks)
    if type(count) is not int or not 1 <= count <= len(tasks):
        raise ValueError("count must be between 1 and the locked panel size, or 'all'")
    if selection == "stratified":
        present = set(tasks)
        ordered = [task for task in TASKS if task in present]
    elif selection == "first":
        ordered = list(tasks)
    elif selection == "random":
        ordered = list(tasks)
        random.Random(selection_seed).shuffle(ordered)
    else:
        raise ValueError("Unknown task selection")
    return ordered[:count]


def experiment(panel, index):
    validate(panel)
    if type(index) is not int or not 0 <= index < len(panel["tasks"]):
        raise ValueError("Episode index is outside the locked panel")
    return {key: panel[key] for key in ["atomic", "seed", "tool_budget", "instruction"]} | {
        "sim_task": panel["tasks"][index]}
