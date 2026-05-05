import json
import os
import time
from google.adk import Agent
from google.genai import Client
from prompts.prompt_manager import PromptManager
from telemetry.arize_setup import get_tracer
from opentelemetry.trace import Status, StatusCode

tracer = get_tracer()

def load_document(file_path: str) -> str:
    """Load a mock document (JSON/text) from disk."""
    if not os.path.exists(file_path):
        return ""
    with open(file_path, "r") as f:
        return f.read()

def extract_fields(file_path: str, document_type: str) -> dict:
    """
    Extract structured fields from document content.
    Uses zero-shot extraction prompt.
    """
    pm = PromptManager()
    
    with tracer.start_as_current_span("extract_document_fields") as span:
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("input.value", f"Extracting {document_type} from {file_path}")
        span.set_attribute("document_type", document_type)
        span.set_attribute("is_pdf", file_path.endswith('.pdf'))
        span.set_attribute("prompt_version", "v1")
        
        api_key = os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            print("Warning: No GOOGLE_API_KEY found")

        client = Client(api_key=api_key)
        contents = []
        if file_path.endswith('.pdf'):
            document_file = client.files.upload(file=file_path)
            
            # Wait for Gemini to index the PDF so it's readable
            while document_file.state.name == "PROCESSING":
                print(f"--> [WAIT] Processing PDF: {file_path}...")
                time.sleep(2)
                document_file = client.files.get(name=document_file.name)
                
            prompt = pm.get_prompt("document_extraction", "v1", {
                "document_type": document_type,
                "document_content": "See attached PDF document."
            })
            contents = [document_file, prompt]
        else:
            document_content = load_document(file_path)
            prompt = pm.get_prompt("document_extraction", "v1", {
                "document_type": document_type,
                "document_content": document_content
            })
            contents = [prompt]

        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=contents,
            )
            response_text = response.text
            if response_text.startswith("```json"):
                response_text = response_text[7:-3]
            elif response_text.startswith("```"):
                response_text = response_text[3:-3]
                
            result = json.loads(response_text)
            span.set_attribute("confidence", result.get("confidence", 0.0))
            span.set_attribute("output.value", json.dumps(result, indent=2))
            span.set_status(Status(StatusCode.OK))
            return result
        except Exception as e:
            span.set_attribute("error", True)
            span.set_attribute("error.message", str(e))
            span.set_status(Status(StatusCode.ERROR, str(e)))
            print(f"Error during extraction: {e}")
            return {
                "document_type": document_type,
                "extracted_fields": {},
                "confidence": 0.0,
                "notes": f"Extraction failed: {str(e)}"
            }

# ── ADK Agent Definition (Spec Section 7.2) ─────────────────────────────────
# Defines this file as a formal ADK Agent with registered tools.
# Used by the Orchestrator as a sub_agent (MVP pattern).
# Also accessible via A2A HTTP through a2a/server.py (Stretch pattern).
document_parser = Agent(
    name="document_parser",
    model="gemini-2.5-flash",
    description="Extracts structured data from mock submission documents (text/JSON). "
                "Runs in parallel for multi-doc submissions.",
    tools=[load_document, extract_fields],
    instruction="Extract structured insurance data from documents. "
                "Return JSON with document_type, extracted_fields, confidence, and notes."
)
