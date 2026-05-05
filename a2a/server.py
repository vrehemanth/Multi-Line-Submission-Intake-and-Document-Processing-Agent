#!/usr/bin/env python3
"""
A2A Agent Server
================
Hosts any ADK Agent or plain Python function as an A2A HTTP microservice.

Execution Modes:
  - AGENT MODE: Handler is an ADK Agent (has .instruction + .tools).
    Uses the official ADK Runner + InMemorySessionService to properly invoke
    agent.run_async(). The ADK handles InvocationContext construction,
    the tool-selection loop, and the multi-turn conversation lifecycle.
    Each request gets a fresh in-memory session (stateless per-request).

  - FUNCTIONAL MODE: Handler is a plain Python function.
    Called directly with matching keyword args extracted from the payload.
"""
import asyncio
import importlib
import inspect
import json
import os
import sys
import traceback
import uuid

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from opentelemetry import propagate, trace

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from telemetry.arize_setup import setup_arize
setup_arize()

tracer = trace.get_tracer("a2a-host")

# App name used for all ADK sessions on this server instance
ADK_APP_NAME = "a2a-agent-server"


def create_app(card: dict) -> FastAPI:
    app = FastAPI(title=f"A2A Agent: {card['name']}", version="1.0.0")
    agent_name = card["name"]
    port = card["port"]

    skill_cfg = card["skills"][0]
    module = importlib.import_module(skill_cfg["handler_module"])
    handler = getattr(module, skill_cfg["handler_fn"])

    # ── Set up ADK Runner once at startup (if handler is an ADK Agent) ──────
    adk_runner: Runner | None = None
    session_service: InMemorySessionService | None = None

    if hasattr(handler, "instruction") and hasattr(handler, "tools"):
        session_service = InMemorySessionService()
        adk_runner = Runner(
            agent=handler,
            app_name=ADK_APP_NAME,
            session_service=session_service,
        )
        print(f"[{agent_name}] ADK Runner initialized for agent '{handler.name}'")

    @app.get("/health")
    def health():
        return {"status": "ok", "agent": agent_name, "port": port}

    @app.get("/.well-known/agent.json")
    @app.get("/.well-known.json")
    def get_agent_info():
        """Returns the agent's metadata and skill definitions."""
        return card

    @app.post("/v1/tasks")
    async def handle_task(task: dict, request: Request):
        ctx = propagate.extract(dict(request.headers))
        try:
            raw_input = task["message"]["parts"][0]["text"]
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid A2A Task structure")

        task_id = str(uuid.uuid4())
        with tracer.start_as_current_span(f"A2A:{agent_name}:run", context=ctx) as span:
            print(f"[{agent_name}] -> PROCESSING {task_id[:8]}")
            sys.stdout.flush()

            try:
                # ── MODE 1: ADK AGENT via Runner.run_async() ────────────────
                if adk_runner is not None:
                    print(f"[{agent_name}] Mode: ADK RUNNER (agent.run_async via InvocationContext)")

                    # Fresh stateless session per request
                    session_id = task_id
                    user_id = "a2a-server"

                    await session_service.create_session(
                        app_name=ADK_APP_NAME,
                        user_id=user_id,
                        session_id=session_id,
                    )

                    new_message = Content(role="user", parts=[Part(text=raw_input)])

                    # We prefer the raw tool output (FunctionResponse) over the model's
                    # final reformatted text, because the model sometimes changes key names
                    # or returns empty text after a tool call.
                    tool_result_text = None   # set when ADK executes a tool
                    final_response_text = None  # fallback: model's final text

                    async for event in adk_runner.run_async(
                        user_id=user_id,
                        session_id=session_id,
                        new_message=new_message,
                    ):
                        if event.content and event.content.parts:
                            for part in event.content.parts:
                                # ── Capture raw tool output from FunctionResponse ──
                                fn_resp = getattr(part, "function_response", None)
                                if fn_resp is not None:
                                    resp = getattr(fn_resp, "response", None)
                                    if resp is not None:
                                        # ADK wraps string returns as {"result": "..."};
                                        # dict returns are stored as proto MapComposite.
                                        if isinstance(resp, dict):
                                            keys = list(resp.keys())
                                            if keys == ["result"]:
                                                # Unwrap string tool return
                                                tool_result_text = str(resp["result"])
                                            else:
                                                tool_result_text = json.dumps(dict(resp))
                                        else:
                                            # proto MapComposite → convert to plain dict
                                            try:
                                                plain = {k: v for k, v in resp.items()}
                                                tool_result_text = json.dumps(plain)
                                            except Exception:
                                                tool_result_text = str(resp)
                                        print(f"[{agent_name}] Captured raw tool result ({len(tool_result_text)} chars): {tool_result_text[:120]}")

                        # Also collect the final model text as fallback
                        if event.is_final_response():
                            if event.content and event.content.parts:
                                for part in reversed(event.content.parts):
                                    if hasattr(part, "text") and part.text:
                                        final_response_text = part.text
                                        break

                    # Prefer raw tool result; fall back to model's final text
                    if tool_result_text:
                        final_text = tool_result_text
                        print(f"[{agent_name}] Using raw tool result as response")
                    elif final_response_text:
                        final_text = final_response_text
                        print(f"[{agent_name}] Using final model text as response ({len(final_response_text)} chars)")
                    else:
                        print(f"[{agent_name}] WARNING: Agent returned no output at all")
                        final_text = json.dumps({"error": "Agent returned empty response"})

                # ── MODE 2: PLAIN FUNCTION ────────────────────────────────────
                else:
                    print(f"[{agent_name}] Mode: FUNCTIONAL EXECUTION")
                    try:
                        input_data = json.loads(raw_input)
                    except Exception:
                        input_data = {"raw": raw_input}

                    sig = inspect.signature(handler, follow_wrapped=True)
                    filtered = {k: v for k, v in input_data.items() if k in sig.parameters}

                    if inspect.iscoroutinefunction(handler):
                        result = await handler(**filtered)
                    else:
                        result = await asyncio.get_event_loop().run_in_executor(
                            None, lambda: handler(**filtered)
                        )
                    final_text = json.dumps(result) if isinstance(result, (dict, list)) else str(result)

                print(f"[{agent_name}] <- SUCCESS")
                sys.stdout.flush()
                return {"artifacts": [{"parts": [{"text": final_text}]}]}

            except Exception as e:
                print(f"!!! [{agent_name}] CRASHED !!!")
                traceback.print_exc()
                sys.stdout.flush()
                raise HTTPException(status_code=500, detail=str(e))

    return app


if __name__ == "__main__":
    with open(sys.argv[1], "r") as f:
        agent_card = json.load(f)
    uvicorn.run(
        create_app(agent_card),
        host="0.0.0.0",
        port=agent_card["port"],
        log_level="warning",
    )
