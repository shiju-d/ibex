import asyncio
import os
from collections import OrderedDict
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import chromadb
from llama_index.core import VectorStoreIndex, SimpleDirectoryReader, Settings, StorageContext
from llama_index.core.node_parser import TokenTextSplitter
from llama_index.core.memory import ChatMemoryBuffer
from llama_index.llms.ollama import Ollama
from llama_index.llms.anthropic import Anthropic
from llama_index.llms.bedrock_converse import BedrockConverse
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.vector_stores.chroma import ChromaVectorStore

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-3-5-sonnet-20241022-v2:0")

local_llm = Ollama(base_url=OLLAMA_BASE_URL, model="qwen2.5-coder:7b", request_timeout=120.0)
claude_llm = Anthropic(model="claude-sonnet-4-6", api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
bedrock_llm = BedrockConverse(
    model=BEDROCK_MODEL_ID,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_REGION,
) if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY else None

Settings.embed_model = OllamaEmbedding(base_url=OLLAMA_BASE_URL, model_name="mxbai-embed-large")

SYSTEM_PROMPT = """You are an expert software engineer specializing in debugging the Stayntouch IBE application.
The codebase consists of three apps:
- ibe-api: LoopBack 4 REST API (TypeScript) — controllers, services, repositories, models
- ibe-frontend: Express + Jade server-rendered app (JavaScript) — controllers, services, Vue client components
- ibe-admin: Angular 19 admin dashboard (TypeScript) — feature modules, services, components

When analysing bugs:
1. Identify the affected layer (controller / service / repository / model)
2. Trace the call chain across files
3. Point to the exact file and function where the bug likely originates
4. Suggest a fix with a code snippet
"""

index = None
MAX_SESSIONS = 100

local_sessions: OrderedDict = OrderedDict()
claude_sessions: OrderedDict = OrderedDict()
bedrock_sessions: OrderedDict = OrderedDict()


def _get_engine(session_id: str, llm, sessions: OrderedDict):
    if session_id not in sessions:
        if len(sessions) >= MAX_SESSIONS:
            sessions.popitem(last=False)
        memory = ChatMemoryBuffer.from_defaults(token_limit=2048)
        sessions[session_id] = {
            "memory": memory,
            "engine": index.as_chat_engine(
                chat_mode="condense_plus_context",
                llm=llm,
                memory=memory,
                similarity_top_k=8,
                system_prompt=SYSTEM_PROMPT,
            )
        }
    return sessions[session_id]["engine"]


def _build_index():
    global index
    PROJECT_ROOT_DIR = "/app/ibe"

    chroma_client = chromadb.PersistentClient(path="/app/chroma_db")
    collection = chroma_client.get_or_create_collection("ibe_codebase")
    vector_store = ChromaVectorStore(chroma_collection=collection)
    storage_context = StorageContext.from_defaults(vector_store=vector_store)

    if collection.count() > 0:
        print(f"Loading existing index ({collection.count()} chunks)...")
        index = VectorStoreIndex.from_vector_store(vector_store)
    else:
        print("Building index from source files...")
        reader = SimpleDirectoryReader(
            input_dir=PROJECT_ROOT_DIR, recursive=True,
            required_exts=[".js", ".jsx", ".ts", ".tsx"], exclude_hidden=True,
            exclude=[
                "**/node_modules/**", "**/dist/**", "**/.git/**",
                "**/ibex/**", "**/log/**", "**/tmp/**",
                "**/__tests__/**", "**/*.spec.ts", "**/*.test.ts",
                "**/cypress/**", "**/e2e/**"
            ]
        )
        documents = reader.load_data()

        splitter = TokenTextSplitter(
            chunk_size=600, chunk_overlap=100, separator="\n",
            backup_separators=["class ", "function ", "const ", "export ", "  "]
        )
        nodes = splitter.get_nodes_from_documents(documents)

        index = VectorStoreIndex(nodes, storage_context=storage_context)
        print(f"Index built and persisted ({len(nodes)} chunks).")

    print("RAG API ready.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(_build_index)
    yield


app = FastAPI(title="ibex", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


async def _chat(request: ChatRequest, llm, sessions: OrderedDict):
    if index is None:
        raise HTTPException(status_code=503, detail="RAG engine is initializing")
    try:
        engine = _get_engine(request.session_id, llm, sessions)
        response = await asyncio.to_thread(engine.chat, request.message)
        sources = list({
            node.metadata.get("file_path", "unknown")
            for node in response.source_nodes
        })
        return {"response": response.response, "sources": sources}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat")
async def chat_local(request: ChatRequest):
    return await _chat(request, local_llm, local_sessions)


@app.post("/chat/claude")
async def chat_claude(request: ChatRequest):
    if not claude_llm:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
    return await _chat(request, claude_llm, claude_sessions)


@app.post("/chat/bedrock")
async def chat_bedrock(request: ChatRequest):
    if not bedrock_llm:
        raise HTTPException(status_code=503, detail="AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY not configured")
    return await _chat(request, bedrock_llm, bedrock_sessions)


@app.delete("/session/{session_id}")
def clear_session(session_id: str):
    local_sessions.pop(session_id, None)
    claude_sessions.pop(session_id, None)
    bedrock_sessions.pop(session_id, None)
    return {"cleared": session_id}


@app.post("/reindex")
async def reindex():
    global index
    index = None
    local_sessions.clear()
    claude_sessions.clear()
    bedrock_sessions.clear()
    chroma_client = chromadb.PersistentClient(path="/app/chroma_db")
    chroma_client.delete_collection("ibe_codebase")
    await asyncio.to_thread(_build_index)
    return {"status": "reindexed"}
