from abc import ABC, abstractmethod

# Ленивый импорт aef_tracing — модуль может отсутствовать при локальной разработке
try:
    from aef_tracing import AEFBatchSpanProcessor, AEFHandler, AEFTracerProvider
    from aef_tracing.exporters import (
        AEFConsoleSender,
        AEFKafkaSender,
        AEFProtobufSenderExporter,
    )
    _AEF_AVAILABLE = True
except ImportError:
    AEFHandler = None  # type: ignore
    AEFTracerProvider = None  # type: ignore
    AEFBatchSpanProcessor = None  # type: ignore
    AEFConsoleSender = None  # type: ignore
    AEFKafkaSender = None  # type: ignore
    AEFProtobufSenderExporter = None  # type: ignore
    _AEF_AVAILABLE = False

from aigw_modules.hub_services.langfuse import LangfuseClient

from aigw_service.config import Secrets
from aigw_service.logger import ContextVarsContainer

if _AEF_AVAILABLE:
    class AEFTracingHandler(AEFHandler):
        def on_startup(self):
            pass
else:
    # Fallback class when aef_tracing is not available
    class AEFTracingHandler:  # type: ignore[no-redef]
        def on_startup(self):
            pass
        def on_shutdown(self):
            pass


class Tracing(ABC):
    @abstractmethod
    def get_tracing(self, secrets: Secrets, logger):
        pass



class AEFTracing(Tracing):
    def get_tracing(self, secrets: Secrets, logger: object) -> object | None:
        if not _AEF_AVAILABLE:
            return None
        if secrets.aef_tracing.enabled:
            tracing = self._init_aef_tracing(secrets.aef_tracing, logger)
            return tracing
        return None

    @staticmethod
    def _init_aef_tracing(env_vars, logger):
        provider = AEFTracerProvider()
        logger.debug(
            f"AEF Tracing Headers: 'cluster-id': '{env_vars.cluster_id}', 'agent-id': '{env_vars.agent_id}', 'namespace': '{env_vars.namespace}', 'distributive': '{env_vars.distributive}'"
        )
        if env_vars.tracing_sender_type == "console":
            sender = AEFConsoleSender()
        elif env_vars.tracing_sender_type == "kafka":
            sender = AEFKafkaSender(
                outbox_topic=env_vars.kafka_topic,
                kafka_producer_config={
                    "bootstrap_servers": env_vars.kafka_hosts,
                    "security_protocol": "PLAINTEXT",
                },
                headers={
                    "cluster-id": env_vars.cluster_id,
                    "agent-id": env_vars.agent_id,
                    "namespace": env_vars.namespace,
                    "distributive": env_vars.distributive,
                },
                metadata_headers=ContextVarsContainer().context_vars.__dict__,
            )
        else:
            raise ValueError("Unknown tracing sender type. Set correct value in .env")

        proto_exporter = AEFProtobufSenderExporter(senders=[sender])
        proto_processor = AEFBatchSpanProcessor(proto_exporter)
        provider.add_span_processor(proto_processor)
        handler = AEFTracingHandler(tracer=provider.get_tracer(__name__))
        logger.debug("AEF Tracing Callback Handler initialized.")
        return handler


class LangFuse(Tracing):
    def get_tracing(self, secrets: Secrets, logger) -> LangfuseClient | None:
        tracing: LangfuseClient | None = None
        if secrets.langfuse.enabled:
            tracing = LangfuseClient(logger=logger, **secrets.langfuse.base_params)
            logger.debug("LangFuse tracing has been initialized.")
        return tracing


class TracingManager:
    def __init__(self, logger, secrets: Secrets):
        self.logger = logger
        self.secrets = secrets

    def get_tracing(self) -> AEFTracingHandler | LangfuseClient:
        if self.secrets.aef_tracing.enabled:
            tracing = AEFTracing().get_tracing(self.secrets, self.logger)
        else:
            tracing = LangFuse().get_tracing(self.secrets, self.logger)
        return tracing
