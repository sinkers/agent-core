"""OpenTelemetry tracing. Exports only when OTEL_EXPORTER_OTLP_ENDPOINT is set."""

import logging
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def setup(agent: str) -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format='{"ts":"%(asctime)s","level":"%(levelname)s","agent":"' + agent + '","logger":"%(name)s","msg":"%(message)s"}',
    )
    # The kill switch polls every 5 s; per-request client logs would drown everything else.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    provider = TracerProvider(resource=Resource.create({"service.name": f"agent-{agent}"}))
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
