"""
Generates synthetic CloudWatch-style logs for five microservices, with a
realistic embedded incident: a bad deploy to payment-service shrinks its DB
connection pool, which starves payment-service, which cascades into
checkout-service failures ~09:00-09:20.

Pure Python, no AWS account or API calls required. Deterministic (seeded)
so the "incident" is reproducible for testing the agent against.
"""

import json
import random
from datetime import datetime, timedelta, timezone

# Seeded so the embedded incident is byte-for-byte reproducible: the agent's
# behaviour can be compared across runs against identical input.
random.seed(42)


def request_id():
    """Short correlation id. Drawn from the seeded RNG to keep output stable."""
    return f"{random.getrandbits(32):08x}"

SERVICES = [
    "checkout-service",
    "payment-service",
    "auth-service",
    "inventory-service",
    "notification-service",
]

BASE_DAY = datetime(2026, 9, 10, tzinfo=timezone.utc)
DAY_START = BASE_DAY.replace(hour=0, minute=0, second=0)
DAY_END = BASE_DAY.replace(hour=23, minute=59, second=59)

# Incident window: deploy at 08:55, cascading failures 09:00-09:20, recovery by 09:25
DEPLOY_TIME = BASE_DAY.replace(hour=8, minute=55, second=0)
INCIDENT_START = BASE_DAY.replace(hour=9, minute=0, second=0)
INCIDENT_END = BASE_DAY.replace(hour=9, minute=20, second=0)
RECOVERY_TIME = BASE_DAY.replace(hour=9, minute=25, second=0)


def rand_ts_between(start, end):
    delta = (end - start).total_seconds()
    return start + timedelta(seconds=random.uniform(0, delta))


def log_line(ts, service, level, message, **extra):
    entry = {
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "service": service,
        "level": level,
        "message": message,
        "request_id": request_id(),
    }
    entry.update(extra)
    return entry


def generate_baseline_traffic(service, count, start=DAY_START, end=DAY_END):
    """Normal INFO-level traffic for a service across the whole day."""
    logs = []
    normal_messages = {
        "checkout-service": [
            "Checkout completed successfully",
            "Cart validated, proceeding to payment",
            "Order confirmation sent",
        ],
        "payment-service": [
            "Payment authorized",
            "Payment captured successfully",
            "Refund processed",
        ],
        "auth-service": [
            "User login successful",
            "Token refreshed",
            "Session validated",
        ],
        "inventory-service": [
            "Stock reserved for order",
            "Inventory count updated",
            "Warehouse sync completed",
        ],
        "notification-service": [
            "Email notification sent",
            "SMS notification sent",
            "Push notification delivered",
        ],
    }
    for _ in range(count):
        ts = rand_ts_between(start, end)
        msg = random.choice(normal_messages[service])
        logs.append(log_line(ts, service, "INFO", msg))
    return logs


def generate_deploy_event():
    """The root cause: a deploy that shrinks payment-service's DB connection pool."""
    return [
        log_line(
            DEPLOY_TIME,
            "payment-service",
            "INFO",
            "Deployment v2.14.3 completed: connection_pool_size changed from 50 to 5",
            deploy_version="v2.14.3",
            config_change="connection_pool_size=5",
        )
    ]


def generate_payment_incident_logs():
    """payment-service starts exhausting its DB connection pool right after deploy."""
    logs = []
    t = INCIDENT_START
    while t < INCIDENT_END:
        logs.append(
            log_line(
                t,
                "payment-service",
                "ERROR",
                "DB connection pool exhausted: 5/5 connections in use, request queued and timed out",
                error_code="DB_POOL_EXHAUSTED",
            )
        )
        t += timedelta(seconds=random.uniform(5, 15))

    # Recovery: on-call rolls back the config
    logs.append(
        log_line(
            RECOVERY_TIME,
            "payment-service",
            "INFO",
            "Deployment v2.14.4 completed: connection_pool_size changed from 5 to 50 (rollback)",
            deploy_version="v2.14.4",
            config_change="connection_pool_size=50",
        )
    )
    return logs


def generate_checkout_incident_logs():
    """checkout-service fails because its calls to payment-service time out."""
    logs = []
    t = INCIDENT_START
    while t < INCIDENT_END:
        logs.append(
            log_line(
                t,
                "checkout-service",
                "ERROR",
                "Checkout failed: downstream call to payment-service timed out after 3000ms",
                error_code="DOWNSTREAM_TIMEOUT",
                downstream_service="payment-service",
            )
        )
        t += timedelta(seconds=random.uniform(3, 10))
    return logs


def generate_all_logs():
    all_logs = []

    # Baseline "everything is fine" traffic for all services
    all_logs += generate_baseline_traffic("checkout-service", 120)
    all_logs += generate_baseline_traffic("payment-service", 120)
    all_logs += generate_baseline_traffic("auth-service", 80)
    all_logs += generate_baseline_traffic("inventory-service", 80)
    all_logs += generate_baseline_traffic("notification-service", 80)

    # A few unrelated warnings sprinkled in, so the agent has to distinguish
    # signal from noise instead of assuming every ERROR/WARN is the incident.
    all_logs.append(
        log_line(
            BASE_DAY.replace(hour=14, minute=12),
            "notification-service",
            "WARN",
            "SMS provider latency elevated (non-critical, retried successfully)",
        )
    )
    all_logs.append(
        log_line(
            BASE_DAY.replace(hour=17, minute=3),
            "auth-service",
            "WARN",
            "Rate limiter triggered for IP range, likely bot traffic",
        )
    )

    # The actual incident: root cause + cascading failure
    all_logs += generate_deploy_event()
    all_logs += generate_payment_incident_logs()
    all_logs += generate_checkout_incident_logs()

    all_logs.sort(key=lambda x: x["timestamp"])
    return all_logs


def write_logs_by_service(logs, out_dir="logs"):
    by_service = {}
    for entry in logs:
        by_service.setdefault(entry["service"], []).append(entry)

    for service, entries in by_service.items():
        path = f"{out_dir}/{service}.log"
        with open(path, "w") as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
        print(f"Wrote {len(entries)} lines to {path}")


if __name__ == "__main__":
    logs = generate_all_logs()
    write_logs_by_service(logs)
    print(f"\nTotal log lines: {len(logs)}")
    print("Incident scenario: payment-service deploy at 08:55 shrinks DB pool -> "
          "payment-service errors 09:00-09:20 -> checkout-service cascading failures "
          "09:00-09:20 -> rollback + recovery at 09:25")
