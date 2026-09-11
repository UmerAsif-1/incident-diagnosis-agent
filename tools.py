"""
Three callable tools the agent uses to investigate the synthetic logs.
Each is a plain Python function first (independently testable), then wrapped
with an Anthropic tool schema in agent.py.
"""

import json
import glob
from datetime import datetime, timezone

LOG_DIR = "logs"


def _load_all_logs():
    entries = []
    for path in glob.glob(f"{LOG_DIR}/*.log"):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    entries.sort(key=lambda e: e["timestamp"])
    return entries


def _parse_ts(ts_str):
    """
    Parse a timestamp into an aware UTC datetime.

    The log files all use "...Z", but the agent passes timestamps it composed
    itself, so accept the common ISO variants ("+00:00", no zone, a space
    separator) instead of failing on anything but the exact log format.
    """
    try:
        return datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    normalized = ts_str.strip().replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    dt = datetime.fromisoformat(normalized)  # raises ValueError if truly unparseable
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def search_logs(query: str, service: str = None, level: str = None, limit: int = 20):
    """
    Search log messages for a substring, optionally filtered by service and/or level.

    Args:
        query: substring to search for in the log message (case-insensitive)
        service: optional service name to filter by (e.g. "checkout-service")
        level: optional log level to filter by (e.g. "ERROR", "WARN", "INFO")
        limit: max number of matching entries to return

    Returns:
        list of matching log entries (most recent first is not guaranteed;
        entries are returned in chronological order)
    """
    logs = _load_all_logs()
    query_lower = query.lower()
    # Levels are stored uppercase; a lowercase "error" from the caller would
    # otherwise match nothing and look like an absence of errors.
    level_upper = level.upper() if level else None

    results = []
    for entry in logs:
        if query_lower not in entry["message"].lower():
            continue
        if service and entry["service"] != service:
            continue
        if level_upper and entry["level"] != level_upper:
            continue
        results.append(entry)
        if len(results) >= limit:
            break

    return {
        "query": query,
        "filters": {"service": service, "level": level},
        "match_count": len(results),
        "results": results,
    }


def get_time_range(start: str, end: str, service: str = None):
    """
    Fetch all log entries within a timestamp range (inclusive).

    Args:
        start: ISO timestamp, e.g. "2026-09-10T09:00:00Z"
        end: ISO timestamp, e.g. "2026-09-10T09:20:00Z"
        service: optional service name to filter by

    Returns:
        list of log entries within the window
    """
    logs = _load_all_logs()
    start_dt = _parse_ts(start)
    end_dt = _parse_ts(end)

    results = []
    for entry in logs:
        entry_dt = _parse_ts(entry["timestamp"])
        if start_dt <= entry_dt <= end_dt:
            if service and entry["service"] != service:
                continue
            results.append(entry)

    return {
        "start": start,
        "end": end,
        "service": service,
        "match_count": len(results),
        "results": results,
    }


def check_related_service(service_name: str):
    """
    Get a health summary for a service: error/warn counts, recent deploy events,
    and the timestamps of its worst error clustering (useful for spotting
    whether a downstream service is the actual root cause).

    Args:
        service_name: e.g. "payment-service"

    Returns:
        summary dict with error_count, warn_count, info_count, recent_deploys,
        and the first/last error timestamps if any errors exist
    """
    logs = _load_all_logs()
    service_logs = [e for e in logs if e["service"] == service_name]

    if not service_logs:
        return {"service": service_name, "found": False, "message": "No logs found for this service"}

    error_count = sum(1 for e in service_logs if e["level"] == "ERROR")
    warn_count = sum(1 for e in service_logs if e["level"] == "WARN")
    info_count = sum(1 for e in service_logs if e["level"] == "INFO")

    deploys = [
        e for e in service_logs
        if "deploy" in e["message"].lower() or "deployment" in e["message"].lower()
    ]

    errors = [e for e in service_logs if e["level"] == "ERROR"]
    first_error = errors[0]["timestamp"] if errors else None
    last_error = errors[-1]["timestamp"] if errors else None

    return {
        "service": service_name,
        "found": True,
        "total_log_lines": len(service_logs),
        "error_count": error_count,
        "warn_count": warn_count,
        "info_count": info_count,
        "recent_deploys": deploys,
        "first_error_timestamp": first_error,
        "last_error_timestamp": last_error,
    }


if __name__ == "__main__":
    # Quick manual smoke test, no API key needed
    print("=== search_logs('timed out') ===")
    r = search_logs("timed out", limit=3)
    print(json.dumps(r, indent=2))

    print("\n=== check_related_service('payment-service') ===")
    r = check_related_service("payment-service")
    print(json.dumps(r, indent=2))

    print("\n=== get_time_range 09:00-09:05 ===")
    r = get_time_range("2026-09-10T09:00:00Z", "2026-09-05T09:05:00Z".replace("09-05", "09-10"))
    print(f"match_count: {r['match_count']}")
