import asyncio
from database import get_db_connection
from automator import (
    LOGIN_URL, PRINCIPAL_URL,
    is_recaptcha_solved, click_login_button, wait_for_logged_in,
    save_open_invoice_pdf_from_viewer, is_pdf_file,
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

        # Navigate to Gerenciar NFSe -> Consulta Nota Fiscal
        print("Abrindo Gerenciar NFSe -> Consulta Nota Fiscal...")
        clicked = await page.evaluate("""
            () => {
                const links = Array.from(document.querySelectorAll('a'));
                const link = links.find((a) => (a.innerText || '').includes('Consulta Nota Fiscal'));
                if (!link) return false;
                link.click();
                return true;
            }
        """)
        print(f"Clique em 'Consulta Nota Fiscal': {clicked}")
        await page.wait_for_timeout(3000)

        html = await page.content()
        with open("C:/Projetos/campinas-nfse-automator/consulta_nota_dom.html", "w", encoding="utf-8") as f:
            f.write(html)
        await page.screenshot(path="C:/Projetos/campinas-nfse-automator/screenshots/consulta_nota_tela.png", full_page=True)
        print("DOM e screenshot da tela de consulta salvos.")

        print("Buscando a nota nº 1 pelo campo de busca rápida...")
        quick_search = page.locator('xpath=//input[@placeholder="Informe o nº da nota para visualizar"]').first
        await quick_search.click()
        await quick_search.fill("1")
        search_btn = page.locator('xpath=//input[@placeholder="Informe o nº da nota para visualizar"]/following-sibling::*[self::button or self::a][1]').first
        if await search_btn.count() == 0:
            search_btn = page.locator('xpath=//input[@placeholder="Informe o nº da nota para visualizar"]/..//button | //input[@placeholder="Informe o nº da nota para visualizar"]/..//a').first
        await search_btn.click()
        await page.wait_for_timeout(4000)

        html2 = await page.content()
        with open("C:/Projetos/campinas-nfse-automator/consulta_nota_resultado_dom.html", "w", encoding="utf-8") as f:
            f.write(html2)
        await page.screenshot(path="C:/Projetos/campinas-nfse-automator/screenshots/consulta_nota_resultado.png", full_page=True)
        print("DOM e screenshot do resultado da busca salvos.")
        print(f"page.url = {page.url}")
        print(f"context.pages = {[p.url for p in context.pages]}")

        # If a new tab/popup opened with the PDF viewer, inspect it.
        target = page
        if len(context.pages) > 1:
            target = context.pages[-1]
            await target.bring_to_front()
            await target.wait_for_timeout(2000)

        viewer_info = await target.evaluate("""
            () => {
                const seen = new Set();
                const out = [];
                function allDocuments(doc) {
                    const docs = [doc];
                    for (const frame of doc.querySelectorAll('iframe')) {
                        try { if (frame.contentDocument) docs.push(...allDocuments(frame.contentDocument)); } catch (e) {}
                    }
                    return docs;
                }
                for (const doc of allDocuments(document)) {
                    for (const el of doc.querySelectorAll('embed,iframe,object')) {
                        out.push({tag: el.tagName, id: el.id || '', src: el.src || el.getAttribute('data') || ''});
                    }
                    for (const el of doc.querySelectorAll('#download, #secondaryDownload, [id*="download" i], [title*="download" i], [aria-label*="download" i]')) {
                        out.push({tag: el.tagName, id: el.id || '', title: el.title || '', text: (el.innerText||'').slice(0,40)});
                    }
                }
                return out;
            }
        """)
        print("Elementos relevantes do visualizador:")
        for item in viewer_info:
            print(f"  {item}")

        await target.screenshot(path="C:/Projetos/campinas-nfse-automator/screenshots/consulta_nota_viewer.png", full_page=True)

        print("Testando save_open_invoice_pdf_from_viewer (fix)...")
        test_pdf_path = "C:/Projetos/campinas-nfse-automator/invoices/09-2026/TESTE_download_fix.pdf"
        import os
        os.makedirs(os.path.dirname(test_pdf_path), exist_ok=True)
        try:
            ok = await save_open_invoice_pdf_from_viewer(target, context, test_pdf_path)
            print(f"save_open_invoice_pdf_from_viewer retornou: {ok}")
            print(f"Arquivo existe: {os.path.exists(test_pdf_path)}")
            if os.path.exists(test_pdf_path):
                print(f"Tamanho: {os.path.getsize(test_pdf_path)} bytes")
                print(f"is_pdf_file: {is_pdf_file(test_pdf_path)}")
        except Exception as e:
            print(f"ERRO ao testar save_open_invoice_pdf_from_viewer: {e}")

        print("Mantendo o navegador aberto por 30s para inspeção/manual, se precisar...")
        await page.wait_for_timeout(30000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
