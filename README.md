# ADK + MCP Agent

用 Google ADK 寫的簡單 agent，工具全部來自 MCP Server，模型使用 GCP Vertex AI 的 Gemini。

```
使用者 ──► ADK LlmAgent (Gemini on Vertex AI)
                │ McpToolset (stdio)
                ├──► @softeria/ms-365-mcp-server（唯讀）──► Microsoft Graph ──► Outlook 信件
                └──► @modelcontextprotocol/server-filesystem ──► ./workspace/
```

## 專案結構

```
adk-mcp-agent/
├── mcp_agent/
│   ├── __init__.py
│   ├── agent.py      # root_agent + McpToolset
│   ├── a365_observability.py  # 選用：把遙測送到 Microsoft Agent 365
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

# Microsoft 365（Entra app 註冊）
MS365_MCP_CLIENT_ID=your-entra-app-client-id
MS365_MCP_TENANT_ID=your-entra-tenant-id
```

### 4. 登入 Microsoft 365（只需一次）

Entra app 需要：Authentication 開啟「Allow public client flows」，Graph Delegated 權限 `User.Read`、`Mail.Read`，並完成 admin consent（API permissions 的 Status 欄顯示綠色勾勾）。

登入時要用和 `agent.py` 相同的工具範圍，server 才只會要求 `Mail.Read`：

```powershell
cd mcp_agent   # server 會讀取目前目錄的 .env 取得 MS365_MCP_* 設定
$env:ENABLED_TOOLS = '^(verify-login|list-mail-folders|list-mail-messages|list-mail-folder-messages|get-mail-message|list-mail-attachments)$'
npx -y @softeria/ms-365-mcp-server --read-only --login
npx -y @softeria/ms-365-mcp-server --verify-login
```

Token 會加密快取在 `%APPDATA%\ms-365-mcp-server\`。

### 5. Agent 365 遙測與工具（選用）

在 `mcp_agent/.env` 加上下列設定後，agent 會以 Entra agent identity 的身分：

- 透過 Microsoft OpenTelemetry Distro（S2S 路線）回報 `invoke_agent`、`chat`、`execute_tool` 給 Agent 365（`a365_observability.py`）
- 多一個 Agent 365 tooling gateway 的網路搜尋工具 `SearchWeb`（`mcp_WebSearchTools`），以 OBO 代表使用者呼叫（`a365_tooling.py`）
- 每次提問與最終回答都送 Microsoft Purview `processContent` 檢查，命中 DLP 就改回覆封鎖訊息（`a365_purview.py`）。需要 agent identity 有 delegated `ProtectionScopes.Compute.User`、`Content.Process.User`，且 DLP policy 的 location 要包含 agent identity 的 app ID。Purview 呼叫失敗時照常回答，並在 console 印警告

沒設定時 agent 照常運作，不送遙測、也沒有網路搜尋。

```dotenv
A365_TENANT_ID=your-entra-tenant-id
A365_BLUEPRINT_ID=your-agent-blueprint-app-id
A365_AGENT_ID=your-agent-identity-id
# Google service account 金鑰；blueprint 上要有信任這個 SA 的 federated credential
A365_GCP_SA_KEY_FILE=%USERPROFILE%\.gcp-keys\gcp-agent-test-key.json
# 預設不送出；設為 true 才會真的傳到 Agent 365
ENABLE_A365_OBSERVABILITY_EXPORTER=true
```

Token 流程：Google ID token → blueprint token（`fmi_path` = agent identity）→ agent identity 的 observability token。遙測會包含提問、模型回答、工具參數與結果。

網路搜尋走 OBO：使用者 token（`az login` 的帳號，scope `api://<blueprint>/access_agent`，blueprint 已預先授權 Azure CLI）＋ blueprint token → agent identity 的 OBO token（`McpServers.WebSearch.All`、`McpServersMetadata.Read.All`）。所以執行 `adk web` 前要先 `az login`；這兩個 scope 需要 Global Admin 在 blueprint 上同意。

Agent 必須已登記到 Agent Registry；只建立 Entra identity 的話，Agent 365 會回 `403 insufficient_scope`（需要 `Agent365.Observability.OtelWrite`）。登記方式：在一個有 `a365.config.json`（`agentIdentityDisplayName` 設為 identity 名稱）與 `a365.generated.config.json`（blueprint 的三個 ID）的資料夾執行 `a365 setup all --agent-registration-only`。這個模式會沿用同名的既有 identity，不建立 identity、不改 blueprint 權限。

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
- 「列出我最近 5 封未讀信」
- 「把最新那封信的重點整理成 summary.md 存到 workspace」
- 「幫我建一個 todo.md，內容列三件今天要做的事」