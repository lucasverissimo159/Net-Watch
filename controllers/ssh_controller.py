"""
Controlador SSH — conexão remota a dispositivos monitorados.
"""
import threading
import time
from typing import Callable, Optional

from config import SSH_DEFAULTS
from utils.logger import setup_logger

logger = setup_logger("ssh")


class SSHSession:
    """Representa uma sessão SSH ativa."""

    def __init__(self, host: str, username: str, password: str = "",
                 port: int = 22, key_file: str = ""):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.key_file = key_file
        self.client = None
        self.channel = None
        self.connected = False
        self._output_callback: Optional[Callable] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._running = False

    def set_output_callback(self, callback: Callable):
        """Define callback para saída do terminal (output_text: str)."""
        self._output_callback = callback

    def connect(self) -> tuple[bool, str]:
        """Estabelece conexão SSH."""
        try:
            import paramiko
        except ImportError:
            return False, (
                "Módulo 'paramiko' não instalado.\n"
                "Execute: pip install paramiko"
            )

        try:
            self.client = paramiko.SSHClient()
            from utils.security import TrustOnFirstUsePolicy
            self.client.set_missing_host_key_policy(TrustOnFirstUsePolicy())

            connect_kwargs = {
                "hostname": self.host,
                "port": self.port,
                "username": self.username,
                "timeout": SSH_DEFAULTS["timeout"],
            }

            if self.key_file:
                connect_kwargs["key_filename"] = self.key_file
            elif self.password:
                connect_kwargs["password"] = self.password

            self.client.connect(**connect_kwargs)

            # Abre shell interativo
            self.channel = self.client.invoke_shell(
                term="xterm-256color", width=120, height=40
            )
            self.channel.settimeout(0.1)

            # Configura keepalive
            transport = self.client.get_transport()
            if transport:
                transport.set_keepalive(SSH_DEFAULTS["keepalive"])

            self.connected = True
            self._running = True

            # Inicia thread de leitura
            self._reader_thread = threading.Thread(
                target=self._read_output, daemon=True
            )
            self._reader_thread.start()

            logger.info(f"Conectado via SSH a {self.host}:{self.port}")
            return True, f"Conectado a {self.host}"

        except Exception as e:
            logger.error(f"Falha SSH {self.host}: {e}")
            return False, f"Erro de conexão: {str(e)}"

    def send_command(self, command: str):
        """Envia comando pelo canal SSH."""
        if self.channel and self.connected:
            try:
                self.channel.send(command + "\n")
            except Exception as e:
                logger.error(f"Erro ao enviar comando: {e}")
                self._emit_output(f"\r\n[ERRO] {e}\r\n")

    def _read_output(self):
        """Thread que lê output do canal SSH continuamente."""
        while self._running and self.connected:
            try:
                if self.channel and self.channel.recv_ready():
                    data = self.channel.recv(4096)
                    if data:
                        text = data.decode("utf-8", errors="replace")
                        self._emit_output(text)
                    else:
                        break
                else:
                    time.sleep(0.05)
            except Exception:
                time.sleep(0.1)

    def _emit_output(self, text: str):
        """Emite saída para o callback."""
        if self._output_callback:
            try:
                self._output_callback(text)
            except Exception:
                pass

    def resize(self, width: int, height: int):
        """Redimensiona o terminal virtual."""
        if self.channel:
            try:
                self.channel.resize_pty(width=width, height=height)
            except Exception:
                pass

    def disconnect(self):
        """Fecha a sessão SSH."""
        self._running = False
        self.connected = False
        try:
            if self.channel:
                self.channel.close()
            if self.client:
                self.client.close()
        except Exception:
            pass
        logger.info(f"Desconectado de {self.host}")

    def exec_single(self, command: str, timeout: int = 30) -> tuple[str, str]:
        """Executa um comando e retorna (stdout, stderr). Não interativo."""
        if not self.client or not self.connected:
            return "", "Não conectado"
        try:
            stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            return out, err
        except Exception as e:
            return "", str(e)


class SSHController:
    """Gerencia múltiplas sessões SSH."""

    def __init__(self):
        self.sessions: dict[str, SSHSession] = {}  # ip -> session
        self._lock = threading.Lock()

    def create_session(self, host: str, username: str, password: str = "",
                       port: int = 22, key_file: str = "") -> SSHSession:
        """Cria uma nova sessão SSH."""
        # Fecha sessão anterior para o mesmo host
        if host in self.sessions:
            self.sessions[host].disconnect()

        session = SSHSession(host, username, password, port, key_file)
        self.sessions[host] = session
        return session

    def get_session(self, host: str) -> Optional[SSHSession]:
        return self.sessions.get(host)

    def close_session(self, host: str):
        if host in self.sessions:
            self.sessions[host].disconnect()
            del self.sessions[host]

    def close_all(self):
        for session in self.sessions.values():
            session.disconnect()
        self.sessions.clear()

    def run_remote_diagnostics(self, host: str, username: str,
                                password: str, port: int = 22) -> dict:
        """
        Executa diagnósticos remotos via SSH e retorna métricas avançadas.
        Útil para pegar métricas que só o dispositivo remoto pode fornecer.
        """
        session = self.create_session(host, username, password, port)
        success, msg = session.connect()
        if not success:
            return {"error": msg}

        results = {}

        try:
            # Uptime
            out, _ = session.exec_single("uptime")
            results["uptime"] = out.strip()

            # Interface info (velocidade, duplex)
            out, _ = session.exec_single("cat /sys/class/net/eth0/speed 2>/dev/null || echo N/A")
            results["interface_speed"] = out.strip()

            out, _ = session.exec_single("cat /sys/class/net/eth0/duplex 2>/dev/null || echo N/A")
            results["duplex"] = out.strip()

            # Estatísticas de rede
            out, _ = session.exec_single("cat /proc/net/dev 2>/dev/null | head -5")
            results["net_stats"] = out.strip()

            # TCP retransmissions
            out, _ = session.exec_single("cat /proc/net/snmp 2>/dev/null | grep Tcp")
            results["tcp_stats"] = out.strip()

            # DNS resolution time
            out, _ = session.exec_single("dig google.com +stats 2>/dev/null | grep 'Query time'")
            results["dns_time"] = out.strip()

            # Queue/buffer
            out, _ = session.exec_single("tc -s qdisc show dev eth0 2>/dev/null | head -5")
            results["qos_stats"] = out.strip()

            # DHCP lease
            out, _ = session.exec_single("cat /var/lib/dhcp/dhclient.*.leases 2>/dev/null | tail -20")
            results["dhcp_lease"] = out.strip()

            # Signal (para interfaces wireless)
            out, _ = session.exec_single("iwconfig 2>/dev/null | grep -i 'signal\\|quality'")
            results["wifi_signal"] = out.strip()

        except Exception as e:
            results["error"] = str(e)
        finally:
            session.disconnect()

        return results
