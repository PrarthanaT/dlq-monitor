#!/usr/bin/env python3
"""Load test for DLQ Monitor: floods the DLQ, triggers polling, measures throughput."""

import argparse
import json
import time
import uuid

import boto3
import requests

API_URL = "https://be7vs42u6g.execute-api.us-east-1.amazonaws.com"
EVENTBRIDGE_RULE = "dlq-monitor-poller-schedule"

FAILURE_TEMPLATES = [
    {"error": "connection timeout", "service": "payments-api", "category": "TIMEOUT"},
    {"error": "validation error: missing required field user_id", "category": "VALIDATION_ERROR"},
    {"error": "connection refused: service unavailable", "service": "inventory-api", "category": "DEPENDENCY_FAILURE"},
    {"error": "something unexpected happened", "category": "UNKNOWN"},
]

TRANSIENT = {"TIMEOUT", "DEPENDENCY_FAILURE", "UNKNOWN"}


def get_dlq_url() -> str:
    cfn = boto3.client("cloudformation", region_name="us-east-1")
    outputs = cfn.describe_stacks(StackName="DlqMonitorStack")["Stacks"][0]["Outputs"]
    for o in outputs:
        if o["OutputKey"] == "DlqUrl":
            return o["OutputValue"]
    raise RuntimeError("DlqUrl not found in CloudFormation outputs")


def set_eventbridge_rule(enabled: bool) -> None:
    eb = boto3.client("events", region_name="us-east-1")
    if enabled:
        eb.enable_rule(Name=EVENTBRIDGE_RULE)
        print("  EventBridge rule re-enabled")
    else:
        eb.disable_rule(Name=EVENTBRIDGE_RULE)
        print("  EventBridge rule disabled (background poller paused)")


def build_messages(count: int) -> list[list[dict]]:
    batches = []
    batch = []
    for i in range(count):
        template = FAILURE_TEMPLATES[i % len(FAILURE_TEMPLATES)]
        body = {**template, "order_id": str(uuid.uuid4()), "seq": i}
        entry = {
            "Id": str(i),
            "MessageBody": json.dumps(body),
        }
        batch.append(entry)
        if len(batch) == 10:
            batches.append(batch)
            batch = []
    if batch:
        batches.append(batch)
    return batches


def send_all(sqs, dlq_url: str, batches: list[list[dict]]) -> int:
    total = 0
    for batch in batches:
        resp = sqs.send_message_batch(QueueUrl=dlq_url, Entries=batch)
        total += len(resp.get("Successful", []))
        failed = resp.get("Failed", [])
        if failed:
            print(f"  WARNING: {len(failed)} messages failed to send")
    return total


def get_queue_depth(sqs, dlq_url: str) -> int:
    attr_names = [
        "ApproximateNumberOfMessages",
        "ApproximateNumberOfMessagesNotVisible",
    ]
    attrs = sqs.get_queue_attributes(
        QueueUrl=dlq_url, AttributeNames=attr_names,
    )
    visible = int(attrs["Attributes"].get("ApproximateNumberOfMessages", "0"))
    in_flight = int(attrs["Attributes"].get("ApproximateNumberOfMessagesNotVisible", "0"))
    return visible, in_flight


def poll_and_process(sqs, dlq_url: str, timeout: int = 300) -> dict:
    """Poll via the API, track results from responses, delete permanent failures."""
    totals = {"processed": 0, "retried": 0, "dead": 0, "polls": 0, "empty_polls": 0}
    deadline = time.monotonic() + timeout
    consecutive_empty = 0

    while time.monotonic() < deadline:
        totals["polls"] += 1
        poll_num = totals["polls"]

        try:
            resp = requests.get(
                f"{API_URL}/messages", params={"retry": "true"}, timeout=30,
            )
            resp.raise_for_status()
            messages = resp.json()
        except requests.RequestException as exc:
            print(f"  Poll #{poll_num:>3d}  |  ERROR: {exc}")
            time.sleep(2)
            continue

        if not messages:
            totals["empty_polls"] += 1
            consecutive_empty += 1
            visible, in_flight = get_queue_depth(sqs, dlq_url)
            print(f"  Poll #{poll_num:>3d}  |  0 messages  |  "
                  f"queue: {visible} visible, {in_flight} in-flight")
            if visible == 0 and in_flight == 0:
                break
            if consecutive_empty >= 5 and visible == 0:
                print("  Waiting for in-flight messages to become visible...")
                time.sleep(10)
                continue
            time.sleep(2)
            continue

        consecutive_empty = 0
        totals["processed"] += len(messages)

        retried_ids = []
        dead_ids = []
        for msg in messages:
            cat = msg.get("failure_category", "UNKNOWN")
            mid = msg.get("message_id", "?")[:12]

            if cat in TRANSIENT:
                retried_ids.append(f"{mid}({cat})")
                totals["retried"] += 1
            else:
                dead_ids.append(f"{mid}({cat})")
                totals["dead"] += 1
                receipt = msg.get("receipt_handle")
                if receipt:
                    try:
                        requests.delete(
                            f"{API_URL}/messages/{msg['message_id']}",
                            json={"receipt_handle": receipt},
                            timeout=10,
                        )
                    except requests.RequestException:
                        pass

        visible, in_flight = get_queue_depth(sqs, dlq_url)
        print(f"  Poll #{poll_num:>3d}  |  {len(messages)} messages  |  "
              f"queue: {visible} visible, {in_flight} in-flight")
        if retried_ids:
            print(f"           retried: {', '.join(retried_ids)}")
        if dead_ids:
            print(f"           dead:    {', '.join(dead_ids)}")

        time.sleep(1)

    if time.monotonic() >= deadline:
        visible, in_flight = get_queue_depth(sqs, dlq_url)
        print(f"  WARNING: timed out after {timeout}s "
              f"({visible} visible, {in_flight} in-flight remain)")

    return totals


def fetch_stats() -> dict:
    resp = requests.get(f"{API_URL}/stats", timeout=10)
    resp.raise_for_status()
    return resp.json()


def print_summary(sent: int, totals: dict, api_stats: dict, elapsed: float) -> None:
    processed = totals["processed"]
    throughput = processed / elapsed if elapsed > 0 else 0

    print()
    print("=" * 60)
    print(f"  {'DLQ Monitor Load Test Results':^56}")
    print("=" * 60)
    print(f"  {'Messages Sent':<34} {sent:>10}")
    print(f"  {'Messages Processed':<34} {processed:>10}")
    print(f"  {'  Retried (transient)':<34} {totals['retried']:>10}")
    print(f"  {'  Dead-Lettered (permanent)':<34} {totals['dead']:>10}")
    print(f"  {'Poll Cycles':<34} {totals['polls']:>10}")
    print(f"  {'  Empty Polls':<34} {totals['empty_polls']:>10}")
    print("-" * 60)
    print(f"  {'Time Elapsed':<34} {elapsed:>9.1f}s")
    print(f"  {'Throughput':<34} {throughput:>8.1f} msg/s")
    print("-" * 60)
    print(f"  {'API /stats (for reference)':^56}")
    print(f"  {'  processed':<34} {api_stats.get('messages_processed', '?'):>10}")
    print(f"  {'  retried':<34} {api_stats.get('messages_retried', '?'):>10}")
    print(f"  {'  dead':<34} {api_stats.get('messages_dead', '?'):>10}")
    print(f"  {'  alerts':<34} {api_stats.get('alerts_sent', '?'):>10}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="DLQ Monitor load test")
    parser.add_argument(
        "--count", type=int, default=500,
        help="Number of messages to send (default: 500)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Send only 10 messages to verify the script works",
    )
    args = parser.parse_args()

    count = 10 if args.dry_run else args.count

    print("Resolving DLQ URL from CloudFormation...")
    dlq_url = get_dlq_url()
    print(f"  DLQ: {dlq_url}")

    sqs = boto3.client("sqs", region_name="us-east-1")

    print("\nDisabling background poller...")
    set_eventbridge_rule(enabled=False)

    try:
        print(f"\nBuilding {count} messages across 4 failure categories...")
        batches = build_messages(count)

        print(f"\nSending {count} messages in {len(batches)} batches...")
        t_start = time.monotonic()
        sent = send_all(sqs, dlq_url, batches)
        send_elapsed = time.monotonic() - t_start
        print(f"  Sent {sent} messages in {send_elapsed:.1f}s")

        visible, in_flight = get_queue_depth(sqs, dlq_url)
        print(f"  Queue: {visible} visible, {in_flight} in-flight")

        print("\nProcessing (GET /messages?retry=true)...")
        totals = poll_and_process(sqs, dlq_url)
        t_end = time.monotonic()

        api_stats = fetch_stats()
        elapsed = t_end - t_start

        print_summary(sent, totals, api_stats, elapsed)
    finally:
        print("\nRe-enabling background poller...")
        set_eventbridge_rule(enabled=True)


if __name__ == "__main__":
    main()
