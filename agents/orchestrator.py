import os
import json
import concurrent.futures
import threading
import time
from datetime import datetime
from google.adk import Agent
from telemetry.arize_setup import setup_arize, get_tracer
from opentelemetry.trace import Status, StatusCode

# Initialize Arize OTel Tracing BEFORE importing child agents
setup_arize()
tracer = get_tracer()

from profiles.user_profile_manager import UserProfileManager
from a2a.client import A2AClient

def process_submission(submission_id: str, user_id: str = "default_user") -> dict:
    """
    Pipeline mode: Process an entire submission.
    1. Load all documents from data/submissions/{submission_id}/
    2. Fan out to Document Parser (parallel, one per doc)
    3. Send parsed data to Validator & Classifier
    4. Send to Router for queue assignment + summary
    """
    sub_dir = os.path.join("data", "submissions", submission_id)
    if not os.path.exists(sub_dir):
        return {"error": f"Submission {submission_id} not found."}

    # Wrap the entire process in a root span for Batch Analytics
    with tracer.start_as_current_span("process_submission") as root_span:
        root_span.set_attribute("openinference.span.kind", "CHAIN")
        root_span.set_attribute("input.value", f"Processing {submission_id} for user {user_id}")
        root_span.set_attribute("submission_id", submission_id)
        
        doc_files = [f for f in os.listdir(sub_dir) if f.endswith(".json") or f.endswith(".pdf")]
    
        # 1 & 2. Parse all documents in parallel via A2A HTTP calls
        parsed_data = []
        a2a = A2AClient()

        def _parse_doc(filename):
            thread_id = threading.get_ident()
            start_time = time.time()
            print(f"--> [PARALLEL START] Doc: {filename} | Thread: {thread_id} | Time: {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")
            
            with tracer.start_as_current_span("parse_document_task") as span:
                span.set_attribute("openinference.span.kind", "CHAIN")
                path = os.path.join(sub_dir, filename)
                doc_type = os.path.splitext(filename)[0]
                span.set_attribute("input.value", f"Parsing document: {filename} from {submission_id}")
                span.set_attribute("document_type", doc_type)
                span.set_attribute("file_path", path)
                
                # Real A2A HTTP call to DocumentParser on :8001
                output_payload = a2a.parse_document(path, doc_type)
                span.set_attribute("output.value", json.dumps(output_payload, indent=2))
                span.set_status(Status(StatusCode.OK))
                duration = time.time() - start_time
                print(f"<-- [PARALLEL END]   Doc: {filename} | Thread: {thread_id} | Duration: {duration:.2f}s")
                return output_payload

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            future_to_doc = {executor.submit(_parse_doc, doc): doc for doc in doc_files}
            for future in concurrent.futures.as_completed(future_to_doc):
                try:
                    res = future.result()
                    parsed_data.append(res)
                except Exception as exc:
                    print(f"Doc parsing generated an exception: {exc}")

        # 3. Classify LOB and validate via A2A HTTP calls to Validator on :8002
        classification = a2a.classify_lob(parsed_data)
        line = classification.get("primary_line", "unknown")
        validation = a2a.validate_completeness(parsed_data, line)

        # Load user profile
        profile_mgr = UserProfileManager()
        profile = profile_mgr.get_or_create(user_id)

        # 4. Route and summarize via A2A HTTP calls to Router on :8003
        routing = a2a.route_submission(line, validation, parsed_data)
        summary = a2a.generate_summary(submission_id, parsed_data, validation, routing, profile)

        # Add Analytics Attributes to Root Span
        root_span.set_attribute("is_multiline", line == "multi_line")
        root_span.set_attribute("submission.priority", routing.get("priority", "routine"))
        root_span.set_attribute("submission.queue", routing.get("queue", "unknown"))
        root_span.set_attribute("completeness_score", validation.get("completeness_score", 0.0))
        root_span.set_attribute("document_count", len(parsed_data))

        # Build result
        result = {
            "submission_id": submission_id,
            "status": "complete",
            "documents_processed": len(parsed_data),
            "classification": classification,
            "validation": validation,
            "routing": routing,
            "summary": summary,
            "processed_at": datetime.utcnow().isoformat()
        }

        # 5. Save to processed
        os.makedirs(os.path.join("data", "processed"), exist_ok=True)
        with open(os.path.join("data", "processed", f"{submission_id}.json"), "w") as f:
            json.dump(result, f, indent=2)

        root_span.set_attribute("output.value", json.dumps(result, indent=2))
        root_span.set_status(Status(StatusCode.OK))
        return result

def process_batch(submission_ids: list[str], user_id: str = "default_user") -> list[dict]:
    """
    Batch mode: Process multiple submissions.
    """
    results = []
    for sid in submission_ids:
        res = process_submission(sid, user_id)
        results.append(res)
    return results

def get_submission_status(submission_id: str, user_id: str = "default_user") -> dict:
    """
    Chat mode: Look up processed result
    """
    path = os.path.join("data", "processed", f"{submission_id}.json")
    if not os.path.exists(path):
        return {"error": f"Submission {submission_id} not found in processed results."}
    
    with open(path) as f:
        data = json.load(f)

    # Adapt detail to user profile
    profile_mgr = UserProfileManager()
    profile = profile_mgr.get_or_create(user_id)
    
    # Simple adaptation for chat mode based on profile
    is_clerk = profile.get("role") == "clerk"
    
    return {
        "submission_id": submission_id,
        "status": data.get("status"),
        "queue": data.get("routing", {}).get("queue"),
        "missing_fields": data.get("validation", {}).get("missing_fields", []) if is_clerk else None,
        "full_summary": data.get("summary")
    }

def set_user_role(user_id: str, role: str) -> dict:
    """Set the user's role (clerk or manager)"""
    profile_mgr = UserProfileManager()
    return profile_mgr.update(user_id, {
        "role": role,
        "preferred_detail": "verbose" if role == "clerk" else "concise"
    })

# Define the root agent in Python to avoid YAML validation issues and ensure tools are loaded
intake_orchestrator = Agent(
    name="intake_orchestrator",
    model="gemini-2.5-pro",
    description="""Dual-mode agent for insurance submission intake.
    Pipeline mode: processes document bundles automatically.
    Chat mode: answers submission status queries.""",
    tools=[
        process_submission,
        process_batch,
        get_submission_status,
        set_user_role
    ],
    instruction="""You are an insurance submission intake assistant with two modes:

    PIPELINE MODE (user says "process submission <ID>" or "process batch"):
    Submission IDs can be in ANY format (e.g. SUB-001, P-001, P-005, etc.).
    Accept whatever ID the user provides — do NOT reject based on prefix.
    1. Load documents from the submission folder
    2. Parse all documents in parallel via parse_documents
    3. Validate completeness and classify line of business via validate_and_classify
    4. Route to underwriting queue and generate summary via route_and_summarize
    5. Save result to data/processed/
    6. Return the intake summary

    CHAT MODE (user asks about status, or general questions):
    1. Look up submission via get_submission_status
    2. Respond conversationally, adapting detail to user profile

    On first interaction, ask the user their role to set up profiling."""
)
