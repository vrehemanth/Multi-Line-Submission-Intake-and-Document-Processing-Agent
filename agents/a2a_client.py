import httpx
import json
import uuid
from opentelemetry import propagate, trace
from opentelemetry.trace import Status, StatusCode

# Unified Agent Registry (Port mappings)
AGENT_REGISTRY = {
    "document_parser": "http://localhost:8011",
    "validator": "http://localhost:8012",
    "router": "http://localhost:8013"
}

class A2AClient:
    """
    Task-Based A2A Client.
    Compatible with the original Orchestrator workflow but using the /v1/tasks protocol.
    """
    def __init__(self, timeout: float = 300.0):
        self.tracer = trace.get_tracer("a2a-client")
        self.client = httpx.Client(timeout=timeout)

    def send_task(self, agent_name: str, payload_data: dict) -> dict:
        """
        Sends a task to the single /v1/tasks endpoint of a sub-agent.
        """
        base_url = AGENT_REGISTRY.get(agent_name)
        if not base_url:
            raise ValueError(f"Unknown agent: '{agent_name}'")

        full_url = f"{base_url}/v1/tasks"
        
        # Wrap data in the Message/Parts envelope from the sample code
        envelope = {
            "message": {
                "parts": [{"text": json.dumps(payload_data)}]
            }
        }

        with self.tracer.start_as_current_span(f"A2A:{agent_name}:task") as span:
            headers = {"Content-Type": "application/json"}
            propagate.inject(headers) 

            try:
                response = self.client.post(full_url, json=envelope, headers=headers)
                response.raise_for_status()
                
                # Unwrap the Artifact from the sample code response format
                res_json = response.json()
                raw_text = res_json["artifacts"][0]["parts"][0]["text"]
                
                try:
                    clean_text = raw_text.strip()
                    if clean_text.startswith("```json"):
                        clean_text = clean_text[7:-3].strip()
                    elif clean_text.startswith("```"):
                        clean_text = clean_text[3:-3].strip()
                    return json.loads(clean_text)
                except:
                    return {"raw_output": raw_text}
            except Exception as e:
                print(f"!! [A2A ERROR] {agent_name} failed: {e}")
                raise ConnectionError(str(e))

    # ── MATCHING YOUR ORCHESTRATOR'S METHOD NAMES ────────────────────────

    def parse_document(self, file_path: str, document_type: str) -> dict:
        return self.send_task("document_parser", {
            "file_path": file_path,
            "document_type": document_type
        })

    def classify_lob(self, parsed_data: list) -> dict:
        return self.send_task("validator", {
            "task": "classify",
            "parsed_data": parsed_data
        })

    def validate_completeness(self, parsed_data: list, line_of_business: str) -> dict:
        return self.send_task("validator", {
            "task": "validate",
            "parsed_data": parsed_data,
            "line_of_business": line_of_business
        })

    def route_submission(self, line_of_business: str, validation_result: dict, parsed_data: list) -> dict:
        return self.send_task("router", {
            "task": "route",
            "line_of_business": line_of_business,
            "validation_result": validation_result,
            "parsed_data": parsed_data
        })

    def generate_summary(self, submission_id: str, parsed_data: list, validation_result: dict, routing: dict, user_profile: dict) -> str:
        result = self.send_task("router", {
            "task": "summarize",
            "submission_id": submission_id,
            "parsed_data": parsed_data,
            "validation_result": validation_result,
            "routing": routing,
            "user_profile": user_profile
        })
        if isinstance(result, dict):
            if "summary" in result:
                return result["summary"]
            if "raw_output" in result:
                return result["raw_output"]
            return json.dumps(result)
        return result

    def __del__(self):
        try: self.client.close()
        except: pass
