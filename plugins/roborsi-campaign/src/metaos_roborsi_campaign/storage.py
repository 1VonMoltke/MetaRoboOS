"""Durable campaign state; journal rows form a checked append-only chain."""
import json
import os
from pathlib import Path
import tempfile

from metaos_harness.graph import canonical, digest


def read(path):
    return json.loads(Path(path).read_text())


def atomic(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def journal(path):
    if not path.exists():
        return []
    data, rows, offset = path.read_bytes(), [], 0
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            # A crash may leave only the last append torn. Retain those bytes
            # before trimming; preceding durable starts are recovered normally.
            tail = path.with_name("episodes.partial-" + digest(list(line))[:16] + ".bin")
            if not tail.exists():
                tail.write_bytes(line)
            with path.open("r+b") as stream:
                stream.truncate(offset)
                stream.flush()
                os.fsync(stream.fileno())
            break
        row = json.loads(line)
        checksum = row.pop("sha256")
        if row.get("sequence") != len(rows) or row.get("previous") != (rows[-1]["sha256"] if rows else None) or digest(row) != checksum:
            raise ValueError("Campaign journal integrity mismatch")
        rows.append({**row, "sha256": checksum})
        offset += len(line)
    return rows


def append(path, rows, value):
    row = {**value, "sequence": len(rows), "previous": rows[-1]["sha256"] if rows else None}
    row["sha256"] = digest(row)
    with path.open("a") as stream:
        stream.write(canonical(row) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    rows.append(row)
