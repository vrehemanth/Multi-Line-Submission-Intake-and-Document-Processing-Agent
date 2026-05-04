import json
import os
from google.adk import Agent
from google.genai import Client
from prompts.prompt_manager import PromptManager
from arize_setup import get_tracer
from opentelemetry.trace import Status, StatusCode

tracer = get_tracer()


def _parse_input(input_json: str) -> dict:
    """Parse the full input JSON string into a dict."""
    if isinstance(input_json, str):
        try:
            return json.loads(input_json)
        except Exception:
            return {}
    if isinstance(input_json, dict):
        return input_json
    return {}


def _as_list(value) -> list:
    """Accept either a list or a JSON string and return a list."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return []
    if value is None:
        return []
    try:
        return list(value)
    except Exception:
        return []


def validate_completeness(input_json: str) -> dict:
    """
    Check if all required fields are present for the given line of business.
    Args:
        input_json: The COMPLETE original input JSON string from the message.
                    Must contain 'parsed_data' (list) and 'line_of_business' (str).
    """
    data = _parse_input(input_json)
    parsed_data = _as_list(data.get("parsed_data", []))
    line_of_business = data.get("line_of_business", "")

    REQUIRED_FIELDS = {
        "commercial_auto": ["vehicle_count", "vehicles", "radius_of_operation", "total_incurred"],
        "commercial_property": ["properties", "building_value", "construction_type"],
        "general_liability": ["annual_revenue", "primary_operations"],
        "multi_line": ["vehicle_count", "properties", "annual_revenue"]
    }

    req_fields = REQUIRED_FIELDS.get(line_of_business, [])

    pm = PromptManager()
    prompt = pm.get_prompt("completeness_validation", "v1", {
        "line_of_business": line_of_business,
        "parsed_data_json": json.dumps(parsed_data, indent=2),
        "required_fields_json": json.dumps(req_fields)
    })

    with tracer.start_as_current_span("validate_completeness") as span:
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("input.value", json.dumps({"line_of_business": line_of_business}))
        span.set_attribute("line_of_business", line_of_business)
        span.set_attribute("document_count", len(parsed_data))

        api_key = os.environ.get("GOOGLE_API_KEY")
        client = Client(api_key=api_key)
        try:
            response = client.models.generate_content(model='gemini-2.5-pro', contents=prompt)
            response_text = response.text or ""
            response_text = response_text.strip()
            if response_text.startswith("```json"):
                response_text = response_text[7:-3].strip()
            elif response_text.startswith("```"):
                response_text = response_text[3:-3].strip()
            if not response_text:
                raise ValueError("Model returned empty response for validate_completeness")
            result = json.loads(response_text)
            span.set_attribute("completeness_score", result.get("completeness_score", 0.0))
            span.set_attribute("output.value", json.dumps(result, indent=2))
            span.set_status(Status(StatusCode.OK))
            return result
        except Exception as e:
            span.set_attribute("error", True)
            span.set_attribute("error.message", str(e))
            span.set_status(Status(StatusCode.ERROR, str(e)))
            print(f"Error during validation: {e}")
            return {
                "completeness_score": 0.0,
                "field_status": {},
                "missing_fields": req_fields,
                "validation_notes": f"Validation failed: {str(e)}"
            }


def classify_line_of_business(input_json: str) -> dict:
    """
    Determine which line of business this submission belongs to.
    Args:
        input_json: The COMPLETE original input JSON string from the message.
                    Must contain 'parsed_data' (list of parsed document dicts).
    """
    data = _parse_input(input_json)
    parsed_data = _as_list(data.get("parsed_data", []))

    pm = PromptManager()
    prompt = pm.get_prompt("lob_classification", "v1", {
        "parsed_data_json": json.dumps(parsed_data, indent=2)
    })

    with tracer.start_as_current_span("classify_line_of_business") as span:
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("document_count", len(parsed_data))

        api_key = os.environ.get("GOOGLE_API_KEY")
        client = Client(api_key=api_key)
        try:
            response = client.models.generate_content(model='gemini-2.5-pro', contents=prompt)
            response_text = response.text or ""
            response_text = response_text.strip()
            if response_text.startswith("```json"):
                response_text = response_text[7:-3].strip()
            elif response_text.startswith("```"):
                response_text = response_text[3:-3].strip()
            if not response_text:
                raise ValueError("Model returned empty response for classify_line_of_business")
            result = json.loads(response_text)
            span.set_attribute("primary_line", result.get("primary_line", "unknown"))
            span.set_attribute("confidence", result.get("confidence", 0.0))
            span.set_attribute("output.value", json.dumps(result, indent=2))
            span.set_status(Status(StatusCode.OK))
            return result
        except Exception as e:
            span.set_attribute("error", True)
            span.set_attribute("error.message", str(e))
            span.set_status(Status(StatusCode.ERROR, str(e)))
            print(f"Error during LOB classification: {e}")
            return {
                "primary_line": "unknown",
                "secondary_lines": [],
                "confidence": 0.0,
                "reasoning": f"Error: {str(e)}"
            }


# ── ADK Agent Definition ─────────────────────────────────────────────────────
validator = Agent(
    name="validator",
    model="gemini-2.5-pro",
    description="Validates extracted data completeness, classifies line of business.",
    tools=[validate_completeness, classify_line_of_business],
    instruction=(
        "You are the Validator Agent. You receive a JSON message with a 'task' field.\n"
        "Rules:\n"
        "1. If 'task' is 'classify': call classify_line_of_business.\n"
        "2. If 'task' is 'validate': call validate_completeness.\n"
        "CRITICAL: For EITHER tool, pass the COMPLETE original input JSON string "
        "as the single 'input_json' argument. Do NOT extract or modify any fields — "
        "pass the entire message text exactly as received."
    )
)
