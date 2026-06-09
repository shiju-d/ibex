# ibex — IBE Expert

> Local AI-powered bug analysis for the IBE codebase.

## The Three Pieces

### 1. Ollama — The AI Engine Running on Your Machine

Ollama is a tool that lets you run AI models **locally** — no internet, no cloud, no API costs. Think of it as Docker, but for AI models. You pull a model once and it runs on your own hardware.

```
Your Machine
└── Ollama (running on port 11434)
    ├── qwen2.5-coder:7b   ← the "brain" that answers questions
    └── mxbai-embed-large  ← the "filing system" that organises code chunks
```

The RAG container talks to Ollama via `host.docker.internal:11434`, which is Docker's way of saying "connect to the host machine's port 11434."

---

### 2. The Two Models

#### `qwen2.5-coder:7b` — The Answering Brain
- Made by Alibaba's Qwen team, trained specifically on code
- **7b** means 7 billion parameters — think of it as 7 billion tiny knobs that were tuned to understand and reason about code
- It reads the relevant code chunks and writes a human-readable answer
- Runs locally via Ollama, responds in ~5–15 seconds depending on your hardware

#### `mxbai-embed-large` — The Filing System
- This model doesn't answer questions — it converts text into a list of numbers called an **embedding**
- Similar text produces similar numbers, so "checkout fails" and "payment error" end up near each other in number-space
- Used twice: once to file every code chunk when indexing, and once to convert your question before searching

---

### 3. LlamaIndex — The Glue

LlamaIndex is the Python library that connects everything:

```
Your files → LlamaIndex → TokenTextSplitter → mxbai-embed-large → ChromaDB (stored on disk)
Your question → LlamaIndex → mxbai-embed-large → find top 8 matches → qwen2.5-coder:7b → answer
```

---

## What Happens Step by Step

### First Run — Building the Index

```
1. Read all .js, .jsx, .ts, .tsx files from ibe-api, ibe-frontend, ibe-admin
   - Skips test files: __tests__/, *.spec.ts, *.test.ts, cypress/, e2e/
2. Split files into 600-token chunks (100 token overlap) using TokenTextSplitter
   - Splits on newlines first, then falls back to code keywords (class, function, const, export)
3. Send each chunk to mxbai-embed-large → get back a list of numbers
4. Store chunk + numbers in ChromaDB on disk (/app/chroma_db)
```

This takes 1–3 minutes the first time. After that, the index is saved to disk and loads in ~2 seconds on every restart.

### Every Restart After That

```
1. ChromaDB already has everything — skip the embedding step entirely
2. Load the existing index from disk
3. Ready in ~2 seconds
```

### When You Ask a Question

```
1. Your message arrives at POST /chat
2. mxbai-embed-large converts your question into numbers
3. ChromaDB finds the 8 code chunks whose numbers are closest to your question's numbers
4. Those 8 chunks + your question are sent to qwen2.5-coder:7b
5. The model reads the chunks and writes an answer
6. The answer + the source file paths are returned to you
```

The top 8 chunks (up from 5) means better cross-file tracing — the model can see a controller, its service, and the repository all at once when debugging a bug.

---

## The Codebase Being Indexed

The RAG reads three applications:

| App | Language | What it does |
|-----|----------|--------------|
| `ibe-api` | TypeScript (LoopBack 4) | REST API — controllers, services, repositories, models |
| `ibe-frontend` | JavaScript (Express + Jade) | Server-rendered booking UI |
| `ibe-admin` | TypeScript (Angular 19) | Admin dashboard for hotel configuration |

**Excluded from indexing:**

| Excluded | Reason |
|----------|--------|
| `node_modules`, `dist` | Build artifacts and dependencies |
| `.git`, `log`, `tmp` | Not source code |
| `__tests__`, `*.spec.ts`, `*.test.ts` | Test doubles add noise to bug analysis |
| `cypress`, `e2e` | E2E test scripts, not application logic |

---

## Conversation Memory

Each chat session keeps a memory of the last ~2,048 tokens of conversation (roughly 5–10 exchanges). This means you can ask follow-up questions without repeating yourself:

```
You:  "Where is the cart service?"
Bot:  "It's in ibe-api/src/services/cart.service.ts ..."

You:  "Why would it fail with a promo code?"   ← no need to say "cart service" again
Bot:  "Looking at the cart service you mentioned ..."
```

Each session is isolated — your conversation doesn't bleed into someone else's. Sessions are cached so the engine doesn't get recreated on every message. Up to **100 sessions** are kept in memory; the oldest is evicted automatically when the limit is reached.

---

## The Full Architecture

```
┌─────────────────────────────────────────────────┐
│                  Your Machine                   │
│                                                 │
│  ┌──────────────┐     ┌───────────────────────┐ │
│  │    Ollama    │     │   Docker              │ │
│  │  port 11434  │◄────│                       │ │
│  │              │     │  ┌─────────────────┐  │ │
│  │ qwen2.5-     │     │  │   RAG Container │  │ │
│  │ coder:7b     │     │  │   port 8000     │  │ │
│  │              │     │  │                 │  │ │
│  │ mxbai-embed- │     │  │  runner.py      │  │ │
│  │ large        │     │  │  LlamaIndex     │  │ │
│  └──────────────┘     │  │  ChromaDB       │  │ │
│                        │  └────────┬────────┘  │ │
│                        │           │ reads      │ │
│                        │  ┌────────▼────────┐  │ │
│                        │  │  /app/ibe (ro)  │  │ │
│                        │  │  ibe-api/       │  │ │
│                        │  │  ibe-frontend/  │  │ │
│                        │  │  ibe-admin/     │  │ │
│                        │  └─────────────────┘  │ │
│                        └───────────────────────┘ │
└─────────────────────────────────────────────────┘
                          ▲
                          │ POST /chat
                   ┌──────┴───────┐
                   │     n8n      │
                   │  Chat UI →   │
                   │  HTTP Node   │
                   └──────────────┘
```

---

## API Endpoints

| Endpoint | What it does |
|----------|--------------|
| `POST /chat` | Ask a question. Send `message` and `session_id`. |
| `DELETE /session/{id}` | Clear conversation history for a session. |
| `POST /reindex` | Wipe and rebuild the index (use after code changes). |

### Example request
```json
POST /chat
{
  "message": "Why is checkout failing when a promo code is applied?",
  "session_id": "debug-session-1"
}
```

### Example response
```json
{
  "response": "The issue is likely in the applyPromoCode method in cart.service.ts ...",
  "sources": [
    "/app/ibe/ibe-api/src/services/cart.service.ts",
    "/app/ibe/ibe-api/src/controllers/cart.controller.ts"
  ]
}
```

---

## Running It

```bash
# 1. Pull the models on your host machine (one-time)
ollama pull qwen2.5-coder:7b
ollama pull mxbai-embed-large

# 2. Start the RAG container
cd rag
docker compose up --build

# 3. First run takes 1-3 minutes to index the codebase
#    Subsequent starts load from disk in ~2 seconds
```

### After pulling new code changes

```bash
curl -X POST http://localhost:8000/reindex
```

This wipes the existing index and rebuilds it from the latest source files.
