
# =========================================================
# AUTOMAÇÃO NFS-e CAMPINAS (NFSe Prestador -> Emitir Nota Fiscal)
# - Lê 1 linha da planilha (faturamento.xlsx)
# - Clona nota (com retry se "nota não encontrada")
# - Confere valores (site x planilha) e pausa para correção manual se diferente
# - Preenche "Descrição Nota Fiscal" com mês/ano + período + descrição da planilha
# - Emite a nota fiscal e captura o número gerado (log)
#
# Requisitos:
#   pip install selenium pandas openpyxl
# =========================================================

import re
import time
import math
import calendar
import datetime as dt
from dataclasses import dataclass
from typing import Optional, Dict

import pandas as pd

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    StaleElementReferenceException,
    ElementClickInterceptedException,
)

# =========================
# CONFIG
# =========================
ARQ_XLSX = "/home/lrocha/Downloads/faturamento.xlsx"  # <-- ajuste se precisar
SHEET_NAME = None  # ex: "Plan1" ou None para primeira aba
HEADLESS = False
TIMEOUT = 25

RETRY_CLONE_NOTA_NAO_ENCONTRADA = 3
WAIT_BEFORE_CLONAR_SECONDS = 5     # esperar 5s antes de clicar em "Clonar"
RETRY_WAIT_SECONDS = 4             # retry após 4s quando "nota não encontrada"

# =========================
# UTIL: formatação BR
# =========================
def to_str(x) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return ""
    return str(x).strip()

def normalize_doc(doc: str) -> str:
    'Remove tudo que não for dígito.'
    return re.sub(r"\D+", "", to_str(doc))

def fmt_money_br(v) -> str:
    'Formata número para padrão BR: 150,00'
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "0,00"
    try:
        v = float(v)
    except Exception:
        s = to_str(v)
        s = s.replace(".", "").replace(",", ".")
        try:
            v = float(s)
        except Exception:
            return "0,00"
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")

def parse_money_br(s: str) -> float:
    'Converte texto BR (1.234,56) em float.'
    s = to_str(s)
    if not s:
        return 0.0
    s = re.sub(r"[^\d,.-]", "", s)
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0

def month_name_pt(m: int) -> str:
    nomes = [
        "Janeiro","Fevereiro","Março","Abril","Maio","Junho",
        "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"
    ]
    return nomes[m-1]

def periodo_mes_atual(now: dt.date) -> str:
    first = now.replace(day=1)
    last_day = calendar.monthrange(now.year, now.month)[1]
    last = now.replace(day=last_day)
    return f"({first.strftime('%d/%m/%Y')} A {last.strftime('%d/%m/%Y')})"

def build_descricao_mes(plan_desc: str, now: Optional[dt.date]=None) -> str:
    now = now or dt.date.today()
    mes = month_name_pt(now.month)
    periodo = periodo_mes_atual(now)
    prefixo = f"Serviços Referente ao Mês de {mes} {now.year} {periodo}"
    plan_desc = to_str(plan_desc)
    if plan_desc:
        return f"{prefixo} - {plan_desc}"
    return prefixo

# =========================
# DATA MODEL
# =========================
@dataclass
class PlanLinha:
    nota: str
    nome: str
    doc: str
    valor_servicos: str
    pis: str
    inss: str
    csll: str
    cofins: str
    ir: str
    desc_nf: str

# =========================
# PLANILHA
# =========================
def detectar_coluna_nota(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lower = {c: str(c).strip().lower() for c in cols}
    for c in cols:
        if lower[c] == "nota" or "nota" == lower[c].replace(" ", ""):
            return c
    for c in cols:
        if str(c).startswith("Unnamed"):
            return c
    for c in cols:
        if "nota" in lower[c]:
            return c
    return cols[0]

def carregar_primeira_linha_planilha(path: str) -> PlanLinha:
    df = pd.read_excel(path, sheet_name=SHEET_NAME)
    df.columns = [str(c).strip() for c in df.columns]

    col_nota = "Nota" if "Nota" in df.columns else detectar_coluna_nota(df)

    # descrição pode vir com espaço no fim
    col_desc = None
    for c in df.columns:
        if str(c).strip().lower() == "descrição da nota fiscal":
            col_desc = c
            break
    if col_desc is None:
        for c in df.columns:
            if "descrição" in str(c).lower() and "nota" in str(c).lower():
                col_desc = c
                break

    def get_col(cands):
        for cand in cands:
            for c in df.columns:
                if str(c).strip().lower() == cand.lower():
                    return c
        return None

    col_nome = get_col(["Nome/Nome Empresarial"])
    col_doc  = get_col(["CNPJ/CPF", "CPF/CNPJ"])
    col_val  = get_col(["Valor da Nota", "valor da nota", "Valor", "valor"])
    col_pis  = get_col(["PIS"])
    col_inss = get_col(["INSS"])
    col_csll = get_col(["CSLL"])
    col_cof  = get_col(["COFINS", "CONFINS"])
    col_ir   = get_col(["IR"])

    serie = df[col_nota].dropna()
    if serie.empty:
        raise ValueError(f"Nenhum valor encontrado na coluna de Nota ({col_nota}).")
    idx = serie.index[0]
    row = df.loc[idx]

    nota_val = to_str(row[col_nota])
    if nota_val.endswith(".0"):
        nota_val = nota_val[:-2]

    return PlanLinha(
        nota=nota_val,
        nome=to_str(row[col_nome]) if col_nome else "",
        doc=normalize_doc(row[col_doc]) if col_doc else "",
        valor_servicos=fmt_money_br(row[col_val]) if col_val else "0,00",
        pis=fmt_money_br(row[col_pis]) if col_pis else "0,00",
        inss=fmt_money_br(row[col_inss]) if col_inss else "0,00",
        csll=fmt_money_br(row[col_csll]) if col_csll else "0,00",
        cofins=fmt_money_br(row[col_cof]) if col_cof else "0,00",
        ir=fmt_money_br(row[col_ir]) if col_ir else "0,00",
        desc_nf=to_str(row[col_desc]) if col_desc else "",
    )

# =========================
# SELENIUM HELPERS
# =========================
def js_click(driver, el):
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(0.2)
    driver.execute_script("arguments[0].click();", el)

def safe_click(driver, el):
    try:
        el.click()
    except (ElementClickInterceptedException, StaleElementReferenceException):
        js_click(driver, el)

def contains_text_in_page(driver, text: str) -> bool:
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text
        return text.lower() in body_text.lower()
    except Exception:
        return False

def wait_dom_ready(driver, timeout=20):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )

def toast_nota_nao_encontrada(driver) -> bool:
    msgs = ["a nota informada não foi encontrada", "nota informada não foi encontrada"]
    try:
        txt = driver.find_element(By.TAG_NAME, "body").text.lower()
        return any(m in txt for m in msgs)
    except Exception:
        return False

def toast_sucesso_clone(driver) -> bool:
    msgs = ["foi clonada com sucesso", "clonada com sucesso"]
    try:
        txt = driver.find_element(By.TAG_NAME, "body").text.lower()
        return any(m in txt for m in msgs)
    except Exception:
        return False

def toast_erro_generico(driver) -> str:
    try:
        els = driver.find_elements(By.CSS_SELECTOR, ".ui-messages-error-summary, .ui-growl-message p")
        txts = [e.text.strip() for e in els if e.text.strip()]
        return " | ".join(txts)
    except Exception:
        return ""

# =========================
# ENCONTRAR CAMPOS / BOTÕES (robustos)
# =========================
def find_input_numero_nota(driver):
    els = driver.find_elements(By.CSS_SELECTOR, "input[placeholder*='Número da Nota']")
    if els:
        return els[0]
    candidates = driver.find_elements(By.CSS_SELECTOR, "input[id^='formNotaFiscal:'][name^='formNotaFiscal:']")
    for e in candidates:
        try:
            if (e.get_attribute("placeholder") or "").lower().find("número da nota") >= 0:
                return e
        except Exception:
            continue
    return None

def find_btn_clonar(driver):
    xpath = (
        "//*[self::a or self::button]["
        "contains(normalize-space(.),'Clonar') and not(contains(normalize-space(.),'Clonar Esta'))"
        "]"
    )
    els = driver.find_elements(By.XPATH, xpath)
    if els:
        return els[0]
    els = driver.find_elements(By.CSS_SELECTOR, "a.btn-success, button.btn-success")
    for e in els:
        if e.text.strip().lower() == "clonar":
            return e
    return None

def find_btn_emitir(driver):
    xpath = "//*[self::a or self::button][contains(normalize-space(.),'Emitir Nota Fiscal')]"
    els = driver.find_elements(By.XPATH, xpath)
    return els[0] if els else None

def find_valor_servicos_input(driver):
    xpath = "//*[contains(normalize-space(.),'Valor dos Serviços')]/following::input[1]"
    els = driver.find_elements(By.XPATH, xpath)
    return els[0] if els else None

def find_retencao_percent_input(driver, imposto_label: str):
    xpath = (
        f"//table//*[normalize-space(text())='{imposto_label}']/ancestor::tr[1]//input[1]"
    )
    els = driver.find_elements(By.XPATH, xpath)
    return els[0] if els else None

def find_descricao_textarea(driver):
    els = driver.find_elements(By.CSS_SELECTOR, "textarea[id*='itDescricao'], textarea[name*='itDescricao']")
    if els:
        return els[0]
    xpath = "//*[contains(normalize-space(.),'Descrição Nota Fiscal')]/following::textarea[1]"
    els = driver.find_elements(By.XPATH, xpath)
    return els[0] if els else None

def read_field_value_safe(el) -> str:
    if el is None:
        return ""
    try:
        v = el.get_attribute("value")
        if v is not None:
            return v.strip()
    except Exception:
        pass
    try:
        return (el.text or "").strip()
    except Exception:
        return ""

# =========================
# FLUXO: CLONAR COM RETRY
# =========================
def tentar_clonar_uma_vez(driver, nota_str: str) -> bool:
    for tentativa in range(1, 4):
        try:
            inp = WebDriverWait(driver, TIMEOUT).until(lambda d: find_input_numero_nota(d))
            btn = WebDriverWait(driver, TIMEOUT).until(lambda d: find_btn_clonar(d))

            inp.click()
            inp.send_keys(Keys.CONTROL, "a")
            inp.send_keys(Keys.BACKSPACE)
            inp.send_keys(nota_str)

            time.sleep(WAIT_BEFORE_CLONAR_SECONDS)

            safe_click(driver, btn)
            time.sleep(2)

            if toast_nota_nao_encontrada(driver) or contains_text_in_page(driver, "A nota informada não foi encontrada"):
                print("❌ 'A nota informada não foi encontrada'.")
                return False

            if toast_sucesso_clone(driver) or ("emissaoNotaFiscalData" in driver.current_url):
                print("✅ Clone realizado.")
                return True

            print("✅ Clique em Clonar realizado (sem toast detectado).")
            return True

        except StaleElementReferenceException:
            print(f"⚠️ STALE ao clonar (tentativa interna {tentativa}/3). Re-localizando...")
            time.sleep(1)
        except TimeoutException:
            print("❌ Timeout ao localizar input/botão de clonar.")
            return False
        except Exception as e:
            print("❌ Erro ao clonar:", e)
            return False
    return False

def clonar_com_retry(driver, nota_str: str) -> bool:
    for tentativa in range(1, RETRY_CLONE_NOTA_NAO_ENCONTRADA + 1):
        print(f"\n🔁 Tentativa de clone {tentativa}/{RETRY_CLONE_NOTA_NAO_ENCONTRADA} (Nota {nota_str})")
        ok = tentar_clonar_uma_vez(driver, nota_str)
        if ok:
            return True
        print(f"⏳ Aguardando {RETRY_WAIT_SECONDS}s para tentar novamente...")
        time.sleep(RETRY_WAIT_SECONDS)
    return False

# =========================
# VALIDAÇÃO: SITE x PLANILHA
# =========================
def coletar_dados_site(driver) -> Dict[str, str]:
    dados = {}

    cands = driver.find_elements(By.CSS_SELECTOR, "input[data-p-mask*='cpf'], input[name*='cpfCnpj'], input[id*='cpfCnpj']")
    if cands:
        dados["doc"] = normalize_doc(read_field_value_safe(cands[0]))

    cands = driver.find_elements(By.CSS_SELECTOR, "input[name*='nome'], input[id*='nome'], input[placeholder*='Nome']")
    for e in cands:
        lab = (e.get_attribute("data-p-label") or "").lower()
        if "nome" in lab and "empres" in lab:
            dados["nome"] = read_field_value_safe(e)
            break

    v_inp = find_valor_servicos_input(driver)
    if v_inp:
        dados["valor_servicos"] = read_field_value_safe(v_inp)

    for imp in ["PIS", "INSS", "CSLL", "COFINS", "IR"]:
        inp = find_retencao_percent_input(driver, imp)
        if inp:
            dados[imp.lower()] = read_field_value_safe(inp)

    desc = find_descricao_textarea(driver)
    if desc:
        dados["descricao"] = read_field_value_safe(desc)

    return dados

def compara_e_pausa_se_diferente(plan: PlanLinha, site: Dict[str, str]) -> None:
    diffs = []

    def cmp_str(label, a, b, norm=None):
        aa = norm(a) if norm else to_str(a).strip()
        bb = norm(b) if norm else to_str(b).strip()
        if aa and bb and aa != bb:
            diffs.append((label, aa, bb))

    def cmp_money(label, a, b):
        aa = parse_money_br(a)
        bb = parse_money_br(b)
        if abs(aa - bb) > 0.009:
            diffs.append((label, fmt_money_br(aa), fmt_money_br(bb)))

    cmp_str("Nome/Nome Empresarial", plan.nome, site.get("nome", ""), norm=lambda x: to_str(x).strip().upper())
    cmp_str("CPF/CNPJ", plan.doc, site.get("doc", ""), norm=normalize_doc)
    cmp_money("Valor dos Serviços", plan.valor_servicos, site.get("valor_servicos", "0"))

    for imp, plan_val in [("PIS", plan.pis), ("INSS", plan.inss), ("CSLL", plan.csll), ("COFINS", plan.cofins), ("IR", plan.ir)]:
        s = site.get(imp.lower(), "")
        if s:
            if abs(parse_money_br(plan_val) - parse_money_br(s)) > 0.009:
                diffs.append((imp, plan_val, s))

    if diffs:
        print("\n⚠️ DIFERENÇAS encontradas (Planilha x Site):")
        for label, a, b in diffs:
            print(f" - {label}: planilha='{a}' | site='{b}'")
        print("\n➡️ Corrija MANUALMENTE no site (se necessário).")
        input("✅ Depois de corrigir, pressione ENTER para continuar...")
    else:
        print("✅ Valores conferidos: OK (planilha x site).")

# =========================
# PREENCHER DESCRIÇÃO
# =========================
def preencher_descricao(driver, texto: str):
    ta = WebDriverWait(driver, TIMEOUT).until(lambda d: find_descricao_textarea(d))
    ta.click()
    ta.send_keys(Keys.CONTROL, "a")
    ta.send_keys(Keys.BACKSPACE)
    ta.send_keys(texto)
    time.sleep(0.5)

# =========================
# EMITIR E CAPTURAR NÚMERO
# =========================
def capturar_numero_nota_emitida(driver) -> Optional[str]:
    try:
        txt = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        txt = ""

    patterns = [
        r"Nota\s+Fiscal\s+.*?\b(\d{3,})\b",
        r"N[ºo]\s*da\s*Nota\s*[:\-]?\s*(\d{3,})",
        r"N[ºo]\s*Nota\s*[:\-]?\s*(\d{3,})",
        r"n[úu]mero\s+da\s+nota\s*[:\-]?\s*(\d{3,})",
    ]
    for p in patterns:
        m = re.search(p, txt, flags=re.IGNORECASE|re.DOTALL)
        if m:
            return m.group(1)
    return None

def emitir_nota_e_capturar(driver) -> Optional[str]:
    btn = WebDriverWait(driver, TIMEOUT).until(lambda d: find_btn_emitir(d))
    safe_click(driver, btn)

    time.sleep(2)
    wait_dom_ready(driver, 30)
    time.sleep(2)

    num = capturar_numero_nota_emitida(driver)
    if num:
        print(f"✅ Número da nota capturado: {num}")
        return num

    err = toast_erro_generico(driver)
    if err:
        print("⚠️ Mensagem após emitir:", err)
    print("⚠️ Não foi possível capturar o número automaticamente (verifique a tela).")
    return None

# =========================
# MAIN
# =========================
def main():
    print("📄 Lendo planilha:", ARQ_XLSX)
    pl = carregar_primeira_linha_planilha(ARQ_XLSX)
    print("✅ Primeira linha carregada:")
    print("   Nota:", pl.nota)
    print("   Nome:", pl.nome)
    print("   Doc:", pl.doc)
    print("   Valor Serviços:", pl.valor_servicos)

    opts = ChromeOptions()
    if HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--start-maximized")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-popup-blocking")

    driver = webdriver.Chrome(options=opts)

    try:
        print("\n➡️ Vá até 'NFSE PRESTADOR > Emitir Nota Fiscal' e deixe em 'Identificação e Clonagem'.")
        input("Quando estiver pronto (logado e na tela), pressione ENTER...")

        wait_dom_ready(driver, 30)

        ok = clonar_com_retry(driver, pl.nota)
        if not ok:
            print("❌ Não foi possível clonar a nota após retries.")
            return

        time.sleep(2)
        site = coletar_dados_site(driver)
        compara_e_pausa_se_diferente(pl, site)

        texto_desc = build_descricao_mes(pl.desc_nf)
        print("\n✍️ Preenchendo Descrição Nota Fiscal:")
        print(texto_desc)
        preencher_descricao(driver, texto_desc)

        print("\n🧾 Emitindo nota fiscal...")
        numero = emitir_nota_e_capturar(driver)

        print("\n✅ Fluxo concluído.")
        if numero:
            print("📌 Número emitido:", numero)

        input("\nPressione ENTER para encerrar e fechar o navegador...")

    finally:
        try:
            driver.quit()
        except Exception:
            pass

if __name__ == "__main__":
    main()
