"""Process-wide capacity and exclusive benchmark access for managed execution."""
import contextlib
import threading


class ExecutionResources:
    def __init__(self, capacity=4, priorities=None):
        self.capacity = capacity
        self.priorities = priorities or {}
        self.denied = {"cancelled": 0, "capacity": 0}
        self.condition = threading.Condition(threading.RLock())
        self.readers = {}
        self.writer = None
        self.waiting_writers = 0

    @contextlib.contextmanager
    def acquire(self, exclusive=False, cancel=None):
        identity = threading.get_ident()
        with self.condition:
            nested = identity == self.writer or identity in self.readers
            if exclusive and not nested:
                self.waiting_writers += 1
            try:
                # A nested shared reservation must not silently become an exclusive
                # benchmark while peer readers still execute.
                if exclusive and identity in self.readers and identity != self.writer:
                    raise RuntimeError("Cannot upgrade nested shared execution to exclusive")
                while not nested and (self.writer is not None or
                        (bool(self.readers) if exclusive else
                         len(self.readers) >= self.capacity or self.waiting_writers > 0)):
                    if cancel and cancel():
                        from .process import Cancelled
                        self.denied["cancelled"] += 1
                        raise Cancelled("Cancelled while waiting for execution resources")
                    self.condition.wait(0.1)
                if not nested:
                    if exclusive:
                        self.writer = identity
                    else:
                        self.readers[identity] = True
            finally:
                if exclusive and not nested:
                    self.waiting_writers -= 1
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
            return {"capacity": self.capacity, "active": len(self.readers),
                    "exclusive": self.writer is not None,
                    "waiting_writers": self.waiting_writers,
                    "denied": dict(self.denied)}


execution_resources = ExecutionResources()
