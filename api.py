"""HTTP API for the support agent (Block 1).

Run:  python -m uvicorn api:app --reload
Docs: http://localhost:8000/docs
"""

import re
import uuid
from dataclasses import dataclass, field
from typing import Literal

import anthropic
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field

import agent  # importing it also loads .env (model, base URL, key)
import tools

app = FastAPI(title="Customer Support Agent API")

# One connection to the model for the whole app, like the repositories below.
model_client = anthropic.Anthropic()


# --- Storage ---------------------------------------------------------------------

@dataclass
class Conversation:
    id: str
    messages: list = field(default_factory=list)  # in the agent's format, sent to the model


class ConversationRepository:
    """Keeps conversations in memory, so they are lost when the server restarts.

    The endpoints only talk to this class, so Block 3 can store conversations in
    Postgres by changing this class alone.
    """

    def __init__(self):
        self._conversations = {}

    def create(self):
        conversation = Conversation(id=f"conv_{uuid.uuid4().hex[:12]}")
        self._conversations[conversation.id] = conversation
        return conversation

    def get(self, conversation_id):
        """Return the conversation, or None if there is no conversation with this id."""
        return self._conversations.get(conversation_id)


@dataclass
class Approval:
    """A refund over the limit, waiting for a supervisor."""
    id: str
    conversation_id: str
    customer_id: int
    order_id: int
    amount: int
    status: Literal["pending", "approved", "rejected"] = "pending"


class ApprovalRepository:
    """Keeps approval requests in memory, apart from conversations, so a supervisor
    screen can later list every pending request across all conversations."""

    def __init__(self):
        self._approvals = {}

    def create(self, conversation_id, customer_id, order_id, amount):
        approval = Approval(id=f"ap_{uuid.uuid4().hex[:12]}", conversation_id=conversation_id,
                            customer_id=customer_id, order_id=order_id, amount=amount)
        self._approvals[approval.id] = approval
        return approval

    def get(self, approval_id):
        """Return the approval, or None if there is no approval with this id."""
        return self._approvals.get(approval_id)

    def delete(self, approval_id):
        self._approvals.pop(approval_id, None)


# One of each for the whole app: every request must see the same data.
conversations = ConversationRepository()
approvals = ApprovalRepository()


# --- Request and response models -------------------------------------------------

class ConversationCreated(BaseModel):
    id: str


class MessageIn(BaseModel):
    customer_id: int
    text: str = Field(min_length=1)


class ApprovalOut(BaseModel):
    id: str
    action: Literal["refund"] = "refund"
    order_id: int
    amount: int


class MessageReply(BaseModel):
    status: Literal["done", "failed", "pending_approval"]
    reply: str
    approval: ApprovalOut | None = None  # only when status is "pending_approval"


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]


class HistoryMessage(BaseModel):
    role: Literal["customer", "agent"]
    text: str


class ConversationHistory(BaseModel):
    conversation_id: str
    messages: list[HistoryMessage]


# --- Internal messages and what the customer saw ---------------------------------------

CUSTOMER_TAG = "[customer_id: {}]\n"                       # added to each customer message
CUSTOMER_TAG_PATTERN = re.compile(r"^\[customer_id: \d+\]\n")   # and removed for the history
DECISION_NOTE = "[supervisor decision on {}: {}]"              # written by the system, not the customer


def _field(block, name):
    """Blocks from the model are SDK objects; blocks the agent built itself are dicts."""
    return block.get(name) if isinstance(block, dict) else getattr(block, name, None)


def visible_messages(messages):
    """Turn the agent's message list into what the customer and the agent said to each
    other: drop tool calls, tool results, thinking, supervisor notes and the customer_id tag."""
    visible = []
    for message in messages:
        content = message["content"]
        if message["role"] == "user":
            # A customer message is a string; tool results are lists. Supervisor notes are
            # strings too, but the system wrote them, so the customer never saw them.
            if isinstance(content, str) and not content.startswith("[supervisor decision"):
                visible.append(HistoryMessage(role="customer",
                                              text=CUSTOMER_TAG_PATTERN.sub("", content)))
            continue
        if any(_field(block, "type") == "tool_use" for block in content):
            continue  # the agent asking for a tool, not talking to the customer
        text = "\n".join(_field(block, "text") for block in content
                         if _field(block, "type") == "text")
        if text:
            visible.append(HistoryMessage(role="agent", text=text))
    return visible


def run_agent(messages, request_approval=None):
    """Run the agent on a conversation. On failure, undo what this turn added, so the
    conversation stays valid. Returns (status, reply)."""
    turn_start = len(messages) - 1  # the last message is the one that started this turn
    try:
        reply = agent.run_agent_turn(
            model_client,
            messages,
            on_event=lambda kind, data: None,  # nobody watches the steps live
            request_approval=request_approval,
        )
        return "done", reply
    except agent.TurnFailed as exc:
        # The agent couldn't finish (step limit, refusal, cut-off reply): pass on its
        # message for the customer.
        del messages[turn_start:]
        return "failed", str(exc)
    except (anthropic.APIStatusError, anthropic.APIConnectionError):
        del messages[turn_start:]
        return "unavailable", None


# --- Endpoints ---------------------------------------------------------------------

@app.post(
    "/api/v1/conversations",
    status_code=status.HTTP_201_CREATED,
    response_model=ConversationCreated,
)
def create_conversation():
    conversation = conversations.create()
    return ConversationCreated(id=conversation.id)


@app.post(
    "/api/v1/conversations/{conversation_id}/messages",
    response_model=MessageReply,
)
def send_message(conversation_id: str, message: MessageIn):
    conversation = conversations.get(conversation_id)
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")

    # Big refunds are recorded as approval requests instead of waiting for a person.
    created = []

    def request_approval(details):
        approval = approvals.create(conversation.id, message.customer_id,
                                    details["order_id"], details["amount_egp"])
        created.append(approval)
        return approval.id

    # The agent loop adds its tool calls and answer to this same list, and the list
    # lives inside the stored conversation, so they are saved without another call.
    # Tell the model who is writing, so it doesn't have to ask for a phone number.
    # The caller can claim any customer_id for now; Block 2 takes it from the login token.
    conversation.messages.append({"role": "user",
                                  "content": CUSTOMER_TAG.format(message.customer_id) + message.text})
    result, reply = run_agent(conversation.messages, request_approval)

    if result != "done":
        for approval in created:  # the turn was undone, so its requests go too
            approvals.delete(approval.id)
    if result == "unavailable":
        # The model itself couldn't be reached: our dependency is down, not the request.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The model is not available right now")
    if result == "failed":
        return MessageReply(status="failed", reply=reply)

    if created:
        approval = created[-1]
        return MessageReply(status="pending_approval", reply=reply,
                            approval=ApprovalOut(id=approval.id, order_id=approval.order_id,
                                                 amount=approval.amount))
    return MessageReply(status="done", reply=reply)


@app.get(
    "/api/v1/conversations/{conversation_id}/messages",
    response_model=ConversationHistory,
)
def get_messages(conversation_id: str, limit: int = Query(50, ge=1, le=200)):
    conversation = conversations.get(conversation_id)
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return ConversationHistory(
        conversation_id=conversation.id,
        messages=visible_messages(conversation.messages)[-limit:],  # the latest ones
    )


@app.post(
    "/api/v1/conversations/{conversation_id}/approvals/{approval_id}",
    response_model=MessageReply,
)
def decide_approval(conversation_id: str, approval_id: str, body: DecisionIn):
    conversation = conversations.get(conversation_id)
    approval = approvals.get(approval_id)
    if conversation is None or approval is None or approval.conversation_id != conversation_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such approval request")
    if approval.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Already {approval.status}")

    # Carry out the decision before anything else, and record it, so a second request
    # gets 409 even if the agent fails below.
    if body.decision == "approve":
        approval.status = "approved"
        # The rules are checked again now: ownership, order state, not refunded twice.
        result = tools.refund_order(approval.customer_id, approval.order_id,
                                    approve_refund=lambda details: True)
        if result.get("refunded"):
            outcome = f"approved, {approval.amount} EGP refunded"
        else:
            outcome = f"approved, but the refund failed: {result.get('error', result.get('message'))}"
    else:
        approval.status = "rejected"
        outcome = "rejected, no money refunded"

    # The decision goes into the conversation as a note, then the agent tells the customer.
    note = DECISION_NOTE.format(approval.id, outcome)
    conversation.messages.append({"role": "user", "content": note})
    result, reply = run_agent(conversation.messages)

    if result != "done":
        # The decision stands even if the agent can't write about it: keep the note so the
        # agent knows later, and answer with the decision itself.
        conversation.messages.append({"role": "user", "content": note})
        return MessageReply(status="failed", reply=f"Supervisor decision: {outcome}.")
    return MessageReply(status="done", reply=reply)
