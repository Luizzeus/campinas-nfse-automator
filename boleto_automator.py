import asyncio
import os
import re
import datetime
from calendar import monthrange
import unicodedata
from urllib.parse import urljoin
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

PAYER_NAME_ALIASES = {
    "victor mammana": "VICTOR PELLEGRINI MAMMANA",
}

def normalize_ascii(text):
    return unicodedata.normalize("NFKD", text or "").encode("ASCII", "ignore").decode("ASCII")

def get_payer_search_names(client_name, bradesco_payer_name=None):
    normalized_key = re.sub(r"\s+", " ", normalize_ascii(client_name).lower()).strip()
    names = []
    if bradesco_payer_name:
        names.append(bradesco_payer_name)
    alias = PAYER_NAME_ALIASES.get(normalized_key)
    if alias and alias not in names:
        names.append(alias)
    if client_name and client_name not in names:
        names.append(client_name)
    return names

def get_due_date_for_client(ref_date=None, due_day=None):
    """Return boleto due date: always day 10 of the current execution month."""
    today = datetime.date.today()
    month = today.month
    year = today.year
    
    days_in_month = monthrange(year, month)[1]
    day = min(10, days_in_month)
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
            try:
                await locator.click(timeout=1000)
            except Exception:
                pass
            await locator.fill(value)
            try:
                await locator.dispatch_event("change")
                await locator.dispatch_event("blur")
            except Exception:
                pass
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
                        try:
                            await locator.dispatch_event("change")
                        except Exception:
                            pass
                        return True
        except Exception:
            pass
    raise RuntimeError(f"None of the selectors in {select_selectors} could select option matching {search_texts}")

async def set_percent_charge(frame, select_id, value_id, days_id, percent_value, days_value):
    select = frame.locator(f"id={select_id}").first
    await select.wait_for(state="attached", timeout=10000)
    await select.select_option(value="2")
    await select.dispatch_event("change")
    try:
        await frame.evaluate(
            """({selectId}) => {
                const sel = document.getElementById(selectId);
                if (sel) {
                    sel.value = '2';
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                    if (selectId.endsWith('selectMulta') && typeof checkSelectMulta === 'function') checkSelectMulta();
                    if (selectId.endsWith('selectJuros') && typeof checkSelectJuros === 'function') checkSelectJuros();
                }
            }""",
            {"selectId": select_id}
        )
    except Exception:
        pass
    await frame.wait_for_timeout(500)
    for field_id, field_value in [(value_id, percent_value), (days_id, days_value)]:
        field = frame.locator(f"id={field_id}").first
        await field.wait_for(state="attached", timeout=10000)
        try:
            await field.evaluate("(el) => { el.disabled = false; el.readOnly = false; el.removeAttribute('disabled'); el.removeAttribute('readonly'); }")
        except Exception:
            pass
        await field.fill(str(field_value))
        await field.dispatch_event("change")
        await field.dispatch_event("blur")

async def get_central_frame(page):
    frame = page.frame(name="paginaCentral")
    if not frame:
        for f in page.frames:
            if f.name == "paginaCentral" or "paginaCentral" in f.url or "Cobranca" in f.url or "cobranca" in f.url:
                frame = f
                break
    return frame or page

async def find_first_visible_in_contexts(contexts, selectors, timeout_ms=10000):
    deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)
    last_error = None
    while datetime.datetime.now() < deadline:
        for ctx in contexts:
            for selector in selectors:
                try:
                    locator = ctx.locator(selector)
                    count = await locator.count()
                    for index in range(count):
                        item = locator.nth(index)
                        if await item.is_visible():
                            return ctx, item
                except Exception as exc:
                    last_error = exc
        await asyncio.sleep(0.3)
    raise RuntimeError(f"Nenhum seletor visível encontrado: {selectors}. Último erro: {last_error}")

async def select_payer_from_list(page, context, frame, cnpj_cpf, client_name, bradesco_payer_name=None):
    lista_pagadores_sel = "id=frm:linkListaPagadores"
    popup = None
    href = None
    try:
        href = await frame.locator(lista_pagadores_sel).first.get_attribute("href")
    except Exception:
        href = None

    try:
        async with context.expect_page(timeout=10000) as page_info:
            await click_element(frame, lista_pagadores_sel)
        popup = await page_info.value
    except Exception:
        if len(context.pages) > 1:
            popup = context.pages[-1]

    if popup == page:
        popup = None
    if not popup and href:
        popup = await context.new_page()
        await popup.goto(urljoin(frame.url, href), timeout=30000)

    target_page = popup if popup else page
    await target_page.bring_to_front()
    await target_page.wait_for_timeout(3000)

    # Dump popup DOM for debugging
    try:
        popup_html = await target_page.content()
        with open("C:/Projetos/campinas-nfse-automator/popup_dom.html", "w", encoding="utf-8") as f:
            f.write(popup_html)
        print("[BRADESCO INFO] DOM do popup salvo com sucesso em popup_dom.html!")
    except Exception as e:
        print(f"[BRADESCO WARNING] Erro ao salvar DOM do popup: {e}")
        
    raise RuntimeError("DOM do popup salvo! Parando para análise de seletores.")



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
                bradesco_payer_name = item.get("bradesco_payer_name") or ""
                
                await log_progress(f"Iniciando geração de boleto para: {client_name} (Nota Nº {invoice_number})", "running")
                
                try:
                    # Navigate to "Cobrança" tab
                    await log_progress("Navegando para o menu Cobrança...", "running")
                    await remove_overlays(page)
                    cobrança_sel = "xpath=//a[normalize-space()='Cobrança' or contains(normalize-space(.), 'Cobrança')]"
                    await click_element(page, cobrança_sel)
                    await page.wait_for_timeout(3000)
                    
                    # Locate the central frame
                    frame = await get_central_frame(page)
                    if frame == page:
                        await log_progress("Aviso: Quadro paginaCentral não encontrado. Usando página principal.", "warning")
                    else:
                        await log_progress("Quadro paginaCentral localizado com sucesso.", "running")
                    
                    # Click "Emitir Boleto"
                    await log_progress("Clicando em Emitir Boleto...", "running")
                    emitir_sel = "xpath=//a[normalize-space()='Emitir Boleto' or contains(normalize-space(.), 'Emitir Boleto')]"
                    await click_element(frame, emitir_sel)
                    await page.wait_for_timeout(3000)
                    
                    # Dump frame HTML for debugging
                    try:
                        html_content = await frame.content()
                        with open("C:/Projetos/campinas-nfse-automator/frame_dom.html", "w", encoding="utf-8") as f:
                            f.write(html_content)
                        await log_progress("DOM do iframe salvo com sucesso em frame_dom.html!", "running")
                    except Exception as e:
                        await log_progress(f"Erro ao salvar DOM do iframe: {e}", "warning")
                    
                    # Passo 2: Fill Boleto Details Form
                    await log_progress("Preenchendo detalhes do boleto...", "running")
                    
                    # 1. Document Number (same as NFS-e)
                    doc_selectors = ["id=frm:txtSeuNumero"]
                    await fill_first_available(frame, doc_selectors, str(invoice_number))
                    
                    # 1.5. Emission Date (Data de Emissão - data atual)
                    try:
                        today = ref_date or datetime.date.today()
                        day_em, month_em, year_em = f"{today.day:02d}", f"{today.month:02d}", f"{today.year}"
                        
                        emissao_dia_selectors = ["id=frm:boxCalendarioEmissaoDia"]
                        
                        # Only fill if the field is empty or "00"
                        dia_loc = frame.locator(emissao_dia_selectors[0]).first
                        has_value = False
                        if await dia_loc.count() > 0:
                            current_val = await dia_loc.input_value()
                            if current_val and current_val != "00" and current_val != "":
                                has_value = True
                                await log_progress(f"Data de emissão já preenchida com {current_val}. Mantendo.", "running")
                        
                        if not has_value:
                            emissao_mes_selectors = ["id=frm:boxCalendarioEmissaoMes"]
                            emissao_ano_selectors = ["id=frm:boxCalendarioEmissaoAno"]
                            await fill_first_available(frame, emissao_dia_selectors, day_em, timeout_ms=2000)
                            await fill_first_available(frame, emissao_mes_selectors, month_em, timeout_ms=2000)
                            await fill_first_available(frame, emissao_ano_selectors, year_em, timeout_ms=2000)
                    except Exception as e:
                        await log_progress(f"Erro ao verificar/preencher data de emissão: {str(e)}", "running")
                    
                    # Close any active floating calendar popups
                    try:
                        await page.keyboard.press("Escape")
                        await page.wait_for_timeout(500)
                    except Exception:
                        pass
                    
                    # 2. Due Date (Vencimento): always day 10 of the current month.
                    day, month, year = get_due_date_for_client()
                    await log_progress(f"Calculada data de vencimento: {day}/{month}/{year}", "running")
                    
                    # Bradesco has separate vencimento input fields when QR Code is enabled vs disabled
                    venc_dia_qrcode = frame.locator("id=frm:boxCalendarioVencimentoComQRCodeDia").first
                    if await venc_dia_qrcode.count() > 0 and await venc_dia_qrcode.is_visible():
                        day_selectors = ["id=frm:boxCalendarioVencimentoComQRCodeDia"]
                        month_selectors = ["id=frm:boxCalendarioVencimentoComQRCodeMes"]
                        year_selectors = ["id=frm:boxCalendarioVencimentoComQRCodeAno"]
                    else:
                        day_selectors = ["id=frm:boxCalendarioVencimentoDia"]
                        month_selectors = ["id=frm:boxCalendarioVencimentoMes"]
                        year_selectors = ["id=frm:boxCalendarioVencimentoAno"]
                        
                    await fill_first_available(frame, day_selectors, day)
                    await fill_first_available(frame, month_selectors, month)
                    await fill_first_available(frame, year_selectors, year)
                    
                    # 3. Document Value (Valor do Documento)
                    val_selectors = ["id=frm:txtValorDocumento"]
                    val_str = f"{boleto_value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                    await fill_first_available(frame, val_selectors, val_str)
                    
                    # 4. Multa e Juros
                    # Multa: % 2,00, cobrar após 1 dia do vencimento.
                    await set_percent_charge(frame, "frm:selectMulta", "frm:textValorMulta", "frm:vencimentoMulta", "2,00", "1")

                    # Juros: % 1,00, cobrar após 1 dia do vencimento.
                    await set_percent_charge(frame, "frm:selectJuros", "frm:textValorJuros", "frm:vencimentoJuros", "1,00", "1")

                    # 5. Pagador Search & Selection, before the first Avançar.
                    await log_progress("Selecionando pagador (cliente)...", "running")
                    await select_payer_from_list(page, context, frame, cnpj_cpf, client_name, bradesco_payer_name)
                    await page.bring_to_front()
                    frame = await get_central_frame(page)

                    # Click Avançar on the filled boleto form.
                    await log_progress("Clicando em Avançar para abrir a confirmação do boleto...", "running")
                    avancar_sel = "id=frm:botaoAvancar"
                    await click_element(frame, avancar_sel)
                    await page.wait_for_timeout(4000)

                    frame = await get_central_frame(page)
                    await log_progress("Confirmando emissão do boleto...", "running")
                    confirmar_sel = "xpath=//*[self::input or self::button or self::a][contains(@value, 'Avançar') or contains(@value, 'Avancar') or contains(normalize-space(.), 'Avançar') or contains(normalize-space(.), 'Avancar') or contains(@value, 'Emitir') or contains(normalize-space(.), 'Emitir') or contains(@id, 'botaoAvancar') or contains(@id, 'botaoConfirmar')]"
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
                    
                    async with target_save_page.expect_download(timeout=30000) as download_info:
                        await click_element(target_save_page, pdf_option_sel)
                    download = await download_info.value
                    await download.save_as(pdf_path)
                    
                    # Log success to database
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute("""
                        UPDATE emissions
                        SET boleto_status = 'gerado',
                            boleto_pdf_path = ?,
                            boleto_due_date = ?,
                            boleto_value = ?,
                            boleto_error_message = NULL,
                            boleto_screenshot_path = NULL
                        WHERE id = ?
                    """, (pdf_path, f"{day}/{month}/{year}", boleto_value, emission_id))
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
