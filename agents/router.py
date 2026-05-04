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
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return []
    try:
        return list(value)
    except Exception:
        return []


def _as_dict(value) -> dict:
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {}
    if isinstance(value, dict):
        return value
    try:
        return dict(value)
    except Exception:
        return {}


def determine_routing(input_json: str) -> dict:
    """
    Route submission to correct queue and assign priority based on line and parsed data.
    Args:
        input_json: The COMPLETE original input JSON string from the message.
                    Must contain 'line_of_business', 'validation_result', and 'parsed_data'.
    """
    data = _parse_input(input_json)
    line_of_business = data.get("line_of_business", "")
    validation_result = _as_dict(data.get("validation_result", {}))
    parsed_data = _as_list(data.get("parsed_data", []))
    pm = PromptManager()

    # Truncate parsed data for routing decision to keep the prompt focused
    # We only need the structure and key fields, not every single claim detail
    safe_data = []
    for doc in parsed_data:
        extracted = doc.get("extracted_fields", {})
        # Keep only the first 10 items in lists (e.g. first 10 vehicles/claims) to save tokens
        summary_fields = {}
        for k, v in extracted.items():
            if isinstance(v, list):
                summary_fields[k] = v[:10] + ([f"... and {len(v)-10} more"] if len(v) > 10 else [])
            else:
                summary_fields[k] = v
        safe_data.append({
            "document_type": doc.get("document_type"),
            "fields": summary_fields
        })

    prompt = pm.get_prompt("routing_decision", "v1", {
        "line_of_business": line_of_business,
        "completeness_score": validation_result.get("completeness_score", 1.0),
        "missing_fields_json": json.dumps(validation_result.get("missing_fields", [])),
        "parsed_data_json": json.dumps(safe_data, indent=2)
    })

    with tracer.start_as_current_span("determine_routing") as span:
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("input.value", json.dumps({"line_of_business": line_of_business}))
        span.set_attribute("line_of_business", line_of_business)
        span.set_attribute("completeness_score", validation_result.get("completeness_score", 1.0))
        
        api_key = os.environ.get("GOOGLE_API_KEY")
        client = Client(api_key=api_key)
        try:
            response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
            response_text = response.text or ""
            
            # More robust JSON extraction
            import re
            json_match = re.search(r'\{.*\}', response_text.replace('\n', ' '), re.DOTALL)
            if json_match:
                response_text = json_match.group(0)
            
            result = json.loads(response_text)
            
            # --- DETERMINISTIC GUARDRAIL ---
            # If the LLM hallucinated a queue or missed the LOB, we override it 
            # to ensure the submission goes to the correct specialist.
            lob_lower = line_of_business.lower()
            expected_queue = None
            if "auto" in lob_lower: expected_queue = "Auto Queue"
            elif "property" in lob_lower: expected_queue = "Property Queue"
            elif "liability" in lob_lower or "gl" in lob_lower: expected_queue = "GL Queue"
            elif "multi" in lob_lower: expected_queue = "Complex Risk Queue"
            
            # Only override if the LLM returned something suspicious or wrong
            current_queue = result.get("queue", "").lower()
            if expected_queue and (not current_queue or "manual" in current_queue or "hold" not in current_queue):
                # If score is good (>0.7), force the LOB queue
                if validation_result.get("completeness_score", 1.0) >= 0.7:
                    result["queue"] = expected_queue
                    if not result.get("routing_reason"):
                        result["routing_reason"] = f"Automatically routed to {expected_queue} based on classified Line of Business."

            span.set_attribute("assigned_queue", result.get("queue", "unknown"))
            span.set_attribute("priority", result.get("priority", "routine"))
            span.set_attribute("output.value", json.dumps(result, indent=2))
            span.set_status(Status(StatusCode.OK))
            return result

        except Exception as e:
            span.set_attribute("error", True)
            span.set_attribute("error.message", str(e))
            span.set_status(Status(StatusCode.ERROR, str(e)))
            print(f"Routing Fallback Triggered: {e}")
            return {
                "queue": "Hold Queue",
                "routing_reason": f"Routing Logic Error: {str(e)}",
                "priority": "urgent",
                "action_needed": "manual_review"
            }



def generate_intake_summary(input_json: str) -> str:
    """
    Generate intake summary report tailored to the user profile.
    Args:
        input_json: The COMPLETE original input JSON string from the message.
                    Must contain 'submission_id', 'parsed_data', 'validation_result',
                    'routing', and 'user_profile'.
    """
    data = _parse_input(input_json)
    submission_id = data.get("submission_id", "unknown")
    parsed_data = _as_list(data.get("parsed_data", []))
    validation_result = _as_dict(data.get("validation_result", {}))
    routing = _as_dict(data.get("routing", {}))
    user_profile = _as_dict(data.get("user_profile", {}))

    pm = PromptManager()
    prompt = pm.get_prompt("intake_summary", "v1", {
        "submission_id": submission_id,
        "parsed_data_json": json.dumps(parsed_data, indent=2),
        "validation_result_json": json.dumps(validation_result, indent=2),
        "routing_result_json": json.dumps(routing, indent=2),
        "user_profile_json": json.dumps(user_profile)
    })

    with tracer.start_as_current_span("generate_intake_summary") as span:
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("submission_id", submission_id)
        span.set_attribute("user_role", user_profile.get("role", "unknown"))

        api_key = os.environ.get("GOOGLE_API_KEY")
        client = Client(api_key=api_key)
        try:
            response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
            if not response.text:
                return f"Summary generation failed for {submission_id}: model returned empty response."
            result_text = response.text.strip()
            span.set_attribute("output.value", result_text[:500])
            span.set_status(Status(StatusCode.OK))
            return result_text
        except Exception as e:
            span.set_attribute("error", True)
            span.set_attribute("error.message", str(e))
            span.set_status(Status(StatusCode.ERROR, str(e)))
            print(f"Error generating summary: {e}")
            return f"Summary generation failed for {submission_id}."


router = Agent(
    name="router",
    model="gemini-2.5-flash",
    description="Routes submission to correct underwriting queue, generates intake summary.",
    tools=[determine_routing, generate_intake_summary],
    instruction=(
        "You are the Router Agent. You receive a JSON message with a 'task' field.\n"
        "Rules:\n"
        "1. If 'task' is 'route': call determine_routing.\n"
        "2. If 'task' is 'summarize': call generate_intake_summary.\n"
        "CRITICAL: For EITHER tool, pass the COMPLETE original input JSON string "
        "as the single 'input_json' argument. Do NOT extract or modify any fields — "
        "pass the entire message text exactly as received."
    )
)
