# Specchio NFS-e Automator — Campinas

Automação em Python desenvolvida para simplificar e agilizar a emissão e recuperação de Notas Fiscais de Serviços Eletrônicas (NFS-e) diretamente no portal da Prefeitura de Campinas/SP.

O sistema possui uma interface web (Single Page Application - SPA) moderna e amigável para gerenciamento de clientes, visualização de logs de execução em tempo real via WebSocket, geração de relatórios mensais consolidados de faturamento em PDF e controle de configurações de acesso.

---

## 🚀 Funcionalidades

*   **Painel de Emissões (Dashboard):** Visualização rápida de clientes ativos e início de automação em lote.
*   **Emissão Automatizada:** Preenchimento inteligente de notas no portal da prefeitura usando Playwright (com suporte para ISSQN retido/não retido e descrição dinâmica de serviço).
*   **Acompanhamento em Tempo Real:** Logs de execução transmitidos em tempo real para a interface via WebSockets.
*   **Recuperação de Notas:** Download automatizado do PDF de notas fiscais já emitidas diretamente do portal.
*   **Histórico & Relatórios:** Registro histórico de emissões no banco de dados SQLite e geração de relatórios de faturamento mensais em formato PDF profissional (usando ReportLab).
*   **Gerenciamento de Clientes:** CRUD completo de tomadores de serviços diretamente pela interface.
*   **Configurações do Portal:** Cadastro seguro de credenciais (CNPJ/Senha do portal) persistidos no banco local.
*   **Envio de E-mails de Faturamento:** Envio automatizado pelo webmail da Specchio com Nota Fiscal e boleto anexados, validação de arquivos e controle para não duplicar cliente/competência.

---

## 📁 Estrutura do Projeto

*   `main.py`: Servidor backend (FastAPI) contendo as APIs REST, gerenciamento de WebSockets e tarefas em segundo plano (*Background Tasks*).
*   `automator.py`: Motor principal da automação usando Playwright. Controla o fluxo de navegação, preenchimento do formulário do portal e download dos PDFs.
*   `email_sender.py`: Automação Playwright do webmail, validações de e-mail/anexos e persistência do status de envio.
*   `database.py`: Interface de conexão e operações com o banco de dados SQLite (`database.db`).
*   `reporter.py`: Gerador de relatórios de faturamento consolidado em PDF usando a biblioteca ReportLab.
*   `utils.py`: Funções auxiliares (slugify, formatação de datas e templates de descrição).
*   `static/`: Contém os arquivos do frontend web (HTML, CSS customizado e JavaScript).
*   `invoices/`: Pasta gerada localmente para armazenar os PDFs das notas recuperadas/emitidas.
*   `boletos/`: Pasta opcional para boletos. O módulo procura em `invoices/MM-AAAA`, `boletos/MM-AAAA` e `boletos/` por arquivos PDF iniciando com `Bradesco_Nome_do_Cliente_Numero_da_Nota`.
*   `reports/`: Pasta para armazenamento dos relatórios em PDF gerados pelo sistema.
*   `screenshots/`: Pasta para registrar capturas de tela durante falhas da automação.

---

## 🛠️ Pré-requisitos e Instalação

### 1. Pré-requisitos
*   Python 3.8 ou superior instalado.

### 2. Instalar dependências de Python
Instale as bibliotecas necessárias usando o pip:
```bash
pip install fastapi uvicorn websockets playwright reportlab pydantic
```

### 3. Instalar navegadores do Playwright
O Playwright precisa instalar o binário do navegador para executar a automação:
```bash
playwright install chromium
```

### 4. Configurar senha do webmail
A senha do webmail do Specchio pode ser configurada de duas formas:
1. **Pela Interface Gráfica (Recomendado):** Acesse a aba **Configurações** no painel do sistema e informe a senha no campo **"Senha do Webmail (Specchio)"**. Ela será salva com segurança no banco de dados local.
2. **Via Variável de Ambiente (Fallback):** Defina a variável `WEBMAIL_PASSWORD` no terminal antes de rodar o servidor:
   ```bash
   set WEBMAIL_PASSWORD="sua-senha-do-webmail"
   ```

O login padrão utilizado é `luiz.rocha@compunettecnologia.com.br` e o e-mail de envio é `financeiro@specchio.info`.

---

## 💻 Como Executar

No Windows, basta dar dois cliques no executável de lote **`start_app.bat`** na raiz do projeto. Ele detectará a instalação do Python e iniciará o servidor automaticamente.

Para iniciar manualmente:
1. Navegue até o diretório do projeto.
2. Inicie o servidor:
   ```bash
   python run_server.py
   ```
3. O painel abrirá automaticamente no seu navegador padrão no endereço:
   👉 **[http://127.0.0.1:8001](http://127.0.0.1:8001)**

---

## 🔒 Segurança

O arquivo do banco de dados `database.db` e as pastas locais `invoices/`, `reports/` e `screenshots/` estão incluídos no arquivo `.gitignore` para evitar que credenciais e informações sigilosas dos clientes sejam comitados publicamente.
