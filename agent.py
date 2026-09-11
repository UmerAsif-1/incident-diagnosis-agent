"""
CLI incident-diagnosis agent.

Usage:
    python agent.py "checkout failures at 9am"

Runs a real plan -> act -> observe loop: Claude decides which tool to call,
we execute the actual Python function, feed the result back, and repeat
until Claude calls submit_diagnosis with a structured final report.

Requires: ANTHROPIC_API_KEY environment variable set.
"""

import sys
import os
import json
import anthropic

from tools import search_logs, get_time_range, check_related_service

MODEL = "claude-opus-5"
MAX_TURNS = 8

# Opus 5 runs adaptive thinking by default and those tokens count against this
# cap, so keep it generous - a low cap truncates the turn before the tool call.
MAX_TOKENS = 16000

SYSTEM_PROMPT = """You are an incident-diagnosis agent for a microservices platform.
You investigate production issues by querying log data using the tools available to you.

You have three investigation tools: search_logs, get_time_range, and check_related_service.
Use them to gather evidence before concluding anything. Don't guess at a root cause without
having actually queried logs that support it. A cascading failure (one service's problem
causing errors in another) is common - if you see errors in one service, check whether a
service it depends on is the actual root cause.

Once you have enough evidence, you MUST call submit_diagnosis with your final structured
report. Do not just describe your conclusion in text - always finish by calling submit_diagnosis.
"""

TOOLS = [
    {
        "name": "search_logs",
        "description": "Search log messages for a substring, optionally filtered by service and/or log level.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Substring to search for in log messages"},
                "service": {"type": "string", "description": "Optional service name to filter by"},
                "level": {"type": "string", "description": "Optional log level filter: ERROR, WARN, or INFO"},
                "limit": {"type": "integer", "description": "Max results to return, default 20"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_time_range",
        "description": "Fetch all log entries within a timestamp range (ISO format, e.g. 2026-09-10T09:00:00Z).",
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "Start ISO timestamp"},
                "end": {"type": "string", "description": "End ISO timestamp"},
                "service": {"type": "string", "description": "Optional service name to filter by"},
            },
            "required": ["start", "end"],
        },
    },
    {
        "name": "check_related_service",
        "description": "Get a health summary for a service: error/warn/info counts, recent deploy events, and error time range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "e.g. payment-service"},
            },
            "required": ["service_name"],
        },
    },
    {
        "name": "submit_diagnosis",
        "description": "Submit your final structured incident diagnosis. Call this exactly once, when you're done investigating.",
        "input_schema": {
            "type": "object",
            "properties": {
                "root_cause": {"type": "string", "description": "What actually broke, in plain language"},
                "location": {"type": "string", "description": "Which service/component and approximate timestamp"},
                "fix_suggestion": {"type": "string", "description": "Concrete suggested fix or mitigation"},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Short list of the specific log evidence that supports this diagnosis",
                },
            },
            "required": ["root_cause", "location", "fix_suggestion", "confidence", "evidence"],
        },
    },
]

TOOL_FUNCTIONS = {
    "search_logs": search_logs,
    "get_time_range": get_time_range,
    "check_related_service": check_related_service,
}


def run_tool(name, tool_input):
    fn = TOOL_FUNCTIONS[name]
    return fn(**tool_input)


def run_tool_safely(name, tool_input):
    """
    Execute a tool, turning any failure into a message the model can recover from.

    Claude occasionally invents an argument or a timestamp format we don't parse.
    Letting that raise would kill the whole investigation, so we hand the error
    back as a tool_result and let the model retry with different arguments.
    """
    try:
        return json.dumps(run_tool(name, tool_input)), False
    except KeyError:
        return f"No such tool: {name}", True
    except TypeError as e:
        return f"Bad arguments for {name}: {e}", True
    except Exception as e:
        return f"{name} failed: {type(e).__name__}: {e}", True


def print_tool_call(name, tool_input):
    print(f"  \033[36m[tool call]\033[0m {name}({json.dumps(tool_input)})")


def print_diagnosis(diagnosis):
    print("\n" + "=" * 60)
    print("INCIDENT DIAGNOSIS")
    print("=" * 60)
    print(f"Root cause:  {diagnosis['root_cause']}")
    print(f"Location:    {diagnosis['location']}")
    print(f"Fix:         {diagnosis['fix_suggestion']}")
    print(f"Confidence:  {diagnosis['confidence']}")
    print("Evidence:")
    for e in diagnosis["evidence"]:
        print(f"  - {e}")
    print("=" * 60)


def run_agent(user_query):
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    messages = [{"role": "user", "content": user_query}]
    print(f"\nInvestigating: \"{user_query}\"\n")

    for turn in range(MAX_TURNS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            # Claude responded with plain text instead of calling a tool.
            for block in response.content:
                if block.type == "text":
                    print(block.text)

            if response.stop_reason == "max_tokens":
                print("\n[Stopped: response hit max_tokens before a tool call. "
                      "Raise MAX_TOKENS and retry.]")
            elif response.stop_reason == "refusal":
                detail = getattr(response, "stop_details", None)
                category = getattr(detail, "category", None) if detail else None
                print(f"\n[Stopped: model declined to answer (category: {category}).]")
            break

        tool_results = []
        submitted_diagnosis = None

        for block in response.content:
            if block.type == "text" and block.text.strip():
                print(f"  \033[90m[reasoning]\033[0m {block.text.strip()}")
            elif block.type == "tool_use":
                print_tool_call(block.name, block.input)

                if block.name == "submit_diagnosis":
                    submitted_diagnosis = block.input
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "Diagnosis received.",
                        }
                    )
                else:
                    content, is_error = run_tool_safely(block.name, block.input)
                    if is_error:
                        print(f"  \033[31m[tool error]\033[0m {content}")
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": content,
                            "is_error": is_error,
                        }
                    )

        if submitted_diagnosis:
            print_diagnosis(submitted_diagnosis)
            return submitted_diagnosis

        messages.append({"role": "user", "content": tool_results})

    print("\n[Stopped: max turns reached without a final diagnosis]")
    return None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('Usage: python agent.py "checkout failures at 9am"')
        sys.exit(1)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Set ANTHROPIC_API_KEY in your environment first.")
        sys.exit(1)

    query = " ".join(sys.argv[1:])
    run_agent(query)
