class BaseAsyncInterface:
    async def on_startup(self):
        pass

    async def on_shutdown(self):
        pass


class BaseSyncInterface:
    async def on_startup(self):
        pass

    async def on_shutdown(self):
        pass
