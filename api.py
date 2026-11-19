"""HTTP API for the support agent (Block 1).

Run:  python -m uvicorn api:app --reload
Docs: http://localhost:8000/docs
"""

import uuid
from dataclasses import dataclass, field

from fastapi import FastAPI, status
from pydantic import BaseModel

app = FastAPI(title="Customer Support Agent API")


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


# --- Endpoints ---------------------------------------------------------------------

@app.post(
    "/api/v1/conversations",
    status_code=status.HTTP_201_CREATED,
    response_model=ConversationCreated,
)
def create_conversation():
    conversation = conversations.create()
    return ConversationCreated(id=conversation.id)
