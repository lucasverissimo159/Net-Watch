"""
Módulo de Segurança — NetWatch Pro v2.12

Fornece:
  • Criptografia transparente de senhas SSH (Fernet)
  • Validação rígida de IPs/hostnames (anti-command injection)
  • Sanitização de nomes de arquivo (anti-path traversal)
  • Gerenciamento de host keys SSH (anti-MITM)
  • Escape de HTML para relatórios (anti-XSS)

Criado por Lucas Veríssimo
"""
import base64
import hashlib
import html
import ipaddress
import os
import re
import secrets
import shlex
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from config import DATA_DIR
from utils.logger import setup_logger

logger = setup_logger("security")


# ══════════════════════════════════════════════════════════════════════
# CRIPTOGRAFIA — chave de deployment + Fernet
# ══════════════════════════════════════════════════════════════════════

MASTER_KEY_PATH = DATA_DIR / ".master.key"
_PREFIX = "enc::"  # marca senhas criptografadas no DB/JSON


def _load_or_create_master_key() -> bytes:
    """
    Carrega a chave mestra de criptografia. Se não existir, gera uma nova
    e tenta restringir permissões do arquivo.

    A chave fica em data/.master.key — DEVE ser copiada para a pasta
    compartilhada antes que outros .exe sejam abertos.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if MASTER_KEY_PATH.exists():
        try:
            return MASTER_KEY_PATH.read_bytes().strip()
        except Exception as e:
            logger.error(f"Erro ao ler chave mestra: {e}")
            raise

    # Gera nova chave (Fernet espera 32 bytes em base64 urlsafe)
    key = Fernet.generate_key()
    try:
        MASTER_KEY_PATH.write_bytes(key)
        _restrict_permissions(MASTER_KEY_PATH)
        logger.info(f"Nova chave mestra gerada em {MASTER_KEY_PATH}")
    except Exception as e:
        logger.error(f"Erro ao gravar chave mestra: {e}")
    return key


def _restrict_permissions(path: Path):
    """
    Restringe permissões de arquivo sensível.

    No Linux/Mac: chmod 600 (apenas owner lê/escreve).
    No Windows: usa icacls para restringir ao usuário atual.
    """
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass

    if os.name == "nt":
        try:
            import subprocess
            user = os.environ.get("USERNAME", "")
            if user:
                # Remove herança + dá acesso apenas ao usuário atual
                subprocess.run(
                    ["icacls", str(path), "/inheritance:r",
                     "/grant:r", f"{user}:F"],
                    capture_output=True, check=False, timeout=5
                )
        except Exception:
            pass


_fernet: Optional[Fernet] = None

def _get_fernet() -> Fernet:
    """Singleton Fernet inicializado lazy."""
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_master_key())
    return _fernet


def encrypt_password(plain: str) -> str:
    """
    Criptografa uma senha. Retorna string com prefixo 'enc::' para
    identificar senhas já criptografadas.

    Strings vazias retornam vazio (sem encriptação).
    Strings já com prefixo 'enc::' retornam inalteradas (idempotente).
    """
    if not plain:
        return ""
    if plain.startswith(_PREFIX):
        return plain  # já criptografada
    try:
        token = _get_fernet().encrypt(plain.encode("utf-8"))
        return _PREFIX + token.decode("ascii")
    except Exception as e:
        logger.error(f"Erro ao criptografar: {e}")
        return plain  # fallback — melhor manter funcionando que travar


def decrypt_password(encrypted: str) -> str:
    """
    Descriptografa uma senha com prefixo 'enc::'.
    Senhas sem prefixo são retornadas como estão (compat com dados legados).
    """
    if not encrypted:
        return ""
    if not encrypted.startswith(_PREFIX):
        return encrypted  # senha legada em texto puro
    try:
        token = encrypted[len(_PREFIX):].encode("ascii")
        return _get_fernet().decrypt(token).decode("utf-8")
    except InvalidToken:
        logger.warning("Senha com formato encriptado inválido — chave mestra incorreta?")
        return ""
    except Exception as e:
        logger.error(f"Erro ao descriptografar: {e}")
        return ""


def is_encrypted(value: str) -> bool:
    """Retorna True se a string parece estar criptografada."""
    return bool(value and value.startswith(_PREFIX))


# ══════════════════════════════════════════════════════════════════════
# VALIDAÇÃO DE IPs E HOSTNAMES — anti command injection
# ══════════════════════════════════════════════════════════════════════

# Hostnames válidos: letras, dígitos, hífens, pontos. Máx 253 chars.
_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
                          r"(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$")


def is_valid_ip(value: str) -> bool:
    """Retorna True se for um IP válido (IPv4 ou IPv6)."""
    if not value or not isinstance(value, str):
        return False
    try:
        ipaddress.ip_address(value.strip())
        return True
    except (ValueError, TypeError):
        return False


def is_valid_hostname(value: str) -> bool:
    """Retorna True se for um hostname/FQDN válido."""
    if not value or not isinstance(value, str):
        return False
    value = value.strip()
    if len(value) > 253:
        return False
    return bool(_HOSTNAME_RE.match(value))


def is_valid_target(value: str) -> bool:
    """Retorna True se for IP ou hostname válido."""
    return is_valid_ip(value) or is_valid_hostname(value)


def sanitize_target(value: str) -> str:
    """
    Sanitiza um IP/hostname para uso seguro em comando SSH.

    1. Faz strip de whitespace
    2. Valida formato (rejeita inválidos retornando string vazia)
    3. Aplica shlex.quote para escape de shell

    Use SEMPRE este wrapper antes de substituir em comandos SSH.
    """
    if not value:
        return ""
    value = value.strip()
    if not is_valid_target(value):
        logger.warning(f"Target rejeitado por validação: {value!r}")
        return ""
    # Mesmo validado, escapa por defesa em profundidade
    return shlex.quote(value)


def is_valid_port(value) -> bool:
    """Retorna True se for porta válida (1-65535)."""
    try:
        p = int(value)
        return 1 <= p <= 65535
    except (ValueError, TypeError):
        return False


# ══════════════════════════════════════════════════════════════════════
# SANITIZAÇÃO DE NOMES DE ARQUIVO — anti path traversal
# ══════════════════════════════════════════════════════════════════════

_FILENAME_INVALID = re.compile(r"[^a-zA-Z0-9_\-]")


def safe_filename(label: str, max_len: int = 80) -> str:
    """
    Sanitiza um label para uso como nome de arquivo.

    Mantém apenas: letras ASCII, dígitos, underscore, hífen.
    Tudo o mais vira underscore. Trunca em max_len.
    Rejeita strings vazias após sanitização retornando 'unnamed'.
    """
    if not label:
        return "unnamed"
    # Substitui espaço por underscore primeiro
    s = label.replace(" ", "_")
    # Remove acentos básicos
    s = (s.replace("ã", "a").replace("á", "a").replace("â", "a").replace("à", "a")
          .replace("é", "e").replace("ê", "e").replace("è", "e")
          .replace("í", "i").replace("ì", "i")
          .replace("ó", "o").replace("ô", "o").replace("ò", "o").replace("õ", "o")
          .replace("ú", "u").replace("ù", "u").replace("ü", "u")
          .replace("ç", "c")
          .replace("Ã", "A").replace("Á", "A").replace("Â", "A").replace("À", "A")
          .replace("É", "E").replace("Ê", "E").replace("È", "E")
          .replace("Í", "I").replace("Ì", "I")
          .replace("Ó", "O").replace("Ô", "O").replace("Ò", "O").replace("Õ", "O")
          .replace("Ú", "U").replace("Ù", "U").replace("Ü", "U")
          .replace("Ç", "C"))
    # Substitui qualquer outro caractere inválido por underscore
    s = _FILENAME_INVALID.sub("_", s)
    # Remove underscores consecutivos
    while "__" in s:
        s = s.replace("__", "_")
    s = s.strip("_")
    # Trunca
    s = s[:max_len]
    return s or "unnamed"


# ══════════════════════════════════════════════════════════════════════
# HOST KEYS SSH — anti MITM
# ══════════════════════════════════════════════════════════════════════

KNOWN_HOSTS_PATH = DATA_DIR / "known_hosts"


class TrustOnFirstUsePolicy:
    """
    Política de host key SSH com Trust-on-First-Use.

    Comportamento:
      • Primeira conexão a um host → aceita e grava a fingerprint
      • Conexões seguintes → compara fingerprint
      • Fingerprint mudou → loga ALERTA mas conecta (evita break em deploys
        legítimos como troca de equipamento) e atualiza
      • UI pode consultar get_changed_keys() para mostrar warnings

    Pode ser elevado para "strict mode" via config no futuro.
    """

    _changed_keys: list[tuple[str, str, str]] = []  # (host, old_fp, new_fp)

    def __init__(self):
        from paramiko import HostKeys
        self.host_keys = HostKeys()
        if KNOWN_HOSTS_PATH.exists():
            try:
                self.host_keys.load(str(KNOWN_HOSTS_PATH))
            except Exception as e:
                logger.warning(f"known_hosts corrompido, recriando: {e}")
                self.host_keys = HostKeys()

    def missing_host_key(self, client, hostname, key):
        """Chamado pelo paramiko quando a chave do host não é conhecida."""
        new_fp = self._fingerprint(key)
        existing = self.host_keys.lookup(hostname)
        if existing:
            for keytype in existing:
                old_fp = self._fingerprint(existing[keytype])
                if old_fp != new_fp:
                    logger.warning(
                        f"⚠ HOST KEY CHANGED para {hostname}!\n"
                        f"  Antiga: {old_fp}\n"
                        f"  Nova:   {new_fp}\n"
                        f"  Possível MITM ou troca de equipamento legítima."
                    )
                    self._changed_keys.append((hostname, old_fp, new_fp))
                    break
        else:
            logger.info(f"Nova host key aceita (TOFU): {hostname} → {new_fp}")
        # Adiciona/atualiza
        self.host_keys.add(hostname, key.get_name(), key)
        try:
            self.host_keys.save(str(KNOWN_HOSTS_PATH))
            _restrict_permissions(KNOWN_HOSTS_PATH)
        except Exception as e:
            logger.debug(f"Erro ao salvar known_hosts: {e}")

    @staticmethod
    def _fingerprint(key) -> str:
        """SHA256 fingerprint da chave, formato OpenSSH."""
        digest = hashlib.sha256(key.asbytes()).digest()
        return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")

    @classmethod
    def get_changed_keys(cls) -> list:
        """Retorna lista de host keys que mudaram (para alerta na UI)."""
        return list(cls._changed_keys)

    @classmethod
    def clear_changed_keys(cls):
        cls._changed_keys.clear()


# ══════════════════════════════════════════════════════════════════════
# ESCAPE HTML — anti XSS
# ══════════════════════════════════════════════════════════════════════

def html_escape(value) -> str:
    """
    Escapa valor para inserção segura em HTML.
    Converte None para "—", outros tipos para str().
    """
    if value is None:
        return "—"
    return html.escape(str(value), quote=True)


# ══════════════════════════════════════════════════════════════════════
# OUTRAS UTILIDADES
# ══════════════════════════════════════════════════════════════════════

def mask_password(value: str) -> str:
    """Retorna senha mascarada para exibição em logs/UI."""
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]
