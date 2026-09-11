# Incident Diagnosis Agent

An autonomous agent that investigates production incidents in CloudWatch-style logs
and produces a structured root-cause report — root cause, location, suggested fix,
supporting evidence, and a confidence level.

It runs a real **plan → act → observe** tool-calling loop: the model chooses which
tool to call, the tool executes against actual log data, the result is fed back, and
the model decides what to do next — until it has enough evidence to commit to a
diagnosis. The final answer is produced through a forced tool call, so the output is
always structured and machine-readable rather than free-form prose.

---

## The problem

When a user-facing service starts failing, the errors you see first are usually not
where the bug is. A checkout service timing out looks like a checkout bug, but the
cause is often a service two hops downstream. Triage means correlating error windows
across services and lining them up against recent deploys — mechanical work that
takes an on-call engineer several minutes per incident, at the worst possible time.

This agent automates that first pass of triage: it distinguishes a *symptom* from a
*cause* by following the dependency chain through the logs.

---

## Example run

```
$ python agent.py "checkout failures at 9am"

Investigating: "checkout failures at 9am"

  [tool call] search_logs({"query": "checkout", "level": "ERROR"})
  [tool call] get_time_range({"start": "2026-09-10T08:45:00Z", "end": "2026-09-10T09:15:00Z"})
  [reasoning] Clear root cause found. Let me confirm with a check on payment-service health.
  [tool call] check_related_service({"service_name": "payment-service"})
  [reasoning] A bad config deploy dropped the DB connection pool from 50 to 5 at 08:55,
              causing pool exhaustion at 09:00, which cascaded into checkout timeouts.
  [tool call] submit_diagnosis({...})

============================================================
INCIDENT DIAGNOSIS
============================================================
Root cause:  A bad config change in payment-service deploy v2.14.3 (08:55) shrank the
             DB connection pool from 50 to 5 connections. Under normal morning traffic
             the 5 connections saturated immediately, so requests queued and timed out.
             checkout-service was not itself broken — it was a cascading victim.
Location:    payment-service (DB connection pool config, deploy v2.14.3),
             incident window 2026-09-10T09:00:00Z – 09:19:59Z
Fix:         Rollback already shipped (v2.14.4 restored pool_size=50). To prevent
             recurrence: guard connection_pool_size with a validated minimum, alert on
             DB_POOL_EXHAUSTED and >80% pool utilisation, add a circuit breaker in
             checkout-service's payment client.
Confidence:  high
Evidence:
  - payment-service deploy at 08:55:00Z: connection_pool_size changed from 50 to 5
  - 117 DB_POOL_EXHAUSTED errors in payment-service starting 09:00:00Z
  - checkout-service DOWNSTREAM_TIMEOUT errors in lockstep, same window
  - rollback deploy v2.14.4 at 09:25:00Z restored pool size; errors stop
============================================================
```

Note what the agent did here: it was asked about **checkout**, but concluded the bug
was in **payment-service** and said so explicitly. It also ruled out a database
outage before committing — the database itself logged no errors, so the fault was
client-side pool sizing rather than a slow or unavailable DB.

---

## How it works

```
                   ┌──────────────────────────────┐
   user query ───► │  agent loop  (agent.py)      │
                   │                              │
                   │  1. model picks a tool       │
                   │  2. tool runs on real logs   │ ◄──┐
                   │  3. result fed back          │    │ repeat until
                   │  4. model decides next step  │ ───┘ enough evidence
                   └──────────────┬───────────────┘
                                  │ forced structured tool call
                                  ▼
                        submit_diagnosis(...)
                   root cause · location · fix · evidence · confidence
```

The loop is capped at `MAX_TURNS` so a confused model cannot spin indefinitely.

### Investigation tools

The agent is given three read-only tools and decides for itself which to use:

| Tool | Purpose |
|---|---|
| `search_logs` | Substring search across all services, filterable by service and level |
| `get_time_range` | Every log entry in a timestamp window — used to correlate across services |
| `check_related_service` | Health summary for one service: error/warn/info counts, recent deploys, first/last error timestamps |

`check_related_service` is the one that makes cascade detection work: returning deploy
events *and* the error time range together lets the model line up "when did this break"
against "what changed" in a single call.

A fourth tool, `submit_diagnosis`, is how the agent finishes. Its schema requires every
field of the report, which is what forces a complete, structured answer instead of a
paragraph of hedged text.

---

## Architecture

```
generate_logs.py   synthetic CloudWatch-style logs (logs/*.log), pure Python,
                   seeded so the incident is byte-for-byte reproducible

tools.py           the three investigation tools as plain Python functions —
                   no API calls, independently runnable and testable

agent.py           the plan → act → observe loop, tool schemas, and the
                   structured-output contract
```

The split is deliberate: the tools are ordinary functions that know nothing about the
model. They can be exercised directly with no API key, which means the retrieval logic
can be debugged without paying for or waiting on inference.

### The incident scenario

The generated data embeds a realistic cascading failure rather than a toy error:

| Time | Event |
|---|---|
| 08:55 | `payment-service` deploy v2.14.3 changes `connection_pool_size` from 50 → 5 |
| 09:00 | `payment-service` begins emitting `DB_POOL_EXHAUSTED` (117 errors) |
| 09:00 | `checkout-service` starts failing with `DOWNSTREAM_TIMEOUT` on payment calls |
| 09:25 | Rollback v2.14.4 restores the pool size; errors stop |

Two unrelated warnings (an SMS latency blip at 14:12, a rate limiter at 17:03) are
seeded elsewhere in the day, so the agent has to separate signal from noise instead of
assuming every non-INFO line belongs to the incident.

---

## Running it

Requires Python 3.10+ and an Anthropic API key.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python generate_logs.py                    # writes logs/*.log

export ANTHROPIC_API_KEY=your_key_here
python agent.py "checkout failures at 9am"
```

The query is free-form — `"payment errors this morning"` or `"why did orders fail"`
exercise different investigation paths.

### Testing the tools without an API key

The retrieval layer runs standalone, no key and no network required:

```bash
python tools.py
```

This prints the result of each tool against the generated logs — useful for verifying
the search and correlation logic in isolation from the model.

---

## Design decisions

**Structured output via a forced tool call.** The model must finish by calling
`submit_diagnosis`, whose schema requires all five report fields. An LLM asked for a
diagnosis in prose tends to hedge across several paragraphs; a required schema forces
it to commit to one root cause, one location, and an explicit confidence level. The
result is also directly consumable by another system — it could open a ticket or page
a team without any text parsing.

**Tool errors are returned to the model, not raised.** A malformed argument or an
unparseable timestamp comes back as a tool result flagged as an error, so the model can
correct itself and continue. Letting it propagate would abort an investigation that was
otherwise one call from finishing.

**Log levels are normalised on input.** A lowercase `"error"` filter would match
nothing against `"ERROR"` records and return an empty result set — which an agent reads
as *"there are no errors"*. A silently empty result is worse than a loud failure here,
because it actively misleads the diagnosis, so level filters are case-normalised.

**Generous token budget.** The model runs with thinking enabled, and those tokens count
against the response cap. Too low a cap truncates a turn *before* it emits its tool
call, which surfaces as an agent that mysteriously stops mid-investigation.

**Seeded log generation.** Every run produces identical logs, so a change in the
agent's behaviour is attributable to the prompt, the tools, or the model — never to
different input data.

---

## Limitations

- Logs are synthetic and read from local files; there is no real CloudWatch integration yet.
- The scenario is a single known incident, so it demonstrates the loop rather than
  proving generality across incident classes.
- Log volume is small enough to pass results inline. Production-scale triage would need
  pagination and aggregation so results fit in the context window.
- There is no automated evaluation of diagnosis accuracy — correctness is currently
  verified by reading the trace.

## Possible next steps

- Read-only AWS CloudWatch Logs Insights integration
- Expose the tools over MCP instead of inline function definitions
- An eval set of several seeded incidents, scoring whether the agent names the right
  root-cause service
- A web UI streaming the live tool-call trace instead of CLI output
