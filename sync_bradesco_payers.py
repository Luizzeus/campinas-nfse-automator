import asyncio
import re
import sqlite3
from database import get_db_connection
from boleto_automator import (
    BRADESCO_LOGIN_URL, wait_for_bradesco_logged_in, click_element,
    get_central_frame, remove_overlays, normalize_ascii,
)
from playwright.async_api import async_playwright

ROW_SELECTOR = 'tr[onclick="pagSelecionarInt(this)"]'


def digits_only(value):
    return re.sub(r"\D+", "", value or "")


async def collect_all_rows(page):
    """Scrape every payer row across all pages of the 'Lista de Pagadores'
    popup. Read-only: never clicks a row (that's the part that's broken)."""
    rows_data = []
    seen_signatures = set()

    for _ in range(60):  # hard cap on pages, just in case pagination loops
        candidates = [page] + list(page.frames)
        row_locator = None
        for ctx in candidates:
            try:
                loc = ctx.locator(ROW_SELECTOR)
                if await loc.count() > 0:
                    row_locator = loc
                    break
            except Exception:
                continue

        if row_locator is None:
            break

        count = await row_locator.count()
        new_this_page = 0
        for i in range(count):
            row = row_locator.nth(i)
            try:
                hidden_values = await row.locator("input[type='hidden']").evaluate_all(
                    "els => els.map(e => e.value)"
                )
            except Exception:
                hidden_values = []
            if len(hidden_values) < 10:
                continue
            signature = tuple(hidden_values[:2])
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            new_this_page += 1
            rows_data.append({
                "nome": hidden_values[0],
                "cpf_cnpj": hidden_values[1],
                "tipo": hidden_values[2] if len(hidden_values) > 2 else "",
                "email": hidden_values[4] if len(hidden_values) > 4 else "",
                "cep1": hidden_values[5] if len(hidden_values) > 5 else "",
                "cep2": hidden_values[6] if len(hidden_values) > 6 else "",
                "endereco": hidden_values[9] if len(hidden_values) > 9 else "",
            })

        print(f"  Página lida: {new_this_page} pagador(es) novo(s), total acumulado {len(rows_data)}")

        # Try to advance to the next page.
        advanced = False
        for ctx in candidates:
            try:
                next_link = ctx.locator(
                    "xpath=//a[contains(@onclick,'carregarPagProx') or contains(@id,'_id77')]"
                ).first
                if await next_link.count() == 0:
                    continue
                is_disabled = await next_link.evaluate(
                    "el => el.classList.contains('desabilitado') || el.getAttribute('disabled') !== null"
                )
                if is_disabled:
                    continue
                await next_link.click()
                await page.wait_for_timeout(2000)
                advanced = True
                break
            except Exception:
                continue

        if not advanced:
            break

    return rows_data


async def main():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM system_config")
    config = {row["key"]: row["value"] for row in cursor.fetchall()}
    cursor.execute("SELECT id, name, cnpj_cpf FROM clients")
    clients = [dict(row) for row in cursor.fetchall()]
    conn.close()
    bradesco_user = config.get("bradesco_user", "LCSR00145")
    bradesco_password = config.get("bradesco_password", "")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=["--start-maximized"])
        context = await browser.new_context(viewport=None)
        page = await context.new_page()

        print("Acessando Bradesco...")
        await page.goto(BRADESCO_LOGIN_URL, timeout=45000)
        await page.wait_for_timeout(4000)

        user_field = page.locator('input[id="identificationForm:txtUsuario"]').first
        await user_field.click()
        await user_field.press_sequentially(bradesco_user, delay=80)
        await page.wait_for_timeout(500)
        pass_field = page.locator('input[id="identificationForm:txtSenha"]').first
        await pass_field.click()
        await pass_field.press_sequentially(bradesco_password, delay=80)
        await page.wait_for_timeout(800)
        await click_element(page, 'input[id="identificationForm:botaoAvancar"]')
        await page.wait_for_timeout(3000)

        print("Aguardando login (2FA)...")
        if not await wait_for_bradesco_logged_in(page, timeout_ms=180000):
            print("Timeout no login.")
            await browser.close()
            return
        print("Login OK.")
        await page.wait_for_timeout(3000)

        await remove_overlays(page)
        await click_element(page, "xpath=//a[normalize-space()='Cobrança' or contains(normalize-space(.), 'Cobrança')]")
        await page.wait_for_timeout(3000)

        frame = await get_central_frame(page)
        await click_element(frame, "xpath=//a[normalize-space()='Emitir Boleto' or contains(normalize-space(.), 'Emitir Boleto')]")
        await page.wait_for_timeout(3000)
        frame = await get_central_frame(page)

        print("Abrindo 'Lista de Pagadores' (somente leitura, sem clicar em nenhuma linha)...")
        popup = None
        try:
            async with context.expect_page(timeout=8000) as page_info:
                await click_element(frame, "id=frm:linkListaPagadores")
            popup = await page_info.value
        except Exception:
            if len(context.pages) > 1:
                popup = context.pages[-1]
        list_page = popup if popup else page
        await list_page.bring_to_front()
        await list_page.wait_for_timeout(2000)

        print("Coletando todos os pagadores cadastrados...")
        rows_data = await collect_all_rows(list_page)
        print(f"Total de pagadores lidos no Bradesco: {len(rows_data)}")

        # Match by CPF/CNPJ digits and update the clients table.
        conn = sqlite3.connect("database.db")
        cur = conn.cursor()
        matched = []
        unmatched_clients = []
        for client in clients:
            client_digits = digits_only(client["cnpj_cpf"])
            found = None
            for row in rows_data:
                if digits_only(row["cpf_cnpj"]) == client_digits and client_digits:
                    found = row
                    break
            if found:
                cep = f"{found['cep1']}-{found['cep2']}" if found["cep1"] and found["cep2"] else ""
                cur.execute(
                    "UPDATE clients SET cep = ?, endereco = ? WHERE id = ?",
                    (cep, found["endereco"], client["id"]),
                )
                matched.append((client["name"], cep, found["endereco"]))
            else:
                unmatched_clients.append(client["name"])
        conn.commit()
        conn.close()

        print("\n=== RESULTADO ===")
        print(f"Atualizados ({len(matched)}):")
        for name, cep, endereco in matched:
            print(f"  {name}: CEP {cep} | {endereco}")
        print(f"\nNão encontrados no Bradesco ({len(unmatched_clients)}):")
        for name in unmatched_clients:
            print(f"  {name}")

        await page.wait_for_timeout(5000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
