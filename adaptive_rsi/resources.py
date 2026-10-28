"""Process-wide capacity and exclusive benchmark access for managed execution."""
import contextlib
import collections
import math
import threading
import time


class ExecutionResources:
    def __init__(self, capacity=4, priorities=None, starvation_seconds=30):
        if type(capacity) is not int or not 1 <= capacity <= 256:
            raise ValueError("Execution capacity must be 1..256")
        if type(starvation_seconds) not in (int, float) or not math.isfinite(starvation_seconds) or not 0 < starvation_seconds <= 3600:
            raise ValueError("Invalid execution starvation interval")
        self.capacity = capacity
        self.priorities = {"model": 10, "command": 20, "benchmark": 30, **(priorities or {})}
        if any(not isinstance(key, str) or type(value) is not int or not -100 <= value <= 100
               for key, value in self.priorities.items()):
            raise ValueError("Execution priorities must be named integers between -100 and 100")
        self.starvation_seconds = starvation_seconds
        self.denied = {"cancelled": 0, "capacity": 0}
        self.condition = threading.Condition(threading.RLock())
        self.readers = {}
        self.writer = None
        self.waiting_writers = 0
        self.queue = []
        self.sequence = 0
        self.wait_samples = collections.deque(maxlen=256)
        self.wait_events = collections.deque(maxlen=64)
        self.admissions = 0

    def _head(self):
        now = time.monotonic()
        # Once aged, FIFO outranks every younger priority class. A reservation
        # already running still has to finish or respond to its own cancellation.
        return min(self.queue, key=lambda ticket: (0, ticket["sequence"]) if
                   now - ticket["created"] >= self.starvation_seconds else
                   (1, self.priorities[ticket["priority"]], ticket["sequence"]))

    @contextlib.contextmanager
    def acquire(self, exclusive=False, cancel=None, priority="command"):
        if priority not in self.priorities:
            raise ValueError("Unknown execution priority")
        identity = threading.get_ident()
        with self.condition:
            nested = identity == self.writer or identity in self.readers
            ticket = None
            outcome = "error"
            try:
                # A nested shared reservation must not silently become an exclusive
                # benchmark while peer readers still execute.
                if exclusive and identity in self.readers and identity != self.writer:
                    raise RuntimeError("Cannot upgrade nested shared execution to exclusive")
                if not nested:
                    self.sequence += 1
                    ticket = {"sequence": self.sequence, "created": time.monotonic(), "priority": priority,
                              "exclusive": bool(exclusive)}
                    self.queue.append(ticket)
                    self.waiting_writers += bool(exclusive)
                counted_wait = False
                while ticket is not None:
                    if cancel and cancel():
                        from .process import Cancelled
                        self.denied["cancelled"] += 1
                        outcome = "cancelled"
                        raise Cancelled("Cancelled while waiting for execution resources")
                    blocked = (self.writer is not None or self._head() is not ticket or
                               (bool(self.readers) if exclusive else len(self.readers) >= self.capacity))
                    if not blocked:
                        break
                    if not counted_wait:
                        self.denied["capacity"] += 1
                        counted_wait = True
                    self.condition.wait(0.1)
                if not nested:
                    if exclusive:
                        self.writer = identity
                    else:
                        self.readers[identity] = True
                    self.admissions += 1
                    outcome = "admitted"
            finally:
                if ticket is not None:
                    waited = max(0, time.monotonic() - ticket["created"])
                    self.wait_samples.append(waited)
                    self.wait_events.append({"sequence": ticket["sequence"], "priority": priority,
                                             "exclusive": bool(exclusive), "wait_seconds": waited,
                                             "outcome": outcome})
                    self.queue.remove(ticket)
                    self.waiting_writers -= bool(exclusive)
                    self.condition.notify_all()
        try:
            yield
        finally:
            if not nested:
                with self.condition:
                    if exclusive:
                        self.writer = None
                    else:
                        self.readers.pop(identity, None)
                    self.condition.notify_all()

    def status(self):
        with self.condition:
            samples = sorted(self.wait_samples)
            now = time.monotonic()
            return {"capacity": self.capacity, "active": len(self.readers),
                    "exclusive": self.writer is not None,
                    "waiting_writers": self.waiting_writers,
                    "denied": dict(self.denied), "admissions": self.admissions,
                    "priorities": dict(self.priorities), "waiting": len(self.queue),
                    "waiting_by_priority": dict(collections.Counter(ticket["priority"] for ticket in self.queue)),
                    "oldest_wait_seconds": max((max(0, now - ticket["created"]) for ticket in self.queue), default=0),
                    "starvation_seconds": self.starvation_seconds,
                    "wait_samples": len(samples), "wait_sample_limit": self.wait_samples.maxlen,
                    "wait_p95_seconds": samples[math.ceil(.95 * len(samples)) - 1] if samples else 0,
                    "wait_quantile": "nearest_rank", "wait_includes_cancelled": True,
                    "recent_wait_events": [dict(event) for event in self.wait_events]}


execution_resources = ExecutionResources()
