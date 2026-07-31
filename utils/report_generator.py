"""
Gerador de Relatórios HTML — NetWatch Pro v2.11

Gera relatórios profissionais em HTML que podem ser:
  • Abertos no navegador
  • Impressos como PDF (Ctrl+P → Salvar como PDF)
  • Enviados por email

Criado por Lucas Veríssimo
"""
import os
import webbrowser
from datetime import datetime
from pathlib import Path
from config import APP_VERSION, APP_AUTHOR, DATA_DIR
from utils.logger import setup_logger

logger = setup_logger("report")

REPORTS_DIR = DATA_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def generate_sla_report(sla_data: list[dict], period_days: int = 30,
                         title: str = "") -> str:
    """
    Gera relatório SLA em HTML para todos os hosts.

    Args:
        sla_data: lista de dicts retornados por calculate_group_sla()
        period_days: período do relatório
        title: título customizado (ex: nome da empresa)

    Returns:
        Caminho do arquivo HTML gerado.
    """
    now = datetime.now()
    report_title = title or "Relatório de Disponibilidade (SLA)"
    filename = f"sla_report_{now.strftime('%Y%m%d_%H%M%S')}.html"
    filepath = REPORTS_DIR / filename

    # Agrupa por grupo
    groups = {}
    for item in sla_data:
        g = item.get("group_name", "Geral")
        groups.setdefault(g, []).append(item)

    # Calcula totais globais
    total_hosts = len(sla_data)
    if sla_data:
        global_uptime = sum(s["uptime_pct"] for s in sla_data) / total_hosts
        total_outages = sum(s["total_outages"] for s in sla_data)
        avg_mttr = sum(s["mttr_s"] for s in sla_data if s["mttr_s"] > 0)
        mttr_count = sum(1 for s in sla_data if s["mttr_s"] > 0)
        avg_mttr = avg_mttr / mttr_count if mttr_count > 0 else 0
    else:
        global_uptime = 0
        total_outages = 0
        avg_mttr = 0

    # ── Gera HTML ──────────────────────────────────────────────────
    # CORREÇÃO v2.12 — escape de HTML em todos os campos dinâmicos (anti-XSS)
    from utils.security import html_escape as _h

    rows_html = ""
    for item in sla_data:
        uptime = item["uptime_pct"]
        color = "#10B981" if uptime >= 99.5 else "#F59E0B" if uptime >= 98 else "#EF4444"
        sla_99 = "✅" if uptime >= 99.0 else "❌"
        sla_995 = "✅" if uptime >= 99.5 else "❌"
        sla_999 = "✅" if uptime >= 99.9 else "❌"

        rows_html += f"""
        <tr>
            <td>{_h(item.get('host_label', item.get('host_ip', '')))}</td>
            <td>{_h(item.get('host_ip', ''))}</td>
            <td>{_h(item.get('group_name', ''))}</td>
            <td style="color:{color};font-weight:bold">{uptime:.2f}%</td>
            <td>{item['total_outages']}</td>
            <td>{_h(item['total_downtime_str'])}</td>
            <td>{_h(item['mtbf_str'])}</td>
            <td>{_h(item['mttr_str'])}</td>
            <td>{_h(item['longest_outage_str'])}</td>
            <td>{sla_999}</td>
            <td>{sla_995}</td>
            <td>{sla_99}</td>
        </tr>"""

    # Outages detail
    outage_rows = ""
    for item in sla_data:
        for o in item.get("outages", []):
            outage_rows += f"""
            <tr>
                <td>{_h(item.get('host_label', ''))}</td>
                <td>{_h(o.get('start_str', '—'))}</td>
                <td>{_h(o.get('end_str', '—'))}</td>
                <td>{_h(o.get('duration_str', '—'))}</td>
            </tr>"""

    from utils.reliability import _format_duration

    html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{report_title}</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #0f1117;
           color: #e6edf3; padding: 40px; }}
    .header {{ text-align: center; margin-bottom: 40px; border-bottom: 2px solid #3b82f6;
               padding-bottom: 20px; }}
    .header h1 {{ font-size: 28px; color: #3b82f6; }}
    .header p {{ color: #8b949e; margin-top: 8px; }}
    .summary {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px;
                margin-bottom: 32px; }}
    .summary-card {{ background: #161b22; border-radius: 12px; padding: 20px;
                     text-align: center; border: 1px solid #2a3040; }}
    .summary-card .value {{ font-size: 32px; font-weight: bold; }}
    .summary-card .label {{ color: #8b949e; font-size: 12px; margin-top: 4px; }}
    .green {{ color: #10b981; }}
    .yellow {{ color: #f59e0b; }}
    .red {{ color: #ef4444; }}
    .blue {{ color: #3b82f6; }}
    table {{ width: 100%; border-collapse: collapse; margin-bottom: 32px; }}
    th {{ background: #161b22; color: #8b949e; font-size: 11px; text-transform: uppercase;
         padding: 12px 8px; text-align: left; border-bottom: 2px solid #2a3040; }}
    td {{ padding: 10px 8px; border-bottom: 1px solid #1c2333; font-size: 13px; }}
    tr:hover {{ background: #1c2333; }}
    h2 {{ color: #e6edf3; font-size: 20px; margin: 32px 0 16px; padding-bottom: 8px;
         border-bottom: 1px solid #2a3040; }}
    .footer {{ text-align: center; color: #484f58; font-size: 11px; margin-top: 40px;
               padding-top: 20px; border-top: 1px solid #2a3040; }}
    @media print {{
        body {{ background: white; color: #1a1a1a; padding: 20px; }}
        .summary-card {{ border: 1px solid #ddd; }}
        th {{ background: #f0f0f0; color: #333; border-bottom: 2px solid #333; }}
        td {{ border-bottom: 1px solid #ddd; }}
        tr:hover {{ background: transparent; }}
        .green {{ color: #059669; }}
        .yellow {{ color: #d97706; }}
        .red {{ color: #dc2626; }}
        .blue {{ color: #2563eb; }}
        .header {{ border-bottom-color: #2563eb; }}
        .header h1 {{ color: #2563eb; }}
        h2 {{ color: #1a1a1a; border-bottom-color: #ddd; }}
    }}
</style>
</head>
<body>
<div class="header">
    <h1>📊 {_h(report_title)}</h1>
    <p>Período: últimos {period_days} dias — Gerado em {now.strftime('%d/%m/%Y %H:%M')}</p>
    <p>NetWatch Pro v{APP_VERSION} — {APP_AUTHOR}</p>
</div>

<div class="summary">
    <div class="summary-card">
        <div class="value blue">{total_hosts}</div>
        <div class="label">HOSTS MONITORADOS</div>
    </div>
    <div class="summary-card">
        <div class="value {'green' if global_uptime >= 99 else 'yellow' if global_uptime >= 95 else 'red'}">{global_uptime:.2f}%</div>
        <div class="label">DISPONIBILIDADE MÉDIA</div>
    </div>
    <div class="summary-card">
        <div class="value {'green' if total_outages < 5 else 'yellow' if total_outages < 20 else 'red'}">{total_outages}</div>
        <div class="label">TOTAL DE QUEDAS</div>
    </div>
    <div class="summary-card">
        <div class="value">{_format_duration(avg_mttr)}</div>
        <div class="label">MTTR MÉDIO</div>
    </div>
</div>

<h2>Disponibilidade por Host</h2>
<table>
<thead>
    <tr>
        <th>Host</th><th>IP</th><th>Grupo</th><th>Uptime</th>
        <th>Quedas</th><th>Downtime</th><th>MTBF</th><th>MTTR</th>
        <th>Maior Queda</th><th>99.9%</th><th>99.5%</th><th>99.0%</th>
    </tr>
</thead>
<tbody>
{rows_html}
</tbody>
</table>

<h2>Histórico de Interrupções</h2>
<table>
<thead>
    <tr><th>Host</th><th>Início</th><th>Fim</th><th>Duração</th></tr>
</thead>
<tbody>
{outage_rows if outage_rows else '<tr><td colspan="4" style="text-align:center;color:#8b949e">Nenhuma interrupção no período</td></tr>'}
</tbody>
</table>

<div class="footer">
    <p>Relatório gerado automaticamente pelo NetWatch Pro v{APP_VERSION}</p>
    <p>Para salvar como PDF: Ctrl+P → "Salvar como PDF"</p>
</div>
</body>
</html>"""

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"Relatório SLA gerado: {filepath}")
        return str(filepath)
    except Exception as e:
        logger.error(f"Erro ao gerar relatório: {e}")
        return ""


def open_report(filepath: str):
    """Abre o relatório no navegador padrão."""
    try:
        webbrowser.open(f"file:///{filepath.replace(os.sep, '/')}")
    except Exception as e:
        logger.error(f"Erro ao abrir relatório: {e}")
