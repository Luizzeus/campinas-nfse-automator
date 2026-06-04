import os
import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional
from database import get_db_connection, init_db
from automator import recover_nfse_pdf, run_nfse_automation
from reporter import generate_pdf_report
from email_sender import get_billing_email_items, run_billing_email_automation, verify_boleto_files

# Initialize app
init_db()
app = FastAPI(title="NFS-e Campinas Automator API")

from fastapi import Request
@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# Directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
INVOICES_DIR = os.path.join(BASE_DIR, "invoices")

# Create directories if not exist
for d in [STATIC_DIR, REPORTS_DIR, INVOICES_DIR]:
    os.makedirs(d, exist_ok=True)

# Mount static files for invoices, reports, and frontend assets
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/reports", StaticFiles(directory=REPORTS_DIR), name="reports")
app.mount("/invoices", StaticFiles(directory=INVOICES_DIR), name="invoices")

# Models
class ClientModel(BaseModel):
    id: Optional[int] = None
    cnpj_cpf: str
    name: str
    invoice_value: float
    boleto_value: float
    reference_note: Optional[str] = ""
    retention_type: str
    description_template: str
    emails: Optional[str] = ""
    requires_boleto: bool = True

class ConfigModel(BaseModel):
    portal_cnpj: str
    portal_password: str
    headless: bool

class RunPayload(BaseModel):
    client_ids: List[int]
    ref_date: Optional[str] = None # format YYYY-MM-DD

class ReportPayload(BaseModel):
    competence: str # format MM/YYYY

class RecoverInvoicePayload(BaseModel):
    client_id: int
    invoice_number: str
    ref_date: Optional[str] = None # format YYYY-MM-DD

class BillingEmailPayload(BaseModel):
    competence: str # format MM/YYYY
    client_ids: Optional[List[int]] = None

class BillingEmailListPayload(BaseModel):
    competence: str # format MM/YYYY

def re_competence(value: str) -> bool:
    return bool(value and __import__("re").match(r"^(0[1-9]|1[0-2])/\d{4}$", value))

# WebSocket manager for real-time logs
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass

manager = ConnectionManager()
billing_email_task_running = False

@app.get("/")
async def get_index():
    """Serve the SPA index.html."""
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "NFS-e Campinas Automator Dashboard is running. Static files are missing."}

# CLIENTS API
@app.get("/api/clients")
def get_clients():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM clients ORDER BY name ASC")
    clients = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return clients

@app.post("/api/clients")
def save_client(client: ClientModel):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    if client.id:
        # Update
        cursor.execute("""
            UPDATE clients 
            SET cnpj_cpf = ?, name = ?, invoice_value = ?, boleto_value = ?, 
                reference_note = ?, retention_type = ?, description_template = ?, emails = ?, requires_boleto = ?
            WHERE id = ?
        """, (
            client.cnpj_cpf, client.name, client.invoice_value, client.boleto_value,
            client.reference_note, client.retention_type, client.description_template,
            client.emails, 1 if client.requires_boleto else 0, client.id
        ))
    else:
        # Insert
        cursor.execute("""
            INSERT INTO clients (cnpj_cpf, name, invoice_value, boleto_value, reference_note, retention_type, description_template, emails, requires_boleto)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            client.cnpj_cpf, client.name, client.invoice_value, client.boleto_value,
            client.reference_note, client.retention_type, client.description_template,
            client.emails, 1 if client.requires_boleto else 0
        ))
    
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Cliente salvo com sucesso."}

@app.delete("/api/clients/{client_id}")
def delete_client(client_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Cliente removido com sucesso."}

# CONFIG API
@app.get("/api/config")
def get_config():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM system_config")
    config = {row["key"]: row["value"] for row in cursor.fetchall()}
    conn.close()
    
    return {
        "portal_cnpj": config.get("portal_cnpj", ""),
        "portal_password": config.get("portal_password", ""),
        "headless": config.get("headless", "false").lower() == "true"
    }

@app.post("/api/config")
def save_config(config: ConfigModel):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    for key, value in [
        ("portal_cnpj", config.portal_cnpj),
        ("portal_password", config.portal_password),
        ("headless", str(config.headless).lower())
    ]:
        cursor.execute("""
            INSERT INTO system_config (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, value))
        
    conn.commit()
    conn.close()
    return {"status": "success", "message": "Configurações salvas com sucesso."}

# EMISSIONS HISTORY API
@app.get("/api/emissions")
def get_emissions(limit: int = 100):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT e.*, c.name as client_name 
        FROM emissions e
        JOIN clients c ON e.client_id = c.id
        ORDER BY e.timestamp DESC
        LIMIT ?
    """, (limit,))
    emissions = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return emissions

# BILLING EMAILS API
@app.get("/api/billing-emails")
def get_billing_emails(competence: str):
    if not competence or not re_competence(competence):
        raise HTTPException(status_code=400, detail="Competência deve estar no formato MM/AAAA.")
    return get_billing_email_items(competence)

@app.get("/api/billing-emails/verify-boletos")
def verify_billing_boletos(competence: str):
    if not competence or not re_competence(competence):
        raise HTTPException(status_code=400, detail="Competência deve estar no formato MM/AAAA.")
    report = verify_boleto_files(competence)
    return {
        "competence": competence,
        "total": len(report),
        "found": sum(1 for item in report if item["boleto_found"]),
        "missing": sum(1 for item in report if not item["boleto_found"]),
        "items": report,
    }

@app.post("/api/billing-emails/send")
def send_billing_emails(payload: BillingEmailPayload, background_tasks: BackgroundTasks):
    if not re_competence(payload.competence):
        raise HTTPException(status_code=400, detail="Competência deve estar no formato MM/AAAA.")
    background_tasks.add_task(execute_billing_email_task, payload.competence, payload.client_ids, False)
    return {"status": "success", "message": "Envio de e-mails iniciado. Acompanhe os logs em tempo real."}

@app.post("/api/billing-emails/reprocess-errors")
def reprocess_billing_email_errors(payload: BillingEmailPayload, background_tasks: BackgroundTasks):
    if not re_competence(payload.competence):
        raise HTTPException(status_code=400, detail="Competência deve estar no formato MM/AAAA.")
    background_tasks.add_task(execute_billing_email_task, payload.competence, payload.client_ids, True)
    return {"status": "success", "message": "Reprocessamento dos erros iniciado. Acompanhe os logs em tempo real."}

@app.post("/api/billing-emails/report")
def generate_billing_email_report(payload: BillingEmailListPayload):
    if not re_competence(payload.competence):
        raise HTTPException(status_code=400, detail="Competência deve estar no formato MM/AAAA.")
    try:
        pdf_path = generate_pdf_report(payload.competence)
        filename = os.path.basename(pdf_path)
        return {"status": "success", "message": "Relatório gerado com sucesso.", "url": f"/reports/{filename}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar relatório: {str(e)}")

# REPORTS API
@app.get("/api/reports")
def get_reports():
    reports = []
    if os.path.exists(REPORTS_DIR):
        for f in os.listdir(REPORTS_DIR):
            if f.endswith(".pdf"):
                fp = os.path.join(REPORTS_DIR, f)
                stat = os.stat(fp)
                reports.append({
                    "filename": f,
                    "size_bytes": stat.st_size,
                    "created_at": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%d/%m/%Y %H:%M:%S"),
                    "url": f"/reports/{f}"
                })
    return sorted(reports, key=lambda x: x["filename"], reverse=True)

@app.post("/api/reports/generate")
def generate_report_endpoint(payload: ReportPayload):
    try:
        pdf_path = generate_pdf_report(payload.competence)
        filename = os.path.basename(pdf_path)
        return {
            "status": "success",
            "message": "Relatório gerado com sucesso.",
            "url": f"/reports/{filename}"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao gerar relatório: {str(e)}")

# AUTOMATION LAUNCHER
async def execute_automation_task(client_ids: List[int], ref_date_str: Optional[str]):
    ref_date = None
    if ref_date_str:
        try:
            ref_date = datetime.datetime.strptime(ref_date_str, "%Y-%m-%d").date()
        except Exception:
            pass
            
    async def log_to_websocket(msg_dict):
        await manager.broadcast(msg_dict)
        
    try:
        await run_nfse_automation(client_ids, ref_date, progress_callback=log_to_websocket)
    except Exception as e:
        await manager.broadcast({
            "timestamp": datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            "status": "error",
            "message": f"Erro inesperado no executor: {str(e)}"
        })

async def execute_billing_email_task(competence: str, client_ids: Optional[List[int]], only_errors: bool):
    global billing_email_task_running

    async def log_to_websocket(msg_dict):
        await manager.broadcast(msg_dict)

    if billing_email_task_running:
        await manager.broadcast({
            "timestamp": datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            "status": "warning",
            "message": "Já existe um envio de faturamento em execução. Aguarde concluir antes de iniciar outro."
        })
        return

    billing_email_task_running = True
    try:
        await run_billing_email_automation(
            competence,
            client_ids=client_ids,
            only_errors=only_errors,
            progress_callback=log_to_websocket,
        )
    except Exception as e:
        await manager.broadcast({
            "timestamp": datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            "status": "error",
            "message": f"Erro inesperado no envio de e-mails: {str(e)}"
        })
    finally:
        billing_email_task_running = False

async def execute_recover_task(client_id: int, invoice_number: str, ref_date_str: Optional[str]):
    ref_date = None
    if ref_date_str:
        try:
            ref_date = datetime.datetime.strptime(ref_date_str, "%Y-%m-%d").date()
        except Exception:
            pass

    async def log_to_websocket(msg_dict):
        await manager.broadcast(msg_dict)

    try:
        await recover_nfse_pdf(client_id, invoice_number, ref_date, progress_callback=log_to_websocket)
    except Exception as e:
        await manager.broadcast({
            "timestamp": datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            "status": "error",
            "message": f"Erro inesperado na recuperação: {str(e)}"
        })

@app.post("/api/run")
def start_automation(payload: RunPayload, background_tasks: BackgroundTasks):
    background_tasks.add_task(execute_automation_task, payload.client_ids, payload.ref_date)
    return {"status": "success", "message": "Automação iniciada. Acompanhe os logs em tempo real."}

@app.post("/api/recover-invoice")
def recover_invoice(payload: RecoverInvoicePayload, background_tasks: BackgroundTasks):
    background_tasks.add_task(execute_recover_task, payload.client_id, payload.invoice_number, payload.ref_date)
    return {"status": "success", "message": "Recuperação da nota iniciada. Acompanhe os logs em tempo real."}

# WEBSOCKET ENDPOINT FOR LOGS
@app.websocket("/ws/logs")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # We just hold the connection open.
            # Client doesn't need to send messages, they only receive broadcast logs.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
