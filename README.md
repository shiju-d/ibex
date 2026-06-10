# ibex — IBE Expert

> AI-powered code intelligence for the IBE codebase — local Ollama, Anthropic Claude, or AWS Bedrock.

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

### 2. The Models

#### `mxbai-embed-large` — The Filing System (always used)
- Runs locally via Ollama — used by every endpoint
- Doesn't answer questions; converts text into a list of numbers called an **embedding**
- Similar text produces similar numbers, so "checkout fails" and "payment error" end up near each other in number-space
- Used twice: once to file every code chunk when indexing, and once to convert your question before searching

#### Answering models — choose one per request

| Model | Where it runs | Best for |
|-------|--------------|----------|
| `qwen2.5-coder:7b` | Local via Ollama | Offline use, fast iteration, no API costs |
| `claude-sonnet-4-6` | Anthropic API | Deeper reasoning, richer explanations |
| `claude-sonnet-4-5` | AWS Bedrock | Cloud-hosted Claude via your AWS account |

All three models read the same retrieved code chunks — only the answering step differs.

---

### 3. LlamaIndex — The Glue

LlamaIndex is the Python library that connects everything:

```
Your files  → LlamaIndex → TokenTextSplitter → mxbai-embed-large → ChromaDB (stored on disk)

Your question → LlamaIndex → mxbai-embed-large → find top 8 matches → answering model → answer
                                                                          ├── qwen2.5-coder:7b  (POST /chat)
                                                                          ├── claude-sonnet-4-6 (POST /chat/claude)
                                                                          └── claude-sonnet-4-5 (POST /chat/bedrock)
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
1. Your message arrives at one of the three endpoints:
      POST /chat          → qwen2.5-coder:7b  (local)
      POST /chat/claude   → claude-sonnet-4-6 (Anthropic API)
      POST /chat/bedrock  → claude-sonnet-4-5 (AWS Bedrock)
2. mxbai-embed-large converts your question into numbers
3. ChromaDB finds the 8 code chunks whose numbers are closest to your question's numbers
4. Those 8 chunks + your question are sent to the chosen model
5. The model reads the chunks and writes an answer
6. The answer + the source file paths are returned to you
```

The top 8 chunks means better cross-file tracing — the model can see a controller, its service, and the repository all at once when answering. All three endpoints retrieve the same chunks from the same index; only the model that generates the answer differs.

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
                                          ┌─────────────────────┐
                                     ┌───►│  Anthropic API      │
                                     │    │  claude-sonnet-4-6  │
                                     │    └─────────────────────┘
                                     │
                                     │    ┌─────────────────────┐
                                     ├───►│  AWS Bedrock        │
                                     │    │  claude-sonnet-4-5  │
                                     │    └─────────────────────┘
┌────────────────────────────────────┼──────────────────────────────┐
│ Your Machine                       │                              │
│                                    │                              │
│  ┌──────────────┐   ┌──────────────┴──────────────────────────┐  │
│  │    Ollama    │   │   Docker                                 │  │
│  │  port 11434  │◄──│                                          │  │
│  │              │   │  ┌──────────────────────────────────┐   │  │
│  │ qwen2.5-     │   │  │   ibex  (port 8000)              │   │  │
│  │ coder:7b  ◄──┼───┼──│                                  │   │  │
│  │              │   │  │  runner.py  LlamaIndex  ChromaDB │   │  │
│  │ mxbai-embed- │   │  └──────────────┬───────────────────┘   │  │
│  │ large     ◄──┼───┘                 │ reads                  │  │
│  └──────────────┘     ┌───────────────▼───────────────────┐   │  │
│                        │  /app/ibe (read-only)             │   │  │
│                        │  ibe-api/  ibe-frontend/          │   │  │
│                        │  ibe-admin/                       │   │  │
│                        └───────────────────────────────────┘   │  │
│                                                                 │  │
└─────────────────────────────────────────────────────────────────┘
                               ▲
                               │ POST /chat
                               │ POST /chat/claude
                               │ POST /chat/bedrock
                        ┌──────┴───────┐
                        │     n8n      │
                        │  Chat UI →   │
                        │  HTTP Node   │
                        └──────────────┘
```

---

## API Endpoints

| Endpoint | LLM | What it does |
|----------|-----|--------------|
| `POST /chat` | `qwen2.5-coder:7b` (local) | Ask a question using the local Ollama model. Fully offline. |
| `POST /chat/claude` | `claude-sonnet-4-6` (Anthropic API) | Ask a question using Claude. Requires `ANTHROPIC_API_KEY`. |
| `POST /chat/bedrock` | `claude-sonnet-4-5` (AWS Bedrock) | Ask a question via AWS Bedrock. Requires AWS credentials. |
| `DELETE /session/{id}` | — | Clear conversation history for a session (all endpoints). |
| `POST /reindex` | — | Wipe and rebuild the index (use after code changes). |

All three chat endpoints accept the same request body and return the same response shape. Sessions are independent per endpoint — a `session_id` used on `/chat` has no memory of conversations on `/chat/claude` or `/chat/bedrock`.

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

### Enabling the Claude endpoint

Create a `.env` file in the `rag/` directory:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Docker Compose picks this up automatically. If the key is not set, `POST /chat/claude` returns `503`.

### Enabling the Bedrock endpoint

Add your AWS credentials to the same `.env` file:

```
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION_NAME=us-east-1
BEDROCK_MODEL_ID=global.anthropic.claude-sonnet-4-5-20250929-v1:0
```

`AWS_REGION_NAME` has a default (`us-east-1`) — only `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `BEDROCK_MODEL_ID` are required. If the AWS keys are missing, `POST /chat/bedrock` returns `503`.

**Important:** Newer Claude models on Bedrock (Claude 3.7+) require a **cross-region inference profile ID**, not a plain model ID. Profile IDs are prefixed with `us.`, `eu.`, or `global.` — find yours in the Bedrock console under **Infer → Inference profiles**. The IAM user or role must have `bedrock:InvokeModel` permission.

---

## n8n Workflow

Import `IBE-RAG-MultiEndpoint.json` into n8n to get a chat UI where you select the LLM by prefixing your message:

| Prefix | LLM |
|--------|-----|
| `local: <message>` | qwen2.5-coder:7b (local, offline) |
| `claude: <message>` | Claude Sonnet 4.6 (Anthropic API) |
| `bedrock: <message>` | Claude Sonnet 4.5 (AWS Bedrock) |
| *(no prefix)* | qwen2.5-coder:7b (default) |

**Example:**
```
bedrock: Why is checkout failing when a promo code is applied?
claude: Trace the call chain for the cart service
```

---

## Running It

```bash
# 1. Pull the models on your host machine (one-time)
ollama pull qwen2.5-coder:7b
ollama pull mxbai-embed-large

# 2. Start ibex
cd rag
docker-compose up --build

# 3. First run takes 1-3 minutes to index the codebase
#    Subsequent starts load from disk in ~2 seconds
```

### Useful commands

```bash
# View logs
docker-compose logs ibex

# Stop
docker-compose down

# Rebuild after code changes to runner.py
docker-compose up --build
```

### After pulling new code changes

```bash
curl -X POST http://localhost:8000/reindex
```

This wipes the existing index and rebuilds it from the latest source files.
