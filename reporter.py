import os
import datetime
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from database import get_db_connection

# Directory for reports
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

class NumberedCanvas(canvas.Canvas):
    """Canvas subclass for adding 'Page X of Y' footer dynamically."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#718096"))
        
        # Header (Top of page, except first page)
        if self._pageNumber > 1:
            self.drawString(54, 750, "Automação NFS-e Campinas — Relatório de Emissão")
            self.drawRightString(self._pagesize[0] - 54, 750, f"Competência: {self.competence_str}")
            self.setStrokeColor(colors.HexColor("#E2E8F0"))
            self.setLineWidth(0.5)
            self.line(54, 742, self._pagesize[0] - 54, 742)

        # Footer (Bottom of all pages)
        page_text = f"Página {self._pageNumber} de {page_count}"
        self.drawRightString(self._pagesize[0] - 54, 36, page_text)
        self.drawString(54, 36, "Gerado automaticamente pelo Sistema de Automação NFS-e.")
        
        self.setStrokeColor(colors.HexColor("#E2E8F0"))
        self.setLineWidth(0.5)
        self.line(54, 48, self._pagesize[0] - 54, 48)
        
        self.restoreState()

def generate_pdf_report(competence):
    """
    Generate PDF report for a given competence (format: MM/YYYY).
    Saves to reports/Relatorio_Faturamento_MM-YYYY.pdf
    """
    competence_fn = competence.replace("/", "-")
    report_filename = f"Relatorio_Faturamento_{competence_fn}.pdf"
    report_path = os.path.join(REPORTS_DIR, report_filename)
    
    # 1. Fetch data from DB
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Get all clients
    cursor.execute("SELECT id, name, cnpj_cpf, invoice_value, boleto_value FROM clients")
    clients = {row["id"]: dict(row) for row in cursor.fetchall()}
    
    # Get only the emitted notes for the competence. The faturamento report is a
    # sales summary, so it should list the notes that were actually issued.
    cursor.execute("""
        SELECT
            e.*,
            c.name as client_name,
            c.cnpj_cpf as client_cnpj,
            c.invoice_value,
            c.boleto_value,
            c.retention_type,
            c.requires_boleto,
            es.emails_sent,
            es.invoice_pdf_path as email_invoice_pdf_path,
            es.boleto_pdf_path,
            es.boleto_due_date,
            es.boleto_value as email_boleto_value,
            es.status as email_status,
            es.sent_at as email_sent_at,
            es.error_message as email_error_message
        FROM emissions e
        JOIN clients c ON e.client_id = c.id
        LEFT JOIN email_sends es
          ON es.client_id = e.client_id
         AND es.competence = e.competence
        WHERE e.competence = ?
          AND e.status = 'emitida'
          AND e.id IN (
              SELECT MAX(id)
              FROM emissions
              WHERE competence = ? AND status = 'emitida'
              GROUP BY client_id
          )
        ORDER BY c.name ASC
    """, (competence, competence))
    emissions = [dict(row) for row in cursor.fetchall()]
    conn.close()
    
    # Calculate KPIs
    total_attempted = len(emissions)
    total_success = sum(1 for e in emissions if e["status"] == "emitida")
    total_failure = sum(1 for e in emissions if e["status"] == "erro")
    
    total_value_issued = sum(e["invoice_value"] for e in emissions if e["status"] == "emitida")
    total_boleto_issued = sum(e["boleto_value"] for e in emissions if e["status"] == "emitida")
    total_value_failed = sum(e["invoice_value"] for e in emissions if e["status"] == "erro")
    
    # Create Document template
    # Margins: 0.75 in (54 pt)
    doc = SimpleDocTemplate(
        report_path,
        pagesize=landscape(letter),
        leftMargin=54,
        rightMargin=54,
        topMargin=54,
        bottomMargin=54
    )
    
    # Pass competence string to canvas for header drawing
    # Store it as property on document to be read by canvas
    doc.competence_str = competence
    
    # Styles
    styles = getSampleStyleSheet()
    
    # Custom styles
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=24,
        leading=28,
        textColor=colors.HexColor("#1A365D"),
        spaceAfter=6
    )
    
    subtitle_style = ParagraphStyle(
        'DocSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=12,
        leading=16,
        textColor=colors.HexColor("#4A5568"),
        spaceAfter=20
    )
    
    section_title_style = ParagraphStyle(
        'SectionTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=14,
        leading=18,
        textColor=colors.HexColor("#2B6CB0"),
        spaceBefore=15,
        spaceAfter=10
    )
    
    cell_style = ParagraphStyle(
        'TableCell',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=7,
        leading=9,
        textColor=colors.HexColor("#2D3748")
    )
    
    cell_bold_style = ParagraphStyle(
        'TableCellBold',
        parent=cell_style,
        fontName='Helvetica-Bold'
    )
    
    cell_success_style = ParagraphStyle(
        'TableCellSuccess',
        parent=cell_style,
        fontName='Helvetica-Bold',
        textColor=colors.HexColor("#2F855A")
    )
    
    cell_error_style = ParagraphStyle(
        'TableCellError',
        parent=cell_style,
        fontName='Helvetica-Bold',
        textColor=colors.HexColor("#C53030")
    )
    
    kpi_num_style = ParagraphStyle(
        'KPINum',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=16,
        leading=20,
        alignment=1, # Centered
        textColor=colors.HexColor("#1A365D")
    )
    
    kpi_label_style = ParagraphStyle(
        'KPILabel',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=10,
        alignment=1, # Centered
        textColor=colors.HexColor("#718096")
    )

    story = []
    
    # Title & Subtitle
    story.append(Paragraph("Relatório de Faturamento NFS-e", title_style))
    today_str = datetime.date.today().strftime("%d/%m/%Y")
    story.append(Paragraph(f"Competência: <b>{competence}</b> | Data de Emissão: {today_str}", subtitle_style))
    
    # 2. KPI Cards Block
    kpi_data = [
        [
            Paragraph("Tentativas", kpi_label_style),
            Paragraph("Sucessos", kpi_label_style),
            Paragraph("Erros", kpi_label_style),
            Paragraph("Total Emitido (R$)", kpi_label_style),
            Paragraph("Total Boletos (R$)", kpi_label_style)
        ],
        [
            Paragraph(str(total_attempted), kpi_num_style),
            Paragraph(f"<font color='#2F855A'>{total_success}</font>", kpi_num_style),
            Paragraph(f"<font color='#C53030'>{total_failure}</font>", kpi_num_style),
            Paragraph(f"R$ {total_value_issued:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."), kpi_num_style),
            Paragraph(f"R$ {total_boleto_issued:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."), kpi_num_style)
        ]
    ]
    
    # KPI Table Styling
    kpi_table = Table(kpi_data, colWidths=[100, 100, 100, 102, 102])
    kpi_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#F7FAFC")),
        ('BOX', (0, 0), (-1, -1), 1, colors.HexColor("#E2E8F0")),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    
    story.append(kpi_table)
    story.append(Spacer(1, 20))
    
    # 3. Detailed Emissions Title
    story.append(Paragraph("Detalhamento das Emissões", section_title_style))
    
    # Table Header
    table_data = [[
        Paragraph("<b>Cliente</b>", cell_bold_style),
        Paragraph("<b>CNPJ/CPF</b>", cell_bold_style),
        Paragraph("<b>Nota Nº</b>", cell_bold_style),
        Paragraph("<b>Valor Nota</b>", cell_bold_style),
        Paragraph("<b>Valor Boleto</b>", cell_bold_style),
        Paragraph("<b>Venc.</b>", cell_bold_style),
        Paragraph("<b>E-mails enviados</b>", cell_bold_style),
        Paragraph("<b>Arquivo NF</b>", cell_bold_style),
        Paragraph("<b>Arquivo Boleto</b>", cell_bold_style),
        Paragraph("<b>Status Envio</b>", cell_bold_style),
        Paragraph("<b>Data/Hora</b>", cell_bold_style),
        Paragraph("<b>Erro</b>", cell_bold_style)
    ]]
    
    # Table Rows
    for e in emissions:
        val_str = f"R$ {e['invoice_value']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        boleto_value = e['email_boleto_value'] if e['email_boleto_value'] is not None else e['boleto_value']
        boleto_str = f"R$ {boleto_value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        email_status = (e["email_status"] or "pendente").upper()
        status_style = cell_success_style if email_status == "ENVIADO" else cell_error_style if email_status == "ERRO" else cell_style
        err_msg = e["email_error_message"] or e["error_message"] or ""
        if len(err_msg) > 120:
            err_msg = err_msg[:117] + "..."
        invoice_file = os.path.basename(e["email_invoice_pdf_path"] or e["pdf_path"] or "") or "-"
        boleto_file = os.path.basename(e["boleto_pdf_path"] or "") or ("Dispensado" if not e["requires_boleto"] else "-")
        table_data.append([
            Paragraph(e["client_name"], cell_style),
            Paragraph(e["client_cnpj"], cell_style),
            Paragraph(str(e["invoice_number"] or "-"), cell_style),
            Paragraph(val_str, cell_style),
            Paragraph(boleto_str, cell_style),
            Paragraph(e["boleto_due_date"] or ("Dispensado" if not e["requires_boleto"] else "-"), cell_style),
            Paragraph(e["emails_sent"] or "-", cell_style),
            Paragraph(invoice_file, cell_style),
            Paragraph(boleto_file, cell_style),
            Paragraph(email_status, status_style),
            Paragraph(e["email_sent_at"] or "-", cell_style),
            Paragraph(err_msg or "-", cell_style)
        ])
        
    # If no emissions found
    if len(emissions) == 0:
        table_data.append([
            Paragraph("Nenhuma emissão registrada para esta competência.", cell_style),
            "", "", "", "", "", "", "", "", "", "", ""
        ])
    
    # Col widths sum up to 684 pt (landscape Letter width 792 - 108 margin)
    det_table = Table(table_data, colWidths=[70, 60, 38, 48, 48, 44, 92, 78, 78, 50, 58, 120])
    
    # Detailed Table Styling
    det_table_style = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#EDF2F7")),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor("#1A365D")),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E0")),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]
    
    # Alternate row background colors
    for i in range(1, len(table_data)):
        if i % 2 == 0:
            det_table_style.append(('BACKGROUND', (0, i), (-1, i), colors.HexColor("#F7FAFC")))
            
    # Span row for empty emissions message
    if len(emissions) == 0:
        det_table_style.append(('SPAN', (0, 1), (-1, 1)))
        det_table_style.append(('ALIGN', (0, 1), (-1, 1), 'CENTER'))
        
    det_table.setStyle(TableStyle(det_table_style))
    story.append(det_table)
    
    # Build Document using custom NumberedCanvas
    # Set the canvas dynamic properties inside standard canvasmaker call
    def canvas_maker(*args, **kwargs):
        canvas = NumberedCanvas(*args, **kwargs)
        canvas.competence_str = competence
        return canvas

    doc.build(story, canvasmaker=canvas_maker)
    
    return report_path

if __name__ == "__main__":
    # Test report generation
    print("Generating test report...")
    p = generate_pdf_report("05/2026")
    print(f"Report generated successfully at: {p}")
