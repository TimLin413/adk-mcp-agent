# ADK + MCP Agent

用 Google ADK 寫的 agent，模型使用 GCP Vertex AI 的 Gemini，工具來自 MCP Server。設定 Agent 365 後，agent 會以 Entra agent identity 的身分運作：回報遙測、使用 Agent 365 的網路搜尋工具，並由 Purview DLP 檢查每則對話。

```
使用者 ──► ADK LlmAgent (Gemini on Vertex AI)
                │ McpToolset
                ├──► @softeria/ms-365-mcp-server（唯讀，stdio）──► Microsoft Graph ──► Outlook 信件
                ├──► @modelcontextprotocol/server-filesystem（stdio）──► ./workspace/
                └──► Agent 365 tooling gateway（HTTP，OBO）──► mcp_WebSearchTools        ┐
                                                                                         │ 選用：
  每則提問與回答 ──► Microsoft Purview processContent（DLP，OBO）                          │ 需要 A365_* 設定
  每次執行 ──► Agent 365 遙測（Microsoft OpenTelemetry Distro，S2S）                      ┘
```

## 專案結構

```
adk-mcp-agent/
├── mcp_agent/
│   ├── __init__.py
│   ├── agent.py               # root_agent、McpToolset、App（掛上 plugin）
│   ├── a365_observability.py  # agent identity token、Agent 365 遙測 plugin
│   ├── a365_tooling.py        # OBO token、Agent 365 tooling gateway 的 MCP 工具
│   ├── a365_purview.py        # Purview DLP 檢查 plugin
│   └── .env                   # 自行建立（見下方）
├── workspace/                 # filesystem MCP server 只能存取這個資料夾
└── pyproject.toml
```

## 前置需求

安裝以下工具（PowerShell；裝完要開新的終端機才會生效）：

```powershell
winget install Git.Git
winget install Python.Python.3.12
winget install astral-sh.uv
winget install OpenJS.NodeJS.LTS          # MCP server 透過 npx 啟動
winget install Google.CloudSDK            # 使用 Vertex AI 時需要
winget install Microsoft.AzureCLI         # 使用 Agent 365 功能時需要
```

另外需要（向專案負責人取得）：

| 項目 | 用途 |
| --- | --- |
| GCP project 的 **Vertex AI User** 角色，或自己的 Gemini API key | 呼叫 Gemini |
| 讀信用的 Entra app client ID 與 tenant ID | `MS365_MCP_*` |
| Agent 365 的 tenant ID、blueprint ID、agent identity ID | `A365_*`（選用） |
| Google service account 金鑰檔 | agent identity 登入用（選用）。**不在 repo 裡**，私下傳遞 |
| 公司 Entra 帳號 | `az login`，網路搜尋與 Purview 以 OBO 代表你呼叫（選用） |

## 設定

### 1. 取得程式與安裝套件

```powershell
git clone https://github.com/TimLin413/adk-mcp-agent.git
cd adk-mcp-agent
uv sync
```

`uv sync` 會依 `.python-version` 用 Python 3.12 建立 `.venv`。之後每次 `git pull` 也要再跑一次。

### 2. Gemini 認證

使用 Vertex AI：

```powershell
gcloud auth application-default login
gcloud config set project <your-gcp-project-id>
```

或改用 Gemini API key（從 [Google AI Studio](https://aistudio.google.com/apikey) 取得），見下一步的 Option B。

### 3. 建立 `mcp_agent/.env`

```dotenv
# --- Gemini ---
# Option A: Vertex AI on GCP
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=global

# Option B: Gemini API key（不經過 GCP）
# GOOGLE_GENAI_USE_VERTEXAI=FALSE
# GOOGLE_API_KEY=your-api-key

# 選填：覆寫模型
# MODEL=gemini-3.5-flash

# --- 讀信工具（Entra app 註冊）---
MS365_MCP_CLIENT_ID=your-entra-app-client-id
MS365_MCP_TENANT_ID=your-entra-tenant-id

# --- Agent 365（選用；缺任一個 A365_* 就不啟用遙測、網路搜尋、Purview）---
A365_TENANT_ID=your-entra-tenant-id
A365_BLUEPRINT_ID=your-agent-blueprint-app-id
A365_AGENT_ID=your-agent-identity-id
# Google service account 金鑰，放在專案資料夾外面
A365_GCP_SA_KEY_FILE=%USERPROFILE%\.gcp-keys\your-sa-key.json
# true 才會把遙測送到 Agent 365；false 可暫停
ENABLE_A365_OBSERVABILITY_EXPORTER=true
```

`.env` 已被 `.gitignore` 排除。金鑰檔請放在 `%USERPROFILE%\.gcp-keys\`：

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.gcp-keys" | Out-Null
Move-Item <下載位置>\your-sa-key.json "$env:USERPROFILE\.gcp-keys\"
```

### 4. 登入 Microsoft 365 讀信（只需一次）

Entra app 需要：Authentication 開啟「Allow public client flows」，Graph Delegated 權限 `User.Read`、`Mail.Read`，並完成 admin consent。每個人用自己的帳號登入，讀的是自己的信。

登入時要用和 `agent.py` 相同的工具範圍，server 才只會要求 `Mail.Read`：

```powershell
cd mcp_agent   # server 會讀取目前目錄的 .env 取得 MS365_MCP_* 設定
$env:ENABLED_TOOLS = '^(verify-login|list-mail-folders|list-mail-messages|list-mail-folder-messages|get-mail-message|list-mail-attachments)$'
npx -y @softeria/ms-365-mcp-server --read-only --login
npx -y @softeria/ms-365-mcp-server --verify-login
cd ..
```

Token 會加密快取在 `%APPDATA%\ms-365-mcp-server\`。

### 5. Agent 365 功能（選用）

`.env` 有完整的 `A365_*` 設定時，agent 會以 Entra agent identity 的身分：

- 透過 Microsoft OpenTelemetry Distro（S2S 路線）回報 `invoke_agent`、`chat`、`execute_tool` 給 Agent 365（`a365_observability.py`）
- 多一個 Agent 365 tooling gateway 的網路搜尋工具 `SearchWeb`（`mcp_WebSearchTools`），以 OBO 代表使用者呼叫（`a365_tooling.py`）。每個問題最多搜尋 4 次
- 每則提問與最終回答都送 Microsoft Purview `processContent` 檢查，命中 DLP 就改回覆封鎖訊息（`a365_purview.py`）。Purview 呼叫失敗時照常回答，並在 console 印警告

執行 `adk web` 前要先用公司帳號登入 Azure CLI：

```powershell
az login --tenant <your-entra-tenant-id> --allow-no-subscriptions
az account show --query user.name -o tsv   # 應顯示你的公司帳號
```

#### 運作方式與前置條件

- **agent identity 的 token**：Google ID token → blueprint token（`fmi_path` = agent identity）→ agent identity 的 token。Blueprint 上要有信任該 Google service account 的 federated credential
- **遙測**：agent identity 必須已登記到 Agent Registry。只建立 Entra identity 的話，Agent 365 會回 `403 insufficient_scope`（需要 `Agent365.Observability.OtelWrite`）。登記方式：在一個有 `a365.config.json`（`agentIdentityDisplayName` 設為 identity 名稱）與 `a365.generated.config.json`（blueprint 的三個 ID）的資料夾執行 `a365 setup all --agent-registration-only`。這個模式會沿用同名的既有 identity，不建立 identity、不改 blueprint 權限
- **網路搜尋（OBO）**：使用者 token（`az login` 的帳號，scope `api://<blueprint>/access_agent`，blueprint 已預先授權 Azure CLI）＋ blueprint token → agent identity 的 OBO token（`McpServers.WebSearch.All`、`McpServersMetadata.Read.All`）。這兩個 scope 需要 Global Admin 在 blueprint 上同意
- **Purview（OBO）**：agent identity 需要 delegated `ProtectionScopes.Compute.User`、`Content.Process.User`，DLP policy 的 location 要包含 agent identity 的 app ID。目前權限透過 access package 授予，**一小時就到期**

## 執行

```powershell
# 瀏覽器 Dev UI（可看到每次 tool call 的事件與參數）
uv run adk web

# 或 CLI
uv run adk run mcp_agent
```

`adk web` 開啟後（http://127.0.0.1:8000），左上角選 `mcp_agent`。

## 測試案例

每個測試請開一個新的 session，結果看對話內容與執行 `adk web` 的終端機輸出。

| # | 測試 | 輸入 | 預期結果 |
| --- | --- | --- | --- |
| 1 | 檔案工具 | `workspace 裡有哪些檔案？` | 列出 `notes.txt` |
| 2 | 讀信 | `列出我最近 3 封未讀信` | 列出**自己**的信 |
| 3 | 網路搜尋 | `搜尋一下最新的 Microsoft Agent 365 文件，簡短說明` | 回答附來源網址；終端機出現 `POST https://agent365.svc.cloud.microsoft/agents/servers/mcp_WebSearchTools "HTTP/1.1 200 OK"`，`SearchWeb` 最多 4 次 |
| 4 | DLP 擋下 | `What is the UTC time? For my records, reference number 4532667785213500.` | 回覆「此內容違反組織的資料保護政策（Microsoft Purview DLP），已被封鎖。」；終端機出現 `Purview DLP blocked the prompt` |
| 5 | DLP 不誤擋 | `你好 明天會放颱風假嗎? 4532667785213500` | 正常回答（2026/10/22 實測時，規則要有「reference number」才會觸發） |

測試 4、5 需要 Purview 權限仍在有效期內。遙測結果在 Microsoft 365 系統管理中心 → [Agents](https://admin.cloud.microsoft/#/agents/all) → 該 agent → Activity 查看，DLP 紀錄在 Purview Audit。

## 常見問題

| 狀況 | 原因 | 處理 |
| --- | --- | --- |
| `az`、`uv`、`npx` 無法辨識 | 終端機是在安裝前開的 | 開一個新的終端機 |
| 終端機出現 `Agent 365 disabled; missing ...` | `.env` 少了 `A365_*` 設定 | 補上「建立 `mcp_agent/.env`」的設定 |
| 啟動時找不到金鑰檔 | 檔案位置跟 `A365_GCP_SA_KEY_FILE` 不同 | 把檔案放到設定的路徑 |
| `429 RESOURCE_EXHAUSTED` | Vertex AI 暫時限流 | 等 1–2 分鐘再試 |
| agent 說網路搜尋需要登入，或出現 `az could not get a user token` | `az login` 過期 | 重新 `az login` |
| 出現 `Purview uploadText check failed ... AADSTS65001` | Purview 權限過期或未授予 | 重新申請 access package；這時 agent 照常回答，只是不做 DLP 檢查 |
| agent 說 M365 尚未登入 | 讀信的登入過期 | 重做「登入 Microsoft 365 讀信」 |

## 安全注意事項

- **Google service account 金鑰檔等於 agent 的密碼**，拿到的人都能以 agent identity 登入。不要放進專案資料夾、不要 commit、不要轉傳；測試完成後刪除。`.gitignore` 會排除 `*-key.json`，但不要依賴這點
- `mcp_agent/.env` 與 `mcp_agent/.adk/`（對話紀錄，可能含信件內容）只留在本機，不會被 commit
- 遙測會把提問、回答、工具參數與結果送到 Agent 365，讀信時信件內容也會在紀錄裡。不想送出時，把 `ENABLE_A365_OBSERVABILITY_EXPORTER` 改為 `false`
