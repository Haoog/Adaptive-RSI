"""Process-wide capacity and exclusive benchmark access for managed execution."""
import contextlib
import threading


class ExecutionResources:
    def __init__(self, capacity=4):
        self.capacity = capacity
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
                while not nested and (self.writer is not None or
                        (bool(self.readers) if exclusive else
                         len(self.readers) >= self.capacity or self.waiting_writers > 0)):
                    if cancel and cancel():
                        from .process import Cancelled
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


execution_resources = ExecutionResources()
