"""Call every tool directly, with no model involved, and check what comes back.

Each check states what we expect: "ok", "error" or "approval_required".
The database is reset before and after, so the tests always start from the seed data.
"""

import json
import sys

from db import create_database, print_table
from tools import (
    cancel_order,
    escalate_to_human,
    get_customer_by_phone,
    get_orders_for_customer,
    refund_order,
    run_tool,
    track_shipment,
)

results = []


def outcome(result):
    if "error" in result:
        return "error"
    if result.get("status") in ("approval_required", "rejected", "pending_approval"):
        return result["status"]
    return "ok"


def supervisor_says(answer):
    """A fake supervisor for the tests: answers yes or no without asking anyone."""
    asked = []

    def approve_refund(details):
        asked.append(details)
        return answer

    approve_refund.asked = asked
    return approve_refund


def check(label, result, expected):
    passed = outcome(result) == expected
    # Refund results must say plainly whether money moved: true only when refunded.
    if "refunded" in result and result["refunded"] != (result.get("status") == "refunded"):
        passed = False
    results.append(passed)
    print(f"\n[{'PASS' if passed else 'FAIL'}] {label}  (expected: {expected})")
    print("   " + json.dumps(result, ensure_ascii=False))


def section(title):
    print(f"\n\n========== {title} ==========")


create_database()

section("get_customer_by_phone")
check("Known phone", get_customer_by_phone("01012345678"), "ok")
check("Same phone, written with +20 and spaces", get_customer_by_phone("+20 101 234 5678"), "ok")
check("Unknown phone", get_customer_by_phone("01000000000"), "error")

section("get_orders_for_customer")
check("Mohamed's orders (customer 1)", get_orders_for_customer(1), "ok")
check("Customer that doesn't exist", get_orders_for_customer(99), "error")

section("track_shipment")
check("Delivered order 1001", track_shipment(1, 1001), "ok")
check("Shipped order 1007", track_shipment(3, 1007), "ok")
check("Delayed order 1004", track_shipment(1, 1004), "ok")
check("Cancelled order 1009", track_shipment(3, 1009), "ok")
check("Mohamed asks about Mona's order 1005", track_shipment(1, 1005), "error")

section("cancel_order")
check("Cancel delayed order 1004", cancel_order(1, 1004), "ok")
check("Cancel 1004 again", cancel_order(1, 1004), "error")
check("Cancel shipped order 1007", cancel_order(3, 1007), "error")
check("Cancel delivered order 1001", cancel_order(1, 1001), "error")
check("Mohamed cancels Mona's order 1005", cancel_order(1, 1005), "error")

section("refund_order")
check("Refund delivered order 1002 (450 EGP)", refund_order(2, 1002), "ok")
check("Refund 1002 again", refund_order(2, 1002), "error")
check("Refund cancelled order 1010 (120 EGP)", refund_order(5, 1010), "ok")
check("Refund 1001 (850 EGP), no supervisor available", refund_order(1, 1001), "approval_required")
no = supervisor_says(False)
check("Refund 1001 (850 EGP), supervisor rejects", refund_order(1, 1001, approve_refund=no), "rejected")
check("  ...and the supervisor saw the right details",
      {"error": "wrong details"} if no.asked != [{"order_id": 1001, "customer": "Mohamed Ali",
                                                   "item": "Wireless Earbuds", "amount_egp": 850}] else {}, "ok")
yes = supervisor_says(True)
check("Refund 1004 (650 EGP, cancelled above), supervisor approves",
      refund_order(1, 1004, approve_refund=yes), "ok")
check("Refund 1004 again", refund_order(1, 1004, approve_refund=yes), "error")
small = supervisor_says(True)
check("Refund cancelled order 1009 (350 EGP) with a supervisor on hand",
      refund_order(3, 1009, approve_refund=small), "ok")
check("  ...and nobody was asked, because it is under the limit",
      {"error": "supervisor was asked"} if small.asked else {}, "ok")
recorded = []
check("Refund 1003 (1800 EGP) with request_approval: recorded, not refunded",
      refund_order(4, 1003, request_approval=lambda details: recorded.append(details) or "ap_test"),
      "pending_approval")
check("  ...the request was recorded once, with the right details",
      {} if recorded == [{"order_id": 1003, "customer": "Yasmin Khaled",
                          "item": "Coffee Maker", "amount_egp": 1800}] else {"error": "wrong"}, "ok")
order_1003 = next(o for o in get_orders_for_customer(4)["orders"] if o["order_id"] == 1003)
check("  ...and no money moved yet", {"error": "refunded"} if order_1003["refunded"] else {}, "ok")
check("Refund delayed order 1006 (not cancelled yet)", refund_order(4, 1006), "error")
check("Refund shipped order 1008", refund_order(5, 1008), "error")

section("escalate_to_human")
check("Ticket for a known customer", escalate_to_human("Air fryer arrived broken", 2), "ok")
check("Ticket without a customer", escalate_to_human("Caller refused to give a phone number"), "ok")
check("Empty reason", escalate_to_human("   "), "error")
check("Customer that doesn't exist", escalate_to_human("Test", 99), "error")

section("run_tool (what the agent loop will call)")
check("Known tool by name", run_tool("track_shipment", {"customer_id": 5, "order_id": 1008}), "ok")
check("Unknown tool name", run_tool("delete_everything", {}), "error")
check("Wrong arguments", run_tool("cancel_order", {"order": 1004}), "error")
check("refund_order through run_tool gets the supervisor",
      run_tool("refund_order", {"customer_id": 4, "order_id": 1003}, approve_refund=supervisor_says(False)),
      "rejected")
check("The model can't pass its own approval",
      run_tool("refund_order", {"customer_id": 4, "order_id": 1003, "approve_refund": True},
               approve_refund=supervisor_says(False)), "error")

section("run_tool with a known customer (HTTP API: customer_id from the login token)")
# Mona (customer 2) is logged in; the model was talked into using Yasmin's id (4).
leaked = run_tool("get_orders_for_customer", {"customer_id": 4}, customer_id=2)
check("Model asks for customer 4's orders: gets Mona's (customer 2) instead",
      {} if leaked.get("customer_id") == 2 and {o["order_id"] for o in leaked["orders"]} == {1002, 1005}
      else {"error": f"wrong orders: {leaked}"}, "ok")
check("Model tracks Yasmin's order 1006 as customer 4: not found for Mona",
      run_tool("track_shipment", {"customer_id": 4, "order_id": 1006}, customer_id=2), "error")
check("Model refunds Yasmin's order 1006 (400 EGP) as customer 4: not found for Mona",
      run_tool("refund_order", {"customer_id": 4, "order_id": 1006}, customer_id=2), "error")
order_1006 = next(o for o in get_orders_for_customer(4)["orders"] if o["order_id"] == 1006)
check("  ...and Yasmin's order 1006 was not refunded",
      {"error": "refunded"} if order_1006["refunded"] else {}, "ok")
check("Model looks up another customer by phone: refused",
      run_tool("get_customer_by_phone", {"phone": "01234567890"}, customer_id=2), "error")
inp = {"customer_id": 4, "order_id": 1005}
check("Mona's own order 1005 still works, even if the model wrote 4",
      run_tool("track_shipment", inp, customer_id=2), "ok")
check("  ...and the model's own request was left as it wrote it",
      {} if inp == {"customer_id": 4, "order_id": 1005} else {"error": "changed"}, "ok")
check("Without customer_id (terminal, web page) the phone lookup still works",
      run_tool("get_customer_by_phone", {"phone": "01098765432"}), "ok")

section("Database after the tests")
print_table(
    "orders that changed",
    "SELECT id, status, refunded FROM orders WHERE id IN (1001, 1002, 1003, 1004, 1010)",
)
print_table("tickets", "SELECT * FROM tickets")

create_database()  # leave store.db clean for the next stage

passed = sum(results)
print(f"\n\n{passed}/{len(results)} checks passed. store.db was reset to the seed data.")
sys.exit(0 if passed == len(results) else 1)
