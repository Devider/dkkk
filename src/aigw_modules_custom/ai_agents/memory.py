from langgraph.checkpoint.memory import MemorySaver
from langgraph.store.memory import InMemoryStore


class AsyncAgentMemory:
    def __init__(self, logger=None):
        self.logger = logger
        self.store = InMemoryStore()
        self.checkpointer = MemorySaver()

    def set_connection(self, pool):
        pass
