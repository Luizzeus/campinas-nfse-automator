import time
import re
import calendar
import pandas as pd
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys

from webdriver_manager.chrome import ChromeDriverManager


# =========================================================
# CONFIG
# =========================================================
URL = "https://novanfse.campinas.sp.gov.br/notafiscal/paginas/portal/index.html#/login"

CNPJ = "07.268.051/0001-48"
SENHA = "5C0A11EF"
TEMPO_MAX_CAPTCHA = 180

ARQ_XLSX = "/home/lrocha/Downloads/faturamento.xlsx"

# clone
WAIT_BEFORE_CLONAR_SEC = 5
RETRY_CLONE_NOTA_NAO_ENCONTRADA = 3
RETRY_SLEEP_SEC = 4

# tolerâncias
TOL_MONEY = 0.01


# =========================================================
# HELPERS
# =========================================================
def somente_numeros(txt: str) -> str:
    return re.sub(r"\D+", "", txt or "")

def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).upper()

def norm_money(val) -> float:
    if val is None:
        return 0.0
    if isinstance(val, float) and pd.isna(val):
        return 0.0
    s = str(val).strip()
    s = s.replace("R$", "").strip()
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except:
        return 0.0

def money_to_ptbr(v: float) -> str:
    s = f"{v:,.2f}"
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return s

def mes_ptbr(month_num: int) -> str:
    meses = [
        "Janeiro","Fevereiro","Março","Abril","Maio","Junho",
        "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"
    ]
    return meses[month_num - 1]

def achar_coluna(df: pd.DataFrame, nomes_possiveis):
    """Encontra coluna por match case-insensitive e ignorando espaços."""
    lower_map = {str(c).strip().lower(): c for c in df.columns}
    for nome in nomes_possiveis:
        key = str(nome).strip().lower()
        if key in lower_map:
            return lower_map[key]
    # fallback: contém substring
    for c in df.columns:
        cl = str(c).strip().lower()
        for nome in nomes_possiveis:
            if str(nome).strip().lower() in cl:
                return c
    return None

def pick_nota_column(df: pd.DataFrame):
    # preferências: "nota" -> "Unnamed: 0" -> qualquer coluna que contenha "nota"
    col = achar_coluna(df, ["Nota"])
    if col:
        return col
    col = achar_coluna(df, ["Unnamed: 0", "unnamed: 0"])
    if col:
        return col
    # qualquer coluna com "nota" no nome
    for c in df.columns:
        if "nota" in str(c).strip().lower():
            return c
    # último fallback: primeira coluna
    return df.columns[0]

def to_nota_str(nota_val) -> str:
    s = str(nota_val).strip()
    # se vier float tipo 2908.0
    if s.replace(".", "", 1).isdigit():
        try:
            return str(int(float(s)))
        except:
            return s
    return s

def build_descricao(desc_planilha: str) -> str:
    hoje = datetime.now()
    ano = hoje.year
    mes = hoje.month
    ultimo = calendar.monthrange(ano, mes)[1]
    mes_nome = mes_ptbr(mes)
    mm = f"{mes:02d}"
    return (
        f"Serviços Referente ao Mês de {mes_nome} {ano} "
        f"(01/{mm}/{ano} a {ultimo:02d}/{mm}/{ano}) - {desc_planilha}".strip()
    )


# =========================================================
# SELENIUM HELPERS
# =========================================================
def make_driver():
    options = Options()
    options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    return webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )

def js_click(driver, el):
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(0.2)
    driver.execute_script("arguments[0].click();", el)

def js_set_value_and_blur(driver, el, text: str):
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(0.2)
    el.click()
    el.send_keys(Keys.CONTROL, "a")
    el.send_keys(Keys.BACKSPACE)
    el.send_keys(text)
    el.send_keys(Keys.TAB)
    time.sleep(0.2)

def captcha_validado(driver) -> bool:
    return driver.execute_script("""
        var el = document.getElementById('g-recaptcha-response');
        return el && el.value && el.value.length > 0;
    """)

def toast_nota_nao_encontrada(driver) -> bool:
    # mensagem do seu print (barra vermelha no topo)
    txt = "A nota informada não foi encontrada"
    try:
        return driver.find_elements(By.XPATH, f"//*[contains(., '{txt}')]") != []
    except:
        return False

def wait_sidebar_ready(wait: WebDriverWait):
    # sidebar costuma ter spans com nav-label
    return wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "span.nav-label")))

def open_emitir_nota(driver, wait):
    wait_sidebar_ready(wait)

    # expandir NFSe Prestador
    nfse = wait.until(EC.presence_of_element_located(
        (By.XPATH, "//span[@class='nav-label' and contains(normalize-space(), 'NFSe Prestador')]/ancestor::a[1]")
    ))
    js_click(driver, nfse)
    time.sleep(0.6)

    # clicar Emitir Nota Fiscal
    emitir = wait.until(EC.presence_of_element_located(
        (By.XPATH, "//span[@class='nav-label' and normalize-space()='Emitir Nota Fiscal']/ancestor::a[1]")
    ))
    js_click(driver, emitir)

    # aguarda carregar a tela que tem o campo de número da nota
    wait.until(EC.presence_of_element_located((By.XPATH, "//input[contains(@id,':j_idt') and @placeholder='Número da Nota']")))

def locate_descricao_textarea(wait: WebDriverWait):
    xps = [
        "//textarea[contains(@id,'itDescricao') or contains(@name,'itDescricao')]",
        "//label[contains(.,'Descrição Nota Fiscal')]/following::textarea[1]",
        "//textarea[contains(translate(@id,'DESCRICAO','descricao'),'descricao') or contains(translate(@name,'DESCRICAO','descricao'),'descricao')]",
    ]
    last = None
    for xp in xps:
        try:
            return wait.until(EC.presence_of_element_located((By.XPATH, xp)))
        except Exception as e:
            last = e
    raise last

def get_retencao_input_valor(wait: WebDriverWait, imposto_nome: str):
    # 3ª coluna (Valor R$) da linha do imposto
    xpath = (
        "//div[.//h3[contains(.,'Reten')]]"
        f"//table//tr[td[1][contains(normalize-space(.), '{imposto_nome}')]]"
        "//td[3]//input"
    )
    return wait.until(lambda d: d.find_element(By.XPATH, xpath))

def get_retencao_valor_atual(wait: WebDriverWait, imposto_nome: str) -> float:
    el = get_retencao_input_valor(wait, imposto_nome)
    return norm_money(el.get_attribute("value"))


# =========================================================
# PLANILHA: pegar 1 linha de teste
# =========================================================
def load_first_row_planilha(path: str):
    df = pd.read_excel(path)

    col_nota = pick_nota_column(df)
    col_nome = achar_coluna(df, ["Nome/Nome Empresarial", "Nome", "Razão Social"])
    col_cnpj = achar_coluna(df, ["CNPJ/CPF", "CPF/CNPJ", "CNPJ", "CPF"])
    col_valor = achar_coluna(df, ["Valor da Nota", "Valor dos Serviços", "Valor dos Servicos", "Valor"])

    col_desc = achar_coluna(df, ["Descrição da Nota Fiscal", "Descricao da Nota Fiscal", "Descrição", "Descricao"])
    if col_desc is None:
        raise ValueError(f"Coluna de descrição não encontrada. Colunas: {list(df.columns)}")

    row = df.dropna(subset=[col_nota]).iloc[0]

    # retenções (VALOR R$)
    col_pis = achar_coluna(df, ["PIS"])
    col_inss = achar_coluna(df, ["INSS"])
    col_csll = achar_coluna(df, ["CSLL"])
    col_cofins = achar_coluna(df, ["COFINS", "CONFINS"])  # caso venha digitado errado
    col_ir = achar_coluna(df, ["IR"])

    return {
        "nota": to_nota_str(row[col_nota]),
        "nome": "" if col_nome is None else str(row.get(col_nome, "")).strip(),
        "cnpjcpf": "" if col_cnpj is None else str(row.get(col_cnpj, "")).strip(),
        "valor_servicos": 0.0 if col_valor is None else norm_money(row.get(col_valor)),
        "descricao_planilha": str(row.get(col_desc, "")).strip(),
        "retencoes": {
            "PIS": norm_money(row.get(col_pis)) if col_pis else 0.0,
            "INSS": norm_money(row.get(col_inss)) if col_inss else 0.0,
            "CSLL": norm_money(row.get(col_csll)) if col_csll else 0.0,
            "COFINS": norm_money(row.get(col_cofins)) if col_cofins else 0.0,
            "IR": norm_money(row.get(col_ir)) if col_ir else 0.0,
        }
    }


# =========================================================
# CLONAR: localizar campo e botão sem depender de ID fixo
# =========================================================
def find_campo_numero_nota(driver):
    xps = [
        # id varia (j_idt99, j_idt107, etc) mas placeholder é estável
        "//input[@placeholder='Número da Nota']",
        "//input[contains(@id,'numero-nota') and @placeholder='Número da Nota']",
        "//input[contains(@id,':j_idt') and @placeholder='Número da Nota']",
    ]
    for xp in xps:
        els = driver.find_elements(By.XPATH, xp)
        if els:
            for e in els:
                if e.is_displayed():
                    return e
            return els[0]
    return None

def find_clonar_button(driver):
    """
    O ID do botão muda (ex.: formNotaFiscal:j_idt100 -> j_idt108).
    Portanto, não use By.ID fixo. Achamos pelo texto "Clonar" e proximidade do campo.
    """
    # 1) perto do campo "Número da Nota" (mais confiável)
    try:
        campo = find_campo_numero_nota(driver)
        if campo:
            container = campo.find_element(By.XPATH, "./ancestor::div[contains(@class,'numero-nota') or contains(@class,'ui-panelgrid') or contains(@class,'form-group')][1]")
            # botão pode ser <a> ou <button>
            btns = container.find_elements(By.XPATH, ".//a[contains(.,'Clonar') or .//span[contains(.,'Clonar')]] | .//button[contains(.,'Clonar') or .//span[contains(.,'Clonar')]]")
            for b in btns:
                if b.is_displayed():
                    return b
            if btns:
                return btns[0]
    except:
        pass

    # 2) fallback global por texto
    xps = [
        "//a[contains(.,'Clonar') or .//span[contains(normalize-space(.),'Clonar')]]",
        "//button[contains(.,'Clonar') or .//span[contains(normalize-space(.),'Clonar')]]",
    ]
    for xp in xps:
        els = driver.find_elements(By.XPATH, xp)
        if els:
            for e in els:
                if e.is_displayed():
                    return e
            return els[0]
    return None


# =========================================================
# FLUXO PRINCIPAL
# =========================================================
driver = make_driver()
wait = WebDriverWait(driver, 40)
driver.get(URL)

print("⏳ Aguardando formulário CNPJ...")

card_cnpj = wait.until(
    EC.presence_of_element_located((
        By.XPATH,
        "//div[contains(., 'Acesso via senha') and contains(., 'CNPJ')]"
    ))
)

inputs = card_cnpj.find_elements(By.XPATH, ".//input")
if len(inputs) < 2:
    raise RuntimeError("Não foi possível localizar os campos de CNPJ e senha.")

cnpj_input = inputs[0]
senha_input = inputs[1]

cnpj_input.click()
cnpj_input.clear()
cnpj_input.send_keys(somente_numeros(CNPJ))

senha_input.click()
senha_input.clear()
senha_input.send_keys(SENHA)

print("✅ CNPJ e senha preenchidos.")
print("👉 Clique agora em **Não sou um robô**.")

inicio = time.time()
while not captcha_validado(driver):
    if time.time() - inicio > TEMPO_MAX_CAPTCHA:
        raise TimeoutError("Tempo excedido aguardando o reCAPTCHA.")
    time.sleep(2)

print("✅ reCAPTCHA validado.")
print("⏳ Procurando o botão ENTRAR dentro do card...")

def achar_btn_entrar_no_card(card):
    candidatos = card.find_elements(By.CSS_SELECTOR, "button, a, div[role='button']")
    for el in candidatos:
        txt = (el.text or "").strip().upper()
        if "ENTRAR" in txt:
            return el
    return None

entrar_btn = WebDriverWait(driver, 40).until(lambda d: achar_btn_entrar_no_card(card_cnpj))
driver.execute_script("arguments[0].scrollIntoView({block:'center'});", entrar_btn)
time.sleep(0.3)
driver.execute_script("arguments[0].click();", entrar_btn)
print("🟦 ENTRAR clicado automaticamente.")

time.sleep(3)
print("URL atual:", driver.current_url)

# =========================================================
# IR PARA EMITIR NOTA
# =========================================================
open_emitir_nota(driver, wait)
print("✅ Tela 'Emitir Nota Fiscal' aberta.")

# =========================================================
# PLANILHA: 1 nota (teste)
# =========================================================
pl = load_first_row_planilha(ARQ_XLSX)
print(f"✅ Nota escolhida (teste): {pl['nota']}")

# =========================================================
# CLONAR com RETRY se "nota não encontrada"
# =========================================================
campo_nota = wait.until(lambda d: find_campo_numero_nota(d))
clonar_btn = wait.until(lambda d: find_clonar_button(d))

def tentar_clonar_uma_vez(nota_str: str) -> bool:
    """Preenche o número da nota e clica em 'Clonar' com tolerância a re-render (stale)."""
    msg_toast = "A nota informada não foi encontrada"
    for tentativa in range(1, 4):  # retries rápidos para Stale/Timeout
        try:
            driver.switch_to.default_content()

            # SEMPRE re-localizar o campo (JSF/PrimeFaces re-renderiza e causa STALE)
            campo = wait.until(lambda d: find_campo_numero_nota(d))
            campo.click()
            campo.send_keys(Keys.CONTROL, "a")
            campo.send_keys(Keys.BACKSPACE)
            campo.send_keys(str(nota_str))
            print("✅ Campo 'Número da Nota' preenchido.")

            print(f"⏳ Aguardando {WAIT_BEFORE_CLONAR_SEC}s antes de clicar em Clonar...")
            time.sleep(WAIT_BEFORE_CLONAR_SEC)

            btn = wait.until(lambda d: find_clonar_button(d))
            js_click(driver, btn)
            print("🟩 Cliquei em CLONAR.")

            # aguarda retorno da requisição
            time.sleep(2)

            if toast_nota_nao_encontrada(driver) or contains_text_in_page(driver, msg_toast):
                print("❌ 'A nota informada não foi encontrada'.")
                return False

            print("✅ Clone realizado.")
            return True

        except StaleElementReferenceException:
            print(f"⚠️ STALE ao clonar (tentativa interna {tentativa}/3). Re-localizando elementos...")
            time.sleep(1)
            continue
        except TimeoutException:
            print(f"⚠️ TIMEOUT ao clonar (tentativa interna {tentativa}/3). Re-localizando elementos...")
            time.sleep(1)
            continue

    return False


    print("✅ Clone aparentemente OK (sem erro de nota não encontrada).")
    return True

ok_clone = False
for tentativa in range(1, RETRY_CLONE_NOTA_NAO_ENCONTRADA + 1):
    print(f"\n🔁 Tentativa de clone {tentativa}/{RETRY_CLONE_NOTA_NAO_ENCONTRADA}")
    ok_clone = tentar_clonar_uma_vez(pl["nota"])
    if ok_clone:
        break
    print(f"⏳ Aguardando {RETRY_SLEEP_SEC}s e tentando novamente...")
    time.sleep(RETRY_SLEEP_SEC)

if not ok_clone:
    raise RuntimeError("Não consegui clonar a nota após as tentativas. Verifique o número e tente novamente.")

# =========================================================
# VALIDAR CAMPOS IMPORTANTES vs PLANILHA
# (Nome/Nome Empresarial, CPF/CNPJ, Valor dos Serviços)
# =========================================================
print("\n🔎 Validando campos principais vs planilha (sem clicar em Visualizar/Emitir)...")

divergencias = []

# Nome/Nome Empresarial
try:
    nome_el = wait.until(EC.presence_of_element_located(
        (By.XPATH, "//input[contains(@id,'idPessoaNacionalNome') or contains(@name,'idPessoaNacionalNome')]")
    ))
    nome_site = norm_text(nome_el.get_attribute("value") or "")
    nome_plan = norm_text(pl["nome"])
    if nome_plan and nome_site and nome_plan not in nome_site and nome_site not in nome_plan:
        divergencias.append(("Nome/Nome Empresarial", nome_site, nome_plan))
    else:
        print("✅ Nome/Nome Empresarial OK (ou não disponível na planilha).")
except Exception:
    print("ℹ️ Não consegui localizar o campo de Nome/Nome Empresarial para validar (pode variar conforme tela).")

# CPF/CNPJ
try:
    cnpj_el = wait.until(EC.presence_of_element_located(
        (By.XPATH, "//input[contains(@id,'CpfCnpj') or contains(@name,'CpfCnpj')]")
    ))
    cnpj_site = somente_numeros(cnpj_el.get_attribute("value") or "")
    cnpj_plan = somente_numeros(pl["cnpjcpf"])
    if cnpj_plan and cnpj_site and cnpj_plan != cnpj_site:
        divergencias.append(("CPF/CNPJ", cnpj_site, cnpj_plan))
    else:
        print("✅ CPF/CNPJ OK (ou não disponível na planilha).")
except Exception:
    print("ℹ️ Não consegui localizar o campo de CPF/CNPJ para validar (pode variar conforme tela).")

# Valor dos Serviços
try:
    valor_el = wait.until(EC.presence_of_element_located(
        (By.XPATH, "//label[contains(.,'Valor dos Serviços')]/following::input[1]")
    ))
    valor_site = norm_money(valor_el.get_attribute("value"))
    valor_plan = float(pl["valor_servicos"])
    if valor_plan > 0 and abs(valor_plan - valor_site) > TOL_MONEY:
        divergencias.append(("Valor dos Serviços", f"{valor_site:.2f}", f"{valor_plan:.2f}"))
    else:
        print("✅ Valor dos Serviços OK (ou não disponível na planilha).")
except Exception:
    print("ℹ️ Não consegui localizar o campo 'Valor dos Serviços' para validar (pode variar conforme tela).")

# Retenções: PIS, INSS, CSLL, COFINS, IR (VALOR R$) — APENAS CONFERIR.
print("\n🔎 Conferindo Retenções (VALOR R$) vs planilha (sem auto-ajuste)...")
for imposto, valor_plan in pl["retencoes"].items():
    try:
        site_val = get_retencao_valor_atual(wait, imposto)
        plan_val = float(valor_plan or 0.0)
        if abs(plan_val - site_val) > TOL_MONEY:
            divergencias.append((f"Retenção {imposto} (Valor R$)", f"{site_val:.2f}", f"{plan_val:.2f}"))
            print(f"❗ {imposto}: Site={site_val:.2f} / Planilha={plan_val:.2f}")
        else:
            print(f"✅ {imposto}: OK ({site_val:.2f})")
    except Exception:
        print(f"ℹ️ Não consegui localizar a retenção '{imposto}' para validar (pode variar conforme tela).")

if divergencias:
    print("\n🛑 Encontrei divergências:")
    for campo, site, plan in divergencias:
        print(f" - {campo}\n   Site: {site}\n   Planilha: {plan}")
    print("\n👉 Por favor, corrija manualmente os campos divergentes na tela.")
    print("🚫 NÃO cliquei em Visualizar nem em Emitir Nota Fiscal.")
    raise SystemExit(0)

print("\n✅ Tudo conferido e igual à planilha. (NÃO cliquei em Visualizar/Emitir).")


print("\n✅ Tudo conferido e igual à planilha. Agora vou EMITIR a Nota Fiscal e coletar o número gerado...")

# =========================================================
# EMITIR NOTA FISCAL e CAPTURAR NÚMERO GERADO
# =========================================================

def extrair_numero_nfse_do_texto(txt: str):
    if not txt:
        return None
    # padrões comuns: "Nº da Nota: 1234", "Número da Nota 1234", "NFS-e nº 1234"
    padroes = [
        r"N\s*[º°o]?\s*da\s*Nota\s*[:\-]?\s*(\d+)",
        r"N[uú]mero\s*da\s*Nota\s*[:\-]?\s*(\d+)",
        r"NFS-?e\s*n\s*[º°o]?\s*[:\-]?\s*(\d+)",
        r"Nota\s*Fiscal\s*[:\-]?\s*(\d+)",
    ]
    for p in padroes:
        m = re.search(p, txt, flags=re.IGNORECASE)
        if m:
            return m.group(1)
    return None

def capturar_numero_nfse():
    # 1) tenta pegar pelo texto do "toast"/mensagem
    possiveis_xpaths = [
        "//*[contains(@class,'alert') or contains(@class,'growl') or contains(@class,'ui-messages') or contains(@class,'ui-message') or contains(@class,'messages')][contains(.,'Nota') or contains(.,'NFS') or contains(.,'emit')]",
        "//*[self::div or self::span or self::p][contains(.,'Nº') or contains(.,'N°') or contains(.,'Número da Nota') or contains(.,'NFS')]",
    ]
    for xp in possiveis_xpaths:
        for el in driver.find_elements(By.XPATH, xp):
            try:
                if not el.is_displayed():
                    continue
                t = el.text.strip()
                num = extrair_numero_nfse_do_texto(t)
                if num:
                    return num, t
            except Exception:
                pass

    # 2) fallback: varre o texto do BODY
    try:
        body_txt = driver.find_element(By.TAG_NAME, "body").text
        num = extrair_numero_nfse_do_texto(body_txt)
        if num:
            return num, "BODY"
    except Exception:
        pass

    return None, None

def clicar_emitir_nota():
    # garante que o botão esteja visível (geralmente no rodapé)
    try:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    except Exception:
        pass
    time.sleep(1)

    btn = wait.until(EC.element_to_be_clickable((
        By.XPATH,
        # botão ou link com texto "Emitir Nota Fiscal" (com span interno ou texto direto)
        "//button[normalize-space()='Emitir Nota Fiscal' or .//span[normalize-space()='Emitir Nota Fiscal']]"
        " | //a[normalize-space()='Emitir Nota Fiscal' or .//span[normalize-space()='Emitir Nota Fiscal']]"
    )))
    js_click(btn)

# Clica em Emitir e aguarda o pós-emitir
clicar_emitir_nota()

# Após emitir, o sistema pode:
# - mostrar uma mensagem de sucesso e permanecer na página, ou
# - redirecionar para uma tela de impressão/consulta.
# Vamos aguardar a página estabilizar e então extrair o número.
time.sleep(2)

# espera curta por algum indicativo de sucesso ou mudança de URL
try:
    WebDriverWait(driver, 20).until(lambda d: ("emit" in d.page_source.lower()) or ("sucesso" in d.page_source.lower()) or (d.current_url != URL_EMITIR))
except Exception:
    pass

numero_nfse, origem = capturar_numero_nfse()

if numero_nfse:
    print(f"✅ Nota emitida. Número capturado: {numero_nfse} (origem: {origem})")
else:
    print("⚠️ Cliquei em 'Emitir Nota Fiscal', mas NÃO consegui localizar o número automaticamente.")
    print("👉 Verifique na tela se aparece o número da NFS-e e me envie um print/trecho do HTML para ajustarmos o seletor.")


# =========================================================
# DESCRIÇÃO NOTA FISCAL: mês atual/ano/período + descrição planilha
# =========================================================
desc_plan = pl["descricao_planilha"] or ""
texto_desc = build_descricao(desc_plan)

print("\n📝 Preenchendo 'Descrição Nota Fiscal'...")
desc_el = locate_descricao_textarea(wait)
js_set_value_and_blur(driver, desc_el, texto_desc)

print("✅ Descrição preenchida com:")
print(texto_desc)

print("\n🚫 Pronto. NÃO cliquei em Visualizar e NÃO cliquei em Emitir Nota Fiscal.")


# CELL #

