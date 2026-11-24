import threading


class ExecutorSlot:
    """进程内的单执行器槽。同一时刻最多一个 runner。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.process: object | None = None
        self.run_id: str | None = None
        self.project_id: str | None = None

    def busy(self) -> bool:
        process = self.process
        if process is None:
            return False
        poll = getattr(process, "poll", None)
        if not callable(poll):
            return False
        return poll() is None
