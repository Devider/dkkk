from typing import Any

from aigw_modules.base import BaseAsyncInterface


class LangfuseClient(BaseAsyncInterface):
    def __init__(
        self,
        logger: Any = None,
        public_key: Any = None,
        secret_key: Any = None,
        base_url: Any = None,
        debug: bool = False,
        tracing_enabled: bool = True,
        ca_bundle: str = "",
        certs: Any = None,
        **langfuse_kwargs: Any,
    ):
        pass
