# ADK + MCP Agent

用 Google ADK 寫的簡單 agent，工具全部來自 MCP Server（官方 filesystem server），模型使用 GCP Vertex AI 的 Gemini。

```
使用者 ──► ADK LlmAgent (Gemini on Vertex AI)
                │ McpToolset (stdio)
                ▼
     @modelcontextprotocol/server-filesystem ──► ./workspace/
```

## 專案結構

```
adk-mcp-agent/
├── mcp_agent/
│   ├── __init__.py
│   ├── agent.py      # root_agent + McpToolset
│   └── .env          # 自行建立（見下方）
├── workspace/        # MCP server 只能存取這個資料夾
└── pyproject.toml
```

## 前置需求

- Python 3.12（已用 `.python-version` 固定）、[uv](https://docs.astral.sh/uv/)
- Node.js（MCP server 透過 `npx` 啟動）
- 二選一：
  - **Vertex AI（GCP）**：安裝 [gcloud CLI](https://cloud.google.com/sdk/docs/install)，GCP project 需啟用 Vertex AI API
  - **Gemini API key**：從 [Google AI Studio](https://aistudio.google.com/apikey) 取得

## 設定

### 1. 安裝套件

```bash
uv sync
```

### 2. 認證（Vertex AI）

```bash
gcloud auth application-default login
gcloud config set project <your-gcp-project-id>
gcloud services enable aiplatform.googleapis.com
```

### 3. 建立 `mcp_agent/.env`

```dotenv
# Option A: Vertex AI on GCP
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=global

# Option B: Gemini API key（不經過 GCP）
# GOOGLE_GENAI_USE_VERTEXAI=FALSE
# GOOGLE_API_KEY=your-api-key

# 選填：覆寫模型
# MODEL=gemini-3.5-flash
```

## 執行

```bash
# 瀏覽器 Dev UI（可看到每次 tool call 的事件與參數）
uv run adk web

# 或 CLI
uv run adk run mcp_agent
```

`adk web` 開啟後，左上角選 `mcp_agent`，可以試試：

- 「workspace 裡有哪些檔案？」
- 「讀一下 notes.txt」
- 「幫我建一個 todo.md，內容列三件今天要做的事」