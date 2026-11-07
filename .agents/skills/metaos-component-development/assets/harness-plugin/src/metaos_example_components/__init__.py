"""CPU-only example of an installed provider pair with an explicit dependency."""
from metaos_harness import Descriptor, Provider


class Counter:
    def __init__(self, config, bindings, runtime):
        self.value = config.get("initial", 0)
        self.closed = False

    def add(self, amount):
        if self.closed:
            raise RuntimeError("Counter closed")
        if type(amount) is not int:
            raise TypeError("Increment must be an integer")
        self.value += amount
        return self.value

    def close(self):
        self.closed = True


class Report:
    def __init__(self, config, bindings, runtime):
        self.counter = bindings["counter"]
        self.closed = False

    def run(self, experiment):
        if self.closed:
            raise RuntimeError("Report closed")
        return {"value": self.counter.add(experiment["increment"])}

    def close(self):
        self.closed = True


def counter():
    return Provider(Descriptor(
        id="counter.example", version="0.1.0", provides="metaos.example.counter/v1",
        config_schema={"type": "object", "additionalProperties": False,
                       "properties": {"initial": {"type": "integer"}}},
    ), Counter)


def report():
    return Provider(Descriptor(
        id="report.example", version="0.1.0", provides="metaos.example.report/v1",
        requires={"counter": "metaos.example.counter/v1"},
    ), Report)
