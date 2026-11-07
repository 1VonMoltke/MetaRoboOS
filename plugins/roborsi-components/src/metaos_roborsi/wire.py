"""Bounded local wire encoding; no pickle or arbitrary object construction."""
import base64
import json
import socket
import struct

MAX_MESSAGE = 32 * 1024 * 1024


def encode(value):
    import numpy as np
    if isinstance(value, np.ndarray):
        if value.dtype.hasobject or value.nbytes > MAX_MESSAGE // 2:
            raise ValueError("Unsupported array")
        return {"__ndarray__": base64.b64encode(value.tobytes()).decode(), "dtype": value.dtype.str, "shape": list(value.shape)}
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [encode(v) for v in value]
    return value


def decode(value):
    import numpy as np
    if isinstance(value, dict):
        if "__ndarray__" in value:
            dtype = np.dtype(value["dtype"])
            if dtype.hasobject:
                raise ValueError("Object arrays forbidden")
            return np.frombuffer(base64.b64decode(value["__ndarray__"], validate=True), dtype=dtype).reshape(value["shape"]).copy()
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value


def observation(obs):
    return encode({"images": obs.images, "state": obs.state, "timestamp": obs.timestamp, "extras": obs.extras})


def restore_obs(value):
    from roborsi.embodied.agent_loop.env import Observation
    return Observation(**decode(value))


def receive(sock):
    def exact(count):
        chunks = bytearray()
        while len(chunks) < count:
            chunk = sock.recv(count - len(chunks))
            if not chunk:
                raise ConnectionError("Native process disconnected")
            chunks.extend(chunk)
        return chunks
    size = struct.unpack("!I", exact(4))[0]
    if size > MAX_MESSAGE:
        raise ValueError("Native message exceeds limit")
    return json.loads(exact(size))


def send(sock, value):
    data = json.dumps(value, allow_nan=False).encode()
    if len(data) > MAX_MESSAGE:
        raise ValueError("Native message exceeds limit")
    sock.sendall(struct.pack("!I", len(data)) + data)


def request(path, body, timeout=650):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        send(sock, body)
        reply = receive(sock)
    if "error" in reply:
        raise RuntimeError(reply["error"])
    return reply
