import asyncio
import base64
import os
import re
import datetime
import subprocess
import unicodedata
import traceback
from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright
from database import get_db_connection
from utils import get_competence_info, format_description

# Directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INVOICES_DIR = os.path.join(BASE_DIR, "invoices")
SCREENSHOTS_DIR = os.path.join(BASE_DIR, "screenshots")
LOGIN_URL = "https://novanfse.campinas.sp.gov.br/notafiscal/paginas/portal/index.html#/login"
PRINCIPAL_URL = "https://novanfse.campinas.sp.gov.br/notafiscal/paginas/portal/index.html#/principal"
NEW_EMISSOR_LIST_URL = "https://novanfse.campinas.sp.gov.br/notafiscal/paginas/notafiscalnacional/notaFiscalNacionalList.jsf"
VALIDATION_ONLY = False
VALIDATION_PAUSE_SECONDS = 900
DESCRIPTION_SELECTORS = [
    'xpath=//textarea[contains(@id, "itDescricao") or contains(@name, "itDescricao")]',
    "xpath=//*[contains(normalize-space(.), 'Descrição Nota Fiscal')]/following::textarea[1]",
    "xpath=//div[.//*[contains(normalize-space(.), 'Informações da Nota')]]//textarea",
    "textarea",
]

for d in [INVOICES_DIR, SCREENSHOTS_DIR]:
    os.makedirs(d, exist_ok=True)

def slugify_name(name):
    """Normalize client name for filenames (no accents, spaces to underscores)."""
    n = unicodedata.normalize('NFKD', name).encode('ASCII', 'ignore').decode('ASCII')
    n = re.sub(r'[^a-zA-Z0-9_\s-]', '', n)
    n = n.replace('/', '_').replace('\\', '_')
    return '_'.join(n.split())

async def is_recaptcha_solved(page):
    """Return True when Google reCAPTCHA v2 writes a response token to the page."""
    try:
        return await page.evaluate("""
            () => {
                const el = document.getElementById('g-recaptcha-response');
                return Boolean(el && el.value && el.value.length > 0);
            }
        """)
    except Exception:
        return False

async def click_login_button(page):
    selectors = [
        "xpath=//*[self::button or self::a][normalize-space()='Entrar' or .//*[normalize-space()='Entrar']]",
        "xpath=//*[self::button or self::a][contains(normalize-space(.), 'Entrar')]",
        "button[type='submit']",
    ]
    last_error = None
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible():
                await locator.click(force=True)
                return True
        except Exception as exc:
            last_error = exc
    if last_error:
        raise last_error
    return False

async def wait_for_logged_in(page, timeout_ms=20000):
    try:
        await page.wait_for_selector("span.nav-label", state="attached", timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return "#/login" not in page.url

async def first_visible_locator(page, selector, timeout_ms=10000, require_enabled=False):
    deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)
    last_count = 0
    while datetime.datetime.now() < deadline:
        locator = page.locator(selector)
        last_count = await locator.count()
        for index in range(last_count):
            item = locator.nth(index)
            try:
                if await item.is_visible() and (not require_enabled or await item.is_enabled()):
                    return item
            except Exception:
                continue
        await page.wait_for_timeout(300)
    enabled_text = " enabled" if require_enabled else ""
    raise PlaywrightTimeoutError(f"No visible{enabled_text} element found for selector {selector!r}; matched {last_count} elements")

async def click_first_visible(page, selector, timeout_ms=10000):
    locator = await first_visible_locator(page, selector, timeout_ms=timeout_ms)
    try:
        await locator.click(force=True)
    except Exception:
        await locator.evaluate("(el) => el.click()")

async def fill_first_visible(page, selector, value, timeout_ms=10000):
    locator = await first_visible_locator(page, selector, timeout_ms=timeout_ms, require_enabled=True)
    await locator.click()
    await locator.fill(str(value))
    return locator

async def fill_invoice_service_value(page, value_br, timeout_ms=15000):
    """Fill the editable Valor dos Servicos field and verify the portal kept it."""
    expected_digits = re.sub(r"\D+", "", str(value_br))
    deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)
    last_result = None
    while datetime.datetime.now() < deadline:
        last_result = await page.evaluate("""
            () => {
                const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return (
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        rect.width > 0 &&
                        rect.height > 0
                    );
                };
                
                const inputs = Array.from(document.querySelectorAll('input'))
                    .filter(input => visible(input) && !input.disabled && !input.readOnly && (input.type || '').toLowerCase() !== 'hidden');
                    
                const norm = (text) => (text || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
                const labels = Array.from(document.querySelectorAll('*'))
                    .filter(el => {
                        if (el.tagName === 'INPUT' || el.tagName === 'SCRIPT' || el.tagName === 'STYLE') return false;
                        if (!visible(el)) return false;
                        const txt = norm(el.innerText || el.textContent || '');
                        return txt.includes('valor dos servicos') && !txt.includes('calculo');
                    });

                let target = null;
                let minDistance = Infinity;

                for (const input of inputs) {
                    const inputRect = input.getBoundingClientRect();
                    for (const label of labels) {
                        const labelRect = label.getBoundingClientRect();
                        if (inputRect.top > labelRect.top - 10) {
                            const dist = Math.abs(inputRect.top - labelRect.bottom) + Math.abs(inputRect.left - labelRect.left);
                            if (dist < minDistance) {
                                minDistance = dist;
                                target = input;
                            }
                        }
                    }
                }

                if (!target) {
                    target = inputs.find(input => input.value === '0,00' || input.value === '0');
                }

                if (!target) {
                    const candidates = inputs.map((input) => {
                        const rect = input.getBoundingClientRect();
                        return {id: input.id || '', name: input.name || '', value: input.value || '', top: Math.round(rect.top), left: Math.round(rect.left)};
                    }).slice(0, 12);
                    return {success: false, error: 'Campo editavel Valor dos Servicos nao encontrado', candidates};
                }
                
                target.scrollIntoView({block: 'center', inline: 'nearest'});
                return {
                    success: true,
                    value: target.value || '',
                    id: target.id || '',
                    name: target.name || '',
                    labelText: 'Valor dos Serviços'
                };
            }
        """)
        if last_result and last_result.get("success"):
            target_id = last_result.get("id")
            field_sel = f'[id="{target_id}"]'
            field = page.locator(field_sel).first
            await field.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await field.press_sequentially(str(value_br), delay=80)
            await field.press("Tab")
            await page.wait_for_timeout(2000)
            
            # Re-locate the input element using the ID selector (which automatically finds the newly rendered element after AJAX)
            current_value = await page.locator(field_sel).first.input_value()
            current_digits = re.sub(r"\D+", "", str(current_value or ""))
            if current_digits == expected_digits:
                last_result["value"] = current_value
                return last_result
            last_result["typedValue"] = current_value
        await page.wait_for_timeout(500)
    raise RuntimeError(f"Valor dos Serviços não aceitou {value_br}. Último retorno: {last_result}")

async def wait_tax_calculation_ready(page, timeout_ms=30000):
    """Wait until ISSQN fields are no longer masked as ***** after value/tomador changes."""
    deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)
    last_snapshot = None
    while datetime.datetime.now() < deadline:
        last_snapshot = await page.evaluate("""
            () => {
                const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                };
                const norm = (text) => (text || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
                const form = document.querySelector('form#formNotaFiscal') || document;
                const inputs = Array.from(form.querySelectorAll('input')).filter(visible);
                const relevant = inputs.filter((input) => {
                    let node = input;
                    for (let depth = 0; depth < 6 && node; depth += 1, node = node.parentElement) {
                        const text = norm(node.innerText || node.textContent || '');
                        if (text.includes('calculo do issqn') || text.includes('aliquota') || text.includes('valor iss')) {
                            return true;
                        }
                    }
                    return false;
                }).map((input) => input.value || '');
                return {
                    values: relevant,
                    loading: relevant.some((value) => String(value).includes('*****'))
                };
            }
        """)
        if last_snapshot and last_snapshot.get("values") and not last_snapshot.get("loading"):
            return last_snapshot
        await page.wait_for_timeout(1000)
    raise RuntimeError(f"Portal não concluiu o cálculo do ISSQN; campos ainda em carregamento: {last_snapshot}")

async def zero_retention_fields(page, taxes_to_zero):
    """Zero both percent and value inputs for the requested retention rows."""
    targets = await page.evaluate("""
        (taxes) => {
            const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
            };
            const norm = (text) => (text || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
            const wanted = taxes.map(norm);
            const rows = Array.from(document.querySelectorAll('tr'));
            document.querySelectorAll('[data-codex-retention-target]').forEach((el) => el.removeAttribute('data-codex-retention-target'));
            const targets = [];
            for (const row of rows) {
                const rowText = norm(row.innerText || row.textContent || '');
                const tax = wanted.find((name) => rowText.includes(name));
                if (!tax) continue;
                const inputs = Array.from(row.querySelectorAll('input')).filter((input) => visible(input) && !input.disabled && !input.readOnly);
                for (const input of inputs) {
                    const current = input.value || '';
                    if (/^0([,.]0+)?$/.test(current.trim())) continue;
                    const decimalPart = (current.split(',')[1] || current.split('.')[1] || '');
                    const value = decimalPart.length >= 4 ? '0,0000' : '0,00';
                    input.setAttribute('data-codex-retention-target', String(targets.length));
                    targets.push({tax, id: input.id || '', name: input.name || '', previous: current, value});
                }
            }

            const all = Array.from(document.querySelectorAll('*'));
            const outrasLabel = all
                .map((el, index) => ({el, index, text: norm(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim()}))
                .find((item) => visible(item.el) && item.text === 'outras retencoes');
            if (outrasLabel) {
                const input = all.slice(outrasLabel.index + 1).find((el) =>
                    el.tagName === 'INPUT' && visible(el) && !el.disabled && !el.readOnly && (el.type || '').toLowerCase() !== 'hidden'
                );
                if (input && !/^0([,.]0+)?$/.test((input.value || '').trim())) {
                    input.setAttribute('data-codex-retention-target', String(targets.length));
                    targets.push({tax: 'outras', id: input.id || '', name: input.name || '', previous: input.value || '', value: '0,00'});
                }
            }
            return targets;
        }
    """, taxes_to_zero)
    changed = []
    for index, target in enumerate(targets or []):
        locator = page.locator(f'[data-codex-retention-target="{index}"]').first
        try:
            await locator.scroll_into_view_if_needed()
            await locator.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await locator.press_sequentially(target["value"], delay=40)
            await locator.press("Tab")
            await page.wait_for_timeout(200)
            final_value = await locator.input_value()
            if re.match(r"^0([,.]0+)?$", final_value.strip()):
                target["final"] = final_value
                changed.append(target)
        except Exception as exc:
            target["error"] = str(exc)
            changed.append(target)
    await page.wait_for_timeout(1000)
    return changed

async def validate_manual_invoice_values(page, expected_service_value, taxes_to_zero):
    """Validate visible service value and retention fields before clicking Emitir."""
    expected_digits = re.sub(r"\D+", "", str(expected_service_value))
    snapshot = await page.evaluate("""
        (taxes) => {
            const visible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return (
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    rect.width > 0 &&
                    rect.height > 0
                );
            };
            
            const inputs = Array.from(document.querySelectorAll('input'))
                .filter(input => visible(input) && !input.disabled && !input.readOnly && (input.type || '').toLowerCase() !== 'hidden');
                
            const normLabel = (text) => (text || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
            const labels = Array.from(document.querySelectorAll('*'))
                .filter(el => {
                    if (el.tagName === 'INPUT' || el.tagName === 'SCRIPT' || el.tagName === 'STYLE') return false;
                    if (!visible(el)) return false;
                    const txt = normLabel(el.innerText || el.textContent || '');
                    return txt.includes('valor dos servicos') && !txt.includes('calculo');
                });

            let target = null;
            let minDistance = Infinity;

            for (const input of inputs) {
                const inputRect = input.getBoundingClientRect();
                for (const label of labels) {
                    const labelRect = label.getBoundingClientRect();
                    if (inputRect.top > labelRect.top - 10) {
                        const dist = Math.abs(inputRect.top - labelRect.bottom) + Math.abs(inputRect.left - labelRect.left);
                        if (dist < minDistance) {
                            minDistance = dist;
                            target = input;
                        }
                    }
                }
            }

            if (!target) {
                target = inputs.find(input => input.value === '0,00' || input.value === '0');
            }
            
            let serviceValue = '';
            if (target) {
                serviceValue = target.value || '';
            }

            const norm = (text) => (text || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
            const wanted = taxes.map(norm);
            const nonZeroRetentions = [];
            for (const row of Array.from(document.querySelectorAll('tr'))) {
                const rowText = norm(row.innerText || row.textContent || '');
                const tax = wanted.find((name) => rowText.includes(name));
                if (!tax) continue;
                const rowInputs = Array.from(row.querySelectorAll('input')).filter((input) => visible(input) && !input.disabled && !input.readOnly);
                for (const input of rowInputs) {
                    const value = input.value || '';
                    if (!/^0([,.]0+)?$/.test(value.trim())) {
                        nonZeroRetentions.push({tax, value, id: input.id || '', name: input.name || ''});
                    }
                }
            }
            return {serviceValue, nonZeroRetentions};
        }
    """, taxes_to_zero)
    service_digits = re.sub(r"\D+", "", str(snapshot.get("serviceValue") or ""))
    if service_digits != expected_digits:
        raise RuntimeError(
            f"Valor dos Serviços visível não confere antes de emitir. "
            f"Esperado {expected_service_value}, encontrado {snapshot.get('serviceValue') or 'vazio'}."
        )
    non_zero = snapshot.get("nonZeroRetentions") or []
    if non_zero:
        details = "; ".join(f"{item.get('tax')}={item.get('value')}" for item in non_zero)
        raise RuntimeError(f"Retenções ainda não estão zeradas antes de emitir: {details}")
    return snapshot

async def select_economic_activity(page, code="620400001", timeout_ms=20000):
    """Select and confirm Atividade do Serviço Tomado by service/CNAE code.

    The portal (as of 2026-10) exposes this as a 'lookup' widget: a small
    read-only pair of inputs (código + descrição) next to a magnifying-glass
    button. Typing directly into the código box and blurring does NOT
    resolve it (confirmed live - the description stays empty); the only
    working path is opening the lookup dialog, searching by CNAE code in its
    'Atividade' tab, and clicking the matching result row.
    """
    desc_field = page.locator('xpath=//input[contains(@id, "idAtividadeLivre:descriptionLookup")]').first
    if await desc_field.count() > 0:
        current_desc = (await desc_field.input_value() or "").strip()
        if current_desc:
            return {"alreadySelected": True, "text": current_desc}

    magnifier_sel = 'xpath=//td[.//input[contains(@id,"idAtividadeLivre:codeLookup")]]/following-sibling::td//a[1]'
    dialog_sel = '[id="formNotaFiscal:idAtividadeLivre:idDialog"]'

    if await page.locator(magnifier_sel).count() > 0:
        deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)
        last_error = None
        while datetime.datetime.now() < deadline:
            try:
                await click_first_visible(page, magnifier_sel, timeout_ms=5000)
                dialog = page.locator(dialog_sel)
                await dialog.wait_for(state="visible", timeout=5000)

                cnae_input = dialog.locator('xpath=.//input[contains(@id, "filterSearchcodigoCnae")]').first
                await cnae_input.click()
                await cnae_input.fill(code)

                search_btn = dialog.locator("xpath=.//a[contains(normalize-space(.), 'Pesquisar')]").first
                await search_btn.click()
                await page.wait_for_timeout(1500)

                row = dialog.locator(f"xpath=.//tr[td[normalize-space()='{code}']]").first
                await row.wait_for(state="visible", timeout=8000)
                row_text = (await row.inner_text()).replace("\n", " ").strip()
                # The first (unlabeled) column holds the actual "select this
                # row" icon/link — clicking the row text itself only
                # highlights it without confirming the selection.
                select_icon = row.locator("xpath=.//td[1]//a | .//td[1]//*[self::span or self::i]").first
                if await select_icon.count() > 0:
                    await select_icon.click()
                else:
                    await row.locator("xpath=.//td[1]").first.click()
                    await page.wait_for_timeout(300)
                    await row.click()
                await page.wait_for_timeout(1000)

                try:
                    await wait_processing_finished(page, timeout_ms=8000)
                except Exception:
                    pass

                resolved_desc = (await desc_field.input_value() or "").strip() if await desc_field.count() > 0 else ""
                if resolved_desc:
                    return {"alreadySelected": False, "text": resolved_desc}
                last_error = f"Linha selecionada ({row_text!r}) mas a descrição não foi preenchida"
            except Exception as exc:
                last_error = exc
            await page.wait_for_timeout(500)
        raise RuntimeError(f"Não consegui selecionar a atividade econômica {code} via diálogo de busca. Último erro: {last_error}")

    # Fallback: older <select>-based UI
    deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)
    last_result = None
    while datetime.datetime.now() < deadline:
        last_result = await page.evaluate("""
            async (code) => {
                const norm = (text) => (text || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
                const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                };
                const optionText = (select) => {
                    const option = select.options && select.selectedIndex >= 0 ? select.options[select.selectedIndex] : null;
                    return option ? (option.text || option.textContent || '') : '';
                };
                const scoreSelect = (select) => {
                    const attrs = norm(`${select.id || ''} ${select.name || ''} ${select.getAttribute('aria-label') || ''}`);
                    let score = /atividade|cnae|servico|servi[cç]o/.test(attrs) ? 20 : 0;
                    let node = select;
                    for (let depth = 0; depth < 7 && node; depth += 1, node = node.parentElement) {
                        const text = norm(node.innerText || node.textContent || '');
                        if (text.includes('atividade') && (text.includes('cadastro') || text.includes('economico'))) score += 30;
                        if (text.includes('tomador') || text.includes('retencoes') || text.includes('calculo do issqn')) score -= 20;
                    }
                    const options = Array.from(select.options || []);
                    if (options.some((opt) => (opt.value || '').includes(code) || (opt.text || opt.textContent || '').includes(code))) score += 40;
                    return score;
                };

                const selects = Array.from(document.querySelectorAll('select'))
                    .map((select) => ({select, score: scoreSelect(select)}))
                    .filter((item) => item.score > 0)
                    .sort((a, b) => b.score - a.score);
                const targetSelect = selects.length ? selects[0].select : null;
                if (!targetSelect) return {success: false, error: 'Select de atividade nao encontrado'};

                const currentText = optionText(targetSelect);
                if ((targetSelect.value || '').includes(code) || currentText.includes(code)) {
                    return {success: true, alreadySelected: true, text: currentText, value: targetSelect.value || ''};
                }

                const rawOption = Array.from(targetSelect.options || [])
                    .find((opt) => (opt.value || '').includes(code) || (opt.text || opt.textContent || '').includes(code));
                if (!rawOption) return {success: false, error: `Opcao ${code} nao encontrada no select de atividade`};

                const container = targetSelect.closest('.ui-selectonemenu');
                if (container) {
                    const labelEl = container.querySelector('.ui-selectonemenu-label') || container;
                    labelEl.click();
                    await new Promise((resolve) => setTimeout(resolve, 500));
                    const panelId = container.id ? `${container.id}_panel` : '';
                    let panel = panelId ? document.getElementById(panelId) : null;
                    if (!panel || !visible(panel)) {
                        panel = Array.from(document.querySelectorAll('.ui-selectonemenu-panel')).find(visible);
                    }
                    if (panel) {
                        const items = Array.from(panel.querySelectorAll('li.ui-selectonemenu-item'));
                        const targetItem = items.find((item) => (item.innerText || item.textContent || '').includes(code));
                        if (targetItem) {
                            targetItem.click();
                            await new Promise((resolve) => setTimeout(resolve, 500));
                            return {success: true, method: 'visual click', text: targetItem.innerText || targetItem.textContent || '', value: rawOption.value || ''};
                        }
                    }
                }

                targetSelect.value = rawOption.value;
                for (const eventName of ['input', 'change', 'blur']) {
                    targetSelect.dispatchEvent(new Event(eventName, {bubbles: true}));
                }
                if (window.jQuery) {
                    window.jQuery(targetSelect).trigger('change');
                }
                return {success: true, method: 'raw select', text: rawOption.text || rawOption.textContent || '', value: rawOption.value || ''};
            }
        """, code)
        if last_result and last_result.get("success"):
            await page.wait_for_timeout(1000)
            try:
                await wait_processing_finished(page, timeout_ms=8000)
            except Exception:
                pass
            return last_result
        await page.wait_for_timeout(500)
    raise RuntimeError(f"Não consegui selecionar a atividade econômica {code}. Último retorno: {last_result}")

async def close_optional_complement_dialog(page, timeout_ms=5000):
    """Close optional Complemento dialogs that block the form after activity/tomador AJAX."""
    deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)
    last_text = None
    while datetime.datetime.now() < deadline:
        result = await page.evaluate("""
            () => {
                const norm = (text) => (text || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toLowerCase();
                const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                };
                const dialogs = Array.from(document.querySelectorAll(
                    '.ui-dialog, .modal, [role="dialog"], .swal2-popup, .ui-confirm-dialog'
                )).filter(visible);
                const dialog = dialogs.find((el) => norm(el.innerText || el.textContent).includes('complement'));
                if (!dialog) return {closed: false, found: false};
                const text = (dialog.innerText || dialog.textContent || '').replace(/\\s+/g, ' ').trim();
                const controls = Array.from(dialog.querySelectorAll('button, a, input[type="button"], input[type="submit"], .ui-dialog-titlebar-close'))
                    .filter(visible);
                const cancel = controls.find((el) => {
                    const label = norm(`${el.innerText || el.textContent || ''} ${el.value || ''} ${el.title || ''} ${el.getAttribute('aria-label') || ''}`);
                    return label.includes('cancelar') || label.includes('fechar') || label === 'nao' || label.includes(' nao ') || label.includes('não');
                });
                const close = cancel || controls.find((el) => {
                    const label = norm(`${el.innerText || el.textContent || ''} ${el.value || ''} ${el.title || ''} ${el.getAttribute('aria-label') || ''} ${el.className || ''}`);
                    return label.includes('close') || label.includes('ui-dialog-titlebar-close');
                });
                if (!close) return {closed: false, found: true, text};
                close.click();
                return {closed: true, found: true, text};
            }
        """)
        if result and result.get("closed"):
            await page.wait_for_timeout(1000)
            try:
                await wait_processing_finished(page, timeout_ms=8000)
            except Exception:
                pass
            return result
        if result and result.get("found"):
            last_text = result.get("text")
        await page.wait_for_timeout(500)
    return {"closed": False, "found": bool(last_text), "text": last_text}

async def handle_emissor_model_choice(page, timeout_ms=8000, prefer="atual"):
    """Handle the 'Escolha o modelo de emissão' transition dialog.

    Since 2026-10-01 the Campinas portal offers two emitters side by side
    while it migrates to the national IBS/CBS standard (Ato Conjunto
    RFB/CGIBS nº 4, 30/07/2026): "Emissor Atual" (the old ABRASF layout this
    automation was originally built for - no "Documento" section, has
    "Clonar") and "Novo Emissor Ajustado ao Padrão Nacional" (the redesigned
    layout). The choice isn't sticky - the dialog can reappear on every
    emission. "Emissor Atual" stays available, per the dialog's own text:
      - until 30/09/2026 for LC 116/2003 service-list prestadores (expired),
      - until 31/10/2026 for Simples Nacional contributors,
      - until 30/11/2026 exclusively for subitens 1.03, 1.05, 1.09 e 16.01.
    `prefer="atual"` tries to keep using the proven old flow while it's
    still offered; pass "novo" to force the redesigned flow once the old
    one's exception window closes. If the preferred option isn't present
    (e.g. the portal stops offering it), falls back to whichever is shown.
    """
    deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)
    while datetime.datetime.now() < deadline:
        result = await page.evaluate(
            """(prefer) => {
                const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                };
                const candidates = Array.from(document.querySelectorAll('a,button'))
                    .filter((el) => visible(el));
                const atual = candidates.find((el) => (el.innerText || el.textContent || '').includes('Emissor Atual'));
                const novo = candidates.find((el) => (el.innerText || el.textContent || '').includes('Novo Emissor'));
                if (!atual && !novo) return {found: false};
                const chosen = prefer === 'atual' ? (atual || novo) : (novo || atual);
                const chosenLabel = chosen === atual ? 'atual' : 'novo';
                chosen.click();
                return {found: true, chosen: chosenLabel};
            }""",
            prefer,
        )
        if result and result.get("found"):
            await page.wait_for_timeout(1500)
            try:
                await wait_processing_finished(page, timeout_ms=8000)
            except Exception:
                pass
            return result
        await page.wait_for_timeout(300)
    return {"found": False}

# --- Novo Emissor Ajustado ao Padrão Nacional (2026-10 wizard) -------------
# The new emitter is a 5-step PrimeFaces wizard (Pessoas > Serviço > Valores >
# Informações Complementares > Emitir NFS-e) served from
# .../notafiscalnacional/notaFiscalNacionalData.jsf. Its component ids are
# generated, so every control below is located by its visible label instead.
NEW_EMISSOR_DEFAULTS = {
    # system_config key: default (values the user picks manually in the portal)
    "nfse_codigo_tributacao_nacional": "01.06.01",
    "nfse_codigo_complementar_municipal": "01.06.01.001",
    "nfse_item_nbs": "1.1510.00.00",
    "nfse_indicador_operacao": "100301",
    "nfse_situacao_pis_cofins": "Nenhum",
    "nfse_tipo_retencao_pis_cofins_csll": "PIS/COFINS/CSLL Não Retidos",
    "nfse_classificacao_tributaria": "000001",
}

# The final 'Emitir NFS-e' link (a disabled twin lives in the preview dialog).
NEW_EMISSOR_EMIT_SELECTOR = 'a[data-test="emitir-nfse"]:not(.ui-state-disabled)'

_WIZARD_JS_LIB = """
    const visible = (el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    };
    // Accent-insensitive: the portal itself misspells labels (e.g. "Classificaçǎo").
    const norm = (s) => (s || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
        .replace(/\\*/g, '').replace(/\\s+/g, ' ').trim().toLowerCase();
    const follows = (a, b) => Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) && !a.contains(b);
    // Innermost visible elements whose whole text is `text` (asterisks ignored).
    const findByText = (text, after) => {
        const target = norm(text);
        const hits = Array.from(document.querySelectorAll('label,span,div,td,th,b,strong,legend,h1,h2,h3,h4,h5,p'))
            .filter((el) => norm(el.textContent) === target && visible(el) && (!after || follows(after, el)));
        return hits.filter((el) => !hits.some((other) => other !== el && el.contains(other)));
    };
    const anchorFor = (args) => {
        let after = null;
        if (args.after) {
            after = findByText(args.after, null)[0] || null;
            if (!after) return {error: 'Seção não encontrada: ' + args.after};
        }
        let labels = findByText(args.label, after);
        // The mandatory asterisk lives in a sibling span inside the <label>.
        const starred = (el) => (el.closest('label') || el).textContent.includes('*');
        if (args.star === true) labels = labels.filter(starred);
        if (args.star === false) labels = labels.filter((el) => !starred(el));
        const label = labels[args.nth || 0];
        if (!label) return {error: 'Rótulo não encontrado: ' + args.label};
        return {label};
    };
    const radioLabel = (rb) => {
        const input = rb.matches('input') ? rb : rb.querySelector('input');
        let lab = input && input.id ? document.querySelector('label[for="' + CSS.escape(input.id) + '"]') : null;
        if (!lab) lab = rb.nextElementSibling;
        if (!lab && rb.closest('td')) lab = rb.closest('td').nextElementSibling;
        return norm(lab ? lab.textContent : '');
    };
"""

async def wizard_settle(page, timeout_ms=60000):
    """Wait for the wizard's PrimeFaces AJAX round-trip ('Processando...') to finish."""
    await page.wait_for_timeout(400)
    # The 'Processando...' SweetAlert stays in the DOM after closing (only its
    # --show-modal class is removed), so a plain visibility check never clears.
    await page.wait_for_function("() => !document.querySelector('.swal-overlay--show-modal')", timeout=timeout_ms)
    try:
        await page.wait_for_function(
            "() => !(window.PrimeFaces && PrimeFaces.ajax && PrimeFaces.ajax.Queue) || PrimeFaces.ajax.Queue.isEmpty()",
            timeout=timeout_ms,
        )
    except Exception:
        pass
    await page.wait_for_timeout(300)

async def wizard_messages(page):
    """Visible validation/error messages currently shown by the wizard."""
    try:
        return await page.evaluate(
            """() => {""" + _WIZARD_JS_LIB + """
                const sel = '.ui-messages-error, .ui-messages-warn, .ui-message-error, .ui-growl-item, .ui-messages-fatal';
                return Array.from(document.querySelectorAll(sel))
                    .filter(visible)
                    .map((el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim())
                    .filter((t, i, arr) => t && arr.indexOf(t) === i)
                    .slice(0, 6);
            }"""
        )
    except Exception:
        return []

async def wizard_control(page, label, kind="text", after=None, star=None, nth=0, timeout_ms=15000):
    """Locate the control that follows a visible label in the wizard.

    kind: 'text' (input/textarea), 'menu' (PrimeFaces selectOneMenu) or
    'checkbox'. `after` restricts the search to labels below a section heading;
    `star` requires (True) or forbids (False) the mandatory-field asterisk.
    Returns (locator, info) where info has value/text/disabled/checked.
    """
    token = f"nfse-{kind}-{nth}-{datetime.datetime.now().timestamp()}"
    deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)
    info = None
    while datetime.datetime.now() < deadline:
        info = await page.evaluate(
            """(args) => {""" + _WIZARD_JS_LIB + """
                const found = anchorFor(args);
                if (found.error) return found;
                const selectors = {
                    text: 'input, textarea',
                    menu: '.ui-selectonemenu',
                    checkbox: '.ui-chkbox, input[type=checkbox]',
                };
                for (const el of document.querySelectorAll(selectors[args.kind])) {
                    if (!follows(found.label, el)) continue;
                    if (args.kind === 'text') {
                        if (el.tagName === 'INPUT' && !['text', 'tel', 'email', 'number', ''].includes((el.type || '').toLowerCase())) continue;
                        if (el.closest('.ui-selectonemenu, .ui-chkbox, .ui-radiobutton, .ui-selectonemenu-panel')) continue;
                        if (!visible(el)) continue;
                    } else if (args.kind === 'checkbox') {
                        if (el.matches('input') && el.closest('.ui-chkbox')) continue;
                        if (!visible(el)) continue;
                    } else if (!visible(el)) continue;
                    el.setAttribute('data-nfse-auto', args.token);
                    const box = el.querySelector ? el.querySelector('.ui-chkbox-box') : null;
                    const labelEl = el.querySelector ? el.querySelector('.ui-selectonemenu-label') : null;
                    return {
                        id: el.id || '',
                        value: 'value' in el ? (el.value || '') : '',
                        text: labelEl ? (labelEl.textContent || '').replace(/\\s+/g, ' ').trim() : '',
                        disabled: Boolean(el.disabled || el.readOnly || el.classList.contains('ui-state-disabled') || (box && box.classList.contains('ui-state-disabled'))),
                        checked: Boolean(box ? box.classList.contains('ui-state-active') : el.checked),
                    };
                }
                return {error: 'Campo não encontrado após o rótulo: ' + args.label};
            }""",
            {"label": label, "kind": kind, "after": after, "star": star, "nth": nth, "token": token},
        )
        if info and not info.get("error"):
            return page.locator(f'[data-nfse-auto="{token}"]').first, info
        await page.wait_for_timeout(400)
    raise PlaywrightTimeoutError(f"Novo emissor: {(info or {}).get('error') or 'campo não localizado'} ({label})")

async def wizard_label_visible(page, label, timeout_ms=20000):
    deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)
    while datetime.datetime.now() < deadline:
        try:
            found = await page.evaluate(
                """(label) => {""" + _WIZARD_JS_LIB + """
                    return findByText(label, null).length > 0;
                }""",
                label,
            )
            if found:
                return True
        except Exception:
            pass
        await page.wait_for_timeout(400)
    return False

async def wizard_select_menu(page, label, option_prefix, after=None, timeout_ms=20000):
    """Pick the option starting with `option_prefix` in the selectOneMenu under `label`."""
    prefix = re.sub(r"\s+", " ", str(option_prefix)).strip().lower()
    menu, info = await wizard_control(page, label, kind="menu", after=after, timeout_ms=timeout_ms)
    if info.get("text", "").lower().startswith(prefix):
        return info["text"]
    if info.get("disabled"):
        raise RuntimeError(f"Novo emissor: o campo '{label}' está bloqueado com '{info.get('text')}' (esperado '{option_prefix}').")
    await menu.scroll_into_view_if_needed()
    await menu.click()
    if info.get("id"):
        panel = page.locator(f'[id="{info["id"]}_panel"]').first
    else:
        panel = page.locator(".ui-selectonemenu-panel:visible").last
    await panel.wait_for(state="visible", timeout=10000)
    filter_input = panel.locator("input.ui-selectonemenu-filter").first
    if await filter_input.count() and await filter_input.is_visible():
        await filter_input.fill(str(option_prefix))
        await page.wait_for_timeout(800)
    deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)
    options_seen = []
    while datetime.datetime.now() < deadline:
        items = panel.locator("li.ui-selectonemenu-item")
        options_seen = []
        for index in range(await items.count()):
            item = items.nth(index)
            text = re.sub(r"\s+", " ", (await item.inner_text()) or "").strip()
            options_seen.append(text)
            if text.lower().startswith(prefix) and await item.is_visible():
                await item.click()
                await wizard_settle(page)
                _, after_info = await wizard_control(page, label, kind="menu", after=after, timeout_ms=timeout_ms)
                if not after_info.get("text", "").lower().startswith(prefix):
                    raise RuntimeError(f"Novo emissor: '{label}' ficou com '{after_info.get('text')}' em vez de '{option_prefix}'.")
                return after_info["text"]
        await page.wait_for_timeout(500)
    await page.keyboard.press("Escape")
    raise RuntimeError(f"Novo emissor: opção '{option_prefix}' não existe em '{label}'. Opções: {options_seen[:12]}")

async def wizard_radio(page, option, after, click=True):
    """Read (and optionally select) the radio labelled `option` that follows the `after` text."""
    token = f"nfse-radio-{datetime.datetime.now().timestamp()}"
    result = await page.evaluate(
        """(args) => {""" + _WIZARD_JS_LIB + """
            const anchor = findByText(args.after, null)[0];
            if (!anchor) return {found: false, error: 'Seção não encontrada: ' + args.after};
            const radios = Array.from(document.querySelectorAll('.ui-radiobutton, input[type=radio]'))
                .filter((rb) => !(rb.matches('input') && rb.closest('.ui-radiobutton')) && follows(anchor, rb) && visible(rb));
            const rb = radios.find((item) => radioLabel(item) === norm(args.option));
            if (!rb) return {found: false, error: 'Opção não encontrada: ' + args.option};
            const box = rb.querySelector ? rb.querySelector('.ui-radiobutton-box') : null;
            const input = rb.matches('input') ? rb : rb.querySelector('input');
            const active = Boolean(box ? box.classList.contains('ui-state-active') : (input && input.checked));
            const disabled = Boolean((box && box.classList.contains('ui-state-disabled')) || (input && input.disabled));
            const wantsClick = Boolean(args.click && !active && !disabled);
            if (wantsClick) (box || rb).setAttribute('data-nfse-auto', args.token);
            return {found: true, active, disabled, clicked: wantsClick};
        }""",
        {"option": option, "after": after, "click": click, "token": token},
    )
    if result.get("clicked"):
        # A real mouse click: PrimeFaces ignores a synthetic el.click() here.
        await page.locator(f'[data-nfse-auto="{token}"]').first.click()
        await wizard_settle(page)
    return result

async def wizard_advance(page, expect_label, step_name, timeout_ms=30000):
    """Click 'Avançar' and confirm the wizard actually moved to the next step."""
    button = await first_visible_locator(
        page,
        "xpath=//*[self::button or self::a][normalize-space()='Avançar' or .//span[normalize-space()='Avançar']]",
        timeout_ms=10000,
    )
    await button.scroll_into_view_if_needed()
    await button.click()
    await wizard_settle(page)
    if expect_label.startswith("css="):
        try:
            await first_visible_locator(page, expect_label[4:], timeout_ms=timeout_ms)
            return
        except PlaywrightTimeoutError:
            pass
    elif await wizard_label_visible(page, expect_label, timeout_ms=timeout_ms):
        return
    messages = await wizard_messages(page)
    detail = "; ".join(messages) if messages else "o portal não exibiu mensagem"
    raise RuntimeError(f"Novo emissor: não avançou da etapa '{step_name}': {detail}")

async def open_new_emissor_page(page, timeout_ms=30000):
    """Open the emission entry that offers the new emitter (notaFiscalNacionalList.jsf).

    The legacy shortcut (emissaoNotaFiscalList) lands straight on the old
    form without the model-choice dialog, so it can't be used here. Returns
    the menu entries found, for diagnostics.
    """
    # The logged-in session opens the page directly by URL, which doesn't
    # depend on the sidebar being rendered. Fall back to the menu entry.
    try:
        await page.goto(NEW_EMISSOR_LIST_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        if "notafiscalnacional" in page.url.lower():
            return {"clicked": False, "entries": []}
    except Exception:
        pass
    await ensure_emission_menu_ready(page, timeout_ms=timeout_ms)
    await page.wait_for_timeout(1000)
    result = await page.evaluate(
        """() => {
            const entries = Array.from(document.querySelectorAll('a,button,li,span,div'))
                .map((el) => ({
                    el,
                    text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim(),
                    target: (el.getAttribute('onclick') || '') + ' ' + (el.getAttribute('href') || ''),
                }))
                .filter((item) => /notafiscal/i.test(item.target));
            const hit = entries.find((item) => /notaFiscalNacional/i.test(item.target));
            if (hit) hit.el.click();
            return {
                clicked: Boolean(hit),
                entries: entries.map((item) => item.text.slice(0, 40) + ' => ' + item.target.trim().slice(0, 160)).slice(0, 20),
            };
        }"""
    )
    if not result.get("clicked"):
        await page.goto(NEW_EMISSOR_LIST_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(2000)
    return result

async def is_new_emissor_wizard(page, timeout_ms=15000):
    deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)
    while datetime.datetime.now() < deadline:
        if "notafiscalnacionaldata" in page.url.lower() and await wizard_label_visible(page, "Data Competência", timeout_ms=1000):
            return True
        if await page.locator("xpath=//input[contains(@id, 'idDocumentoNumeroNotaFiscal') or @placeholder='Número da Nota']").count():
            return False
        await page.wait_for_timeout(400)
    return False

async def fill_new_emissor_wizard(page, client, desc_text, settings, log, debug_prefix=None):
    """Fill the new emitter's wizard up to the final review step ('Emitir NFS-e').

    Does NOT click the final 'Emitir NFS-e' button. `log` is an async
    callable(message, status). With `debug_prefix`, saves the DOM and a
    screenshot of each step (emissao_dom_novo_*.html / screenshots).
    """
    doc_formatted = client["cnpj_cpf"]
    doc_digits = re.sub(r"\D+", "", doc_formatted)
    invoice_value = float(client["invoice_value"])
    value_br = f"{invoice_value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

    async def snapshot(step):
        if not debug_prefix:
            return
        try:
            with open(os.path.join(BASE_DIR, f"emissao_dom_novo_{step}.html"), "w", encoding="utf-8") as f:
                f.write(await page.content())
            await page.screenshot(path=f"{debug_prefix}_{step}.png", full_page=True)
        except Exception:
            pass

    try:
        # ---- Etapa 1: Pessoas -------------------------------------------
        # Data Competência is left as the portal's default (today), as the
        # month being billed goes in the service description.
        await log("Novo emissor - etapa Pessoas: localizando o tomador...", "running")
        nacional = await wizard_radio(page, "Nacional", after="TOMADOR SERVIÇO")
        if not nacional.get("found"):
            raise RuntimeError(f"Novo emissor: {nacional.get('error')}")
        search_field, _ = await wizard_control(page, "CPF/CNPJ", after="TOMADOR SERVIÇO", star=False)
        await search_field.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await search_field.press_sequentially(doc_digits, delay=60)
        if re.sub(r"\D+", "", await search_field.input_value()) != doc_digits:
            # The input mask can truncate typed digits - set the formatted value directly.
            await search_field.evaluate(
                """(el, val) => {
                    el.value = val;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                doc_formatted,
            )
        await page.wait_for_timeout(300)
        clicked_lookup = await search_field.evaluate(
            """(el) => {
                const visible = (n) => { const r = n.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
                const nodes = Array.from(document.querySelectorAll('a, button, input[type=image], img'));
                const next = nodes.find((n) => (el.compareDocumentPosition(n) & Node.DOCUMENT_POSITION_FOLLOWING) && visible(n));
                if (!next) return false;
                (next.closest('a, button') || next).click();
                return true;
            }"""
        )
        if not clicked_lookup:
            raise RuntimeError("Novo emissor: não encontrei a lupa de pesquisa do tomador.")
        await wizard_settle(page)

        tomador_doc = ""
        tomador_name = ""
        for _ in range(20):
            _, doc_info = await wizard_control(page, "CPF/CNPJ", after="TOMADOR SERVIÇO", star=True)
            _, name_info = await wizard_control(page, "Nome/Nome Empresarial", after="TOMADOR SERVIÇO", star=True)
            tomador_doc = re.sub(r"\D+", "", doc_info.get("value", ""))
            tomador_name = name_info.get("value", "").strip()
            if tomador_doc == doc_digits and tomador_name:
                break
            await page.wait_for_timeout(500)
        if tomador_doc != doc_digits or not tomador_name:
            messages = await wizard_messages(page)
            raise RuntimeError(
                f"Novo emissor: a pesquisa do tomador {doc_formatted} não preencheu os dados cadastrais "
                f"(CPF/CNPJ='{tomador_doc}', nome='{tomador_name}'). {'; '.join(messages)}"
            )
        await log(f"Tomador carregado pelo portal: {tomador_name}", "success")

        compra_gov = await wizard_radio(page, "Não", after="A operação se trata de uma compra governamental?")
        if not compra_gov.get("found"):
            raise RuntimeError(f"Novo emissor: {compra_gov.get('error')}")
        checkbox, check_info = await wizard_control(page, "Próprio Tomador Serviço", kind="checkbox", after="DESTINATÁRIO")
        if not check_info.get("checked"):
            box = checkbox.locator(".ui-chkbox-box")
            await (box.first if await box.count() else checkbox).click()
            await wizard_settle(page)
        await snapshot("1_pessoas")
        await wizard_advance(page, "Código Tributação Nacional", "Pessoas")

        # ---- Etapa 2: Serviço -------------------------------------------
        await log("Novo emissor - etapa Serviço: códigos de tributação, descrição e NBS...", "running")
        chosen = await wizard_select_menu(page, "Código Tributação Nacional", settings["nfse_codigo_tributacao_nacional"])
        await log(f"Código Tributação Nacional: {chosen}", "info")
        chosen = await wizard_select_menu(page, "Código Complementar Municipal", settings["nfse_codigo_complementar_municipal"])
        await log(f"Código Complementar Municipal: {chosen}", "info")
        desc_field, _ = await wizard_control(page, "Descrição Serviço")
        await desc_field.click()
        await desc_field.fill(desc_text)
        await desc_field.press("Tab")
        await wizard_settle(page)
        chosen = await wizard_select_menu(page, "Item NBS Correspondente ao Serviço Prestado", settings["nfse_item_nbs"])
        await log(f"Item NBS: {chosen}", "info")
        chosen = await wizard_select_menu(page, "Indicador da Operação", settings["nfse_indicador_operacao"])
        await log(f"Indicador da Operação: {chosen}", "info")
        desc_field, desc_info = await wizard_control(page, "Descrição Serviço")
        if desc_info.get("value", "").strip() != desc_text.strip():
            # An AJAX re-render can wipe the textarea - type it again.
            await desc_field.fill(desc_text)
            await desc_field.press("Tab")
            await wizard_settle(page)
        await snapshot("2_servico")
        await wizard_advance(page, "Valor Serviço Prestado", "Serviço")

        # ---- Etapa 3: Valores -------------------------------------------
        await log(f"Novo emissor - etapa Valores: R$ {value_br}...", "running")
        applied_value = None
        for typed in (value_br, re.sub(r"\D+", "", value_br)):
            value_field, _ = await wizard_control(page, "Valor Serviço Prestado")
            await value_field.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await value_field.press_sequentially(typed, delay=60)
            await value_field.press("Tab")
            await wizard_settle(page)
            _, value_info = await wizard_control(page, "Valor Serviço Prestado")
            applied_value = parse_brl_money(value_info.get("value"))
            if applied_value is not None and abs(applied_value - invoice_value) < 0.005:
                break
        if applied_value is None or abs(applied_value - invoice_value) >= 0.005:
            raise RuntimeError(f"Novo emissor: Valor Serviço Prestado ficou '{applied_value}' em vez de {value_br}.")

        wants_retention = "retido" in (client.get("retention_type") or "").lower()
        wanted_option = "Sim" if wants_retention else "Não"
        iss = await wizard_radio(page, wanted_option, after="Há Retenção do ISSQN pelo Tomador ou pelo Intermediário")
        if not iss.get("found"):
            # The portal hides the question when retention doesn't apply
            # (e.g. tomador pessoa física / fora do município).
            await log(
                "O portal não oferece a opção de retenção do ISSQN para este tomador.",
                "warning" if wants_retention else "info",
            )
        elif not iss.get("active") and not iss.get("clicked"):
            await log(
                f"O portal fixou a retenção do ISSQN diferente do cadastro do cliente (cadastro: {wanted_option}). Mantendo a regra do portal.",
                "warning",
            )
        else:
            await log(f"Retenção do ISSQN pelo tomador: {wanted_option}", "info")

        await wizard_select_menu(page, "Situação Tributária PIS/COFINS", settings["nfse_situacao_pis_cofins"])
        await wizard_select_menu(page, "Tipo de Retenção do PIS/COFINS/CSLL", settings["nfse_tipo_retencao_pis_cofins_csll"])
        chosen = await wizard_select_menu(page, "Classificação Tributária", settings["nfse_classificacao_tributaria"])
        await log(f"Classificação Tributária IBS/CBS: {chosen}", "info")
        _, value_info = await wizard_control(page, "Valor Serviço Prestado")
        if abs((parse_brl_money(value_info.get("value")) or 0) - invoice_value) >= 0.005:
            raise RuntimeError(f"Novo emissor: o valor do serviço mudou para '{value_info.get('value')}' após as seleções de tributação.")
        await snapshot("3_valores")
        await wizard_advance(page, "Número Documento Responsabilidade Técnica", "Valores")

        # ---- Etapa 4: Informações Complementares (nada a preencher) ------
        await snapshot("4_info_complementares")
        await wizard_advance(page, "css=" + NEW_EMISSOR_EMIT_SELECTOR, "Informações Complementares")

        # ---- Etapa 5: revisão da DPS -------------------------------------
        review = await page.evaluate("() => Array.from(document.querySelectorAll('input, textarea')).map((el) => el.value || '').join('\\n')")
        review = review.replace(" ", " ")
        if doc_digits not in re.sub(r"[.\-/]", "", review):
            raise RuntimeError("Novo emissor: a revisão final não mostra o CPF/CNPJ do tomador esperado.")
        if desc_text.strip() not in review:
            raise RuntimeError("Novo emissor: a revisão final não mostra a descrição do serviço esperada.")
        if f"R$ {value_br}" not in review:
            raise RuntimeError(f"Novo emissor: a revisão final não mostra o valor R$ {value_br}.")
        await snapshot("5_revisao")
        await log("Novo emissor: revisão final conferida (tomador, descrição e valor).", "success")
    except Exception:
        await snapshot("erro")
        raise

async def click_new_emissor_emit_button(page):
    """Click the final 'Emitir NFS-e' button of the wizard (not the step header of the same name)."""
    button = await first_visible_locator(page, NEW_EMISSOR_EMIT_SELECTOR, timeout_ms=10000)
    await button.scroll_into_view_if_needed()
    await button.click()
    await page.wait_for_timeout(1000)
    # PrimeFaces may ask for a confirmation before issuing.
    confirm = page.locator(
        "xpath=//div[contains(@class,'ui-confirm-dialog') or contains(@class,'ui-dialog')][not(contains(@style,'display: none'))]"
        "//*[self::button or self::a][normalize-space()='Sim' or normalize-space()='Confirmar' or .//span[normalize-space()='Sim' or normalize-space()='Confirmar']]"
    )
    for index in range(await confirm.count()):
        item = confirm.nth(index)
        if await item.is_visible():
            await item.click()
            break

async def find_description_field(page, timeout_ms=30000):
    deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)
    last_error = None
    while datetime.datetime.now() < deadline:
        for selector in DESCRIPTION_SELECTORS:
            try:
                return await first_visible_locator(page, selector, timeout_ms=800, require_enabled=True)
            except Exception as exc:
                last_error = exc
        await page.wait_for_timeout(300)
    raise PlaywrightTimeoutError(f"Campo de descrição não encontrado ou não habilitado. Último erro: {last_error}")

async def fill_description(page, value, timeout_ms=30000):
    field = await find_description_field(page, timeout_ms=timeout_ms)
    await field.click()
    await field.fill(str(value))
    return field

async def fill_competence_field(page, comp_info, timeout_ms=30000):
    """Set the invoice competence field in the Campinas portal form."""
    month_year = comp_info["month_year_short"]
    start_date = comp_info["start_date"]
    expected_digits = re.sub(r"\D+", "", month_year)
    fallback_digits = re.sub(r"\D+", "", start_date)
    deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)
    last_error = None

    async def apply_value(locator, value):
        tag_name = (await locator.evaluate("(el) => el.tagName")).lower()
        input_type = ""
        try:
            input_type = (await locator.get_attribute("type") or "").lower()
        except Exception:
            input_type = ""
        if tag_name == "select":
            try:
                await locator.select_option(label=value)
            except Exception:
                await locator.select_option(value=value)
        else:
            await locator.click(force=True)
            try:
                await locator.fill(value)
            except Exception:
                await locator.evaluate(
                    """(el, val) => {
                        el.value = val;
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        el.dispatchEvent(new Event('blur', {bubbles: true}));
                    }""",
                    value,
                )
            await locator.press("Tab")
        await page.wait_for_timeout(500)

    async def value_matches(locator):
        try:
            value = await locator.input_value()
        except Exception:
            value = await locator.evaluate("(el) => el.value || el.textContent || ''")
        digits = re.sub(r"\D+", "", value or "")
        return expected_digits in digits or fallback_digits in digits

    def is_safe_competence_control(meta):
        text = " ".join(
            str(meta.get(key) or "")
            for key in ("id", "name", "placeholder", "ariaLabel", "labelText", "text")
        ).lower()
        # Strip the form's own name prefix before excluding - every field's id
        # starts with "formnotafiscal", which otherwise false-matches "nota".
        exclude_text = text.replace("formnotafiscal", "")
        if not re.search(r"\bcompet(e|ê)ncia\b|\bperiodo\b|\bper[ií]odo\b", text):
            return False
        if re.search(r"\bpesquis|buscar|\bnota\b|numero|n[uú]mero|emiss|valor|descricao|descri[cç]ao|retenc", exclude_text):
            return False
        return True

    try:
        candidate = await page.evaluate("""
            () => {
                const visible = (el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                };
                const form = document.querySelector('form#formNotaFiscal');
                if (!form) return null;
                const controls = Array.from(form.querySelectorAll('input,select,textarea'))
                    .filter((el) => visible(el) && !el.disabled);
                for (const el of controls) {
                    const label = el.labels && el.labels.length ? (el.labels[0].innerText || el.labels[0].textContent || '') : '';
                    const wrapperLabel = el.closest('label') ? (el.closest('label').innerText || el.closest('label').textContent || '') : '';
                    const attrs = `${el.id || ''} ${el.name || ''} ${el.getAttribute('placeholder') || ''} ${el.getAttribute('aria-label') || ''} ${label} ${wrapperLabel}`.toLowerCase();
                    const excludeAttrs = attrs.replace(/formnotafiscal/g, '');
                    if (/compet[eê]ncia|competencia|per[ií]odo|periodo/.test(attrs) && !/pesquis|buscar|\\bnota\\b|numero|n[uú]mero|emiss|valor|descricao|descri[cç]ao|retenc/.test(excludeAttrs)) {
                        return {
                            id: el.id || '',
                            name: el.name || '',
                            tag: el.tagName,
                            type: el.getAttribute('type') || '',
                            inputMode: el.getAttribute('inputmode') || '',
                            labelText: label.replace(/\\s+/g, ' ').trim(),
                            wrapperText: wrapperLabel.replace(/\\s+/g, ' ').trim(),
                            placeholder: el.getAttribute('placeholder') || '',
                            ariaLabel: el.getAttribute('aria-label') || '',
                            value: el.value || '',
                        };
                    }
                }
                return null;
            }
        """)
    except Exception as exc:
        candidate = None
        last_error = exc

    if not candidate:
        return None, None

    try:
        if not is_safe_competence_control(candidate):
            return None, None
        # Note: don't exclude on inputMode "numeric" - the real Competência
        # calendar field (a masked MM/YYYY text input) legitimately uses
        # inputmode="numeric" for the mobile keypad hint.
        if str(candidate.get("type") or "").lower() == "number":
            return None, None
        if candidate.get("id"):
            locator = page.locator(f"xpath=//form[@id='formNotaFiscal']//*[@id='{candidate['id']}']").first
        elif candidate.get("name"):
            locator = page.locator(f"xpath=//form[@id='formNotaFiscal']//*[@name='{candidate['name']}']").first
        else:
            locator = None
        if locator and await locator.count() and await locator.is_visible() and await locator.is_enabled():
            await locator.scroll_into_view_if_needed()
            try:
                current_type = (await locator.get_attribute("type") or "").lower()
            except Exception:
                current_type = ""
            if current_type == "number":
                return None, None
            await apply_value(locator, month_year)
            if await value_matches(locator):
                return locator, month_year
            await apply_value(locator, start_date)
            if await value_matches(locator):
                return locator, start_date
            return None, None
    except Exception as exc:
        last_error = exc

    diagnostic = await page.evaluate("""
        () => Array.from(document.querySelectorAll('form#formNotaFiscal label, form#formNotaFiscal span, form#formNotaFiscal div, form#formNotaFiscal input, form#formNotaFiscal select'))
            .map((el) => ({
                tag: el.tagName,
                text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 120),
                id: el.id || '',
                name: el.getAttribute('name') || '',
                placeholder: el.getAttribute('placeholder') || '',
                value: el.value || ''
            }))
            .filter((item) => /compet/i.test(`${item.text} ${item.id} ${item.name} ${item.placeholder}`))
            .slice(0, 20)
    """)
    raise PlaywrightTimeoutError(
        f"Campo de competência não encontrado ou não aceitou {month_year}. "
        f"Diagnóstico: {diagnostic}. Último erro: {last_error}"
    )

async def any_visible(page, selector):
    locator = page.locator(selector)
    count = await locator.count()
    for index in range(count):
        try:
            if await locator.nth(index).is_visible():
                return True
        except Exception:
            continue
    return False

async def wait_processing_finished(page, timeout_ms=60000):
    processing_selectors = [
        "text=Processando",
        "text=Aguarde Término do Processamento",
        "xpath=//*[contains(@class,'ui-dialog') and contains(.,'Processando')]",
        "xpath=//*[contains(@class,'modal') and contains(.,'Processando')]",
    ]
    deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)
    while datetime.datetime.now() < deadline:
        visible = False
        for selector in processing_selectors:
            if await any_visible(page, selector):
                visible = True
                break
        if not visible:
            return
        await page.wait_for_timeout(500)
    raise PlaywrightTimeoutError("Portal permaneceu em 'Processando' após o tempo limite")

def extract_invoice_number_from_text(text):
    if not text:
        return None
    patterns = [
        # The new (2026-10) layout's numbering restarts from 1 (e.g. "1 / E"),
        # unlike the old scheme's always-4-digit numbers - require only 1+ digit.
        r"\b(\d{1,})\s*/\s*[a-zA-Z]",
        r"N[uú]mero\s*/\s*S[ée]rie\s*([\d.]{1,})\s*/",
        r"N\s*[º°o]?\s*da\s*Nota\s*[:\-]?\s*([\d.]{3,})",
        r"N[uú]mero\s*da\s*Nota\s*[:\-]?\s*([\d.]{3,})",
        r"NFS-?e\s*n\s*[º°o]?\s*[:\-]?\s*([\d.]{3,})",
        r"Nota\s+Fiscal\s+(?:de\s+Servi[cç]o\s+)?(?:Eletr[oô]nica\s+)?(?:n[º°o]?\s*)?([\d.]{3,})",
        r"Nota\s+emitida\s+com\s+sucesso.*?\b([\d.]{3,})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return re.sub(r"\D+", "", match.group(1))
    return None

def is_pdf_file(path):
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except Exception:
        return False

async def capture_emitted_invoice_number(page, timeout_ms=30000, client_name=None, client_doc=None, expected_value=None):
    message_selectors = [
        "xpath=//*[contains(@class,'alert') or contains(@class,'growl') or contains(@class,'ui-messages') or contains(@class,'ui-message') or contains(@class,'messages') or contains(@class,'toast')][(contains(.,'emitida') or contains(.,'Emitida') or contains(.,'sucesso') or contains(.,'Sucesso')) and (contains(.,'Nota') or contains(.,'NFS'))]",
        "xpath=//*[self::div or self::span or self::p][(contains(.,'Nota emitida') or contains(.,'nota emitida') or contains(.,'NFS-e emitida') or contains(.,'emitida com sucesso'))]",
        "xpath=//*[contains(normalize-space(), 'Última Nota Emitida') or contains(normalize-space(), 'Ultima Nota Emitida')]",
    ]
    client_name_norm = unicodedata.normalize("NFKD", client_name or "").encode("ASCII", "ignore").decode("ASCII").lower().strip()
    client_doc_digits = re.sub(r"\D+", "", client_doc or "")
    expected_value_norm = re.sub(r"\s+", "", f"{expected_value:.2f}".replace(".", "").replace(",", ".") if isinstance(expected_value, (int, float)) else "")
    deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)
    while datetime.datetime.now() < deadline:
        for selector in message_selectors:
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(count):
                item = locator.nth(index)
                try:
                    if not await item.is_visible():
                        continue
                    text = (await item.inner_text()).strip()
                    lowered = text.lower()
                    if "última nota emitida" in lowered or "ultima nota emitida" in lowered or "clonar esta" in lowered or "tomador" in lowered:
                        # Only accept this panel if it clearly matches the current client.
                        if client_name_norm or client_doc_digits or expected_value_norm:
                            text_norm = unicodedata.normalize("NFKD", text).encode("ASCII", "ignore").decode("ASCII").lower()
                            text_digits = re.sub(r"\D+", "", text)
                            if client_name_norm and client_name_norm not in text_norm and client_doc_digits not in text_digits and expected_value_norm not in re.sub(r"\s+", "", text_norm):
                                continue
                        else:
                            continue
                    number = extract_invoice_number_from_text(text)
                    if number:
                        return number, text
                except Exception:
                    continue
        await page.wait_for_timeout(500)
    return None, None

async def click_emit_invoice_button(page):
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(500)
    selectors = [
        "xpath=//form//*[self::button or self::a][not(.//span[contains(@class,'nav-label')]) and (normalize-space()='Emitir Nota Fiscal' or .//span[normalize-space()='Emitir Nota Fiscal'])]",
        "xpath=//*[self::button or self::a][not(ancestor::*[contains(@class,'sidebar')]) and not(ancestor::*[contains(@class,'nav')]) and not(.//span[contains(@class,'nav-label')]) and (normalize-space()='Emitir Nota Fiscal' or .//span[normalize-space()='Emitir Nota Fiscal'])]",
    ]
    last_error = None
    for selector in selectors:
        try:
            locator = await first_visible_locator(page, selector, timeout_ms=5000)
            try:
                await locator.click(force=True)
            except Exception:
                await locator.evaluate("(el) => el.click()")
            return
        except Exception as exc:
            last_error = exc
    raise PlaywrightTimeoutError(f"Não encontrei o botão final Emitir Nota Fiscal no formulário. Último erro: {last_error}")

async def ensure_emission_menu_ready(page, timeout_ms=30000):
    deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)

    async def portal_ready():
        try:
            direct_links = page.locator(
                "xpath=//*[contains(@onclick, 'emissaoNotaFiscalList') or contains(@href, 'emissaoNotaFiscalList') or contains(normalize-space(), 'Emitir Nota Fiscal')]"
            )
            if await direct_links.count():
                for index in range(await direct_links.count()):
                    item = direct_links.nth(index)
                    try:
                        if await item.is_visible():
                            return True
                    except Exception:
                        continue
        except Exception:
            pass
        try:
            await page.wait_for_selector("span.nav-label", state="attached", timeout=1000)
            return True
        except Exception:
            return False

    while datetime.datetime.now() < deadline:
        if await portal_ready():
            return
        try:
            await page.goto(PRINCIPAL_URL, wait_until="domcontentloaded")
        except Exception:
            pass
        await page.wait_for_timeout(1500)

    raise PlaywrightTimeoutError("Portal principal não carregou os atalhos de navegação após o login")

async def open_emission_page(page, timeout_ms=30000):
    await ensure_emission_menu_ready(page, timeout_ms=timeout_ms)
    await page.wait_for_timeout(1000)

    async def click_visible_or_first(match_js):
        return await page.evaluate(
            """(matchJs) => {
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                };
                const nodes = Array.from(document.querySelectorAll('a,button,span,div,li'));
                const matched = nodes.filter((el) => {
                    const txt = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    const onclick = el.getAttribute('onclick') || '';
                    const href = el.getAttribute('href') || '';
                    return eval(matchJs);
                });
                const visibleNode = matched.find(visible);
                const node = visibleNode || matched[0] || null;
                if (!node) return false;
                node.click();
                return true;
            }""",
            match_js,
        )

    # First try to expand the parent menu if it is collapsed.
    parent_patterns = [
        "(/NFSe Prestador/i.test(txt) || /NFSe Prestador/i.test(onclick) || /NFSe Prestador/i.test(href))",
        "(/NFSe/i.test(txt) && /Prestador/i.test(txt))",
    ]
    for pattern in parent_patterns:
        try:
            clicked = await click_visible_or_first(pattern)
            if clicked:
                await page.wait_for_timeout(1500)
        except Exception:
            pass

    emitir_patterns = [
        "(/emissaoNotaFiscalList/i.test(onclick) || /emissaoNotaFiscalList/i.test(href))",
        "(/Emitir Nota Fiscal/i.test(txt) || /Emitir Nota Fiscal/i.test(onclick) || /Emitir Nota Fiscal/i.test(href))",
    ]
    for pattern in emitir_patterns:
        try:
            clicked = await click_visible_or_first(pattern)
            if clicked:
                await page.wait_for_timeout(2000)
                return
        except Exception:
            continue

    # Last resort: locate by visible text or onclick using Playwright locators.
    fallback_selectors = [
        "xpath=//*[contains(@onclick, 'emissaoNotaFiscalList') or contains(@href, 'emissaoNotaFiscalList')]",
        "xpath=//*[normalize-space()='Emitir Nota Fiscal' or .//span[normalize-space()='Emitir Nota Fiscal']]",
    ]
    last_error = None
    for selector in fallback_selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(count):
                item = locator.nth(index)
                try:
                    if await item.is_visible():
                        try:
                            await item.click(force=True)
                        except Exception:
                            await item.evaluate("(el) => el.click()")
                        await page.wait_for_timeout(2000)
                        return
                except Exception:
                    continue
        except Exception as exc:
            last_error = exc
    raise PlaywrightTimeoutError(f"Não consegui abrir a tela de emissão. Último erro: {last_error}")

async def open_manage_nfse(page):
    manage_number_field = "xpath=//label[contains(.,'Número') or contains(.,'Nº') or contains(.,'Nota')]/following::input[1]"

    async def has_manage_search_screen(timeout_ms=2500):
        try:
            await first_visible_locator(
                page,
                manage_number_field,
                timeout_ms=timeout_ms,
                require_enabled=True,
            )
            return True
        except Exception:
            pass
        try:
            relation_title = page.locator("text=Relação de Notas Fiscais").first
            number_block = page.locator("text=Número da Nota").first
            deadline = datetime.datetime.now() + datetime.timedelta(milliseconds=timeout_ms)
            while datetime.datetime.now() < deadline:
                if await relation_title.count() and await relation_title.is_visible() and await number_block.count() and await number_block.is_visible():
                    return True
                await page.wait_for_timeout(250)
        except Exception:
            pass
        return False

    async def menu_debug():
        try:
            return await page.evaluate("""
                () => Array.from(document.querySelectorAll('a,button'))
                    .map((el) => ({
                        text: (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim(),
                        href: el.getAttribute('href') || '',
                        onclick: el.getAttribute('onclick') || '',
                        id: el.id || '',
                        cls: el.className || ''
                    }))
                    .filter((item) => /gerenciar|nfse|nfs|nota|consultar|pesquisar/i.test(
                        `${item.text} ${item.href} ${item.onclick} ${item.id} ${item.cls}`
                    ))
                    .slice(0, 25)
            """)
        except Exception:
            return []

    if await has_manage_search_screen(timeout_ms=1000):
        return

    menu_candidates = [
        "xpath=//span[@class='nav-label' and contains(normalize-space(), 'Gerenciar NFSE')]/ancestor::a[1]",
        "xpath=//span[contains(normalize-space(), 'Gerenciar NFSE')]/ancestor::a[1]",
        "xpath=//*[self::a or self::button][contains(normalize-space(), 'Gerenciar NFSE')]",
        "xpath=//*[self::a or self::button][contains(normalize-space(), 'Gerenciar NFS')]",
    ]
    last_error = None
    for selector in menu_candidates:
        try:
            item = await first_visible_locator(page, selector, timeout_ms=3000)
            await item.click(force=True)
            await page.wait_for_timeout(1500)
            if await has_manage_search_screen():
                return
            # In this portal, parent menu labels sometimes only expand when the
            # small chevron at the right edge is clicked.
            box = await item.bounding_box()
            if box:
                await page.mouse.click(box["x"] + box["width"] - 12, box["y"] + box["height"] / 2)
                await page.wait_for_timeout(1500)
                if await has_manage_search_screen():
                    return
            break
        except Exception as exc:
            last_error = exc
    else:
        raise PlaywrightTimeoutError(f"Não consegui abrir Gerenciar NFSE. Último erro: {last_error}")

    if await has_manage_search_screen():
        return

    # Some portals expose Gerenciar NFSE as a collapsible parent. Try visible
    # children before falling back to direct JSF menu routes.
    for selector in menu_candidates:
        locator = page.locator(selector)
        count = await locator.count()
        for index in range(1, count):
            item = locator.nth(index)
            try:
                if await item.is_visible():
                    await item.click(force=True)
                    await page.wait_for_timeout(1500)
                    if await has_manage_search_screen():
                        return
            except Exception:
                continue

    child_candidates = [
        "xpath=//*[self::a or self::button][normalize-space()='Consulta Nota Fiscal' or .//*[normalize-space()='Consulta Nota Fiscal']]",
        "xpath=//*[self::a or self::button][contains(normalize-space(), 'Consulta Nota Fiscal')]",
        "xpath=//*[self::a or self::button][not(ancestor::*[contains(@style,'display: none')])][contains(normalize-space(), 'Consultar')]",
        "xpath=//*[self::a or self::button][not(ancestor::*[contains(@style,'display: none')])][contains(normalize-space(), 'Consulta')]",
        "xpath=//*[self::a or self::button][not(ancestor::*[contains(@style,'display: none')])][contains(normalize-space(), 'Pesquisar')]",
        "xpath=//*[self::a or self::button][not(ancestor::*[contains(@style,'display: none')])][contains(normalize-space(), 'Nota') and not(contains(normalize-space(), 'Emitir Nota Fiscal'))]",
        "xpath=//*[self::a or self::button][not(ancestor::*[contains(@style,'display: none')])][contains(normalize-space(), 'NFS') and not(contains(normalize-space(), 'Emitir Nota Fiscal'))]",
    ]
    for selector in child_candidates:
        locator = page.locator(selector)
        count = await locator.count()
        for index in range(count):
            item = locator.nth(index)
            try:
                if await item.is_visible():
                    await item.click(force=True)
                    await page.wait_for_timeout(2000)
                    if await has_manage_search_screen():
                        return
            except Exception:
                continue

    debug_items = await menu_debug()
    try:
        debug_path = os.path.join(SCREENSHOTS_DIR, "last_manage_nfse_menu_debug.txt")
        with open(debug_path, "w", encoding="utf-8") as f:
            for item in debug_items:
                f.write(f"{item}\n")
    except Exception:
        pass
    raise PlaywrightTimeoutError(
        "Não consegui abrir a tela de pesquisa do Gerenciar NFSE. "
        f"Itens de menu encontrados para diagnóstico: {debug_items}"
    )

async def search_nfse_by_number(page, invoice_number, emission_date=None):
    if emission_date:
        date_br = emission_date.strftime("%d/%m/%Y")
        try:
            await page.evaluate(
                """(dateValue) => {
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                    };
                    const headers = Array.from(document.querySelectorAll('*'))
                        .filter((el) => visible(el) && (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim() === 'Emissão');
                    for (const header of headers) {
                        let root = header;
                        for (let depth = 0; depth < 8 && root; depth += 1, root = root.parentElement) {
                            const inputs = Array.from(root.querySelectorAll('input'))
                                .filter((input) => visible(input) && !input.disabled && input.type !== 'hidden');
                            if (inputs.length >= 2) {
                                inputs.slice(0, 2).forEach((input) => {
                                    input.focus();
                                    input.value = dateValue;
                                    input.dispatchEvent(new Event('input', {bubbles: true}));
                                    input.dispatchEvent(new Event('change', {bubbles: true}));
                                    input.blur();
                                });
                                return true;
                            }
                        }
                    }
                    return false;
                }""",
                date_br,
            )
            await page.wait_for_timeout(500)
        except Exception:
            pass

    filled_number = False
    try:
        number_block = page.locator("text=Número da Nota").first
        if await number_block.count() and await number_block.is_visible():
            filled_count = await page.evaluate(
                """(invoiceNumber) => {
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                    };
                    const headers = Array.from(document.querySelectorAll('*'))
                        .filter((el) => visible(el) && (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim() === 'Número da Nota');
                    for (const header of headers) {
                        let root = header;
                        for (let depth = 0; depth < 8 && root; depth += 1, root = root.parentElement) {
                            const inputs = Array.from(root.querySelectorAll('input'))
                                .filter((input) => visible(input) && !input.disabled && input.type !== 'hidden');
                            if (inputs.length >= 2) {
                                inputs.slice(0, 2).forEach((input) => {
                                    input.focus();
                                    input.value = invoiceNumber;
                                    input.dispatchEvent(new Event('input', {bubbles: true}));
                                    input.dispatchEvent(new Event('change', {bubbles: true}));
                                    input.blur();
                                });
                                return inputs.length;
                            }
                        }
                    }
                    return 0;
                }""",
                str(invoice_number),
            )
            if filled_count >= 2:
                filled_number = True
                await page.wait_for_timeout(500)
    except Exception:
        pass

    number_inputs = [
        "xpath=//label[contains(.,'Número') or contains(.,'Nº') or contains(.,'Nota')]/following::input[1]",
        "xpath=//input[contains(@placeholder,'Número') or contains(@placeholder,'Nota') or contains(@id,'numero') or contains(@name,'numero') or contains(@id,'Numero') or contains(@name,'Numero')]",
    ]
    for selector in number_inputs:
        try:
            field = await fill_first_visible(page, selector, invoice_number, timeout_ms=5000)
            await field.press("Tab")
            filled_number = True
            break
        except Exception:
            continue

    if not filled_number:
        try:
            filled_count = await page.evaluate(
                """(invoiceNumber) => {
                    const visible = (el) => {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                    };
                    const headers = Array.from(document.querySelectorAll('*'))
                        .filter((el) => visible(el) && (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim() === 'Número da Nota');
                    for (const header of headers) {
                        let root = header;
                        for (let depth = 0; depth < 8 && root; depth += 1, root = root.parentElement) {
                            const inputs = Array.from(root.querySelectorAll('input'))
                                .filter((input) => visible(input) && !input.disabled && input.type !== 'hidden');
                            if (inputs.length >= 2) {
                                inputs.slice(0, 2).forEach((input) => {
                                    input.focus();
                                    input.value = invoiceNumber;
                                    input.dispatchEvent(new Event('input', {bubbles: true}));
                                    input.dispatchEvent(new Event('change', {bubbles: true}));
                                    input.blur();
                                });
                                return inputs.length;
                            }
                        }
                    }
                    return 0;
                }""",
                str(invoice_number),
            )
            if filled_count >= 2:
                filled_number = True
                await page.wait_for_timeout(500)
        except Exception:
            pass

    if not filled_number:
        raise PlaywrightTimeoutError("Não encontrei o campo de número da nota em Gerenciar NFSE")

    search_buttons = [
        "xpath=//*[self::button or self::a][normalize-space()='Pesquisar' or .//span[normalize-space()='Pesquisar']]",
        "xpath=//*[self::button or self::a][contains(normalize-space(), 'Consultar') or contains(normalize-space(), 'Buscar')]",
        "xpath=//*[self::button or self::a][contains(normalize-space(), 'Gerar Relação Notas') or contains(normalize-space(), 'Gerar Relação')]",
    ]
    for selector in search_buttons:
        try:
            await click_first_visible(page, selector, timeout_ms=5000)
            break
        except Exception:
            continue
    else:
        raise PlaywrightTimeoutError("Não encontrei o botão de pesquisa em Gerenciar NFSE")

    download_confirm_selector = "xpath=//*[contains(normalize-space(), 'Deseja Realmente Confirmar')]/following::*[self::button or self::a][contains(normalize-space(), 'Download')]"
    deadline = datetime.datetime.now() + datetime.timedelta(seconds=15)
    while datetime.datetime.now() < deadline:
        try:
            download_confirm = page.locator(download_confirm_selector).first
            if await download_confirm.count() and await download_confirm.is_visible():
                return "download_confirmation"
        except Exception:
            pass
        await page.wait_for_timeout(500)

    await wait_processing_finished(page, timeout_ms=60000)
    await page.wait_for_timeout(2000)
    return "search_results"

async def open_nfse_search_result(page, invoice_number, client_name, client_doc):
    rows = page.locator(f"xpath=//tr[contains(normalize-space(.), '{invoice_number}')]")
    deadline = datetime.datetime.now() + datetime.timedelta(seconds=30)
    while datetime.datetime.now() < deadline and await rows.count() == 0:
        await page.wait_for_timeout(500)

    if await rows.count() == 0:
        body_text = await page.locator("body").inner_text()
        if invoice_number not in body_text:
            raise PlaywrightTimeoutError(f"Gerenciar NFSE não retornou a nota {invoice_number}")
        return

    row = rows.first
    row_text = await row.inner_text()
    client_doc_digits = re.sub(r"\D+", "", client_doc or "")
    row_digits = re.sub(r"\D+", "", row_text)
    if client_doc_digits and client_doc_digits not in row_digits and client_name.lower() not in row_text.lower():
        raise RuntimeError(f"Nota {invoice_number} encontrada, mas a linha não confere com o cliente esperado. Linha: {row_text}")

    action_selectors = [
        ".//*[self::a or self::button][contains(.,'Visualizar') or contains(.,'Abrir') or contains(.,'Detalhar') or contains(.,'Imprimir') or contains(.,'PDF')]",
        ".//*[self::a or self::button or self::span or self::i][contains(@class,'print') or contains(@class,'pdf') or contains(@class,'search') or contains(@class,'eye')]",
    ]
    for selector in action_selectors:
        try:
            action = row.locator(f"xpath={selector}").first
            if await action.count() and await action.is_visible():
                await action.click(force=True)
                await page.wait_for_timeout(2000)
                return
        except Exception:
            continue

    await row.click(force=True)
    await page.wait_for_timeout(2000)

async def download_current_nfse_pdf(page, context, pdf_path):
    download_buttons = [
        "xpath=//*[self::button or self::a][contains(normalize-space(), 'Imprimir') or contains(normalize-space(), 'PDF') or contains(normalize-space(), 'Download') or contains(normalize-space(), 'Baixar')]",
        "xpath=//*[self::button or self::a or self::span or self::i][contains(@class,'print') or contains(@class,'pdf') or contains(@class,'download')]",
    ]
    last_error = None
    for selector in download_buttons:
        try:
            button = await first_visible_locator(page, selector, timeout_ms=5000)
            try:
                async with page.expect_download(timeout=15000) as download_info:
                    await button.click(force=True)
                download = await download_info.value
                await download.save_as(pdf_path)
                if is_pdf_file(pdf_path):
                    return True
                last_error = RuntimeError("Download consultado não retornou um PDF válido")
            except Exception as download_error:
                last_error = download_error
                try:
                    async with context.expect_page(timeout=15000) as popup_info:
                        await button.click(force=True)
                    popup = await popup_info.value
                    await popup.wait_for_load_state()
                    if "pdf" in popup.url.lower():
                        response = await popup.request.get(popup.url)
                        with open(pdf_path, "wb") as f:
                            f.write(await response.body())
                    else:
                        await save_open_invoice_pdf_from_viewer(popup, context, pdf_path)
                    await popup.close()
                    if is_pdf_file(pdf_path):
                        return True
                    last_error = RuntimeError("Popup consultado não retornou um PDF válido")
                except Exception as popup_error:
                    last_error = popup_error
        except Exception as exc:
            last_error = exc

    try:
        return await save_open_invoice_pdf_from_viewer(page, context, pdf_path)
    except Exception as viewer_error:
        raise RuntimeError(f"Não consegui baixar o PDF oficial da nota consultada pelo site. Último erro: {last_error}; viewer: {viewer_error}")

async def save_open_invoice_pdf_from_viewer(page, context, pdf_path):
    """Save the currently opened invoice PDF from the portal viewer/modal."""
    # First try to extract a direct PDF/blob source from the viewer. This avoids
    # the native download control, which can be canceled by the portal when the
    # viewer is embedded in a modal or popup.
    try:
        encoded_pdf = await page.evaluate("""
            async () => {
                const seen = new Set();
                const candidates = [];
                function allDocuments(doc) {
                    const docs = [doc];
                    for (const frame of doc.querySelectorAll('iframe')) {
                        try {
                            if (frame.contentDocument) docs.push(...allDocuments(frame.contentDocument));
                        } catch (e) {}
                    }
                    return docs;
                }
                function walk(root) {
                    if (!root || seen.has(root)) return;
                    seen.add(root);
                    const nodes = root.querySelectorAll ? Array.from(root.querySelectorAll('*')) : [];
                    for (const node of nodes) {
                        if (node.shadowRoot) walk(node.shadowRoot);
                        const tag = (node.tagName || '').toUpperCase();
                        if ((tag === 'EMBED' || tag === 'IFRAME' || tag === 'OBJECT') && (node.src || node.data)) {
                            candidates.push(node.src || node.data);
                        }
                        if (node.id && /download|pdf|viewer/i.test(node.id)) {
                            const src = node.getAttribute && (node.getAttribute('src') || node.getAttribute('data') || node.getAttribute('href'));
                            if (src) candidates.push(src);
                        }
                    }
                }
                for (const doc of allDocuments(document)) walk(doc);

                // The portal (PrimeFaces Extensions pdfviewer.html.jsf) wraps the
                // real PDF resource URL inside its own iframe src's "file" query
                // param (URL-encoded) - that inner URL is the one that actually
                // streams PDF bytes; the wrapper URL itself returns HTML.
                const resolved = [];
                for (const url of candidates) {
                    try {
                        const parsed = new URL(url, window.location.href);
                        const inner = parsed.searchParams.get('file');
                        if (inner) resolved.push(new URL(inner, window.location.href).href);
                    } catch (e) {}
                    resolved.push(url);
                }

                for (const src of resolved) {
                    const lower = String(src || '').toLowerCase();
                    if (!(lower.startsWith('blob:') || lower.includes('.pdf') || lower.includes('download') || lower.includes('dynamiccontent'))) continue;
                    try {
                        const response = await fetch(src);
                        const contentType = response.headers.get('content-type') || '';
                        const buffer = await response.arrayBuffer();
                        const bytes = new Uint8Array(buffer);
                        const isPdf = bytes.length > 4 && bytes[0] === 0x25 && bytes[1] === 0x50 && bytes[2] === 0x44 && bytes[3] === 0x46;
                        if (!isPdf && !contentType.toLowerCase().includes('pdf')) continue;
                        let binary = '';
                        for (let i = 0; i < bytes.length; i += 0x8000) {
                            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
                        }
                        return btoa(binary);
                    } catch (e) {}
                }
                return null;
            }
        """)
        if encoded_pdf:
            with open(pdf_path, "wb") as f:
                f.write(base64.b64decode(encoded_pdf))
            if is_pdf_file(pdf_path):
                return True
    except Exception:
        pass

    # Fallback: click the native download control in the embedded PDF viewer.
    download_error = None
    try:
        async with page.expect_download(timeout=15000) as download_info:
            clicked = await page.evaluate("""
                () => {
                    const seen = new Set();
                    function allDocuments(doc) {
                        const docs = [doc];
                        for (const frame of doc.querySelectorAll('iframe')) {
                            try {
                                if (frame.contentDocument) docs.push(...allDocuments(frame.contentDocument));
                            } catch (e) {}
                        }
                        return docs;
                    }
                    function allNodes(root) {
                        if (!root || seen.has(root)) return [];
                        seen.add(root);
                        const out = [];
                        const nodes = root.querySelectorAll ? Array.from(root.querySelectorAll('*')) : [];
                        for (const node of nodes) {
                            out.push(node);
                            if (node.shadowRoot) out.push(...allNodes(node.shadowRoot));
                        }
                        return out;
                    }
                    const docs = allDocuments(document);
                    for (const doc of docs) {
                        const direct = doc.querySelector('#download, #secondaryDownload, button[title*="Download"], button[title*="download"]');
                        if (direct) {
                            direct.click();
                            return true;
                        }
                    }
                    const nodes = docs.flatMap((doc) => allNodes(doc));
                    const button = nodes.find((node) => {
                        const txt = `${node.id || ''} ${node.title || ''} ${node.ariaLabel || ''} ${node.className || ''}`.toLowerCase();
                        return txt.includes('download') || txt.includes('baixar') || txt.includes('save') || txt.includes('salvar');
                    });
                    if (!button) return false;
                    button.click();
                    return true;
                }
            """)
            if not clicked:
                raise RuntimeError("Controle de download do visualizador PDF não encontrado")
        download = await download_info.value
        await download.save_as(pdf_path)
        if is_pdf_file(pdf_path):
            return True
        raise RuntimeError("Download do visualizador não retornou um PDF válido")
    except Exception as exc:
        download_error = exc

    # Last fallback: fetch a direct blob/pdf source, but only accept real PDFs.
    try:
        encoded_pdf = await page.evaluate("""
            async () => {
                const seen = new Set();
                const candidates = [];
                function allDocuments(doc) {
                    const docs = [doc];
                    for (const frame of doc.querySelectorAll('iframe')) {
                        try {
                            if (frame.contentDocument) {
                                docs.push(...allDocuments(frame.contentDocument));
                            }
                        } catch (e) {}
                    }
                    return docs;
                }
                function walk(root) {
                    if (!root || seen.has(root)) return;
                    seen.add(root);
                    const nodes = root.querySelectorAll ? root.querySelectorAll('*') : [];
                    for (const node of nodes) {
                        if ((node.tagName === 'EMBED' || node.tagName === 'IFRAME' || node.tagName === 'OBJECT') && node.src) {
                            candidates.push(node.src);
                        }
                        if (node.shadowRoot) walk(node.shadowRoot);
                    }
                }
                for (const doc of allDocuments(document)) walk(doc);
                const src = candidates.find((url) => url.startsWith('blob:') || url.toLowerCase().includes('.pdf'));
                if (!src) return null;
                const response = await fetch(src);
                const contentType = response.headers.get('content-type') || '';
                const buffer = await response.arrayBuffer();
                const bytes = new Uint8Array(buffer);
                const isPdf = bytes[0] === 0x25 && bytes[1] === 0x50 && bytes[2] === 0x44 && bytes[3] === 0x46;
                if (!isPdf && !contentType.toLowerCase().includes('pdf')) return null;
                let binary = '';
                for (let i = 0; i < bytes.length; i += 0x8000) {
                    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
                }
                return btoa(binary);
            }
        """)
        if encoded_pdf:
            with open(pdf_path, "wb") as f:
                f.write(base64.b64decode(encoded_pdf))
            if is_pdf_file(pdf_path):
                return True
    except Exception:
        pass

    raise RuntimeError(f"Não consegui salvar um PDF válido aberto no visualizador: {download_error}")

def _is_pdf_response(response):
    try:
        headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
    except Exception:
        headers = {}
    url = (getattr(response, "url", "") or "").lower()
    content_type = headers.get("content-type", "").lower()
    content_disp = headers.get("content-disposition", "").lower()
    return (
        "pdf" in content_type
        or url.endswith(".pdf")
        or ".pdf?" in url
        or "pdf" in content_disp
    )

async def capture_pdf_response_bytes(context, trigger_coro=None, timeout_ms=15000):
    """Capture the first real PDF response produced after trigger_coro starts."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    async def handle_response(response):
        if future.done() or not _is_pdf_response(response):
            return
        try:
            body = await response.body()
            if body[:5] != b"%PDF-":
                return
            future.set_result(body)
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)

    def attach(page):
        page.on("response", lambda response: asyncio.create_task(handle_response(response)))

    for page in context.pages:
        attach(page)

    context.on("page", attach)

    if trigger_coro is not None:
        await trigger_coro

    try:
        body = await asyncio.wait_for(future, timeout=timeout_ms / 1000)
        return body
    except Exception:
        return None

def extract_invoice_number_from_pdf(pdf_path):
    if not is_pdf_file(pdf_path):
        return None, ""
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        text_list = []
        for page in reader.pages:
            t = page.extract_text()
            if t:
                text_list.append(t)
        text = "\n".join(text_list)
        return extract_invoice_number_from_text(text), text
    except Exception:
        return None, ""

def parse_brl_money(text):
    """Parse a Brazilian-formatted amount like '3.482,89' or 'R$ 3.482,89' into a float."""
    if text is None:
        return None
    cleaned = re.sub(r"[^\d,.\-]", "", str(text)).strip()
    if not cleaned:
        return None
    cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None

def extract_valor_liquido_from_text(text):
    """Extract "Valor Líquido da NFSe Campinas" (net value after all retentions/deductions)
    from the emitted NFSe's PDF text.

    The portal's PDF layout places the row of amounts (Base de cálculo do ISSQN,
    Retenções, Desc. incondicionado, Valor Líquido) immediately before the literal
    "VALOR TOTAL" label and its column headers (text extraction order is reversed
    relative to visual layout), e.g.:
        R$ 3.580,00 R$ 97,11 R$ 0,00 R$ 3.482,89
        VALOR TOTAL
        Base de cálculo do ISSQN (R$) Retenções (R$) Desc. incondicionado (R$) Valor Líquido da NFSe Campinas (R$)
    """
    if not text:
        return None
    match = re.search(r"((?:R\$\s*[\d.,]+\s*){4})\n\s*VALOR TOTAL", text)
    if not match:
        return None
    amounts = re.findall(r"R\$\s*([\d.,]+)", match.group(1))
    if len(amounts) != 4:
        return None
    return parse_brl_money(amounts[-1])

def extract_valor_liquido_from_pdf(pdf_path):
    """Read the emitted NFSe PDF and return its real net value (Valor Líquido da NFSe),
    i.e. the invoice value minus whatever retentions the portal actually calculated.
    Returns None if the PDF can't be read or the field can't be found."""
    if not pdf_path or not is_pdf_file(pdf_path):
        return None
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        text = "\n".join(p.extract_text() or "" for p in reader.pages)
        return extract_valor_liquido_from_text(text)
    except Exception:
        return None

def pdf_text_matches_client(pdf_text, client):
    digits_text = re.sub(r"\D+", "", pdf_text or "")
    client_doc = re.sub(r"\D+", "", client.get("cnpj_cpf") or "")
    if client_doc and client_doc in digits_text:
        return True

    normalized_text = unicodedata.normalize("NFKD", pdf_text or "").encode("ASCII", "ignore").decode("ASCII").lower()
    normalized_name = unicodedata.normalize("NFKD", client.get("name") or "").encode("ASCII", "ignore").decode("ASCII").lower()
    normalized_name = re.sub(r"\s+", " ", normalized_name).strip()
    return bool(normalized_name and normalized_name in normalized_text)

async def run_nfse_automation(client_ids, ref_date=None, progress_callback=None, dry_run=False, dry_run_pause_seconds=20):
    """
    Automate invoice issuance for selected client IDs.
    ref_date: datetime.date (default is today)
    progress_callback: async function(msg_dict)
    dry_run: fill the form up to the final review and stop - nothing is
        emitted and nothing is written to the emissions history.
    """
    # 1. Fetch configurations
    conn = get_db_connection()
    emissions_to_process = []
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM system_config")
    config = {row["key"]: row["value"] for row in cursor.fetchall()}
    
    # Fetch clients
    placeholders = ",".join("?" for _ in client_ids)
    cursor.execute(f"SELECT * FROM clients WHERE id IN ({placeholders})", client_ids)
    clients = [dict(row) for row in cursor.fetchall()]

    # The portal's "Documento" section (added 2026-10) requires a Número
    # Documento Fiscal. Per user instruction: increment it from the last one
    # used, every run. This is NOT the same counter as invoice_number (the
    # real NFSe number, separately assigned by the portal in its own série -
    # e.g. "1", "2"... since the 2026-10 redesign), so it's tracked on its
    # own in system_config, persisted across runs.
    cursor.execute("SELECT value FROM system_config WHERE key = 'nfse_last_document_number'")
    last_doc_row = cursor.fetchone()
    if last_doc_row and last_doc_row["value"]:
        next_document_number = int(last_doc_row["value"]) + 1
    else:
        # One-time bootstrap for the first run after this feature was added:
        # fall back to the old invoice_number sequence (last used before the
        # portal redesign) so we don't start over from 1 and collide with it.
        cursor.execute(
            "SELECT MAX(CAST(invoice_number AS INTEGER)) AS max_num FROM emissions "
            "WHERE status = 'emitida' AND invoice_number IS NOT NULL AND invoice_number != ''"
        )
        max_row = cursor.fetchone()
        next_document_number = (max_row["max_num"] or 0) + 1 if max_row else 1
    conn.close()
    
    portal_cnpj = config.get("portal_cnpj", "07.268.051/0001-48")
    portal_password = config.get("portal_password", "5C0A11EF")
    headless = config.get("headless", "false").lower() == "true"
    # Which emitter to pick in the portal's transition dialog: "novo" (padrão
    # nacional, mandatory for Simples Nacional from 01/11/2026) or "atual".
    emissor_model = (config.get("nfse_emissor_model") or "novo").strip().lower()
    emissor_settings = {key: (config.get(key) or default) for key, default in NEW_EMISSOR_DEFAULTS.items()}
    
    # Calculate competence info
    comp_info = get_competence_info(ref_date)
    competence_str = comp_info["month_year_short"]  # MM/YYYY
    folder_name = comp_info["month_year_short"].replace("/", "-")  # MM-YYYY
    
    # Folder paths
    invoice_folder = os.path.join(INVOICES_DIR, folder_name)
    screenshot_folder = os.path.join(SCREENSHOTS_DIR, folder_name)
    os.makedirs(invoice_folder, exist_ok=True)
    os.makedirs(screenshot_folder, exist_ok=True)
    
    async def log_progress(msg, status="info", client_id=None, pdf_url=None):
        if progress_callback:
            await progress_callback({
                "timestamp": datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                "client_id": client_id,
                "status": status,
                "message": msg,
                "pdf_url": pdf_url
            })
        print(f"[{status.upper()}] {msg}")

    await log_progress("Iniciando Playwright...", "info")
    
    async with async_playwright() as p:
        # Launch headed browser so user can solve captcha
        browser = await p.chromium.launch(
            headless=headless,
            args=["--start-maximized", "--disable-notifications", "--disable-popup-blocking"]
        )
        
        # Open in maximized viewport if headed
        context = await browser.new_context(
            # Headed: follow the real window size - a fixed 1920x1080 viewport on a
            # smaller/scaled screen clips the page and leaves the reCAPTCHA unclickable.
            **({"viewport": {"width": 1920, "height": 1080}} if headless else {"no_viewport": True})
        )
        page = await context.new_page()
        
        # 2. Login flow
        await log_progress("Acessando portal da NFS-e Campinas...", "info")
        await page.goto(LOGIN_URL)
        await page.wait_for_timeout(3000)
        
        # Check if already logged in or if on login screen
        if "#/login" in page.url:
            await log_progress("Preenchendo credenciais no portal...", "info")
            
            # Locate input card for "Acesso via senha"
            # In campinas portal: Name is cpfCnpj, senha is senha
            await page.fill('input[name="cpfCnpj"]', re.sub(r"\D+", "", portal_cnpj))
            await page.fill('input[name="senha"]', portal_password)
            
            await log_progress("Aguardando solução do reCAPTCHA pelo usuário. Resolva o captcha na janela do navegador; a automação clicará em ENTRAR e seguirá sozinha.", "captcha")

            # Poll until the captcha is solved, then click "Entrar". If the user clicks
            # manually first, the URL/sidebar check below will also detect success.
            login_success = False
            clicked_login = False
            warned_manual_login = False
            for sec in range(180): # Wait up to 3 minutes
                await page.wait_for_timeout(1000)
                try:
                    if await wait_for_logged_in(page, timeout_ms=750):
                        login_success = True
                        break

                    if not clicked_login and await is_recaptcha_solved(page):
                        await log_progress("reCAPTCHA resolvido. Clicando em ENTRAR...", "captcha")
                        if await click_login_button(page):
                            clicked_login = True
                            try:
                                await page.wait_for_load_state("networkidle", timeout=10000)
                            except PlaywrightTimeoutError:
                                pass
                        elif not warned_manual_login:
                            await log_progress("Não localizei o botão ENTRAR automaticamente. Clique em ENTRAR na janela do navegador para continuar.", "warning")
                            warned_manual_login = True

                    if clicked_login and "#/login" not in page.url:
                        login_success = await wait_for_logged_in(page, timeout_ms=10000)
                        if login_success:
                            break
                except Exception:
                    # Ignore context destroyed/navigation errors during transition
                    pass
            
            if not login_success:
                await log_progress("Tempo esgotado aguardando o login/captcha. Certifique-se de que a opção 'Executar navegador em modo oculto (headless)' está DESMARCADA nas Configurações para que a tela do navegador apareça e você possa resolver o captcha.", "error")
                await browser.close()
                return
            
            await log_progress("Login realizado com sucesso!", "success")
        else:
            await log_progress("Já autenticado no portal.", "info")
            
        await page.wait_for_timeout(2000)

        if not await wait_for_logged_in(page, timeout_ms=20000):
            await log_progress("Login detectado, mas o menu do portal ainda não carregou. Reabrindo a página principal...", "warning")
            await page.goto(PRINCIPAL_URL)
            await wait_for_logged_in(page, timeout_ms=30000)
        
        # 3. Process each client
        for client in clients:
            client_id = client["id"]
            client_name = client["name"]
            client_cnpj = re.sub(r"\D+", "", client["cnpj_cpf"])
            ref_note = client["reference_note"]
            retention_type = client["retention_type"]
            invoice_value = client["invoice_value"]
            boleto_value = client["boleto_value"]
            
            await log_progress(f"Iniciando emissão para: {client_name}", "running", client_id)
            
            # Format value for insertion (standard currency: 1.234,56 or 150,00)
            value_br = f"{invoice_value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            
            # Calculate formatted description
            desc_text = format_description(client["description_template"], ref_date)
            
            try:
                # Navigate to note emission page
                # Expand sidebar menu
                await log_progress("Navegando para o menu de Emissão...", "running", client_id)
                if emissor_model == "novo":
                    menu_info = await open_new_emissor_page(page, timeout_ms=30000)
                    if dry_run:
                        await log_progress(f"Entradas de menu de nota fiscal: {menu_info.get('entries')} | URL: {page.url}", "info", client_id)
                else:
                    await open_emission_page(page, timeout_ms=30000)

                # The portal may offer a choice between the old ("Emissor
                # Atual") and new ("Novo Emissor") layouts during its 2026-10
                # transition - see handle_emissor_model_choice's docstring.
                model_choice = await handle_emissor_model_choice(page, timeout_ms=5000, prefer=emissor_model)
                if model_choice.get("found"):
                    await log_progress(
                        f"Modelo de emissor selecionado: {'Emissor Atual (ABRASF)' if model_choice.get('chosen') == 'atual' else 'Novo Emissor (padrão nacional)'}.",
                        "info",
                        client_id,
                    )

                is_wizard = await is_new_emissor_wizard(
                    page, timeout_ms=15000 if model_choice.get('chosen') == 'novo' else 4000
                )
                if emissor_model == "novo" and not is_wizard:
                    raise RuntimeError(
                        f"Não consegui abrir o Novo Emissor (padrão nacional). URL atual: {page.url}. "
                        "Para usar o emissor antigo, defina nfse_emissor_model = 'atual' na configuração."
                    )
                if is_wizard:
                    async def wizard_log(message, status="info"):
                        await log_progress(message, status, client_id)

                    debug_prefix = os.path.join(screenshot_folder, f"{slugify_name(client_name)}_novo_emissor") if dry_run else None
                    await fill_new_emissor_wizard(page, client, desc_text, emissor_settings, wizard_log, debug_prefix=debug_prefix)
                else:
                    await page.wait_for_timeout(2000)

                    # Wait for the note form to load. On the old layout this is
                    # "Número da Nota" (by @placeholder); on the new layout (no
                    # placeholders at all) it's "Número Documento Fiscal" (by id).
                    note_input_sel = "xpath=//input[contains(@id, 'idDocumentoNumeroNotaFiscal') or (@placeholder='Número da Nota' and contains(@id, ':j_idt'))]"
                    await page.wait_for_selector(note_input_sel, timeout=20000)

                    # The "Documento" section only exists on the new (2026-10)
                    # layout and is mandatory there: a Documento type and a
                    # Número Documento Fiscal. Per user instruction, always use
                    # "Nota Fiscal de Serviços" and the increment of the last
                    # emitted invoice number. Skipped entirely on the old layout,
                    # which has no such section.
                    doc_type_label = page.locator('[id="formNotaFiscal:idDocumentoDocumento_label"]').first
                    if await doc_type_label.count() > 0:
                        await log_progress(
                            f"Preenchendo seção Documento (Nota Fiscal de Serviços nº {next_document_number})...",
                            "running",
                            client_id,
                        )
                        await doc_type_label.click()
                        await page.wait_for_timeout(500)
                        doc_type_option = page.locator(
                            "xpath=//li[contains(@class,'ui-selectonemenu-item') and normalize-space()='Nota Fiscal de Serviços']"
                        ).first
                        await doc_type_option.click()
                        await page.wait_for_timeout(500)

                        doc_number_field = page.locator(note_input_sel).first
                        await doc_number_field.click()
                        await page.keyboard.press("Control+A")
                        await page.keyboard.press("Backspace")
                        await doc_number_field.press_sequentially(str(next_document_number), delay=40)
                        await doc_number_field.press("Tab")
                        await page.wait_for_timeout(300)
                        # Persist immediately (not just increment in memory) so the
                        # next run - even if this one crashes right after - never
                        # reuses this number.
                        doc_num_conn = get_db_connection()
                        doc_num_conn.execute(
                            "INSERT INTO system_config (key, value) VALUES ('nfse_last_document_number', ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                            (str(next_document_number),),
                        )
                        doc_num_conn.commit()
                        doc_num_conn.close()
                        next_document_number += 1

                    # 4. Clone or fill from scratch
                    cloned = False
                    clonar_btn_sel = "xpath=//*[self::a or self::button][normalize-space()='Clonar' or .//span[normalize-space()='Clonar']]"
                    clone_feature_available = await page.locator(clonar_btn_sel).count() > 0
                    if ref_note and not clone_feature_available:
                        await log_progress(
                            "O portal não oferece mais a opção 'Clonar' nesta tela (mudou de layout). "
                            "Preenchendo os dados manualmente em vez de clonar.",
                            "warning",
                            client_id,
                        )
                    if ref_note and clone_feature_available:
                        await log_progress(f"Clonando a nota de referência: {ref_note}...", "running", client_id)

                        # Try cloning (up to 3 attempts as in the user's script)
                        for attempt in range(1, 4):
                            try:
                                # Re-locate field to avoid stale references
                                note_input = await fill_first_visible(page, note_input_sel, str(ref_note), timeout_ms=10000)
                                await page.wait_for_timeout(1000) # Wait state settle
                            
                                # Click the search clone button. Avoid "Clonar Esta" from the last-issued-note panel.
                                clonar_btn_sel = "xpath=//*[self::a or self::button][normalize-space()='Clonar' or .//span[normalize-space()='Clonar']]"
                                await click_first_visible(page, clonar_btn_sel, timeout_ms=10000)
                                await log_progress("Clonagem iniciada; aguardando retorno do portal...", "running", client_id)
                                await page.wait_for_timeout(3000)
                            
                                # Check for "nota não encontrada" error
                                toast_err = page.locator("text=A nota informada não foi encontrada")
                                if await toast_err.count() > 0 and await toast_err.is_visible():
                                    await log_progress(f"Erro: Nota de referência {ref_note} não encontrada.", "error", client_id)
                                    break
                                
                                cloned = True
                                await log_progress("Nota clonada. Seguindo sem tentar clonar novamente.", "success", client_id)
                                break
                            except Exception as e:
                                await log_progress(f"Tentativa de clone {attempt}/3 falhou: {str(e)}", "warning", client_id)
                                await page.wait_for_timeout(2000)
                
                    if not cloned:
                        if ref_note:
                            await log_progress("Falha ao clonar nota de referência. Tentando preencher dados manualmente...", "warning", client_id)
                        else:
                            await log_progress("Cliente sem nota de referência. Preenchendo dados manualmente...", "running", client_id)
                    
                        # Wait for form/widget initialization
                        await page.wait_for_timeout(3000)
                    
                        # Select Atividade do cadastro econômico (CNAE/Serviço) first
                        await log_progress("Selecionando atividade econômica (620400001 - Consultoria em TI)...", "running", client_id)
                        activity_result = await select_economic_activity(page, "620400001", timeout_ms=20000)
                        already_selected = " já estava selecionada" if activity_result.get("alreadySelected") else ""
                        await log_progress(f"Atividade econômica{already_selected}: {activity_result.get('text')}", "success", client_id)
                        complement_result = await close_optional_complement_dialog(page, timeout_ms=5000)
                        if complement_result.get("closed"):
                            await log_progress("Popup opcional de complemento fechada para continuar a emissão.", "info", client_id)
                            activity_result = await select_economic_activity(page, "620400001", timeout_ms=20000)
                            await log_progress(f"Atividade econômica confirmada após fechar complemento: {activity_result.get('text')}", "success", client_id)
                    
                        await page.wait_for_timeout(3000) # Wait for AJAX reload of taxation
                    
                        # Fill CNPJ/CPF of tomador second
                        cnpj_field_sel = 'xpath=//input[contains(@id, "CpfCnpj") or contains(@name, "CpfCnpj")]'
                        cnpj_field = await first_visible_locator(page, cnpj_field_sel, timeout_ms=10000, require_enabled=True)
                        await cnpj_field.click()
                        # Clear it first using focus and select_all
                        await page.keyboard.press("Control+A")
                        await page.keyboard.press("Backspace")
                        # Type the CNPJ character by character
                        await cnpj_field.press_sequentially(client_cnpj, delay=100)
                        await page.wait_for_timeout(500)
                        await cnpj_field.press("Tab")
                    
                        # Manually dispatch events to force PrimeFaces/jQuery autocomplete & AJAX triggers
                        await page.evaluate("""
                            (el) => {
                                if (el) {
                                    el.dispatchEvent(new Event('input', { bubbles: true }));
                                    el.dispatchEvent(new Event('change', { bubbles: true }));
                                    el.dispatchEvent(new Event('blur', { bubbles: true }));
                                }
                            }
                        """, cnpj_field)
                    
                        await page.wait_for_timeout(1000)
                    

                        # Click the "Pesquisar" button next to CNPJ field
                        await log_progress("Acionando botão de pesquisa do Tomador...", "running", client_id)
                        search_selectors = [
                            "xpath=//*[self::button or self::a][contains(@id, 'pesquisar') or contains(@id, 'Pesquisar') or contains(., 'Pesquisar')]",
                            "xpath=//*[contains(@id, 'tomador') or contains(@class, 'tomador') or contains(., 'Tomador')]//*[self::button or self::a][contains(., 'Pesquisar') or contains(., 'Pesq')]",
                            "xpath=//*[self::button or self::a][contains(., 'Pesquisar')]",
                            "xpath=//*[self::button or self::a][contains(., 'Pesq')]"
                        ]
                        clicked_search = False
                        for sel in search_selectors:
                            try:
                                loc = page.locator(sel)
                                if await loc.count() > 0:
                                    for index in range(await loc.count()):
                                        item = loc.nth(index)
                                        if await item.is_visible():
                                            btn_text = (await item.inner_text()).strip()
                                            # Skip menu button
                                            if btn_text.lower() == "menu":
                                                continue
                                            await log_progress(f"Clicando no botão de pesquisa do Tomador: '{btn_text}'", "running", client_id)
                                            await item.click()
                                            clicked_search = True
                                            break
                                    if clicked_search:
                                        break
                            except Exception:
                                continue
                            
                        if clicked_search:
                            await log_progress("Botão de pesquisa do Tomador clicado com sucesso.", "success", client_id)
                        else:
                            await log_progress("Alerta: não consegui localizar o botão de pesquisa do tomador.", "warning", client_id)
                        
                        await page.wait_for_timeout(4000) # Wait for AJAX load of tomador details
                        complement_result = await close_optional_complement_dialog(page, timeout_ms=3000)
                        if complement_result.get("closed"):
                            await log_progress("Popup opcional de complemento fechada após pesquisar o tomador.", "info", client_id)
                    
                        # Check if "Inserir" button is visible under Dados Cadastrais and click it to bind/add the tomador to the invoice
                        inserir_selectors = [
                            "xpath=//*[self::button or self::a][contains(normalize-space(.), 'Inserir') or contains(normalize-space(text()), 'Inserir')]",
                            "xpath=//*[contains(@id, 'cadastrais') or contains(@id, 'cadastro') or contains(., 'Dados Cadastrais')]//*[self::button or self::a][contains(., 'Inserir')]",
                            "xpath=//*[self::button or self::a][contains(., 'Inserir')]"
                        ]
                        clicked_inserir = False
                        for sel in inserir_selectors:
                            try:
                                loc = page.locator(sel)
                                if await loc.count() > 0:
                                    for index in range(await loc.count()):
                                        item = loc.nth(index)
                                        if await item.is_visible():
                                            await log_progress("Botão 'Inserir' do Tomador detectado (tomador de fora). Vinculando tomador à nota...", "running", client_id)
                                            await item.click()
                                            await page.wait_for_timeout(3000) # Wait for AJAX load of tomador association
                                            clicked_inserir = True
                                            break
                                    if clicked_inserir:
                                        break
                            except Exception:
                                continue
                            
                        if clicked_inserir:
                            await log_progress("Tomador de fora vinculado com sucesso.", "success", client_id)
                            complement_result = await close_optional_complement_dialog(page, timeout_ms=5000)
                            if complement_result.get("closed"):
                                await log_progress("Popup opcional de complemento fechada após vincular o tomador.", "info", client_id)

                        await log_progress("Conferindo atividade econômica após carregar/vincular o tomador...", "running", client_id)
                        activity_result = await select_economic_activity(page, "620400001", timeout_ms=20000)
                        already_selected = " permaneceu selecionada" if activity_result.get("alreadySelected") else " foi reaplicada"
                        await log_progress(f"Atividade econômica{already_selected}: {activity_result.get('text')}", "success", client_id)
                        complement_result = await close_optional_complement_dialog(page, timeout_ms=3000)
                        if complement_result.get("closed"):
                            await log_progress("Popup opcional de complemento fechada após reconferir a atividade.", "info", client_id)
                            activity_result = await select_economic_activity(page, "620400001", timeout_ms=20000)
                            await log_progress(f"Atividade econômica confirmada novamente: {activity_result.get('text')}", "success", client_id)
                        
                        # Wait for AJAX reload of taxation details (when Alíquota and Valor ISS exit the '*****' loading state)
                        await log_progress("Aguardando o portal calcular as alíquotas (saindo do estado '*****')...", "running", client_id)
                        aliquota_sel = "xpath=(//*[contains(normalize-space(.), 'Alíquota')]/following::input)[1]"
                    
                        aliquota_loaded = False
                        for attempt in range(15): # Wait up to 15 seconds
                            try:
                                val = await page.locator(aliquota_sel).first.input_value()
                                if val and "*****" not in val:
                                    await log_progress(f"Alíquotas carregadas com sucesso: {val}", "success", client_id)
                                    aliquota_loaded = True
                                    break
                            except Exception:
                                pass
                            await page.wait_for_timeout(1000)
                        
                        if not aliquota_loaded:
                            await log_progress("Aviso: tempo esgotado aguardando carregamento das alíquotas. Prosseguindo...", "warning", client_id)






                    
                    # 5. Pre-fill note details
                    await log_progress(f"Ajustando competência da nota para {competence_str}...", "running", client_id)
                    _, applied_competence = await fill_competence_field(page, comp_info, timeout_ms=30000)
                    if applied_competence:
                        await log_progress(f"Competência aplicada no portal: {applied_competence}", "success", client_id)
                    else:
                        await log_progress("Não encontrei um campo editável de competência no formulário; seguindo sem alterar esse campo.", "warning", client_id)

                    if not cloned:
                        await log_progress("Revalidando atividade econômica antes de preencher os dados finais da nota...", "running", client_id)
                        activity_result = await select_economic_activity(page, "620400001", timeout_ms=20000)
                        already_selected = " confirmada" if activity_result.get("alreadySelected") else " reaplicada"
                        await log_progress(f"Atividade econômica{already_selected}: {activity_result.get('text')}", "success", client_id)
                        complement_result = await close_optional_complement_dialog(page, timeout_ms=3000)
                        if complement_result.get("closed"):
                            await log_progress("Popup opcional de complemento fechada antes dos dados finais.", "info", client_id)
                            activity_result = await select_economic_activity(page, "620400001", timeout_ms=20000)
                            await log_progress(f"Atividade econômica confirmada para emissão: {activity_result.get('text')}", "success", client_id)

                    await log_progress("Preenchendo descrição da nota...", "running", client_id)

                    # Fill Descrição Nota Fiscal. In cloned notes the service value is
                    # kept from the reference note and must not be changed.
                    if cloned:
                        await log_progress("Aguardando a tela da nota clonada carregar...", "running", client_id)
                        try:
                            # Wait up to 8 seconds to see if description field is already visible (already unlocked)
                            await find_description_field(page, timeout_ms=8000)
                        except Exception:
                            # If description is not loaded, search for Tomador by CNPJ to unlock the form
                            await log_progress("Campos de serviço não carregados automaticamente após clonagem. Efetuando pesquisa do Tomador para desbloquear...", "warning", client_id)
                        
                            # Fill CNPJ/CPF of tomador
                            cnpj_field_sel = 'xpath=//input[contains(@id, "CpfCnpj") or contains(@name, "CpfCnpj")]'
                            cnpj_field = await first_visible_locator(page, cnpj_field_sel, timeout_ms=10000, require_enabled=True)
                            cnpj_id = await cnpj_field.get_attribute("id")
                            client_cnpj_formatted = client["cnpj_cpf"] # formatted value e.g. "60.993.193/0001-50"
                        
                            await log_progress(f"Preenchendo CNPJ/CPF do Tomador: {client_cnpj_formatted}", "running", client_id)
                            await page.evaluate("""
                                (args) => {
                                    const el = document.getElementById(args.id);
                                    if (el) {
                                        el.value = args.val;
                                        el.dispatchEvent(new Event('input', { bubbles: true }));
                                        el.dispatchEvent(new Event('change', { bubbles: true }));
                                        el.dispatchEvent(new Event('blur', { bubbles: true }));
                                    }
                                }
                            """, {"id": cnpj_id, "val": client_cnpj_formatted})
                            await page.wait_for_timeout(1000)
                        
                            # Click the "Pesquisar" button next to CNPJ field
                            await log_progress("Acionando botão de pesquisa do Tomador...", "running", client_id)
                            search_btn_sel = "xpath=//a[contains(@id, 'formNotaFiscal:') and contains(normalize-space(.), 'Pesquisar')]"
                            try:
                                btn_locator = page.locator(search_btn_sel).first
                                await btn_locator.wait_for(state="visible", timeout=8000)
                                await btn_locator.evaluate("(el) => el.click()")
                                await log_progress("Botão de pesquisa do Tomador clicado via JS.", "running", client_id)
                            except Exception as search_click_exc:
                                await log_progress(f"Falha ao clicar no botão de pesquisa via seletor específico: {search_click_exc}. Tentando fallback...", "warning", client_id)
                                search_selectors = [
                                    "xpath=//*[self::button or self::a][contains(@id, 'pesquisar') or contains(@id, 'Pesquisar') or contains(., 'Pesquisar')]",
                                    "xpath=//*[contains(@id, 'tomador') or contains(@class, 'tomador') or contains(., 'Tomador')]//*[self::button or self::a][contains(., 'Pesquisar') or contains(., 'Pesq')]",
                                    "xpath=//*[self::button or self::a][contains(., 'Pesquisar')]",
                                    "xpath=//*[self::button or self::a][contains(., 'Pesq')]"
                                ]
                                clicked_search = False
                                for sel in search_selectors:
                                    try:
                                        loc = page.locator(sel)
                                        if await loc.count() > 0:
                                            for index in range(await loc.count()):
                                                item = loc.nth(index)
                                                if await item.is_visible():
                                                    btn_text = (await item.inner_text()).strip()
                                                    if btn_text.lower() == "menu":
                                                        continue
                                                    await log_progress(f"Clicando no botão de pesquisa do Tomador (fallback): '{btn_text}'", "running", client_id)
                                                    await item.evaluate("(el) => el.click()")
                                                    clicked_search = True
                                                    break
                                            if clicked_search:
                                                break
                                    except Exception:
                                        pass
                        
                            await page.wait_for_timeout(3000)
                        
                            # Wait for description field again
                            try:
                                await find_description_field(page, timeout_ms=30000)
                            except Exception as e:
                                try:
                                    dom = await page.content()
                                    with open("C:/Projetos/campinas-nfse-automator/campinas_dom.html", "w", encoding="utf-8") as f:
                                        f.write(dom)
                                    print("[CAMPINAS INFO] DOM da tela de emissão salvo em campinas_dom.html!")
                                except Exception as dom_exc:
                                    print(f"[CAMPINAS WARNING] Falha ao salvar DOM da página: {dom_exc}")
                                raise e
                    desc_field = await fill_description(page, desc_text, timeout_ms=30000)
                    await desc_field.press("Tab")
                    await page.wait_for_timeout(1000)

                    if cloned:
                        await log_progress("Nota clonada: mantendo Valor dos Serviços original da referência.", "info", client_id)
                    else:
                        await log_progress("Preenchendo Valor dos Serviços...", "running", client_id)
                        value_result = await fill_invoice_service_value(page, value_br, timeout_ms=15000)
                        await log_progress(
                            f"Valor dos Serviços confirmado no portal: {value_result.get('value')}",
                            "success",
                            client_id
                        )
                        await log_progress("Aguardando recálculo do ISSQN após informar o valor...", "running", client_id)
                        tax_snapshot = await wait_tax_calculation_ready(page, timeout_ms=30000)
                        await log_progress(
                            f"Cálculo do ISSQN concluído: {', '.join(tax_snapshot.get('values') or [])}",
                            "success",
                            client_id
                        )

                    if VALIDATION_ONLY:
                        await log_progress("Modo validação ativo: NÃO vou clicar em Emitir Nota Fiscal.", "warning", client_id)
                        await log_progress(f"Descrição aplicada a partir do template cadastrado: {desc_text}", "info", client_id)

                        validation_screenshot = os.path.join(
                            screenshot_folder,
                            f"{slugify_name(client_name)}_validacao_descricao.png"
                        )
                        try:
                            await page.screenshot(path=validation_screenshot, full_page=True)
                            await log_progress(f"Screenshot para conferência salvo em: {validation_screenshot}", "success", client_id)
                        except Exception as se:
                            await log_progress(f"Não consegui salvar screenshot de validação: {se}", "warning", client_id)
                            validation_screenshot = None

                        conn = get_db_connection()
                        cursor = conn.cursor()
                        cursor.execute("""
                        INSERT INTO emissions (client_id, competence, status, error_message, screenshot_path, timestamp)
                        VALUES (?, ?, ?, ?, ?, datetime('now'))
                        """, (
                            client_id,
                            competence_str,
                            "pendente",
                            "Validação manual: descrição preenchida, emissão não executada.",
                            validation_screenshot
                        ))
                        conn.commit()
                        conn.close()

                        await log_progress(f"A janela ficará aberta por {VALIDATION_PAUSE_SECONDS // 60} minutos para validação manual. Feche o navegador se terminar antes.", "warning", client_id)
                        await page.wait_for_timeout(VALIDATION_PAUSE_SECONDS * 1000)
                        break
                
                    # 6. Retentions Screen checks
                    await log_progress("Ajustando impostos retidos conforme o tipo de retenção...", "running", client_id)
                
                    # Determine which taxes to zero out
                    taxes_to_zero = ["PIS", "INSS", "CSLL", "COFINS", "IR", "Outras"]
                    if retention_type == "Sem retenção":
                        taxes_to_zero.extend(["ISS", "ISSQN"])

                    zeroed_fields = await zero_retention_fields(page, taxes_to_zero)
                    if zeroed_fields:
                        await log_progress(f"Retenções zeradas: {len(zeroed_fields)} campo(s) ajustado(s).", "success", client_id)
                    else:
                        await log_progress("Retenções já estavam zeradas ou não havia campos editáveis para ajustar.", "info", client_id)

                    await page.wait_for_timeout(1000)
                    if not cloned:
                        await log_progress("Conferindo valor e retenções antes de emitir...", "running", client_id)
                        validation_snapshot = await validate_manual_invoice_values(page, value_br, taxes_to_zero)
                        await log_progress(
                            f"Conferência final OK: Valor dos Serviços {validation_snapshot.get('serviceValue')} e retenções zeradas.",
                            "success",
                            client_id
                        )
                
                if dry_run:
                    await log_progress("Simulação concluída: formulário preenchido até a revisão final. NÃO cliquei em emitir.", "success", client_id)
                    await page.wait_for_timeout(dry_run_pause_seconds * 1000)
                    continue

                # 7. Click Emitir Nota Fiscal
                await log_progress("Clicando em Emitir Nota Fiscal...", "running", client_id)
                emit_pdf_future = asyncio.create_task(capture_pdf_response_bytes(context, timeout_ms=30000))
                if is_wizard:
                    await click_new_emissor_emit_button(page)
                else:
                    await click_emit_invoice_button(page)

                # The portal may reject the submission with a validation
                # banner (e.g. a required field left empty) instead of
                # proceeding to emit. Detect that explicitly so it surfaces
                # as a clear error instead of a confusing PDF-capture failure.
                await page.wait_for_timeout(1500)
                validation_errors = await page.evaluate("""
                    () => {
                        const visible = (el) => {
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
                        };
                        return Array.from(document.querySelectorAll('*'))
                            .filter((el) => visible(el) && /de preenchimento obrigat[oó]rio/i.test(el.innerText || el.textContent || ''))
                            .map((el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim())
                            .filter((t, i, arr) => t.length > 0 && arr.indexOf(t) === i)
                            .slice(0, 5);
                    }
                """)
                if validation_errors:
                    try:
                        emit_pdf_future.cancel()
                    except Exception:
                        pass
                    raise RuntimeError(f"Portal rejeitou a emissão por validação: {'; '.join(validation_errors)}")

                # 8. Capture only from explicit visible success messages. If the
                # portal opens the invoice PDF directly, save that PDF and extract
                # the number from its text instead.
                slug_name = slugify_name(client_name)
                date_for_filename = datetime.date.today().strftime("%d-%m-%Y")
                invoice_number, number_source = await capture_emitted_invoice_number(page, timeout_ms=30000)
                opened_pdf_path = None
                if not invoice_number:
                    await log_progress("Número não apareceu em mensagem clara. Salvando a nota aberta para extrair o número do PDF...", "warning", client_id)
                    opened_pdf_path = os.path.join(invoice_folder, f"{slug_name}_TEMP_{date_for_filename}.pdf")
                    pdf_bytes = None
                    try:
                        pdf_bytes = await asyncio.wait_for(emit_pdf_future, timeout=10)
                    except Exception:
                        pdf_bytes = None
                    if pdf_bytes:
                        with open(opened_pdf_path, "wb") as f:
                            f.write(pdf_bytes)
                    else:
                        await save_open_invoice_pdf_from_viewer(page, context, opened_pdf_path)
                    invoice_number, pdf_text = extract_invoice_number_from_pdf(opened_pdf_path)
                    if not invoice_number:
                        raise RuntimeError("Nota foi aberta, mas não consegui extrair o número do PDF salvo. Verifique manualmente antes de reexecutar.")
                    await log_progress(f"Número extraído do PDF aberto: {invoice_number}", "success", client_id)
                else:
                    await log_progress(f"Número capturado da mensagem de emissão: {invoice_number}", "success", client_id)

                # 9. Confirm and download the emitted invoice from Gerenciar NFSE.
                filename = f"{slug_name}_{invoice_number}_{date_for_filename}.pdf"
                pdf_path = os.path.join(invoice_folder, filename)
                if opened_pdf_path and os.path.exists(opened_pdf_path):
                    os.replace(opened_pdf_path, pdf_path)
                    await log_progress("PDF aberto salvo e renomeado com cliente, número e data.", "success", client_id)

                await log_progress("Abrindo Gerenciar NFSE para validar e baixar a nota emitida...", "running", client_id)
                manage_nfse_error = None
                try:
                    await open_manage_nfse(page)
                    search_mode = await search_nfse_by_number(page, invoice_number, datetime.date.today())
                    if search_mode != "download_confirmation":
                        await open_nfse_search_result(page, invoice_number, client_name, client["cnpj_cpf"])
                    try:
                        await download_current_nfse_pdf(page, context, pdf_path)
                        await log_progress("PDF salvo a partir da consulta em Gerenciar NFSE.", "success", client_id)
                    except Exception as download_error:
                        if os.path.exists(pdf_path):
                            manage_nfse_error = str(download_error)
                            await log_progress(f"Não baixei novamente em Gerenciar NFSE, mas o PDF oficial aberto após emissão já está salvo: {download_error}", "warning", client_id)
                        else:
                            raise
                except Exception as manage_error:
                    manage_nfse_error = str(manage_error)
                    if os.path.exists(pdf_path) and is_pdf_file(pdf_path):
                        await log_progress(f"Validação em Gerenciar NFSE falhou, mas a nota já está salva localmente: {manage_error}", "warning", client_id)
                    else:
                        raise
                # Use the real net value ("Valor Líquido da NFSe") calculated by the
                # portal on the emitted invoice itself, instead of the static
                # client.boleto_value config, which can drift out of date whenever
                # the ISSQN retention rate changes (e.g. Simples Nacional aliquot
                # varies with rolling revenue) and silently under-charge the boleto.
                valor_liquido_real = extract_valor_liquido_from_pdf(pdf_path)
                if valor_liquido_real is not None and abs(valor_liquido_real - boleto_value) > 0.01:
                    await log_progress(
                        f"Valor líquido real da NFSe (R$ {valor_liquido_real:,.2f}) difere do valor cadastrado no cliente (R$ {boleto_value:,.2f}). Usando o valor real da nota para o boleto.".replace(",", "X").replace(".", ",").replace("X", "."),
                        "warning", client_id
                    )
                    boleto_value = valor_liquido_real
                elif valor_liquido_real is None:
                    await log_progress("Não consegui ler o valor líquido no PDF da nota emitida; usando o valor cadastrado do cliente para o boleto.", "warning", client_id)

                await log_progress(f"Valor do boleto no relatório: R$ {boleto_value:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."), "info", client_id)

                # 10. Log Success in SQLite
                conn = get_db_connection()
                cursor = conn.cursor()
                boleto_status_val = "pendente" if client.get("requires_boleto", 1) else "nao_exigido"
                cursor.execute("""
                INSERT INTO emissions (client_id, competence, status, invoice_number, pdf_path, boleto_status, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                """, (client_id, competence_str, "emitida", invoice_number or "N/A", pdf_path, boleto_status_val))
                emission_id = cursor.lastrowid
                conn.commit()
                conn.close()
                
                if client.get("requires_boleto", 1):
                    emissions_to_process.append({
                        "emission_id": emission_id,
                        "client_name": client_name,
                        "cnpj_cpf": client["cnpj_cpf"],
                        "invoice_number": invoice_number,
                        "boleto_value": boleto_value,
                        "due_day": client["due_day"]
                    })
                
                pdf_url = f"/invoices/{folder_name}/{filename}"
                await log_progress(f"Nota emitida, validada em Gerenciar NFSE e salva para {client_name}. Nota Nº {invoice_number}", "success", client_id, pdf_url=pdf_url)
                if manage_nfse_error:
                    await log_progress(f"Observação: a validação em Gerenciar NFSE teve falha secundária, mas o PDF e o histórico foram gravados normalmente.", "warning", client_id)
                await page.wait_for_timeout(3000)
                
            except Exception as e:
                # Capture screenshot on failure
                screenshot_filename = f"{slugify_name(client_name)}_error.png"
                screenshot_path = os.path.join(screenshot_folder, screenshot_filename)
                try:
                    await page.screenshot(path=screenshot_path)
                    await log_progress(f"Screenshot de erro salvo em: {screenshot_path}", "warning", client_id)
                except Exception:
                    screenshot_path = None
                    
                error_msg = f"Erro na automação: {str(e)}\n{traceback.format_exc()}"
                await log_progress(f"Erro ao processar cliente {client_name}: {str(e)}", "error", client_id)
                
                # Log Failure in SQLite (a dry run leaves no history behind)
                if not dry_run:
                    conn = get_db_connection()
                    cursor = conn.cursor()
                    cursor.execute("""
                    INSERT INTO emissions (client_id, competence, status, error_message, screenshot_path, timestamp)
                    VALUES (?, ?, ?, ?, ?, datetime('now'))
                    """, (client_id, competence_str, "erro", str(e), screenshot_path))
                    conn.commit()
                    conn.close()
                
                # Go back or refresh portal state to prepare for next client
                try:
                    await page.goto(PRINCIPAL_URL)
                    await page.wait_for_timeout(2000)
                except Exception:
                    pass
                
        await log_progress("Processamento concluído para todos os clientes selecionados.", "success")
        await page.wait_for_timeout(3000)
        await browser.close()

    # If there are successfully emitted invoices that require boletos, run Bradesco automation
    if emissions_to_process:
        await log_progress(f"Iniciando a geração automática de {len(emissions_to_process)} boleto(s) no Bradesco...", "info")
        from boleto_automator import run_boleto_automation
        try:
            await run_boleto_automation(emissions_to_process, ref_date, progress_callback)
        except Exception as e:
            await log_progress(f"Erro na execução sequencial de boletos: {str(e)}", "error")

async def recover_nfse_pdf(client_id, invoice_number, ref_date=None, progress_callback=None):
    """Download an already emitted invoice from Gerenciar NFSE without emitting again."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM system_config")
    config = {row["key"]: row["value"] for row in cursor.fetchall()}
    cursor.execute("SELECT * FROM clients WHERE id = ?", (client_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise RuntimeError(f"Cliente {client_id} não encontrado")

    client = dict(row)
    portal_cnpj = config.get("portal_cnpj", "07.268.051/0001-48")
    portal_password = config.get("portal_password", "5C0A11EF")
    headless = config.get("headless", "false").lower() == "true"

    comp_info = get_competence_info(ref_date)
    competence_str = comp_info["month_year_short"]
    folder_name = competence_str.replace("/", "-")
    invoice_folder = os.path.join(INVOICES_DIR, folder_name)
    screenshot_folder = os.path.join(SCREENSHOTS_DIR, folder_name)
    os.makedirs(invoice_folder, exist_ok=True)
    os.makedirs(screenshot_folder, exist_ok=True)

    async def log_progress(msg, status="info", client_id_override=None, pdf_url=None):
        if progress_callback:
            await progress_callback({
                "timestamp": datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                "client_id": client_id_override or client_id,
                "status": status,
                "message": msg,
                "pdf_url": pdf_url
            })
        print(f"[{status.upper()}] {msg}")

    browser = None
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                headless=headless,
                args=["--start-maximized", "--disable-notifications", "--disable-popup-blocking"]
            )
            context = await browser.new_context(
                **({"viewport": {"width": 1920, "height": 1080}} if headless else {"no_viewport": True})
            )
            page = await context.new_page()

            await log_progress("Recuperação: acessando portal da NFS-e Campinas...", "info")
            await page.goto(LOGIN_URL)
            await page.wait_for_timeout(3000)

            if "#/login" in page.url:
                await log_progress("Recuperação: preenchendo credenciais...", "info")
                await page.fill('input[name="cpfCnpj"]', re.sub(r"\D+", "", portal_cnpj))
                await page.fill('input[name="senha"]', portal_password)
                await log_progress("Resolva o reCAPTCHA; a recuperação seguirá automaticamente.", "captcha")

                login_success = False
                clicked_login = False
                for _ in range(180):
                    await page.wait_for_timeout(1000)
                    if await wait_for_logged_in(page, timeout_ms=750):
                        login_success = True
                        break
                    if not clicked_login and await is_recaptcha_solved(page):
                        await log_progress("reCAPTCHA resolvido. Clicando em ENTRAR...", "captcha")
                        clicked_login = await click_login_button(page)
                    if clicked_login and "#/login" not in page.url:
                        login_success = await wait_for_logged_in(page, timeout_ms=10000)
                        if login_success:
                            break
                if not login_success:
                    raise RuntimeError("Tempo esgotado aguardando login/captcha")

            await log_progress(f"Recuperando nota {invoice_number} em Gerenciar NFSE...", "running")
            await open_manage_nfse(page)
            search_mode = await search_nfse_by_number(page, str(invoice_number), ref_date or datetime.date.today())
            if search_mode != "download_confirmation":
                await open_nfse_search_result(page, str(invoice_number), client["name"], client["cnpj_cpf"])

            slug_name = slugify_name(client["name"])
            date_for_filename = (ref_date or datetime.date.today()).strftime("%d-%m-%Y")
            filename = f"{slug_name}_{invoice_number}_{date_for_filename}.pdf"
            pdf_path = os.path.join(invoice_folder, filename)

            try:
                await download_current_nfse_pdf(page, context, pdf_path)
            except Exception:
                await save_open_invoice_pdf_from_viewer(page, context, pdf_path)

            if not is_pdf_file(pdf_path):
                raise RuntimeError("Arquivo baixado não é um PDF válido")

            extracted_number, _ = extract_invoice_number_from_pdf(pdf_path)
            if extracted_number and str(extracted_number) != str(invoice_number):
                raise RuntimeError(f"PDF baixado é da nota {extracted_number}, não da nota {invoice_number}")
            _, pdf_text = extract_invoice_number_from_pdf(pdf_path)
            if not pdf_text_matches_client(pdf_text, client):
                raise RuntimeError(f"PDF baixado é da nota {invoice_number}, mas não confere com o cliente {client['name']}")

            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO emissions (client_id, competence, status, invoice_number, pdf_path, timestamp)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            """, (client_id, competence_str, "emitida", str(invoice_number), pdf_path))
            conn.commit()
            conn.close()

            pdf_url = f"/invoices/{folder_name}/{filename}"
            await log_progress(f"Nota {invoice_number} recuperada e salva em {pdf_path}", "success", pdf_url=pdf_url)
            await page.wait_for_timeout(3000)
        except Exception as e:
            screenshot_path = os.path.join(screenshot_folder, f"{slugify_name(client['name'])}_recover_error.png")
            try:
                if 'page' in locals():
                    await page.screenshot(path=screenshot_path, full_page=True)
            except Exception:
                screenshot_path = None
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
            INSERT INTO emissions (client_id, competence, status, error_message, screenshot_path, timestamp)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            """, (client_id, competence_str, "erro", f"Recuperação da nota {invoice_number}: {e}", screenshot_path))
            conn.commit()
            conn.close()
            await log_progress(f"Erro ao recuperar nota {invoice_number}: {e}", "error")
        finally:
            if browser:
                await browser.close()
