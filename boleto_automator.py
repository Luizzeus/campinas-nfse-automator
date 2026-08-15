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
    """Return boleto due date: day 10, rolled to next month if day 10 already passed."""
    today = datetime.date.today()
    month = today.month
    year = today.year

    if today.day > 10:
        month += 1
        if month > 12:
            month = 1
            year += 1

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
async def dismiss_promo_banners(page):
    """Close promotional interstitials (e.g. 'Contrate a Folha de Pagamento')
    that Bradesco overlays on top of the form and can swallow real clicks."""
    try:
        closers = page.locator(
            "xpath=//*[self::a or self::button or self::span or self::div]"
            "[normalize-space(text())='Fechar' or normalize-space(text())='fechar']"
        )
        count = await closers.count()
        for i in range(count):
            btn = closers.nth(i)
            try:
                if await btn.is_visible():
                    await btn.click(timeout=2000)
                    await page.wait_for_timeout(300)
            except Exception:
                pass
    except Exception:
        pass

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

async def select_payer_from_list(page, context, frame, cnpj_cpf, client_name, bradesco_payer_name=None, cep=None, endereco=None):
    """Fill the payer directly via 'Informar novo pagador' (name + CPF/CNPJ +
    CEP + endereço) instead of the "Lista de Pagadores" lookup. The lookup
    link reliably falls back to opening a real separate browser window/tab
    in our automated session (the target="modal_infra_estrutura" iframe it
    expects doesn't exist in the DOM yet when we click), and that popup's own
    JS then fails to hand the selection back to the parent form. Filling the
    document fields directly sidesteps that entirely and is fully within the
    same frame."""
    payer_name = bradesco_payer_name or client_name
    digits = re.sub(r"\D+", "", cnpj_cpf or "")
    cep_digits = re.sub(r"\D+", "", cep or "")

    if len(cep_digits) != 8:
        raise RuntimeError(
            f"CEP do pagador '{client_name}' ausente ou inválido ({cep!r}) - necessário para o cadastro no Bradesco."
        )
    if not endereco:
        raise RuntimeError(f"Endereço do pagador '{client_name}' ausente - necessário para o cadastro no Bradesco.")

    payer_field = frame.locator("id=frm:txtPagador").first
    await payer_field.wait_for(state="visible", timeout=10000)
    await payer_field.click()
    try:
        await payer_field.fill("")
    except Exception:
        pass
    await payer_field.press_sequentially(payer_name, delay=80)
    await frame.wait_for_timeout(800)

    cadastrar_btn = frame.locator("id=frm:cadastrarPagador").first
    await cadastrar_btn.click()
    await frame.wait_for_timeout(1200)

    if len(digits) == 11:
        pf_radio = frame.locator("id=frm:rdoPF").first
        if await pf_radio.count() > 0:
            await pf_radio.check()
            await frame.wait_for_timeout(500)
        for field_id, value in [
            ("frm:txtCPF1", digits[0:3]),
            ("frm:txtCPF2", digits[3:6]),
            ("frm:txtCPF3", digits[6:9]),
            ("frm:txtCPF4", digits[9:11]),
        ]:
            await frame.locator(f"id={field_id}").first.fill(value)
    elif len(digits) == 14:
        pj_radio = frame.locator("id=frm:rdoPJ").first
        if await pj_radio.count() > 0:
            await pj_radio.check()
            await frame.wait_for_timeout(500)
        for field_id, value in [
            ("frm:txtCNPJ1", digits[0:2]),
            ("frm:txtCNPJ2", digits[2:5]),
            ("frm:txtCNPJ3", digits[5:8]),
            ("frm:txtCNPJ4", digits[8:12]),
            ("frm:txtCNPJ5", digits[12:14]),
        ]:
            await frame.locator(f"id={field_id}").first.fill(value)
    else:
        raise RuntimeError(f"CNPJ/CPF com tamanho inesperado ({len(digits)} dígitos): {digits}")

    # CEP: campoCep1 (5 digits) + campoCep2 (3 digits), then "Consultar CEP"
    # auto-fills Estado/Cidade via AJAX.
    await frame.locator("id=frm:campoCep1").first.fill(cep_digits[0:5])
    await frame.locator("id=frm:campoCep2").first.fill(cep_digits[5:8])
    consultar_cep_btn = frame.locator("id=frm:consultaCepPagador").first
    await consultar_cep_btn.click()
    await frame.wait_for_timeout(2500)

    endereco_field = frame.locator("id=frm:inputEnderecoAux").first
    await endereco_field.fill(endereco)
    await endereco_field.dispatch_event("blur")
    await frame.wait_for_timeout(500)

    estado_text = None
    cidade_text = None
    try:
        estado_text = (await frame.locator("id=frm:estadoPagador").first.inner_text()).strip()
        cidade_text = (await frame.locator("id=frm:cidadePagador").first.inner_text()).strip()
    except Exception:
        pass
    print(f"[BRADESCO INFO] Após consultar CEP {cep}: Estado={estado_text!r} Cidade={cidade_text!r}")
    if not estado_text or not cidade_text:
        raise RuntimeError(f"Consulta de CEP não retornou Estado/Cidade para '{client_name}' (CEP {cep}).")

    payer_value = await payer_field.input_value()
    print(f"[BRADESCO INFO] Pagador '{client_name}' preenchido: nome={payer_value!r}, doc={digits}, cep={cep}, endereço={endereco}.")



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
            await page.wait_for_timeout(4000)

            await log_progress("Preenchendo usuário e senha do Bradesco...", "info")
            # Type with human-like keystrokes/delay instead of an instant .fill() -
            # too-fast automated fills can get bounced back to the login form by
            # the bank's anti-fraud checks.
            user_field = page.locator('input[id="identificationForm:txtUsuario"]').first
            await user_field.click()
            await user_field.press_sequentially(bradesco_user, delay=80)
            await page.wait_for_timeout(500)
            pass_field = page.locator('input[id="identificationForm:txtSenha"]').first
            await pass_field.click()
            await pass_field.press_sequentially(bradesco_password, delay=80)
            await page.wait_for_timeout(800)

            await log_progress("Clicando em Avançar no login do Bradesco...", "info")
            await click_element(page, 'input[id="identificationForm:botaoAvancar"]')
            await page.wait_for_timeout(3000)

            # A captcha/challenge may appear here before 2FA. Just let the
            # existing wait_for_bradesco_logged_in loop below ride it out -
            # it already polls for up to 3 minutes for the human to clear
            # whatever step (captcha and/or 2FA) is in the way.
            if await user_field.count() > 0 and await user_field.is_visible():
                await log_progress("Ainda na tela de login (possível captcha) - aguardando você resolver manualmente...", "warning")
            
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
                payer_cep = item.get("payer_cep") or ""
                payer_endereco = item.get("payer_endereco") or ""
                
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
                    await select_payer_from_list(page, context, frame, cnpj_cpf, client_name, bradesco_payer_name, cep=payer_cep, endereco=payer_endereco)
                    await page.bring_to_front()
                    frame = await get_central_frame(page)

                    # Dismiss any promotional banner (e.g. "Contrate a Folha de
                    # Pagamento") that can sit on top of the Avançar button and
                    # swallow the click.
                    await dismiss_promo_banners(page)

                    # Click Avançar on the filled boleto form.
                    await log_progress("Clicando em Avançar para abrir a confirmação do boleto...", "running")
                    avancar_sel = "id=frm:botaoAvancar"
                    avancar_btn = frame.locator(avancar_sel).first
                    await click_element(frame, avancar_sel)

                    # Verify we actually left "1. Dados da Emissão" instead of
                    # blindly sleeping and hoping the step changed.
                    left_step_1 = True
                    try:
                        await avancar_btn.wait_for(state="detached", timeout=10000)
                    except Exception:
                        try:
                            await avancar_btn.wait_for(state="hidden", timeout=5000)
                        except Exception:
                            left_step_1 = False
                    if not left_step_1:
                        await dismiss_promo_banners(page)
                        await click_element(frame, avancar_sel)
                        try:
                            await avancar_btn.wait_for(state="detached", timeout=10000)
                            left_step_1 = True
                        except Exception:
                            left_step_1 = False
                    if not left_step_1:
                        raise RuntimeError("Não foi possível sair da etapa 'Dados da Emissão' (botão Avançar não teve efeito).")

                    await page.wait_for_timeout(1500)
                    frame = await get_central_frame(page)
                    await dismiss_promo_banners(page)

                    # NOTE: clicking "Avançar" above already fully creates the
                    # boleto - this Bradesco flow has no separate confirmation
                    # click. What follows immediately is the "Confirmação de
                    # Operação" receipt (Nosso Número, barcode, QR code already
                    # assigned), not a review step. Confirmed by checking
                    # "Consultar Boletos" after a live run.
                    await log_progress("Boleto emitido - acessando recibo para salvar o PDF...", "running")

                    # Save generated PDF using "Salvar como arquivo" button.
                    # NOTE: use get_by_text (not raw xpath contains()) - the
                    # button labels use non-breaking spaces between words,
                    # which xpath's contains() does not treat as a normal
                    # space, causing silent match failures.
                    await log_progress("Acessando arquivo de boleto para download...", "running")
                    salvar_el = frame.get_by_text("Salvar como arquivo", exact=False).first

                    popup_save = None
                    try:
                        async with context.expect_page(timeout=10000) as page_info:
                            await salvar_el.click()
                        popup_save = await page_info.value
                    except Exception:
                        if len(context.pages) > 1:
                            popup_save = context.pages[-1]

                    target_save_page = popup_save if popup_save else page
                    await target_save_page.bring_to_front()
                    await target_save_page.wait_for_timeout(2000)

                    # Click "pdf" option inside the popup to download the PDF file.
                    # Search every frame on that page for the "pdf" option -
                    # its exact nesting isn't guaranteed.
                    await log_progress("Iniciando download do PDF do boleto...", "running")
                    pdf_el = None
                    for f in target_save_page.frames:
                        try:
                            candidate = f.get_by_text("pdf", exact=False).first
                            if await candidate.count() > 0:
                                pdf_el = candidate
                                break
                        except Exception:
                            continue
                    if pdf_el is None:
                        raise RuntimeError("Não encontrei a opção 'pdf' na tela de salvar arquivo.")

                    # Capture the download
                    date_for_filename = datetime.date.today().strftime("%d-%m-%Y")
                    slug_client = slugify_name(client_name)
                    filename = f"Boleto_{slug_client}_{invoice_number}_{date_for_filename}.pdf"
                    pdf_path = os.path.join(invoice_folder, filename)

                    async with target_save_page.expect_download(timeout=30000) as download_info:
                        await pdf_el.click()
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
