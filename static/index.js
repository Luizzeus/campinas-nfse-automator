// App State
let clients = [];
let selectedClients = new Set();
let emissionsHistory = [];
let reportsList = [];
let billingEmailItems = [];
let selectedBillingEmailClients = new Set();
let ws = null;
let refreshTimer = null;

// DOM Elements
const tabButtons = document.querySelectorAll('.nav-menu .nav-item');
const tabContents = document.querySelectorAll('.tab-content');
const wsStatusText = document.getElementById('ws-status-text');
const wsStatusIndicator = document.querySelector('.system-status .status-indicator');

// Dashboard Tab Elements
const clientsEmissionsList = document.getElementById('clients-emissions-list');
const selectAllClientsCheckbox = document.getElementById('select-all-clients');
const selectedCountEl = document.getElementById('selected-count');
const totalCountEl = document.getElementById('total-count');
const btnStartAutomation = document.getElementById('btn-start-automation');
const btnStartBoletosOnly = document.getElementById('btn-start-boboleto-only') || document.getElementById('btn-start-boletos-only');
const runRefDateInput = document.getElementById('run-ref-date');
const terminalLogOutput = document.getElementById('terminal-log-output');
const btnClearLogs = document.getElementById('btn-clear-logs');

// History Tab Elements
const emissionsHistoryList = document.getElementById('emissions-history-list');
const reportsListEl = document.getElementById('reports-list');
const reportCompetenceInput = document.getElementById('report-competence-input');
const btnGenerateReport = document.getElementById('btn-generate-report');

// Billing Emails Tab Elements
const billingEmailCompetenceInput = document.getElementById('billing-email-competence');
const billingEmailsList = document.getElementById('billing-emails-list');
const selectAllBillingEmailsCheckbox = document.getElementById('select-all-billing-emails');
const selectedBillingEmailCountEl = document.getElementById('selected-billing-email-count');
const totalBillingEmailCountEl = document.getElementById('total-billing-email-count');
const btnRefreshBillingEmails = document.getElementById('btn-refresh-billing-emails');
const btnVerifyBoletos = document.getElementById('btn-verify-boletos');
const btnSendSelectedEmails = document.getElementById('btn-send-selected-emails');
const btnSendAllEmails = document.getElementById('btn-send-all-emails');
const btnReprocessEmailErrors = document.getElementById('btn-reprocess-email-errors');
const btnGenerateBillingEmailReport = document.getElementById('btn-generate-billing-email-report');

// Clients Tab Elements
const clientsCrudList = document.getElementById('clients-crud-list');
const btnAddClient = document.getElementById('btn-add-client');

// Config Tab Elements
const configForm = document.getElementById('config-form');
const configCnpj = document.getElementById('config-cnpj');
const configPassword = document.getElementById('config-password');
const configBradescoUser = document.getElementById('config-bradesco-user');
const configBradescoPassword = document.getElementById('config-bradesco-password');
const configHeadless = document.getElementById('config-headless');

// Modal Elements
const clientModal = document.getElementById('client-modal');
const clientForm = document.getElementById('client-form');
const modalTitle = document.getElementById('modal-title');
const btnCloseModal = document.getElementById('btn-close-modal');
const btnCancelModal = document.getElementById('btn-cancel-modal');
const clientIdInput = document.getElementById('client-id');
const clientNameInput = document.getElementById('client-name');
const clientCnpjInput = document.getElementById('client-cnpj');
const clientInvoiceValInput = document.getElementById('client-invoice-val');
const clientBillingValInput = document.getElementById('client-billing-val');
const clientRefNoteInput = document.getElementById('client-ref-note');
const clientRetentionSelect = document.getElementById('client-retention');
const clientEmailInput = document.getElementById('client-email');
const clientRequiresBoletoInput = document.getElementById('client-requires-boleto');
const clientDescTemplateInput = document.getElementById('client-desc-template');

// Initialize App
document.addEventListener('DOMContentLoaded', () => {
    // Set default run execution date to today (calculations are done relative to this)
    const today = new Date().toISOString().split('T')[0];
    runRefDateInput.value = today;
    
    // Set default report competence to previous month
    const d = new Date();
    d.setMonth(d.getMonth() - 1);
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const yyyy = d.getFullYear();
    reportCompetenceInput.value = `${mm}/${yyyy}`;
    billingEmailCompetenceInput.value = `${mm}/${yyyy}`;

    initTabNavigation();
    initWebSocket();
    loadDashboardClients();
    loadConfig();
    loadEmissionsHistory();
    loadReportsList();
    loadBillingEmails();
    loadCrudClients();
    initBillingEmailEvents();
    initModalEvents();
    
    // Clear logs button event
    btnClearLogs.addEventListener('click', () => {
        terminalLogOutput.innerHTML = `<div class="log-line info"><span class="log-time">[${new Date().toLocaleTimeString()}]</span> Terminal limpo.</div>`;
    });
});

// Tab Navigation
function initTabNavigation() {
    tabButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            const tabId = btn.getAttribute('data-tab');
            
            // Toggle active classes on buttons
            tabButtons.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            
            // Toggle active classes on tabs
            tabContents.forEach(content => {
                content.classList.remove('active');
                if (content.id === `tab-${tabId}`) {
                    content.classList.add('active');
                }
            });
            
            // Refresh data on specific tab click
            if (tabId === 'history') {
                loadEmissionsHistory();
                loadReportsList();
            } else if (tabId === 'billing-emails') {
                loadBillingEmails();
            } else if (tabId === 'clients') {
                loadCrudClients();
            } else if (tabId === 'dashboard') {
                loadDashboardClients();
            }
        });
    });
}

// WebSocket connection for logs
function initWebSocket() {
    const loc = window.location;
    const proto = loc.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${proto}//${loc.host}/ws/logs`;
    
    ws = new WebSocket(wsUrl);
    
    ws.onopen = () => {
        wsStatusText.textContent = "Conectado ao Servidor";
        wsStatusIndicator.className = "status-indicator online";
    };
    
    ws.onclose = () => {
        wsStatusText.textContent = "Desconectado do Servidor";
        wsStatusIndicator.className = "status-indicator offline";
        // Reconnect after 3s
        setTimeout(initWebSocket, 3000);
    };
    
    ws.onmessage = (event) => {
        const log = JSON.parse(event.data);
        appendTerminalLog(log);
        if (shouldRefreshAfterLog(log)) {
            scheduleUiRefresh();
        }
    };
}

function shouldRefreshAfterLog(log) {
    const message = (log && log.message) ? String(log.message).toLowerCase() : '';
    return Boolean(
        log && (
            log.pdf_url ||
            message.includes('processamento concluído') ||
            message.includes('processamento concluido') ||
            message.includes('nota emitida') ||
            message.includes('nota recuperada') ||
            message.includes('salva para') ||
            message.includes('recuperada e salva')
        )
    );
}

function scheduleUiRefresh() {
    if (refreshTimer) {
        clearTimeout(refreshTimer);
    }
    refreshTimer = setTimeout(() => {
        loadDashboardClients();
        loadEmissionsHistory();
        loadReportsList();
        loadBillingEmails();
    }, 700);
}

function appendTerminalLog(log) {
    const logLine = document.createElement('div');
    logLine.className = `log-line ${log.status || 'info'}`;
    
    const timeSpan = document.createElement('span');
    timeSpan.className = 'log-time';
    timeSpan.textContent = `[${log.timestamp || new Date().toLocaleTimeString()}] `;
    
    logLine.appendChild(timeSpan);
    logLine.appendChild(document.createTextNode(log.message));
    
    if (log.pdf_url) {
        const link = document.createElement('a');
        link.href = log.pdf_url;
        link.target = '_blank';
        link.className = 'btn btn-primary';
        link.style.padding = '2px 8px';
        link.style.fontSize = '10px';
        link.style.marginLeft = '10px';
        link.style.display = 'inline-flex';
        link.style.alignItems = 'center';
        link.style.gap = '4px';
        link.innerHTML = '<span class="material-icons-round" style="font-size:12px;">download</span> Baixar Nota';
        logLine.appendChild(link);
    }
    
    terminalLogOutput.appendChild(logLine);
    terminalLogOutput.scrollTop = terminalLogOutput.scrollHeight;
}

// Helper formatting currency
function formatCurrency(val) {
    return new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' }).format(val);
}

// Load Clients for Dashboard Checkbox List
async function loadDashboardClients() {
    try {
        const res = await fetch('/api/clients');
        clients = await res.json();
        
        clientsEmissionsList.innerHTML = '';
        totalCountEl.textContent = clients.length;
        
        clients.forEach(c => {
            const tr = document.createElement('tr');
            
            // Checkbox td
            const tdCheck = document.createElement('td');
            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.value = c.id;
            checkbox.checked = selectedClients.has(c.id);
            checkbox.addEventListener('change', () => {
                if (checkbox.checked) {
                    selectedClients.add(c.id);
                } else {
                    selectedClients.delete(c.id);
                    selectAllClientsCheckbox.checked = false;
                }
                updateSelectionCount();
            });
            tdCheck.appendChild(checkbox);
            
            // Name
            const tdName = document.createElement('td');
            tdName.style.fontWeight = '600';
            tdName.textContent = c.name;
            
            // CNPJ
            const tdCnpj = document.createElement('td');
            tdCnpj.textContent = c.cnpj_cpf;
            
            // Value
            const tdVal = document.createElement('td');
            tdVal.textContent = formatCurrency(c.invoice_value);
            
            // Retention
            const tdRet = document.createElement('td');
            const retBadge = document.createElement('span');
            retBadge.className = c.retention_type === 'ISSQN Retido' ? 'badge badge-warning' : 'badge badge-success';
            retBadge.textContent = c.retention_type;
            tdRet.appendChild(retBadge);
            
            // Reference Note
            const tdRef = document.createElement('td');
            tdRef.textContent = c.reference_note || '-';
            
            tr.appendChild(tdCheck);
            tr.appendChild(tdName);
            tr.appendChild(tdCnpj);
            tr.appendChild(tdVal);
            tr.appendChild(tdRet);
            tr.appendChild(tdRef);
            
            clientsEmissionsList.appendChild(tr);
        });
        
        updateSelectionCount();
    } catch (e) {
        console.error("Error loading dashboard clients:", e);
    }
}

function updateSelectionCount() {
    selectedCountEl.textContent = selectedClients.size;
}

// Select All event
selectAllClientsCheckbox.addEventListener('change', () => {
    const checkboxes = clientsEmissionsList.querySelectorAll('input[type="checkbox"]');
    checkboxes.forEach(cb => {
        cb.checked = selectAllClientsCheckbox.checked;
        const id = parseInt(cb.value);
        if (selectAllClientsCheckbox.checked) {
            selectedClients.add(id);
        } else {
            selectedClients.delete(id);
        }
    });
    updateSelectionCount();
});

// Run Automation Trigger
btnStartAutomation.addEventListener('click', async () => {
    if (selectedClients.size === 0) {
        alert("Por favor, selecione pelo menos um cliente para emitir.");
        return;
    }
    

    
    // Clear logs
    terminalLogOutput.innerHTML = '';
    appendTerminalLog({
        timestamp: new Date().toLocaleTimeString(),
        status: 'info',
        message: "Lançando executor de automação..."
    });
    
    try {
        const payload = {
            client_ids: Array.from(selectedClients),
            ref_date: runRefDateInput.value
        };
        
        const res = await fetch('/api/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        
        const data = await res.json();
        appendTerminalLog({
            timestamp: new Date().toLocaleTimeString(),
            status: 'success',
            message: data.message
        });
        
        // Reset check selections
        selectedClients.clear();
        selectAllClientsCheckbox.checked = false;
        loadDashboardClients();
    } catch (e) {
        appendTerminalLog({
            timestamp: new Date().toLocaleTimeString(),
            status: 'error',
            message: "Erro ao tentar conectar com a API de inicialização da automação."
        });
    }
});

// Run Boletos Standalone Trigger
btnStartBoletosOnly.addEventListener('click', async () => {
    if (selectedClients.size === 0) {
        alert("Por favor, selecione pelo menos um cliente para gerar boletos.");
        return;
    }
    
    // Clear logs
    terminalLogOutput.innerHTML = '';
    appendTerminalLog({
        timestamp: new Date().toLocaleTimeString(),
        status: 'info',
        message: "Lançando executor de boletos..."
    });
    
    try {
        const payload = {
            client_ids: Array.from(selectedClients),
            ref_date: runRefDateInput.value
        };
        
        const res = await fetch('/api/run-boletos', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        
        const data = await res.json();
        appendTerminalLog({
            timestamp: new Date().toLocaleTimeString(),
            status: 'success',
            message: data.message
        });
        
        // Reset check selections
        selectedClients.clear();
        selectAllClientsCheckbox.checked = false;
        loadDashboardClients();
    } catch (e) {
        appendTerminalLog({
            timestamp: new Date().toLocaleTimeString(),
            status: 'error',
            message: "Erro ao tentar conectar com a API de boletos."
        });
    }
});

// Load System Config
async function loadConfig() {
    try {
        const res = await fetch('/api/config');
        const config = await res.json();
        
        configCnpj.value = config.portal_cnpj;
        configPassword.value = config.portal_password;
        configBradescoUser.value = config.bradesco_user || '';
        configBradescoPassword.value = config.bradesco_password || '';
        configHeadless.checked = config.headless;
    } catch (e) {
        console.error("Error loading config:", e);
    }
}

// Save System Config
configForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    
    const payload = {
        portal_cnpj: configCnpj.value,
        portal_password: configPassword.value,
        bradesco_user: configBradescoUser.value,
        bradesco_password: configBradescoPassword.value,
        headless: configHeadless.checked
    };
    
    try {
        const res = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        alert(data.message);
    } catch (err) {
        alert("Erro ao tentar salvar configurações.");
    }
});

// Load Emissions History
async function loadEmissionsHistory() {
    try {
        const res = await fetch('/api/emissions');
        emissionsHistory = await res.json();
        
        emissionsHistoryList.innerHTML = '';
        
        emissionsHistory.forEach(e => {
            const tr = document.createElement('tr');
            
            // Format timestamp
            const dateVal = e.timestamp ? e.timestamp.replace(' ', ' às ') : '-';
            
            const tdTime = document.createElement('td');
            tdTime.textContent = dateVal;
            
            const tdClient = document.createElement('td');
            tdClient.style.fontWeight = '600';
            tdClient.textContent = e.client_name;
            
            const tdComp = document.createElement('td');
            tdComp.textContent = e.competence;
            
            // Status badge for NFS-e
            const tdStatus = document.createElement('td');
            const badge = document.createElement('span');
            badge.className = e.status === 'emitida' ? 'badge badge-success' : 'badge badge-danger';
            badge.textContent = e.status.toUpperCase();
            tdStatus.appendChild(badge);
            
            const tdNote = document.createElement('td');
            tdNote.textContent = e.invoice_number || '-';
            
            // Status badge for Boleto
            const tdBoletoStatus = document.createElement('td');
            const boletoBadge = document.createElement('span');
            if (e.boleto_status === 'gerado') {
                boletoBadge.className = 'badge badge-success';
                boletoBadge.textContent = 'GERADO';
            } else if (e.boleto_status === 'erro') {
                boletoBadge.className = 'badge badge-danger';
                boletoBadge.textContent = 'ERRO';
            } else if (e.boleto_status === 'pendente') {
                boletoBadge.className = 'badge badge-warning';
                boletoBadge.textContent = 'PENDENTE';
            } else if (e.boleto_status === 'nao_exigido') {
                boletoBadge.className = 'badge badge-secondary';
                boletoBadge.style.opacity = '0.7';
                boletoBadge.textContent = 'NÃO EXIGIDO';
            } else {
                boletoBadge.className = 'badge';
                boletoBadge.textContent = '-';
            }
            tdBoletoStatus.appendChild(boletoBadge);
            
            // Actions
            const tdActions = document.createElement('td');
            tdActions.style.display = 'flex';
            tdActions.style.gap = '6px';
            
            // NFS-e download/view link
            if (e.status === 'emitida' && e.pdf_path) {
                const pdfLink = document.createElement('a');
                const index = e.pdf_path.indexOf('/invoices/');
                const url = index !== -1 ? e.pdf_path.substring(index) : '#';
                pdfLink.href = url;
                pdfLink.target = '_blank';
                pdfLink.className = 'btn btn-icon';
                pdfLink.title = 'Visualizar Nota PDF';
                pdfLink.innerHTML = '<span class="material-icons-round" style="font-size:16px;">receipt</span>';
                tdActions.appendChild(pdfLink);
            } else if (e.status === 'erro' && e.screenshot_path) {
                const imgLink = document.createElement('a');
                const index = e.screenshot_path.indexOf('/screenshots/');
                const url = index !== -1 ? e.screenshot_path.substring(index) : '#';
                imgLink.href = url;
                imgLink.target = '_blank';
                imgLink.className = 'btn btn-icon';
                imgLink.title = 'Erro Nota: Ver Screenshot';
                imgLink.innerHTML = '<span class="material-icons-round" style="font-size:16px;">error_outline</span>';
                tdActions.appendChild(imgLink);
            }
            
            // Boleto download/view link
            if (e.boleto_status === 'gerado' && e.boleto_pdf_path) {
                const boletoLink = document.createElement('a');
                const index = e.boleto_pdf_path.indexOf('/boletos/');
                const indexInv = e.boleto_pdf_path.indexOf('/invoices/');
                const url = index !== -1 ? e.boleto_pdf_path.substring(index) : (indexInv !== -1 ? e.boleto_pdf_path.substring(indexInv) : '#');
                boletoLink.href = url;
                boletoLink.target = '_blank';
                boletoLink.className = 'btn btn-icon';
                boletoLink.style.color = '#e53935'; // distinct red color for boleto download
                boletoLink.title = 'Visualizar Boleto PDF';
                boletoLink.innerHTML = '<span class="material-icons-round" style="font-size:16px;">picture_as_pdf</span>';
                tdActions.appendChild(boletoLink);
            } else if (e.boleto_status === 'erro' && e.boleto_screenshot_path) {
                const imgLink = document.createElement('a');
                const index = e.boleto_screenshot_path.indexOf('/screenshots/');
                const url = index !== -1 ? e.boleto_screenshot_path.substring(index) : '#';
                imgLink.href = url;
                imgLink.target = '_blank';
                imgLink.className = 'btn btn-icon';
                imgLink.title = 'Erro Boleto: Ver Screenshot';
                imgLink.innerHTML = '<span class="material-icons-round" style="font-size:16px;">photo</span>';
                tdActions.appendChild(imgLink);
            }
            
            if (tdActions.childNodes.length === 0) {
                tdActions.textContent = '-';
            }
            
            tr.appendChild(tdTime);
            tr.appendChild(tdClient);
            tr.appendChild(tdComp);
            tr.appendChild(tdStatus);
            tr.appendChild(tdNote);
            tr.appendChild(tdBoletoStatus);
            tr.appendChild(tdActions);
            
            emissionsHistoryList.appendChild(tr);
        });
    } catch (e) {
        console.error("Error loading emissions history:", e);
    }
}

// Load Reports List
async function loadReportsList() {
    try {
        const res = await fetch('/api/reports');
        reportsList = await res.json();
        
        reportsListEl.innerHTML = '';
        
        if (reportsList.length === 0) {
            reportsListEl.innerHTML = '<li style="text-align:center; color:var(--text-muted); font-size:12px; margin-top:20px;">Nenhum relatório emitido ainda.</li>';
            return;
        }
        
        reportsList.forEach(r => {
            const li = document.createElement('li');
            li.className = 'report-item';
            
            const infoDiv = document.createElement('div');
            infoDiv.className = 'report-info';
            
            const h4 = document.createElement('h4');
            h4.textContent = r.filename;
            
            const p = document.createElement('p');
            const sizeKB = (r.size_bytes / 1024).toFixed(1);
            p.textContent = `${r.created_at} | ${sizeKB} KB`;
            
            infoDiv.appendChild(h4);
            infoDiv.appendChild(p);
            
            const downloadLink = document.createElement('a');
            downloadLink.href = r.url;
            downloadLink.target = '_blank';
            downloadLink.className = 'btn btn-icon';
            downloadLink.title = 'Baixar Relatório';
            downloadLink.innerHTML = '<span class="material-icons-round" style="font-size:16px;">download</span>';
            
            li.appendChild(infoDiv);
            li.appendChild(downloadLink);
            
            reportsListEl.appendChild(li);
        });
    } catch (e) {
        console.error("Error loading reports:", e);
    }
}

// Generate PDF Report Trigger
btnGenerateReport.addEventListener('click', async () => {
    const comp = reportCompetenceInput.value.trim();
    if (!comp || !/^\d{2}\/\d{4}$/.test(comp)) {
        alert("Por favor, preencha a competência no formato MM/AAAA (Ex: 05/2026).");
        return;
    }
    
    try {
        btnGenerateReport.disabled = true;
        btnGenerateReport.textContent = "Gerando...";
        
        const res = await fetch('/api/reports/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ competence: comp })
        });
        
        const data = await res.json();
        btnGenerateReport.disabled = false;
        btnGenerateReport.innerHTML = '<span class="material-icons-round">picture_as_pdf</span> Gerar';
        
        if (res.ok) {
            alert(data.message);
            loadReportsList();
        } else {
            alert(`Erro: ${data.detail}`);
        }
    } catch (e) {
        btnGenerateReport.disabled = false;
        btnGenerateReport.innerHTML = '<span class="material-icons-round">picture_as_pdf</span> Gerar';
        alert("Erro ao enviar pedido de geração de relatório.");
    }
});

function statusBadge(ok, labelOk = 'Sim', labelNo = 'Não') {
    const badge = document.createElement('span');
    badge.className = ok ? 'badge badge-success' : 'badge badge-danger';
    badge.textContent = ok ? labelOk : labelNo;
    return badge;
}

function emailStatusBadge(item) {
    if (item.email_sent) return statusBadge(true, 'E-mail enviado', '');
    if (item.status === 'erro') return statusBadge(false, 'Enviado', 'Erro no envio');
    const badge = document.createElement('span');
    badge.className = 'badge badge-warning';
    badge.textContent = item.status === 'processando' ? 'Processando' : 'Pendente';
    return badge;
}

function getBillingCompetence() {
    return billingEmailCompetenceInput.value.trim();
}

function validCompetence(comp) {
    return /^(0[1-9]|1[0-2])\/\d{4}$/.test(comp);
}

async function loadBillingEmails() {
    const comp = getBillingCompetence();
    if (!validCompetence(comp)) {
        billingEmailsList.innerHTML = '<tr><td colspan="8" style="text-align:center; color:var(--text-muted);">Informe a competência no formato MM/AAAA.</td></tr>';
        return;
    }

    try {
        const res = await fetch(`/api/billing-emails?competence=${encodeURIComponent(comp)}`);
        billingEmailItems = await res.json();
        billingEmailsList.innerHTML = '';
        totalBillingEmailCountEl.textContent = billingEmailItems.length;

        if (billingEmailItems.length === 0) {
            billingEmailsList.innerHTML = '<tr><td colspan="8" style="text-align:center; color:var(--text-muted);">Nenhuma nota emitida encontrada para esta competência.</td></tr>';
            updateBillingEmailSelectionCount();
            return;
        }

        billingEmailItems.forEach(item => {
            const tr = document.createElement('tr');
            const boletoOk = item.boleto_found || item.boleto_exempt;
            const eligible = !item.email_sent && item.note_issued && item.pdf_found && boletoOk && item.emails.length > 0;

            const tdCheck = document.createElement('td');
            const checkbox = document.createElement('input');
            checkbox.type = 'checkbox';
            checkbox.value = item.client_id;
            checkbox.disabled = !eligible;
            checkbox.checked = selectedBillingEmailClients.has(item.client_id) && eligible;
            checkbox.addEventListener('change', () => {
                if (checkbox.checked) {
                    selectedBillingEmailClients.add(item.client_id);
                } else {
                    selectedBillingEmailClients.delete(item.client_id);
                    selectAllBillingEmailsCheckbox.checked = false;
                }
                updateBillingEmailSelectionCount();
            });
            tdCheck.appendChild(checkbox);

            const tdClient = document.createElement('td');
            tdClient.style.fontWeight = '600';
            tdClient.innerHTML = `${item.client_name}<br><span class="muted-small">NF ${item.invoice_number || '-'}</span>`;

            const tdNote = document.createElement('td');
            tdNote.appendChild(statusBadge(item.note_issued, 'Nota emitida'));

            const tdPdf = document.createElement('td');
            tdPdf.appendChild(statusBadge(item.pdf_found, 'PDF encontrado', 'PDF ausente'));

            const tdBoleto = document.createElement('td');
            if (item.boleto_exempt) {
                const badge = document.createElement('span');
                badge.className = 'badge badge-warning';
                badge.textContent = 'Boleto dispensado';
                tdBoleto.appendChild(badge);
            } else {
                tdBoleto.appendChild(statusBadge(item.boleto_found, 'Boleto encontrado', 'Boleto ausente'));
            }

            const tdEmail = document.createElement('td');
            tdEmail.appendChild(emailStatusBadge(item));

            const tdError = document.createElement('td');
            tdError.textContent = item.error_message || '-';
            tdError.title = item.failed_step ? `Etapa: ${item.failed_step}` : '';

            const tdRecipients = document.createElement('td');
            tdRecipients.textContent = item.emails_text || 'Sem e-mail válido';

            tr.appendChild(tdCheck);
            tr.appendChild(tdClient);
            tr.appendChild(tdNote);
            tr.appendChild(tdPdf);
            tr.appendChild(tdBoleto);
            tr.appendChild(tdEmail);
            tr.appendChild(tdError);
            tr.appendChild(tdRecipients);
            billingEmailsList.appendChild(tr);
        });
        updateBillingEmailSelectionCount();
    } catch (e) {
        console.error('Error loading billing emails:', e);
        billingEmailsList.innerHTML = '<tr><td colspan="8" style="text-align:center; color:var(--error);">Erro ao carregar envios de faturamento.</td></tr>';
    }
}

function updateBillingEmailSelectionCount() {
    selectedBillingEmailCountEl.textContent = selectedBillingEmailClients.size;
}

function initBillingEmailEvents() {
    btnRefreshBillingEmails.addEventListener('click', loadBillingEmails);

    btnVerifyBoletos.addEventListener('click', async () => {
        const comp = getBillingCompetence();
        if (!validCompetence(comp)) {
            alert('Preencha a competência no formato MM/AAAA.');
            return;
        }
        try {
            const res = await fetch(`/api/billing-emails/verify-boletos?competence=${encodeURIComponent(comp)}`);
            const data = await res.json();
            if (!res.ok) {
                alert(`Erro: ${data.detail || 'Falha ao verificar boletos.'}`);
                return;
            }
            const missing = data.items.filter(item => !item.boleto_found).map(item => `${item.client_name} (NF ${item.invoice_number})`);
            const message = missing.length
                ? `Boletos encontrados: ${data.found}/${data.total}. Ausentes: ${missing.join(', ')}`
                : `Todos os boletos foram encontrados (${data.found}/${data.total}).`;
            alert(message);
            loadBillingEmails();
        } catch (e) {
            alert('Erro ao verificar boletos.');
        }
    });

    selectAllBillingEmailsCheckbox.addEventListener('change', () => {
        const checkboxes = billingEmailsList.querySelectorAll('input[type="checkbox"]:not(:disabled)');
        checkboxes.forEach(cb => {
            cb.checked = selectAllBillingEmailsCheckbox.checked;
            const id = parseInt(cb.value);
            if (selectAllBillingEmailsCheckbox.checked) {
                selectedBillingEmailClients.add(id);
            } else {
                selectedBillingEmailClients.delete(id);
            }
        });
        updateBillingEmailSelectionCount();
    });

    btnSendSelectedEmails.addEventListener('click', async () => {
        if (selectedBillingEmailClients.size === 0) {
            alert('Selecione pelo menos um cliente elegível para envio.');
            return;
        }
        await triggerBillingEmailSend('/api/billing-emails/send', Array.from(selectedBillingEmailClients));
    });

    btnSendAllEmails.addEventListener('click', async () => {
        if (!confirm('Enviar e-mails para todos os clientes elegíveis desta competência?')) return;
        await triggerBillingEmailSend('/api/billing-emails/send', null);
    });

    btnReprocessEmailErrors.addEventListener('click', async () => {
        await triggerBillingEmailSend('/api/billing-emails/reprocess-errors', null);
    });

    btnGenerateBillingEmailReport.addEventListener('click', async () => {
        const comp = getBillingCompetence();
        if (!validCompetence(comp)) {
            alert('Preencha a competência no formato MM/AAAA.');
            return;
        }
        try {
            const res = await fetch('/api/billing-emails/report', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ competence: comp })
            });
            const data = await res.json();
            if (res.ok) {
                alert(data.message);
                loadReportsList();
                if (data.url) window.open(data.url, '_blank');
            } else {
                alert(`Erro: ${data.detail}`);
            }
        } catch (e) {
            alert('Erro ao gerar relatório PDF.');
        }
    });
}

async function triggerBillingEmailSend(endpoint, clientIds) {
    const comp = getBillingCompetence();
    if (!validCompetence(comp)) {
        alert('Preencha a competência no formato MM/AAAA.');
        return;
    }

    terminalLogOutput.innerHTML = '';
    appendTerminalLog({
        timestamp: new Date().toLocaleTimeString(),
        status: 'info',
        message: 'Iniciando módulo de envio de e-mails de faturamento...'
    });

    try {
        const payload = { competence: comp, client_ids: clientIds };
        const res = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        appendTerminalLog({
            timestamp: new Date().toLocaleTimeString(),
            status: res.ok ? 'success' : 'error',
            message: res.ok ? data.message : (data.detail || 'Erro ao iniciar envio.')
        });
        selectedBillingEmailClients.clear();
        selectAllBillingEmailsCheckbox.checked = false;
        loadBillingEmails();
    } catch (e) {
        appendTerminalLog({
            timestamp: new Date().toLocaleTimeString(),
            status: 'error',
            message: 'Erro ao conectar com a API de envio de e-mails.'
        });
    }
}

// Load Clients CRUD List
async function loadCrudClients() {
    try {
        const res = await fetch('/api/clients');
        const crudClients = await res.json();
        
        clientsCrudList.innerHTML = '';
        
        crudClients.forEach(c => {
            const tr = document.createElement('tr');
            
            const tdName = document.createElement('td');
            tdName.style.fontWeight = '600';
            tdName.textContent = c.name;
            
            const tdCnpj = document.createElement('td');
            tdCnpj.textContent = c.cnpj_cpf;
            
            const tdValNf = document.createElement('td');
            tdValNf.textContent = formatCurrency(c.invoice_value);
            
            const tdValBol = document.createElement('td');
            tdValBol.textContent = formatCurrency(c.boleto_value);
            
            const tdRef = document.createElement('td');
            tdRef.textContent = c.reference_note || '-';
            
            const tdRet = document.createElement('td');
            const retBadge = document.createElement('span');
            retBadge.className = c.retention_type === 'ISSQN Retido' ? 'badge badge-warning' : 'badge badge-success';
            retBadge.textContent = c.retention_type;
            tdRet.appendChild(retBadge);
            
            // Actions
            const tdActions = document.createElement('td');
            tdActions.style.display = 'flex';
            tdActions.style.gap = '8px';
            
            const btnEdit = document.createElement('button');
            btnEdit.className = 'btn btn-icon';
            btnEdit.title = 'Editar Cliente';
            btnEdit.innerHTML = '<span class="material-icons-round" style="font-size:16px;">edit</span>';
            btnEdit.addEventListener('click', () => openClientModal(c));
            
            const btnDelete = document.createElement('button');
            btnDelete.className = 'btn btn-icon btn-danger';
            btnDelete.title = 'Excluir Cliente';
            btnDelete.innerHTML = '<span class="material-icons-round" style="font-size:16px;">delete</span>';
            btnDelete.addEventListener('click', () => deleteClient(c.id, c.name));
            
            tdActions.appendChild(btnEdit);
            tdActions.appendChild(btnDelete);
            
            tr.appendChild(tdName);
            tr.appendChild(tdCnpj);
            tr.appendChild(tdValNf);
            tr.appendChild(tdValBol);
            tr.appendChild(tdRef);
            tr.appendChild(tdRet);
            tr.appendChild(tdActions);
            
            clientsCrudList.appendChild(tr);
        });
    } catch (e) {
        console.error("Error loading crud clients:", e);
    }
}

// Client Modal handling
function initModalEvents() {
    btnAddClient.addEventListener('click', () => openClientModal());
    btnCloseModal.addEventListener('click', closeClientModal);
    btnCancelModal.addEventListener('click', closeClientModal);
    
    clientForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        
        const payload = {
            id: clientIdInput.value ? parseInt(clientIdInput.value) : null,
            name: clientNameInput.value.trim(),
            cnpj_cpf: clientCnpjInput.value.trim(),
            invoice_value: parseFloat(clientInvoiceValInput.value),
            boleto_value: parseFloat(clientBillingValInput.value),
            reference_note: clientRefNoteInput.value.trim(),
            retention_type: clientRetentionSelect.value,
            emails: clientEmailInput.value.trim(),
            requires_boleto: clientRequiresBoletoInput.checked,
            description_template: clientDescTemplateInput.value.trim()
        };
        
        try {
            const res = await fetch('/api/clients', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            
            if (res.ok) {
                closeClientModal();
                loadCrudClients();
            } else {
                const data = await res.json();
                alert(`Erro: ${data.message || 'Falha ao salvar cliente'}`);
            }
        } catch (err) {
            alert("Erro de conexão ao tentar salvar o cliente.");
        }
    });
}

function openClientModal(client = null) {
    clientForm.reset();
    
    if (client) {
        modalTitle.textContent = "Editar Cliente";
        clientIdInput.value = client.id;
        clientNameInput.value = client.name;
        clientCnpjInput.value = client.cnpj_cpf;
        clientInvoiceValInput.value = client.invoice_value;
        clientBillingValInput.value = client.boleto_value;
        clientRefNoteInput.value = client.reference_note || '';
        clientRetentionSelect.value = client.retention_type;
        clientEmailInput.value = client.emails || '';
        clientRequiresBoletoInput.checked = client.requires_boleto !== 0 && client.requires_boleto !== false;
        clientDescTemplateInput.value = client.description_template;
    } else {
        modalTitle.textContent = "Novo Cliente";
        clientIdInput.value = '';
        clientRequiresBoletoInput.checked = true;
    }
    
    clientModal.classList.add('show');
}

function closeClientModal() {
    clientModal.classList.remove('show');
}

// Delete Client
async function deleteClient(id, name) {
    if (!confirm(`Tem certeza que deseja excluir permanentemente o cliente ${name}?`)) {
        return;
    }
    
    try {
        const res = await fetch(`/api/clients/${id}`, {
            method: 'DELETE'
        });
        
        if (res.ok) {
            loadCrudClients();
        } else {
            alert("Erro ao tentar excluir cliente.");
        }
    } catch (e) {
        alert("Erro de rede ao tentar excluir cliente.");
    }
}
