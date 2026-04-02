"""
Odoo BOL (Bill of Lading) Validation Module
- Odoo 인증 및 RPC 호출
- Sale Order / Picking / Lot 조회
- BOL 데이터로 Odoo 업데이트 + 피킹 검증
"""

import requests
import logging
import logging.handlers
import base64
import gzip
import os
from datetime import datetime
from io import BytesIO

logger = logging.getLogger('odoo_bol')
logger.setLevel(logging.INFO)
_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(_log_dir, exist_ok=True)
_fh = logging.handlers.RotatingFileHandler(os.path.join(_log_dir, 'odoo_bol.log'), maxBytes=10*1024*1024, backupCount=5)
_fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger.addHandler(_fh)

# ============================================
# ODOO CONFIGURATION
# ============================================
ODOO_URL = os.environ.get("ODOO_URL", "")
ODOO_DB = os.environ.get("ODOO_DB", "")
ODOO_USERNAME = os.environ.get("ODOO_USERNAME", "")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD", "")

EMAIL_SERVICE_URL = os.environ.get("EMAIL_SERVICE_URL", "")

# Global session
odoo_session = None
odoo_uid = None


# ============================================
# ODOO AUTHENTICATION
# ============================================
def odoo_authenticate():
    global odoo_session, odoo_uid
    try:
        session = requests.Session()
        resp = session.post(
            f"{ODOO_URL}/web/session/authenticate",
            json={"jsonrpc": "2.0", "method": "call", "params": {"db": ODOO_DB, "login": ODOO_USERNAME, "password": ODOO_PASSWORD}},
            timeout=30,
        )
        resp.raise_for_status()
        uid = resp.json().get("result", {}).get("uid")
        if uid:
            odoo_session = session
            odoo_uid = uid
            logger.info(f"Authenticated with Odoo as uid={uid}")
            return True
        logger.error("Odoo authentication failed - no uid")
        return False
    except Exception as e:
        logger.error(f"Odoo auth error: {e}")
        return False


def odoo_call_kw(model, method, args=None, kwargs=None):
    global odoo_session, odoo_uid
    if not odoo_session or not odoo_uid:
        if not odoo_authenticate():
            return None

    payload = {"jsonrpc": "2.0", "method": "call", "params": {"model": model, "method": method, "args": args or [], "kwargs": kwargs or {}}}

    for attempt in range(2):
        try:
            resp = odoo_session.post(f"{ODOO_URL}/web/dataset/call_kw/{model}/{method}", json=payload, timeout=30)
            resp.raise_for_status()
            result = resp.json()
            if "error" in result:
                error_msg = result["error"].get("message", "")
                logger.error(f"Odoo RPC error: {error_msg}")
                if attempt == 0 and "session" in error_msg.lower():
                    odoo_authenticate()
                    continue
                return None
            return result.get("result")
        except Exception as e:
            logger.error(f"Odoo RPC call error: {e}")
            if attempt == 0:
                odoo_authenticate()
                continue
            return None
    return None


# ============================================
# HELPERS
# ============================================
def parse_date(date_str):
    if not date_str:
        return None
    if isinstance(date_str, datetime):
        return date_str
    date_str = str(date_str).strip()
    formats = [
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y",
        "%d/%m/%Y", "%Y/%m/%d", "%B %d, %Y", "%b %d, %Y",
        "%m/%d/%Y %I:%M %p", "%m/%d/%y %I:%M %p",
        "%m/%d/%Y %H:%M", "%m/%d/%y %H:%M",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def find_sale_order_by_client_order_ref(customer_po):
    if not customer_po:
        return None
    fields = ["id", "name", "client_order_ref", "pickup_date", "pickup_delivery_date", "state", "partner_id"]
    orders = odoo_call_kw("sale.order", "search_read", args=[[("client_order_ref", "=", customer_po)]], kwargs={"fields": fields, "limit": 1})
    if orders:
        return orders[0]
    orders = odoo_call_kw("sale.order", "search_read", args=[[("client_order_ref", "ilike", customer_po)]], kwargs={"fields": fields, "limit": 1})
    if orders:
        return orders[0]
    return None


def find_lot(lot_name, product_id=None):
    if not lot_name:
        return None, None
    if product_id:
        lots = odoo_call_kw("stock.lot", "search_read", args=[[("name", "=", lot_name), ("product_id", "=", product_id)]], kwargs={"fields": ["id", "name", "product_id"], "limit": 1})
        if lots:
            return lots[0]["id"], product_id
    lots = odoo_call_kw("stock.lot", "search_read", args=[[("name", "=", lot_name)]], kwargs={"fields": ["id", "name", "product_id"], "limit": 1})
    if lots:
        lot_product_id = lots[0]["product_id"][0] if lots[0]["product_id"] else None
        return lots[0]["id"], lot_product_id
    return None, None


def find_picking_for_sale_order(order_name):
    pickings = odoo_call_kw("stock.picking", "search_read", args=[[("origin", "ilike", order_name)]], kwargs={"fields": ["id", "name", "origin", "state"], "order": "id desc", "limit": 5})
    if pickings:
        for p in pickings:
            if "/OUT/" in p["name"]:
                return p
        return pickings[0]
    return None


def get_moves_for_picking(picking_id):
    return odoo_call_kw("stock.move", "search_read", args=[[("picking_id", "=", picking_id)]], kwargs={"fields": ["id", "product_id", "product_uom_qty", "quantity", "product_uom"]}) or []


def get_move_lines_for_move(move_id):
    return odoo_call_kw("stock.move.line", "search_read", args=[[("move_id", "=", move_id)]], kwargs={"fields": ["id", "product_id", "lot_id", "lot_name", "quantity", "qty_done", "move_id", "picking_id", "location_id", "location_dest_id", "product_uom_id"]}) or []


def check_stock_available(product_id, lot_id, location_id, qty_needed):
    quants = odoo_call_kw("stock.quant", "search_read", args=[[("product_id", "=", product_id), ("lot_id", "=", lot_id), ("location_id", "=", location_id)]], kwargs={"fields": ["quantity", "reserved_quantity"], "limit": 1})
    if not quants:
        return 0, False, f"No stock found for this lot at location (id={location_id})"
    q = quants[0]
    available = q["quantity"] - q["reserved_quantity"]
    if available < qty_needed:
        return available, False, f"Insufficient stock: available={available}, needed={qty_needed}"
    return available, True, "OK"


def upload_attachment_to_odoo(picking_id, file_data, filename, compress=True):
    if not picking_id or not file_data:
        return None
    try:
        if compress:
            buf = BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
                gz.write(file_data)
            file_data = buf.getvalue()
            if not filename.endswith(".gz"):
                filename += ".gz"
        existing = odoo_call_kw("ir.attachment", "search_read", args=[[("res_model", "=", "stock.picking"), ("res_id", "=", picking_id), ("name", "=", filename)]], kwargs={"fields": ["id"], "limit": 1})
        if existing:
            return existing[0]["id"]
        return odoo_call_kw("ir.attachment", "create", args=[{"name": filename, "datas": base64.b64encode(file_data).decode(), "res_model": "stock.picking", "res_id": picking_id, "type": "binary"}])
    except Exception as e:
        logger.error(f"Attachment upload error: {e}")
        return None


# ============================================
# EMAIL
# ============================================
def send_email_notification(to_email, subject, html_body, cc_email=""):
    try:
        resp = requests.post(EMAIL_SERVICE_URL, json={"to_email": to_email, "cc_email": cc_email, "subject": subject, "html_body": html_body}, timeout=30)
        return resp.json().get("success", False)
    except Exception as e:
        logger.error(f"Email error: {e}")
        return False


def _send_failure_email(customer_po, reasons, order_name=""):
    subject = f"BOL Update Failed - {customer_po}"
    if order_name:
        subject += f" ({order_name})"
    reasons_html = "".join(f"<li>{r}</li>" for r in reasons)
    html = f"""<html><body style="font-family:Arial;padding:20px;">
    <h2>BOL Update Failed</h2>
    <div style="background:#dc3545;color:white;padding:15px;border-radius:5px;margin-bottom:20px;"><strong>Automatic update could not be completed</strong></div>
    <p><strong>Customer PO:</strong> {customer_po}</p>
    {"<p><strong>Sale Order:</strong> " + order_name + "</p>" if order_name else ""}
    <h3>Reasons</h3><ul>{reasons_html}</ul>
    <p style="color:#666;"><em>Manual intervention required.</em></p>
    </body></html>"""
    send_email_notification("sam.kwon@innofoods.ca,logistics@innofoods.ca,it@innofoods.ca", subject, html, cc_email="")


def _send_success_email(result, customer_po, order_name, picking_name):
    items_html = ""
    for lu in result.get("lot_updates", []):
        items_html += f"<tr><td style='border:1px solid #ddd;padding:8px;'>{lu.get('customer_sku','')}</td><td style='border:1px solid #ddd;padding:8px;'>{lu.get('bol_lot','')}</td><td style='border:1px solid #ddd;padding:8px;'>{lu.get('bol_quantity','')}</td><td style='border:1px solid #ddd;padding:8px;'>{lu.get('status','')}</td></tr>"
    html = f"""<html><body style="font-family:Arial;padding:20px;">
    <h2>BOL Update Successful</h2>
    <div style="background:#28a745;color:white;padding:15px;border-radius:5px;margin-bottom:20px;"><strong>BOL has been processed and validated</strong></div>
    <table style="border-collapse:collapse;"><tr><td style="border:1px solid #ddd;padding:8px;"><strong>Customer PO</strong></td><td style="border:1px solid #ddd;padding:8px;">{customer_po}</td></tr>
    <tr><td style="border:1px solid #ddd;padding:8px;"><strong>Sale Order</strong></td><td style="border:1px solid #ddd;padding:8px;">{order_name}</td></tr>
    <tr><td style="border:1px solid #ddd;padding:8px;"><strong>Picking</strong></td><td style="border:1px solid #ddd;padding:8px;">{picking_name}</td></tr></table>
    <h3>Items</h3><table style="border-collapse:collapse;width:100%;"><tr style="background:#f2f2f2;"><th style="border:1px solid #ddd;padding:8px;">SKU</th><th style="border:1px solid #ddd;padding:8px;">Lot</th><th style="border:1px solid #ddd;padding:8px;">Qty</th><th style="border:1px solid #ddd;padding:8px;">Status</th></tr>{items_html}</table>
    </body></html>"""
    send_email_notification("sam.kwon@innofoods.ca,logistics@innofoods.ca,it@innofoods.ca", f"BOL Update Successful - {customer_po} ({order_name})", html, cc_email="")


# ============================================
# MAIN: UPDATE BOL
# ============================================
def update_bol(customer_po, items=None, time_out="", ship_date=""):
    """
    Lagrou 크롤링 데이터로 Odoo 업데이트.
    Returns dict with success, order info, lot_updates, failure_reasons etc.
    """
    if not customer_po:
        return {"success": False, "error": "customer_po is required"}

    failure_reasons = []
    result = {"success": True, "customer_po": customer_po, "pickup_date_updated": False, "lot_updates": []}

    # 1. Find sale.order
    order = find_sale_order_by_client_order_ref(customer_po)
    if not order:
        failure_reasons.append(f"Sale order not found for customer PO: {customer_po}")
        _send_failure_email(customer_po, failure_reasons)
        return {"success": False, "found": False, "error": f"No sale.order for PO: {customer_po}", "failure_reasons": failure_reasons}

    order_id = order["id"]
    order_name = order["name"]
    result["order_id"] = order_id
    result["order_name"] = order_name

    # 2. Parse date
    date_str = time_out or ship_date
    odoo_datetime = None
    if date_str:
        parsed = parse_date(date_str)
        if parsed:
            try:
                from zoneinfo import ZoneInfo
            except ImportError:
                from backports.zoneinfo import ZoneInfo
            local_dt = parsed.replace(hour=14, minute=0, second=0, tzinfo=ZoneInfo("America/Vancouver")) if parsed.hour == 0 and parsed.minute == 0 else parsed.replace(tzinfo=ZoneInfo("America/Vancouver"))
            utc_dt = local_dt.astimezone(ZoneInfo("UTC"))
            odoo_datetime = utc_dt.strftime("%Y-%m-%d %H:%M:%S")
            result["time_out_display"] = date_str

    # 3. Find picking
    if not items:
        items = []

    picking = None
    if items:
        picking = find_picking_for_sale_order(order_name)
        if not picking:
            failure_reasons.append(f"No stock.picking found for {order_name}")
        else:
            result["picking_id"] = picking["id"]
            result["picking_name"] = picking["name"]

            if picking.get("state") == "done":
                result["already_done"] = True
                return result

            # Update act_delivery_date
            if odoo_datetime:
                wr = odoo_call_kw("stock.picking", "write", args=[[picking["id"]], {"act_delivery_date": odoo_datetime}], kwargs={})
                if wr is not None:
                    result["act_delivery_date_updated"] = True

            moves = get_moves_for_picking(picking["id"])
            if not moves:
                failure_reasons.append(f"No stock.move for picking {picking['name']}")

            # Process each item
            for bol_item in items:
                bol_lot = (bol_item.get("lot_number") or "").strip()
                bol_qty = float(bol_item.get("quantity", 0))
                customer_sku = (bol_item.get("item") or bol_item.get("item_number") or "").strip()

                lot_result = {"customer_sku": customer_sku, "bol_lot": bol_lot, "bol_quantity": bol_qty, "updated": False, "status": ""}

                if not bol_lot:
                    lot_result["status"] = "no lot_number"
                    result["lot_updates"].append(lot_result)
                    continue

                lot_id, lot_product_id = find_lot(bol_lot)
                if not lot_id:
                    lot_result["status"] = f"lot {bol_lot} not found"
                    failure_reasons.append(f"Lot '{bol_lot}' (SKU: {customer_sku}) not found")
                    result["lot_updates"].append(lot_result)
                    continue

                matched_move = None
                for mv in moves:
                    if mv["product_id"] and mv["product_id"][0] == lot_product_id:
                        matched_move = mv
                        break
                if not matched_move and len(moves) == 1 and len(items) == 1:
                    matched_move = moves[0]
                if not matched_move:
                    lot_result["status"] = "no matching stock.move"
                    failure_reasons.append(f"No matching move for lot '{bol_lot}' (SKU: {customer_sku})")
                    result["lot_updates"].append(lot_result)
                    continue

                move_id = matched_move["id"]
                existing_lines = get_move_lines_for_move(move_id)

                # Check stock
                source_location_id = None
                if existing_lines:
                    loc = existing_lines[0].get("location_id")
                    source_location_id = loc[0] if isinstance(loc, list) else loc
                if not source_location_id:
                    pk = odoo_call_kw("stock.picking", "read", args=[[picking["id"]]], kwargs={"fields": ["location_id"]})
                    if pk and pk[0].get("location_id"):
                        loc = pk[0]["location_id"]
                        source_location_id = loc[0] if isinstance(loc, list) else loc

                if source_location_id:
                    avail, ok, msg = check_stock_available(lot_product_id, lot_id, source_location_id, bol_qty)
                    if not ok:
                        lot_result["status"] = f"insufficient stock: {msg}"
                        failure_reasons.append(f"Lot '{bol_lot}': {msg}")
                        result["lot_updates"].append(lot_result)
                        continue

                # Check demand
                demand = matched_move.get("product_uom_qty", 0)
                existing_qty = sum(el.get("quantity", 0) or 0 for el in existing_lines)
                if demand > 0 and (existing_qty + bol_qty) > demand:
                    lot_result["status"] = f"exceeds demand: {existing_qty}+{bol_qty}>{demand}"
                    failure_reasons.append(f"Lot '{bol_lot}' qty exceeds demand")
                    result["lot_updates"].append(lot_result)
                    continue

                # Create move_line
                vals = {"move_id": move_id, "picking_id": picking["id"], "product_id": lot_product_id, "lot_id": lot_id, "quantity": bol_qty, "qty_done": bol_qty, "picked": True}
                if existing_lines:
                    ref = existing_lines[0]
                    if ref.get("location_id"):
                        vals["location_id"] = ref["location_id"][0] if isinstance(ref["location_id"], list) else ref["location_id"]
                    if ref.get("location_dest_id"):
                        vals["location_dest_id"] = ref["location_dest_id"][0] if isinstance(ref["location_dest_id"], list) else ref["location_dest_id"]
                    if ref.get("product_uom_id"):
                        vals["product_uom_id"] = ref["product_uom_id"][0] if isinstance(ref["product_uom_id"], list) else ref["product_uom_id"]
                else:
                    if matched_move.get("product_uom"):
                        vals["product_uom_id"] = matched_move["product_uom"][0] if isinstance(matched_move["product_uom"], list) else matched_move["product_uom"]

                new_line_id = odoo_call_kw("stock.move.line", "create", args=[vals])
                if new_line_id:
                    lot_result["updated"] = True
                    lot_result["new_move_line_id"] = new_line_id
                    lot_result["status"] = "move_line created"
                else:
                    lot_result["status"] = "failed to create move_line"
                    failure_reasons.append(f"Failed to create move_line for lot '{bol_lot}'")

                result["lot_updates"].append(lot_result)

    # 4. Validate picking
    any_updated = any(lu.get("updated") for lu in result.get("lot_updates", []))
    if any_updated and result.get("picking_id") and not failure_reasons:
        picking_id = result["picking_id"]
        validate_result = odoo_call_kw("stock.picking", "button_validate", args=[[picking_id]], kwargs={})
        if validate_result is None:
            failure_reasons.append(f"Failed to validate picking {result.get('picking_name', '')}")
            result["validated"] = False
        elif isinstance(validate_result, dict) and validate_result.get("res_model") == "stock.backorder.confirmation":
            failure_reasons.append(f"Picking {result.get('picking_name', '')} requires backorder")
            result["validated"] = False
        else:
            result["validated"] = True

    # 5. Post chatter note
    if result.get("picking_id") and not failure_reasons:
        try:
            lot_items = "".join(f"<li>{lu['bol_lot']} - Qty: {int(lu['bol_quantity'])} (SKU: {lu.get('customer_sku','')})</li>" for lu in result.get("lot_updates", []) if lu.get("updated"))
            body = "<p><b>BOL auto-processed (Lagrou Sync)</b></p><ul>"
            if lot_items:
                body += f"<li>Lots:<ul>{lot_items}</ul></li>"
            if result.get("validated"):
                body += "<li>Picking validated (done)</li>"
            body += "</ul>"
            odoo_call_kw("mail.message", "create", args=[{"model": "stock.picking", "res_id": result["picking_id"], "body": body, "message_type": "comment", "subtype_id": 2}])
        except Exception:
            pass

    # 6. Emails
    if not failure_reasons and any_updated:
        _send_success_email(result, customer_po, order_name, result.get("picking_name", ""))

    if failure_reasons:
        result["failure_reasons"] = failure_reasons
        _send_failure_email(customer_po, failure_reasons, order_name=order_name)

    return result
