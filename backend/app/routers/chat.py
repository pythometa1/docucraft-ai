from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import ChatMessage, Conversation, SourceChunk, User
from app.ownership import owned_conversation, owned_project
from app.security import error, get_current_user
from app.llm.boundary import prepare_context
from app.tenancy import llm_policy_for
from app.llm.provider import get_llm_provider
from app.retrieval.lexical import retrieve

router = APIRouter(tags=["chat"])


class ConversationCreate(BaseModel):
    project_id: str | None = None
    title: str = "New conversation"


@router.post("/projects/{project_id}/conversations", status_code=201)
def create_conversation(project_id: str, body: ConversationCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    # Unchecked, this let one org create a conversation *inside another org's
    # project*: the row carried the caller's org_id, so it passed every later
    # filter while hanging off a project they cannot see.
    owned_project(db, project_id, user)
    c = Conversation(org_id=user.org_id, project_id=project_id, user_id=user.id, title=body.title)
    db.add(c)
    db.commit()
    db.refresh(c)
    return {"id": c.id, "title": c.title, "updated_at": c.updated_at}


@router.get("/projects/{project_id}/conversations")
def list_conversations(project_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    owned_project(db, project_id, user)
    rows = db.scalars(select(Conversation).where(Conversation.project_id == project_id, Conversation.org_id == user.org_id).order_by(Conversation.updated_at.desc())).all()
    return {"items": [{"id": c.id, "title": c.title, "updated_at": c.updated_at} for c in rows]}


@router.get("/conversations/{conversation_id}/messages")
def list_messages(conversation_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    # Had no org check at all -- any authenticated user could read any other
    # org's chat history, which is grounded in their uploaded source data.
    owned_conversation(db, conversation_id, user)
    rows = db.scalars(select(ChatMessage).where(ChatMessage.conversation_id == conversation_id).order_by(ChatMessage.created_at)).all()
    return {"items": [{"id": m.id, "role": m.role, "text": m.text, "sources": m.sources, "created_at": m.created_at} for m in rows]}


class MessageCreate(BaseModel):
    text: str


@router.post("/conversations/{conversation_id}/messages", status_code=201)
def send_message(conversation_id: str, body: MessageCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    conv = db.get(Conversation, conversation_id)
    if not conv or conv.org_id != user.org_id:
        raise error("CONVERSATION_NOT_FOUND", "Conversation not found", 404)

    user_msg = ChatMessage(conversation_id=conversation_id, org_id=conv.org_id, role="user", text=body.text)
    db.add(user_msg)

    chunks = db.scalars(select(SourceChunk).where(SourceChunk.project_id == conv.project_id)).all() if conv.project_id else []
    ranked = retrieve(chunks, body.text, k=6)
    policy = llm_policy_for(db, conv.org_id, project_id=conv.project_id,
                            user_id=user.id, subject_type="conversation",
                            subject_id=conv.id)
    llm = get_llm_provider("Chat", policy=policy)

    # §16 data minimisation. A source chunk is a flattened spreadsheet row --
    # "employee_id: EMP-000417; full_name: Dana Ruiz; annual_salary: 118400" --
    # and this path used to hand those to the provider verbatim. `prepare_context`
    # replaces every sensitive value with a type-preserving synthetic one, keeps
    # the column names (which are the schema, and the part the answer needs), and
    # raises rather than sending anything it cannot clear.
    prepared = prepare_context(
        org_id=conv.org_id,
        model=llm.model_name if hasattr(llm, "model_name") else settings.llm_model,
        context_chunks=[{"id": c.id, "text": c.text} for c, _ in ranked],
        # §16 residency and zero retention. Resolved per request rather than
        # captured once at startup, because a customer's requirement is recorded
        # against their organisation and can change between two messages in the
        # same conversation.
        policy=policy,
    )
    context = list(prepared.context_chunks)
    result = llm.generate(instructions=f"Answer the user's question grounded only in <context>. Question: {body.text}", context_chunks=context, fact_sheet={})
    answer_text = " ".join(b["text"] for b in result.blocks)
    sources = [{"chunk_id": c.id, "heading_path": c.heading_path} for c, _ in ranked[:3]]

    assistant_msg = ChatMessage(conversation_id=conversation_id, org_id=conv.org_id, role="assistant", text=answer_text, sources=sources)
    db.add(assistant_msg)
    db.commit()
    db.refresh(assistant_msg)
    return {"id": assistant_msg.id, "role": "assistant", "text": assistant_msg.text, "sources": sources}
