# Lagrou BOL Automation Service

## Project Summary

An end-to-end automation service that crawls shipment data from Lagrou Distribution's web portal and integrates it with Odoo 18 ERP in real-time, eliminating manual data entry for Bill of Lading (BOL) processing.

**Duration:** April 2026  
**Role:** Full-Stack Development (Backend + Frontend + DevOps)  
**Tech Stack:** Python, FastAPI, SQLite, BeautifulSoup, Odoo JSON-RPC, SSE, HTML/CSS/JS  
**Environment:** Linux (systemd user service), Internal Network

---

## Problem Statement

- InnoFoods ships products to Aldi through Lagrou Distribution warehouse
- Shipment information was manually checked on Lagrou's web portal, then hand-entered into Odoo ERP
- 70–100 BOL entries per month required manual matching of lot numbers, quantities, and delivery dates
- Manual process caused delays, data entry errors, and inconsistent records

## Solution

Automated pipeline: Web Scraping → Data Parsing → Odoo ERP Update — all triggered with a single click.

---

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Lagrou Portal   │────▶│  Ragro Service   │────▶│   Odoo 18 ERP   │
│ (Web Scraping)   │     │  (FastAPI/SSE)   │     │  (JSON-RPC API) │
└─────────────────┘     └──────┬───────────┘     └─────────────────┘
                               │
                        ┌──────┴───────┐
                        │   SQLite DB   │
                        │ - orders      │
                        │ - processed   │
                        │ - bill_details│
                        └──────────────┘
```

---

## Key Features

### 1. Automated Web Scraping (Lagrou Portal)
- Session-based login with cookie management
- Session caching with alive-check to avoid redundant logins
- SHIPDT sort optimization: reverse traversal from newest, early termination after 5 consecutive processed entries
- Current month first, previous month as secondary check

### 2. Odoo ERP Integration
- Sale Order lookup (exact match → partial match fallback)
- Stock Picking state validation and automatic processing
- Lot number matching + stock availability check + Move Line creation
- Automatic Picking validation (button_validate)
- Already-completed Pickings treated as "Already done" (success, not failure)
- Chatter note auto-posting
- Success/failure email notifications

### 3. Real-Time Sync Dashboard (SSE)
- Server-Sent Events for live progress streaming
- Progress bar + terminal-style log output
- Step-by-step visibility: Login → Order fetch → BL detail crawl → Odoo submission
- Odoo results (success/fail + SO number) displayed immediately

### 4. Web Dashboard
- InnoFoods branded UI
- Stats cards: Total / Success / Failed
- Searchable order table with status badges and BL links
- BL detail page with individual Odoo send/resend capability
- Built-in DB Console for direct SQL access

### 5. Smart State Management
- 3 SQLite tables for complete state tracking
- Processing status: success / failed with detailed reason storage
- BL detail caching: crawled details stored in DB for instant loading on subsequent views
- Failed entries handled via manual resend only (prevents failure email spam)

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Main dashboard (order list) |
| `/orders/sync` | GET | Sync execution with real-time progress UI |
| `/bills/{bl}` | GET | BL detail view + Odoo send button |
| `/bills/{bl}/send-odoo` | POST | Send individual BL to Odoo |
| `/update-bol` | POST | Update Odoo via BOL JSON payload |
| `/health` | GET | Service health check |
| `/db` | GET | SQL Console UI |
| `/db/query` | POST | Execute SQL query |

---

## Database Schema

### orders
| Column | Type | Description |
|--------|------|-------------|
| bill_of_lading | TEXT PK | BOL number |
| order_number | TEXT | Order number |
| po_number | TEXT | Purchase order number |
| consignee | TEXT | Consignee name |
| tracking_number | TEXT | Tracking/trailer info |
| ship_date | TEXT | Ship date |
| synced_at | TEXT | Last sync timestamp |

### processed_bills
| Column | Type | Description |
|--------|------|-------------|
| bill_of_lading | TEXT PK | BOL number |
| processed_date | TEXT | Processing date |
| processed_time | TEXT | Processing time |
| odoo_status | TEXT | success / failed |
| odoo_order | TEXT | Odoo SO number |
| odoo_detail | TEXT | Result detail / failure reason |

### bill_details
| Column | Type | Description |
|--------|------|-------------|
| bill_of_lading | TEXT PK | BOL number |
| order_number | TEXT | Order number |
| po_number | TEXT | Purchase order number |
| consignee | TEXT | Consignee name |
| route_via | TEXT | Shipping route |
| tracking_number | TEXT | Tracking info |
| date_shipped | TEXT | Ship date |
| total_lines | TEXT | Number of line items |
| total_units | TEXT | Total units |
| total_weight | TEXT | Total weight (lbs) |
| items_json | TEXT | Line item details (JSON) |
| crawled_at | TEXT | Crawl timestamp |

---

## Technical Highlights

### Crawling Optimization
- **Session reuse**: Alive-check before login, cached globally
- **Sort-based early termination**: SHIPDT sort + reverse traversal, skip remaining after 5 consecutive processed entries
- **Month prioritization**: Current month first, previous month as backup
- **Request throttling**: 1-second delay between requests to avoid bot detection

### Odoo Integration
- **Auto-reauthentication**: Automatic re-login on session expiry (2 retry attempts)
- **Graceful handling**: Already-validated Pickings return success ("Already done") instead of failure
- **Stock verification**: Available quantity check before shipment processing
- **Demand validation**: Prevents quantity exceeding demand

### Operations
- Registered as systemd user service (auto-start on boot)
- Session caching minimizes external service load
- BL detail DB caching eliminates redundant crawling

---

## Results

- **90%+ reduction** in processing time compared to manual workflow
- 70–100 monthly BOL entries automated with single-click sync
- Eliminated data entry errors (automated lot number and quantity matching)
- Real-time processing status monitoring
- Immediate failure detection with manual resend support
