from aigw_modules.base import BaseAsyncInterface


class AsyncPangolinClient(BaseAsyncInterface):
    def __init__(self, logger=None, conninfo=None, timeout=60, min_connections=5, max_connections=10):
        self.logger = logger
        self.conninfo = conninfo
        self.pool = None
