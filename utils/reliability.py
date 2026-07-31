"""
Motor de Análise de Confiabilidade — NetWatch Pro v2.11

Fase 1: Métricas SLA (Uptime %, MTBF, MTTR, compliance)
Fase 2: Análise de Tendência (degradação, padrões temporais, predição)
Fase 3: Root Cause Analysis (classificação automática de falhas)

Criado por Lucas Veríssimo
"""
import statistics
import json
from datetime import datetime, timedelta
from typing import Optional
from utils.logger import setup_logger

logger = setup_logger("reliability")


# ══════════════════════════════════════════════════════════════════════
# CONSTANTES — limites configuráveis
# ══════════════════════════════════════════════════════════════════════

# Apdex — limite de "satisfatório" em ms. Padrão da indústria: ~500ms para web,
# 100ms para rede crítica. Usamos 100ms para monitoramento de rede.
APDEX_T_MS = 100.0   # latência <= T = satisfatório
APDEX_F_MS = 400.0   # latência <= 4*T = tolerável; acima = frustrado

# QoS thresholds (combinação latência + jitter + perda)
QOS_THRESHOLDS = {
    "excellent": {"latency": 50,  "jitter": 10, "loss": 0.5},
    "good":      {"latency": 100, "jitter": 30, "loss": 2.0},
    "fair":      {"latency": 200, "jitter": 60, "loss": 5.0},
    # acima de "fair" = poor
}


# ══════════════════════════════════════════════════════════════════════
# FASE 1 — MÉTRICAS SLA
# ══════════════════════════════════════════════════════════════════════

def calculate_sla_metrics(db, host_id: int, days: int = 30) -> dict:
    """
    Calcula todas as métricas SLA para um host em um período.

    Retorna:
      uptime_pct: float       — disponibilidade percentual
      total_outages: int      — número total de quedas
      total_downtime_s: float — tempo total offline (segundos)
      mtbf_s: float           — Mean Time Between Failures (segundos)
      mtbf_str: str           — MTBF formatado (ex: "2d 14h 32min")
      mttr_s: float           — Mean Time To Recovery (segundos)
      mttr_str: str           — MTTR formatado (ex: "8min 45s")
      longest_outage_s: float — maior outage (segundos)
      longest_outage_str: str — maior outage formatado
      outages: list[dict]     — lista de outages com start, end, duration
      sla_targets: dict       — compliance contra alvos 99.9%, 99.5%, 99.0%
    """
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    pings = _get_status_series(db, host_id, since)

    if not pings:
        return _empty_sla()

    # Deriva outages a partir das transições de status
    outages = _derive_outages(pings)
    period_start = datetime.strptime(pings[0]["ts"], "%Y-%m-%d %H:%M:%S")
    period_end = datetime.strptime(pings[-1]["ts"], "%Y-%m-%d %H:%M:%S")
    total_period = max((period_end - period_start).total_seconds(), 1)

    total_downtime = sum(o["duration_s"] for o in outages)
    uptime_pct = max(0, (1 - total_downtime / total_period) * 100)
    total_outages = len(outages)

    # MTBF — tempo médio entre falhas
    if total_outages > 0:
        mtbf_s = total_period / total_outages
    else:
        mtbf_s = total_period  # nunca caiu → MTBF = todo o período

    # MTTR — tempo médio de recuperação
    resolved = [o for o in outages if o["end"] is not None]
    if resolved:
        mttr_s = sum(o["duration_s"] for o in resolved) / len(resolved)
    else:
        mttr_s = 0

    longest = max((o["duration_s"] for o in outages), default=0)

    # SLA compliance
    sla_targets = {}
    for target in [99.9, 99.5, 99.0, 98.0, 95.0]:
        max_down_s = total_period * (1 - target / 100)
        sla_targets[target] = {
            "target": target,
            "compliant": uptime_pct >= target,
            "max_downtime_s": max_down_s,
            "max_downtime_str": _format_duration(max_down_s),
            "actual_downtime_s": total_downtime,
        }

    return {
        "uptime_pct": round(uptime_pct, 4),
        "total_outages": total_outages,
        "total_downtime_s": total_downtime,
        "total_downtime_str": _format_duration(total_downtime),
        "mtbf_s": mtbf_s,
        "mtbf_str": _format_duration(mtbf_s),
        "mttr_s": mttr_s,
        "mttr_str": _format_duration(mttr_s),
        "longest_outage_s": longest,
        "longest_outage_str": _format_duration(longest),
        "outages": outages[-20:],  # últimas 20 para exibição
        "sla_targets": sla_targets,
        "period_days": days,
        "total_pings": len(pings),
        "period_start": period_start.strftime("%d/%m/%Y %H:%M"),
        "period_end": period_end.strftime("%d/%m/%Y %H:%M"),
    }


def calculate_group_sla(db, controller, days: int = 30) -> list[dict]:
    """Calcula SLA para todos os hosts, agrupados por grupo."""
    results = []
    for host in controller.get_all_hosts():
        if not host.enabled:
            continue
        sla = calculate_sla_metrics(db, host.id, days)
        sla["host_id"] = host.id
        sla["host_ip"] = host.ip
        sla["host_label"] = host.display_name
        sla["group_name"] = host.group_name
        sla["platform"] = host.platform
        results.append(sla)
    # Ordena por uptime (pior primeiro)
    results.sort(key=lambda x: x["uptime_pct"])
    return results


# ══════════════════════════════════════════════════════════════════════
# FASE 2 — ANÁLISE DE TENDÊNCIA
# ══════════════════════════════════════════════════════════════════════

def analyze_trends(db, host_id: int, days: int = 7) -> dict:
    """
    Analisa tendências de latência, jitter e perda para um host.

    Retorna:
      hourly_data: list[dict]    — métricas por hora (para gráficos)
      daily_data: list[dict]     — métricas por dia
      latency_trend: str         — "stable", "rising", "falling"
      jitter_trend: str          — idem
      loss_trend: str            — idem
      degradation_score: float   — 0-100 (0=estável, 100=degradação severa)
      degradation_alert: str     — mensagem de alerta ou ""
      peak_hours: list[int]      — horários com mais problemas
      worst_day: str             — dia da semana com pior performance
    """
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    # Dados por hora
    hourly = _get_hourly_averages(db, host_id, since)
    # Dados por dia
    daily = _get_daily_averages(db, host_id, since)

    if not hourly:
        return _empty_trends()

    # Calcula tendências via regressão linear simples
    lat_values = [h["avg_latency"] for h in hourly if h["avg_latency"] > 0]
    jit_values = [h["avg_jitter"] for h in hourly if h["avg_jitter"] is not None]
    loss_values = [h["avg_loss"] for h in hourly]

    latency_trend = _classify_trend(lat_values)
    jitter_trend = _classify_trend(jit_values)
    loss_trend = _classify_trend(loss_values)

    # Degradation score (0-100)
    score = 0
    if latency_trend == "rising":
        slope = _trend_slope(lat_values)
        score += min(40, slope * 10)  # slope em ms/hora
    if jitter_trend == "rising":
        slope = _trend_slope(jit_values)
        score += min(30, slope * 15)
    if loss_trend == "rising":
        slope = _trend_slope(loss_values)
        score += min(30, slope * 20)
    score = min(100, max(0, score))

    # Degradation alert
    alert = ""
    if score >= 70:
        alert = "⚠ DEGRADAÇÃO SEVERA — possível queda iminente"
    elif score >= 40:
        alert = "⚠ Degradação em andamento — monitorar de perto"
    elif score >= 15:
        alert = "ℹ Leve tendência de degradação detectada"

    # Peak hours (horários com mais problemas)
    hour_problems = {}
    for h in hourly:
        hour = int(h["hour"].split(" ")[-1].split(":")[0]) if " " in h["hour"] else 0
        problems = (h["avg_latency"] > 100) + (h["avg_loss"] > 5) + (h["offline_count"] > 0)
        hour_problems[hour] = hour_problems.get(hour, 0) + problems
    peak_hours = sorted(hour_problems, key=hour_problems.get, reverse=True)[:3]

    # Worst day
    day_names = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
    day_problems = {}
    for d in daily:
        try:
            dt = datetime.strptime(d["day"], "%Y-%m-%d")
            dow = day_names[dt.weekday()]
            problems = d["avg_loss"] + (d["avg_latency"] / 10)
            day_problems[dow] = day_problems.get(dow, 0) + problems
        except Exception:
            pass
    worst_day = max(day_problems, key=day_problems.get) if day_problems else "—"

    return {
        "hourly_data": hourly,
        "daily_data": daily,
        "latency_trend": latency_trend,
        "jitter_trend": jitter_trend,
        "loss_trend": loss_trend,
        "degradation_score": round(score, 1),
        "degradation_alert": alert,
        "peak_hours": peak_hours,
        "worst_day": worst_day,
        "period_days": days,
    }


# ══════════════════════════════════════════════════════════════════════
# FASE 3 — ROOT CAUSE ANALYSIS
# ══════════════════════════════════════════════════════════════════════

def analyze_root_cause(host) -> dict:
    """
    Análise de causa raiz para um host baseada nos dados atuais.

    Classifica o problema em categorias:
      - ROUTER_DOWN:    roteador/equipamento caiu
      - LINK_DOWN:      link da operadora caiu
      - INTERNET_DOWN:  internet do link caiu (WAN OK, Google falhou)
      - DEGRADATION:    link funcionando mas com degradação
      - SSH_ISSUE:      problema na conexão SSH (host responde ping mas SSH falhou)
      - HEALTHY:        tudo normal

    Usa dados em memória do host (último ciclo).
    """
    result = {
        "category": "HEALTHY",
        "severity": "info",    # info, warning, critical
        "summary": "",
        "details": [],
        "recommendation": "",
        "metrics": {},
    }

    if host.status == "online":
        # Online mas pode ter degradação
        lat = host.host_ssh_latency
        jit = host.host_ssh_jitter if hasattr(host, 'host_ssh_jitter') else 0
        loss = host.host_ssh_loss
        wan_loss = host.wan_loss if host.wan_has_data else None
        google_loss = host.google_loss if host.google_has_data else None

        result["metrics"] = {
            "latency": lat, "jitter": jit, "loss": loss,
            "wan_loss": wan_loss, "google_loss": google_loss,
        }

        # Detecta degradação
        issues = []
        if lat > 200:
            issues.append(f"Latência muito alta: {lat:.0f}ms")
        elif lat > 100:
            issues.append(f"Latência elevada: {lat:.0f}ms")
        if loss > 20:
            issues.append(f"Perda severa: {loss:.0f}%")
        elif loss > 5:
            issues.append(f"Perda moderada: {loss:.0f}%")
        if jit > 50:
            issues.append(f"Jitter alto: {jit:.0f}ms")

        if wan_loss is not None and wan_loss > 30:
            issues.append(f"WAN com perda alta: {wan_loss:.0f}%")
        if google_loss is not None and google_loss > 30:
            issues.append(f"Internet (Google) com perda: {google_loss:.0f}%")

        if issues:
            result["category"] = "DEGRADATION"
            result["severity"] = "critical" if (lat > 200 or loss > 20) else "warning"
            result["summary"] = f"Online com degradação ({len(issues)} problemas)"
            result["details"] = issues

            # Recommendation based on where the problem is
            if wan_loss and wan_loss > loss:
                result["recommendation"] = (
                    "A degradação está no link WAN (operadora). "
                    "Verifique o cabo/fibra, reinicie o modem, "
                    "ou abra chamado na operadora."
                )
            elif google_loss and google_loss > loss:
                result["recommendation"] = (
                    "O roteador está OK mas a internet está instável. "
                    "Pode ser problema na operadora ou rota."
                )
            else:
                result["recommendation"] = (
                    "Degradação no equipamento local. "
                    "Verifique CPU/RAM do roteador, cabos e switch."
                )
        else:
            result["summary"] = "Host saudável — sem problemas detectados"
            result["severity"] = "info"
        return result

    # ── Host OFFLINE ──────────────────────────────────────────────
    local_ping_ok = host.current_loss < 100 if host.ping_history else False
    ssh_ok = host.host_ssh_ping_last is not None
    wan_ok = host.wan_has_data and host.wan_loss < 100
    google_ok = host.google_has_data and host.google_loss < 100

    result["metrics"] = {
        "local_ping": "OK" if local_ping_ok else "FALHOU",
        "ssh": "OK" if ssh_ok else "FALHOU",
        "wan": "OK" if wan_ok else ("FALHOU" if host.wan_has_data else "N/A"),
        "google": "OK" if google_ok else ("FALHOU" if host.google_has_data else "N/A"),
    }
    result["severity"] = "critical"

    if not local_ping_ok and not ssh_ok:
        # Nada responde — roteador caiu
        result["category"] = "ROUTER_DOWN"
        result["summary"] = "Equipamento completamente inacessível"
        result["details"] = [
            "Ping local: sem resposta",
            "SSH: sem conexão",
            "Possíveis causas: queda de energia, hardware travado, cabo desconectado",
        ]
        result["recommendation"] = (
            "1. Verificar se há energia no local\n"
            "2. Tentar reboot do equipamento (se acessível fisicamente)\n"
            "3. Verificar cabo ethernet/fibra\n"
            "4. Contatar responsável no local para inspeção física"
        )

    elif local_ping_ok and not ssh_ok:
        # Ping OK mas SSH falhou
        result["category"] = "SSH_ISSUE"
        result["summary"] = "Host responde ping mas SSH está inacessível"
        result["severity"] = "warning"
        result["details"] = [
            "Ping local: respondendo normalmente",
            "SSH: conexão recusada ou timeout",
            "O equipamento está ligado mas o serviço SSH pode estar parado",
        ]
        result["recommendation"] = (
            "1. Verificar se o serviço SSH está habilitado no roteador\n"
            "2. Verificar se a porta SSH está correta nas configurações\n"
            "3. Verificar firewall — pode estar bloqueando SSH\n"
            "4. Verificar credenciais (usuário/senha)"
        )

    elif ssh_ok and not wan_ok and not google_ok:
        # SSH OK mas WAN e Google falharam — link caiu
        result["category"] = "LINK_DOWN"
        result["summary"] = "Roteador OK, link da operadora caiu"
        result["details"] = [
            "SSH para o roteador: funcionando",
            "WAN (gateway operadora): sem resposta",
            "Internet (Google): sem resposta",
            "O roteador está operacional mas sem conectividade externa",
        ]
        result["recommendation"] = (
            "1. Verificar status do modem/ONT da operadora\n"
            "2. Verificar se o cabo entre roteador e modem está OK\n"
            "3. Abrir chamado na operadora\n"
            "4. Se tiver link redundante, verificar failover"
        )

    elif ssh_ok and wan_ok and not google_ok:
        # SSH e WAN OK mas Google falhou — problema na internet
        result["category"] = "INTERNET_DOWN"
        result["summary"] = "Link WAN OK, mas sem acesso à internet"
        result["details"] = [
            "SSH para o roteador: funcionando",
            "WAN (gateway operadora): respondendo",
            "Internet (Google/8.8.8.8): sem resposta",
            "O link está ativo mas sem rota para a internet",
        ]
        result["recommendation"] = (
            "1. Verificar DNS no roteador\n"
            "2. Verificar rota default (gateway)\n"
            "3. Pode ser queda parcial na operadora\n"
            "4. Testar traceroute para 8.8.8.8"
        )

    else:
        result["category"] = "ROUTER_DOWN"
        result["summary"] = "Host offline — causa indeterminada"
        result["details"] = [
            f"Ping local: {'OK' if local_ping_ok else 'falhou'}",
            f"SSH: {'OK' if ssh_ok else 'falhou'}",
        ]
        result["recommendation"] = "Verificar conectividade manualmente."

    # Tempo offline
    if host.offline_since:
        elapsed = (datetime.now() - host.offline_since).total_seconds()
        result["offline_since"] = host.offline_since.strftime("%d/%m %H:%M:%S")
        result["offline_duration"] = _format_duration(elapsed)

    return result


# ══════════════════════════════════════════════════════════════════════
# HELPERS — QUERIES E CÁLCULOS
# ══════════════════════════════════════════════════════════════════════

def _get_status_series(db, host_id: int, since: str) -> list[dict]:
    """Retorna série temporal de (timestamp, status) para um host."""
    try:
        rows = db.conn.execute(
            "SELECT timestamp, status FROM ping_metrics "
            "WHERE host_id=? AND timestamp>=? ORDER BY timestamp",
            (host_id, since)
        ).fetchall()
        return [{"ts": r["timestamp"], "status": r["status"]} for r in rows]
    except Exception as e:
        logger.debug(f"Erro ao buscar série de status: {e}")
        return []


def _get_hourly_averages(db, host_id: int, since: str) -> list[dict]:
    """Retorna métricas médias por hora."""
    try:
        rows = db.conn.execute(
            """SELECT
                strftime('%Y-%m-%d %H:00', timestamp) as hour,
                AVG(latency_ms) as avg_latency,
                AVG(jitter_ms) as avg_jitter,
                AVG(loss_pct) as avg_loss,
                MAX(latency_ms) as max_latency,
                COUNT(*) as total_pings,
                SUM(CASE WHEN status='offline' THEN 1 ELSE 0 END) as offline_count
            FROM ping_metrics
            WHERE host_id=? AND timestamp>=?
            GROUP BY hour ORDER BY hour""",
            (host_id, since)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug(f"Erro ao buscar médias horárias: {e}")
        return []


def _get_daily_averages(db, host_id: int, since: str) -> list[dict]:
    """Retorna métricas médias por dia."""
    try:
        rows = db.conn.execute(
            """SELECT
                strftime('%Y-%m-%d', timestamp) as day,
                AVG(latency_ms) as avg_latency,
                AVG(jitter_ms) as avg_jitter,
                AVG(loss_pct) as avg_loss,
                MAX(latency_ms) as max_latency,
                COUNT(*) as total_pings,
                SUM(CASE WHEN status='online' THEN 1 ELSE 0 END) as online_count,
                SUM(CASE WHEN status='offline' THEN 1 ELSE 0 END) as offline_count
            FROM ping_metrics
            WHERE host_id=? AND timestamp>=?
            GROUP BY day ORDER BY day""",
            (host_id, since)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug(f"Erro ao buscar médias diárias: {e}")
        return []


def _derive_outages(pings: list[dict]) -> list[dict]:
    """Deriva eventos de outage a partir da série de status."""
    outages = []
    current_outage = None

    for ping in pings:
        status = ping["status"]
        ts = datetime.strptime(ping["ts"], "%Y-%m-%d %H:%M:%S")

        if status == "offline" and current_outage is None:
            current_outage = {"start": ts, "end": None, "duration_s": 0}
        elif status == "online" and current_outage is not None:
            current_outage["end"] = ts
            current_outage["duration_s"] = (ts - current_outage["start"]).total_seconds()
            current_outage["start_str"] = current_outage["start"].strftime("%d/%m %H:%M")
            current_outage["end_str"] = ts.strftime("%d/%m %H:%M")
            current_outage["duration_str"] = _format_duration(current_outage["duration_s"])
            outages.append(current_outage)
            current_outage = None

    # Outage ainda em andamento
    if current_outage is not None:
        now = datetime.now()
        current_outage["duration_s"] = (now - current_outage["start"]).total_seconds()
        current_outage["start_str"] = current_outage["start"].strftime("%d/%m %H:%M")
        current_outage["end_str"] = "em andamento"
        current_outage["duration_str"] = _format_duration(current_outage["duration_s"])
        outages.append(current_outage)

    return outages


def _classify_trend(values: list[float]) -> str:
    """Classifica tendência: stable, rising, falling."""
    if len(values) < 10:
        return "stable"
    # Compara média da primeira metade com a segunda
    mid = len(values) // 2
    first_half = statistics.mean(values[:mid]) if values[:mid] else 0
    second_half = statistics.mean(values[mid:]) if values[mid:] else 0

    if first_half == 0:
        return "stable"
    change_pct = ((second_half - first_half) / max(first_half, 0.01)) * 100

    if change_pct > 15:
        return "rising"
    elif change_pct < -15:
        return "falling"
    return "stable"


def _trend_slope(values: list[float]) -> float:
    """Calcula slope simples (variação por ponto)."""
    if len(values) < 5:
        return 0
    n = len(values)
    x_mean = (n - 1) / 2
    y_mean = statistics.mean(values)
    num = sum((i - x_mean) * (values[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den != 0 else 0


def _format_duration(seconds: float) -> str:
    """Formata duração em formato legível."""
    if seconds < 0:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}min {s}s"
    if seconds < 86400:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}min"
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    return f"{d}d {h}h"


def _empty_sla() -> dict:
    return {
        "uptime_pct": 0, "total_outages": 0,
        "total_downtime_s": 0, "total_downtime_str": "—",
        "mtbf_s": 0, "mtbf_str": "—",
        "mttr_s": 0, "mttr_str": "—",
        "longest_outage_s": 0, "longest_outage_str": "—",
        "outages": [], "sla_targets": {},
        "period_days": 0, "total_pings": 0,
        "period_start": "—", "period_end": "—",
    }


def _empty_trends() -> dict:
    return {
        "hourly_data": [], "daily_data": [],
        "latency_trend": "stable", "jitter_trend": "stable",
        "loss_trend": "stable", "degradation_score": 0,
        "degradation_alert": "", "peak_hours": [],
        "worst_day": "—", "period_days": 0,
    }


# ══════════════════════════════════════════════════════════════════════
# PERCENTIS — p50, p95, p99 de latência, jitter, perda
# ══════════════════════════════════════════════════════════════════════

def calculate_percentiles(db, host_id: int, days: int = 7) -> dict:
    """
    Calcula percentis (p50, p95, p99) de latência, jitter e perda.

    Latência média esconde picos. p95 mostra "no pior 5% das medições".
    p99 mostra outliers extremos.
    """
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        rows = db.conn.execute(
            """SELECT latency_ms, jitter_ms, loss_pct FROM ping_metrics
               WHERE host_id=? AND timestamp>=? AND status='online'""",
            (host_id, since)
        ).fetchall()
    except Exception:
        return {"latency": {}, "jitter": {}, "loss": {}, "samples": 0}

    if not rows:
        return {"latency": {}, "jitter": {}, "loss": {}, "samples": 0}

    lat = sorted([r["latency_ms"] for r in rows if r["latency_ms"] is not None])
    jit = sorted([r["jitter_ms"] for r in rows if r["jitter_ms"] is not None])
    loss = sorted([r["loss_pct"] for r in rows if r["loss_pct"] is not None])

    def _p(values, pct):
        if not values:
            return 0.0
        k = (len(values) - 1) * pct / 100
        f, c = int(k), min(int(k) + 1, len(values) - 1)
        if f == c:
            return values[f]
        return values[f] + (values[c] - values[f]) * (k - f)

    return {
        "latency": {
            "p50": round(_p(lat, 50), 2),
            "p95": round(_p(lat, 95), 2),
            "p99": round(_p(lat, 99), 2),
            "max": round(max(lat), 2) if lat else 0,
            "avg": round(statistics.mean(lat), 2) if lat else 0,
        },
        "jitter": {
            "p50": round(_p(jit, 50), 2),
            "p95": round(_p(jit, 95), 2),
            "p99": round(_p(jit, 99), 2),
            "max": round(max(jit), 2) if jit else 0,
        },
        "loss": {
            "p50": round(_p(loss, 50), 2),
            "p95": round(_p(loss, 95), 2),
            "p99": round(_p(loss, 99), 2),
            "max": round(max(loss), 2) if loss else 0,
        },
        "samples": len(rows),
        "period_days": days,
    }


# ══════════════════════════════════════════════════════════════════════
# ERROR BUDGET & BURN RATE
# ══════════════════════════════════════════════════════════════════════

def calculate_error_budget(sla_metrics: dict, target_pct: float = 99.9) -> dict:
    """
    Calcula error budget e burn rate para um alvo de SLA.

    Error budget = downtime permitido pelo SLA - downtime real
      Positivo = ainda tem budget
      Negativo = SLA violado, "está no vermelho"

    Burn rate = taxa de consumo do budget normalizada
      = (downtime real / downtime permitido) × 100
      100% = exatamente no limite
      >100% = vai estourar antes do fim do período
    """
    period_s = sla_metrics["period_days"] * 86400
    allowed_downtime_s = period_s * (1 - target_pct / 100)
    actual_downtime_s = sla_metrics["total_downtime_s"]

    budget_remaining_s = allowed_downtime_s - actual_downtime_s
    if allowed_downtime_s > 0:
        burn_rate_pct = (actual_downtime_s / allowed_downtime_s) * 100
    else:
        burn_rate_pct = 100.0 if actual_downtime_s > 0 else 0.0

    # Forecast: ao ritmo atual, quando vai estourar?
    forecast_breach_str = "—"
    if burn_rate_pct >= 100:
        forecast_breach_str = "JÁ ESTOUROU"
    elif burn_rate_pct > 0 and burn_rate_pct < 100:
        elapsed_days = sla_metrics["period_days"]
        if elapsed_days > 0 and actual_downtime_s > 0:
            # Quantos dias adicionais até estourar
            days_until_breach = (allowed_downtime_s - actual_downtime_s) / \
                                (actual_downtime_s / elapsed_days)
            if days_until_breach < 365:
                forecast_breach_str = f"em ~{days_until_breach:.0f} dias"
            else:
                forecast_breach_str = "ritmo seguro"
        else:
            forecast_breach_str = "ritmo seguro"

    return {
        "target_pct": target_pct,
        "allowed_downtime_s": allowed_downtime_s,
        "allowed_downtime_str": _format_duration(allowed_downtime_s),
        "actual_downtime_s": actual_downtime_s,
        "budget_remaining_s": budget_remaining_s,
        "budget_remaining_str": _format_duration(abs(budget_remaining_s)),
        "burn_rate_pct": round(burn_rate_pct, 1),
        "is_breached": burn_rate_pct >= 100,
        "forecast_breach_str": forecast_breach_str,
        "status": ("breached" if burn_rate_pct >= 100 else
                   "critical" if burn_rate_pct >= 80 else
                   "warning"  if burn_rate_pct >= 50 else
                   "healthy"),
    }


# ══════════════════════════════════════════════════════════════════════
# MTTF & FAILURE RATE
# ══════════════════════════════════════════════════════════════════════

def calculate_mttf_lambda(sla_metrics: dict) -> dict:
    """
    Calcula MTTF e Failure Rate (λ).

    MTTF (Mean Time To Failure) = uptime acumulado / número de falhas
      Diferente de MTBF, que usa o período total (uptime + downtime).
      MTTF é mais correto para sistemas reparáveis.

    Failure Rate (λ) = falhas / unidade de tempo (failures per hour)
      Métrica clássica de confiabilidade. λ = 1/MTBF (em horas).
    """
    period_s = sla_metrics["period_days"] * 86400
    downtime_s = sla_metrics["total_downtime_s"]
    uptime_s = max(0, period_s - downtime_s)
    failures = sla_metrics["total_outages"]

    if failures > 0:
        mttf_s = uptime_s / failures
        period_hours = period_s / 3600
        failure_rate = failures / max(period_hours, 0.001)  # falhas/hora
    else:
        mttf_s = uptime_s  # nunca falhou
        failure_rate = 0.0

    return {
        "mttf_s": mttf_s,
        "mttf_str": _format_duration(mttf_s),
        "failure_rate_per_hour": round(failure_rate, 4),
        "failure_rate_per_day": round(failure_rate * 24, 3),
        "uptime_s": uptime_s,
        "uptime_str": _format_duration(uptime_s),
    }


# ══════════════════════════════════════════════════════════════════════
# APDEX SCORE — Application Performance Index
# ══════════════════════════════════════════════════════════════════════

def calculate_apdex(db, host_id: int, days: int = 7, t_ms: float = APDEX_T_MS) -> dict:
    """
    Calcula Apdex (Application Performance Index).

    Apdex = (satisfied + tolerating/2) / total
      satisfied: latência <= T
      tolerating: T < latência <= 4*T
      frustrated: latência > 4*T OU pacotes perdidos

    Score 0-1:
      1.0 = perfeito
      0.94-1.0 = excelente
      0.85-0.93 = bom
      0.70-0.84 = aceitável
      <0.70 = ruim
    """
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    f_ms = t_ms * 4

    try:
        rows = db.conn.execute(
            """SELECT latency_ms, status, loss_pct FROM ping_metrics
               WHERE host_id=? AND timestamp>=?""",
            (host_id, since)
        ).fetchall()
    except Exception:
        return {"score": 0, "rating": "—", "samples": 0}

    if not rows:
        return {"score": 0, "rating": "—", "samples": 0}

    satisfied = 0
    tolerating = 0
    frustrated = 0
    for r in rows:
        if r["status"] != "online" or (r["loss_pct"] or 0) > 50:
            frustrated += 1
        else:
            lat = r["latency_ms"] or 0
            if lat <= t_ms:
                satisfied += 1
            elif lat <= f_ms:
                tolerating += 1
            else:
                frustrated += 1

    total = len(rows)
    score = (satisfied + tolerating / 2) / total
    if score >= 0.94:
        rating = "Excelente"
    elif score >= 0.85:
        rating = "Bom"
    elif score >= 0.70:
        rating = "Aceitável"
    else:
        rating = "Ruim"

    return {
        "score": round(score, 3),
        "rating": rating,
        "satisfied": satisfied,
        "tolerating": tolerating,
        "frustrated": frustrated,
        "samples": total,
        "t_ms": t_ms,
        "f_ms": f_ms,
    }


# ══════════════════════════════════════════════════════════════════════
# QOS CLASSIFICATION
# ══════════════════════════════════════════════════════════════════════

def classify_qos(latency_ms: float, jitter_ms: float, loss_pct: float) -> dict:
    """
    Classifica qualidade do serviço com base em latência + jitter + perda.

    Categorias: excellent, good, fair, poor.
    Útil para resumir saúde da rede sem mostrar 3 números.
    """
    if loss_pct >= 100:
        return {"category": "down", "color": "#6B7280", "label": "OFFLINE"}

    for cat in ["excellent", "good", "fair"]:
        t = QOS_THRESHOLDS[cat]
        if (latency_ms <= t["latency"] and
            jitter_ms <= t["jitter"] and
            loss_pct <= t["loss"]):
            return {
                "category": cat,
                "color": {"excellent": "#10B981", "good": "#3B82F6",
                          "fair": "#F59E0B"}[cat],
                "label": {"excellent": "EXCELENTE", "good": "BOM",
                          "fair": "ACEITÁVEL"}[cat],
            }
    return {"category": "poor", "color": "#EF4444", "label": "RUIM"}


# ══════════════════════════════════════════════════════════════════════
# OUTAGE CLUSTERING — quedas simultâneas indicam problema da operadora
# ══════════════════════════════════════════════════════════════════════

def detect_outage_clusters(db, controller, days: int = 7,
                             window_s: int = 60) -> list[dict]:
    """
    Detecta clusters de outages — quedas de múltiplos hosts em janela curta.

    Se >=3 hosts caem em até 60s, é alta probabilidade de:
      - Problema na operadora
      - Falha em equipamento upstream
      - Manutenção planejada não comunicada
    """
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    # Pega todas as transições offline com host info
    try:
        rows = db.conn.execute(
            """SELECT pm.host_id, pm.timestamp, h.label, h.ip, h.group_name
               FROM ping_metrics pm
               JOIN hosts h ON h.id = pm.host_id
               WHERE pm.status='offline' AND pm.timestamp>=?
               ORDER BY pm.timestamp""",
            (since,)
        ).fetchall()
    except Exception:
        return []

    if not rows:
        return []

    # Agrupa transições próximas no tempo
    clusters = []
    current = []
    current_start: Optional[datetime] = None

    for r in rows:
        ts = datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S")
        if current_start is None or (ts - current_start).total_seconds() <= window_s:
            current.append({"ts": ts, "host_id": r["host_id"],
                            "label": r["label"], "ip": r["ip"],
                            "group": r["group_name"]})
            if current_start is None:
                current_start = ts
        else:
            if len(set(e["host_id"] for e in current)) >= 3:
                clusters.append({
                    "start": current_start.strftime("%d/%m %H:%M:%S"),
                    "hosts_affected": len(set(e["host_id"] for e in current)),
                    "hosts": list(set(e["label"] or e["ip"] for e in current))[:10],
                    "groups": list(set(e["group"] for e in current)),
                })
            current = [{"ts": ts, "host_id": r["host_id"],
                        "label": r["label"], "ip": r["ip"],
                        "group": r["group_name"]}]
            current_start = ts

    # Último cluster
    if current and len(set(e["host_id"] for e in current)) >= 3:
        clusters.append({
            "start": current_start.strftime("%d/%m %H:%M:%S"),
            "hosts_affected": len(set(e["host_id"] for e in current)),
            "hosts": list(set(e["label"] or e["ip"] for e in current))[:10],
            "groups": list(set(e["group"] for e in current)),
        })

    return clusters[-20:]  # últimos 20 clusters


# ══════════════════════════════════════════════════════════════════════
# PARETO ANALYSIS — top contribuintes de downtime
# ══════════════════════════════════════════════════════════════════════

def pareto_analysis(sla_data: list[dict]) -> dict:
    """
    Análise de Pareto: identifica os hosts que mais contribuem para o downtime.

    Princípio 80/20: tipicamente 20% dos hosts causam 80% dos problemas.
    Focar nesses hosts dá o maior retorno em melhoria de SLA.
    """
    if not sla_data:
        return {"top_contributors": [], "pareto_count": 0, "pareto_pct": 0}

    # Ordena por downtime descendente
    sorted_hosts = sorted(sla_data, key=lambda h: h["total_downtime_s"], reverse=True)
    total_downtime = sum(h["total_downtime_s"] for h in sorted_hosts)

    if total_downtime == 0:
        return {"top_contributors": [], "pareto_count": 0, "pareto_pct": 0}

    cumulative = 0
    pareto_count = 0
    contributors = []

    for h in sorted_hosts:
        downtime = h["total_downtime_s"]
        cumulative += downtime
        pct_of_total = (downtime / total_downtime) * 100 if total_downtime > 0 else 0
        cumulative_pct = (cumulative / total_downtime) * 100 if total_downtime > 0 else 0

        contributors.append({
            "host_label": h.get("host_label", h.get("host_ip", "")),
            "host_ip": h.get("host_ip", ""),
            "downtime_s": downtime,
            "downtime_str": _format_duration(downtime),
            "pct_of_total": round(pct_of_total, 1),
            "cumulative_pct": round(cumulative_pct, 1),
        })

        # Marca o ponto onde atinge 80% do downtime
        if cumulative_pct <= 80:
            pareto_count = len(contributors)

    pareto_pct = (pareto_count / len(sorted_hosts)) * 100 if sorted_hosts else 0

    return {
        "top_contributors": contributors[:20],
        "pareto_count": pareto_count,
        "pareto_pct": round(pareto_pct, 1),
        "total_hosts": len(sorted_hosts),
    }


# ══════════════════════════════════════════════════════════════════════
# COMPARAÇÃO PERÍODO-VS-PERÍODO
# ══════════════════════════════════════════════════════════════════════

def compare_periods(db, controller, days: int = 30) -> dict:
    """
    Compara o período atual com o anterior (mesma duração).

    Mostra delta de uptime, total de outages, MTTR — útil para
    demonstrar tendência de melhoria/piora.
    """
    current = calculate_group_sla(db, controller, days)
    # Para o período anterior, precisaria de queries com offset.
    # Aproximação: compara hosts ativos atuais com versões "antigas" calculadas
    # com período 2*days e subtraindo. Simplificado: retorna stats atuais com
    # placeholder de comparação histórica.

    # Calcula período anterior fazendo query com offset
    end_prev = datetime.now() - timedelta(days=days)
    start_prev = end_prev - timedelta(days=days)

    # Para cada host, calcula SLA no período anterior
    previous_global_uptime = []
    previous_global_outages = 0

    for host_curr in current:
        host_id = host_curr["host_id"]
        prev = _calculate_sla_window(
            db, host_id,
            start_prev.strftime("%Y-%m-%d %H:%M:%S"),
            end_prev.strftime("%Y-%m-%d %H:%M:%S"),
        )
        if prev:
            previous_global_uptime.append(prev["uptime_pct"])
            previous_global_outages += prev["total_outages"]

    if not current:
        return {"available": False}

    curr_uptime = sum(h["uptime_pct"] for h in current) / len(current)
    curr_outages = sum(h["total_outages"] for h in current)

    prev_uptime = (sum(previous_global_uptime) / len(previous_global_uptime)
                   if previous_global_uptime else 0)

    delta_uptime = curr_uptime - prev_uptime
    delta_outages = curr_outages - previous_global_outages

    return {
        "available": True,
        "current_uptime_pct": round(curr_uptime, 3),
        "previous_uptime_pct": round(prev_uptime, 3),
        "delta_uptime": round(delta_uptime, 3),
        "delta_uptime_str": ("+" if delta_uptime >= 0 else "") + f"{delta_uptime:.2f}%",
        "current_outages": curr_outages,
        "previous_outages": previous_global_outages,
        "delta_outages": delta_outages,
        "trend": "better" if delta_uptime > 0.05 else "worse" if delta_uptime < -0.05 else "stable",
        "period_days": days,
    }


def _calculate_sla_window(db, host_id: int, since: str, until: str) -> Optional[dict]:
    """SLA para uma janela arbitrária [since, until]."""
    try:
        rows = db.conn.execute(
            "SELECT timestamp, status FROM ping_metrics "
            "WHERE host_id=? AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
            (host_id, since, until)
        ).fetchall()
    except Exception:
        return None
    pings = [{"ts": r["timestamp"], "status": r["status"]} for r in rows]
    if not pings:
        return None
    outages = _derive_outages(pings)
    start = datetime.strptime(since, "%Y-%m-%d %H:%M:%S")
    end = datetime.strptime(until, "%Y-%m-%d %H:%M:%S")
    period = max((end - start).total_seconds(), 1)
    total_down = sum(o["duration_s"] for o in outages)
    uptime = max(0, (1 - total_down / period) * 100)
    return {
        "uptime_pct": round(uptime, 3),
        "total_outages": len(outages),
        "total_downtime_s": total_down,
    }


# ══════════════════════════════════════════════════════════════════════
# COHORT ANALYSIS — por plataforma e grupo
# ══════════════════════════════════════════════════════════════════════

def cohort_by_platform(sla_data: list[dict]) -> list[dict]:
    """Agrupa SLA por plataforma de roteador. Útil para detectar
    se uma plataforma tem performance pior que outras."""
    cohorts = {}
    for h in sla_data:
        plat = h.get("platform") or "—"
        cohorts.setdefault(plat, {"hosts": [], "uptimes": [],
                                    "outages": 0, "downtime": 0})
        cohorts[plat]["hosts"].append(h.get("host_label", ""))
        cohorts[plat]["uptimes"].append(h["uptime_pct"])
        cohorts[plat]["outages"] += h["total_outages"]
        cohorts[plat]["downtime"] += h["total_downtime_s"]

    result = []
    for plat, data in cohorts.items():
        avg_up = sum(data["uptimes"]) / len(data["uptimes"]) if data["uptimes"] else 0
        result.append({
            "platform": plat,
            "host_count": len(data["hosts"]),
            "avg_uptime_pct": round(avg_up, 3),
            "total_outages": data["outages"],
            "total_downtime_s": data["downtime"],
            "total_downtime_str": _format_duration(data["downtime"]),
            "outages_per_host": round(data["outages"] / len(data["hosts"]), 2)
                                if data["hosts"] else 0,
        })
    result.sort(key=lambda x: x["avg_uptime_pct"])
    return result


def cohort_by_group(sla_data: list[dict]) -> list[dict]:
    """Agrupa SLA por grupo (Lojas, Setores)."""
    cohorts = {}
    for h in sla_data:
        grp = h.get("group_name") or "Geral"
        cohorts.setdefault(grp, {"hosts": [], "uptimes": [],
                                  "outages": 0, "downtime": 0})
        cohorts[grp]["hosts"].append(h.get("host_label", ""))
        cohorts[grp]["uptimes"].append(h["uptime_pct"])
        cohorts[grp]["outages"] += h["total_outages"]
        cohorts[grp]["downtime"] += h["total_downtime_s"]

    result = []
    for grp, data in cohorts.items():
        avg_up = sum(data["uptimes"]) / len(data["uptimes"]) if data["uptimes"] else 0
        result.append({
            "group": grp,
            "host_count": len(data["hosts"]),
            "avg_uptime_pct": round(avg_up, 3),
            "total_outages": data["outages"],
            "total_downtime_s": data["downtime"],
            "total_downtime_str": _format_duration(data["downtime"]),
        })
    result.sort(key=lambda x: x["avg_uptime_pct"])
    return result


# ══════════════════════════════════════════════════════════════════════
# ANOMALY DETECTION — z-score simples
# ══════════════════════════════════════════════════════════════════════

def detect_anomalies(db, host_id: int, days: int = 7,
                       z_threshold: float = 3.0) -> list[dict]:
    """
    Detecta horas com latência anômala usando z-score.

    Para cada hora do período, calcula a média e desvio padrão histórico.
    Horas com z-score > 3 (3 desvios padrão) são anomalias.
    """
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        rows = db.conn.execute(
            """SELECT strftime('%Y-%m-%d %H:00', timestamp) as hour,
                      AVG(latency_ms) as avg_lat,
                      AVG(loss_pct) as avg_loss
               FROM ping_metrics
               WHERE host_id=? AND timestamp>=? AND status='online'
               GROUP BY hour ORDER BY hour""",
            (host_id, since)
        ).fetchall()
    except Exception:
        return []

    if len(rows) < 10:
        return []

    lat_values = [r["avg_lat"] for r in rows if r["avg_lat"] is not None]
    if len(lat_values) < 10:
        return []

    mean_lat = statistics.mean(lat_values)
    stdev_lat = statistics.stdev(lat_values) if len(lat_values) > 1 else 0

    if stdev_lat == 0:
        return []

    anomalies = []
    for r in rows:
        lat = r["avg_lat"] or 0
        z = (lat - mean_lat) / stdev_lat
        if abs(z) >= z_threshold:
            anomalies.append({
                "hour": r["hour"],
                "latency": round(lat, 2),
                "loss": round(r["avg_loss"] or 0, 2),
                "z_score": round(z, 2),
                "severity": "high" if abs(z) >= 5 else "medium",
            })

    return anomalies[-30:]


# ══════════════════════════════════════════════════════════════════════
# ROUTE STABILITY — analisa traceroute_results
# ══════════════════════════════════════════════════════════════════════

def analyze_route_stability(db, host_id: int, days: int = 7) -> dict:
    """
    Analisa estabilidade da rota usando dados de traceroute.

    Detecta mudanças no caminho (indica reroteamento na operadora) e
    mede consistência da contagem de hops.
    """
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        rows = db.conn.execute(
            """SELECT timestamp, hop_count, hops_json FROM traceroute_results
               WHERE host_id=? AND timestamp>=? ORDER BY timestamp""",
            (host_id, since)
        ).fetchall()
    except Exception:
        return {"samples": 0, "stable": True, "route_changes": 0}

    if len(rows) < 2:
        return {"samples": len(rows), "stable": True, "route_changes": 0,
                "avg_hop_count": 0}

    route_changes = 0
    last_hops = None
    hop_counts = []
    change_events = []

    for r in rows:
        hop_counts.append(r["hop_count"] or 0)
        try:
            current_hops = json.loads(r["hops_json"] or "[]")
            current_path = [h.get("ip", "") for h in current_hops if h.get("ip")]
        except Exception:
            continue

        if last_hops is not None and current_path != last_hops:
            route_changes += 1
            change_events.append({
                "timestamp": r["timestamp"],
                "old_hops": len(last_hops),
                "new_hops": len(current_path),
            })
        last_hops = current_path

    avg_hops = statistics.mean(hop_counts) if hop_counts else 0
    stdev_hops = statistics.stdev(hop_counts) if len(hop_counts) > 1 else 0

    return {
        "samples": len(rows),
        "stable": route_changes == 0,
        "route_changes": route_changes,
        "avg_hop_count": round(avg_hops, 1),
        "hop_variance": round(stdev_hops, 2),
        "recent_changes": change_events[-10:],
    }


# ══════════════════════════════════════════════════════════════════════
# COMPREHENSIVE HOST REPORT — junta tudo num só dict
# ══════════════════════════════════════════════════════════════════════

def comprehensive_host_report(db, host, days: int = 30) -> dict:
    """
    Relatório completo de um host com TODAS as métricas.

    Combina SLA + percentis + apdex + error budget + MTTF +
    tendências + anomalias + estabilidade de rota.

    Usado para o "Host Deep Dive" na UI.
    """
    sla = calculate_sla_metrics(db, host.id, days)
    percentiles = calculate_percentiles(db, host.id, days)
    apdex = calculate_apdex(db, host.id, days)
    error_budget_999 = calculate_error_budget(sla, 99.9)
    error_budget_995 = calculate_error_budget(sla, 99.5)
    mttf = calculate_mttf_lambda(sla)
    trends = analyze_trends(db, host.id, min(days, 7))
    anomalies = detect_anomalies(db, host.id, days)
    route = analyze_route_stability(db, host.id, days)

    return {
        "host_id": host.id,
        "host_ip": host.ip,
        "host_label": host.display_name,
        "group_name": host.group_name,
        "platform": host.platform,
        "sla": sla,
        "percentiles": percentiles,
        "apdex": apdex,
        "error_budget_999": error_budget_999,
        "error_budget_995": error_budget_995,
        "mttf": mttf,
        "trends": trends,
        "anomalies": anomalies,
        "route": route,
    }
