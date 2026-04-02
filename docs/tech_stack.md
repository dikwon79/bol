# Tech Stack & Design Decisions

## Technology Stack

| Layer | Technology | Rationale |
|-------|-----------|-----------|
| **Backend** | Python 3.13 + FastAPI | Async support, native SSE streaming, auto API docs |
| **Web Scraping** | requests + BeautifulSoup | Optimal for parsing legacy ASP-based sites |
| **Database** | SQLite | Serverless, zero setup, ideal for single-service scope |
| **ERP Integration** | Odoo 18 JSON-RPC | Odoo standard API with session-based authentication |
| **Frontend** | Vanilla HTML/CSS/JS | Lightweight, no framework overhead, native SSE support |
| **Real-time** | Server-Sent Events (SSE) | Unidirectional streaming, simpler than WebSocket |
| **Deployment** | systemd user service | No sudo required, auto-restart, boot persistence |

## Design Decisions

### 1. FastAPI over Flask
The original service on server 12 was Flask-based; migrated to FastAPI for consolidation.
- Native SSE via StreamingResponse
- Async endpoints for concurrent request handling
- Automatic OpenAPI documentation

### 2. SQLite over PostgreSQL/MySQL
- Single-service, single-user environment
- Sub-100 records per month data volume
- No separate DB server to maintain
- Single-file backup and portability

### 3. SSE over WebSocket / Polling
Chosen for sync progress display:
- Only server → client communication needed
- Simpler implementation than WebSocket
- Browser-native EventSource API
- Built-in auto-reconnection

### 4. Session Caching Strategy
- **Lagrou**: Alive-check via data page access before reusing cached session
- **Odoo**: Global session with auto-reauthentication on RPC failure (2 attempts)
- Minimizes unnecessary login requests → avoids bot detection

### 5. Crawling Optimization: SHIPDT Sort + Reverse Traversal
- **Problem**: Iterating all 75 entries monthly is wasteful (most March entries already processed)
- **Solution**: Sort by Ship Date → traverse from newest → stop after 5 consecutive processed entries
- **Result**: When only new entries exist in the current month, nearly all previous month entries are skipped

### 6. State Management: success / failed (No Pending)
- Sync triggers crawl → immediate Odoo submission, so Pending state never exists
- Picking already in "done" state → treated as "Already done" success (not failure)
- Failed entries are not auto-retried to prevent repeated failure emails
- Manual resend available via BL detail page

### 7. BL Detail DB Caching
- Crawled details stored in bill_details table
- Subsequent BL views load from DB instantly (3–4 seconds → instant)
- Auto-saved during sync; also saved on individual BL views

## File Structure

```
ragro/
├── main.py              # FastAPI server (scraping + API + web UI)
├── odoo_bol.py          # Odoo integration module (auth, RPC, BOL update)
├── lagrou.db            # SQLite database
├── lagrou_credentials.txt
├── bol_app.py           # Original source from server 12 (reference)
├── bol_requirements.txt
└── docs/
    ├── project_overview.md   # Project summary & features
    ├── workflow_diagram.md   # Process flow diagrams
    └── tech_stack.md         # Tech stack & design rationale
```
