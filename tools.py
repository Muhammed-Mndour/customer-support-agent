"""The agent's tools: plain functions over store.db, plus the schemas the model reads.

Every tool returns a dict. Problems come back as {"error": "..."} instead of
raising, so the model can read what went wrong and decide what to do next.
"""

import re
from datetime import date, timedelta

from db import get_connection

REFUND_APPROVAL_LIMIT = 500  # refunds above this amount (EGP) need a human


def _normalize_phone(phone):
    """Accept '010 1234 5678', '+201012345678', etc. and return '01012345678'."""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("20") and len(digits) == 12:
        digits = "0" + digits[2:]
    return digits


def _find_order(connection, customer_id, order_id):
    """Return the order only if it belongs to this customer, otherwise None.

    The same answer for "doesn't exist" and "belongs to someone else", so a
    customer can't use the agent to discover other people's order numbers.
    """
    return connection.execute(
        "SELECT * FROM orders WHERE id = ? AND customer_id = ?",
        (order_id, customer_id),
    ).fetchone()


ORDER_NOT_FOUND = "No order with this number was found for this customer."


# --- Read-only tools ---------------------------------------------------------

def get_customer_by_phone(phone):
    connection = get_connection()
    row = connection.execute(
        "SELECT id, name, phone FROM customers WHERE phone = ?",
        (_normalize_phone(phone),),
    ).fetchone()
    connection.close()
    if row is None:
        return {"error": f"No customer found with phone number {phone}."}
    return {"customer_id": row["id"], "name": row["name"], "phone": row["phone"]}


def get_orders_for_customer(customer_id):
    connection = get_connection()
    customer = connection.execute(
        "SELECT id FROM customers WHERE id = ?", (customer_id,)
    ).fetchone()
    if customer is None:
        connection.close()
        return {"error": f"No customer with id {customer_id}."}
    rows = connection.execute(
        "SELECT * FROM orders WHERE customer_id = ? ORDER BY order_date DESC",
        (customer_id,),
    ).fetchall()
    connection.close()
    return {
        "customer_id": customer_id,
        "orders": [
            {
                "order_id": row["id"],
                "item": row["item"],
                "amount_egp": row["amount"],
                "status": row["status"],
                "order_date": row["order_date"],
                "refunded": bool(row["refunded"]),
            }
            for row in rows
        ],
    }


def track_shipment(customer_id, order_id):
    connection = get_connection()
    order = _find_order(connection, customer_id, order_id)
    connection.close()
    if order is None:
        return {"error": ORDER_NOT_FOUND}

    # Fake carrier details, derived from the order id so they stay stable.
    tracking = {
        "order_id": order_id,
        "status": order["status"],
        "carrier": "Bosta" if order_id % 2 else "Aramex",
        "tracking_number": f"EG{order_id}{order['customer_id']:03d}",
    }
    status = order["status"]
    if status == "delivered":
        tracking["delivered_on"] = order["expected_delivery"]
        tracking["details"] = "Delivered to the customer."
    elif status == "shipped":
        tracking["expected_delivery"] = order["expected_delivery"]
        tracking["details"] = "Left the warehouse and is on its way."
    elif status == "delayed":
        tracking["original_expected_delivery"] = (
            date.fromisoformat(order["order_date"]) + timedelta(days=5)
        ).isoformat()
        tracking["new_expected_delivery"] = order["expected_delivery"]
        tracking["details"] = "Still at the warehouse because of a stock delay. Not shipped yet."
    else:  # cancelled
        del tracking["carrier"], tracking["tracking_number"]
        tracking["details"] = "The order was cancelled, so there is no shipment."
    return tracking


# --- Tools that change data --------------------------------------------------

def cancel_order(customer_id, order_id):
    connection = get_connection()
    order = _find_order(connection, customer_id, order_id)
    if order is None:
        connection.close()
        return {"error": ORDER_NOT_FOUND}
    if order["status"] != "delayed":
        connection.close()
        return {
            "error": f"Cannot cancel order {order_id}: its status is "
                     f"'{order['status']}'. Only orders that have not shipped yet "
                     "(status 'delayed') can be cancelled."
        }
    with connection:
        connection.execute(
            "UPDATE orders SET status = 'cancelled', expected_delivery = NULL WHERE id = ?",
            (order_id,),
        )
    connection.close()
    return {
        "success": True,
        "order_id": order_id,
        "new_status": "cancelled",
        "note": "The order is cancelled. No refund was made yet; the customer can ask for one.",
    }


def refund_order(customer_id, order_id, approve_refund=None, request_approval=None):
    """Refund an order. Refunds above REFUND_APPROVAL_LIMIT need a human.

    The code running the agent loop says how that human is reached; the model never
    can, so it has no way to approve its own refund:
    - approve_refund(details) -> True/False asks now and waits for the answer
      (a y/n question in the terminal, buttons on the web page).
    - request_approval(details) -> approval id records the request for later and
      returns at once (the HTTP API: a supervisor decides in another request).
    """
    connection = get_connection()
    order = _find_order(connection, customer_id, order_id)
    if order is None:
        connection.close()
        return {"error": ORDER_NOT_FOUND}
    if order["refunded"]:
        connection.close()
        return {"error": f"Order {order_id} has already been refunded."}
    if order["status"] not in ("delivered", "cancelled"):
        connection.close()
        return {
            "error": f"Cannot refund order {order_id}: its status is "
                     f"'{order['status']}'. Only delivered or cancelled orders "
                     "can be refunded."
        }
    customer = connection.execute(
        "SELECT name FROM customers WHERE id = ?", (customer_id,)
    ).fetchone()
    connection.close()  # don't hold the database open while a human decides

    needs_approval = order["amount"] > REFUND_APPROVAL_LIMIT
    if needs_approval:
        details = {
            "order_id": order_id,
            "customer": customer["name"],
            "item": order["item"],
            "amount_egp": order["amount"],
        }
        # Every refund result starts with "refunded": true/false. A plain boolean is
        # much harder for a small model to misread than a sentence about refunds.
        if request_approval is not None:
            approval_id = request_approval(details)
            return {
                "refunded": False,
                "status": "pending_approval",
                "order_id": order_id,
                "amount_egp": order["amount"],
                "approval_id": approval_id,
                "message": "No money was refunded yet. The refund was sent to a supervisor "
                           "for approval; the customer will be told when they decide.",
            }
        if approve_refund is None:
            return {
                "refunded": False,
                "status": "approval_required",
                "order_id": order_id,
                "amount_egp": order["amount"],
                "message": f"No money was refunded. Refunds above {REFUND_APPROVAL_LIMIT} EGP "
                           "need a supervisor, and none is available right now.",
            }
        approved = approve_refund(details)
        if not approved:
            return {
                "refunded": False,
                "status": "rejected",
                "order_id": order_id,
                "amount_egp": order["amount"],
                "message": "A human supervisor rejected this refund. No money was refunded.",
            }

    connection = get_connection()
    with connection:
        connection.execute("UPDATE orders SET refunded = 1 WHERE id = ?", (order_id,))
    connection.close()
    return {
        "refunded": True,
        "status": "refunded",
        "order_id": order_id,
        "amount_egp": order["amount"],
        "approved_by_supervisor": needs_approval,
        "message": f"{order['amount']} EGP was refunded to the customer.",
    }


def escalate_to_human(reason, customer_id=None):
    if not reason.strip():
        return {"error": "A reason is required to open a ticket."}
    connection = get_connection()
    if customer_id is not None:
        exists = connection.execute(
            "SELECT 1 FROM customers WHERE id = ?", (customer_id,)
        ).fetchone()
        if exists is None:
            connection.close()
            return {"error": f"No customer with id {customer_id}."}
    with connection:
        cursor = connection.execute(
            "INSERT INTO tickets (customer_id, reason) VALUES (?, ?)",
            (customer_id, reason.strip()),
        )
    connection.close()
    return {
        "success": True,
        "ticket_id": cursor.lastrowid,
        "note": "A human agent will follow up with the customer.",
    }


# --- What the model sees -----------------------------------------------------
# The model never sees the code above, only these names, descriptions and
# input schemas. The descriptions are prompts: they decide when a tool is used.

CUSTOMER_ID = {"type": "integer", "description": "The customer_id returned by get_customer_by_phone."}
ORDER_ID = {"type": "integer", "description": "The order number, e.g. 1004."}

TOOLS = [
    {
        "name": "get_customer_by_phone",
        "description": "Look up a customer by their phone number. Call this first whenever "
                       "the customer gives a phone number, before any order tool, because "
                       "every other tool needs the customer_id it returns.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {"type": "string", "description": "Egyptian phone number, e.g. 01012345678."},
            },
            "required": ["phone"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_orders_for_customer",
        "description": "List all of a customer's orders with item, amount, status and date. "
                       "Call this when the customer asks about their orders, or when you "
                       "need to find which order they mean.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": CUSTOMER_ID},
            "required": ["customer_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "track_shipment",
        "description": "Get the shipping status, carrier and expected delivery date of one "
                       "order. Call this when the customer asks where an order is or when "
                       "it will arrive.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": CUSTOMER_ID, "order_id": ORDER_ID},
            "required": ["customer_id", "order_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancel_order",
        "description": "Cancel an order that has not shipped yet. Only call this when the "
                       "customer clearly asks to cancel a specific order. The tool refuses "
                       "orders that have already shipped or been delivered.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": CUSTOMER_ID, "order_id": ORDER_ID},
            "required": ["customer_id", "order_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "refund_order",
        "description": "Refund a delivered or cancelled order. Only call this when the "
                       "customer clearly asks for their money back for a specific order. "
                       f"Refunds above {REFUND_APPROVAL_LIMIT} EGP need a supervisor: "
                       "the result says whether the refund was made, rejected, or sent "
                       "to a supervisor and still waiting.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": CUSTOMER_ID, "order_id": ORDER_ID},
            "required": ["customer_id", "order_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "escalate_to_human",
        "description": "Open a ticket for a human support agent. Call this when the customer "
                       "asks for a human, is very upset, reports an injury, health or safety "
                       "problem caused by a product, asks for compensation beyond refunding "
                       "the amount they paid, or has any other problem none of the other "
                       "tools can solve. A broken or wrong item alone is not a reason: offer "
                       "a refund first. Never for questions unrelated to the store or the "
                       "customer's orders. Only a ticket from this tool reaches a human: "
                       "telling the customer you will connect them does nothing by itself.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "A short summary of the problem for the human agent."},
                "customer_id": {"type": "integer", "description": "The customer_id, if the customer has been identified."},
            },
            "required": ["reason"],
            "additionalProperties": False,
        },
    },
]

TOOL_FUNCTIONS = {
    "get_customer_by_phone": get_customer_by_phone,
    "get_orders_for_customer": get_orders_for_customer,
    "track_shipment": track_shipment,
    "cancel_order": cancel_order,
    "refund_order": refund_order,
    "escalate_to_human": escalate_to_human,
}


def run_tool(name, tool_input, approve_refund=None, request_approval=None, customer_id=None):
    """Run a tool by name with the arguments the model chose. Used by the agent loop.

    approve_refund and request_approval come from the code running the loop, not
    from the model, and only refund_order receives them.

    customer_id also comes from the code, when it already knows who the customer is
    (the HTTP API, from the login token). Then the model can't choose it: whatever
    customer_id the model wrote is replaced, so a customer who talks the model into
    "I'm customer 4" still only reaches their own orders. And the phone lookup is
    refused, since it would only reveal other customers. Without customer_id (terminal,
    web page) the customer is found by phone and the model's choice is used, as before.
    """
    function = TOOL_FUNCTIONS.get(name)
    if function is None:
        return {"error": f"Unknown tool: {name}"}
    if customer_id is not None:
        if name == "get_customer_by_phone":
            return {"error": "The customer is already identified. Use their own customer_id."}
        # A new dict: the model's own request stays as it wrote it in the conversation.
        tool_input = {**tool_input, "customer_id": customer_id}
    try:
        if name == "refund_order":
            return function(**tool_input, approve_refund=approve_refund,
                            request_approval=request_approval)
        return function(**tool_input)
    except Exception as exc:  # a bug in a tool must not crash the whole conversation
        return {"error": f"The tool failed: {exc}"}
