"""
Controlador principal — v2.5: viewer via JSON snapshot (sem SQLite em rede).

ARQUITETURA MULTI-INSTÂNCIA:
  • Apenas UMA instância monitora (o "servidor"). As demais são "visualizadores".
  • O lock é um arquivo data/.monitor.lock com heartbeat a cada ciclo.
  • Ao abrir o app, se o lock existe e está fresco (< 120s), a instância
    entra em modo VISUALIZADOR automaticamente.

CORREÇÃO v2.5 — "file is not a database":
  Problema raiz: SQLite usa modo WAL (Write-Ahead Log), que exige locks de
  arquivo precisos. Pastas de rede Windows (SMB/CIFS) não implementam esses
  locks corretamente, causando o erro "file is not a database" sempre que
  o viewer tenta ler o DB compartilhado.

  Solução: o viewer NUNCA toca o SQLite. O servidor, após cada ciclo,
  grava um arquivo data/viewer_snapshot.json com o estado completo de
  todos os hosts. O viewer lê apenas esse JSON — um arquivo de texto
  simples, sem locks, sem WAL, 100% compatível com SMB.

  Fluxo:
    SERVIDOR:  pinga → atualiza memória → grava SQLite → grava snapshot.json
    VIEWER:    lê snapshot.json → atualiza memória → atualiza UI

  O snapshot é escrito atomicamente (grava em .tmp, depois renomeia) para
  evitar que o viewer leia um arquivo pela metade.
"""
import json
import os
import platform
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from config import MONITOR_DEFAULTS, THRESHOLDS, DATA_DIR, get_ssh_credentials, get_google_target, load_user_config, save_user_config
from models.database import Database
from models.host_model import Host, PingResult
from controllers.audio_controller import AudioController, ALERTA_DURATION_S
from utils.network import (
    discover_mtu, ping_host, ping_host_via_ssh, ping_triple_via_ssh,
    resolve_dns, traceroute, traceroute_via_ssh,
    run_mtr_local, run_mtr_via_ssh,
)
from utils.logger import setup_logger, read_recent_logs

logger = setup_logger("monitor")

# ── Paths ─────────────────────────────────────────────────────────────
LOCK_PATH      = DATA_DIR / ".monitor.lock"
SNAPSHOT_PATH  = DATA_DIR / "viewer_snapshot.json"   # escrito pelo servidor, lido pelo viewer
COMMANDS_PATH  = DATA_DIR / "viewer_commands.json"   # escrito pelo viewer, aplicado pelo servidor

LOCK_STALE_SECONDS = 120


# ══════════════════════════════════════════════════════════════════════
# COMANDO FILE — viewer escreve, servidor aplica
# ══════════════════════════════════════════════════════════════════════

def _append_command(cmd: str, **kwargs):
    """
    Acrescenta um comando à fila viewer_commands.json.
    Escrita atômica: lê + atualiza + grava via rename().
    Chamado pelos métodos CRUD quando em modo viewer.
    """
    tmp = COMMANDS_PATH.with_suffix(".ctmp")
    # Lê fila atual
    existing = []
    try:
        if COMMANDS_PATH.exists():
            with open(COMMANDS_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
    except Exception:
        existing = []
    existing.append({"cmd": cmd, "args": kwargs,
                     "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False)
        tmp.replace(COMMANDS_PATH)
    except Exception as e:
        logger.warning(f"Não foi possível gravar comando '{cmd}': {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _pop_commands() -> list:
    """
    Lê e apaga viewer_commands.json atomicamente.
    Chamado pelo servidor a cada ciclo.
    Retorna lista de comandos ou [] se não há nada.
    """
    if not COMMANDS_PATH.exists():
        return []
    try:
        with open(COMMANDS_PATH, "r", encoding="utf-8") as f:
            cmds = json.load(f)
        COMMANDS_PATH.unlink(missing_ok=True)
        return cmds if isinstance(cmds, list) else []
    except Exception:
        try:
            COMMANDS_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        return []


# ══════════════════════════════════════════════════════════════════════
# LOCK FILE
# ══════════════════════════════════════════════════════════════════════

def _machine_base_id() -> str:
    """
    Identidade BASE da máquina: hostname + usuário.
    Usada para comparação de lock — NÃO inclui PID.

    CORREÇÃO v2.10 — identidade incorreta:
      A versão anterior incluía o PID na identidade. Quando a mesma máquina
      fechava e reabria o .exe, o novo PID era diferente do PID gravado no
      lock file. Resultado: a máquina que deveria ser o servidor se via como
      "outra máquina" e entrava como viewer de si mesma.

      Agora, a comparação de identidade usa apenas hostname/user. O PID é
      incluído apenas no display (para debugging) mas não na comparação.
    """
    hostname = platform.node() or "unknown"
    user = os.environ.get("USERNAME", os.environ.get("USER", "?"))
    return f"{hostname}/{user}"


def _this_machine_id() -> str:
    """Identidade completa para exibição (inclui PID para debugging)."""
    return f"{_machine_base_id()} (PID {os.getpid()})"


def _read_lock() -> Optional[dict]:
    try:
        if LOCK_PATH.exists():
            with open(LOCK_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _write_lock(machine_id: str):
    data = {
        "machine": machine_id,
        "base_id": _machine_base_id(),
        "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "heartbeat": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(LOCK_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"Não foi possível criar lock file: {e}")


def _update_lock_heartbeat() -> bool:
    """
    Atualiza o heartbeat do lock file.

    CORREÇÃO v2.10 — verifica se o lock ainda pertence a esta máquina:
      Retorna True se o heartbeat foi atualizado com sucesso.
      Retorna False se o lock foi sobrescrito por outra máquina (takeover).

      Sem esta verificação, quando a Máquina B clicava "Assumir Servidor",
      ela sobrescrevia o lock. Mas a Máquina A, no próximo ciclo, chamava
      _update_lock_heartbeat() que lia o lock da B e ATUALIZAVA o heartbeat
      — efetivamente "roubando" o lock de volta, porque o mtime ficava
      fresco e a B via o lock como ativo.

      Agora, se o base_id do lock não bate com esta máquina, o heartbeat
      NÃO é atualizado e retorna False para o _monitor_loop demover.
    """
    try:
        lock = _read_lock()
        if not lock:
            return False
        # Verifica se o lock ainda é desta máquina
        lock_base = lock.get("base_id", "")
        my_base = _machine_base_id()
        if lock_base and lock_base != my_base:
            # Outra máquina assumiu o lock — NÃO atualizar
            return False
        lock["heartbeat"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOCK_PATH, "w", encoding="utf-8") as f:
            json.dump(lock, f, indent=2)
        return True
    except Exception:
        return True  # em caso de erro de I/O, assume que ainda é dono


def _release_lock():
    try:
        if LOCK_PATH.exists():
            LOCK_PATH.unlink()
    except Exception:
        pass


def _is_lock_stale() -> bool:
    """
    Usa mtime do arquivo no filesystem — mais confiável que o conteúdo JSON
    em pastas SMB, onde o conteúdo pode ficar em cache local por segundos.
    """
    try:
        if not LOCK_PATH.exists():
            return True
        return (time.time() - LOCK_PATH.stat().st_mtime) > LOCK_STALE_SECONDS
    except Exception:
        return True


# ══════════════════════════════════════════════════════════════════════
# SNAPSHOT JSON — escrito pelo servidor, lido pelo viewer
# ══════════════════════════════════════════════════════════════════════

def _write_snapshot(hosts: dict, alerts: list, stats_cache: dict = None,
                    alerts_history: list = None, server_log_lines: list = None):
    """
    Grava o estado atual de todos os hosts em viewer_snapshot.json.

    Escrita ATÔMICA: grava em .tmp e depois faz replace() (rename).

    v2.7: inclui server_log_lines — as últimas 200 linhas do log do servidor,
    para que o viewer exiba a aba "Logs do Sistema" com dados reais em vez
    de linhas do log local (que no viewer está vazio).
    """
    if stats_cache is None:
        stats_cache = {}
    hosts_data = []
    for host in hosts.values():
        if not host.enabled:
            continue
        hosts_data.append({
            "id":                   host.id,
            "ip":                   host.ip,
            "label":                host.label,
            "group_name":           host.group_name,
            "wan_ip":               host.wan_ip,
            "wan_ip_2":             host.wan_ip_2,
            "wan_ip_3":             host.wan_ip_3,
            "ssh_port":             host.ssh_port,
            "platform":             host.platform,
            "enabled":              host.enabled,
            "status":               host.status,
            "last_ping_mode":       host.last_ping_mode,
            "consecutive_failures": host.consecutive_failures,
            "alerts_fired":         host.alerts_fired,
            "offline_since":        host.offline_since.strftime("%Y-%m-%d %H:%M:%S") if host.offline_since else None,
            "last_seen":            host.last_seen.strftime("%Y-%m-%d %H:%M:%S") if host.last_seen else None,
            "last_collection_ts":   host.last_collection_ts.strftime("%Y-%m-%d %H:%M:%S") if host.last_collection_ts else None,
            # Contadores de ciclo — necessários para o painel de Estatísticas
            "ttl":                  host.ping_history[-1].ttl if host.ping_history else 0,
            "cycle_number":         host.cycle_number,
            "ping_in_cycle":        host.ping_in_cycle,
            "total_pings_all_time": host.total_pings_all_time,
            # Ping local
            "latency_ms":           host.current_latency,
            "jitter_ms":            host.current_jitter,
            "loss_pct":             host.current_loss,
            "rtt":                  host.current_rtt,
            "availability":         host.availability_recent,
            # HOST SSH
            "host_ssh_latency":     host.host_ssh_latency,
            "host_ssh_jitter":      host.host_ssh_jitter,
            "host_ssh_loss":        host.host_ssh_loss,
            "host_ssh_rtt":         host.host_ssh_rtt,
            "host_ssh_avail":       host.host_ssh_avail,
            "host_ssh_has_data":    host.host_ssh_has_data,
            "host_ssh_source":      host.host_ssh_source,
            # WAN
            "wan_latency":          host.wan_latency,
            "wan_jitter":           host.wan_jitter,
            "wan_loss":             host.wan_loss,
            "wan_rtt":              host.wan_rtt,
            "wan_avail":            host.wan_avail,
            "wan_has_data":         host.wan_has_data,
            # Google
            "google_latency":       host.google_latency,
            "google_jitter":        host.google_jitter,
            "google_loss":          host.google_loss,
            "google_rtt":           host.google_rtt,
            "google_avail":         host.google_avail,
            "google_has_data":      host.google_has_data,
            # Estatísticas 24h (incluídas do cache do servidor)
            "stats_24h":            stats_cache.get(host.id, {}),
        })

    snapshot = {
        "version":   "2.7",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "hosts":     hosts_data,
        "alerts":    alerts,
        "alerts_history": alerts_history or [],
        "server_log_lines": server_log_lines or [],
    }

    tmp_path = SNAPSHOT_PATH.with_suffix(".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
        # Retry loop para WinError 5 (PermissionError) — antivírus ou outro
        # processo pode abrir o .tmp para scan exatamente no momento do rename,
        # causando falha esporádica. 3 tentativas com 150ms entre elas resolvem.
        for _attempt in range(3):
            try:
                tmp_path.replace(SNAPSHOT_PATH)
                return  # sucesso
            except PermissionError:
                if _attempt < 2:
                    time.sleep(0.15)
        # Se ainda falhou após retries, loga como warning
        logger.warning("Snapshot: não foi possível renomear após 3 tentativas (arquivo em uso)")
    except Exception as e:
        logger.warning(f"Não foi possível gravar snapshot: {e}")
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _read_snapshot() -> Optional[dict]:
    """Lê viewer_snapshot.json. Retorna None se não existe ou está corrompido."""
    try:
        if SNAPSHOT_PATH.exists():
            with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.debug(f"Snapshot ilegível: {e}")
    return None


def _snapshot_is_fresh(max_age_s: float = 60.0) -> bool:
    """True se o snapshot foi gravado recentemente (servidor ainda ativo)."""
    try:
        if not SNAPSHOT_PATH.exists():
            return False
        return (time.time() - SNAPSHOT_PATH.stat().st_mtime) < max_age_s
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════
# HELPER — aplica dados do snapshot a um objeto Host existente
# ══════════════════════════════════════════════════════════════════════

def _apply_snapshot_to_host(host: Host, data: dict):
    """
    Aplica os campos do snapshot JSON diretamente nos atributos do Host.

    Isso substitui completamente a necessidade de popular ping_history
    a partir do SQLite — os valores calculados (latência, perda, avail)
    já vêm prontos no snapshot, gerados pelo servidor que fez os pings reais.
    """
    host.status               = data.get("status", "unknown")
    host.last_ping_mode       = data.get("last_ping_mode", "PING")
    host.consecutive_failures = int(data.get("consecutive_failures", 0))
    # CORREÇÃO v2.6 — alerts_fired ausente do snapshot:
    #   Sem este campo, ao reiniciar o servidor qualquer host offline tinha
    #   alerts_fired = 0, fazendo o schedule de alertas recomeçar do zero:
    #   "ficou OFFLINE" era re-disparado imediatamente para hosts que já
    #   estavam offline há horas, e os marcos de 5min/10min/30min eram
    #   ignorados até o host voltar online e sair offline novamente.
    host.alerts_fired = int(data.get("alerts_fired", 0))
    # Contadores de ciclo — necessários para exibir no painel Estatísticas
    host.cycle_number         = int(data.get("cycle_number", 0))
    host.ping_in_cycle        = int(data.get("ping_in_cycle", 0))
    host.total_pings_all_time = int(data.get("total_pings_all_time", 0))

    def _parse_dt(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S") if s else None
        except Exception:
            return None

    host.offline_since      = _parse_dt(data.get("offline_since"))
    host.last_seen          = _parse_dt(data.get("last_seen"))
    host.last_collection_ts = _parse_dt(data.get("last_collection_ts"))

    # Injeta um PingResult sintético em ping_history para que
    # current_latency, current_loss, current_jitter e host_ssh_has_data
    # retornem os valores corretos (eles leem de ping_history[-1]).
    latency = float(data.get("latency_ms", 0.0) or 0.0)
    loss    = float(data.get("loss_pct",   0.0) or 0.0)
    jitter  = float(data.get("jitter_ms",  0.0) or 0.0)
    ttl     = int(data.get("ttl", 0) or 0)
    ts      = host.last_collection_ts or datetime.now()

    synthetic = PingResult(
        timestamp=ts,
        latency_ms=latency,
        jitter_ms=jitter,
        loss_pct=loss,
        ttl=ttl,
        status=host.status,
        ping_mode="PING",
    )

    # Substitui o último elemento apenas se o timestamp for diferente
    # (evita acumular duplicatas a cada ciclo do viewer)
    if (not host.ping_history or
            host.ping_history[-1].timestamp != synthetic.timestamp):
        host.ping_history.append(synthetic)

    # SSH / WAN / Google: injeta ou LIMPA dados para garantir que o viewer
    # reflita exatamente o estado atual do servidor.
    #
    # CORREÇÃO — dados obsoletos no viewer:
    #   Sem o `else` abaixo, quando um host perdia o dado WAN num ciclo
    #   (ex.: wan_ip não configurado, SSH falhou só no WAN), o viewer
    #   continuava mostrando os valores do ciclo anterior porque
    #   wan_ping_last nunca era resetado para None.
    #   Isso causava discrepância entre servidor (mostra "—") e viewer
    #   (mostra valor antigo), confundindo o operador.

    def _fill_avail_history(history_deque, avail_pct: float, last_pr: PingResult):
        """Popula o deque com 10 PingResults que aproximam a disponibilidade."""
        n = 10
        online_n = max(0, min(n, round(float(avail_pct or 0.0) / 100 * n)))
        for i in range(n - 1):
            history_deque.append(PingResult(
                timestamp=ts,
                loss_pct=0.0 if i < online_n else 100.0,
                status="online" if i < online_n else "offline",
                ping_mode=last_pr.ping_mode,
            ))
        history_deque.append(last_pr)  # último com valores reais

    if data.get("host_ssh_has_data"):
        host_ssh_pr = PingResult(
            timestamp=ts,
            latency_ms=float(data.get("host_ssh_latency", 0.0) or 0.0),
            jitter_ms= float(data.get("host_ssh_jitter",  0.0) or 0.0),
            loss_pct=  float(data.get("host_ssh_loss",    0.0) or 0.0),
            status=host.status, ping_mode="SSH",
        )
        host.host_ssh_ping_last = host_ssh_pr
        _fill_avail_history(host.host_ssh_history,
                            float(data.get("host_ssh_avail", 100.0) or 100.0),
                            host_ssh_pr)
    else:
        # Sem dado SSH neste ciclo — limpa para que "—" apareça corretamente
        host.host_ssh_ping_last = None
        host.host_ssh_history.clear()

    if data.get("wan_has_data"):
        wan_pr = PingResult(
            timestamp=ts,
            latency_ms=float(data.get("wan_latency", 0.0) or 0.0),
            jitter_ms= float(data.get("wan_jitter",  0.0) or 0.0),
            loss_pct=  float(data.get("wan_loss",    0.0) or 0.0),
            status=host.status, ping_mode="SSH",
        )
        host.wan_ping_last = wan_pr
        _fill_avail_history(host.wan_history,
                            float(data.get("wan_avail", 100.0) or 100.0),
                            wan_pr)
    else:
        # Sem dado WAN neste ciclo (wan_ip não configurado ou SSH falhou) — limpa
        host.wan_ping_last = None
        host.wan_history.clear()

    if data.get("google_has_data"):
        google_pr = PingResult(
            timestamp=ts,
            latency_ms=float(data.get("google_latency", 0.0) or 0.0),
            jitter_ms= float(data.get("google_jitter",  0.0) or 0.0),
            loss_pct=  float(data.get("google_loss",    0.0) or 0.0),
            status=host.status, ping_mode="SSH",
        )
        host.google_ping_last = google_pr
        _fill_avail_history(host.google_history,
                            float(data.get("google_avail", 100.0) or 100.0),
                            google_pr)
    else:
        # Sem dado Google neste ciclo — limpa
        host.google_ping_last = None
        host.google_history.clear()


# ══════════════════════════════════════════════════════════════════════
# CONTROLLER
# ══════════════════════════════════════════════════════════════════════

class MonitorController:
    def __init__(self, db: Database, audio: AudioController):
        self.db = db
        self.audio = audio
        self.hosts: dict[int, Host] = {}
        self._running = False
        self._paused = False
        self._viewer_mode = False
        self._monitor_owner = ""
        self._thread: Optional[threading.Thread] = None
        self._pool: Optional[ThreadPoolExecutor] = None
        self._lock = threading.Lock()
        self._on_host_updated: Optional[Callable] = None
        self._on_cycle_complete: Optional[Callable] = None
        self._on_alert: Optional[Callable] = None
        # CORREÇÃO v2.10 — callback de failover: quando o viewer detecta que
        # o servidor morreu (lock stale), chama este callback para promover
        # a instância de viewer para servidor automaticamente.
        self._on_failover: Optional[Callable] = None
        # Callback de demoção: quando o servidor detecta que outra máquina
        # assumiu o lock (via "Assumir Servidor"), chama este callback para
        # demover a instância de servidor para viewer automaticamente.
        self._on_demoted: Optional[Callable] = None
        self.ping_count    = MONITOR_DEFAULTS["ping_count"]
        self.ping_timeout  = MONITOR_DEFAULTS["ping_timeout_ms"]
        self.cycle_delay   = MONITOR_DEFAULTS["cycle_delay_s"]
        self.between_hosts = MONITOR_DEFAULTS["between_hosts_s"]

        # CORREÇÃO v2.6 — parâmetros não eram carregados do config.json no startup:
        #   MonitorController sempre iniciava com os padrões do código, ignorando
        #   o que o usuário tivesse salvo em Configurações → Parâmetros de Monitoramento.
        #   Ao reiniciar o app, ping_count, cycle_delay etc. voltavam ao padrão.
        #   Correção: aplica os valores salvos sobre os defaults logo abaixo.
        #
        #   Os thresholds (THRESHOLDS dict) também são atualizados aqui para que
        #   _process_alerts() use os valores salvos desde o primeiro ciclo.
        try:
            _cfg = load_user_config()
            if _cfg.get("ping_count"):
                self.ping_count    = int(_cfg["ping_count"])
            if _cfg.get("ping_timeout"):
                self.ping_timeout  = int(_cfg["ping_timeout"])
            if _cfg.get("cycle_delay"):
                self.cycle_delay   = int(_cfg["cycle_delay"])
            if _cfg.get("between_hosts"):
                self.between_hosts = float(_cfg["between_hosts"])
            # Restaura thresholds salvos no THRESHOLDS dict em memória
            _threshold_map = {
                "lat_warn":  ("latency_warning_ms",  int),
                "lat_crit":  ("latency_critical_ms", int),
                "jit_warn":  ("jitter_warning_ms",   int),
                "jit_crit":  ("jitter_critical_ms",  int),
                "loss_warn": ("loss_warning_pct",    float),
                "loss_crit": ("loss_critical_pct",   float),
            }
            for cfg_key, (thresh_key, cast) in _threshold_map.items():
                raw = _cfg.get(cfg_key, "")
                if raw:
                    try:
                        THRESHOLDS[thresh_key] = cast(raw)
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass  # qualquer erro → mantém os padrões do código
        self._machine_id   = _this_machine_id()
        # Cache de estatísticas 24h por host_id (atualizado a cada 5 ciclos no servidor;
        # populado a partir do snapshot no viewer)
        self._stats_cache: dict[int, dict] = {}
        # Cache das últimas linhas de log do servidor (incluídas no snapshot para o viewer)
        self._server_log_lines: list[str] = []
        # Rastreia último status de cada host no viewer para disparar áudio
        self._viewer_prev_statuses: dict[int, str] = {}
        # CORREÇÃO v2.9 — rastreia IDs de alertas já vistos pelo viewer.
        # O viewer anterior só detectava transições online↔offline, mas não
        # reproduzia os alertas agendados (5min, 10min, 30min, 1h) que o
        # servidor cria via _process_alerts(). Agora o viewer compara os
        # IDs dos alertas no snapshot e reproduz áudio para alertas novos.
        self._viewer_seen_alert_ids: set[int] = set()
        self._viewer_alerts_initialized: bool = False
        self._load_hosts()

    def set_on_host_updated(self, cb):   self._on_host_updated  = cb
    def set_on_cycle_complete(self, cb): self._on_cycle_complete = cb
    def set_on_alert(self, cb):          self._on_alert          = cb
    def set_on_failover(self, cb):       self._on_failover       = cb
    def set_on_demoted(self, cb):        self._on_demoted        = cb

    # ── Propriedades de modo ──────────────────────────────────────────

    @property
    def is_viewer(self) -> bool:
        return self._viewer_mode

    @property
    def monitor_owner(self) -> str:
        return self._monitor_owner

    def check_monitor_available(self) -> tuple[bool, str]:
        """
        Verifica se é possível iniciar o monitoramento.

        CORREÇÃO v2.10 — comparação de identidade sem PID:
          Compara apenas hostname/user (base_id) em vez da identidade
          completa (que inclui PID). Assim, a mesma máquina reaberta
          (novo PID) reconhece o lock como seu e reassume o servidor.
        """
        lock = _read_lock()
        if lock is None:
            return True, "Nenhuma instancia monitorando — pode iniciar."
        if _is_lock_stale():
            return True, f"Lock antigo de {lock.get('machine','?')} — pode assumir."
        # Compara base_id (hostname/user) — ignora PID
        lock_base = lock.get("base_id", lock.get("machine", "?"))
        my_base = _machine_base_id()
        if lock_base == my_base:
            return True, "Reassumindo monitoramento anterior."
        self._monitor_owner = lock.get("machine", "?")
        return False, f"Monitoramento ativo em: {self._monitor_owner}"

    def get_monitor_owner(self) -> Optional[str]:
        if _is_lock_stale():
            return None
        lock = _read_lock()
        return lock.get("machine", "?") if lock else None

    # ── Load hosts (startup) ──────────────────────────────────────────

    def _load_hosts(self):
        """
        Carrega hosts no startup.

        Tenta o snapshot JSON primeiro — evita tocar o SQLite via rede SMB.
        Se o snapshot ainda não existe (primeiro boot do servidor), faz
        uma leitura do SQLite como fallback.
        """
        snapshot = _read_snapshot()
        if snapshot and snapshot.get("hosts"):
            self._load_hosts_from_snapshot(snapshot)
            logger.info(f"Hosts carregados do snapshot ({len(self.hosts)} hosts)")
            return

        try:
            self._load_hosts_from_db()
            logger.info(f"Hosts carregados do DB ({len(self.hosts)} hosts)")
        except Exception as e:
            logger.warning(f"Não foi possível carregar hosts do DB: {e}")

    def _load_hosts_from_db(self):
        """Carrega hosts do SQLite (servidor ou primeiro boot antes do snapshot existir)."""
        for r in self.db.get_hosts(enabled_only=False):
            host = Host(
                id=r["id"], ip=r["ip"], label=r["label"],
                group_name=r["group_name"],
                ssh_user=r.get("ssh_user", ""),
                ssh_password=r.get("ssh_password", ""),
                ssh_port=r.get("ssh_port", 22),
                wan_ip=r.get("wan_ip", ""),
                wan_ip_2=r.get("wan_ip_2", ""),
                wan_ip_3=r.get("wan_ip_3", ""),
                platform=r.get("platform", ""),
                enabled=bool(r["enabled"]),
            )
            try:
                latest = self.db.get_latest_ping(host.id)
                if latest:
                    host.status = latest["status"]
            except Exception:
                pass
            self.hosts[host.id] = host

    def _load_hosts_from_snapshot(self, snapshot: dict):
        """Popula self.hosts a partir do snapshot JSON (viewer)."""
        for h in snapshot.get("hosts", []):
            host = Host(
                id=h["id"], ip=h["ip"], label=h.get("label", ""),
                group_name=h.get("group_name", "Geral"),
                ssh_user="", ssh_password="",
                ssh_port=h.get("ssh_port", 22),
                wan_ip=h.get("wan_ip", ""),
                wan_ip_2=h.get("wan_ip_2", ""),
                wan_ip_3=h.get("wan_ip_3", ""),
                platform=h.get("platform", ""),
                enabled=h.get("enabled", True),
            )
            _apply_snapshot_to_host(host, h)
            self.hosts[host.id] = host

    # ── Reload hosts (viewer loop) ────────────────────────────────────

    def _reload_from_snapshot(self):
        """
        Atualiza self.hosts a partir do snapshot gravado pelo servidor.
        Chamado a cada ciclo do _viewer_loop — nunca acessa o SQLite.
        """
        snapshot = _read_snapshot()
        if not snapshot:
            return

        snap_hosts = {h["id"]: h for h in snapshot.get("hosts", [])}

        # Adiciona hosts novos
        for hid, h in snap_hosts.items():
            if hid not in self.hosts:
                host = Host(
                    id=h["id"], ip=h["ip"], label=h.get("label", ""),
                    group_name=h.get("group_name", "Geral"),
                    ssh_user="", ssh_password="",
                    ssh_port=h.get("ssh_port", 22),
                    wan_ip=h.get("wan_ip", ""),
                    wan_ip_2=h.get("wan_ip_2", ""),
                    wan_ip_3=h.get("wan_ip_3", ""),
                    platform=h.get("platform", ""),
                    enabled=h.get("enabled", True),
                )
                _apply_snapshot_to_host(host, h)
                self.hosts[hid] = host

        # Remove hosts deletados
        for hid in list(self.hosts.keys()):
            if hid not in snap_hosts:
                self.hosts.pop(hid)
                # CORREÇÃO v2.6 — entrada stale em _viewer_prev_statuses:
                #   Entradas de hosts removidos ficavam indefinidamente no
                #   dicionário. Em cenário improvável de reutilização de ID
                #   (restaurar host deletado com mesmo IP → mesmo ID pelo DB),
                #   o viewer poderia disparar áudio indevido na primeira aparição
                #   do host "novo", pois encontraria um prev status antigo.
                self._viewer_prev_statuses.pop(hid, None)

        # Atualiza hosts existentes
        for hid, host in self.hosts.items():
            if hid in snap_hosts:
                _apply_snapshot_to_host(host, snap_hosts[hid])

        # Popula cache de estatísticas 24h a partir do snapshot
        for h_data in snapshot.get("hosts", []):
            stats = h_data.get("stats_24h")
            if stats:
                self._stats_cache[h_data["id"]] = stats

        # ── Detecção de alertas para áudio no visualizador ────────────────
        #
        # CORREÇÃO v2.9 — o viewer agora reproduz áudio para TODOS os alertas
        # que o servidor cria (incluindo os agendados: 5min, 10min, 30min, 1h).
        #
        # A versão anterior só detectava transições de status (online↔offline),
        # então só tocava na primeira vez que o host caía. Os alertas de repeat
        # que o servidor gera via _process_alerts() (com schedule de 5min, 10min
        # etc.) nunca eram reproduzidos pelo viewer.
        #
        # Novo mecanismo: compara os IDs dos alertas no snapshot com os já vistos.
        # Alertas com IDs novos = alertas que o servidor acabou de criar.
        # Para cada novo alerta offline, reproduz sirene + voz da loja.
        # Para cada novo alerta online (volta), reproduz voz online.
        #
        # No primeiro ciclo, apenas popula o set sem tocar áudio (evita
        # replay de todos os alertas históricos ao abrir o viewer).

        snap_alerts = snapshot.get("alerts_history", [])
        if not snap_alerts:
            snap_alerts = snapshot.get("alerts", [])

        current_alert_ids = set()
        for a in snap_alerts:
            aid = a.get("id")
            if aid is not None:
                current_alert_ids.add(aid)

        if not self._viewer_alerts_initialized:
            # Primeiro ciclo: popula sem tocar áudio
            self._viewer_seen_alert_ids = current_alert_ids.copy()
            self._viewer_alerts_initialized = True
            logger.debug(f"Viewer: inicializado rastreamento de alertas ({len(current_alert_ids)} IDs)")
        else:
            new_ids = current_alert_ids - self._viewer_seen_alert_ids
            if new_ids and not self.audio.muted:
                for a in snap_alerts:
                    aid = a.get("id")
                    if aid not in new_ids:
                        continue
                    alert_type = a.get("alert_type", "")
                    host_label = a.get("label", "") or a.get("ip", "")
                    host_id = a.get("host_id")

                    if alert_type == "offline" and host_label:
                        logger.info(
                            f"[VIEWER] Novo alerta offline detectado — "
                            f"{host_label} (alert_id={aid})"
                        )
                        def _play_offline(lbl=host_label):
                            try:
                                self.audio.play_generic_alert()
                                time.sleep(ALERTA_DURATION_S + 0.5)
                                self.audio.play_alert(lbl, "offline")
                            except Exception:
                                pass
                        threading.Thread(
                            target=_play_offline, daemon=True).start()
                        break  # Um alerta por ciclo para não sobrecarregar

            # Detecta transições offline→online para áudio de "voltou"
            # (alertas online não ficam em alerts_history, então usamos
            # a detecção de status como antes, mas só para online)
            for hid, host in self.hosts.items():
                prev = self._viewer_prev_statuses.get(hid)
                curr = host.status
                if (not self.audio.muted and prev is not None
                        and prev == "offline" and curr == "online"):
                    # Suprime áudio para hosts recém-adicionados
                    if host.total_pings_all_time > 3:
                        label = host.display_name
                        threading.Thread(
                            target=lambda l=label: self.audio.play_alert(l, "online"),
                            daemon=True).start()

            self._viewer_seen_alert_ids = current_alert_ids.copy()

        # Atualiza prev_statuses (sempre, independente de mute)
        for hid, host in self.hosts.items():
            self._viewer_prev_statuses[hid] = host.status
        # Limpa entradas de hosts removidos
        for hid in list(self._viewer_prev_statuses.keys()):
            if hid not in self.hosts:
                self._viewer_prev_statuses.pop(hid, None)

    # ── CRUD de hosts ─────────────────────────────────────────────────

    def add_host(self, ip, label="", group="Geral", ssh_user="",
                 ssh_password="", ssh_port=22, wan_ip="", wan_ip_2="",
                 wan_ip_3="", platform=""):
        if not ssh_user:
            ssh_user, ssh_password = get_ssh_credentials()
        if self._viewer_mode:
            # CORREÇÃO v2.12 — criptografa senha antes de gravar em viewer_commands.json
            from utils.security import encrypt_password
            ssh_pwd_enc = encrypt_password(ssh_password) if ssh_password else ""
            _append_command("add_host", ip=ip, label=label, group=group,
                            ssh_user=ssh_user, ssh_password=ssh_pwd_enc,
                            ssh_port=ssh_port, wan_ip=wan_ip,
                            wan_ip_2=wan_ip_2, wan_ip_3=wan_ip_3,
                            platform=platform)
            logger.info(f"[VIEWER] Comando add_host enfileirado para {ip}")
            return None
        host_id = self.db.add_host(ip, label, group, ssh_user, ssh_password, ssh_port,
                                   wan_ip=wan_ip, wan_ip_2=wan_ip_2, wan_ip_3=wan_ip_3,
                                   platform=platform)
        host = Host(id=host_id, ip=ip, label=label, group_name=group,
                    ssh_user=ssh_user, ssh_password=ssh_password,
                    ssh_port=ssh_port, wan_ip=wan_ip,
                    wan_ip_2=wan_ip_2, wan_ip_3=wan_ip_3,
                    platform=platform)
        self.hosts[host_id] = host
        return host

    def remove_host(self, host_id):
        if self._viewer_mode:
            host = self.hosts.get(host_id)
            label = host.display_name if host else ""
            _append_command("remove_host", host_id=host_id, label=label)
            logger.info(f"[VIEWER] Comando remove_host enfileirado para id={host_id}")
            # Remove localmente imediato para a UI refletir sem esperar ciclo
            if host_id in self.hosts:
                self.hosts.pop(host_id)
            # CORREÇÃO v2.6: deleta TODOS os áudios do host (não só cache local).
            # Antes, só limpava o cache temp — os MP3 na pasta compartilhada
            # ficavam órfãos (online/offline). delete_host_alerts() cuida de:
            #   1. Parar pygame e liberar handles
            #   2. Apagar os MP3 da pasta AUDIO_DIR (compartilhada)
            #   3. Limpar cópias do cache temp local
            if label:
                self.audio.delete_host_alerts(label)
            return
        if host_id in self.hosts:
            h = self.hosts.pop(host_id)
            self.db.remove_host(host_id)
            self.audio.delete_host_alerts(h.display_name)

    def update_host(self, host_id, **kwargs):
        if self._viewer_mode:
            # CORREÇÃO v2.12 — criptografa senhas antes de enviar pela fila
            from utils.security import encrypt_password
            kwargs_enc = dict(kwargs)
            if kwargs_enc.get("ssh_password"):
                kwargs_enc["ssh_password"] = encrypt_password(kwargs_enc["ssh_password"])
            if kwargs_enc.get("cisco_enable_password"):
                kwargs_enc["cisco_enable_password"] = encrypt_password(
                    kwargs_enc["cisco_enable_password"])
            _append_command("update_host", host_id=host_id, **kwargs_enc)
            logger.info(f"[VIEWER] Comando update_host enfileirado para id={host_id}")
            # Atualiza localmente imediato para a UI refletir sem esperar ciclo
            if host_id in self.hosts:
                host = self.hosts[host_id]
                for k, v in kwargs.items():
                    if hasattr(host, k):
                        setattr(host, k, v)
            return
        if host_id in self.hosts:
            host = self.hosts[host_id]
            for k, v in kwargs.items():
                if hasattr(host, k): setattr(host, k, v)
            self.db.update_host(host_id, **kwargs)

    def rename_group(self, old_name, new_name):
        if self._viewer_mode:
            _append_command("rename_group", old_name=old_name, new_name=new_name)
            logger.info(f"[VIEWER] Comando rename_group enfileirado: {old_name} → {new_name}")
            for h in self.hosts.values():
                if h.group_name == old_name:
                    h.group_name = new_name
            return 0
        count = self.db.rename_group(old_name, new_name)
        for h in self.hosts.values():
            if h.group_name == old_name: h.group_name = new_name
        return count

    def get_group_names(self):
        if self._viewer_mode:
            return sorted({h.group_name for h in self.hosts.values()})
        return self.db.get_group_names()

    def get_host(self, host_id):   return self.hosts.get(host_id)
    def get_host_by_ip(self, ip):  return next((h for h in self.hosts.values() if h.ip == ip), None)
    def get_all_hosts(self):       return list(self.hosts.values())

    def get_hosts_by_group(self):
        groups = {}
        for h in self.hosts.values(): groups.setdefault(h.group_name, []).append(h)
        return groups

    def send_config_update(self, config_dict: dict):
        """
        CORREÇÃO v2.9 — envia configurações do viewer para o servidor.

        Quando o viewer salva configurações (SSH, parâmetros, thresholds),
        este método enfileira um comando 'update_config' que o servidor
        aplica no próximo ciclo.

        O viewer também aplica os parâmetros localmente em memória para
        refletir na UI imediatamente (embora no viewer os parâmetros de
        monitoramento não afetem pings — apenas afetam a exibição de
        thresholds e o cycle_delay do viewer loop).
        """
        if self._viewer_mode:
            # CORREÇÃO v2.12 — criptografa senha antes de enfileirar
            # (viewer_commands.json fica no SMB compartilhado).
            cfg_to_send = dict(config_dict)
            if cfg_to_send.get("ssh_default_password"):
                from utils.security import encrypt_password
                cfg_to_send["ssh_default_password"] = encrypt_password(
                    cfg_to_send["ssh_default_password"])
            _append_command("update_config", config=cfg_to_send)
            logger.info("[VIEWER] Comando update_config enfileirado")
            # Aplica localmente para refletir na UI do viewer
            try:
                if config_dict.get("ping_count"):
                    self.ping_count = int(config_dict["ping_count"])
                if config_dict.get("ping_timeout"):
                    self.ping_timeout = int(config_dict["ping_timeout"])
                if config_dict.get("cycle_delay"):
                    self.cycle_delay = int(config_dict["cycle_delay"])
                if config_dict.get("between_hosts"):
                    self.between_hosts = float(config_dict["between_hosts"])
            except (ValueError, TypeError):
                pass
        else:
            # Servidor: salva diretamente (chamado pela settings_view normal)
            save_user_config(config_dict)

    # ── Aplica comandos do viewer (chamado pelo servidor no início do ciclo)
    def _apply_viewer_commands(self):
        """
        Lê viewer_commands.json (se existir) e aplica cada comando no DB.
        Chamado pelo servidor no início de cada ciclo. O viewer não tem
        acesso direto ao SQLite via SMB — usa esta fila de comandos.
        """
        cmds = _pop_commands()
        if not cmds:
            return
        for item in cmds:
            cmd  = item.get("cmd", "")
            args = item.get("args", {})
            try:
                if cmd == "add_host":
                    ip  = args.get("ip", "")
                    if not ip:
                        continue
                    existing = self.db.get_host_by_ip(ip)
                    if existing:
                        continue  # já existe, ignora duplicata
                    host_id = self.db.add_host(
                        ip, args.get("label", ""), args.get("group", "Geral"),
                        args.get("ssh_user", ""), args.get("ssh_password", ""),
                        int(args.get("ssh_port", 22)), args.get("wan_ip", ""),
                        wan_ip_2=args.get("wan_ip_2", ""),
                        wan_ip_3=args.get("wan_ip_3", ""),
                        platform=args.get("platform", ""),
                    )
                    host = Host(id=host_id, ip=ip,
                                label=args.get("label", ""),
                                group_name=args.get("group", "Geral"),
                                ssh_user=args.get("ssh_user", ""),
                                ssh_password=args.get("ssh_password", ""),
                                ssh_port=int(args.get("ssh_port", 22)),
                                wan_ip=args.get("wan_ip", ""),
                                wan_ip_2=args.get("wan_ip_2", ""),
                                wan_ip_3=args.get("wan_ip_3", ""),
                                platform=args.get("platform", ""))
                    self.hosts[host_id] = host
                    self.audio.generate_host_alerts(host.display_name)
                    logger.info(f"[CMD] Host adicionado pelo viewer: {ip}")

                elif cmd == "remove_host":
                    host_id = int(args.get("host_id", -1))
                    label   = args.get("label", "")
                    if host_id in self.hosts:
                        self.hosts.pop(host_id)
                    self.db.remove_host(host_id)
                    if label:
                        self.audio.delete_host_alerts(label)
                    logger.info(f"[CMD] Host removido pelo viewer: id={host_id}")

                elif cmd == "update_host":
                    host_id = int(args.pop("host_id", -1))
                    if host_id in self.hosts:
                        host = self.hosts[host_id]
                        for k, v in args.items():
                            if hasattr(host, k):
                                setattr(host, k, v)
                    self.db.update_host(host_id, **args)
                    logger.info(f"[CMD] Host atualizado pelo viewer: id={host_id}")

                elif cmd == "rename_group":
                    old = args.get("old_name", "")
                    new = args.get("new_name", "")
                    if old and new:
                        self.db.rename_group(old, new)
                        for h in self.hosts.values():
                            if h.group_name == old:
                                h.group_name = new
                        logger.info(f"[CMD] Grupo renomeado pelo viewer: {old} → {new}")

                elif cmd == "update_config":
                    # CORREÇÃO v2.9 — sincronização de configurações viewer → servidor:
                    #   Quando o viewer salva configurações, envia este comando
                    #   com o dict completo. O servidor persiste no config.json
                    #   e aplica em memória imediatamente.
                    cfg_data = args.get("config", {})
                    if cfg_data:
                        try:
                            save_user_config(cfg_data)
                            # Aplica parâmetros de monitoramento em memória
                            if cfg_data.get("ping_count"):
                                self.ping_count = int(cfg_data["ping_count"])
                            if cfg_data.get("ping_timeout"):
                                self.ping_timeout = int(cfg_data["ping_timeout"])
                            if cfg_data.get("cycle_delay"):
                                self.cycle_delay = int(cfg_data["cycle_delay"])
                            if cfg_data.get("between_hosts"):
                                self.between_hosts = float(cfg_data["between_hosts"])
                            # Aplica thresholds em memória
                            _threshold_map = {
                                "lat_warn":  ("latency_warning_ms",  int),
                                "lat_crit":  ("latency_critical_ms", int),
                                "jit_warn":  ("jitter_warning_ms",   int),
                                "jit_crit":  ("jitter_critical_ms",  int),
                                "loss_warn": ("loss_warning_pct",    float),
                                "loss_crit": ("loss_critical_pct",   float),
                            }
                            for cfg_key, (thresh_key, cast) in _threshold_map.items():
                                raw = cfg_data.get(cfg_key, "")
                                if raw:
                                    try:
                                        THRESHOLDS[thresh_key] = cast(raw)
                                    except (ValueError, TypeError):
                                        pass
                            # Aplica IP alvo Google
                            if cfg_data.get("google_target"):
                                from config import set_google_target
                                set_google_target(cfg_data["google_target"])
                            logger.info(
                                f"[CMD] Configurações atualizadas pelo viewer: "
                                f"ping_count={self.ping_count}, cycle_delay={self.cycle_delay}"
                            )
                        except Exception as e:
                            logger.error(f"[CMD] Erro ao aplicar configurações: {e}")

            except Exception as e:
                logger.error(f"[CMD] Erro ao aplicar comando '{cmd}': {e}")

    # ── Start / Stop ──────────────────────────────────────────────────

    def start(self) -> tuple[bool, str]:
        # CORREÇÃO v2.8 — detecção de thread morta:
        #   Se _running == True mas a thread morreu (exceção não tratada), o
        #   start() retornava "Já está rodando" e o monitoramento ficava
        #   permanentemente parado. Agora verifica se a thread está viva;
        #   se morreu, faz reset e reinicia normalmente.
        if self._running:
            if self._thread is not None and not self._thread.is_alive():
                logger.warning(
                    "Thread de monitoramento morreu — reiniciando automaticamente"
                )
                # Reset do estado sem liberar lock (pode ser o servidor legítimo)
                self._running = False
                if self._pool:
                    try:
                        self._pool.shutdown(wait=False, cancel_futures=True)
                    except Exception:
                        pass
                    self._pool = None
                self._viewer_mode = False
                # Continua abaixo para re-iniciar
            else:
                return True, "Já está rodando."

        can_start, msg = self.check_monitor_available()

        if not can_start:
            self._viewer_mode = True
            self._running = True
            # Garante que stop_flag está limpo ao entrar no modo viewer
            # (pode ter sido setado por stop_all em algum ponto anterior)
            self.audio.resume()
            self._thread = threading.Thread(target=self._viewer_loop, daemon=True,
                                             name="viewer-loop")
            self._thread.start()
            logger.info(f"Modo VISUALIZADOR — {msg}")
            return False, msg

        self._viewer_mode = False
        self._running = True
        self._paused = False
        # Garante que o stop_flag está limpo ao (re)iniciar como servidor.
        # stop() chama audio.stop_all() que seta o flag; sem este resume()
        # o áudio ficaria permanentemente silenciado após Parar → Iniciar.
        self.audio.resume()
        _write_lock(self._machine_id)
        self._pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="cycle")
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True,
                                         name="monitor-loop")
        self._thread.start()
        logger.info(f"Modo SERVIDOR — monitoramento iniciado ({self._machine_id})")
        return True, "Monitoramento iniciado."

    def stop(self, shutdown_audio=False):
        was_server = self._running and not self._viewer_mode
        self._running = False
        if shutdown_audio:
            self.audio.shutdown()
        else:
            self.audio.stop_all()
        if self._pool:
            try:
                self._pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            self._pool = None
        if was_server:
            _release_lock()
            logger.info("Monitoramento parado — lock liberado")
        else:
            logger.info("Visualização parada")
        self._viewer_mode = False

    def force_start_as_server(self) -> tuple[bool, str]:
        self.stop()
        _release_lock()
        return self.start()

    def pause(self):  self._paused = True;  logger.info("Pausado")
    def resume(self): self._paused = False; self.audio.resume(); logger.info("Retomado")

    @property
    def is_running(self): return self._running
    @property
    def is_paused(self):  return self._paused

    # ── Viewer loop — lê snapshot, ZERO SQLite ────────────────────────

    def _viewer_loop(self):
        """
        Loop do visualizador: lê viewer_snapshot.json a cada ciclo.
        Não faz nenhuma query ao SQLite — resolve o erro "file is not a database"
        que ocorre quando o SQLite é acessado via rede SMB.

        CORREÇÃO v2.8: envolvido em try/except externo para que exceções
        inesperadas não matem a thread silenciosamente.
        """
        cycle = 0
        _warned_stale = False
        _consecutive_errors = 0
        _stale_count = 0

        try:
            while self._running:
                cycle += 1
                try:
                    self._reload_from_snapshot()
                    _consecutive_errors = 0   # reset ao conseguir completar o ciclo

                    if self._on_host_updated:
                        for host in self.hosts.values():
                            try: self._on_host_updated(host)
                            except Exception: pass

                    if self._on_cycle_complete:
                        stats = {
                            "online":  sum(1 for h in self.hosts.values() if h.is_online),
                            "offline": sum(1 for h in self.hosts.values() if h.status == "offline"),
                            "ssh": 0, "local": 0,
                        }
                        try: self._on_cycle_complete(stats)
                        except Exception: pass

                    owner = self.get_monitor_owner()
                    if owner:
                        self._monitor_owner = owner
                        _warned_stale = False
                        _stale_count = 0
                    else:
                        _stale_count += 1
                        self._monitor_owner = "(nenhum — servidor parou)"
                        if not _warned_stale:
                            logger.warning("Servidor parou — snapshot pode estar desatualizado")
                            _warned_stale = True

                        # ── FAILOVER AUTOMÁTICO (1-N safe) ──────────────────
                        #
                        # Quando N viewers detectam servidor morto ao mesmo
                        # tempo, todos tentariam assumir simultaneamente.
                        #
                        # Solução em 3 fases:
                        #   1. Espera 3 ciclos (~30s) para confirmar que o
                        #      servidor realmente morreu (não é só um lag).
                        #   2. Espera um delay aleatório (0-20s) único por
                        #      máquina — desempata os N viewers.
                        #   3. Re-verifica o lock: se outro viewer já assumiu
                        #      durante o delay, desiste e continua como viewer.
                        #
                        # Apenas UMA máquina vence a corrida. As demais veem
                        # o lock fresco do vencedor e resetam o contador.

                        if _stale_count >= 3:
                            import random
                            # Jitter único por máquina (baseado no hash do hostname)
                            # para que a mesma máquina sempre tenha o mesmo "slot"
                            jitter = (hash(_machine_base_id()) % 200) / 10.0  # 0-20s
                            logger.info(
                                f"FAILOVER: servidor inativo por {_stale_count} ciclos — "
                                f"aguardando {jitter:.1f}s antes de tentar assumir"
                            )

                            # Espera com o jitter (checando _running a cada 100ms)
                            for _ in range(int(jitter * 10)):
                                if not self._running:
                                    break
                                time.sleep(0.1)

                            if not self._running:
                                break

                            # Re-verifica: outro viewer já assumiu?
                            recheck_owner = self.get_monitor_owner()
                            if recheck_owner:
                                logger.info(
                                    f"FAILOVER: cancelado — {recheck_owner} "
                                    "já assumiu como servidor"
                                )
                                self._monitor_owner = recheck_owner
                                _warned_stale = False
                                _stale_count = 0
                                continue  # volta ao loop normal como viewer

                            # Ninguém assumiu — esta instância assume
                            logger.info(
                                "FAILOVER: lock ainda stale após jitter — "
                                "promovendo esta instância a servidor"
                            )
                            self._running = False
                            self._viewer_mode = False
                            _release_lock()
                            if self._on_failover:
                                try:
                                    self._on_failover()
                                except Exception as e:
                                    logger.error(f"Erro no callback de failover: {e}")
                            break

                except Exception as e:
                    _consecutive_errors += 1
                    logger.error(f"Viewer loop erro (ciclo #{cycle}, consecutivo #{_consecutive_errors}): {e}")
                    if _consecutive_errors >= 50:
                        logger.critical(
                            f"Viewer loop: {_consecutive_errors} erros consecutivos — "
                            "aguardando 30s antes de continuar"
                        )
                        for _ in range(300):
                            if not self._running: break
                            time.sleep(0.1)
                        _consecutive_errors = 0

                for _ in range(int(self.cycle_delay * 10)):
                    if not self._running: break
                    time.sleep(0.1)

        except Exception as e:
            logger.critical(f"Viewer loop CRASH FATAL: {e}", exc_info=True)
            self._running = False

    # ── Credenciais SSH ───────────────────────────────────────────────

    def _get_effective_ssh_creds(self, host):
        user = (host.ssh_user or "").strip()
        pwd  = (host.ssh_password or "").strip()
        port = host.ssh_port or 22
        if not user:
            user, pwd = get_ssh_credentials()
        return user, pwd, port

    # ── Ping ──────────────────────────────────────────────────────────

    def _ping_host_all(self, host):
        ssh_user, ssh_pwd, ssh_port = self._get_effective_ssh_creds(host)
        wan_target  = (host.wan_ip   or "").strip() or None
        wan_target2 = (host.wan_ip_2 or "").strip() or None
        # wan_ip_3 é o alvo do ping "Google" para este host específico.
        # Se não configurado, usa o IP global definido em Configurações (padrão 8.8.8.8).
        wan_target3 = (host.wan_ip_3 or "").strip() or None
        google_target = wan_target3 or get_google_target()

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix=f"p-{host.ip}") as mini:
            f_local = mini.submit(ping_host, host.ip, self.ping_count, self.ping_timeout)
            f_ssh = None
            if ssh_user:
                f_ssh = mini.submit(ping_triple_via_ssh,
                    host.ip, ssh_user, ssh_pwd, ssh_port,
                    self.ping_count, self.ping_timeout, wan_target,
                    google_target, host.platform)
            try:
                local_r = f_local.result(timeout=45)
            except Exception:
                local_r = PingResult(timestamp=datetime.now(), status="offline",
                                     loss_pct=100.0, ping_mode="PING")
            host_ssh = wan_r = google_r = None
            if f_ssh:
                try:
                    host_ssh, wan_r, google_r = f_ssh.result(timeout=60)
                except Exception as e:
                    logger.debug(f"SSH triple {host.ip} timeout/erro: {e}")

        # WAN principal falhou mas WAN2 está configurada → tenta WAN2 como fallback
        if host_ssh and wan_target and not wan_r and wan_target2:
            logger.info(
                f"{host.display_name} ({host.ip}): WAN1 ({wan_target}) sem resposta — "
                f"tentando WAN2 ({wan_target2})"
            )
            try:
                _, wan_r2, _ = ping_triple_via_ssh(
                    host.ip, ssh_user, ssh_pwd, ssh_port,
                    count=2, timeout_ms=self.ping_timeout,
                    wan_target=wan_target2,
                    platform=host.platform,
                )
                if wan_r2:
                    wan_r = wan_r2
                    logger.info(
                        f"{host.display_name} ({host.ip}): WAN2 ({wan_target2}) respondeu "
                        f"({wan_r.latency_ms:.1f}ms)"
                    )
            except Exception as e:
                logger.debug(f"WAN2 fallback {host.ip} → {wan_target2}: {e}")

        # Log informativo quando SSH OK mas WAN1 e WAN2 ambas falharam
        if host_ssh and google_r and wan_target and not wan_r:
            wan_info = f"WAN1={wan_target}" + (f" WAN2={wan_target2}" if wan_target2 else "")
            logger.info(
                f"{host.display_name} ({host.ip}): SSH OK (HOST+Google), "
                f"mas {wan_info} sem resposta neste ciclo"
            )

        return local_r, host_ssh, wan_r, google_r

    # ── Monitor loop (servidor) ───────────────────────────────────────

    def _monitor_loop(self):
        """
        Loop principal de monitoramento do servidor.

        CORREÇÃO v2.8 — thread morria silenciosamente:
          O loop anterior não tinha try/except externo. Qualquer exceção não
          tratada (database locked, I/O error no snapshot, RuntimeError do
          ThreadPoolExecutor) matava a thread permanentemente, mas _running
          continuava True. O resultado: monitoramento parava sem aviso, e
          start() retornava "Já está rodando" sem verificar a thread.

          Correções aplicadas:
            1. try/except externo — thread nunca morre sem logar o motivo
            2. try/except individual para cada operação de DB e I/O
            3. Refresh do ThreadPoolExecutor a cada 100 ciclos para evitar
               acúmulo de threads presas em conexões SSH travadas
            4. Contador de erros consecutivos com backoff progressivo
        """
        cycle_count = 0
        _consecutive_errors = 0

        try:
            while self._running:
                if self._paused:
                    time.sleep(1)
                    continue

                cycle_count += 1
                cycle_start = time.time()
                stats = {"online": 0, "offline": 0, "ssh": 0, "local": 0}

                try:
                    active = [h for h in self.hosts.values() if h.enabled]

                    if cycle_count % 10 == 1 or cycle_count == 1:
                        logger.info(f"Ciclo #{cycle_count} — {len(active)} hosts")

                    try:
                        lock_ok = _update_lock_heartbeat()
                    except Exception:
                        lock_ok = True

                    # CORREÇÃO v2.10 — detecção de takeover:
                    #   Se outra máquina clicou "Assumir Servidor" e sobrescreveu
                    #   o lock file, _update_lock_heartbeat retorna False.
                    #   Esta instância precisa parar o monitoramento e virar viewer.
                    if not lock_ok:
                        lock = _read_lock()
                        new_owner = lock.get("machine", "?") if lock else "?"
                        logger.warning(
                            f"DEMOÇÃO: lock assumido por {new_owner} — "
                            "parando monitoramento e virando viewer"
                        )
                        self._running = False
                        self._viewer_mode = False
                        if self._pool:
                            try:
                                self._pool.shutdown(wait=False, cancel_futures=True)
                            except Exception:
                                pass
                            self._pool = None
                        if self._on_demoted:
                            try:
                                self._on_demoted()
                            except Exception as e:
                                logger.error(f"Erro no callback de demoção: {e}")
                        break  # sai do while

                    # Aplica comandos enviados pelos visualizadores (add/edit/remove host)
                    try:
                        self._apply_viewer_commands()
                    except Exception as e:
                        logger.error(f"Erro ao aplicar comandos do viewer: {e}")

                    # CORREÇÃO v2.8 — refresh periódico do ThreadPoolExecutor:
                    #   Após muitas horas, threads do pool podem ficar presas em
                    #   conexões SSH que nunca retornam (timeout do SO, não do Python).
                    #   Recriar o pool a cada 100 ciclos libera essas threads órfãs.
                    if cycle_count % 100 == 0 and self._pool:
                        try:
                            old_pool = self._pool
                            self._pool = ThreadPoolExecutor(max_workers=10,
                                                            thread_name_prefix="cycle")
                            old_pool.shutdown(wait=False, cancel_futures=True)
                            logger.debug(f"ThreadPoolExecutor recriado no ciclo #{cycle_count}")
                        except Exception as e:
                            logger.warning(f"Erro ao recriar pool: {e}")

                    results = {}
                    if self._pool:
                        try:
                            futures = {self._pool.submit(self._ping_host_all, h): h.id
                                       for h in active if self._running}
                            for f in as_completed(futures, timeout=120):
                                hid = futures[f]
                                try:
                                    results[hid] = f.result(timeout=60)
                                except Exception:
                                    results[hid] = (
                                        PingResult(timestamp=datetime.now(), status="offline",
                                                   loss_pct=100.0, ping_mode="PING"),
                                        None, None, None,
                                    )
                        except TimeoutError:
                            logger.warning(
                                f"Ciclo #{cycle_count}: timeout global de 120s atingido — "
                                f"apenas {len(results)}/{len(active)} hosts responderam"
                            )
                        except Exception as e:
                            logger.error(f"Erro no pool de pings: {e}")

                    ping_batch = []
                    status_changes = []

                    for host in active:
                        if not self._running or host.id not in results:
                            continue
                        local_r, host_ssh, wan_r, google_r = results[host.id]

                        was_off = host.status == "offline"
                        was_on  = host.status == "online"
                        prev_status = host.status
                        host.add_ping(local_r)
                        host.add_ssh_results(host_ssh, wan_r, google_r)

                        if any(r and r.ping_mode == "SSH" for r in [host_ssh, wan_r, google_r]):
                            host.last_ping_mode = "SSH"; stats["ssh"] += 1
                        else:
                            host.last_ping_mode = "PING"; stats["local"] += 1

                        ping_batch.append((
                            host.id, local_r.latency_ms, local_r.jitter_ms,
                            local_r.loss_pct, local_r.rtt_min, local_r.rtt_max,
                            local_r.rtt_avg, local_r.ttl, local_r.status,
                        ))

                        try:
                            self._process_alerts(host, local_r, was_off, was_on)
                        except Exception as e:
                            logger.error(f"Erro em _process_alerts para {host.display_name}: {e}")

                        stats["online" if local_r.is_online else "offline"] += 1

                        if host.status != prev_status:
                            status_changes.append(f"{host.display_name}: {prev_status}->{host.status}")

                        if self._on_host_updated:
                            try: self._on_host_updated(host)
                            except Exception: pass

                    # CORREÇÃO v2.8 — DB insert protegido contra database locked:
                    if ping_batch:
                        try:
                            self.db.insert_pings_batch(ping_batch)
                        except Exception as e:
                            logger.warning(f"Erro ao gravar pings no DB: {e}")

                    # Atualiza cache de estatísticas 24h a cada 5 ciclos
                    # (evita query por host a cada ciclo — amortizado no tempo)
                    if cycle_count % 5 == 0 or cycle_count == 1:
                        for h in active:
                            try:
                                self._stats_cache[h.id] = self.db.get_host_stats_24h(h.id)
                            except Exception:
                                pass

                    # Grava snapshot para os viewers após cada ciclo
                    try:
                        alerts = self.db.get_active_alerts()
                    except Exception:
                        alerts = []
                    try:
                        alerts_history = self.db.get_alerts_history(200)
                    except Exception:
                        alerts_history = []
                    # v2.7: inclui as últimas 200 linhas de log do servidor no snapshot
                    # (atualizado a cada 5 ciclos para não ler disco toda vez)
                    if cycle_count % 5 == 0 or cycle_count == 1:
                        try:
                            self._server_log_lines = read_recent_logs(max_days=2, max_lines=200)
                        except Exception:
                            self._server_log_lines = []
                    try:
                        _write_snapshot(self.hosts, alerts, self._stats_cache,
                                        alerts_history, self._server_log_lines)
                    except Exception as e:
                        logger.warning(f"Erro ao gravar snapshot: {e}")

                    if cycle_count % 5 == 0:
                        try:
                            self._run_traceroutes()
                        except Exception:
                            pass
                    if cycle_count % 100 == 0:
                        try:
                            self.db.cleanup_old_data(days=30)
                        except Exception:
                            pass
                    if self._on_cycle_complete:
                        try: self._on_cycle_complete(stats)
                        except Exception: pass

                    elapsed = time.time() - cycle_start
                    if status_changes:
                        logger.info(f"Ciclo #{cycle_count} em {elapsed:.1f}s — Mudanças: {', '.join(status_changes)}")
                    elif cycle_count % 10 == 0:
                        logger.info(
                            f"Ciclo #{cycle_count} em {elapsed:.1f}s — "
                            f"On:{stats['online']} Off:{stats['offline']} "
                            f"SSH:{stats['ssh']} Local:{stats['local']}"
                        )

                    # Reset do contador de erros após ciclo bem sucedido
                    _consecutive_errors = 0

                except Exception as e:
                    _consecutive_errors += 1
                    logger.error(
                        f"Erro no ciclo #{cycle_count} "
                        f"(consecutivo #{_consecutive_errors}): {e}",
                        exc_info=True
                    )
                    # Backoff progressivo: se muitos erros seguidos,
                    # espera mais tempo para não sobrecarregar logs
                    if _consecutive_errors >= 10:
                        backoff = min(60, _consecutive_errors * 2)
                        logger.warning(
                            f"Monitor loop: {_consecutive_errors} erros consecutivos — "
                            f"aguardando {backoff}s antes de continuar"
                        )
                        for _ in range(backoff * 10):
                            if not self._running: break
                            time.sleep(0.1)

                wait = max(0, self.cycle_delay - (time.time() - cycle_start))
                for _ in range(int(wait * 10)):
                    if not self._running: break
                    time.sleep(0.1)

        except Exception as e:
            logger.critical(f"Monitor loop CRASH FATAL: {e}", exc_info=True)
            # Sinaliza que o loop morreu para que start() possa detectar
            # via is_alive() e reiniciar
            self._running = False

    # ── Alertas ───────────────────────────────────────────────────────

    def _process_alerts(self, host, result, was_offline, was_online):
        label = host.display_name

        if result.is_online and was_offline:
            # CORREÇÃO v2.8 — suprime áudio "voltou ONLINE" para hosts recém-adicionados:
            #   Quando um host é adicionado, o primeiro ping pode falhar (timeout
            #   de rede, host ainda iniciando, etc.), colocando-o em "offline".
            #   No segundo ping, se o host responde, a transição offline→online
            #   dispara o alerta sonoro "voltou ONLINE" — indesejado, pois o host
            #   nunca esteve realmente offline (era apenas o estado inicial).
            #
            #   Hosts com <= 3 pings totais são considerados "em fase de detecção
            #   inicial" e não disparam áudio de online. O alerta textual (log +
            #   resolução de alertas no DB) é mantido normalmente.
            is_new_host = host.total_pings_all_time <= 3

            msg = f"{label} ({host.ip}) voltou ONLINE"
            logger.info(msg)
            for a in self.db.get_active_alerts(host.id):
                self.db.resolve_alert(a["id"])

            if is_new_host:
                logger.debug(
                    f"Suprimindo áudio ONLINE para {label} — host recém-adicionado "
                    f"(total_pings={host.total_pings_all_time})"
                )
            elif not self.audio.muted:
                threading.Thread(
                    target=lambda l=label: self.audio.play_alert(l, "online"),
                    daemon=True).start()

            if self._on_alert:
                try: self._on_alert(host, "online", msg)
                except Exception: pass
            return

        if result.status == "offline" and host.offline_since is not None:
            threshold = host.next_alert_at_seconds()
            elapsed = (datetime.now() - host.offline_since).total_seconds()
            if elapsed >= threshold:
                host.alerts_fired += 1
                host.last_alert_time = datetime.now()
                if threshold == 0:
                    msg = f"{label} ({host.ip}) ficou OFFLINE"
                else:
                    m = int(threshold // 60); h = m // 60
                    tempo = (f"há {h}h{f' {m%60}min' if m%60 else ''}"
                             if h >= 1 else f"há {m} min")
                    msg = f"{label} ({host.ip}) continua OFFLINE {tempo}"
                logger.warning(msg)
                self.db.insert_alert(host.id, "offline", msg)
                if not self.audio.muted:
                    logger.debug(
                        f"Disparando áudio OFFLINE para {label} "
                        f"(alerts_fired={host.alerts_fired}, threshold={threshold}s, elapsed={elapsed:.0f}s)"
                    )
                    def _play(lbl=label):
                        try:
                            self.audio.play_generic_alert()
                            time.sleep(ALERTA_DURATION_S + 0.5)
                            self.audio.play_alert(lbl, "offline")
                        except Exception as e:
                            logger.error(f"Erro ao tocar áudio offline para {lbl}: {e}")
                    threading.Thread(target=_play, daemon=True).start()
                if self._on_alert:
                    try: self._on_alert(host, "offline", msg)
                    except Exception: pass
            return

        if result.is_online:
            # CORREÇÃO: alertas de latência e perda agora:
            #   1. Só criam novo alerta se não há um ativo do mesmo tipo
            #      (evita spam de alertas a cada ciclo enquanto o problema persiste)
            #   2. Resolvem automaticamente quando o valor melhora
            #      (antes ficavam ativos para sempre mesmo após melhora)
            active = self.db.get_active_alerts(host.id)
            active_types = {a["alert_type"]: a["id"] for a in active}

            if result.latency_ms > THRESHOLDS["latency_critical_ms"]:
                if "high_latency" not in active_types:
                    msg = f"Latência CRÍTICA em {label}: {result.latency_ms:.0f}ms"
                    logger.warning(msg)
                    self.db.insert_alert(host.id, "high_latency", msg)
            else:
                if "high_latency" in active_types:
                    self.db.resolve_alert(active_types["high_latency"])
                    logger.info(f"Latência normalizada em {label}: {result.latency_ms:.0f}ms")

            if result.loss_pct > THRESHOLDS["loss_warning_pct"]:
                if "packet_loss" not in active_types:
                    msg = f"Perda em {label}: {result.loss_pct:.0f}%"
                    logger.warning(msg)
                    self.db.insert_alert(host.id, "packet_loss", msg)
            else:
                if "packet_loss" in active_types:
                    self.db.resolve_alert(active_types["packet_loss"])
                    logger.info(f"Perda normalizada em {label}: {result.loss_pct:.0f}%")

    # ── Traceroutes ───────────────────────────────────────────────────

    def _run_traceroutes(self):
        max_tr = MONITOR_DEFAULTS.get("max_concurrent_traceroutes", 5)
        def _t():
            count = 0
            for h in list(self.hosts.values()):
                if not self._running or not h.enabled: continue
                if count >= max_tr: break
                if h.status == "offline" or h.consecutive_failures > 0:
                    try:
                        r = traceroute(h.ip); h.latest_traceroute = r
                        hops = [{"hop": hp.hop_number, "ip": hp.ip,
                                 "rtt1": hp.rtt1, "rtt2": hp.rtt2, "rtt3": hp.rtt3}
                                for hp in r.hops]
                        self.db.insert_traceroute(h.id, r.hop_count, json.dumps(hops))
                        count += 1
                    except Exception: pass
        threading.Thread(target=_t, daemon=True).start()

    def run_traceroute(self, host_id):
        host = self.hosts.get(host_id)
        if not host: return None
        target = (host.wan_ip or "").strip() or "8.8.8.8"
        u, p, pt = self._get_effective_ssh_creds(host)
        if u:
            try:
                r = traceroute_via_ssh(ssh_host=host.ip, ssh_user=u, ssh_password=p,
                                       target_ip=target, ssh_port=pt)
                r["source"] = "SSH"
                self.db.insert_traceroute(host.id, len(r.get("hops", [])),
                                          json.dumps(r.get("hops", [])))
                return r
            except Exception: pass
        rl = traceroute(host.ip); host.latest_traceroute = rl
        hops = [{"hop": h.hop_number, "ip": h.ip,
                 "rtt1": h.rtt1, "rtt2": h.rtt2, "rtt3": h.rtt3} for h in rl.hops]
        self.db.insert_traceroute(host.id, rl.hop_count, json.dumps(hops))
        return {"hop_count": rl.hop_count, "hops": hops,
                "target_reached": rl.target_reached, "source": "LOCAL"}

    def run_wan_traceroute(self, host_id):
        host = self.hosts.get(host_id)
        if not host: return None
        target = (host.wan_ip or "").strip()
        if not target: raise ValueError("IP WAN não configurado.")
        u, p, pt = self._get_effective_ssh_creds(host)
        if u:
            try:
                r = traceroute_via_ssh(ssh_host=host.ip, ssh_user=u, ssh_password=p,
                                       target_ip=target, ssh_port=pt)
                r["source"] = "SSH"; return r
            except Exception as e:
                logger.warning(f"Traceroute WAN SSH {host.ip}: {e}")
        rl = traceroute(target)
        hops = [{"hop": h.hop_number, "ip": h.ip,
                 "rtt1": h.rtt1, "rtt2": h.rtt2, "rtt3": h.rtt3} for h in rl.hops]
        return {"wan_ip": target, "hop_count": rl.hop_count, "hops": hops,
                "target_reached": rl.target_reached, "source": "LOCAL"}

    def run_mtr_wan(self, host_id, stop_event, on_round=None):
        host = self.get_host(host_id)
        if not host: raise ValueError("Host não encontrado.")
        target = (host.wan_ip or "").strip()
        if not target: raise ValueError("IP WAN principal não configurado.")
        u, p, pt = self._get_effective_ssh_creds(host)
        if u:
            try:
                return run_mtr_via_ssh(ssh_host=host.ip, ssh_user=u, ssh_password=p,
                    target_ip=target, ssh_port=pt,
                    stop_event=stop_event, on_round=on_round)
            except Exception as e:
                if on_round: on_round(0, {}, f"SSH falhou ({e})")
        return run_mtr_local(target_ip=target, stop_event=stop_event, on_round=on_round)

    def run_mtr_wan_2(self, host_id, stop_event, on_round=None):
        """MTR para a WAN secundária — wan_ip_2 (IP público da loja)."""
        host = self.get_host(host_id)
        if not host: raise ValueError("Host não encontrado.")
        target = (host.wan_ip_2 or "").strip()
        if not target: raise ValueError("IP WAN2 não configurado.")
        u, p, pt = self._get_effective_ssh_creds(host)
        if u:
            try:
                return run_mtr_via_ssh(ssh_host=host.ip, ssh_user=u, ssh_password=p,
                    target_ip=target, ssh_port=pt,
                    stop_event=stop_event, on_round=on_round)
            except Exception as e:
                if on_round: on_round(0, {}, f"SSH falhou ({e})")
        return run_mtr_local(target_ip=target, stop_event=stop_event, on_round=on_round)

    def run_mtr_wan_3(self, host_id, stop_event, on_round=None):
        """MTR para a WAN terciária — wan_ip_3 (2º hop da operadora)."""
        host = self.get_host(host_id)
        if not host: raise ValueError("Host não encontrado.")
        target = (host.wan_ip_3 or "").strip()
        if not target: raise ValueError("IP WAN3 não configurado.")
        u, p, pt = self._get_effective_ssh_creds(host)
        if u:
            try:
                return run_mtr_via_ssh(ssh_host=host.ip, ssh_user=u, ssh_password=p,
                    target_ip=target, ssh_port=pt,
                    stop_event=stop_event, on_round=on_round)
            except Exception as e:
                if on_round: on_round(0, {}, f"SSH falhou ({e})")
        return run_mtr_local(target_ip=target, stop_event=stop_event, on_round=on_round)

    def run_mtr_google(self, host_id, stop_event, on_round=None):
        """
        MTR para o alvo Google/internet.
        Usa o IP global configurado em Configurações (padrão 8.8.8.8).
        """
        host = self.get_host(host_id)
        if not host: raise ValueError("Host não encontrado.")
        target = get_google_target()
        u, p, pt = self._get_effective_ssh_creds(host)
        if u:
            try:
                return run_mtr_via_ssh(ssh_host=host.ip, ssh_user=u, ssh_password=p,
                    target_ip=target, ssh_port=pt,
                    stop_event=stop_event, on_round=on_round)
            except Exception as e:
                if on_round: on_round(0, {}, f"SSH falhou ({e})")
        return run_mtr_local(target_ip=target, stop_event=stop_event, on_round=on_round)

    def run_ssh_ping(self, host_id, target_ip):
        host = self.hosts.get(host_id)
        if not host: return {"error": "Host não encontrado", "source": "ERROR"}
        target_ip = (target_ip or "").strip()
        if not target_ip: return {"error": "IP inválido", "source": "ERROR"}
        u, p, pt = self._get_effective_ssh_creds(host)
        if u:
            try:
                r = ping_host_via_ssh(host.ip, u, p, pt,
                                      count=4, timeout_ms=1000, target_ip=target_ip)
                if r.ping_mode == "SSH":
                    return {"source": "SSH", "latency_ms": r.latency_ms,
                            "loss_pct": r.loss_pct, "jitter_ms": r.jitter_ms,
                            "status": r.status, "host": host.ip, "target": target_ip}
            except Exception: pass
        r = ping_host(target_ip, count=4, timeout_ms=1000)
        return {"source": "LOCAL", "latency_ms": r.latency_ms,
                "loss_pct": r.loss_pct, "jitter_ms": r.jitter_ms,
                "status": r.status, "host": "local", "target": target_ip}

    def run_mtu_discovery(self, host_id):
        host = self.hosts.get(host_id)
        return discover_mtu(host.ip) if host else 0

    def run_diagnostics(self, host_id: int) -> dict:
        host = self.hosts.get(host_id)
        if not host: return {"error": "Host não encontrado"}
        u, p, pt = self._get_effective_ssh_creds(host)
        if not u: return {"error": "Sem credenciais SSH"}
        try:
            import paramiko
        except ImportError:
            return {"error": "paramiko não instalado"}
        client = None
        results = {}
        try:
            client = paramiko.SSHClient()
            from utils.security import TrustOnFirstUsePolicy; client.set_missing_host_key_policy(TrustOnFirstUsePolicy())
            client.connect(hostname=host.ip, port=pt, username=u, password=p,
                           timeout=10, allow_agent=False, look_for_keys=False)
            def _cmd(c, t=15):
                try:
                    _, so, se = client.exec_command(c, timeout=t)
                    return (so.read().decode("utf-8", errors="replace").strip()
                            or se.read().decode("utf-8", errors="replace").strip())
                except Exception: return ""
            results["uptime"]          = _cmd("uptime") or _cmd("cat /proc/uptime")
            results["wan_ip"]          = _cmd("curl -s --max-time 5 https://api.ipify.org") or "—"
            results["gateway"]         = (_cmd("route -n 2>/dev/null | grep '^0.0.0.0' | awk '{print $2}'")
                                          or _cmd("netstat -rn 2>/dev/null | grep default | awk '{print $2}'")
                                          or "—")
            results["dns"]             = _cmd("cat /etc/resolv.conf 2>/dev/null | grep nameserver | head -3") or "—"
            results["interface_speed"] = _cmd("cat /sys/class/net/eth0/speed 2>/dev/null") or "—"
            results["duplex"]          = _cmd("cat /sys/class/net/eth0/duplex 2>/dev/null") or "—"
            results["hostname"]        = _cmd("hostname") or "—"
            results["disk"]            = _cmd("df -h / 2>/dev/null | tail -1") or "—"
            results["memory"]          = (_cmd("free -m 2>/dev/null | grep Mem | awk '{printf \"%s/%sMB (%.0f%%)\", $3, $2, $3/$2*100}'")
                                          or _cmd("top -l1 2>/dev/null | head -5 | grep PhysMem")
                                          or "—")
            results["source"] = "SSH"
        except Exception as exc:
            results["error"] = str(exc); results["source"] = "ERROR"
        finally:
            if client:
                try: client.close()
                except Exception: pass
        return results

    def run_dns_check(self, hostname, host_id=None):
        ms, ip = resolve_dns(hostname)
        if host_id: self.db.insert_dns(host_id, ms, ip)
        return ms, ip

    def get_server_log_lines(self, max_lines: int = 200) -> list[str]:
        """
        Retorna as últimas linhas de log do servidor.
        No modo servidor: lê diretamente dos arquivos de log locais.
        No modo viewer: lê as linhas incluídas no snapshot pelo servidor.
        Isso permite que o viewer exiba logs reais do servidor sem acessar
        o sistema de arquivos da máquina servidora via SMB.
        """
        if self._viewer_mode:
            snap = _read_snapshot() or {}
            lines = snap.get("server_log_lines", [])
            return lines[-max_lines:] if lines else []
        # Servidor lê do arquivo local
        try:
            return read_recent_logs(max_days=2, max_lines=max_lines)
        except Exception:
            return []

    def get_active_alerts(self, host_id=None):
        """
        Retorna alertas ativos — do snapshot (viewer) ou do DB (servidor).
        Use este método em vez de db.get_active_alerts() para compatibilidade
        com o modo visualizador, que não pode acessar o SQLite via rede SMB.
        """
        if self._viewer_mode:
            snap = _read_snapshot() or {}
            alerts = snap.get("alerts", [])
            if host_id:
                alerts = [a for a in alerts if a.get("host_id") == host_id]
            return alerts
        return self.db.get_active_alerts(host_id)

    def get_alerts_history(self, limit: int = 100):
        """
        Retorna histórico de alertas — do DB (servidor) ou do snapshot (viewer).

        CORREÇÃO v2.6: o viewer agora lê 'alerts_history' do snapshot, que
        contém o histórico completo (resolvidos + ativos). Antes lia apenas
        'alerts' (somente ativos), fazendo a aba Histórico ficar vazia ou
        incompleta no visualizador.
        """
        if self._viewer_mode:
            snap = _read_snapshot() or {}
            # Tenta campo novo (v2.6+), fallback para campo antigo
            history = snap.get("alerts_history", [])
            if not history:
                history = snap.get("alerts", [])
            return history[:limit]
        return self.db.get_alerts_history(limit)

    def get_host_stats(self, host_id):
        # Em modo viewer, o DB está na rede SMB e não pode ser consultado.
        # Usa o cache de estatísticas 24h populado a partir do snapshot.
        if self._viewer_mode:
            s = dict(self._stats_cache.get(host_id, {
                "total_pings": 0, "avg_latency": 0, "max_latency": 0,
                "min_latency": 0, "avg_jitter": 0, "avg_loss": 0,
                "online_count": 0, "offline_count": 0, "availability_pct": 0,
            }))
        else:
            s = self.db.get_host_stats_24h(host_id)
        h = self.hosts.get(host_id)
        if h:
            s["cycle_number"]         = h.cycle_number
            s["ping_in_cycle"]        = h.ping_in_cycle
            s["total_pings_all_time"] = h.total_pings_all_time
            s["cycle_size"]           = MONITOR_DEFAULTS.get("cycle_size", 100)
            s["last_ping_mode"]       = h.last_ping_mode
            s["consecutive_failures"] = h.consecutive_failures
            s["stddev_latency"]       = h.stddev_latency_recent
            s["success_rate"]         = h.success_rate_recent
            s["last_collection_ts"]   = h.last_collection_ts.strftime("%H:%M:%S") if h.last_collection_ts else "—"
            s["rtt_current"]          = h.current_rtt if h.ping_history else "—"
            s["host_ssh_latency"]     = h.host_ssh_latency
            s["host_ssh_loss"]        = h.host_ssh_loss
            s["host_ssh_jitter"]      = h.host_ssh_jitter
            s["host_ssh_rtt"]         = h.host_ssh_rtt
            s["host_ssh_avail"]       = h.host_ssh_avail
            s["host_ssh_source"]      = h.host_ssh_source
            s["wan_latency"]          = h.wan_latency
            s["wan_loss"]             = h.wan_loss
            s["wan_jitter"]           = h.wan_jitter
            s["wan_rtt"]              = h.wan_rtt
            s["wan_avail"]            = h.wan_avail
            s["wan_has_data"]         = h.wan_has_data
            s["google_latency"]       = h.google_latency
            s["google_loss"]          = h.google_loss
            s["google_jitter"]        = h.google_jitter
            s["google_rtt"]           = h.google_rtt
            s["google_avail"]         = h.google_avail
            s["google_has_data"]      = h.google_has_data
            s["delta_wan"]            = h.delta_wan
            s["delta_google"]         = h.delta_google
        return s

    def get_dashboard_summary(self):
        active = [h for h in self.hosts.values() if h.enabled]
        on = [h for h in active if h.is_online]
        def _avg(vals): return sum(vals) / len(vals) if vals else 0.0

        return {
            "total":           len(active),
            "online":          len(on),
            "offline":         sum(1 for h in active if h.status == "offline"),
            "unknown":         sum(1 for h in active if h.status == "unknown"),
            "host_avg_lat":    _avg([h.host_ssh_latency for h in on if h.host_ssh_has_data]),
            "host_avg_loss":   _avg([h.host_ssh_loss    for h in on if h.host_ssh_has_data]),
            "wan_avg_lat":     _avg([h.wan_latency      for h in on if h.wan_has_data]),
            "wan_avg_loss":    _avg([h.wan_loss         for h in on if h.wan_has_data]),
            "google_avg_lat":  _avg([h.google_latency   for h in on if h.google_has_data]),
            "google_avg_loss": _avg([h.google_loss      for h in on if h.google_has_data]),
            "active_alerts":   len(self.get_active_alerts()),
            "ssh_count":       sum(1 for h in active if h.last_ping_mode == "SSH"),
            "local_count":     sum(1 for h in active if h.last_ping_mode != "SSH"),
        }
