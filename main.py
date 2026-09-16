import json
import os
import logging
import re
import time
import uuid
import threading
from dataclasses import dataclass, field
from typing import List, Literal, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from openrouter import OpenRouter

from agent.prompts import build_system_prompt
from agent.tools import TOOLS, cards_for, focus_for, run_tool, draft_public, draft_pdf_path, email_method  # all defined in agent/tools.py

from dotenv import load_dotenv
load_dotenv()


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")  # shows in Render's Logs tab
log = logging.getLogger("agent.main")
# ============================================================================
# variables  (env values are always text -> int() them; the "or" gives a default when a variable is missing)
# ============================================================================
SESSION_IDLE_MINUTES = int(os.getenv('SESSION_IDLE_MINUTES') or 120)
MAX_SESSIONS = int(os.getenv('MAX_SESSIONS') or 1000)
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')

MAX_HISTORY_MESSAGES = 24 # agent/session can store 24 prevous user conversation -> if it exids then remove the oldest conversation
MAX_TOOL_STEPS = int(os.getenv('MAX_TOOL_STEPS') or 6) # 6 -> tool calls per visitor message
MAX_TOOL_RESULT_CHARS = 6000  # long tool results are cut so prompts stay small

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# minimax/minimax-m3:free is no longer on OpenRouter; this free model answers in a few seconds.
OPENROUTER_MODEL = os.getenv('OPENROUTER_MODEL') or "nvidia/nemotron-3-super-120b-a12b:free"  # nvidia/nemotron-3-ultra-550b-a55b:free   nvidia/nemotron-3.5-lightning:free

LLM_MAX_TOKENS = int(os.getenv('LLM_MAX_TOKENS') or 8000)  # free models cap output at 32k-65k tokens, so 100000 is refused
LLM_TIMEOUT_SECONDS = int(os.getenv('LLM_TIMEOUT_SECONDS') or 90)  # give up on a model that doesn't answer
LLM_RETRIES = int(os.getenv('LLM_RETRIES') or 2)  # extra tries when the free provider is busy or times out

# The website's URL isn't known when the backend is deployed, so any origin may call the API ("*").
# Once you know it, set ALLOWED_ORIGINS=https://your-site.onrender.com,http://localhost:5173 on Render.
ALLOWED_ORIGINS = [origin.strip() for origin in (os.getenv('ALLOWED_ORIGINS') or '*').split(',') if origin.strip()]


# ============================================================================
# sessions : build a session that holds time .. if expires then delete the session + previous sotored conversation + tracked time ... create  session with time with statefull convesation with time and storage of 24 message
# ============================================================================

@dataclass
class Session:
    id: str
    history: list = field(default_factory=list)  # [{"role": "user"|"assistant", "content": str}]  *** can store previous 24 message **
    turn: int = 0  # how many visitor messages so far
    drafts: dict = field(default_factory=dict)  # draft_id -> draft
    pending: dict = field(default_factory=dict)  # action -> details waiting for the visitor's "yes"
    emails_sent: int = 0
    inquiries_saved: int = 0
    saved_inquiries: set = field(default_factory=set)
    events: dict = field(default_factory=dict)  # what the current turn did, for the API response
    website_url: str = ""  # the website's origin, sent by the browser (window.location.origin)
    last_seen: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def remember(self, role, content):  # append and remove oldest conversation/message from the session
        self.history.append({"role": role, "content": content})
        del self.history[:-MAX_HISTORY_MESSAGES]



class SessionStore:
    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def _drop_idle(self):
        cutoff = time.time() - SESSION_IDLE_MINUTES * 60
        for session_id in [sid for sid, s in self._sessions.items() if s.last_seen < cutoff]: # time expired -> delete user session
            del self._sessions[session_id]

        # remove some sessions from '_sessions' -> cause I can't hold inf ammount of user session here ... if old user can't find -> then he will create a new one
        while len(self._sessions) >= MAX_SESSIONS:  # forget the least recently used
            oldest = min(self._sessions.values(), key=lambda s: s.last_seen)
            del self._sessions[oldest.id]


    def get_or_create(self, session_id=None):
        # validate the session against time ... if the time expired then delete it ... if the sessions not in '_sessions' dict -> then create a new one
        with self._lock:
            session = self._sessions.get(session_id , None)

            if session is None:
                self._drop_idle()
                new_id = session_id if session_id and SESSION_ID_PATTERN.fullmatch(session_id) else str(uuid.uuid4())
                session = self._sessions[new_id] = Session(id=new_id)
            session.last_seen = time.time()
            return session


    def get(self, session_id):
        with self._lock:
            return self._sessions.get(session_id)

    def delete(self, session_id):
        with self._lock:
            return self._sessions.pop(session_id, None) is not None

sessions = SessionStore()




# ============================================================================
# LangGraph Staatefull Variables
# ============================================================================

class ChatRequest(BaseModel):
    message: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="What the visitor typed."
    )
    session_id: Optional[str] = Field(
        None,
        max_length=64,
        description="Leave empty to start a conversation; send back the session_id from the last reply to continue it.",
    )
    website_url: Optional[str] = Field(
        None,
        max_length=200,
        description="The website's origin (window.location.origin in React), used for links in PDFs and emails.",
    )

class ChatResponse(BaseModel):
    session_id: str
    reply: str
    cards: List[dict] = []  # listing cards to draw under the reply
    map_focus: Optional[dict] = None  # {name, emirate, isEmirate, center: [lat, lng]}
    suggestions: List[str] = []  # quick-reply chips
    draft: Optional[dict] = None  # a PDF created or updated in this turn
    pending_confirmation: Optional[dict] = None  # an enquiry or email waiting for the visitor's "yes"
    inquiry: Optional[dict] = None  # set when an enquiry was saved
    email: Optional[dict] = None  # set when an email was sent
    tools_used: List[str] = []



# ============================================================================
# Setup LLMs
# ============================================================================
_TEMPORARY = ("provider_unavailable", "rate limit", "rate-limit", "429", "timeout", "timed out", "overloaded", "502", "503", "504", "temporarily", "connection")
_THINKING = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    """The model couldn't be reached, or sent nothing back."""

def _explain(err):
    text = str(err).lower()
    if "unavailable for free" in text or "no endpoints found" in text or "not a valid model" in text:
        return f"The model '{OPENROUTER_MODEL}' isn't available (it may no longer be free). Set another OPENROUTER_MODEL in .env."

    if "401" in text or "unauthorized" in text or "invalid api key" in text or "user not found" in text:
        return "OpenRouter rejected the API key. Check OPENROUTER_API_KEY in .env."

    if any(word in text for word in _TEMPORARY):
        return "The model's provider is busy or unavailable right now. Please try again in a moment."

    return f"OpenRouter request failed: {str(err)[:200]}"


def chat_llm(messages):
    """Send a whole conversation ([{"role": ..., "content": ...}, ...]) and return the reply text. Temporary failures (busy provider, rate limit, timeout, empty reply) are retried."""
    for attempt in range(LLM_RETRIES + 1):
        last_try = attempt == LLM_RETRIES
        try:
            with OpenRouter(api_key=OPENROUTER_API_KEY) as client:
                response = client.chat.send(
                    model=OPENROUTER_MODEL,  # nvidia/nemotron-3-super-120b-a12b:free   nvidia/nemotron-3.5-lightning:free
                    messages=messages,
                    temperature=0,  # 0 to 2.0
                    max_tokens=LLM_MAX_TOKENS,
                    reasoning={"enabled": False, "effort": "minimal", "exclude": True},
                    reasoning_effort="minimal",  # xhigh, high, medium, low, minimal, none
                    timeout_ms=LLM_TIMEOUT_SECONDS * 1000,  # a stuck free model can't hang the chat forever
                )
        except Exception as err:
            temporary = any(word in str(err).lower() for word in _TEMPORARY)
            log.warning("OpenRouter call failed (try %s of %s): %s", attempt + 1, LLM_RETRIES + 1, str(err)[:300])
            if temporary and not last_try:
                time.sleep(2 * (attempt + 1))
                continue
            raise LLMError(_explain(err)) from err

        content = response.choices[0].message.content if response.choices else None
        if isinstance(content, list):  # some models answer with a list of parts
            content = "".join(
                (part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "") or "")
                for part in content
            )
        if content and content.strip():
            return content
        log.warning("The model returned an empty reply (try %s of %s)", attempt + 1, LLM_RETRIES + 1)
        if not last_try:
            time.sleep(1)

    raise LLMError("The model returned an empty reply. Please try again.")


def generator_llm_openrouter(systemPrompt, humanPrompt):
    """Your one-question helper, with the same name and settings."""
    return chat_llm([
        {"role": "system", "content": systemPrompt},
        {"role": "user", "content": humanPrompt},
    ])

def parse_json_object(text):
    """Pull the JSON object out of a model reply: removes ```json fences and <think> blocks."""
    raw = _THINKING.sub("", text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1].removeprefix("json").strip()
    try:
        value = json.loads(raw)

    except json.JSONDecodeError:

        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("the reply contained no JSON object") from None
        value = json.loads(raw[start : end + 1])

    if not isinstance(value, dict):
        raise ValueError("the reply was JSON, but not an object")
    return value







# ============================================================================
# Agent Graph
# ============================================================================

class AgentDecision(BaseModel):
    """Agent will send response with the below value in JSON format ... and we will validate them using below vars...
      One step from the model: use a tool, or reply to the visitor."""
    action: Literal["tool", "reply"]
    tool: Optional[str] = None
    args: dict = Field(default_factory=dict)
    reply: Optional[str] = None
    property_ids: List[str] = Field(default_factory=list)
    focus_area: Optional[str] = None
    suggestions: List[str] = Field(default_factory=list)


# ======= States for agent =======
class AgentState(BaseModel):
    user_message: str
    history:List[dict] = [] # earlier turns of this conversation
    scratchpad: List[dict] = []  # this turn's tool calls and their results
    steps: int = 0
    decision: Optional[dict] = None
    tools_used: List[str] = [] # filled by tool_node
    reply: str = ""  # reply / cards / map_focus / suggestions are filled by finalize_node
    cards:List[dict] = []
    map_focus: Optional[dict] = None
    suggestions: List[str] = []





def agent_llm_node(state: AgentState, config:RunnableConfig):
    messages = [
        {"role": "system", "content": build_system_prompt()},
        *state.history,
        {"role": "user", "content": state.user_message},
        *state.scratchpad,
    ]

    out_of_steps = state.steps >= MAX_TOOL_STEPS
    if out_of_steps:
        messages.append({"role": "user", "content": 'SYSTEM NOTE: no more tools for this message. Answer the visitor now with "action": "reply".'})

    raw = ""
    for attempt in range(2):  # one retry if the reply isn't valid JSON
        raw = chat_llm(messages)
        try:
            decision = AgentDecision.model_validate(parse_json_object(raw))

            if decision.action == "tool" and decision.tool not in TOOLS:
                raise ValueError(f"there is no tool called {decision.tool!r}")
            if decision.action == "tool" and out_of_steps:
                raise ValueError("no more tools are allowed for this message")
            if decision.action == "reply" and not (decision.reply or "").strip():
                raise ValueError("the reply text is empty")
            return {"decision": decision.model_dump()}

        except (ValueError, ValidationError) as err:
            log.info("Model reply rejected in agent Node - agent_llm_node : (attempt %s): %s", attempt + 1, err)
            messages = [
                *messages,
                {"role": "assistant", "content": raw[:2000]},
                {"role": "user", "content": f"SYSTEM NOTE: that wasn't a valid answer ({err}). Reply again with ONLY one JSON object in the required format."},
            ]

    # Still not valid. A plain-text answer is usually fine to show as it is.
    text = raw.strip()
    if text and "{" not in text:
        return {"decision": AgentDecision(action="reply", reply=text[:1500]).model_dump()}
    return {"decision": AgentDecision(action="reply", reply="Sorry, I got muddled there. Could you ask that another way?").model_dump()}



def route_after_llm(state: AgentState):
    if state.decision and state.decision["action"] == "tool" :
        return "tool_node"
    else :
        return "finalize_node"


def tool_node(state: AgentState, config: RunnableConfig):
    name = state.decision["tool"]
    args = state.decision.get("args") or {}

    result = run_tool(name, args, config["configurable"]["session"])
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + " ...(cut short)"

    return {
        "scratchpad": [
            *state.scratchpad,
            {"role": "assistant", "content": json.dumps({"action": "tool", "tool": name, "args": args}, ensure_ascii=False)},
            {"role": "user", "content": f"TOOL RESULT for {name}:\n{text}"},
        ],
        "steps": state.steps + 1,
        "tools_used": [*state.tools_used, name],
    }



def finalize_node(state: AgentState, config: RunnableConfig):
    session = config["configurable"]["session"]
    decision = state.decision or {}

    return {
        "reply": (decision.get("reply") or "").strip(),
        "cards": cards_for(decision.get("property_ids") or []),
        "map_focus": focus_for(decision.get("focus_area")) or session.events.get("map_focus"),
        "suggestions": [s.strip() for s in decision.get("suggestions") or [] if isinstance(s, str) and s.strip()][:3],
    }


# ================== Now Build Graph ==================
graph = StateGraph(AgentState)
graph.add_node("agent_llm_node", agent_llm_node)
graph.add_node("tool_node", tool_node)
graph.add_node("finalize_node", finalize_node)

graph.add_edge(START, "agent_llm_node")
graph.add_conditional_edges(
    "agent_llm_node",
    route_after_llm,
    {"tool_node": "tool_node", "finalize_node": "finalize_node"},
)
graph.add_edge("tool_node", "agent_llm_node")
graph.add_edge("finalize_node", END)

mainWorkingGraph = graph.compile()




# ================== Now run the agent ==================

def run_turn(session, message):
    session.turn += 1
    session.events = {} # current turn Q&A

    result = mainWorkingGraph.invoke(
        {
            "user_message": message,
            "history": list(session.history)
        },
        config={
            "configurable": {"session": session},
            "recursion_limit": 4 * MAX_TOOL_STEPS + 10
        },
    )

    # LangGraph only returns fields a node actually changed, so fall back to the
    # defaults (e.g. tools_used stays unset when the model answered without tools).
    reply = result.get("reply") or ""
    cards = result.get("cards") or []
    focus = result.get("map_focus")
    suggestions = result.get("suggestions") or []
    events = session.events

    context = []
    if cards:
        context.append("shown listings: " + "; ".join(f"{c['id']} = {c['title']}" for c in cards))
    if events.get("draft"):
        draft = events["draft"]
        context.append(f"draft ready: draft_id={draft['draft_id']} title={draft['title']!r} version={draft['version']}")
    if events.get("pending_confirmation"):
        context.append(f"waiting for the visitor to confirm: {events['pending_confirmation']['action']}")

    session.remember("user", message)
    session.remember("assistant", json.dumps({
        "action": "reply",
        "reply": reply,
        "property_ids": [c["id"] for c in cards],
        "focus_area": (focus or {}).get("name"),
        "suggestions": suggestions,
        **({"context": " | ".join(context)} if context else {}),
    }, ensure_ascii=False))


    return {
        "reply": reply,
        "cards": cards,
        "map_focus": focus,
        "suggestions": suggestions,
        "draft": events.get("draft"),
        "pending_confirmation": events.get("pending_confirmation"),
        "inquiry": events.get("inquiry"),
        "email": events.get("email"),
        "tools_used": result.get("tools_used") or [],
    }



# ============================================================================
# Fast Api Setup
# ============================================================================
app = FastAPI(
    title="Real Estate Agent API",
    version="1.0.0",
    description="Chat agent for the Dubai & Abu Dhabi property website: search listings, estimate costs, save enquiries, and create PDF drafts that can be emailed.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


@app.get("/")
def home():
    return {
        "answer": "Real estate agent API is running!",
        "docs": "/docs",
        "chat": "POST /chat",
        "model": OPENROUTER_MODEL,
        "email": email_method() or "not set up",
    }


@app.post("/chat", response_model=ChatResponse)
def chat(data: ChatRequest): # ChatRequest : message , session_id , website_url
    if not OPENROUTER_API_KEY:
        raise HTTPException(status_code=503, detail="OPENROUTER_API_KEY is not set on the server.")
    if data.session_id and not SESSION_ID_PATTERN.fullmatch(data.session_id):
        raise HTTPException(status_code=422, detail="session_id may only contain letters, numbers, - and _ (8-64 characters).")

    session = sessions.get_or_create(data.session_id)
    if not session.lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="This conversation is still answering the previous message.")


    try:
        if data.website_url:  # checked in tools.website_url(): only a clean https://host is used in PDFs and emails
            session.website_url = data.website_url.strip()
        result = run_turn(session, data.message.strip())
    except LLMError as err:
        raise HTTPException(status_code=503, detail=f"The language model is unavailable right now. {err}") from err
    except Exception as err:
        log.exception("Chat turn failed")
        raise HTTPException(status_code=500, detail="The assistant hit an unexpected problem. Please try again.") from err
    finally:
        session.lock.release()

    return {"session_id": session.id, **result}


@app.get("/sessions/{session_id}")
def get_session(session_id: str):
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="No conversation with that session_id (they end after a while, or when the server restarts).")

    messages = []
    for message in session.history:
        if message["role"] == "assistant":
            try:
                saved = json.loads(message["content"])
                messages.append({"role": "assistant", "text": saved.get("reply", ""), "property_ids": saved.get("property_ids", [])})
                continue
            except ValueError:
                pass

        messages.append({"role": message["role"], "text": message["content"]})
    return {
        "session_id": session.id,
        "messages": messages,
        "drafts": [draft_public(d) for d in session.drafts.values()],
        "inquiries_saved": session.inquiries_saved,
        "emails_sent": session.emails_sent,
    }




@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    return {"deleted": sessions.delete(session_id)}


@app.get("/drafts/{draft_id}.pdf")
def download_draft(draft_id: str):
    path = draft_pdf_path(draft_id)  # None for a bad id or a missing file
    if path is None:
        raise HTTPException(status_code=404, detail="Draft not found (drafts are lost when the server restarts).")

    return FileResponse(path, media_type="application/pdf", filename=f"draft-{draft_id[:8]}.pdf")




if __name__ == "__main__":
    # Locally: python main.py   |   On Render the start command is: uvicorn main:app --host 0.0.0.0 --port $PORT
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=int(os.getenv("PORT") or 8000), reload=True)
