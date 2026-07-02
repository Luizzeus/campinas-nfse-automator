import datetime
import os
import re
import traceback
import unicodedata
from calendar import monthrange
from email.utils import getaddresses
from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright

from automator import is_pdf_file, slugify_name
from database import get_db_connection
from utils import get_competence_info

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INVOICES_DIR = os.path.join(BASE_DIR, "invoices")
BOLETOS_DIR = os.path.join(BASE_DIR, "boletos")
SCREENSHOTS_DIR = os.path.join(BASE_DIR, "screenshots")
WEBMAIL_URL = "https://webmail.specchio.info"
WEBMAIL_LOGIN = "luiz.rocha@compunettecnologia.com.br"
WEBMAIL_PASSWORD_ENV = "WEBMAIL_PASSWORD"
WEBMAIL_FROM = "financeiro@specchio.info"

os.makedirs(BOLETOS_DIR, exist_ok=True)
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


def _now_sql():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _display_now():
    return datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def normalize_filename_part(value):
    text = unicodedata.normalize("NFKD", value or "").encode("ASCII", "ignore").decode("ASCII")
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return re.sub(r"_+", "_", text)


def parse_emails(raw):
    addresses = []
    normalized_raw = (raw or "").replace(";", ",")
    for _, address in getaddresses([normalized_raw]):
        if re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", address):
            addresses.append(address)
    seen = set()
    return [email for email in addresses if not (email.lower() in seen or seen.add(email.lower()))]


def competence_to_ref_date(competence):
    month, year = [int(part) for part in competence.split("/")]
    if month == 12:
        return datetime.date(year + 1, 1, 1)
    return datetime.date(year, month + 1, 1)


def competence_folder(competence):
    return competence.replace("/", "-")


def due_date_for_competence(competence, due_day):
    month, year = [int(part) for part in competence.split("/")]
    if month == 12:
        due_month, due_year = 1, year + 1
    else:
        due_month, due_year = month + 1, year
    day = min(int(due_day or 10), monthrange(due_year, due_month)[1])
    return datetime.date(due_year, due_month, day)


def latest_emitted_notes(competence):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT
            e.*,
            c.name as client_name,
            c.cnpj_cpf,
            c.invoice_value,
            c.boleto_value,
            c.due_day,
            c.emails,
            c.requires_boleto
        FROM emissions e
        JOIN clients c ON c.id = e.client_id
        WHERE e.competence = ?
          AND e.status = 'emitida'
          AND e.id IN (
              SELECT MAX(id)
              FROM emissions
              WHERE competence = ? AND status = 'emitida'
              GROUP BY client_id
          )
        ORDER BY c.name ASC
    """, (competence, competence))
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def boleto_search_roots(competence):
    folder = competence_folder(competence)
    return [
        os.path.join(INVOICES_DIR, folder),
        os.path.join(BOLETOS_DIR, folder),
        BOLETOS_DIR,
    ]


def list_boleto_candidates(competence):
    candidates = []
    seen = set()
    for root in boleto_search_roots(competence):
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            for filename in filenames:
                if not filename.lower().endswith(".pdf"):
                    continue
                path = os.path.join(dirpath, filename)
                if path in seen or not is_pdf_file(path):
                    continue
                seen.add(path)
                stem = os.path.splitext(filename)[0]
                normalized_name = normalize_filename_part(stem)
                if not (normalized_name.startswith("bradesco_") or normalized_name.startswith("boleto_")):
                    continue
                candidates.append({
                    "path": path,
                    "filename": filename,
                    "normalized_name": normalized_name,
                })
    return candidates


def find_boleto_file(client_name, invoice_number, competence):
    normalized_client = normalize_filename_part(client_name)
    normalized_invoice = normalize_filename_part(str(invoice_number or ""))
    if not normalized_invoice:
        return None

    expected_prefix_bradesco = f"bradesco_{normalized_client}_{normalized_invoice}"
    expected_prefix_boleto = f"boleto_{normalized_client}_{normalized_invoice}"
    candidates = list_boleto_candidates(competence)

    # Exact historical rule: Bradesco_Nome_do_Cliente_Numero_da_Nota or Boleto_Nome_do_Cliente_Numero_da_Nota
    for candidate in candidates:
        if candidate["normalized_name"].startswith(expected_prefix_bradesco) or candidate["normalized_name"].startswith(expected_prefix_boleto):
            return candidate["path"]

    # Correction routine: when the invoice number uniquely identifies a Bradesco
    # boleto in the competence folder, accept it even if the client name is
    # abbreviated or has a small typo in the filename.
    invoice_matches = [
        candidate for candidate in candidates
        if re.search(rf"(^|_)%s($|_)" % re.escape(normalized_invoice), candidate["normalized_name"])
    ]
    if len(invoice_matches) == 1:
        return invoice_matches[0]["path"]

    # Last safe fallback: require both invoice number and at least one relevant
    # client token. This handles shortened names without risking cross-client use.
    client_tokens = [token for token in normalized_client.split("_") if len(token) >= 4]
    token_matches = [
        candidate for candidate in invoice_matches
        if any(token in candidate["normalized_name"] for token in client_tokens)
    ]
    if len(token_matches) == 1:
        return token_matches[0]["path"]

    return None


def verify_boleto_files(competence):
    report = []
    for emission in latest_emitted_notes(competence):
        boleto_path = find_boleto_file(emission["client_name"], emission.get("invoice_number"), competence)
        report.append({
            "client_id": emission["client_id"],
            "client_name": emission["client_name"],
            "invoice_number": emission.get("invoice_number") or "",
            "boleto_found": bool(boleto_path),
            "boleto_pdf_path": boleto_path or "",
            "boleto_filename": os.path.basename(boleto_path) if boleto_path else "",
        })
    return report


def get_billing_email_items(competence):
    emissions = latest_emitted_notes(competence)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM email_sends WHERE competence = ?", (competence,))
    sends = {row["client_id"]: dict(row) for row in cursor.fetchall()}
    conn.close()

    items = []
    for emission in emissions:
        emails = parse_emails(emission.get("emails"))
        invoice_pdf_path = emission.get("pdf_path") or ""
        invoice_number = emission.get("invoice_number") or ""
        invoice_pdf_found = bool(invoice_pdf_path and os.path.exists(invoice_pdf_path) and is_pdf_file(invoice_pdf_path))
        requires_boleto = bool(emission.get("requires_boleto", 1))
        boleto_pdf_path = find_boleto_file(emission["client_name"], invoice_number, competence) if requires_boleto else None
        boleto_found = bool(boleto_pdf_path) or not requires_boleto
        send = sends.get(emission["client_id"], {})
        status = send.get("status") or "pendente"
        if status == "pendente" and send.get("error_message"):
            status = "erro"
        items.append({
            "client_id": emission["client_id"],
            "emission_id": emission["id"],
            "client_name": emission["client_name"],
            "cnpj_cpf": emission["cnpj_cpf"],
            "competence": competence,
            "invoice_number": invoice_number,
            "invoice_value": emission["invoice_value"],
            "boleto_value": emission["boleto_value"],
            "due_date": due_date_for_competence(competence, emission.get("due_day")).strftime("%d/%m/%Y"),
            "emails": emails,
            "emails_text": ", ".join(emails),
            "note_issued": True,
            "pdf_found": invoice_pdf_found,
            "requires_boleto": requires_boleto,
            "boleto_found": boleto_found,
            "boleto_exempt": not requires_boleto,
            "email_sent": status == "enviado",
            "status": status,
            "sent_at": send.get("sent_at"),
            "error_message": send.get("error_message") or "",
            "failed_step": send.get("failed_step") or "",
            "invoice_pdf_path": invoice_pdf_path,
            "boleto_pdf_path": boleto_pdf_path or send.get("boleto_pdf_path") or "",
            "invoice_filename": os.path.basename(invoice_pdf_path) if invoice_pdf_path else "",
            "boleto_filename": os.path.basename(boleto_pdf_path or send.get("boleto_pdf_path") or ""),
        })
    return items


def upsert_email_send(item, status, error_message=None, failed_step=None, screenshot_path=None, sent_at=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO email_sends (
            client_id, emission_id, competence, status, emails_sent, from_email,
            subject, invoice_pdf_path, boleto_pdf_path, boleto_due_date,
            boleto_value, sent_at, error_message, failed_step, screenshot_path,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(client_id, competence) DO UPDATE SET
            emission_id = excluded.emission_id,
            status = excluded.status,
            emails_sent = excluded.emails_sent,
            from_email = excluded.from_email,
            subject = excluded.subject,
            invoice_pdf_path = excluded.invoice_pdf_path,
            boleto_pdf_path = excluded.boleto_pdf_path,
            boleto_due_date = excluded.boleto_due_date,
            boleto_value = excluded.boleto_value,
            sent_at = excluded.sent_at,
            error_message = excluded.error_message,
            failed_step = excluded.failed_step,
            screenshot_path = excluded.screenshot_path,
            updated_at = excluded.updated_at
    """, (
        item["client_id"], item["emission_id"], item["competence"], status,
        item.get("emails_text") or ", ".join(item.get("emails") or []), WEBMAIL_FROM,
        build_subject(item), item.get("invoice_pdf_path"), item.get("boleto_pdf_path"),
        item.get("due_date"), item.get("boleto_value"), sent_at,
        error_message, failed_step, screenshot_path, _now_sql(), _now_sql()
    ))
    conn.commit()
    conn.close()


def validate_item(item, allow_already_sent=False):
    if item.get("email_sent") and not allow_already_sent:
        raise RuntimeError("E-mail já enviado para este cliente e competência")
    if not item.get("emails"):
        raise RuntimeError("Cliente sem e-mail cadastrado válido")
    if not item.get("invoice_number") or str(item.get("invoice_number")).upper() == "N/A":
        raise RuntimeError("Número da nota não foi gerado")
    if not item.get("pdf_found"):
        raise RuntimeError("PDF da nota fiscal não encontrado ou inválido")
    if item.get("requires_boleto", True) and not item.get("boleto_found"):
        raise RuntimeError("Boleto correspondente não encontrado")


def build_subject(item):
    return f"Faturamento de Serviços Prestados - {item['client_name']} - {item['competence']}"


def build_body(item):
    ref_date = competence_to_ref_date(item["competence"])
    comp_info = get_competence_info(ref_date)
    documents = f"Nota Fiscal Eletrônica (NF-e) de Serviços nº {item['invoice_number']}."
    deadline_text = "antes da data de vencimento"
    if item.get("requires_boleto", True):
        documents += f"\n\nBoleto Bancário para pagamento, com vencimento em {item['due_date']}."
    else:
        deadline_text = "assim que possível"

    return f"""Prezado(a) {item['client_name']},

Esperamos que este e-mail o(a) encontre bem.

Informamos que o faturamento relativo aos serviços de engenharia e suporte técnico prestados durante o mês de {comp_info['competence_month_name']} já foi consolidado.

Anexos a este e-mail, encaminhamos os seguintes documentos para conferência e liquidação:

{documents}

Solicitamos a gentileza de confirmar o recebimento deste e-mail e dos respectivos anexos.

Caso necessite de alguma alteração no faturamento, esclarecimentos sobre o relatório de horas/atividades ou qualquer outra informação adicional, por favor, entre em contato conosco {deadline_text}.

Agradecemos pela parceria de sempre e permanecemos à inteira disposição.

Atenciosamente,

Financeiro"""


async def first_visible(page, selectors, timeout_ms=12000):
    deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)
    last_error = None
    while datetime.datetime.now() < deadline:
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = await locator.count()
                for index in range(count):
                    item = locator.nth(index)
                    if await item.is_visible():
                        return item
            except Exception as exc:
                last_error = exc
        await page.wait_for_timeout(300)
    raise PlaywrightTimeoutError(f"Elemento não encontrado. Último erro: {last_error}")


async def fill_control(page, selectors, value, timeout_ms=12000):
    control = await first_visible(page, selectors, timeout_ms=timeout_ms)
    await control.click(force=True)
    try:
        await control.fill(value)
    except Exception:
        await control.evaluate("""(el, value) => {
            el.value = value;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }""", value)
    return control


async def login_webmail(page, password):
    await page.goto(WEBMAIL_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)
    body = (await page.locator("body").inner_text()).lower()
    if "nova mensagem" in body or "escrever" in body or "compose" in body:
        return
    await fill_control(page, [
        "input[name='Email']",
        "input[type='email']",
        "input[name='email']",
        "input[name='user']",
        "input[name='_user']",
        "input[name='username']",
        "input[type='text']",
    ], WEBMAIL_LOGIN)
    await fill_control(page, [
        "input[name='Password']",
        "input[type='password']",
        "input[name='password']",
        "input[name='_pass']",
        "input[name='pass']",
    ], password)
    button = await first_visible(page, [
        "xpath=//*[self::button or self::input or self::a][contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'entrar')]",
        "xpath=//*[self::button or self::input or self::a][contains(translate(@value, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'entrar')]",
        ".buttonLogin",
        "button[type='submit']",
        "input[type='submit']",
    ], timeout_ms=10000)
    await button.click(force=True)
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(3000)


async def select_sender(page):
    try:
        toggle = page.locator("#identity-toggle").first
        if await toggle.count() and await toggle.is_visible():
            await toggle.click(force=True)
            option = await first_visible(page, [
                f"xpath=//menu[contains(@class, 'show')]//*[self::a or self::li][contains(normalize-space(), '{WEBMAIL_FROM}')]",
                f"xpath=//*[self::a or self::li][contains(normalize-space(), '{WEBMAIL_FROM}')]",
            ], timeout_ms=5000)
            await option.click(force=True)
            await page.wait_for_timeout(700)
            identity_value = await page.locator("input[type='text']").first.input_value()
            if WEBMAIL_FROM not in identity_value:
                raise RuntimeError(f"Identidade selecionada não contém {WEBMAIL_FROM}: {identity_value}")
            return
    except Exception as exc:
        raise RuntimeError(f"Não foi possível selecionar o remetente {WEBMAIL_FROM}: {exc}")

    raise RuntimeError(f"Não foi possível selecionar o remetente {WEBMAIL_FROM}")

async def compose_and_send(page, item):
    new_message = await first_visible(page, [
        ".buttonCompose",
        "xpath=//*[self::button or self::a][contains(normalize-space(), 'New message')]",
        "xpath=//*[self::button or self::a][contains(normalize-space(), 'Nova mensagem')]",
        "xpath=//*[self::button or self::a][contains(normalize-space(), 'Escrever')]",
        "xpath=//*[self::button or self::a][contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'compose')]",
    ], timeout_ms=20000)
    await new_message.click(force=True)
    await page.wait_for_timeout(2000)

    await select_sender(page)

    # SnappyMail compose fields: first visible text input is identity, second is To.
    to_field = page.locator("input[type='text']").nth(1)
    await to_field.wait_for(state="visible", timeout=12000)
    await to_field.click(force=True)
    await to_field.fill(item["emails_text"])
    await to_field.press("Enter")

    subject_field = page.locator("input[name='subject']").first
    await subject_field.wait_for(state="visible", timeout=12000)
    await subject_field.click(force=True)
    await subject_field.fill(build_subject(item))

    body = build_body(item)
    body_control = page.locator(".squire-wysiwyg").first
    await body_control.wait_for(state="visible", timeout=12000)
    await body_control.click(force=True)
    await body_control.evaluate("""(el, value) => {
        const existingNodes = Array.from(el.childNodes);
        const fragment = document.createDocumentFragment();
        String(value).split('\\n').forEach((line) => {
            const div = document.createElement('div');
            if (line) {
                div.textContent = line;
            } else {
                div.appendChild(document.createElement('br'));
            }
            fragment.appendChild(div);
        });
        const separator = document.createElement('div');
        separator.appendChild(document.createElement('br'));
        fragment.appendChild(separator);
        el.innerHTML = '';
        el.appendChild(fragment);
        existingNodes.forEach((node) => el.appendChild(node));
        el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
        el.dispatchEvent(new Event('change', {bubbles: true}));
    }""", body)

    attach_button = await first_visible(page, [
        "#composeUploadButton",
        "xpath=//*[self::button or self::a or self::span][contains(normalize-space(), 'Anexar')]",
        "xpath=//*[self::button or self::a or self::span][contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'attach')]",
        "xpath=//*[self::button or self::a or self::span][contains(@class, 'attach') or contains(@class, 'clip')]",
    ], timeout_ms=8000)
    try:
        async with page.expect_file_chooser(timeout=10000) as file_chooser_info:
            await attach_button.click(force=True)
        file_chooser = await file_chooser_info.value
        attachment_paths = [item["invoice_pdf_path"]]
        if item.get("requires_boleto", True):
            attachment_paths.append(item["boleto_pdf_path"])
        await file_chooser.set_files(attachment_paths)
    except Exception:
        file_input = page.locator("input[type='file']").last
        attachment_paths = [item["invoice_pdf_path"]]
        if item.get("requires_boleto", True):
            attachment_paths.append(item["boleto_pdf_path"])
        await file_input.set_input_files(attachment_paths)
    await page.wait_for_timeout(7000)

    body_text = await page.locator("body").inner_text()
    missing_attachments = [
        path for path in ([item["invoice_pdf_path"], item["boleto_pdf_path"]] if item.get("requires_boleto", True) else [item["invoice_pdf_path"]])
        if path and os.path.basename(path) not in body_text
    ]
    if missing_attachments:
        raise RuntimeError("Anexos não aparecem carregados na tela: " + ", ".join(os.path.basename(p) for p in missing_attachments))
    identity_value = await page.locator("input[type='text']").first.input_value()
    if WEBMAIL_FROM not in identity_value:
        raise RuntimeError(f"Remetente selecionado não confirmado como {WEBMAIL_FROM}")

    send_button = await first_visible(page, [
        "xpath=//*[self::button or self::a][normalize-space()='Send' or normalize-space()='Enviar' or .//*[normalize-space()='Enviar']]",
        "xpath=//*[self::button or self::a][contains(translate(normalize-space(.), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'send')]",
    ], timeout_ms=10000)
    await send_button.click(force=True)
    await page.wait_for_timeout(3500)

async def run_billing_email_automation(competence, client_ids=None, only_errors=False, progress_callback=None):
    selected_ids = set(client_ids or [])
    items = get_billing_email_items(competence)
    if selected_ids:
        items = [item for item in items if item["client_id"] in selected_ids]
    if only_errors:
        items = [item for item in items if item["status"] == "erro"]

    async def log_progress(msg, status="info", client_id=None):
        if progress_callback:
            await progress_callback({
                "timestamp": _display_now(),
                "client_id": client_id,
                "status": status,
                "message": msg,
            })
        print(f"[{status.upper()}] {msg}")

    if not items:
        await log_progress("Nenhum cliente elegível para envio de e-mail nesta competência.", "warning")
        return

    password = os.environ.get(WEBMAIL_PASSWORD_ENV)
    if not password:
        message = f"Variável de ambiente {WEBMAIL_PASSWORD_ENV} não configurada"
        for item in items:
            upsert_email_send(item, "erro", error_message=message, failed_step="credenciais")
        await log_progress(message, "error")
        return

    await log_progress(f"Iniciando envio de e-mails de faturamento para {len(items)} cliente(s).", "info")
    screenshot_folder = os.path.join(SCREENSHOTS_DIR, competence_folder(competence))
    os.makedirs(screenshot_folder, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=["--start-maximized", "--disable-notifications"])
        context = await browser.new_context(viewport={"width": 1600, "height": 1000})
        page = await context.new_page()
        try:
            await log_progress("Acessando webmail e efetuando login...", "info")
            try:
                await login_webmail(page, password)
            except Exception as login_exc:
                message = f"Falha no login do webmail: {login_exc}"
                for item in items:
                    upsert_email_send(item, "erro", error_message=message, failed_step="login")
                await log_progress(message, "error")
                return
            for item in items:
                failed_step = "validação"
                screenshot_path = None
                try:
                    await log_progress(f"Validando faturamento de {item['client_name']}...", "info", item["client_id"])
                    validate_item(item)
                    upsert_email_send(item, "processando")
                    failed_step = "composição"
                    await log_progress(f"Compondo e-mail para {item['client_name']}...", "running", item["client_id"])
                    await compose_and_send(page, item)
                    upsert_email_send(item, "enviado", sent_at=_now_sql())
                    await log_progress(f"E-mail enviado para {item['client_name']}.", "success", item["client_id"])
                except Exception as exc:
                    try:
                        screenshot_path = os.path.join(screenshot_folder, f"email_{slugify_name(item['client_name'])}_erro.png")
                        await page.screenshot(path=screenshot_path, full_page=True)
                    except Exception:
                        screenshot_path = None
                    error_message = f"{exc}"
                    upsert_email_send(item, "erro", error_message=error_message, failed_step=failed_step, screenshot_path=screenshot_path)
                    await log_progress(f"Erro no envio para {item['client_name']}: {error_message}", "error", item["client_id"])
                    try:
                        await page.goto(WEBMAIL_URL, wait_until="domcontentloaded")
                        await page.wait_for_timeout(1500)
                    except Exception:
                        pass
        finally:
            await browser.close()

    await log_progress("Processamento de e-mails de faturamento concluído.", "success")
