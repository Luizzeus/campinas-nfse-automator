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
async def remove_overlays(page):
    """Remove any modal overlays or windows blocking input in the DOM."""
    try:
        await page.evaluate("""() => {
            document.querySelectorAll('.jqmOverlay, .jqmWindow, [class*="Overlay"], [class*="modal"], [id*="Overlay"], [id*="jqm"]').forEach(el => el.remove());
        }""")
    except Exception:
        pass

async def click_element(page, selector, timeout_ms=15000):
    """Click an element, falling back to JS click to bypass overlays if standard click fails or is blocked."""
    locator = page.locator(selector).first
    try:
        await locator.wait_for(state="attached", timeout=timeout_ms)
        # Standard click (will throw if blocked/intercepted, allowing fallback to JS click)
        await locator.click(timeout=5000)
    except Exception:
        try:
            await locator.evaluate("(el) => el.click()")
        except Exception as e:
            raise RuntimeError(f"Failed to click selector {selector}: {e}")
async def fill_first_available(frame, selectors, value, timeout_ms=10000):
    for selector in selectors:
        try:
            locator = frame.locator(selector).first
            await locator.wait_for(state="attached", timeout=max(1000, timeout_ms // len(selectors)))
            await locator.fill(value)
            return True
        except Exception:
            pass
    raise RuntimeError(f"None of the selectors in {selectors} could be filled with '{value}'")

async def select_first_available(frame, selectors, label_or_val, timeout_ms=10000):
    for selector in selectors:
        try:
            locator = frame.locator(selector).first
            await locator.wait_for(state="attached", timeout=max(1000, timeout_ms // len(selectors)))
            try:
                await locator.select_option(label=label_or_val, timeout=2000)
            except Exception:
                await locator.select_option(value=label_or_val, timeout=2000)
            return True
        except Exception:
            pass
    raise RuntimeError(f"None of the selectors in {selectors} could select option '{label_or_val}'")

async def click_first_available(frame, selectors, timeout_ms=15000):
    for selector in selectors:
        try:
            locator = frame.locator(selector).first
            await locator.wait_for(state="attached", timeout=max(1000, timeout_ms // len(selectors)))
            try:
                await locator.click(timeout=3000)
            except Exception:
                await locator.evaluate("(el) => el.click()")
            return True
        except Exception:
            pass
    raise RuntimeError(f"None of the selectors in {selectors} could be clicked")

async def select_dropdown_option(frame, select_selectors, search_texts, timeout_ms=10000):
    for selector in select_selectors:
        try:
            locator = frame.locator(selector).first
            await locator.wait_for(state="attached", timeout=max(1000, timeout_ms // len(select_selectors)))
            options = await locator.evaluate("""(select) => {
                return Array.from(select.options).map(opt => ({
                    text: opt.text,
                    value: opt.value,
                    index: opt.index
                }));
            }""")
            for search_text in search_texts:
                for opt in options:
                    if search_text.lower() in opt["text"].lower() or search_text.lower() == opt["value"].lower():
                        await locator.select_option(index=opt["index"])
                        return True
        except Exception:
            pass
    raise RuntimeError(f"None of the selectors in {select_selectors} could select option matching {search_texts}")



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
                    await remove_overlays(page)
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
                    doc_selectors = [
                        "xpath=//input[contains(@id, 'numDocumento') or contains(@name, 'numDocumento') or contains(@id, 'NumeroDocumento') or contains(@id, 'txtNumero') or contains(@id, 'txtNroDoc')]",
                        "xpath=//*[contains(text(), 'documento *') or contains(., 'documento *') or contains(text(), 'Documento *') or contains(., 'Documento *')]/following::input[1]",
                        "xpath=//*[contains(text(), 'Número do documento') or contains(., 'Número do documento') or contains(text(), 'Numero do documento') or contains(., 'Numero do documento')]/following::input[1]",
                        "xpath=//td[contains(., 'documento') or contains(., 'Documento')]/following::input[1]"
                    ]
                    await fill_first_available(frame, doc_selectors, str(invoice_number))
                    
                    # 1.5. Emission Date (Data de Emissão - data atual)
                    try:
                        today = ref_date or datetime.date.today()
                        day_em, month_em, year_em = f"{today.day:02d}", f"{today.month:02d}", f"{today.year}"
                        
                        emissao_dia_selectors = [
                            "xpath=//input[contains(@id, 'diaEmissao') or contains(@id, 'dtEmissaoDia') or contains(@id, 'txtDiaEmissao')]",
                            "xpath=//*[contains(text(), 'Emissão') or contains(text(), 'Emissao') or contains(., 'Emissão') or contains(., 'Emissao')]/following::input[1]"
                        ]
                        emissao_mes_selectors = [
                            "xpath=//input[contains(@id, 'mesEmissao') or contains(@id, 'dtEmissaoMes') or contains(@id, 'txtMesEmissao')]",
                            "xpath=//*[contains(text(), 'Emissão') or contains(text(), 'Emissao') or contains(., 'Emissão') or contains(., 'Emissao')]/following::input[2]"
                        ]
                        emissao_ano_selectors = [
                            "xpath=//input[contains(@id, 'anoEmissao') or contains(@id, 'dtEmissaoAno') or contains(@id, 'txtAnoEmissao')]",
                            "xpath=//*[contains(text(), 'Emissão') or contains(text(), 'Emissao') or contains(., 'Emissão') or contains(., 'Emissao')]/following::input[3]"
                        ]
                        await fill_first_available(frame, emissao_dia_selectors, day_em, timeout_ms=2000)
                        await fill_first_available(frame, emissao_mes_selectors, month_em, timeout_ms=2000)
                        await fill_first_available(frame, emissao_ano_selectors, year_em, timeout_ms=2000)
                    except Exception as e:
                        await log_progress(f"Data de emissão já preenchida ou não editável: {str(e)}", "running")
                    
                    # 2. Due Date (Vencimento)
                    day, month, year = get_due_date_for_client(ref_date, due_day)
                    await log_progress(f"Calculada data de vencimento: {day}/{month}/{year}", "running")
                    
                    day_selectors = [
                        "xpath=//input[contains(@id, 'diaVencimento') or contains(@id, 'dtVencimentoDia') or contains(@id, 'txtDiaVenc')]",
                        "xpath=//*[contains(text(), 'Vencimento') or contains(., 'Vencimento')]/following::input[1]"
                    ]
                    month_selectors = [
                        "xpath=//input[contains(@id, 'mesVencimento') or contains(@id, 'dtVencimentoMes') or contains(@id, 'txtMesVenc')]",
                        "xpath=//*[contains(text(), 'Vencimento') or contains(., 'Vencimento')]/following::input[2]"
                    ]
                    year_selectors = [
                        "xpath=//input[contains(@id, 'anoVencimento') or contains(@id, 'dtVencimentoAno') or contains(@id, 'txtAnoVenc')]",
                        "xpath=//*[contains(text(), 'Vencimento') or contains(., 'Vencimento')]/following::input[3]"
                    ]
                    await fill_first_available(frame, day_selectors, day)
                    await fill_first_available(frame, month_selectors, month)
                    await fill_first_available(frame, year_selectors, year)
                    
                    # 3. Document Value (Valor do Documento)
                    val_selectors = [
                        "xpath=//input[contains(@id, 'valor') or contains(@id, 'vlDoc') or contains(@id, 'txtValor') or contains(@id, 'vlDocumento')]",
                        "xpath=//*[contains(text(), 'Valor do Documento') or contains(., 'Valor do Documento')]/following::input[1]",
                        "xpath=//*[contains(text(), 'Valor') or contains(., 'Valor')]/following::input[1]"
                    ]
                    val_str = f"{boleto_value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                    await fill_first_available(frame, val_selectors, val_str)
                    
                    # 4. Multa e Juros
                    # Multa: Select %, Value 2,00, Days 1
                    multa_selectors = [
                        "xpath=//select[contains(@id, 'multa') or contains(@id, 'tipoMulta') or contains(@id, 'selMulta')]",
                        "xpath=//*[contains(text(), 'Multa') or contains(., 'Multa')]/following::select[1]"
                    ]
                    multa_val_selectors = [
                        "xpath=//input[contains(@id, 'vlMulta') or contains(@id, 'pctMulta') or contains(@id, 'txtMulta')]",
                        "xpath=//*[contains(text(), 'Multa') or contains(., 'Multa')]/following::input[1]"
                    ]
                    multa_dias_selectors = [
                        "xpath=//input[contains(@id, 'diasMulta') or contains(@id, 'atrasoMulta')]",
                        "xpath=//*[contains(text(), 'Multa') or contains(., 'Multa')]/following::input[2]"
                    ]
                    
                    await select_dropdown_option(frame, multa_selectors, ["%", "percentual", "percent", "taxa"])
                    await fill_first_available(frame, multa_val_selectors, "2,00")
                    await fill_first_available(frame, multa_dias_selectors, "1")
                    
                    # Juros: Select %, Value 1,00, Days 1
                    juros_selectors = [
                        "xpath=//select[contains(@id, 'juros') or contains(@id, 'tipoJuros') or contains(@id, 'selJuros')]",
                        "xpath=//*[contains(text(), 'Juros') or contains(., 'Juros')]/following::select[1]"
                    ]
                    juros_val_selectors = [
                        "xpath=//input[contains(@id, 'vlJuros') or contains(@id, 'pctJuros') or contains(@id, 'txtJuros')]",
                        "xpath=//*[contains(text(), 'Juros') or contains(., 'Juros')]/following::input[1]"
                    ]
                    juros_dias_selectors = [
                        "xpath=//input[contains(@id, 'diasJuros') or contains(@id, 'atrasoJuros')]",
                        "xpath=//*[contains(text(), 'Juros') or contains(., 'Juros')]/following::input[2]"
                    ]
                    
                    await select_dropdown_option(frame, juros_selectors, ["%", "percentual", "percent", "taxa"])
                    await fill_first_available(frame, juros_val_selectors, "1,00")
                    await fill_first_available(frame, juros_dias_selectors, "1")
                        
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
