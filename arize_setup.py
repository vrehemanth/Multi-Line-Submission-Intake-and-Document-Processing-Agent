import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.trace import Status, StatusCode
# If installed, this automatically traces Gemini calls
try:
    from openinference.instrumentation.gemini import GeminiInstrumentor
    HAS_INSTRUMENTOR = True
except ImportError:
    HAS_INSTRUMENTOR = False

class ArizeProjectInjector(SpanProcessor):
    """Automatically injects Arize project attributes into every span."""
    def on_start(self, span, parent_context=None):
        if span.is_recording():
            span.set_attribute("arize.project.name", "insurance-intake-pipeline")
            span.set_attribute("model_id", "insurance-intake-pipeline")
            span.set_attribute("queue", "annotation_review")
            
            # Inject fallbacks for Google ADK internal spans so the Arize UI is never empty
            span.set_attribute("openinference.span.kind", "CHAIN")
            span.set_attribute("input.value", "Internal ADK framework execution.")
            span.set_attribute("output.value", "Completed.")

def setup_arize():
    """Set up OpenTelemetry tracing and export to Arize AI."""
    space_key = os.environ.get("ARIZE_SPACE_KEY")
    api_key = os.environ.get("ARIZE_API_KEY")
    
    if not space_key or not api_key:
        print("Warning: Arize credentials missing in .env. Tracing will run locally only.")
        return

    # ADK already initializes a TracerProvider! We cannot override it.
    # Instead, we just get the existing one.
    tracer_provider = trace.get_tracer_provider()
    
    # If we are running a raw script (like demo_day7.py) and no provider has been set yet,
    # trace.get_tracer_provider() returns a ProxyTracerProvider which lacks add_span_processor.
    if not hasattr(tracer_provider, "add_span_processor"):
        tracer_provider = TracerProvider()
        trace.set_tracer_provider(tracer_provider)
    
    endpoint = "https://otlp.arize.com/v1"
    headers = {
        "space_id": space_key,
        "Authorization": f"Bearer {api_key}"
    }
    exporter = OTLPSpanExporter(endpoint=f"{endpoint}/traces", headers=headers)
    
    # Avoid adding processors twice if setup_arize is called multiple times
    has_arize = any(isinstance(p, ArizeProjectInjector) for p in getattr(tracer_provider, "_span_processors", []))
    if not has_arize:
        # Add our attribute injector FIRST, then the Arize exporter
        tracer_provider.add_span_processor(ArizeProjectInjector())
        tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
    
    if HAS_INSTRUMENTOR:
        GeminiInstrumentor().instrument()
        print("Arize OTel tracing and Gemini instrumentation attached to ADK successfully.")
    else:
        print("Arize OTel attached to ADK, but openinference-instrumentation-gemini not found.")

def get_tracer():
    """Get the tracer for custom spans."""
    return trace.get_tracer("insurance-intake-pipeline")
