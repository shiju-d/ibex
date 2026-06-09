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
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.vector_stores.chroma import ChromaVectorStore

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")

Settings.llm = Ollama(base_url=OLLAMA_BASE_URL, model="qwen2.5-coder:7b", request_timeout=120.0)
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
session_memories: OrderedDict = OrderedDict()
session_engines: OrderedDict = OrderedDict()


def _get_engine(session_id: str):
    if session_id not in session_engines:
        if len(session_engines) >= MAX_SESSIONS:
            session_memories.popitem(last=False)
            session_engines.popitem(last=False)
        memory = ChatMemoryBuffer.from_defaults(token_limit=2048)
        session_memories[session_id] = memory
        session_engines[session_id] = index.as_chat_engine(
            chat_mode="condense_plus_context",
            memory=memory,
            similarity_top_k=8,
            system_prompt=SYSTEM_PROMPT,
        )
    return session_engines[session_id]


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
                "**/rag/**", "**/log/**", "**/tmp/**",
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


app = FastAPI(title="IBE Codebase RAG API", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


@app.post("/chat")
async def chat_endpoint(request: ChatRequest):
    if index is None:
        raise HTTPException(status_code=503, detail="RAG engine is initializing")
    try:
        engine = _get_engine(request.session_id)
        response = await asyncio.to_thread(engine.chat, request.message)
        sources = list({
            node.metadata.get("file_path", "unknown")
            for node in response.source_nodes
        })
        return {"response": response.response, "sources": sources}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/session/{session_id}")
def clear_session(session_id: str):
    session_memories.pop(session_id, None)
    session_engines.pop(session_id, None)
    return {"cleared": session_id}


@app.post("/reindex")
async def reindex():
    global index
    index = None
    session_engines.clear()
    session_memories.clear()
    chroma_client = chromadb.PersistentClient(path="/app/chroma_db")
    chroma_client.delete_collection("ibe_codebase")
    await asyncio.to_thread(_build_index)
    return {"status": "reindexed"}
