import datetime

def get_competence_info(ref_date=None):
    """
    Given a reference date (defaults to today), calculate the competence month details
    (always the previous month).
    """
    if ref_date is None:
        ref_date = datetime.date.today()
    elif isinstance(ref_date, str):
        # Allow format YYYY-MM-DD or DD/MM/YYYY
        if "/" in ref_date:
            ref_date = datetime.datetime.strptime(ref_date, "%d/%m/%Y").date()
        else:
            ref_date = datetime.datetime.strptime(ref_date, "%Y-%m-%d").date()
            
    # Calculate previous month
    first_day_current = ref_date.replace(day=1)
    last_day_prev = first_day_current - datetime.timedelta(days=1)
    prev_month = last_day_prev.month
    prev_year = last_day_prev.year
    
    first_day_prev = datetime.date(prev_year, prev_month, 1)
    
    months_pt = {
        1: "Janeiro", 2: "Fevereiro", 3: "Março", 4: "Abril", 5: "Maio", 6: "Junho",
        7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro"
    }
    
    month_name = months_pt[prev_month]
    
    return {
        "competence_month_num": f"{prev_month:02d}",
        "competence_month_name": month_name,
        "competence_year": str(prev_year),
        "start_date": first_day_prev.strftime("%d/%m/%Y"),
        "end_date": last_day_prev.strftime("%d/%m/%Y"),
        "month_year_short": f"{prev_month:02d}/{prev_year}",
        "month_year_long": f"{month_name}/{prev_year}",
        "month_year_text": f"{month_name} de {prev_year}"
    }

import re

def format_description(template, ref_date=None):
    """
    Replaces date/competence tags in the description template.
    Handles spaces inside tags like < DATA INICIAL > or < DATA FINAL >.
    """
    info = get_competence_info(ref_date)
    desc = template
    
    def replacer(match):
        tag_content = match.group(1).strip()
        tag_lower = tag_content.lower()
        
        if tag_lower in ["preencher com o mês de competência", "preencher com o mes de competencia", "mês de referência", "mês de referencia", "mes de referencia"]:
            return info["month_year_long"]
        elif tag_lower in ["data inicial", "data inicio", "mês de competência inicio", "mes de competencia inicio"]:
            return info["start_date"]
        elif tag_lower in ["data final", "fim"]:
            return info["end_date"]
        elif tag_lower in ["mês/ano", "mes/ano"]:
            return info["month_year_short"]
        elif tag_lower == "mês":
            return info["competence_month_name"]
        elif tag_lower == "ano":
            return info["competence_year"]
        else:
            # Return original if unrecognized
            return match.group(0)
            
    # Match pattern < tag > with arbitrary spaces
    desc = re.sub(r'<\s*([^>]+?)\s*>', replacer, desc)
    
    # Handle "DE ANO" or "de ano" without angle brackets
    desc = re.sub(r'\bDE\s+ANO\b', f"DE {info['competence_year']}", desc)
    desc = re.sub(r'\bde\s+ano\b', f"de {info['competence_year']}", desc, flags=re.IGNORECASE)

    # Templates may use ** only to mark the part that must be replaced/reviewed.
    # These markers should not be sent to the Campinas portal description.
    desc = desc.replace("**", "")

    # Some saved templates place tags immediately after words, e.g.
    # "Mês de Competência< DATA INICIO >". Keep the final description readable.
    desc = re.sub(r'([A-Za-zÀ-ÿ])(\d{2}/\d{2}/\d{4})', r'\1 \2', desc)
    
    return desc

if __name__ == "__main__":
    # Quick test
    test_date = datetime.date(2026, 6, 2)
    info = get_competence_info(test_date)
    print("Competence Info for 02/06/2026:")
    for k, v in info.items():
        print(f"  {k}: {v}")
        
    templates = [
        "Prestação de serviços de consultoria em Linux referente à competência de **<Preencher com o mês de competência>**.",
        "Mês de competência < DATA INICIAL > A < DATA FINAL>",
        "HOSPEDAGEM DE MÁQUINA VIRTUAL - SERVIÇOS REF. AO MÊS DE **< Mês de referencia>** Mês de Competência< DATA INICIO > ATÉ **< DATA FINAL >**",
        "PRESTAÇÃO DE SERVIÇOS DE CONSULTORIA EM informática REFERENTE À COMPETÊNCIA DE <MÊS> DE ANO."
    ]
    
    print("\nFormatted Templates:")
    for t in templates:
        print("Template:", t)
        print("Formatted:", format_description(t, test_date))
        print("-" * 20)
