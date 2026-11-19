"""HTTP API for the support agent (Block 1).

Run:  python -m uvicorn api:app --reload
Docs: http://localhost:8000/docs
"""

import uuid
from dataclasses import dataclass, field
from typing import Literal

import anthropic
from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

import agent  # importing it also loads .env (model, base URL, key)

app = FastAPI(title="Customer Support Agent API")

# One connection to the model for the whole app, like the repository below.
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


# One repository for the whole app: every request must see the same conversations.
conversations = ConversationRepository()


# --- Request and response models -------------------------------------------------

class ConversationCreated(BaseModel):
    id: str


class MessageIn(BaseModel):
    customer_id: int
    text: str = Field(min_length=1)


class MessageReply(BaseModel):
    status: Literal["done", "failed"]
    reply: str


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

    # The agent loop adds its tool calls and answer to this same list, and the list
    # lives inside the stored conversation, so they are saved without another call.
    messages = conversation.messages
    turn_start = len(messages)
    # Tell the model who is writing, so it doesn't have to ask for a phone number.
    # The caller can claim any customer_id for now; Block 2 takes it from the login token.
    messages.append({"role": "user",
                     "content": f"[customer_id: {message.customer_id}]\n{message.text}"})

    try:
        reply = agent.run_agent_turn(
            model_client,
            messages,
            on_event=lambda kind, data: None,  # nobody watches the steps live
            approve_refund=None,               # big refunds are refused until the approvals endpoint exists
        )
    except agent.TurnFailed as exc:
        # The agent couldn't finish (step limit, refusal, cut-off reply). Undo this
        # turn so the conversation stays valid, and pass on its message for the customer.
        del messages[turn_start:]
        return MessageReply(status="failed", reply=str(exc))
    except (anthropic.APIStatusError, anthropic.APIConnectionError):
        # The model itself couldn't be reached: our dependency is down, not the request.
        del messages[turn_start:]
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The model is not available right now")

    return MessageReply(status="done", reply=reply)
