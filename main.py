from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, HTMLResponse
from starlette.responses import StreamingResponse
from datetime import datetime, date
import requests
import re
import os
import json
import time
import sqlite3
from bs4 import BeautifulSoup
from odoo_bol import update_bol as odoo_update_bol
import logging
from logging.handlers import RotatingFileHandler

app = FastAPI(title="Lagrou Order Scraper + BOL Validation API")

# ─── Logging ──────────────────────────────────────────
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger('ragro')
logger.setLevel(logging.INFO)
fh = RotatingFileHandler(os.path.join(LOG_DIR, 'ragro.log'), maxBytes=10*1024*1024, backupCount=5)
fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger.addHandler(fh)
logger.info('=== Ragro Service Started ===')

BASE_URL = "https://customers.lagrou.com"
CREDENTIALS = {
    "username": os.environ.get("LAGROU_USERNAME", ""),
    "password": os.environ.get("LAGROU_PASSWORD", ""),
}
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DATA_DIR, "lagrou.db")


# ─── DB ───────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            bill_of_lading TEXT PRIMARY KEY,
            order_number TEXT,
            po_number TEXT,
            consignee TEXT,
            tracking_number TEXT,
            ship_date TEXT,
            synced_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS processed_bills (
            bill_of_lading TEXT PRIMARY KEY,
            processed_date TEXT,
            processed_time TEXT,
            odoo_status TEXT DEFAULT '',
            odoo_order TEXT DEFAULT '',
            odoo_detail TEXT DEFAULT ''
        )
    """)
    # 기존 테이블에 컬럼 없으면 추가
    try:
        conn.execute("ALTER TABLE processed_bills ADD COLUMN odoo_status TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE processed_bills ADD COLUMN odoo_order TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE processed_bills ADD COLUMN odoo_detail TEXT DEFAULT ''")
    except Exception:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bill_details (
            bill_of_lading TEXT PRIMARY KEY,
            order_number TEXT,
            po_number TEXT,
            consignee TEXT,
            route_via TEXT,
            tracking_number TEXT,
            date_shipped TEXT,
            total_lines TEXT,
            total_units TEXT,
            total_weight TEXT,
            items_json TEXT,
            crawled_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_bill_detail_to_db(detail: dict):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO bill_details (bill_of_lading, order_number, po_number, consignee, route_via, tracking_number, date_shipped, total_lines, total_units, total_weight, items_json, crawled_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (detail["bill_of_lading"], detail["order_number"], detail["po_number"], detail["consignee"],
         detail["route_via"], detail["tracking_number"], detail["date_shipped"], detail["total_lines"],
         detail["total_units"], detail["total_weight"], json.dumps(detail["items"], ensure_ascii=False),
         datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_bill_detail_from_db(bl: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM bill_details WHERE bill_of_lading = ?", (bl.strip(),)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["items"] = json.loads(d.get("items_json", "[]"))
    return d


def save_orders_to_db(orders: list[dict]):
    now = datetime.now().isoformat()
    conn = sqlite3.connect(DB_PATH)
    for o in orders:
        conn.execute(
            "INSERT OR REPLACE INTO orders (bill_of_lading, order_number, po_number, consignee, tracking_number, ship_date, synced_at) VALUES (?,?,?,?,?,?,?)",
            (o["bill_of_lading"].strip(), o["order_number"].strip(), o["po_number"].strip(),
             o["consignee"].strip(), o["tracking_number"].strip(), o["ship_date"].strip(), now),
        )
    conn.commit()
    conn.close()


def get_orders_from_db() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM orders ORDER BY ship_date DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_bill_status(bl: str) -> dict | None:
    """processed_bills에서 상태 조회. 없으면 None."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM processed_bills WHERE bill_of_lading = ?", (bl.strip(),)).fetchone()
    conn.close()
    return dict(row) if row else None


def is_processed(bl: str) -> bool:
    return get_bill_status(bl) is not None


def mark_processed(bl: str, odoo_status: str = "", odoo_order: str = "", odoo_detail: str = ""):
    now = datetime.now()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO processed_bills (bill_of_lading, processed_date, processed_time, odoo_status, odoo_order, odoo_detail) VALUES (?, ?, ?, ?, ?, ?)",
        (bl.strip(), now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), odoo_status, odoo_order, odoo_detail),
    )
    conn.commit()
    conn.close()


# ─── 크롤링 ──────────────────────────────────────────
_cached_session = None


def get_default_passdates():
    today = date.today()
    current = f"{today.month}/1/{today.year}"
    if today.month == 1:
        prev = f"12/1/{today.year - 1}"
    else:
        prev = f"{today.month - 1}/1/{today.year}"
    return [prev, current]


def is_session_alive(session: requests.Session) -> bool:
    """세션이 살아있는지 데이터 페이지로 확인"""
    try:
        resp = session.get(f"{BASE_URL}/invcloseorder.asp", params={"passdate": "1/1/2000"}, timeout=5)
        return "Shipped Order Listing" in resp.text
    except Exception:
        return False


def login() -> requests.Session:
    global _cached_session
    if _cached_session and is_session_alive(_cached_session):
        return _cached_session

    session = requests.Session()
    login_data = {
        "username": CREDENTIALS["username"].upper(),
        "password": CREDENTIALS["password"].upper(),
        "B1": "Log In",
    }
    session.post(f"{BASE_URL}/access.asp", data=login_data)

    # SHIPDT 정렬 설정 (세션 동안 유지됨)
    time.sleep(1)
    session.post(f"{BASE_URL}/updcokcloseorder.asp?passdate=1/1/2000", data={"chgsort": "shipdt"})

    _cached_session = session
    logger.info("Lagrou login successful")
    return session


def fetch_orders(session: requests.Session, passdate: str) -> list[dict]:
    resp = session.get(f"{BASE_URL}/invcloseorder.asp", params={"passdate": passdate})
    resp.encoding = "windows-1252"
    soup = BeautifulSoup(resp.text, "html.parser")
    headers = ["bill_of_lading", "order_number", "po_number", "consignee", "tracking_number", "ship_date"]

    orders = []
    for tr in soup.find_all("tr", bgcolor=re.compile(r"FFFFCC|white")):
        cells = tr.find_all("td")
        if len(cells) == 6:
            values = [cell.get_text(strip=True) for cell in cells]
            orders.append(dict(zip(headers, values)))
    return orders


def fetch_new_orders(session: requests.Session, passdate: str, stop_after: int = 5) -> tuple[list[dict], int]:
    """
    SHIPDT 정렬된 목록을 뒤에서부터(최신) 확인.
    이미 처리된 BL이 연속 stop_after개 나오면 중단.
    Returns: (미처리 주문 목록, 스킵한 수)
    """
    all_orders = fetch_orders(session, passdate)
    new_orders = []
    skipped = 0
    consecutive_processed = 0

    # 뒤에서부터 (최신 ship date부터)
    for o in reversed(all_orders):
        bl = o["bill_of_lading"].strip()
        if is_processed(bl):
            consecutive_processed += 1
            skipped += 1
            if consecutive_processed >= stop_after:
                # 나머지는 전부 처리된 것으로 간주
                skipped += len(all_orders) - skipped - len(new_orders)
                break
        else:
            consecutive_processed = 0
            new_orders.append(o)

    # DB 저장용으로 전체 목록도 저장
    save_orders_to_db(all_orders)
    logger.info(f"Synced {len(all_orders)} orders from {passdate} to DB")

    return new_orders, skipped, len(all_orders)


def fetch_all_orders(session: requests.Session, passdates: list[str]) -> list[dict]:
    all_orders = []
    for pd in passdates:
        all_orders.extend(fetch_orders(session, pd))
        if pd != passdates[-1]:
            time.sleep(1)
    return all_orders


def fetch_bill_detail(session: requests.Session, bl: str) -> dict:
    resp = session.get(f"{BASE_URL}/closebill.asp", params={"bl": bl})
    resp.encoding = "windows-1252"
    soup = BeautifulSoup(resp.text, "html.parser")

    info = {}
    tables = soup.find_all("table")
    if len(tables) >= 2:
        for tr in tables[0].find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) == 2:
                key = cells[0].get_text(strip=True)
                val = cells[1].get_text(strip=True)
                if key:
                    info[key] = val
        for tr in tables[1].find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) == 2:
                key = cells[0].get_text(strip=True)
                val = cells[1].get_text(strip=True)
                if key:
                    info[key] = val

    items = []
    item_headers = ["quantity", "item", "lot_number", "description", "weight"]
    last_table = tables[-1] if tables else None
    if last_table:
        data_rows = [tr for tr in last_table.find_all("tr") if not tr.find("u")]
        for tr in data_rows:
            cells = tr.find_all("td")
            if len(cells) == 5:
                values = [c.get_text(strip=True) for c in cells]
                items.append(dict(zip(item_headers, values)))

    return {
        "bill_of_lading": info.get("Bill of Lading Number", bl).strip(),
        "order_number": info.get("Order Number", "").strip(),
        "po_number": info.get("Purchase Order Number", "").strip(),
        "consignee": info.get("Consignee", "").strip(),
        "route_via": info.get("Route via", "").strip(),
        "tracking_number": info.get("Tracking Number", "").strip(),
        "date_shipped": info.get("Date Shipped", "").strip(),
        "total_lines": info.get("Total Lines", "").strip(),
        "total_units": info.get("Total Units", "").strip(),
        "total_weight": info.get("Total Weight", "").strip(),
        "items": items,
    }


# ─── API ──────────────────────────────────────────────
@app.on_event("startup")
def startup():
    init_db()


@app.get("/api")
def api_root():
    return {
        "service": "Lagrou Order Scraper + BOL Validation",
        "endpoints": {
            "/orders/data": "GET - DB에서 주문 목록 보기",
            "/orders/sync": "GET - Lagrou 크롤링 → DB 동기화 + Odoo 업데이트 (실시간 진행)",
            "/bills/{bl}": "GET - 특정 BL 상세 (실시간 크롤링)",
            "/update-bol": "POST - BOL 데이터로 Odoo 업데이트 (JSON)",
            "/health": "GET - 서비스 상태",
        },
    }


@app.get("/health")
def health():
    return {"status": "healthy", "service": "lagrou-bol-service", "port": 8200}


@app.post("/update-bol")
async def update_bol_endpoint(request: Request):
    """BOL 데이터로 Odoo 업데이트 — 12번 서버와 동일 인터페이스"""
    data = await request.json()
    if not data:
        return JSONResponse({"success": False, "error": "No JSON data provided"}, status_code=400)

    customer_po = data.get("customer_po", "").strip()
    items = data.get("items", [])
    time_out = data.get("time_out", "").strip()

    result = odoo_update_bol(customer_po=customer_po, items=items, time_out=time_out)
    return JSONResponse(result)


@app.get("/", response_class=HTMLResponse)
@app.get("/orders/data", response_class=HTMLResponse)
def data_orders():
    """DB에서만 읽어서 보여줌 — 크롤링 없음"""
    orders = get_orders_from_db()

    total = len(orders)

    success_count = 0
    failed_count = 0

    rows = ""
    for i, o in enumerate(orders, 1):
        bill_info = get_bill_status(o["bill_of_lading"])
        if bill_info:
            odoo_st = bill_info.get("odoo_status", "")
            odoo_ord = bill_info.get("odoo_order", "")
            if odoo_st == "success":
                status = f'<span class="badge done">Success</span>'
                if odoo_ord:
                    status += f' <span style="color:#888;font-size:11px;">{odoo_ord}</span>'
                success_count += 1
            else:
                status = f'<span class="badge failed">Failed</span>'
                failed_count += 1
        else:
            status = ''
        rows += f"""<tr>
            <td>{i}</td>
            <td><a href="/bills/{o['bill_of_lading']}" class="bl-link">{o['bill_of_lading']}</a></td>
            <td>{o['order_number']}</td>
            <td>{o['po_number']}</td>
            <td>{o['consignee']}</td>
            <td class="tracking">{o['tracking_number']}</td>
            <td>{o['ship_date']}</td>
            <td>{status}</td>
        </tr>"""

    empty_msg = ""
    if not orders:
        empty_msg = """<div class="empty-state">
            <div style="font-size:48px; margin-bottom:15px;">📦</div>
            <h3>데이터가 없습니다</h3>
            <p>동기화를 실행하여 Lagrou에서 주문 데이터를 가져오세요.</p>
            <a class="btn primary" href="/orders/sync">동기화 실행</a>
        </div>"""

    synced_at = ""
    if orders:
        synced_at = orders[0].get("synced_at", "")[:19].replace("T", " ")

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<title>InnoFoods - Lagrou Warehouse</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #f0f2f5; color: #333; }}

    /* Header */
    .header {{
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
        padding: 20px 40px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        box-shadow: 0 2px 10px rgba(0,0,0,0.3);
        }}
    .header-left {{ display: flex; align-items: center; gap: 20px; }}
    .header img {{ height: 45px; }}
    .header h1 {{ color: #fff; font-size: 20px; font-weight: 400; }}
    .header h1 span {{ color: #e74c3c; font-weight: 700; }}
    .header-right {{ display: flex; align-items: center; gap: 12px; }}
    .sync-time {{ color: #8892a0; font-size: 12px; }}

    /* Stats Cards */
    .stats {{
        display: flex;
        gap: 20px;
        padding: 15px 40px;
        
        flex-wrap: wrap;
    }}
    .stat-card {{
        flex: 1;
        min-width: 200px;
        background: white;
        border-radius: 12px;
        padding: 22px 25px;
        box-shadow: 0 1px 6px rgba(0,0,0,0.06);
        border-left: 4px solid #ddd;
    }}
    .stat-card.total {{ border-left-color: #3498db; }}
    .stat-card.done {{ border-left-color: #27ae60; }}
    .stat-card.pending {{ border-left-color: #e74c3c; }}
    .stat-card .number {{ font-size: 32px; font-weight: 700; color: #1a1a2e; }}
    .stat-card .label {{ font-size: 13px; color: #8892a0; margin-top: 4px; }}

    /* Content */
    .content {{ padding: 0 40px 40px; }}
    .toolbar {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 15px;
        }}
    .toolbar h2 {{ font-size: 18px; color: #1a1a2e; }}

    /* Buttons */
    .btn {{
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 10px 22px;
        border-radius: 8px;
        text-decoration: none;
        font-size: 14px;
        font-weight: 600;
        transition: all 0.2s;
        border: none;
        cursor: pointer;
    }}
    .btn.primary {{
        background: linear-gradient(135deg, #e74c3c, #c0392b);
        color: white;
        box-shadow: 0 2px 8px rgba(231,76,60,0.3);
    }}
    .btn.primary:hover {{
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(231,76,60,0.4);
    }}

    /* Table */
    .table-wrap {{
        background: white;
        border-radius: 12px;
        overflow: auto;
        max-height: 60vh;
        box-shadow: 0 1px 6px rgba(0,0,0,0.06);
    }}
    table {{ width: 100%; border-collapse: collapse; }}
    thead th {{
        position: sticky; top: 0; z-index: 10;
        background: #1a1a2e;
        color: #ccc;
        padding: 14px 15px;
        text-align: left;
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        font-weight: 600;
    }}
    tbody td {{
        padding: 12px 15px;
        border-bottom: 1px solid #f0f0f0;
        font-size: 13px;
    }}
    tbody tr:hover {{ background: #f8f9fb; }}
    tbody tr:last-child td {{ border-bottom: none; }}

    .bl-link {{
        color: #2980b9;
        text-decoration: none;
        font-weight: 600;
    }}
    .bl-link:hover {{ color: #e74c3c; text-decoration: underline; }}
    .tracking {{ font-size: 12px; color: #666; }}

    /* Badge */
    .badge {{
        display: inline-block;
        padding: 4px 12px;
        border-radius: 20px;
        font-size: 11px;
        font-weight: 600;
        text-transform: uppercase;
    }}
    .badge.done {{ background: #eafaf1; color: #27ae60; }}
    .badge.failed {{ background: #fef0f0; color: #e74c3c; }}

    /* Empty State */
    .empty-state {{
        text-align: center;
        padding: 60px 20px;
        background: white;
        border-radius: 12px;
        box-shadow: 0 1px 6px rgba(0,0,0,0.06);
    }}
    .empty-state h3 {{ margin-bottom: 8px; color: #333; }}
    .empty-state p {{ color: #888; margin-bottom: 20px; }}

    /* Search */
    .search-box {{
        padding: 8px 14px;
        border: 1px solid #ddd;
        border-radius: 8px;
        font-size: 13px;
        width: 250px;
        outline: none;
        transition: border 0.2s;
    }}
    .search-box:focus {{ border-color: #3498db; }}

    /* Pagination */
    .pagination {{ margin-top: 15px; display: flex; align-items: center; justify-content: center; gap: 4px; flex-wrap: wrap; }}
    .pagination a {{ display: inline-flex; align-items: center; justify-content: center; min-width: 36px; height: 36px; padding: 0 10px; background: white; border: 1px solid #ddd; border-radius: 6px; color: #333; text-decoration: none; font-size: 13px; cursor: pointer; transition: all 0.2s; }}
    .pagination a:hover {{ background: #e74c3c; color: white; border-color: #e74c3c; }}
    .pagination a.active {{ background: #1a1a2e; color: white; border-color: #1a1a2e; }}
    .pagination .dots {{ color: #888; padding: 0 4px; }}
    .pagination .page-info {{ color: #888; font-size: 12px; margin-left: 10px; }}
</style>
</head><body>

<div class="header">
    <div class="header-left">
        <img src="https://innofoods.shop/wp-content/uploads/2025/04/innofoods_logo_white-scaled.png" alt="InnoFoods">
        <h1><span>Lagrou</span> Warehouse Management</h1>
    </div>
    <div class="header-right">
        <span class="sync-time">Last sync: {synced_at if synced_at else 'Never'}</span>
        <a class="btn primary" href="/orders/sync">Sync Now</a>
    </div>
</div>

<div class="stats">
    <div class="stat-card total">
        <div class="number">{total}</div>
        <div class="label">Total Orders</div>
    </div>
    <div class="stat-card done">
        <div class="number">{success_count}</div>
        <div class="label">Success</div>
    </div>
    <div class="stat-card pending" style="border-left-color:#e74c3c;">
        <div class="number">{failed_count}</div>
        <div class="label">Failed</div>
    </div>
</div>

<div class="content">
    {empty_msg}
    <div class="toolbar">
        <h2>Shipped Orders</h2>
        <input type="text" class="search-box" id="search" placeholder="Search orders..." oninput="filterTable()">
    </div>
    <div class="table-wrap">
    <table>
    <thead>
    <tr>
        <th>#</th><th>Bill of Lading</th><th>Order Number</th><th>P.O. Number</th>
        <th>Consignee</th><th>Tracking Number</th><th>Ship Date</th><th>Status</th>
    </tr>
    </thead>
    <tbody id="orderBody">
    {rows}
    </tbody>
    </table>
    </div>
</div>

<div class="pagination" id="pagination"></div>

<script>
const PAGE_SIZE = 20;
let currentPage = 1;
let allRows = [];
let filteredRows = [];

function init() {{
    const tbody = document.getElementById('orderBody');
    allRows = Array.from(tbody.getElementsByTagName('tr'));
    filteredRows = [...allRows];
    const p = parseInt(new URLSearchParams(location.search).get('page'));
    showPage(p > 0 ? p : 1);
}}

function showPage(page) {{
    currentPage = page;
    const url = new URL(location);
    url.searchParams.set('page', page);
    url.hash = '';
    history.replaceState(null, '', url);
    const start = (page - 1) * PAGE_SIZE;
    const end = start + PAGE_SIZE;

    allRows.forEach(r => r.style.display = 'none');
    filteredRows.slice(start, end).forEach(r => r.style.display = '');

    renderPagination();
}}

function renderPagination() {{
    const totalPages = Math.ceil(filteredRows.length / PAGE_SIZE);
    const el = document.getElementById('pagination');
    if (totalPages <= 1) {{ el.innerHTML = ''; return; }}

    let html = '';
    if (currentPage > 1) html += '<a onclick="showPage(' + (currentPage-1) + ')">&laquo; Prev</a>';

    const startP = Math.max(1, currentPage - 3);
    const endP = Math.min(totalPages, currentPage + 3);

    if (startP > 1) html += '<a onclick="showPage(1)">1</a><span class="dots">...</span>';
    for (let i = startP; i <= endP; i++) {{
        html += '<a onclick="showPage(' + i + ')"' + (i === currentPage ? ' class="active"' : '') + '>' + i + '</a>';
    }}
    if (endP < totalPages) html += '<span class="dots">...</span><a onclick="showPage(' + totalPages + ')">' + totalPages + '</a>';

    if (currentPage < totalPages) html += '<a onclick="showPage(' + (currentPage+1) + ')">Next &raquo;</a>';

    html += '<span class="page-info">(' + filteredRows.length + ' total)</span>';
    el.innerHTML = html;
}}

function filterTable() {{
    const q = document.getElementById('search').value.toLowerCase();
    if (q === '') {{
        filteredRows = [...allRows];
    }} else {{
        filteredRows = allRows.filter(r => r.textContent.toLowerCase().includes(q));
    }}
    showPage(1);
}}

init();

window.addEventListener("pageshow", function(evt) {{
    if (evt.persisted || (window.performance && window.performance.getEntriesByType("navigation")[0].type === "back_forward")) {{
        location.reload();
    }}
}});
</script>
</body></html>"""


@app.get("/orders/sync", response_class=HTMLResponse)
def sync_orders_page():
    """동기화 진행 화면 (SSE로 실시간 진행 표시)"""
    return """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>Lagrou - 동기화 진행중</title>
<style>
    body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
    h2 { color: #333; }
    .container { max-width: 900px; margin: 0 auto; }
    .status { background: white; border-radius: 8px; padding: 20px; box-shadow: 0 1px 4px rgba(0,0,0,0.1); margin-bottom: 15px; }
    .progress-bar { width: 100%; height: 30px; background: #ddd; border-radius: 15px; overflow: hidden; margin: 15px 0; }
    .progress-fill { height: 100%; background: linear-gradient(90deg, #c0392b, #e74c3c); transition: width 0.3s; width: 0%; display: flex; align-items: center; justify-content: center; color: white; font-size: 13px; font-weight: bold; }
    .log { background: #1e1e1e; color: #0f0; border-radius: 8px; padding: 15px; font-family: monospace; font-size: 13px; max-height: 500px; overflow-y: auto; }
    .log-line { margin: 3px 0; }
    .log-line.skip { color: #888; }
    .log-line.fetch { color: #3498db; }
    .log-line.done { color: #2ecc71; }
    .log-line.error { color: #e74c3c; }
    .log-line.info { color: #f39c12; }
    .summary { display: none; background: white; border-radius: 8px; padding: 20px; box-shadow: 0 1px 4px rgba(0,0,0,0.1); margin-top: 15px; }
    .summary h3 { color: #27ae60; }
    .btn { display: inline-block; margin: 10px 5px; padding: 10px 20px; background: #c0392b; color: white; text-decoration: none; border-radius: 5px; }
    .btn:hover { background: #a93226; }
    .btn.green { background: #27ae60; }
    .btn.green:hover { background: #219a52; }
</style>
</head><body>
<div class="container">
    <h2>Lagrou 동기화</h2>
    <div class="status">
        <div id="statusText">시작 중...</div>
        <div class="progress-bar"><div class="progress-fill" id="progressFill">0%</div></div>
        <div>처리: <b id="processed">0</b> | 스킵: <b id="skipped">0</b> | 총: <b id="total">-</b></div>
    </div>
    <div class="log" id="log"></div>
    <div class="summary" id="summary">
        <h3>동기화 완료!</h3>
        <p id="summaryText"></p>
        <a class="btn" href="/">주문 목록</a>
    </div>
</div>
<script>
    const log = document.getElementById('log');
    const es = new EventSource('/orders/sync/stream');
    let total = 0, processed = 0, skipped = 0;

    function addLog(msg, cls) {
        const div = document.createElement('div');
        div.className = 'log-line ' + (cls || '');
        div.textContent = msg;
        log.appendChild(div);
        log.scrollTop = log.scrollHeight;
    }

    es.onmessage = function(e) {
        const d = JSON.parse(e.data);

        if (d.type === 'login') {
            document.getElementById('statusText').textContent = 'Lagrou 로그인 완료';
            addLog('[LOGIN] ' + d.message, 'done');
        }
        else if (d.type === 'order_list') {
            total = d.total;
            document.getElementById('total').textContent = total;
            document.getElementById('statusText').textContent = d.passdates + ': ' + d.new + '건 신규, ' + d.skipped + '건 스킵';
            addLog('[INFO] ' + d.passdates + ' → 총 ' + total + '건, 신규 ' + d.new + '건, 스킵 ' + d.skipped + '건', 'info');
        }
        else if (d.type === 'info') {
            addLog('[INFO] ' + d.message, 'info');
        }
        else if (d.type === 'fetch') {
            addLog('[FETCH] ' + d.bl + ' 크롤링 중...', 'fetch');
        }
        else if (d.type === 'done') {
            processed++;
            document.getElementById('processed').textContent = processed;
            const pct = Math.round((processed + skipped) / total * 100);
            document.getElementById('progressFill').style.width = pct + '%';
            document.getElementById('progressFill').textContent = pct + '%';
            var odooTag = d.odoo === 'success' ? ' | Odoo: ✓ ' + d.odoo_order : ' | Odoo: ✗ ' + (d.odoo_order || '');
            addLog('[DONE] ' + d.bl + ' → ' + d.items + '개 아이템, ' + d.weight + ' lbs' + odooTag, 'done');
        }
        else if (d.type === 'complete') {
            document.getElementById('statusText').textContent = '동기화 완료!';
            document.getElementById('progressFill').style.width = '100%';
            document.getElementById('progressFill').textContent = '100%';
            document.getElementById('summaryText').textContent =
                '총 ' + d.total + '건 중 신규 ' + d.newly_processed + '건 처리, ' + d.skipped + '건 스킵';
            document.getElementById('summary').style.display = 'block';
            addLog('[COMPLETE] 동기화 완료! 신규 ' + d.newly_processed + '건', 'done');
            es.close();
        }
        else if (d.type === 'error') {
            addLog('[ERROR] ' + d.message, 'error');
        }
    };
    es.onerror = function() { addLog('[ERROR] 연결 끊김', 'error'); es.close(); };
</script>
</body></html>"""


@app.get("/orders/sync/stream")
def sync_orders_stream():
    """SSE 스트림: 당월 먼저 → 전월 순서로 크롤링. SHIPDT 정렬 후 최신부터 역순 체크."""
    def generate():
        try:
            session = login()
            yield f"data: {json.dumps({'type': 'login', 'message': '로그인 성공 (SHIPDT 정렬)'})}\n\n"
            time.sleep(2)

            passdates = get_default_passdates()
            # 당월 먼저, 전월 나중에
            passdates_ordered = list(reversed(passdates))

            total_all = 0
            new_count = 0
            skipped_all = 0

            for pd in passdates_ordered:
                yield f"data: {json.dumps({'type': 'info', 'message': f'{pd} 조회 중...'})}\n\n"
                new_orders, skipped, page_total = fetch_new_orders(session, pd)
                total_all += page_total
                skipped_all += skipped

                yield f"data: {json.dumps({'type': 'order_list', 'total': total_all, 'passdates': pd, 'new': len(new_orders), 'skipped': skipped})}\n\n"

                if not new_orders:
                    yield f"data: {json.dumps({'type': 'info', 'message': f'{pd}: 미처리 건 없음 ({skipped}건 스킵)'})}\n\n"
                    time.sleep(1)
                    continue

                for o in new_orders:
                    bl = o["bill_of_lading"].strip()
                    yield f"data: {json.dumps({'type': 'fetch', 'bl': bl})}\n\n"
                    detail = fetch_bill_detail(session, bl)
                    save_bill_detail_to_db(detail)
                    logger.info(f"Crawled BL {bl}: {detail.get('total_lines')} items, {detail.get('total_weight')} lbs")

                    # Odoo 업데이트
                    odoo_items = [{"item": it["item"], "lot_number": it["lot_number"], "quantity": it["quantity"]} for it in detail.get("items", [])]
                    odoo_result = odoo_update_bol(customer_po=detail["po_number"], items=odoo_items, ship_date=detail["date_shipped"])
                    odoo_order = odoo_result.get("order_name", "")
                    if odoo_result.get("already_done"):
                        odoo_ok = "success"
                        odoo_detail_str = "Already done"
                    elif odoo_result.get("failure_reasons"):
                        odoo_ok = "failed"
                        odoo_detail_str = "; ".join(odoo_result["failure_reasons"])
                    else:
                        odoo_ok = "success"
                        odoo_detail_str = "OK"

                    mark_processed(bl, odoo_status=odoo_ok, odoo_order=odoo_order, odoo_detail=odoo_detail_str)
                    logger.info(f"Odoo update BL {bl}: {odoo_ok} | {odoo_order} | {odoo_detail_str}")
                    new_count += 1

                    yield f"data: {json.dumps({'type': 'done', 'bl': bl, 'items': detail['total_lines'], 'weight': detail['total_weight'], 'odoo': odoo_ok, 'odoo_order': odoo_order})}\n\n"
                    time.sleep(1)

                time.sleep(1)

            yield f"data: {json.dumps({'type': 'complete', 'total': total_all, 'newly_processed': new_count, 'skipped': skipped_all})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/bills/{bl}", response_class=HTMLResponse)
def view_bill_detail(bl: str):
    """특정 BL 상세 — DB에 있으면 바로, 없으면 크롤링"""
    detail = get_bill_detail_from_db(bl)
    if not detail or not detail.get("items"):
        session = login()
        time.sleep(2)
        detail = fetch_bill_detail(session, bl)
        save_bill_detail_to_db(detail)

    # 현재 처리 상태
    bill_info = get_bill_status(bl)
    if bill_info:
        st = bill_info.get("odoo_status", "")
        if st == "success":
            status_html = f'<span style="background:#eafaf1;color:#27ae60;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:600;">SUCCESS — {bill_info.get("odoo_order","")}</span>'
        elif st == "failed":
            status_html = f'<span style="background:#fef0f0;color:#e74c3c;padding:4px 12px;border-radius:20px;font-size:12px;font-weight:600;">FAILED</span> <span style="color:#888;font-size:12px;">{bill_info.get("odoo_detail","")}</span>'
        else:
            status_html = '<span style="color:#888;font-size:12px;">처리됨 (Odoo 상태 없음)</span>'
    else:
        status_html = '<span style="color:#f39c12;font-size:12px;">미처리</span>'

    item_rows = ""
    for item in detail.get("items", []):
        item_rows += f"<tr><td>{item['quantity']}</td><td>{item['item']}</td><td>{item['lot_number']}</td><td>{item['description']}</td><td>{item['weight']}</td></tr>"

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>BL #{detail['bill_of_lading']}</title>
<style>
    body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
    .card {{ background: white; border-radius: 8px; padding: 20px; max-width: 900px; margin: 0 auto; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
    h2 {{ color: #c0392b; }}
    .field {{ margin: 5px 0; }}
    .label {{ font-weight: bold; display: inline-block; width: 180px; color: #333; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 15px; }}
    th {{ background-color: #34495e; color: white; padding: 10px; text-align: left; }}
    td {{ padding: 8px; border-bottom: 1px solid #ddd; }}
    a {{ color: #2980b9; }}
    .back {{ margin-bottom: 15px; display: block; }}
    .odoo-btn {{ display: inline-block; margin-top: 15px; padding: 10px 24px; background: linear-gradient(135deg, #8e44ad, #6c3483); color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 600; }}
    .odoo-btn:hover {{ transform: translateY(-1px); box-shadow: 0 4px 12px rgba(142,68,173,0.4); }}
    .odoo-btn:disabled {{ background: #999; cursor: not-allowed; transform: none; box-shadow: none; }}
    .odoo-result {{ margin-top: 12px; padding: 12px; border-radius: 6px; font-size: 13px; display: none; }}
    .odoo-result.ok {{ background: #eafaf1; color: #27ae60; border: 1px solid #27ae60; }}
    .odoo-result.fail {{ background: #fef0f0; color: #e74c3c; border: 1px solid #e74c3c; }}
    .status-row {{ margin: 10px 0; }}
</style>
</head><body>
<div class="card">
    <a class="back" href="javascript:history.back()">← 주문 목록으로</a>
    <h2>Bill of Lading #{detail['bill_of_lading']}</h2>
    <div class="status-row">Odoo 상태: {status_html}</div>
    <div class="field"><span class="label">Order Number:</span> {detail['order_number']}</div>
    <div class="field"><span class="label">Purchase Order:</span> {detail['po_number']}</div>
    <div class="field"><span class="label">Consignee:</span> {detail['consignee']}</div>
    <div class="field"><span class="label">Route via:</span> {detail['route_via']}</div>
    <div class="field"><span class="label">Tracking Number:</span> {detail['tracking_number']}</div>
    <div class="field"><span class="label">Date Shipped:</span> {detail['date_shipped']}</div>
    <div class="field"><span class="label">Total Lines:</span> {detail['total_lines']}</div>
    <div class="field"><span class="label">Total Units:</span> {detail['total_units']}</div>
    <div class="field"><span class="label">Total Weight:</span> {detail['total_weight']}</div>
    <table>
        <tr><th>Quantity</th><th>Item</th><th>Lot Number</th><th>Description</th><th>Weight</th></tr>
        {item_rows}
    </table>
    <button class="odoo-btn" id="odooBtn" onclick="sendToOdoo()">Odoo 전송</button>
    <div class="odoo-result" id="odooResult"></div>
</div>
<script>
async function sendToOdoo() {{
    const btn = document.getElementById('odooBtn');
    const res = document.getElementById('odooResult');
    btn.disabled = true;
    btn.textContent = '전송 중...';
    res.style.display = 'none';

    try {{
        const resp = await fetch('/bills/{detail["bill_of_lading"].strip()}/send-odoo', {{ method: 'POST' }});
        const data = await resp.json();

        res.style.display = 'block';
        if (data.odoo_status === 'success') {{
            res.className = 'odoo-result ok';
            res.innerHTML = '<b>성공!</b> ' + (data.odoo_order || '') + ' — ' + (data.odoo_detail || '');

        }} else {{
            res.className = 'odoo-result fail';
            res.innerHTML = '<b>실패:</b> ' + (data.odoo_detail || data.error || 'Unknown error');
        }}
        btn.textContent = 'Odoo 재전송';
        btn.disabled = false;
    }} catch(e) {{
        res.style.display = 'block';
        res.className = 'odoo-result fail';
        res.innerHTML = '<b>에러:</b> ' + e.message;
        btn.textContent = 'Odoo 전송';
        btn.disabled = false;
    }}
}}
</script>
</body></html>"""


@app.post("/bills/{bl}/send-odoo")
def send_bill_to_odoo(bl: str):
    """개별 BL을 Odoo로 전송"""
    detail = get_bill_detail_from_db(bl)
    if not detail or not detail.get("items"):
        session = login()
        time.sleep(2)
        detail = fetch_bill_detail(session, bl)
        save_bill_detail_to_db(detail)

    odoo_items = [{"item": it["item"], "lot_number": it["lot_number"], "quantity": it["quantity"]} for it in detail.get("items", [])]
    odoo_result = odoo_update_bol(customer_po=detail["po_number"], items=odoo_items, ship_date=detail["date_shipped"])

    odoo_order = odoo_result.get("order_name", "")
    if odoo_result.get("already_done"):
        odoo_ok = "success"
        odoo_detail_str = "Already done"
    elif odoo_result.get("failure_reasons"):
        odoo_ok = "failed"
        odoo_detail_str = "; ".join(odoo_result["failure_reasons"])
    else:
        odoo_ok = "success"
        odoo_detail_str = "OK"

    mark_processed(bl, odoo_status=odoo_ok, odoo_order=odoo_order, odoo_detail=odoo_detail_str)
    logger.info(f"Manual Odoo send BL {bl}: {odoo_ok} | {odoo_order} | {odoo_detail_str}")

    return JSONResponse({"bl": bl, "odoo_status": odoo_ok, "odoo_order": odoo_order, "odoo_detail": odoo_detail_str})


@app.get("/db", response_class=HTMLResponse)
def db_console():
    """웹 SQL 콘솔"""
    return """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>DB Console - Lagrou</title>
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: 'Segoe UI', monospace; background: #1e1e1e; color: #d4d4d4; padding: 20px; }
    h2 { color: #569cd6; margin-bottom: 15px; }
    .toolbar { display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
    .toolbar button { padding: 6px 14px; background: #333; color: #9cdcfe; border: 1px solid #555; border-radius: 4px; cursor: pointer; font-size: 12px; }
    .toolbar button:hover { background: #444; }
    textarea { width: 100%; height: 120px; background: #2d2d2d; color: #ce9178; border: 1px solid #555; border-radius: 6px; padding: 12px; font-family: 'Consolas', monospace; font-size: 14px; resize: vertical; }
    .run-btn { margin: 10px 0; padding: 10px 30px; background: #0e639c; color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: bold; }
    .run-btn:hover { background: #1177bb; }
    .result { margin-top: 15px; }
    .result-info { color: #6a9955; margin-bottom: 8px; font-size: 13px; }
    .result-error { color: #f44747; margin-bottom: 8px; font-size: 13px; }
    table { width: 100%; border-collapse: collapse; background: #2d2d2d; border-radius: 6px; overflow: hidden; }
    th { background: #264f78; color: #9cdcfe; padding: 10px; text-align: left; font-size: 12px; text-transform: uppercase; }
    td { padding: 8px 10px; border-bottom: 1px solid #333; font-size: 13px; }
    tr:hover td { background: #333; }
    .empty { color: #888; padding: 20px; text-align: center; }
    a { color: #569cd6; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .nav { margin-bottom: 15px; }
</style>
</head><body>
<div class="nav"><a href="/">← Dashboard</a></div>
<h2>DB Console</h2>
<div class="toolbar">
    <button onclick="setQuery('SELECT * FROM orders ORDER BY ship_date DESC LIMIT 20')">Orders</button>
    <button onclick="setQuery('SELECT * FROM processed_bills ORDER BY processed_date DESC, processed_time DESC LIMIT 20')">Processed</button>
    <button onclick="setQuery('SELECT * FROM processed_bills WHERE odoo_status=\\'failed\\' ORDER BY processed_date DESC')">Failed</button>
    <button onclick="setQuery('SELECT bill_of_lading, order_number, po_number, consignee, date_shipped, total_units, total_weight, crawled_at FROM bill_details ORDER BY crawled_at DESC LIMIT 20')">Bill Details</button>
    <button onclick="setQuery('SELECT COUNT(*) as total, odoo_status FROM processed_bills GROUP BY odoo_status')">Stats</button>
    <button onclick="setQuery('SELECT name FROM sqlite_master WHERE type=\\'table\\'')">Tables</button>
</div>
<textarea id="sql" placeholder="SQL query...">SELECT * FROM processed_bills ORDER BY processed_date DESC LIMIT 20</textarea>
<br>
<button class="run-btn" onclick="runQuery()">Run Query</button>
<div class="result" id="result"></div>

<script>
function setQuery(q) { document.getElementById('sql').value = q; }

async function runQuery() {
    const sql = document.getElementById('sql').value.trim();
    if (!sql) return;
    const res = document.getElementById('result');
    res.innerHTML = '<div class="result-info">실행 중...</div>';

    try {
        const resp = await fetch('/db/query', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({sql: sql})
        });
        const data = await resp.json();

        if (data.error) {
            res.innerHTML = '<div class="result-error">ERROR: ' + data.error + '</div>';
            return;
        }

        let html = '<div class="result-info">' + data.message + '</div>';

        if (data.columns && data.rows) {
            html += '<table><tr>';
            data.columns.forEach(c => html += '<th>' + c + '</th>');
            html += '</tr>';
            if (data.rows.length === 0) {
                html += '<tr><td colspan="' + data.columns.length + '" class="empty">No results</td></tr>';
            }
            data.rows.forEach(row => {
                html += '<tr>';
                row.forEach(v => html += '<td>' + (v === null ? '<span style="color:#888">NULL</span>' : v) + '</td>');
                html += '</tr>';
            });
            html += '</table>';
        }

        res.innerHTML = html;
    } catch(e) {
        res.innerHTML = '<div class="result-error">ERROR: ' + e.message + '</div>';
    }
}

document.getElementById('sql').addEventListener('keydown', function(e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { runQuery(); }
});
</script>
</body></html>"""


@app.post("/db/query")
async def db_query(request: Request):
    """SQL 쿼리 실행"""
    data = await request.json()
    sql = (data.get("sql") or "").strip()
    if not sql:
        return JSONResponse({"error": "No SQL provided"})

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(sql)

        if sql.upper().startswith("SELECT") or sql.upper().startswith("PRAGMA"):
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            rows = cursor.fetchall()
            conn.close()
            return JSONResponse({"columns": columns, "rows": rows, "message": f"{len(rows)}건 조회됨"})
        else:
            conn.commit()
            affected = cursor.rowcount
            conn.close()
            return JSONResponse({"message": f"실행 완료. {affected}건 영향받음", "columns": [], "rows": []})

    except Exception as e:
        return JSONResponse({"error": str(e)})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8200)
