import asyncio
from database import get_db_connection
from automator import (
    LOGIN_URL, PRINCIPAL_URL,
    is_recaptcha_solved, click_login_button, wait_for_logged_in, open_emission_page,
)
from playwright.async_api import async_playwright
import re


async def main():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM system_config")
    config = {row["key"]: row["value"] for row in cursor.fetchall()}
    conn.close()
    portal_cnpj = config.get("portal_cnpj", "07.268.051/0001-48")
    portal_password = config.get("portal_password", "5C0A11EF")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=["--start-maximized"])
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()

        print("Acessando portal da NFS-e Campinas...")
        await page.goto(LOGIN_URL)
        await page.wait_for_timeout(3000)

        if "#/login" in page.url:
            await page.fill('input[name="cpfCnpj"]', re.sub(r"\D+", "", portal_cnpj))
            await page.fill('input[name="senha"]', portal_password)
            print("Resolva o reCAPTCHA na janela do navegador...")

            login_success = False
            clicked_login = False
            for _ in range(180):
                await page.wait_for_timeout(1000)
                if await wait_for_logged_in(page, timeout_ms=750):
                    login_success = True
                    break
                if not clicked_login and await is_recaptcha_solved(page):
                    print("reCAPTCHA resolvido. Clicando em ENTRAR...")
                    if await click_login_button(page):
                        clicked_login = True
            if not login_success:
                print("Timeout no login.")
                await browser.close()
                return
        print("Login OK.")
        await page.wait_for_timeout(2000)

        if not await wait_for_logged_in(page, timeout_ms=20000):
            await page.goto(PRINCIPAL_URL)
            await wait_for_logged_in(page, timeout_ms=30000)

        print("Abrindo tela de Emissão de Nota Fiscal...")
        await open_emission_page(page, timeout_ms=30000)
        await page.wait_for_timeout(3000)

        html = await page.content()
        with open("C:/Projetos/campinas-nfse-automator/emissao_dom_novo.html", "w", encoding="utf-8") as f:
            f.write(html)
        await page.screenshot(path="C:/Projetos/campinas-nfse-automator/screenshots/emissao_tela_nova_full.png", full_page=True)
        print("DOM completo salvo em emissao_dom_novo.html")
        print("Screenshot full-page salvo em screenshots/emissao_tela_nova_full.png")

        # Look for anything that could be the old "clone" button, anywhere on the page
        clone_candidates = await page.evaluate("""
            () => Array.from(document.querySelectorAll('a,button'))
                .map((el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim())
                .filter((t) => /clon/i.test(t))
        """)
        print(f"Elementos com texto contendo 'clon': {clone_candidates}")

        # Dump every visible input's placeholder/id/name/label-ish context
        input_info = await page.evaluate("""
            () => Array.from(document.querySelectorAll('input'))
                .filter((el) => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                })
                .map((el) => ({
                    placeholder: el.placeholder || '',
                    id: el.id || '',
                    name: el.name || '',
                    type: el.type || ''
                }))
        """)
        print("Inputs visíveis na tela:")
        for info in input_info:
            print(f"  {info}")

        # Try the new 'lookup' code field for Atividade: type the code, blur, see what happens.
        print("Testando o campo de código da Atividade (digitar 620400001 e sair do campo)...")
        code_field = page.locator('xpath=//input[contains(@id, "idAtividadeLivre:codeLookup")]').first
        await code_field.click()
        await code_field.fill("620400001")
        await code_field.press("Tab")
        await page.wait_for_timeout(3000)
        desc_field = page.locator('xpath=//input[contains(@id, "idAtividadeLivre:descriptionLookup")]').first
        desc_value = await desc_field.input_value()
        print(f"Valor da descrição após blur: '{desc_value}'")

        html_after_code = await page.content()
        with open("C:/Projetos/campinas-nfse-automator/emissao_dom_apos_codigo.html", "w", encoding="utf-8") as f:
            f.write(html_after_code)
        await page.screenshot(path="C:/Projetos/campinas-nfse-automator/screenshots/emissao_apos_codigo.png", full_page=True)

        # Check for any validation/error message visible on the page now
        error_texts = await page.evaluate("""
            () => Array.from(document.querySelectorAll('.ui-message, .ui-messages-error, .growl, .toast, [class*=error]'))
                .map((el) => (el.innerText || el.textContent || '').trim())
                .filter((t) => t.length > 0)
        """)
        print(f"Mensagens de erro/validação visíveis: {error_texts}")

        # Now open the lookup dialog (magnifying glass) to see the search UI
        print("Abrindo o diálogo de busca (lupa) da Atividade...")
        magnifier = page.locator('xpath=//a[contains(@id, "idAtividadeLivre") and .//span[contains(text(), "🔍")]]').first
        if await magnifier.count() == 0:
            magnifier = page.locator('xpath=//td[input[contains(@id,"idAtividadeLivre:codeLookup")]]/following-sibling::td//a').first
        await magnifier.click()
        await page.wait_for_timeout(2000)

        dialog_html = await page.content()
        with open("C:/Projetos/campinas-nfse-automator/emissao_dom_dialog_atividade.html", "w", encoding="utf-8") as f:
            f.write(dialog_html)
        await page.screenshot(path="C:/Projetos/campinas-nfse-automator/screenshots/emissao_dialog_atividade.png", full_page=True)
        print("DOM e screenshot do diálogo de Atividade salvos.")

        print("Mantendo o navegador aberto por 90s para inspeção manual, se quiser...")
        await page.wait_for_timeout(90000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
