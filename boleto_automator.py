import asyncio
import os
import re
import datetime
from calendar import monthrange
import unicodedata
from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright
from database import get_db_connection
from utils import get_competence_info
from automator import slugify_name

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOLETOS_DIR = os.path.join(BASE_DIR, "boletos")
SCREENSHOTS_DIR = os.path.join(BASE_DIR, "screenshots")
os.makedirs(BOLETOS_DIR, exist_ok=True)
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)

# Bradesco URLs
BRADESCO_LOGIN_URL = "https://www.ne12.bradesconetempresa.b.br/ibpjlogin/login.jsf"

def get_due_date_for_client(ref_date, due_day):
    """Calculate the due date (DD/MM/YYYY) for the current month of execution."""
    today = ref_date or datetime.date.today()
    month = today.month
    year = today.year
    
    days_in_month = monthrange(year, month)[1]
    day = min(int(due_day or 10), days_in_month)
    return f"{day:02d}", f"{month:02d}", f"{year}"

async def wait_for_bradesco_logged_in(page, timeout_ms=180000):
    """Wait for the user to solve 2FA and login successfully."""
    # Look for elements that appear only on the logged-in screen (e.g. "SAIR", "Cobrança")
    selectors = [
        "xpath=//a[contains(normalize-space(.), 'SAIR') or contains(normalize-space(.), 'Sair')]",
        "xpath=//*[normalize-space()='Cobrança' or normalize-space()='Saldos e Extratos']",
        "xpath=//div[contains(@class, 'menu')]//a[contains(normalize-space(.), 'Cobrança')]"
    ]
    
    deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)
    while datetime.datetime.now() < deadline:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() > 0 and await locator.is_visible():
                    return True
            except Exception:
                pass
        await page.wait_for_timeout(1000)
    return False
async def click_element(page, selector, timeout_ms=15000):
    """Click an element, using force=True and falling back to JS click to bypass overlays."""
    locator = page.locator(selector).first
    try:
        await locator.wait_for(state="attached", timeout=timeout_ms)
        await locator.click(force=True, timeout=5000)
    except Exception:
        try:
            await locator.evaluate("(el) => el.click()")
        except Exception as e:
            raise RuntimeError(f"Failed to click selector {selector}: {e}")


async def run_boleto_automation(emissions_to_process, ref_date=None, progress_callback=None):
    """
    Automates Bradesco Net Empresa to create boletos for successfully issued NFS-es.
    emissions_to_process: List of dicts, e.g.:
      [{"emission_id": 1, "client_name": "Congregação Sta Cruz", "cnpj_cpf": "...", "invoice_number": "2676", "boleto_value": 3460.43, "due_day": 10}]
    """
    if not emissions_to_process:
        return
        
    # 1. Fetch credentials
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM system_config")
    config = {row["key"]: row["value"] for row in cursor.fetchall()}
    conn.close()
    
    bradesco_user = config.get("bradesco_user", "LCSR00145")
    bradesco_password = config.get("bradesco_password", "@ccessINC21*")
    # For Bradesco, since 2FA is always needed, we prefer to run in headed mode unless explicitly config says true
    headless = config.get("headless", "false").lower() == "true"
    
    comp_info = get_competence_info(ref_date)
    folder_name = comp_info["month_year_short"].replace("/", "-")
    INVOICES_DIR = os.path.join(BASE_DIR, "invoices")
    invoice_folder = os.path.join(INVOICES_DIR, folder_name)
    screenshot_folder = os.path.join(SCREENSHOTS_DIR, folder_name)
    os.makedirs(invoice_folder, exist_ok=True)
    os.makedirs(screenshot_folder, exist_ok=True)
    
    async def log_progress(msg, status="info", client_id=None, boleto_url=None):
        if progress_callback:
            await progress_callback({
                "timestamp": datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                "client_id": client_id,
                "status": status,
                "message": msg,
                "boleto_url": boleto_url
            })
        print(f"[BRADESCO {status.upper()}] {msg}")

    await log_progress("Iniciando Playwright para o portal Bradesco...", "info")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--start-maximized", "--disable-notifications", "--disable-popup-blocking"]
        )
        context = await browser.new_context(viewport={"width": 1920, "height": 1080} if not headless else None)
        page = await context.new_page()
        
        try:
            # 2. Login Flow
            await log_progress("Acessando Bradesco Net Empresa...", "info")
            await page.goto(BRADESCO_LOGIN_URL, timeout=45000)
            await page.wait_for_timeout(2000)
            
            await log_progress("Preenchendo usuário e senha do Bradesco...", "info")
            await page.fill('input[id="identificationForm:txtUsuario"]', bradesco_user)
            await page.fill('input[id="identificationForm:txtSenha"]', bradesco_password)
            
            await log_progress("Clicando em Avançar no login do Bradesco...", "info")
            await click_element(page, 'input[id="identificationForm:botaoAvancar"]')
            await page.wait_for_timeout(3000)
            
            # Wait up to 3 minutes for login to complete
            await log_progress("Aguardando autenticação 2FA (Chave de Segurança/Token) e login pelo usuário na tela do navegador...", "warning")
            
            if not await wait_for_bradesco_logged_in(page, timeout_ms=180000):
                await log_progress("Tempo esgotado aguardando o login no Bradesco. Certifique-se de realizar o login completo na tela.", "error")
                await browser.close()
                return
                
            await log_progress("Login no Bradesco realizado com sucesso!", "success")
            await page.wait_for_timeout(3000)
            
            # 3. Process each client
            for item in emissions_to_process:
                emission_id = item["emission_id"]
                client_name = item["client_name"]
                cnpj_cpf = re.sub(r"\D+", "", item["cnpj_cpf"])
                invoice_number = item["invoice_number"]
                boleto_value = item["boleto_value"]
                due_day = item["due_day"]
                
                await log_progress(f"Iniciando geração de boleto para: {client_name} (Nota Nº {invoice_number})", "running")
                
                try:
                    # Navigate to "Cobrança" tab
                    await log_progress("Navegando para o menu Cobrança...", "running")
                    cobrança_sel = "xpath=//a[normalize-space()='Cobrança' or contains(normalize-space(.), 'Cobrança')]"
                    await click_element(page, cobrança_sel)
                    await page.wait_for_timeout(3000)
                    
                    # Locate the central frame
                    frame = page.frame(name="paginaCentral")
                    if not frame:
                        for f in page.frames:
                            if f.name == "paginaCentral" or "paginaCentral" in f.url or "Cobranca" in f.url or "cobranca" in f.url:
                                frame = f
                                break
                    if not frame:
                        frame = page
                        await log_progress("Aviso: Quadro paginaCentral não encontrado. Usando página principal.", "warning")
                    else:
                        await log_progress("Quadro paginaCentral localizado com sucesso.", "running")
                    
                    # Click "Emitir Boleto"
                    await log_progress("Clicando em Emitir Boleto...", "running")
                    emitir_sel = "xpath=//a[normalize-space()='Emitir Boleto' or contains(normalize-space(.), 'Emitir Boleto')]"
                    await click_element(frame, emitir_sel)
                    await page.wait_for_timeout(3000)
                    
                    # Passo 2: Fill Boleto Details Form
                    await log_progress("Preenchendo detalhes do boleto...", "running")
                    
                    # 1. Document Number (same as NFS-e)
                    doc_input_sel = "xpath=//input[contains(@id, 'numDocumento') or contains(@name, 'numDocumento') or contains(@id, 'NumeroDocumento') or contains(@id, 'txtNumero')]"
                    if await frame.locator(doc_input_sel).count() == 0:
                        doc_input_sel = "xpath=//td[contains(., 'Número do documento')]/following::input[1]"
                    await frame.fill(doc_input_sel, str(invoice_number))
                    
                    # 2. Due Date (Vencimento)
                    day, month, year = get_due_date_for_client(ref_date, due_day)
                    await log_progress(f"Calculada data de vencimento: {day}/{month}/{year}", "running")
                    
                    # Find day, month, year inputs
                    day_sel = "xpath=//input[contains(@id, 'diaVencimento') or contains(@id, 'dtVencimentoDia') or contains(@id, 'txtDiaVenc')]"
                    if await frame.locator(day_sel).count() == 0:
                        day_sel = "xpath=//td[contains(., 'Vencimento')]/following::input[1]"
                    await frame.fill(day_sel, day)
                    
                    month_sel = "xpath=//input[contains(@id, 'mesVencimento') or contains(@id, 'dtVencimentoMes') or contains(@id, 'txtMesVenc')]"
                    if await frame.locator(month_sel).count() == 0:
                        month_sel = "xpath=//td[contains(., 'Vencimento')]/following::input[2]"
                    await frame.fill(month_sel, month)
                    
                    year_sel = "xpath=//input[contains(@id, 'anoVencimento') or contains(@id, 'dtVencimentoAno') or contains(@id, 'txtAnoVenc')]"
                    if await frame.locator(year_sel).count() == 0:
                        year_sel = "xpath=//td[contains(., 'Vencimento')]/following::input[3]"
                    await frame.fill(year_sel, year)
                    
                    # 3. Document Value (Valor do Documento)
                    val_input_sel = "xpath=//input[contains(@id, 'valor') or contains(@id, 'vlDoc') or contains(@id, 'txtValor')]"
                    if await frame.locator(val_input_sel).count() == 0:
                        val_input_sel = "xpath=//*[contains(text(), 'Valor do Documento')]/following::input[1]"
                    val_str = f"{boleto_value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                    await frame.fill(val_input_sel, val_str)
                    
                    # 4. Multa e Juros
                    # Multa: Select %, Value 2,00, Days 1
                    multa_sel = "xpath=//select[contains(@id, 'multa') or contains(@id, 'tipoMulta')]"
                    if await frame.locator(multa_sel).count() > 0:
                        try:
                            await frame.select_option(multa_sel, label="%")
                        except Exception:
                            try:
                                await frame.select_option(multa_sel, value="2")
                            except Exception:
                                pass
                    
                    multa_val_sel = "xpath=//input[contains(@id, 'vlMulta') or contains(@id, 'pctMulta')]"
                    if await frame.locator(multa_val_sel).count() > 0:
                        await frame.fill(multa_val_sel, "2,00")
                        
                    multa_dias_sel = "xpath=//input[contains(@id, 'diasMulta') or contains(@id, 'atrasoMulta')]"
                    if await frame.locator(multa_dias_sel).count() > 0:
                        await frame.fill(multa_dias_sel, "1")
                        
                    # Juros: Select %, Value 1,00, Days 1
                    juros_sel = "xpath=//select[contains(@id, 'juros') or contains(@id, 'tipoJuros')]"
                    if await frame.locator(juros_sel).count() > 0:
                        try:
                            await frame.select_option(juros_sel, label="%")
                        except Exception:
                            try:
                                await frame.select_option(juros_sel, value="1")
                            except Exception:
                                pass
                        
                    juros_val_sel = "xpath=//input[contains(@id, 'vlJuros') or contains(@id, 'pctJuros')]"
                    if await frame.locator(juros_val_sel).count() > 0:
                        await frame.fill(juros_val_sel, "1,00")
                        
                    juros_dias_sel = "xpath=//input[contains(@id, 'diasJuros') or contains(@id, 'atrasoJuros')]"
                    if await frame.locator(juros_dias_sel).count() > 0:
                        await frame.fill(juros_dias_sel, "1")
                        
                    # Click next (Avançar)
                    avancar_sel = "xpath=//input[contains(@value, 'Avançar') or contains(@id, 'botaoAvancar') or @type='submit']"
                    await click_element(frame, avancar_sel)
                    await page.wait_for_timeout(3000)
                    
                    # Passo 3: Pagador Search & Selection
                    await log_progress("Selecionando pagador (cliente)...", "running")
                    
                    # Click "Lista de pagadores"
                    lista_pagadores_sel = "xpath=//a[contains(normalize-space(.), 'Lista de pagadores') or contains(., 'Lista') or contains(., 'pagador')]"
                    
                    popup = None
                    try:
                        async with context.expect_page(timeout=10000) as page_info:
                            await click_element(frame, lista_pagadores_sel)
                        popup = await page_info.value
                    except Exception:
                        if len(context.pages) > 1:
                            popup = context.pages[-1]
                            
                    target_page = popup if popup else page
                    await target_page.bring_to_front()
                    await target_page.wait_for_timeout(2000)
                    
                    # Search by CNPJ/CPF inside the search window/element
                    search_cnpj_sel = "xpath=//input[contains(@id, 'cnpj') or contains(@id, 'cpf') or contains(@name, 'cnpj') or contains(@name, 'cpf')]"
                    await target_page.fill(search_cnpj_sel, cnpj_cpf)
                    
                    buscar_btn_sel = "xpath=//input[contains(@value, 'Buscar') or contains(@value, 'Pesquisar') or contains(@id, 'btnBuscar') or contains(@id, 'botaoBuscar')]"
                    await click_element(target_page, buscar_btn_sel)
                    await target_page.wait_for_timeout(2000)
                    
                    # Select the client from search results
                    select_client_sel = "xpath=//a[contains(normalize-space(.), 'Selecionar') or contains(normalize-space(.), 'OK') or contains(@id, 'lnkSelecionar')]"
                    await click_element(target_page, select_client_sel)
                    await page.wait_for_timeout(2000)
                    
                    # Click Avançar/Confirmar to generate the boleto
                    confirmar_sel = "xpath=//input[contains(@value, 'Avançar') or contains(@value, 'Avancar') or contains(@value, 'Confirmar') or contains(@value, 'Emitir') or contains(@value, 'Gerar') or contains(@id, 'botaoConfirmar') or contains(@id, 'botaoAvancar') or @type='submit']"
                    await click_element(frame, confirmar_sel)
                    await page.wait_for_timeout(4000)
                    
                    # Save generated PDF using "Salvar como arquivo" button
                    await log_progress("Acessando arquivo de boleto para download...", "running")
                    salvar_arquivo_sel = "xpath=//*[self::a or self::button or self::input][contains(normalize-space(.), 'Salvar como') or contains(normalize-space(.), 'salvar') or contains(@id, 'salvar') or contains(@id, 'Salvar')]"
                    
                    # Wait for the file saving options popup
                    popup_save = None
                    try:
                        async with context.expect_page(timeout=10000) as page_info:
                            await click_element(frame, salvar_arquivo_sel)
                        popup_save = await page_info.value
                    except Exception:
                        if len(context.pages) > 1:
                            popup_save = context.pages[-1]
                            
                    target_save_page = popup_save if popup_save else page
                    await target_save_page.bring_to_front()
                    await target_save_page.wait_for_timeout(2000)
                    
                    # Click "pdf" option inside the popup to download the PDF file
                    await log_progress("Iniciando download do PDF do boleto...", "running")
                    pdf_option_sel = "xpath=//*[self::a or self::button or self::input][contains(normalize-space(.), 'pdf') or contains(normalize-space(.), 'PDF') or contains(normalize-space(text()), 'PDF') or contains(normalize-space(text()), 'pdf')]"
                    
                    # Capture the download
                    date_for_filename = datetime.date.today().strftime("%d-%m-%Y")
                    slug_client = slugify_name(client_name)
                    filename = f"Boleto_{slug_client}_{invoice_number}_{date_for_filename}.pdf"
                    pdf_path = os.path.join(invoice_folder, filename)
                    
                    async with page.expect_download(timeout=30000) as download_info:
                        await click_element(target_save_page, pdf_option_sel)
                    download = await download_info.value
                    await download.save_as(pdf_path)
                    
                    # Log success to database
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute("""
                        UPDATE emissions
                        SET boleto_status = 'gerado', boleto_pdf_path = ?, boleto_error_message = NULL, boleto_screenshot_path = NULL
                        WHERE id = ?
                    """, (pdf_path, emission_id))
                    conn.commit()
                    conn.close()
                    
                    boleto_url = f"/invoices/{folder_name}/{filename}"
                    await log_progress(f"Boleto gerado e salvo com sucesso para {client_name}!", "success", boleto_url=boleto_url)
                    
                except Exception as ex:
                    screenshot_filename = f"bradesco_{slugify_name(client_name)}_error.png"
                    screenshot_path = os.path.join(screenshot_folder, screenshot_filename)
                    try:
                        await page.screenshot(path=screenshot_path)
                    except Exception:
                        screenshot_path = None
                        
                    err_msg = str(ex)
                    await log_progress(f"Erro ao gerar boleto para {client_name}: {err_msg}", "error")
                    
                    # Log failure to database
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute("""
                        UPDATE emissions
                        SET boleto_status = 'erro', boleto_error_message = ?, boleto_screenshot_path = ?
                        WHERE id = ?
                    """, (err_msg, screenshot_path, emission_id))
                    conn.commit()
                    conn.close()
                    
                    # Return to Cobrança dashboard for next client
                    try:
                        await page.click("xpath=//a[normalize-space()='Cobrança' or contains(normalize-space(.), 'Cobrança')]")
                        await page.wait_for_timeout(2000)
                    except Exception:
                        pass
                        
            await log_progress("Automação de boletos concluída com sucesso!", "success")
            
        except Exception as global_ex:
            await log_progress(f"Erro global na automação do Bradesco: {str(global_ex)}", "error")
            raise global_ex
            
        finally:
            await browser.close()
