import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.db")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Create clients table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS clients (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        cnpj_cpf TEXT NOT NULL,
        invoice_value REAL NOT NULL,
        boleto_value REAL NOT NULL,
        reference_note TEXT,
        description_template TEXT NOT NULL,
        due_day INTEGER NOT NULL,
        emails TEXT,
        retention_type TEXT NOT NULL, -- 'ISSQN retido', 'Sem retenção', 'Pagamento por depósito bancário'
        active INTEGER DEFAULT 1,
        requires_boleto INTEGER DEFAULT 1,
        bradesco_payer_name TEXT DEFAULT ''
    )
    """)
    
    # Create emissions table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS emissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER,
        competence TEXT NOT NULL, -- MM/YYYY
        status TEXT NOT NULL, -- 'emitida', 'erro', 'pendente'
        invoice_number TEXT,
        error_message TEXT,
        pdf_path TEXT,
        screenshot_path TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (client_id) REFERENCES clients (id)
    )
    """)
    
    # Create system_config table
    # Create billing e-mail sending control table. The unique key prevents
    # duplicated sends for the same client and competence.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS email_sends (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        emission_id INTEGER,
        competence TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pendente',
        emails_sent TEXT,
        from_email TEXT,
        subject TEXT,
        invoice_pdf_path TEXT,
        boleto_pdf_path TEXT,
        boleto_due_date TEXT,
        boleto_value REAL,
        sent_at DATETIME,
        error_message TEXT,
        failed_step TEXT,
        screenshot_path TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(client_id, competence),
        FOREIGN KEY (client_id) REFERENCES clients (id),
        FOREIGN KEY (emission_id) REFERENCES emissions (id)
    )
    """)
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS system_config (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    
    # Lightweight migrations for existing local databases.
    cursor.execute("PRAGMA table_info(clients)")
    client_columns = {row["name"] for row in cursor.fetchall()}
    if "requires_boleto" not in client_columns:
        cursor.execute("ALTER TABLE clients ADD COLUMN requires_boleto INTEGER DEFAULT 1")
    if "bradesco_payer_name" not in client_columns:
        cursor.execute("ALTER TABLE clients ADD COLUMN bradesco_payer_name TEXT DEFAULT ''")
    cursor.execute("""
        UPDATE clients
        SET bradesco_payer_name = ?
        WHERE lower(name) = lower(?) AND (bradesco_payer_name IS NULL OR trim(bradesco_payer_name) = '')
    """, ("VICTOR PELLEGRINI MAMMANA", "Victor Mammana"))
    cursor.execute("""
        UPDATE clients
        SET requires_boleto = 0
        WHERE lower(name) = lower(?)
    """, ("Elite",))

    # Migrate emissions table
    cursor.execute("PRAGMA table_info(emissions)")
    emissions_columns = {row["name"] for row in cursor.fetchall()}
    if "boleto_status" not in emissions_columns:
        cursor.execute("ALTER TABLE emissions ADD COLUMN boleto_status TEXT DEFAULT NULL")
    if "boleto_pdf_path" not in emissions_columns:
        cursor.execute("ALTER TABLE emissions ADD COLUMN boleto_pdf_path TEXT DEFAULT NULL")
    if "boleto_error_message" not in emissions_columns:
        cursor.execute("ALTER TABLE emissions ADD COLUMN boleto_error_message TEXT DEFAULT NULL")
    if "boleto_screenshot_path" not in emissions_columns:
        cursor.execute("ALTER TABLE emissions ADD COLUMN boleto_screenshot_path TEXT DEFAULT NULL")

    conn.commit()
    
    # Seed default configurations
    default_configs = [
        ("portal_cnpj", "07.268.051/0001-48"),
        ("portal_password", "5C0A11EF"),
        ("headless", "false"),
        ("bradesco_user", "LCSR00145"),
        ("bradesco_password", "@ccessINC21*"),
    ]
    for key, value in default_configs:
        cursor.execute("INSERT OR IGNORE INTO system_config (key, value) VALUES (?, ?)", (key, value))
        
    conn.commit()

    # Seed default clients
    cursor.execute("SELECT COUNT(*) as count FROM clients")
    if cursor.fetchone()["count"] == 0:
        clients = [
            {
                "name": "Congregação Sta Cruz",
                "cnpj_cpf": "60.993.193/0001-50",
                "invoice_value": 3580.00,
                "boleto_value": 3460.43,
                "reference_note": "2865",
                "description_template": "Prestação de serviços de consultoria em Linux referente à competência de **<Preencher com o mês de competência>**.",
                "due_day": 10,
                "emails": "aguesse@santacruzbr.com.br, nfecsc@santacruzbr.com.br, nfe@santacruzbr.com.br",
                "retention_type": "ISSQN retido"
            },
            {
                "name": "Cândido",
                "cnpj_cpf": "46.044.368/0001-52",
                "invoice_value": 6440.00,
                "boleto_value": 6224.90,
                "reference_note": "2870",
                "description_template": "TERMO DE CONVÊNIO 06/21 RECURSO FEDERAL - SSCF-GJL-1153-2025 CONSULTORIA EM INFORMÁTICA Mês de competência < DATA INICIAL > A < DATA FINAL> - CONTRATO DE PRESTAÇÃO DE SERVIÇOS DE INFORMÁTICA - ASSINADO EM JUNHO 2025 - EM VIGOR A PARTIR DE 02/06/2025 A 31/05/2027 - 24 MESES. NO VALOR MENSAL FIXO DE R$ 6.440,00.",
                "due_day": 10,
                "emails": "contabilidade@candido.org.br, contratos@candido.org.br, ti@candido.org.br, douglas.almeida@candido.org.br",
                "retention_type": "ISSQN retido"
            },
            {
                "name": "Genética",
                "cnpj_cpf": "04.213.796/0001-11",
                "invoice_value": 1450.00,
                "boleto_value": 1401.57,
                "reference_note": "2868",
                "description_template": "CONSULTORIA EM INFORMÁTICA DE Mês de Competência< DATA INICIAL > A < DATA FINAL> -VITUAL HOSTING - 2VCPU, 2G VRAM, 50 GB DISCO - FIREWALL + BACKUP + GESTÃO - SISTEMA",
                "due_day": 15,
                "emails": "financeiro@geneticamedica.com.br",
                "retention_type": "ISSQN retido"
            },
            {
                "name": "Essência do Cuidar",
                "cnpj_cpf": "53.978.067/0001-61",
                "invoice_value": 200.00,
                "boleto_value": 200.00,
                "reference_note": "",
                "description_template": "SERVIÇO DE HOSPEDAGEM E ADMINISTRAÇÃO DE SERVIDOR PROFISSIONAL PARA ADMINISTRAÇÃO DE E-MAILS FATURA REFERENTE A Mês de Competência< DATA INICIO > ATÉ < DATA FINAL >",
                "due_day": 10,
                "emails": "ivone@essenciadocuidar.com.br",
                "retention_type": "Sem retenção"
            },
            {
                "name": "Victor Mammana",
                "cnpj_cpf": "171.115.968-97",
                "invoice_value": 150.00,
                "boleto_value": 150.00,
                "reference_note": "2863",
                "description_template": "HOSPEDAGEM DE MÁQUINA VIRTUAL - SERVIÇOS REF. AO MÊS DE **< Mês de referencia>** Mês de Competência< DATA INICIO > ATÉ **< DATA FINAL >** - VALOR DE R$ 150,00/MÊS VALOR ADICIONAL DE R$ 40,00 POR SERVIÇOS ADICIONAIS VALOR APROXIMADO DOS TRIBUTOS FEDERAIS R$ 20,17 (13,45%) - FONTE IBPT VALOR APROXIMADO DOS TRIBUTOS MUNICIPAIS R$ 5,87 (3,91%) - FONTE IBPT",
                "due_day": 10,
                "emails": "vpmammana@gmail.com",
                "retention_type": "Sem retenção"
            },
            {
                "name": "Tradição",
                "cnpj_cpf": "00.668.571/0001-07",
                "invoice_value": 1117.48,
                "boleto_value": 1117.48,
                "reference_note": "2864",
                "description_template": "ASSESSORIA INFORMÁTICA - PRESTAÇÃO DE SERVIÇOS DE GESTÃO E GUARDA DE E-MAILS FATURA REF. SERVIÇOS DE Mês de Competência < DATA INICIAL > A < DATA FINAL >",
                "due_day": 10,
                "emails": "ramon@tradicaonline.com.br",
                "retention_type": "Sem retenção"
            },
            {
                "name": "Performance",
                "cnpj_cpf": "57.949.539/0001-09",
                "invoice_value": 546.70,
                "boleto_value": 546.70,
                "reference_note": "2866",
                "description_template": "PRESTAÇÃO DE SERVIÇOS DE CONSULTORIA EM informática REFERENTE À COMPETÊNCIA DE <MÊS> DE ANO.",
                "due_day": 10,
                "emails": "ana@performance.ind.br",
                "retention_type": "Sem retenção"
            },
            {
                "name": "Prof. Carlos Mammana",
                "cnpj_cpf": "000.950.368-49",
                "invoice_value": 500.70,
                "boleto_value": 500.70,
                "reference_note": "2867",
                "description_template": "PRESTAÇÃO DE SERVIÇOS DE CONSULTORIA EM INFORMÁTICA REFERENTE À COMPETÊNCIA DE <MÊS> DE ANO.",
                "due_day": 15,
                "emails": "alaide.mammana@abinfo.com.br, alaide.mammana@uol.com.br, alessandragreatti@yahoo.com.br",
                "retention_type": "Sem retenção"
            },
            {
                "name": "Elite",
                "cnpj_cpf": "28.768.685/0001-30",
                "invoice_value": 400.17,
                "boleto_value": 400.17,
                "reference_note": "2869",
                "description_template": "SERVIÇO DE HOSPEDAGEM E ADMINISTRAÇÃO DE SERVIDOR PROFISSIONAL PARA ADMINISTRAÇÃO DE E-MAILS - 300 GB MENSAIS - R$ 332,16 + LOG = R$ 68,00 = R$ 368,00 CONSULTORIA EM INFORMÁTICA DE <Mês de Competência Inicio> A <fim>",
                "due_day": 10,
                "emails": "rodrigo.duarte.silveira@gmail.com, janaina@dallas-ps.com",
                "retention_type": "Sem retenção",
                "requires_boleto": 0
            },
            {
                "name": "Laticínio Vale do Pardo",
                "cnpj_cpf": "02.749.513/0001-25",
                "invoice_value": 400.00,
                "boleto_value": 400.00,
                "reference_note": "2871",
                "description_template": "SERVIÇO DE HOSPEDAGEM E ADMINISTRAÇÃO DE SERVIDOR PROFISSIONAL PARA ADMINISTRAÇÃO DE E-MAILS R$ 400,00 REFERENTE A <Mês de Competência Inicio> A <Fim>",
                "due_day": 10,
                "emails": "priscila@valedopardo.com.br, fiscal@valedopardo.com.br",
                "retention_type": "Pagamento por depósito bancário"
            },
            {
                "name": "Snap / Edison",
                "cnpj_cpf": "53.983.340/0001-46",
                "invoice_value": 1800.00,
                "boleto_value": 1800.00,
                "reference_note": "2892",
                "description_template": "Colocation de equipamentos em data center - Mês de competência <Mês/Ano>",
                "due_day": 10,
                "emails": "WhatsApp do Edison 19 99753-8463",
                "retention_type": "Pagamento por depósito bancário"
            }
        ]
        
        for c in clients:
            cursor.execute("""
            INSERT INTO clients (name, cnpj_cpf, invoice_value, boleto_value, reference_note, description_template, due_day, emails, retention_type, requires_boleto)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (c["name"], c["cnpj_cpf"], c["invoice_value"], c["boleto_value"], c["reference_note"], c["description_template"], c["due_day"], c["emails"], c["retention_type"], c.get("requires_boleto", 1)))
            
        conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully at:", DB_PATH)
