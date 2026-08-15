import asyncio
import os
from database import get_db_connection
from boleto_automator import (
    BRADESCO_LOGIN_URL, wait_for_bradesco_logged_in, click_element,
    get_central_frame, remove_overlays,
)
from playwright.async_api import async_playwright


async def main():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM system_config")
    config = {row["key"]: row["value"] for row in cursor.fetchall()}
    conn.close()
    bradesco_user = config.get("bradesco_user", "LCSR00145")
    bradesco_password = config.get("bradesco_password", "")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=["--start-maximized"])
        context = await browser.new_context(viewport=None)
        page = await context.new_page()
        page.on("console", lambda msg: print(f"[CONSOLE] {msg.type}: {msg.text}"))

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
        html = await frame.content()
        with open("C:/Projetos/campinas-nfse-automator/frame_dom_cobranca_home.html", "w", encoding="utf-8") as f:
            f.write(html)
        await page.screenshot(path="C:/Projetos/campinas-nfse-automator/screenshots/cobranca_home.png", full_page=True)
        print("DOM e screenshot da home de Cobrança salvos.")

        print("Clicando em '2ª via de boleto'...")
        await click_element(frame, "xpath=//a[contains(@title, 'via de boleto')]")
        await page.wait_for_timeout(4000)

        frame2 = await get_central_frame(page)
        try:
            html2 = await frame2.content()
        except Exception:
            html2 = await page.content()
        with open("C:/Projetos/campinas-nfse-automator/frame_dom_2via.html", "w", encoding="utf-8") as f:
            f.write(html2)
        await page.screenshot(path="C:/Projetos/campinas-nfse-automator/screenshots/2via_boleto.png", full_page=True)
        print("DOM e screenshot da tela de 2ª via salvos.")

        print("Filtrando pelo Nosso Número do Alisson (62270000002) e buscando...")
        nosso_numero_field = frame2.locator("input[name='relatorioTituloPendente.nossoNumeroPesquisa']").first
        await nosso_numero_field.fill("62270000002")
        radio = frame2.locator("input[name='relatorioTituloPendente.cdFaixaVencimento'][value='2']").first
        await radio.check()
        await page.wait_for_timeout(500)
        buscar_btn = frame2.locator("input.bt_buscar").first
        await buscar_btn.click()
        await page.wait_for_timeout(4000)

        frame3 = await get_central_frame(page)
        try:
            html3 = await frame3.content()
        except Exception:
            html3 = await page.content()
        with open("C:/Projetos/campinas-nfse-automator/frame_dom_2via_resultado.html", "w", encoding="utf-8") as f:
            f.write(html3)
        await page.screenshot(path="C:/Projetos/campinas-nfse-automator/screenshots/2via_resultado.png", full_page=True)
        print("DOM e screenshot do resultado filtrado salvos.")

        print("Clicando em 'Mais detalhes' da linha do Alisson...")
        detalhe_btn = frame3.locator("input[name='btnDetalhe'][value='62270000002']").first
        await detalhe_btn.click()
        await page.wait_for_timeout(4000)

        frame4 = await get_central_frame(page)
        print("Clicando em 'Acessar 2ª via de boleto' (o boleto de verdade, com código de barras)...")
        acessar_btn = frame4.locator("xpath=//*[@title='Acessar 2ª via de boleto']").first

        popup = None
        try:
            async with context.expect_page(timeout=8000) as page_info:
                await acessar_btn.click()
            popup = await page_info.value
        except Exception:
            if len(context.pages) > 1:
                popup = context.pages[-1]

        target_page = popup if popup else page
        await target_page.bring_to_front()
        await target_page.wait_for_timeout(3000)

        await target_page.screenshot(path="C:/Projetos/campinas-nfse-automator/screenshots/boleto_real.png", full_page=True)

        print(f"target_page.url = {target_page.url}")
        print(f"context.pages = {[p.url for p in context.pages]}")
        print(f"Procurando 'Salvar como arquivo' em {len(target_page.frames)} frame(s) da página...")
        for f in target_page.frames:
            print(f"  frame url: {f.url}")

        salvar_ctx = None
        salvar_el = None
        for f in target_page.frames:
            try:
                loc = f.get_by_text("Salvar como arquivo", exact=False).first
                if await loc.count() > 0:
                    salvar_ctx = f
                    salvar_el = loc
                    print(f"Achado (get_by_text) no frame: {f.url}")
                    break
            except Exception as e:
                print(f"  erro buscando em frame {f.url}: {e}")
                continue

        if salvar_ctx is None:
            # Last resort: dump the raw HTML so we can see exactly what's there.
            try:
                raw_html = await target_page.content()
                with open("C:/Projetos/campinas-nfse-automator/page_dom_boleto_real_raw.html", "w", encoding="utf-8") as f:
                    f.write(raw_html)
                print("DOM bruto salvo em page_dom_boleto_real_raw.html")
            except Exception as e:
                print(f"Erro ao salvar DOM bruto: {e}")
            print("Não encontrei 'Salvar como arquivo' em nenhum frame. Abortando download.")
            await page.wait_for_timeout(15000)
            await browser.close()
            return

        os.makedirs("C:/Projetos/campinas-nfse-automator/invoices/07-2026", exist_ok=True)
        pdf_path = "C:/Projetos/campinas-nfse-automator/invoices/07-2026/Boleto_Alisson_Araujo_3018_15-08-2026.pdf"

        popup2 = None
        try:
            async with context.expect_page(timeout=8000) as page_info2:
                await salvar_el.click()
            popup2 = await page_info2.value
        except Exception:
            if len(context.pages) > 1:
                popup2 = context.pages[-1]

        target_page2 = popup2 if popup2 else target_page
        await target_page2.bring_to_front()
        await target_page2.wait_for_timeout(2000)
        await target_page2.screenshot(path="C:/Projetos/campinas-nfse-automator/screenshots/boleto_real_apos_salvar.png", full_page=True)

        print(f"target_page2.url = {target_page2.url}")
        print(f"Procurando opção 'pdf' em {len(target_page2.frames)} frame(s)...")
        for f in target_page2.frames:
            print(f"  frame url: {f.url}")

        pdf_ctx = None
        pdf_el = None
        for f in target_page2.frames:
            try:
                loc = f.get_by_text("pdf", exact=False).first
                if await loc.count() > 0:
                    pdf_ctx = f
                    pdf_el = loc
                    print(f"Opção pdf achada no frame: {f.url}")
                    break
            except Exception as e:
                print(f"  erro buscando pdf em frame {f.url}: {e}")
                continue

        if pdf_ctx is None:
            try:
                raw_html2 = await target_page2.content()
                with open("C:/Projetos/campinas-nfse-automator/page_dom_apos_salvar_raw.html", "w", encoding="utf-8") as f:
                    f.write(raw_html2)
                print("DOM bruto (pós-salvar) salvo em page_dom_apos_salvar_raw.html")
            except Exception as e:
                print(f"Erro ao salvar DOM bruto pós-salvar: {e}")

        if pdf_el is not None:
            try:
                async with target_page2.expect_download(timeout=20000) as download_info:
                    await pdf_el.click()
                download = await download_info.value
                await download.save_as(pdf_path)
                print(f"PDF baixado com sucesso em {pdf_path}")
            except Exception as e:
                print(f"Falha ao clicar/baixar pdf: {e}")
        else:
            print("Não encontrei opção 'pdf' após clicar em Salvar como arquivo.")

        await target_page2.screenshot(path="C:/Projetos/campinas-nfse-automator/screenshots/boleto_real_final.png", full_page=True)
        print("Aguardando 15s antes de fechar...")
        await page.wait_for_timeout(15000)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
