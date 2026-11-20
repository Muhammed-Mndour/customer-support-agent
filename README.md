# Customer Support AI Agent

An AI agent that handles customer support for an online store: it tracks orders, cancels them, issues refunds, and hands off to a human, deciding each step on its own. Built from scratch in Python, no agent framework.

## What it does

- **Understands the request** in English or Arabic, and replies in the same language.
- **Picks its own tools**: looks up the customer, lists their orders, tracks shipments, cancels, refunds, or opens a ticket.
- **Chains steps** until it has the answer, with no fixed script.
- **Asks a human** before any refund over 500 EGP.

## Example

```text
Customer: Why is my order late? My number is 01012345678

  step 1  get_customer_by_phone    -> Mohamed Ali (customer 1)
  step 2  get_orders_for_customer  -> order 1004, Desk Lamp, delayed
  step 3  track_shipment           -> stock delay, new date Nov 13

Agent: Your Desk Lamp (order 1004) is delayed because of a stock
       shortage at the warehouse. It should arrive on November 13.
```

## How it works

```text
customer  ->  agent loop  <->  model (Claude or a local Ollama model)
                  |
                  v
                tools  ->  database
                  |
                  +-> refund over 500 EGP: waits for approve / reject
```

- **The model decides, the code executes.** The model only asks for a tool; the loop runs it and sends the result back, until the model answers.
- **Rules live in code, not in the prompt.** A customer can only touch their own orders, shipped orders can't be cancelled, nothing is refunded twice. However the model is persuaded, the tools refuse.
- **A human approves big refunds.** The approval comes from the code running the loop, so the model can't approve itself.

## HTTP API

A FastAPI server puts the agent behind four endpoints. Run it with `python -m uvicorn api:app`; interactive docs are at `/docs`.

| Endpoint | What it does |
| --- | --- |
| `POST /api/v1/conversations` | Start a conversation (201) |
| `POST /api/v1/conversations/{id}/messages` | Send a customer message, get the agent's reply |
| `GET /api/v1/conversations/{id}/messages?limit=50` | Read the conversation as the customer saw it |
| `POST /api/v1/conversations/{id}/approvals/{approval_id}` | A supervisor approves or rejects a big refund |

```bash
# Start a conversation
curl -X POST localhost:8000/api/v1/conversations
# {"id": "conv_a2de8f7978bb"}

# Ask for a refund over 500 EGP: recorded for a supervisor, the chat goes on
curl -X POST localhost:8000/api/v1/conversations/conv_a2de8f7978bb/messages \
  -H "Content-Type: application/json" \
  -d '{"customer_id": 4, "text": "I want a refund for my coffee maker, order 1003"}'
# {"status": "pending_approval", "reply": "Your refund ... was sent to a supervisor ...",
#  "approval": {"id": "ap_1b41382a0fad", "action": "refund", "order_id": 1003, "amount": 1800}}

# The supervisor decides; the agent tells the customer
curl -X POST localhost:8000/api/v1/conversations/conv_a2de8f7978bb/approvals/ap_1b41382a0fad \
  -H "Content-Type: application/json" \
  -d '{"decision": "approve"}'
# {"status": "done", "reply": "Your refund of 1800 EGP has been approved ..."}
```

Unknown conversation or approval: 404. Deciding twice: 409. Invalid body: 422. Model unreachable: 503.
