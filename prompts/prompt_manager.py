from typing import Optional

TEMPLATES = {
    "document_extraction": {
      "v1": """Extract structured data from this insurance submission document.

      Document Type: {document_type}
      Document Content:
      {document_content}

      Extract ALL relevant fields. Return JSON:
      {{
        "document_type": "{document_type}",
        "extracted_fields": {{
          "field_name": "value"
        }},
        "confidence": 0.9,
        "notes": "any extraction issues or ambiguities"
      }}

      For {document_type}, key fields to look for:
      - application: insured_name, business_type, industry, revenue, employee_count, effective_date, requested_limits
      - fleet_schedule: vehicle_count, vehicle_types, model_years, VINs, garaging_locations
      - property_schedule: building_addresses, construction_types, square_footage, building_values, occupancy
      - loss_history: claim_count, total_incurred, largest_loss, loss_years, claim_descriptions
      - revenue_info: annual_revenue, revenue_by_segment, revenue_trend
      - operations_description: primary_operations, locations, hazards, safety_programs"""
    },
    "lob_classification": {
      "v1": """Classify this submission's line of business based on the extracted documents.

      Extracted data from all documents:
      {parsed_data_json}

      EXAMPLES:
      ---
      Example 1: Documents contain fleet_schedule (50 vehicles), driver_list, auto loss history
      Classification: commercial_auto
      Reasoning: Fleet schedule and driver list are auto-specific. No property or GL docs.
      ---
      Example 2: Documents contain property_schedule (3 buildings), building_details, property loss history
      Classification: commercial_property
      Reasoning: Property schedule and building details indicate property line. No auto or GL docs.
      ---
      Example 3: Documents contain application (consulting firm), revenue_info, operations_description, fleet_schedule
      Classification: multi_line
      Reasoning: Operations description + revenue = GL indicators. Fleet schedule = auto indicator. Two lines present.
      ---

      Now classify the given submission. Think step-by-step:
      1. What document types are present?
      2. Which line-specific indicators exist?
      3. Is this single-line or multi-line?

      Return JSON:
      {{
        "primary_line": "commercial_auto|commercial_property|general_liability|multi_line",
        "secondary_lines": [],
        "confidence": 0.9,
        "reasoning": "step-by-step reasoning"
      }}"""
    },
    "completeness_validation": {
      "v1": """Validate the completeness of this {line_of_business} submission.

      Extracted data:
      {parsed_data_json}

      Required fields for {line_of_business}:
      {required_fields_json}

      Think step-by-step:
      1. List each required field
      2. Check if it was extracted (present and non-null)
      3. Rate the quality of each extracted value (complete, partial, missing)
      4. Calculate overall completeness score

      Return JSON:
      {{
        "completeness_score": 0.9,
        "field_status": {{
          "field_name": {{"status": "complete|partial|missing", "value": "extracted value or null", "note": "..."}}
        }},
        "missing_fields": ["list of missing field names"],
        "validation_notes": "overall assessment"
      }}"""
    },
    "routing_decision": {
      "v1": """Determine the underwriting queue for this submission.

      [CRITICAL] Line of Business: {line_of_business}
      [CONTEXT] Completeness Score: {completeness_score}
      [CONTEXT] Missing Fields: {missing_fields_json}

      Routing Rules:
      - commercial_auto → "Auto Queue"
      - commercial_property → "Property Queue"
      - general_liability → "GL Queue"
      - multi_line → "Complex Risk Queue"
      - completeness_score < 0.7 → "Hold Queue" (regardless of line)

      Priority Rules:
      - 'urgent' if completeness_score < 0.7 OR if line is multi_line OR if parsed data shows large exposure (e.g. >10 vehicles, high revenue)
      - 'routine' otherwise

      Parsed Data:
      {parsed_data_json}

      Return JSON:
      {{
        "queue": "queue name",
        "routing_reason": "why this queue",
        "priority": "routine|urgent",
        "action_needed": "none|request_missing_info|manual_review"
      }}"""
    },
    "user_profiling": {
      "v1": """Determine this user's role and experience from their input.

      User message: {user_message}
      Conversation history: {conversation_history}

      Indicators of OPERATIONS CLERK (junior):
      - Asks what fields are required
      - Processes one submission at a time
      - Needs explanations of validation results

      Indicators of OPERATIONS MANAGER (senior):
      - Submits batches
      - Asks about stats and routing accuracy
      - Uses technical insurance terms
      - Wants concise summaries

      Return JSON:
      {{
        "role": "clerk|manager",
        "confidence": 0.9,
        "signals": ["observed indicators"]
      }}"""
    },
    "intake_summary": {
      "v1": """Generate an intake summary report for submission {submission_id}.

      Parsed Data: {parsed_data_json}
      Validation Result: {validation_result_json}
      Routing Result: {routing_result_json}
      User Profile: {user_profile_json}

      If the user is a clerk (preferred_detail='verbose'), provide a detailed field-by-field breakdown and explain any missing fields clearly.
      If the user is a manager (preferred_detail='concise'), provide a concise summary table with key metrics and routing decision.

      Return ONLY the summary string (can be formatted as Markdown). Do not wrap it in JSON.
      """
    }
}

class PromptManager:
    def __init__(self):
        self._templates = TEMPLATES
        self._usage_log = []

    def get_prompt(
        self,
        template_name: str,
        version: str = "v1",
        variables: Optional[dict] = None
    ) -> str:
        template = self._templates[template_name][version]
        # Use format_map as requested
        rendered = template.format_map(variables or {})
        self._usage_log.append({
            "template": template_name,
            "version": version,
            "variables_keys": list((variables or {}).keys())
        })
        return rendered

    def list_templates(self) -> list[str]:
        return list(self._templates.keys())

    def get_versions(self, template_name: str) -> list[str]:
        return [v for v, t in self._templates[template_name].items() if t is not None]

    def get_usage_log(self) -> list[dict]:
        return self._usage_log
