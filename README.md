# Customer Support AI Agent

An AI agent that handles customer support for an online store: it tracks orders, cancels them, issues refunds, and hands off to a human, deciding each step on its own. Built from scratch in Python, no agent framework.

## What it does

- **Understands the request** in English or Arabic, and replies in the same language.
- **Picks its own tools**: looks up the customer, lists their orders, tracks shipments, cancels, refunds, or opens a ticket.
- **Chains steps** until it has the answer, with no fixed script.
- **Asks a human** before any refund over 500 EGP.
- **Knows who it's talking to.** Customers log in, and only ever reach their own orders.

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
customer  ->  agent loop  <->  model (Claude, Gemini or a local Ollama model)
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

A FastAPI server with login. Run `python -m uvicorn api:app` and open `/docs`.

| Endpoint | Who | What |
| --- | --- | --- |
| `POST /api/v1/auth/register` | anyone | Open a store account |
| `POST /api/v1/auth/login` | anyone | Get a token |
| `GET /api/v1/auth/me` | logged in | Who the token belongs to |
| `POST /api/v1/conversations` | customer | Start a conversation |
| `POST /api/v1/conversations/{id}/messages` | its owner | Talk to the agent |
| `GET /api/v1/conversations/{id}/messages` | owner, supervisor | Read the history |
| `POST /api/v1/conversations/{id}/approvals/{approval_id}` | supervisor | Approve or reject a big refund |

```bash
# Log in, then send the token with every call
curl -X POST localhost:8000/api/v1/auth/login -H "Content-Type: application/json" \
  -d '{"email": "yasmin@example.com", "password": "12345678"}'
# {"access_token": "eyJhbGci...", "token_type": "bearer"}

# Ask for a refund over 500 EGP: it waits for a supervisor, the chat goes on
curl -X POST localhost:8000/api/v1/conversations/conv_a2de8f7978bb/messages \
  -H "Authorization: Bearer eyJhbGci..." -H "Content-Type: application/json" \
  -d '{"text": "I want a refund for my coffee maker, order 1003"}'
# {"status": "pending_approval", "approval": {"id": "ap_1b41382a0fad", "order_id": 1003, "amount": 1800}, ...}
```

The supervisor then logs in and approves it; the agent tells the customer.
Test accounts: `mohamed`, `mona`, `mahmoud`, `yasmin`, `karim` `@example.com`, password `12345678`. The supervisor's login lives in `.env`.

**Safe by design**

- The customer id comes from the token, never from the request or the model. Talk the agent into "I'm customer 4" and every tool still runs as you.
- Someone else's conversation looks exactly like one that doesn't exist (404).
- Passwords are stored as argon2 hashes; tokens are signed and expire after 30 minutes.

`401` no or bad token · `403` wrong role · `404` not found, or not yours · `409` already taken or decided · `422` invalid body · `503` model down
