"""
Perfis de dispositivos de rede — comandos SSH e parsers por plataforma.

Cada plataforma de roteador tem comandos diferentes para ping/traceroute.
Este módulo centraliza:
  • Constantes de plataforma
  • Templates de comandos SSH (exec e interativo)
  • Parsers de output específicos (MikroTik, Cisco)
  • Setup de conexão (ex: Cisco enable mode)

Plataformas suportadas:
  • pfSense / OPNsense (FreeBSD)
  • MikroTik RouterOS (CLI proprietário + fallback Linux)
  • Cisco IOS (com auto-enable)
  • Ubiquiti EdgeOS (Vyatta/Debian)
  • Linux genérico (Ubuntu, Debian, CentOS, etc.)

Criado por Lucas Veríssimo — NetWatch Pro v2.11
"""
import re
import statistics
from datetime import datetime
from typing import Optional

from utils.logger import setup_logger

logger = setup_logger("device_profiles")


# ══════════════════════════════════════════════════════════════════════
# CONSTANTES DE PLATAFORMA
# ══════════════════════════════════════════════════════════════════════

PLATFORM_PFSENSE   = "pfsense"
PLATFORM_OPNSENSE  = "opnsense"
PLATFORM_MIKROTIK  = "mikrotik"
PLATFORM_CISCO     = "cisco"
PLATFORM_UBIQUITI  = "ubiquiti"
PLATFORM_LINUX     = "linux"

# Lista ordenada para exibição em dropdowns
PLATFORM_CHOICES = [
    ("",            "— Selecione a plataforma —"),
    (PLATFORM_PFSENSE,  "pfSense (FreeBSD)"),
    (PLATFORM_OPNSENSE, "OPNsense (FreeBSD)"),
    (PLATFORM_MIKROTIK, "MikroTik (RouterOS)"),
    (PLATFORM_CISCO,    "Cisco IOS"),
    (PLATFORM_UBIQUITI, "Ubiquiti EdgeOS"),
    (PLATFORM_LINUX,    "Linux Genérico"),
]

# IDs para validação
PLATFORM_IDS = {p[0] for p in PLATFORM_CHOICES if p[0]}

# Mapa de labels curtos para o dashboard
PLATFORM_SHORT_LABELS = {
    PLATFORM_PFSENSE:  "pfSense",
    PLATFORM_OPNSENSE: "OPNsense",
    PLATFORM_MIKROTIK: "MikroTik",
    PLATFORM_CISCO:    "Cisco",
    PLATFORM_UBIQUITI: "EdgeOS",
    PLATFORM_LINUX:    "Linux",
}

# Cores por plataforma (para badges no dashboard)
PLATFORM_COLORS = {
    PLATFORM_PFSENSE:  "#3B82F6",   # azul
    PLATFORM_OPNSENSE: "#F97316",   # laranja
    PLATFORM_MIKROTIK: "#EF4444",   # vermelho
    PLATFORM_CISCO:    "#10B981",   # verde
    PLATFORM_UBIQUITI: "#8B5CF6",   # roxo
    PLATFORM_LINUX:    "#06B6D4",   # cyan
}


# ══════════════════════════════════════════════════════════════════════
# COMANDOS DE PING POR PLATAFORMA
# ══════════════════════════════════════════════════════════════════════

def get_ping_commands(platform: str) -> dict:
    """
    Retorna os templates de comandos de ping para a plataforma.

    Retorna dict com:
      exec_cmds: list[str]        — comandos para exec_command (sem PTY)
      interactive_cmds: list[str] — comandos para shell interativo (fallback)
      parser: str                 — "unix" | "mikrotik" | "cisco"
      needs_enable: bool          — True se precisa de 'enable' antes (Cisco)

    Placeholders nos templates: {COUNT}, {TIMEOUT}, {TARGET}
    """
    if platform in (PLATFORM_PFSENSE, PLATFORM_OPNSENSE):
        return {
            "exec_cmds": [
                # FreeBSD — sem -W (pfSense não suporta)
                "ping -c {COUNT} {TARGET} 2>&1",
                "/sbin/ping -c {COUNT} {TARGET} 2>&1",
                "/bin/ping -c {COUNT} {TARGET} 2>&1",
                "/usr/bin/ping -c {COUNT} {TARGET} 2>&1",
                "/usr/local/bin/ping -c {COUNT} {TARGET} 2>&1",
            ],
            "interactive_cmds": [
                "ping -c {COUNT} {TARGET}",
                "/sbin/ping -c {COUNT} {TARGET}",
                "/bin/ping -c {COUNT} {TARGET}",
            ],
            "parser": "unix",
            "needs_enable": False,
        }

    elif platform == PLATFORM_MIKROTIK:
        return {
            "exec_cmds": [
                # MikroTik CLI proprietário (RouterOS)
                "/ping {TARGET} count={COUNT} 2>&1",
                # Fallback: Linux-style (se o admin habilitou)
                "ping -c {COUNT} {TARGET} 2>&1",
                "ping -c {COUNT} -W {TIMEOUT} {TARGET} 2>&1",
            ],
            "interactive_cmds": [
                "/ping {TARGET} count={COUNT}",
                "ping -c {COUNT} {TARGET}",
            ],
            "parser": "mikrotik_or_unix",
            "needs_enable": False,
        }

    elif platform == PLATFORM_CISCO:
        return {
            "exec_cmds": [
                # Cisco IOS — formato simplificado
                "ping {TARGET} repeat {COUNT} timeout {TIMEOUT} 2>&1",
                "ping {TARGET} repeat {COUNT} 2>&1",
                "ping {TARGET} 2>&1",
            ],
            "interactive_cmds": [
                "ping {TARGET} repeat {COUNT} timeout {TIMEOUT}",
                "ping {TARGET} repeat {COUNT}",
                "ping {TARGET}",
            ],
            "parser": "cisco_or_unix",
            "needs_enable": True,
        }

    elif platform == PLATFORM_UBIQUITI:
        return {
            "exec_cmds": [
                # EdgeOS é Debian — comandos Linux padrão
                "ping -c {COUNT} -W {TIMEOUT} {TARGET} 2>&1",
                "/bin/ping -c {COUNT} -W {TIMEOUT} {TARGET} 2>&1",
                "ping -c {COUNT} {TARGET} 2>&1",
            ],
            "interactive_cmds": [
                "ping -c {COUNT} -W {TIMEOUT} {TARGET}",
                "ping -c {COUNT} {TARGET}",
            ],
            "parser": "unix",
            "needs_enable": False,
        }

    elif platform == PLATFORM_LINUX:
        return {
            "exec_cmds": [
                "ping -c {COUNT} -W {TIMEOUT} {TARGET} 2>&1",
                "/bin/ping -c {COUNT} -W {TIMEOUT} {TARGET} 2>&1",
                "/sbin/ping -c {COUNT} -W {TIMEOUT} {TARGET} 2>&1",
                "ping -c {COUNT} {TARGET} 2>&1",
                "busybox ping -c {COUNT} -W {TIMEOUT} {TARGET} 2>&1",
            ],
            "interactive_cmds": [
                "ping -c {COUNT} -W {TIMEOUT} {TARGET}",
                "ping -c {COUNT} {TARGET}",
            ],
            "parser": "unix",
            "needs_enable": False,
        }

    else:
        # Fallback: tenta tudo (comportamento legacy — pfSense + Linux)
        return {
            "exec_cmds": [
                "ping -c {COUNT} {TARGET} 2>&1",
                "/sbin/ping -c {COUNT} {TARGET} 2>&1",
                "/bin/ping -c {COUNT} {TARGET} 2>&1",
                "/usr/bin/ping -c {COUNT} {TARGET} 2>&1",
                "/usr/local/bin/ping -c {COUNT} {TARGET} 2>&1",
                "ping -c {COUNT} -W {TIMEOUT} {TARGET} 2>&1",
                "/sbin/ping -c {COUNT} -W {TIMEOUT} {TARGET} 2>&1",
                "busybox ping -c {COUNT} -W {TIMEOUT} {TARGET} 2>&1",
            ],
            "interactive_cmds": [
                "ping -c {COUNT} {TARGET}",
                "/sbin/ping -c {COUNT} {TARGET}",
                "/bin/ping -c {COUNT} {TARGET}",
            ],
            "parser": "unix",
            "needs_enable": False,
        }


# ══════════════════════════════════════════════════════════════════════
# COMANDOS DE TRACEROUTE POR PLATAFORMA
# ══════════════════════════════════════════════════════════════════════

def get_traceroute_commands(platform: str) -> list[tuple[str, str]]:
    """
    Retorna lista de (comando, label) para traceroute via SSH.

    Placeholders: {TARGET}, {MAX_HOPS}, {TIMEOUT}
    """
    if platform in (PLATFORM_PFSENSE, PLATFORM_OPNSENSE):
        return [
            ("traceroute -n {TARGET}",                         "traceroute -n"),
            ("traceroute {TARGET}",                            "traceroute"),
            ("/usr/sbin/traceroute -n {TARGET}",               "/usr/sbin/traceroute"),
            ("/usr/bin/traceroute -n {TARGET}",                "/usr/bin/traceroute"),
        ]

    elif platform == PLATFORM_MIKROTIK:
        return [
            # MikroTik CLI
            ("/tool traceroute {TARGET} count=1 2>&1",         "mikrotik /tool traceroute"),
            # Fallback Linux-style
            ("traceroute -n -m {MAX_HOPS} {TARGET}",           "traceroute -n"),
            ("tracepath -n {TARGET}",                          "tracepath"),
        ]

    elif platform == PLATFORM_CISCO:
        return [
            ("traceroute {TARGET}",                            "cisco traceroute"),
        ]

    elif platform == PLATFORM_UBIQUITI:
        return [
            ("traceroute -n -m {MAX_HOPS} -w {TIMEOUT} {TARGET}",  "traceroute"),
            ("traceroute -n {TARGET}",                              "traceroute -n"),
            ("tracepath -n {TARGET}",                               "tracepath"),
        ]

    elif platform == PLATFORM_LINUX:
        return [
            ("traceroute -n -m {MAX_HOPS} -w {TIMEOUT} {TARGET}",  "traceroute"),
            ("traceroute -n {TARGET}",                              "traceroute -n"),
            ("/usr/sbin/traceroute -n {TARGET}",                    "/usr/sbin/traceroute"),
            ("busybox traceroute -n -m {MAX_HOPS} {TARGET}",       "busybox traceroute"),
            ("tracepath -n {TARGET}",                               "tracepath"),
        ]

    else:
        # Fallback legacy
        return [
            ("traceroute -n {TARGET}",                         "traceroute -n"),
            ("traceroute {TARGET}",                            "traceroute"),
            ("/usr/sbin/traceroute -n {TARGET}",               "/usr/sbin/traceroute"),
            ("/usr/bin/traceroute -n {TARGET}",                "/usr/bin/traceroute"),
            ("busybox traceroute -n -m {MAX_HOPS} {TARGET}",   "busybox traceroute"),
            ("tracepath -n {TARGET}",                          "tracepath"),
        ]


# ══════════════════════════════════════════════════════════════════════
# CISCO — ENABLE MODE
# ══════════════════════════════════════════════════════════════════════

def cisco_ensure_enable(client, password: str) -> bool:
    """
    Verifica se a sessão Cisco está em modo enable (prompt #).
    Se estiver em user mode (prompt >), envia 'enable' + password.

    Retorna True se está em enable, False se não conseguiu.
    """
    try:
        # Envia newline para ver o prompt
        stdin, stdout, stderr = client.exec_command("", timeout=5)
        # Lê o banner/prompt
        import time
        time.sleep(0.5)

        channel = client.invoke_shell(width=200, height=40)
        channel.settimeout(3)
        time.sleep(1)

        # Lê output inicial
        output = ""
        while channel.recv_ready():
            output += channel.recv(4096).decode("utf-8", errors="replace")
            time.sleep(0.1)

        if "#" in output:
            channel.close()
            return True  # Já está em enable

        if ">" in output:
            # User mode — envia enable
            channel.send("enable\n")
            time.sleep(1)

            # Lê resposta (pode pedir password)
            output = ""
            while channel.recv_ready():
                output += channel.recv(4096).decode("utf-8", errors="replace")
                time.sleep(0.1)

            if "assword" in output:
                channel.send(f"{password}\n")
                time.sleep(1)
                output = ""
                while channel.recv_ready():
                    output += channel.recv(4096).decode("utf-8", errors="replace")
                    time.sleep(0.1)

            channel.close()
            return "#" in output

        channel.close()
        return True  # Assume enable se não reconheceu o prompt

    except Exception as e:
        logger.debug(f"Cisco enable mode check falhou: {e}")
        return True  # Assume enable — melhor tentar do que desistir


# ══════════════════════════════════════════════════════════════════════
# PARSERS ESPECÍFICOS — MIKROTIK
# ══════════════════════════════════════════════════════════════════════

def _parse_mikrotik_time(time_str: str) -> float:
    """
    Converte tempo do MikroTik para milissegundos.
    Formatos: "12ms182us", "12ms", "0.5ms", "timeout"
    """
    if not time_str or "timeout" in time_str.lower():
        return 0.0
    # "12ms182us" → 12.182
    m = re.match(r"(\d+)ms(?:(\d+)us)?", time_str)
    if m:
        ms = int(m.group(1))
        us = int(m.group(2)) if m.group(2) else 0
        return ms + us / 1000.0
    # "0.5ms"
    m2 = re.match(r"([\d.]+)ms", time_str)
    if m2:
        return float(m2.group(1))
    return 0.0


def is_mikrotik_output(output: str) -> bool:
    """Detecta se o output é do MikroTik RouterOS (não Unix)."""
    low = output.lower()
    return any(ind in low for ind in (
        "sent=", "received=", "packet-loss=",
        "min-rtt=", "avg-rtt=", "max-rtt=",
    ))


def parse_mikrotik_ping(output: str, count: int) -> Optional[dict]:
    """
    Parse output do /ping do MikroTik RouterOS.

    Exemplo de output:
      SEQ HOST                                     SIZE TTL TIME       STATUS
        0 8.8.8.8                                    56  57 12ms182us
        1 8.8.8.8                                    56  57 11ms934us
        sent=4 received=4 packet-loss=0% min-rtt=11ms934us avg-rtt=12ms228us max-rtt=12ms649us

    Retorna dict com: latency_ms, jitter_ms, loss_pct, rtt_min, rtt_max, rtt_avg, ttl, status
    """
    if not is_mikrotik_output(output):
        return None

    result = {
        "latency_ms": 0.0, "jitter_ms": 0.0, "loss_pct": 100.0,
        "rtt_min": 0.0, "rtt_max": 0.0, "rtt_avg": 0.0,
        "ttl": 0, "status": "offline",
    }

    # Extrai TTL da primeira resposta
    ttl_match = re.search(r"\s+(\d+)\s+\d+ms", output)
    if ttl_match:
        result["ttl"] = int(ttl_match.group(1))

    # Extrai summary line: sent=4 received=4 packet-loss=0% min-rtt=...
    loss_m = re.search(r"packet-loss=(\d+)%", output)
    if loss_m:
        result["loss_pct"] = float(loss_m.group(1))

    min_m = re.search(r"min-rtt=(\S+)", output)
    avg_m = re.search(r"avg-rtt=(\S+)", output)
    max_m = re.search(r"max-rtt=(\S+)", output)

    if min_m:
        result["rtt_min"] = _parse_mikrotik_time(min_m.group(1))
    if avg_m:
        result["rtt_avg"] = _parse_mikrotik_time(avg_m.group(1))
        result["latency_ms"] = result["rtt_avg"]
    if max_m:
        result["rtt_max"] = _parse_mikrotik_time(max_m.group(1))

    # Extrai tempos individuais para jitter
    times = re.findall(r"\s(\d+ms\d*us?)\s*$", output, re.MULTILINE)
    if not times:
        times = re.findall(r"\s(\d+ms(?:\d+us)?)", output)
    rtt_values = [_parse_mikrotik_time(t) for t in times if _parse_mikrotik_time(t) > 0]

    if len(rtt_values) >= 2:
        result["jitter_ms"] = round(statistics.stdev(rtt_values), 2)

    if result["loss_pct"] < 100:
        result["status"] = "online"

    return result


# ══════════════════════════════════════════════════════════════════════
# PARSERS ESPECÍFICOS — CISCO IOS
# ══════════════════════════════════════════════════════════════════════

def is_cisco_output(output: str) -> bool:
    """Detecta se o output é do Cisco IOS."""
    low = output.lower()
    return "success rate" in low and ("percent" in low or "%" in low)


def parse_cisco_ping(output: str, count: int) -> Optional[dict]:
    """
    Parse output do ping do Cisco IOS.

    Exemplo de output:
      Type escape sequence to abort.
      Sending 4, 100-byte ICMP Echos to 8.8.8.8, timeout is 2 seconds:
      !!!!
      Success rate is 100 percent (4/4), round-trip min/avg/max = 1/2/4 ms

    Retorna dict com: latency_ms, jitter_ms, loss_pct, rtt_min, rtt_max, rtt_avg, status
    """
    if not is_cisco_output(output):
        return None

    result = {
        "latency_ms": 0.0, "jitter_ms": 0.0, "loss_pct": 100.0,
        "rtt_min": 0.0, "rtt_max": 0.0, "rtt_avg": 0.0,
        "ttl": 0, "status": "offline",
    }

    # Success rate is 100 percent (4/4)
    rate_m = re.search(r"success rate is (\d+) percent", output, re.IGNORECASE)
    if rate_m:
        success_pct = int(rate_m.group(1))
        result["loss_pct"] = 100.0 - success_pct

    # round-trip min/avg/max = 1/2/4 ms
    rtt_m = re.search(r"min/avg/max\s*=\s*(\d+)/(\d+)/(\d+)\s*ms", output, re.IGNORECASE)
    if rtt_m:
        result["rtt_min"] = float(rtt_m.group(1))
        result["rtt_avg"] = float(rtt_m.group(2))
        result["rtt_max"] = float(rtt_m.group(3))
        result["latency_ms"] = result["rtt_avg"]

    # Count !'s and .'s for individual results
    dots_line = re.search(r"[!.]+", output)
    if dots_line:
        chars = dots_line.group(0)
        successes = chars.count("!")
        failures = chars.count(".")
        total = successes + failures
        if total > 0:
            result["loss_pct"] = (failures / total) * 100.0

    if result["loss_pct"] < 100:
        result["status"] = "online"

    return result


# ══════════════════════════════════════════════════════════════════════
# VALIDAÇÃO DE OUTPUT — MULTI-PLATAFORMA
# ══════════════════════════════════════════════════════════════════════

def is_valid_ping_output(output: str, platform: str = "") -> bool:
    """
    Verifica se a saída é output válido de ping para a plataforma.
    Extensão da _is_valid_ping_output original para incluir MikroTik e Cisco.
    """
    if not output or not output.strip():
        return False

    low = output.lower()

    # Erros universais
    for err in ("not found", "no such file", "permission denied",
                "command not found", "unknown host", "invalid option",
                "illegal option", "bad option", "unrecognized option",
                "syntax error", "bad command"):
        if err in low:
            return False

    # MikroTik output
    if is_mikrotik_output(output):
        return True

    # Cisco output
    if is_cisco_output(output):
        return True

    # Unix/BSD/Linux output (padrão)
    return any(ind in low for ind in (
        "bytes from", "ttl=", "packet loss", "packets transmitted",
        "ping statistics", "round-trip", "tempo=", "% loss",
        "icmp_seq=", "time=",
    ))


# ══════════════════════════════════════════════════════════════════════
# API PÚBLICA
# ══════════════════════════════════════════════════════════════════════

def get_platform_label(platform: str) -> str:
    """Retorna o label curto para exibição."""
    return PLATFORM_SHORT_LABELS.get(platform, platform or "—")


def get_platform_color(platform: str) -> str:
    """Retorna a cor hex para o badge."""
    return PLATFORM_COLORS.get(platform, "#484F58")


def is_valid_platform(platform: str) -> bool:
    """Retorna True se é uma plataforma válida."""
    return platform in PLATFORM_IDS
