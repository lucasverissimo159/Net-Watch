"""
Utilitários de rede: ping, traceroute, DNS, MTU discovery e WAN IP.
Compatível com Windows e Linux.

CORREÇÕES v2.1:
  1. ping_host_via_ssh: adicionados caminhos completos (/bin/ping, /sbin/ping,
     /usr/bin/ping, /usr/local/bin/ping) para cobrir pfSense/FreeBSD onde o
     PATH não está configurado em sessões não-interativas (exec_command).
  2. ping_host_via_ssh: adicionado fallback via shell interativo
     (_exec_ssh_interactive_command) — idêntico ao que o Terminal SSH usa.
  3. _is_valid_ping_output: nova função para validação robusta de output.
  4. traceroute_via_ssh e run_mtr_via_ssh: mesma estratégia de caminhos
     completos + fallback via shell interativo.
"""
import logging
import platform
import re
import threading
import socket
import statistics
import subprocess
import time
from datetime import datetime
from typing import Optional
from urllib import request

from models.host_model import PingResult, TracerouteHop, TracerouteResult


IS_WINDOWS = platform.system().lower() == "windows"
logger = logging.getLogger("network")


def _build_ping_result_from_output(
    output: str,
    count: int,
    timestamp: Optional[datetime] = None,
    mode: str = "PING",
) -> PingResult:
    """Converte a saída de um ping em PingResult."""
    result = PingResult(timestamp=timestamp or datetime.now(), ping_mode=mode)

    ttl_match = re.search(r"TTL[=:](\d+)", output, re.IGNORECASE)
    if ttl_match:
        result.ttl = int(ttl_match.group(1))

    windows_times = re.findall(r"tempo[=<](\d+)ms|time[=<](\d+)ms", output, re.IGNORECASE)
    unix_times    = re.findall(r"time[=]?([\d.]+)\s*ms", output, re.IGNORECASE)
    if windows_times:
        rtt_values = [float(left or right) for left, right in windows_times if (left or right)]
    else:
        rtt_values = [float(value) for value in unix_times]

    loss_match = re.search(
        r"\((\d+)%\s*(de )?perda\)|(\d+)%\s*loss|(\d+)%\s*packet loss",
        output, re.IGNORECASE,
    )
    if loss_match:
        loss_val = next((g for g in loss_match.groups() if g and g.isdigit()), None)
        result.loss_pct = float(loss_val) if loss_val is not None else 100.0
    elif not rtt_values:
        result.loss_pct = 100.0

    stats_windows = re.search(
        r"M[ii]nimo\s*=\s*(\d+)ms.*?M[aá]ximo\s*=\s*(\d+)ms.*?M[eé]dia\s*=\s*(\d+)ms|"
        r"Minimum\s*=\s*(\d+)ms.*?Maximum\s*=\s*(\d+)ms.*?Average\s*=\s*(\d+)ms",
        output, re.IGNORECASE | re.DOTALL,
    )
    stats_unix = re.search(r"([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)", output)

    if stats_windows:
        groups = [g for g in stats_windows.groups() if g is not None]
        if len(groups) >= 3:
            result.rtt_min = float(groups[0])
            result.rtt_max = float(groups[1])
            result.rtt_avg = float(groups[2])
    elif stats_unix:
        result.rtt_min = float(stats_unix.group(1))
        result.rtt_avg = float(stats_unix.group(2))
        result.rtt_max = float(stats_unix.group(3))

    if rtt_values and result.rtt_avg == 0:
        result.rtt_min = min(rtt_values)
        result.rtt_max = max(rtt_values)
        result.rtt_avg = statistics.mean(rtt_values)

    result.latency_ms   = result.rtt_avg
    result.packets_sent = count
    result.packets_recv = len(rtt_values)

    if len(rtt_values) >= 2:
        result.jitter_ms = round(statistics.stdev(rtt_values), 2)
    elif len(rtt_values) == 1:
        result.jitter_ms = 0.0

    result.status = "online" if result.loss_pct < 100 else "offline"
    return result


def _exec_ssh_command(client, command: str, timeout: int = 30) -> tuple[str, int]:
    """Executa um comando no host remoto e devolve stdout+stderr e exit code."""
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout, get_pty=True)
    del stdin
    output  = stdout.read().decode("utf-8", errors="replace")
    output += stderr.read().decode("utf-8", errors="replace")
    status  = stdout.channel.recv_exit_status()
    return output, status


def _exec_ssh_plain(client, command: str, timeout: int = 60) -> tuple[str, int]:
    """Executa comando sem PTY — output limpo (sem ANSI/escape codes)."""
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout, get_pty=False)
    del stdin
    try:
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()
    except Exception:
        out, err, status = "", "", 1
    return (out + err), status


def _exec_ssh_interactive_command(client, command: str, timeout: int = 45) -> tuple[str, int]:
    """
    Executa um comando dentro de um shell interativo, simulando o Terminal SSH.
    Útil em hosts pfSense/FreeBSD onde PATH/profile só existem em sessão interativa.
    """
    marker = "__CODEx_DONE__"
    channel = client.invoke_shell(width=160, height=40)
    channel.settimeout(0.2)

    output_chunks: list[str] = []
    try:
        # Aguarda prompt inicial
        start = time.time()
        while time.time() - start < 2:
            if channel.recv_ready():
                output_chunks.append(channel.recv(4096).decode("utf-8", errors="replace"))
            else:
                time.sleep(0.05)

        channel.send(command + "\n")
        channel.send(f"echo {marker}$?\n")

        deadline = time.time() + timeout
        while time.time() < deadline:
            if channel.recv_ready():
                chunk = channel.recv(4096).decode("utf-8", errors="replace")
                output_chunks.append(chunk)
                if marker in chunk or marker in "".join(output_chunks):
                    break
            else:
                time.sleep(0.05)
    finally:
        try:
            channel.close()
        except Exception:
            pass

    output = "".join(output_chunks)
    status = 0
    marker_match = re.search(rf"{marker}(\d+)", output)
    if marker_match:
        status = int(marker_match.group(1))
        output = re.sub(rf"{marker}\d+.*", "", output, flags=re.DOTALL)
    return output, status


def _parse_remote_ttl_ping(output: str, hop_number: int, target_ip: str) -> tuple[TracerouteHop, bool]:
    """Interpreta um ping com TTL fixo como um hop aproximado de traceroute."""
    ip_matches  = re.findall(r"(\d{1,3}(?:\.\d{1,3}){3})", output)
    hop_ip      = "*"
    reached_target = False

    if re.search(r"bytes from|ttl=", output, re.IGNORECASE):
        reached_target = True
        if target_ip in ip_matches:
            hop_ip = target_ip
        elif ip_matches:
            hop_ip = ip_matches[-1]
    elif re.search(r"time to live exceeded|ttl expired|time exceeded", output, re.IGNORECASE):
        hop_ip = ip_matches[0] if ip_matches else "*"
    elif ip_matches:
        hop_ip = ip_matches[0]

    rtts = [float(v) for v in re.findall(r"([\d.]+)\s*ms", output, re.IGNORECASE)[:3]]
    while len(rtts) < 3:
        rtts.append(0.0)

    hop = TracerouteHop(
        hop_number=hop_number, ip=hop_ip,
        rtt1=rtts[0], rtt2=rtts[1], rtt3=rtts[2],
    )
    return hop, reached_target


# ── Validação de output de ping ──────────────────────────────────────────

def _is_valid_ping_output(output: str) -> bool:
    """
    Verifica se a saída de um comando é realmente output de ping e não
    uma mensagem de erro, output vazio, ou "command not found".

    CORREÇÃO v2.1: esta função substitui a checagem simplista anterior
    que só olhava "not found" e falhava em pfSense/FreeBSD onde o
    exec_command retorna output vazio em vez de erro explícito.
    """
    if not output or not output.strip():
        return False
    low = output.lower()
    # Indicadores de erro — NÃO é output válido
    for err in ("not found", "no such file", "permission denied",
                "command not found", "unknown host", "invalid option",
                "illegal option", "bad option", "unrecognized option"):
        if err in low:
            return False
    # Indicadores de output VÁLIDO de ping
    return any(ind in low for ind in (
        "bytes from", "ttl=", "packet loss", "packets transmitted",
        "ping statistics", "round-trip", "tempo=", "% loss",
        "icmp_seq=", "time=",
    ))


def get_wan_ip() -> Optional[str]:
    """Obtém o IP WAN (público) via serviços externos."""
    urls = [
        "https://api.ipify.org",
        "https://api4.my-ip.io/ip",
        "https://ifconfig.me/ip",
    ]
    for url in urls:
        try:
            req = request.Request(url, headers={"User-Agent": "NetWatch-Pro/2.0"})
            with request.urlopen(req, timeout=5) as resp:
                ip = resp.read().decode("utf-8").strip()
                if re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", ip):
                    return ip
        except Exception:
            continue
    return None


def ping_host(ip: str, count: int = 4, timeout_ms: int = 1000) -> PingResult:
    """Executa ping local com parsing detalhado de métricas."""
    result = PingResult(timestamp=datetime.now(), ping_mode="PING")
    try:
        if IS_WINDOWS:
            cmd = ["ping", "-n", str(count), "-w", str(timeout_ms), ip]
        else:
            timeout_s = max(1, timeout_ms // 1000)
            cmd = ["ping", "-c", str(count), "-W", str(timeout_s), ip]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        output = proc.stdout + proc.stderr
        result = _build_ping_result_from_output(output, count, timestamp=result.timestamp, mode="PING")
    except subprocess.TimeoutExpired:
        result.status   = "timeout"
        result.loss_pct = 100.0
    except Exception:
        result.status   = "offline"
        result.loss_pct = 100.0
    return result


def traceroute(ip: str, max_hops: int = 30, timeout_ms: int = 3000) -> TracerouteResult:
    trace = TracerouteResult(timestamp=datetime.now())
    try:
        if IS_WINDOWS:
            cmd = ["tracert", "-d", "-h", str(max_hops), "-w", str(timeout_ms), ip]
        else:
            cmd = ["traceroute", "-n", "-m", str(max_hops),
                   "-w", str(max(1, timeout_ms // 1000)), ip]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
        output = proc.stdout
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith(("Tracing", "traceroute", "Tracando")):
                continue
            if "Trace complete" in line or "Rastreamento" in line:
                trace.target_reached = True
                continue
            hop_match = re.match(r"\s*(\d+)\s+(.+)", line)
            if not hop_match:
                continue
            hop_num = int(hop_match.group(1))
            rest    = hop_match.group(2)
            hop_ip    = "*"
            ip_matches = re.findall(r"(\d+\.\d+\.\d+\.\d+)", rest)
            if ip_matches:
                hop_ip = ip_matches[-1]
            time_matches = re.findall(r"(\d+)\s*ms", rest)
            rtts = [float(v) for v in time_matches]
            hop = TracerouteHop(
                hop_number=hop_num, ip=hop_ip,
                rtt1=rtts[0] if len(rtts) > 0 else 0.0,
                rtt2=rtts[1] if len(rtts) > 1 else 0.0,
                rtt3=rtts[2] if len(rtts) > 2 else 0.0,
            )
            trace.hops.append(hop)
            if hop_ip == ip:
                trace.target_reached = True
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass
    return trace


def traceroute_via_ssh(
    ssh_host: str,
    ssh_user: str,
    ssh_password: str,
    target_ip: str,
    ssh_port: int = 22,
    max_hops: int = 20,
    timeout_ms: int = 2000,
) -> dict:
    """
    Executa traceroute dentro do host remoto via SSH.

    CORREÇÃO v2.1: agora tenta caminhos completos e shell interativo como
    fallback para cobrir pfSense/FreeBSD.
    """
    if not ssh_user:
        raise ValueError("Credenciais SSH não configuradas para este host.")
    try:
        import paramiko
    except ImportError as exc:
        raise RuntimeError("paramiko não instalado — execute: pip install paramiko") from exc

    target_ip = (target_ip or "").strip()
    if not re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", target_ip):
        raise ValueError("IP de destino inválido para traceroute.")

    timeout_s = max(2, timeout_ms // 1000)
    client    = paramiko.SSHClient()
    from utils.security import TrustOnFirstUsePolicy; client.set_missing_host_key_policy(TrustOnFirstUsePolicy())

    try:
        client.connect(
            hostname=ssh_host, port=int(ssh_port or 22),
            username=ssh_user, password=ssh_password or "",
            timeout=12, allow_agent=False, look_for_keys=False,
        )

        traceroute_commands = [
            (f"traceroute -n {target_ip}",                                      "traceroute -n"),
            (f"traceroute {target_ip}",                                         "traceroute"),
            (f"/usr/sbin/traceroute -n {target_ip}",                            "/usr/sbin/traceroute -n"),
            (f"/usr/sbin/traceroute {target_ip}",                               "/usr/sbin/traceroute"),
            (f"/usr/bin/traceroute -n {target_ip}",                             "/usr/bin/traceroute -n"),
            (f"busybox traceroute -n -m {max_hops} -w {timeout_s} {target_ip}", "busybox traceroute"),
            (f"tracepath -n {target_ip}",                                       "tracepath -n"),
            (f"tracepath {target_ip}",                                          "tracepath"),
        ]

        output       = ""
        last_output  = ""
        command_used = ""

        # Fase 1: exec_command sem PTY
        for cmd, label in traceroute_commands:
            try:
                raw, _  = _exec_ssh_plain(client, cmd, timeout=60)
                clean   = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\r", "", raw)
                last_output = clean.strip() or last_output
                if re.search(r"^\s*\d+\s+", clean, re.MULTILINE):
                    output       = clean
                    command_used = label
                    logger.info(f"traceroute SSH ok em {ssh_host}: '{label}'")
                    break
            except Exception as exc:
                logger.debug(f"Comando '{label}' falhou: {exc}")
                continue

        # Fase 2: shell interativo (fallback para pfSense)
        if not output:
            logger.info(f"traceroute SSH {ssh_host}: tentando shell interativo")
            for cmd, label in traceroute_commands[:6]:
                try:
                    raw, _ = _exec_ssh_interactive_command(client, cmd, timeout=60)
                    clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\r", "", raw)
                    if re.search(r"^\s*\d+\s+", clean, re.MULTILINE):
                        output = clean
                        command_used = f"{label} (interativo)"
                        logger.info(f"traceroute SSH ok em {ssh_host}: '{label}' (shell interativo)")
                        break
                except Exception as exc:
                    logger.debug(f"Interativo '{label}' falhou: {exc}")
                    continue

        hops_data: list[dict] = []
        reached = False

        if output:
            for line in output.splitlines():
                line = line.strip()
                if not line:
                    continue
                if re.match(r"^(traceroute|tracepath)\s+to", line, re.IGNORECASE):
                    continue
                m = re.match(r"^\s*(\d+)\s+(.+)", line)
                if not m:
                    continue
                hop_num    = int(m.group(1))
                rest       = m.group(2)
                ip_matches = re.findall(r"(\d{1,3}(?:\.\d{1,3}){3})", rest)
                hop_ip     = ip_matches[-1] if ip_matches else "*"
                if re.match(r"^[\s*]+$", rest.replace("ms", "")) and not ip_matches:
                    hop_ip = "*"
                rtts = [float(v) for v in re.findall(r"([\d.]+)\s*ms", rest)[:3]]
                while len(rtts) < 3:
                    rtts.append(0.0)
                hops_data.append({
                    "hop": hop_num, "ip": hop_ip,
                    "rtt1": rtts[0], "rtt2": rtts[1], "rtt3": rtts[2],
                })
                if hop_ip == target_ip:
                    reached = True
                    break
        else:
            # Fallback: TTL incrementando via ping (com e sem -W)
            command_used = "ping-ttl-fallback"
            logger.info(f"traceroute SSH: usando fallback ping-TTL para {target_ip}")
            ping_paths = ["ping", "/bin/ping", "/sbin/ping", "/usr/bin/ping",
                          "/usr/local/bin/ping", "busybox ping"]
            for ttl in range(1, max_hops + 1):
                ttl_output = ""
                for ping_bin in ping_paths:
                    for w_flag in [f"-W {timeout_s} ", ""]:
                        cmd_tpl = f"{ping_bin} -c 1 {w_flag}-t {ttl} {target_ip} 2>&1"
                        try:
                            raw, _ = _exec_ssh_plain(client, cmd_tpl, timeout=timeout_s + 5)
                            clean  = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\r", "", raw)
                            if _is_valid_ping_output(clean) or "time exceeded" in clean.lower():
                                ttl_output = clean
                                break
                        except Exception:
                            continue
                    if ttl_output:
                        break
                if not ttl_output:
                    for ping_bin in ["ping", "/sbin/ping", "/bin/ping"]:
                        for w_flag in [f"-W {timeout_s} ", ""]:
                            cmd_tpl = f"{ping_bin} -c 1 {w_flag}-t {ttl} {target_ip} 2>&1"
                            try:
                                raw, _ = _exec_ssh_interactive_command(client, cmd_tpl, timeout=timeout_s + 10)
                                clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\r", "", raw)
                                if _is_valid_ping_output(clean) or "time exceeded" in clean.lower():
                                    ttl_output = clean
                                    break
                            except Exception:
                                continue
                        if ttl_output:
                            break
                if not ttl_output:
                    break
                hop, hop_reached = _parse_remote_ttl_ping(ttl_output, ttl, target_ip)
                hops_data.append({
                    "hop": hop.hop_number, "ip": hop.ip,
                    "rtt1": hop.rtt1, "rtt2": hop.rtt2, "rtt3": hop.rtt3,
                })
                if hop_reached:
                    reached = True
                    break

        if not hops_data:
            snippet = (last_output or "sem retorno").replace("\n", " ")[:300]
            raise RuntimeError(
                f"Não foi possível executar traceroute no host {ssh_host}. "
                f"Última saída SSH: {snippet}"
            )

        return {
            "wan_ip": target_ip, "hop_count": len(hops_data),
            "hops": hops_data, "target_reached": reached,
            "via_ssh": ssh_host, "command_used": command_used,
        }

    finally:
        try:
            client.close()
        except Exception:
            pass


def resolve_dns(hostname: str) -> tuple[float, str]:
    try:
        start = time.perf_counter()
        ip    = socket.gethostbyname(hostname)
        elapsed = (time.perf_counter() - start) * 1000
        return round(elapsed, 2), ip
    except socket.gaierror:
        return -1.0, ""


def discover_mtu(ip: str, start_size: int = 1472, min_size: int = 68) -> int:
    low, high = min_size, start_size
    best = min_size
    while low <= high:
        mid = (low + high) // 2
        try:
            if IS_WINDOWS:
                cmd = ["ping", "-n", "1", "-f", "-l", str(mid), "-w", "1000", ip]
            else:
                cmd = ["ping", "-c", "1", "-M", "do", "-s", str(mid), "-W", "1", ip]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
            )
            output = proc.stdout + proc.stderr
            if "fragment" in output.lower() or "frag" in output.lower() or "grande" in output.lower():
                high = mid - 1
            elif proc.returncode == 0 and ("TTL=" in output or "ttl=" in output):
                best = mid
                low  = mid + 1
            else:
                high = mid - 1
        except Exception:
            high = mid - 1
    return best + 28


def check_port(ip: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def ping_host_via_ssh(
    host_ip: str,
    ssh_user: str,
    ssh_password: str,
    ssh_port: int = 22,
    count: int = 4,
    timeout_ms: int = 1000,
    target_ip: Optional[str] = None,
) -> PingResult:
    """
    Executa ping DE DENTRO do host via SSH para medir conectividade real do link.

    CORREÇÃO v2.1:
      1. Adicionados caminhos completos (/bin/ping, /sbin/ping, /usr/bin/ping,
         /usr/local/bin/ping) para cobrir pfSense/FreeBSD onde exec_command
         (sem PTY) não tem o PATH configurado.
      2. Se nenhum comando funcionar via exec_command, tenta via shell
         interativo (_exec_ssh_interactive_command) — que carrega o PATH
         completo do /etc/profile, exatamente como o Terminal SSH faz.
      3. Usa _is_valid_ping_output() para detecção mais robusta.
    """
    result = PingResult(timestamp=datetime.now(), ping_mode="PING")

    if not ssh_user:
        return ping_host(host_ip, count, timeout_ms)

    remote_target = (target_ip or "8.8.8.8").strip()
    if remote_target == host_ip:
        remote_target = "8.8.8.8"

    # CORREÇÃO v2.12 — valida + escapa target antes de inserir em comando shell
    from utils.security import sanitize_target, is_valid_target
    if not is_valid_target(remote_target):
        logger.warning(f"ping_host_via_ssh: target inválido rejeitado: {remote_target!r}")
        return result
    remote_target = sanitize_target(remote_target)

    client = None
    try:
        import paramiko

        client = paramiko.SSHClient()
        from utils.security import TrustOnFirstUsePolicy; client.set_missing_host_key_policy(TrustOnFirstUsePolicy())
        client.connect(
            hostname=host_ip,
            port=int(ssh_port or 22),
            username=ssh_user,
            password=ssh_password or "",
            timeout=10,
            allow_agent=False,
            look_for_keys=False,
        )

        timeout_s = max(1, timeout_ms // 1000)

        # ── Comandos: com -W (Linux) + sem -W (FreeBSD/pfSense) ────────
        ping_commands = [
            f"ping -c {count} -W {timeout_s} -i 0.2 {remote_target} 2>&1",
            f"ping -c {count} -W {timeout_s} {remote_target} 2>&1",
            f"/bin/ping -c {count} -W {timeout_s} {remote_target} 2>&1",
            f"/sbin/ping -c {count} -W {timeout_s} {remote_target} 2>&1",
            f"/usr/bin/ping -c {count} -W {timeout_s} {remote_target} 2>&1",
            f"busybox ping -c {count} -W {timeout_s} {remote_target} 2>&1",
            # Sem -W (FreeBSD/pfSense)
            f"ping -c {count} {remote_target} 2>&1",
            f"/sbin/ping -c {count} {remote_target} 2>&1",
            f"/bin/ping -c {count} {remote_target} 2>&1",
            f"/usr/bin/ping -c {count} {remote_target} 2>&1",
            f"/usr/local/bin/ping -c {count} {remote_target} 2>&1",
        ]

        output = ""

        # ── Fase 1: exec_command sem PTY (rápido, output limpo) ──────
        for command in ping_commands:
            try:
                current_output, _ = _exec_ssh_plain(client, command, timeout=30)
                if _is_valid_ping_output(current_output):
                    output = current_output
                    logger.debug(f"ping SSH {host_ip}: ok via exec_command")
                    break
            except Exception:
                continue

        # ── Fase 2: shell interativo (fallback pfSense/FreeBSD) ──────
        # Idêntico ao que o Terminal SSH usa — carrega PATH do /etc/profile
        if not output:
            logger.debug(
                f"ping SSH {host_ip}: exec_command falhou, "
                "tentando via shell interativo"
            )
            interactive_commands = [
                f"ping -c {count} -W {timeout_s} {remote_target}",
                f"/sbin/ping -c {count} -W {timeout_s} {remote_target}",
                f"ping -c {count} {remote_target}",
                f"/sbin/ping -c {count} {remote_target}",
                f"/bin/ping -c {count} {remote_target}",
            ]
            for command in interactive_commands:
                try:
                    current_output, _ = _exec_ssh_interactive_command(
                        client, command, timeout=count * timeout_s + 15,
                    )
                    # Limpa ANSI escapes do shell interativo
                    clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\r", "", current_output)
                    if _is_valid_ping_output(clean):
                        output = clean
                        logger.debug(f"ping SSH {host_ip}: ok via shell interativo")
                        break
                except Exception:
                    continue

        if not output:
            logger.warning(
                f"ping SSH {host_ip}: nenhum comando de ping disponível "
                "(exec_command e shell interativo falharam) — usando fallback local"
            )
            return ping_host(host_ip, count, timeout_ms)

        result = _build_ping_result_from_output(
            output, count, timestamp=result.timestamp, mode="SSH",
        )
        result.gateway = remote_target
        return result

    except ImportError:
        logger.debug("paramiko não instalado — usando ping local")

    except Exception as exc:
        logger.warning(f"ping SSH {host_ip} → {remote_target} falhou: {exc} — fallback local")

    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    return ping_host(host_ip, count, timeout_ms)


# ── Ping duplo via SSH (WAN + Google em UMA conexão) ──────────────────────

def ping_triple_via_ssh(
    host_ip: str,
    ssh_user: str,
    ssh_password: str,
    ssh_port: int = 22,
    count: int = 4,
    timeout_ms: int = 1000,
    wan_target: Optional[str] = None,
    google_target: str = "8.8.8.8",
    platform: str = "",
) -> tuple[Optional["PingResult"], Optional["PingResult"], Optional["PingResult"]]:
    """
    Abre UMA única conexão SSH e executa 3 pings de dentro do host:
      1. host_ip   — o host pinga a si mesmo (auto-teste de stack de rede)
      2. wan_target — o host pinga o gateway WAN (saúde do link)
      3. google     — o host pinga 8.8.8.8 (conectividade internet)

    CORREÇÃO v2.11 — multi-plataforma:
      Aceita parâmetro `platform` que define os comandos SSH e parsers
      de output para cada tipo de roteador (pfSense, MikroTik, Cisco, etc.).
      Se vazio, usa o comportamento legacy (auto-discovery pfSense/Linux).

    Retorna (host_result, wan_result, google_result). Cada um pode ser None.
    """
    host_result = None
    wan_result = None
    google_result = None

    if not ssh_user:
        return None, None, None

    client = None
    try:
        import paramiko

        client = paramiko.SSHClient()
        from utils.security import TrustOnFirstUsePolicy; client.set_missing_host_key_policy(TrustOnFirstUsePolicy())
        client.connect(
            hostname=host_ip,
            port=int(ssh_port or 22),
            username=ssh_user,
            password=ssh_password or "",
            timeout=10,
            allow_agent=False,
            look_for_keys=False,
        )

        timeout_s = max(1, timeout_ms // 1000)

        # ── CORREÇÃO v2.11 — Comandos por plataforma ─────────────────
        #
        # Em vez de hardcoded pfSense/Linux, usa device_profiles para obter
        # os comandos corretos para cada plataforma de roteador.

        from utils.device_profiles import (
            get_ping_commands, is_valid_ping_output as _platform_valid,
            is_mikrotik_output, parse_mikrotik_ping,
            is_cisco_output, parse_cisco_ping,
            cisco_ensure_enable,
        )

        profile = get_ping_commands(platform)
        _EXEC_CMDS = profile["exec_cmds"]
        _INTERACTIVE_CMDS = profile["interactive_cmds"]

        # Cisco: tenta entrar em enable mode antes de pingar
        if profile.get("needs_enable"):
            try:
                cisco_ensure_enable(client, ssh_password or "")
            except Exception as e:
                logger.debug(f"Cisco enable mode falhou: {e}")

        _cached_cmd: Optional[str] = None
        _cached_mode: str = ""
        _cached_interactive_cmd: Optional[str] = None

        def _platform_parse(output: str, cnt: int) -> Optional[PingResult]:
            """
            Parser multi-plataforma: detecta o formato do output e usa o
            parser correto (MikroTik, Cisco, ou Unix padrão).
            """
            # MikroTik
            if is_mikrotik_output(output):
                parsed = parse_mikrotik_ping(output, cnt)
                if parsed:
                    return PingResult(
                        timestamp=datetime.now(),
                        latency_ms=parsed["latency_ms"],
                        jitter_ms=parsed["jitter_ms"],
                        loss_pct=parsed["loss_pct"],
                        rtt_min=parsed["rtt_min"],
                        rtt_max=parsed["rtt_max"],
                        rtt_avg=parsed["rtt_avg"],
                        ttl=parsed["ttl"],
                        status=parsed["status"],
                        ping_mode="SSH",
                    )
            # Cisco
            if is_cisco_output(output):
                parsed = parse_cisco_ping(output, cnt)
                if parsed:
                    return PingResult(
                        timestamp=datetime.now(),
                        latency_ms=parsed["latency_ms"],
                        jitter_ms=parsed["jitter_ms"],
                        loss_pct=parsed["loss_pct"],
                        rtt_min=parsed["rtt_min"],
                        rtt_max=parsed["rtt_max"],
                        rtt_avg=parsed["rtt_avg"],
                        ttl=parsed.get("ttl", 0),
                        status=parsed["status"],
                        ping_mode="SSH",
                    )
            # Unix/BSD/Linux (padrão)
            return _build_ping_result_from_output(output, cnt, timestamp=datetime.now(), mode="SSH")

        def _fill_cmd(tpl: str, target: str, cnt: int = count) -> str:
            # CORREÇÃO v2.12 — anti command injection:
            #   sanitize_target valida IP/hostname e aplica shlex.quote.
            #   Se o target for inválido, retorna string vazia que faz o
            #   comando falhar de forma controlada em vez de injetar.
            from utils.security import sanitize_target
            safe = sanitize_target(target)
            return (tpl
                    .replace("{COUNT}", str(cnt))
                    .replace("{TIMEOUT}", str(timeout_s))
                    .replace("{TARGET}", safe))

        def _try_exec(cmd: str, tgt: str, tmout: int = None) -> Optional[tuple[str, PingResult, str]]:
            """Tenta um exec_command. Retorna (template, PingResult, raw_output) ou None."""
            filled = _fill_cmd(cmd, tgt)
            t = tmout or (count * timeout_s + 12)
            try:
                output, _ = _exec_ssh_plain(client, filled, timeout=t)
                if _platform_valid(output, platform) or _is_valid_ping_output(output):
                    r = _platform_parse(output, count)
                    if r:
                        r.gateway = tgt
                        return cmd, r, output
            except Exception:
                pass
            return None

        def _try_interactive(cmd: str, tgt: str, tmout: int = None) -> Optional[tuple[str, PingResult]]:
            """Tenta um shell interativo. Retorna (template, PingResult) ou None."""
            filled = _fill_cmd(cmd, tgt)
            t = tmout or (count * timeout_s + 18)
            try:
                output, _ = _exec_ssh_interactive_command(client, filled, timeout=t)
                clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\r", "", output)
                if _platform_valid(clean, platform) or _is_valid_ping_output(clean):
                    r = _platform_parse(clean, count)
                    if r:
                        r.gateway = tgt
                        return cmd, r
            except Exception:
                pass
            return None

        def _try_ping(target: str) -> Optional[PingResult]:
            """
            Pinga um alvo via SSH. Usa cache do comando que funcionou.
            Máximo de 2-3 canais SSH na primeira chamada, 1 canal nas seguintes.
            """
            nonlocal _cached_cmd, _cached_mode, _cached_interactive_cmd

            # ── Fase 0: usar cache (1 canal) ────────────────────────────
            if _cached_cmd and _cached_mode == "exec":
                res = _try_exec(_cached_cmd, target)
                if res:
                    logger.debug(
                        f"SSH {host_ip} → {target}: OK via cache exec "
                        f"[{_fill_cmd(res[0], target).split()[0]}] "
                        f"{res[1].latency_ms:.1f}ms"
                    )
                    return res[1]
                # Cache falhou para este alvo — tenta interativo como fallback
                logger.debug(f"Cache exec falhou para {target}, tentando interativo")

            if _cached_interactive_cmd and _cached_mode == "interactive":
                res = _try_interactive(_cached_interactive_cmd, target)
                if res:
                    logger.debug(
                        f"SSH {host_ip} → {target}: OK via cache interativo "
                        f"[{_fill_cmd(res[0], target).split()[0]}] "
                        f"{res[1].latency_ms:.1f}ms"
                    )
                    return res[1]
                logger.debug(f"Cache interativo falhou para {target}")

            # Se cache existe mas falhou, tenta o outro modo como fallback
            if _cached_cmd and _cached_mode == "exec":
                # exec falhou — tenta interativo com o mesmo path
                inter_tpl = _cached_cmd.replace(" 2>&1", "")
                # Remove -W se presente
                inter_tpl = re.sub(r"\s+-W\s+\{TIMEOUT\}", "", inter_tpl)
                inter_tpl = re.sub(r"\s+-W\s+\d+", "", inter_tpl)
                res = _try_interactive(inter_tpl, target)
                if res:
                    _cached_interactive_cmd = res[0]
                    _cached_mode = "interactive"
                    logger.debug(f"Fallback interativo funcionou para {target}: {_fill_cmd(res[0], target).split()[0]}")
                    return res[1]

            # ── Fase 1: Discovery (apenas na primeira chamada) ──────────
            # Tenta comandos sem -W primeiro (pfSense não suporta -W)
            skip_W = False
            for cmd_tpl in _EXEC_CMDS:
                # Se já sabemos que -W é inválido, pula comandos com -W
                if skip_W and "-W" in cmd_tpl:
                    continue
                res = _try_exec(cmd_tpl, target)
                if res:
                    _cached_cmd = res[0]
                    _cached_mode = "exec"
                    cmd_used = _fill_cmd(res[0], target)
                    logger.info(
                        f"SSH {host_ip} → {target}: DISCOVERY OK "
                        f"[exec: {cmd_used.split()[0]}] "
                        f"{res[1].latency_ms:.1f}ms lat / {res[1].loss_pct:.0f}% perda"
                    )
                    return res[1]
                # Verifica se a saída indica "illegal option" (sem abrir canal extra)
                # _try_exec já capturou o output — checamos via _exec_ssh_plain output
                # que foi consumido internamente. Para evitar canal extra, detectamos
                # pela posição na lista: os primeiros 5 são sem -W, os últimos 4 com -W.
                # Se o primeiro com -W falha, marcamos skip.
                if "-W" in cmd_tpl and not skip_W:
                    skip_W = True  # se falhou com -W, pula os demais com -W

            # Interativo — fallback para pfSense onde exec falha
            for cmd_tpl in _INTERACTIVE_CMDS:
                res = _try_interactive(cmd_tpl, target)
                if res:
                    _cached_interactive_cmd = res[0]
                    _cached_mode = "interactive"
                    cmd_used = _fill_cmd(res[0], target)
                    logger.info(
                        f"SSH {host_ip} → {target}: DISCOVERY OK "
                        f"[interativo: {cmd_used.split()[0]}] "
                        f"{res[1].latency_ms:.1f}ms lat / {res[1].loss_pct:.0f}% perda"
                    )
                    return res[1]

            logger.warning(
                f"SSH {host_ip} → {target}: NENHUM COMANDO FUNCIONOU "
                f"(tentados {len(_EXEC_CMDS)} exec + {len(_INTERACTIVE_CMDS)} interativo)"
            )
            return None

        # ── Executa os 3 pings na mesma conexão SSH ───────────────────
        # 1. HOST IP (auto-teste) — serve também para descobrir o comando
        host_result = _try_ping(host_ip)
        if host_result:
            logger.debug(f"ping_triple SSH {host_ip} → HOST: {host_result.latency_ms:.1f}ms")
        else:
            logger.warning(f"ping_triple SSH {host_ip} → HOST: falhou (nenhum comando funcionou)")

        # 2. WAN IP (se configurado)
        wan_ip = (wan_target or "").strip()
        if wan_ip:
            wan_result = _try_ping(wan_ip)
            if wan_result:
                logger.debug(f"ping_triple SSH {host_ip} → WAN {wan_ip}: {wan_result.latency_ms:.1f}ms")
            else:
                logger.debug(f"ping_triple SSH {host_ip} → WAN {wan_ip}: tentando fallback -c 1 interativo")
                for path in ["ping", "/sbin/ping", "/bin/ping", "/usr/bin/ping", "/usr/local/bin/ping"]:
                    try:
                        cmd = f"{path} -c 1 {wan_ip}"
                        output, _ = _exec_ssh_interactive_command(client, cmd, timeout=15)
                        clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\r", "", output)
                        if _is_valid_ping_output(clean):
                            wan_result = _build_ping_result_from_output(
                                clean, 1, timestamp=datetime.now(), mode="SSH"
                            )
                            wan_result.gateway = wan_ip
                            wan_result.packets_sent = 1
                            logger.info(
                                f"ping_triple SSH {host_ip} → WAN {wan_ip}: "
                                f"OK via fallback extra [{path}] "
                                f"{wan_result.latency_ms:.1f}ms"
                            )
                            break
                    except Exception:
                        continue

                if not wan_result:
                    logger.warning(
                        f"ping_triple SSH {host_ip} → WAN {wan_ip}: FALHOU em todos os métodos — "
                        "verifique se o IP WAN está correto e acessível de dentro do host"
                    )
        else:
            logger.debug(f"ping_triple SSH {host_ip}: WAN IP não configurado — pulando")

        # 3. Google
        google_result = _try_ping(google_target)
        if google_result:
            logger.debug(f"ping_triple SSH {host_ip} → Google: {google_result.latency_ms:.1f}ms")

    except ImportError:
        logger.debug("paramiko não instalado — ping triplo SSH indisponível")

    except Exception as exc:
        logger.warning(f"ping_triple SSH {host_ip} falhou: {exc}")

    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    return host_result, wan_result, google_result


# ── MTR (My TraceRoute) — estatísticas contínuas por hop ─────────────────

def _parse_traceroute_text(output: str, target_ip: str) -> list[dict]:
    """Converte saída textual de traceroute em lista de dicts de hop."""
    hops: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r"^(traceroute|tracepath)\s+to", line, re.IGNORECASE):
            continue
        m = re.match(r"^\s*(\d+)\s+(.+)", line)
        if not m:
            continue
        hop_num = int(m.group(1))
        rest    = m.group(2)
        ip_matches = re.findall(r"(\d{1,3}(?:\.\d{1,3}){3})", rest)
        hop_ip = ip_matches[-1] if ip_matches else "*"
        if not ip_matches and "ms" not in rest.lower():
            hop_ip = "*"
        rtts = [float(v) for v in re.findall(r"([\d.]+)\s*ms", rest)[:3]]
        while len(rtts) < 3:
            rtts.append(0.0)
        hops.append({"hop": hop_num, "ip": hop_ip,
                     "rtt1": rtts[0], "rtt2": rtts[1], "rtt3": rtts[2]})
        if hop_ip == target_ip:
            break
    return hops


def _accumulate_hop_stats(hop_stats: dict, hops: list[dict]) -> None:
    """Acumula dados de uma rodada de traceroute no dicionário hop_stats."""
    for hop in hops:
        num  = hop["hop"]
        ip   = hop["ip"]
        rtts = [v for v in [hop.get("rtt1", 0), hop.get("rtt2", 0), hop.get("rtt3", 0)] if v > 0]
        if num not in hop_stats:
            hop_stats[num] = {
                "ip": ip, "sent": 0, "recv": 0,
                "all_rtts": [], "best": float("inf"),
                "worst": 0.0, "avg": 0.0, "last": 0.0, "loss_pct": 100,
            }
        s = hop_stats[num]
        s["ip"]   = ip
        s["sent"] += 3
        s["recv"] += len(rtts)
        s["all_rtts"].extend(rtts)
        if rtts:
            s["best"]  = min(s["best"], min(rtts))
            s["worst"] = max(s["worst"], max(rtts))
            s["last"]  = rtts[-1]
            s["avg"]   = statistics.mean(s["all_rtts"])
        if s["best"] == float("inf"):
            s["best"] = 0.0
        s["loss_pct"] = round((1 - s["recv"] / s["sent"]) * 100) if s["sent"] > 0 else 100


def run_mtr_local(
    target_ip: str,
    stop_event: threading.Event,
    on_round: Optional[callable] = None,
    timeout_ms: int = 500,
    max_hops: int = 20,
) -> dict:
    """MTR-style local: roda rodadas de traceroute continuamente."""
    hop_stats: dict[int, dict] = {}
    round_num = 0
    while not stop_event.is_set():
        round_num += 1
        try:
            result   = traceroute(target_ip, max_hops, timeout_ms)
            hops_raw = [
                {"hop": h.hop_number, "ip": h.ip,
                 "rtt1": h.rtt1, "rtt2": h.rtt2, "rtt3": h.rtt3}
                for h in result.hops
            ]
            _accumulate_hop_stats(hop_stats, hops_raw)
            if on_round:
                on_round(round_num, dict(hop_stats), "Local")
        except Exception as exc:
            logger.error(f"run_mtr_local rodada {round_num}: {exc}")
            if on_round:
                on_round(round_num, dict(hop_stats), f"Local (erro: {exc})")
    return hop_stats


def run_mtr_via_ssh(
    ssh_host: str,
    ssh_user: str,
    ssh_password: str,
    target_ip: str,
    ssh_port: int = 22,
    stop_event: Optional[threading.Event] = None,
    on_round: Optional[callable] = None,
    timeout_ms: int = 500,
    max_hops: int = 20,
) -> dict:
    """
    MTR-style via SSH com fallback para shell interativo.
    """
    try:
        import paramiko
    except ImportError:
        raise RuntimeError("paramiko não instalado — execute: pip install paramiko")

    hop_stats: dict[int, dict] = {}
    client = paramiko.SSHClient()
    from utils.security import TrustOnFirstUsePolicy; client.set_missing_host_key_policy(TrustOnFirstUsePolicy())

    try:
        client.connect(
            hostname=ssh_host, port=int(ssh_port or 22),
            username=ssh_user, password=ssh_password or "",
            timeout=12, allow_agent=False, look_for_keys=False,
        )

        timeout_s = max(1, timeout_ms // 1000)
        candidates = [
            (f"traceroute -n -m {max_hops} -w {timeout_s} {target_ip}", "traceroute -n"),
            (f"traceroute -m {max_hops} -w {timeout_s} {target_ip}",    "traceroute"),
            (f"/usr/sbin/traceroute -n -m {max_hops} -w {timeout_s} {target_ip}", "/usr/sbin/traceroute"),
            (f"/usr/bin/traceroute -n -m {max_hops} -w {timeout_s} {target_ip}",  "/usr/bin/traceroute"),
            (f"busybox traceroute -n -m {max_hops} -w {timeout_s} {target_ip}",   "busybox traceroute"),
            (f"tracepath -n {target_ip}",                                          "tracepath -n"),
        ]

        best_cmd   = None
        best_label = "SSH"
        use_interactive = False

        # Fase 1: exec_command
        for cmd, label in candidates:
            if stop_event and stop_event.is_set():
                break
            try:
                raw, _ = _exec_ssh_plain(client, cmd, timeout=90)
                clean  = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\r", "", raw)
                if re.search(r"^\s*\d+\s+", clean, re.MULTILINE):
                    best_cmd   = cmd
                    best_label = f"SSH:{ssh_host} ({label})"
                    hops_data = _parse_traceroute_text(clean, target_ip)
                    if hops_data:
                        _accumulate_hop_stats(hop_stats, hops_data)
                    if on_round:
                        on_round(1, dict(hop_stats), best_label)
                    break
            except Exception as exc:
                logger.debug(f"MTR SSH cmd '{label}' falhou: {exc}")
                continue

        # Fase 2: shell interativo
        if not best_cmd:
            logger.info(f"MTR SSH {ssh_host}: tentando shell interativo")
            for cmd, label in candidates[:4]:
                if stop_event and stop_event.is_set():
                    break
                try:
                    raw, _ = _exec_ssh_interactive_command(client, cmd, timeout=90)
                    clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\r", "", raw)
                    if re.search(r"^\s*\d+\s+", clean, re.MULTILINE):
                        best_cmd = cmd
                        best_label = f"SSH:{ssh_host} ({label} interativo)"
                        use_interactive = True
                        hops_data = _parse_traceroute_text(clean, target_ip)
                        if hops_data:
                            _accumulate_hop_stats(hop_stats, hops_data)
                        if on_round:
                            on_round(1, dict(hop_stats), best_label)
                        break
                except Exception as exc:
                    logger.debug(f"MTR interativo '{label}' falhou: {exc}")
                    continue

        if not best_cmd:
            raise RuntimeError(
                f"Nenhum comando traceroute encontrado em {ssh_host}. "
                "Instale 'traceroute' ou 'busybox' no host remoto."
            )

        # Loop de rodadas
        round_num = 2
        exec_fn = _exec_ssh_interactive_command if use_interactive else _exec_ssh_plain
        while not (stop_event and stop_event.is_set()):
            try:
                raw, _ = exec_fn(client, best_cmd, timeout=90)
                clean  = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\r", "", raw)
                hops_data = _parse_traceroute_text(clean, target_ip)
                if hops_data:
                    _accumulate_hop_stats(hop_stats, hops_data)
                if on_round:
                    on_round(round_num, dict(hop_stats), best_label)
                round_num += 1
            except Exception as exc:
                logger.error(f"MTR SSH rodada {round_num}: {exc}")
                if stop_event and stop_event.is_set():
                    break

    finally:
        try:
            client.close()
        except Exception:
            pass

    return hop_stats