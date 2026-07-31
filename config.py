"""
Configuracao central do NetWatch Pro v2.4
Criado por Lucas Verissimo

ARQUITETURA DE DIRETORIOS (hibrida):

  COMPARTILHADO (ao lado do .exe - pasta de rede):
    data/netwatch.db       <- banco de dados (hosts, pings, alertas)
    data/config.json       <- credenciais SSH, parametros de monitoramento
    audio/                 <- audios TTS de cada host

  POR USUARIO (USERPROFILE/.netwatch_pro/):
    logs/                  <- logs do sistema (cada maquina gera os seus)
    user_prefs.json        <- mute, preferencias visuais individuais

  Isso garante que:
    - Todos os PCs veem os mesmos hosts, dados e audios
    - Cada usuario controla seu proprio mute sem afetar o servidor
    - Logs ficam separados por maquina (facilita diagnostico)
"""
import os
import sys
import json
from pathlib import Path


# ======================================================================
# PATHS
# ======================================================================

def resource_path(relative: str) -> Path:
    """
    Retorna caminho de um recurso EMPACOTADO dentro do .exe.
    No PyInstaller --onefile, os assets vao para _MEIPASS (pasta temporaria).
    Em desenvolvimento, usa o diretorio do script.
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return Path(base) / relative


def _get_app_dir() -> Path:
    """
    Retorna o diretorio onde o .exe (ou main.py) esta.
    Usado para dados COMPARTILHADOS (DB, audios, config de hosts).
    """
    if getattr(sys, "frozen", False):
        return Path(os.path.dirname(sys.executable))
    else:
        return Path(os.path.dirname(os.path.abspath(__file__)))


def _get_user_dir() -> Path:
    """
    Retorna diretorio POR USUARIO do Windows.
    Usado para logs e preferencias individuais (mute, etc).
    Cada usuario do Windows tem seu proprio USERPROFILE.
    """
    return Path(os.path.expanduser("~")) / ".netwatch_pro"


# -- Diretorios da aplicacao -------------------------------------------
APP_NAME = "NetWatch Pro"
APP_VERSION = "2.4.0"
APP_AUTHOR = "Lucas Veríssimo"

# COMPARTILHADO - ao lado do .exe (pasta de rede)
SHARED_DIR  = _get_app_dir()
DATA_DIR    = SHARED_DIR / "data"
AUDIO_DIR   = SHARED_DIR / "audio"
DB_PATH     = DATA_DIR / "netwatch.db"
CONFIG_PATH = DATA_DIR / "config.json"

# POR USUARIO - %USERPROFILE%/.netwatch_pro/
USER_DIR        = _get_user_dir()
LOGS_DIR        = USER_DIR / "logs"
USER_PREFS_PATH = USER_DIR / "user_prefs.json"

# Atalho legado (para quem importa BASE_DIR de config)
BASE_DIR = SHARED_DIR

# Garante que os diretorios existam
for d in [DATA_DIR, AUDIO_DIR, LOGS_DIR]:
    d.mkdir(parents=True, exist_ok=True)


# ======================================================================
# TEMA E CORES
# ======================================================================

COLORS = {
    "bg_primary":       "#0F1117",
    "bg_secondary":     "#161B22",
    "bg_tertiary":      "#1C2333",
    "bg_elevated":      "#21283B",
    "sidebar":          "#0D1117",
    "border":           "#2A3040",
    "border_focus":     "#3B82F6",

    "text_primary":     "#E6EDF3",
    "text_secondary":   "#8B949E",
    "text_muted":       "#484F58",
    "text_inverse":     "#0F1117",

    "accent_blue":      "#3B82F6",
    "accent_blue_hover":"#2563EB",
    "accent_green":     "#10B981",
    "accent_green_dim": "#064E3B",
    "accent_red":       "#EF4444",
    "accent_red_dim":   "#7F1D1D",
    "accent_yellow":    "#F59E0B",
    "accent_yellow_dim":"#78350F",
    "accent_purple":    "#8B5CF6",
    "accent_orange":    "#F97316",
    "accent_cyan":      "#06B6D4",

    "chart_grid":       "#1E2A3A",
    "chart_line1":      "#3B82F6",
    "chart_line2":      "#10B981",
    "chart_line3":      "#F59E0B",
    "chart_fill":       "#3B82F620",

    "scrollbar":        "#2A3040",
    "scrollbar_hover":  "#3B4560",
}

FONTS = {
    "family":           "Segoe UI",
    "family_mono":      "Cascadia Code",
    "size_xs":          10,
    "size_sm":          11,
    "size_md":          13,
    "size_lg":          16,
    "size_xl":          20,
    "size_xxl":         28,
    "size_display":     36,
}


# ======================================================================
# PARAMETROS DE MONITORAMENTO
# ======================================================================

MONITOR_DEFAULTS = {
    "ping_count":       4,
    "ping_timeout_ms":  1000,
    "cycle_delay_s":    10,
    "between_hosts_s":  0.5,
    "retry_intervals":  [300, 900, 1800, 3600],
    "max_history":      1000,
    "traceroute_interval_s": 300,
    "cycle_size":       100,
    "fallback_target":  "8.8.8.8",
    "max_concurrent_traceroutes": 5,
}

THRESHOLDS = {
    "latency_warning_ms":   100,
    "latency_critical_ms":  250,
    "jitter_warning_ms":    30,
    "jitter_critical_ms":   80,
    "loss_warning_pct":     5,
    "loss_critical_pct":    20,
    "dns_warning_ms":       100,
    "dns_critical_ms":      500,
}

LOG_CONFIG = {
    "max_bytes":        5 * 1024 * 1024,
    "backup_count":     10,
    "format":           "%(asctime)s │ %(levelname)-8s │ %(name)-20s │ %(message)s",
    "date_format":      "%Y-%m-%d %H:%M:%S",
}

SSH_DEFAULTS = {
    "port":             22,
    "timeout":          10,
    "keepalive":        30,
    "default_user":     "",
    "default_password": "",
}

# Exemplos usando faixa reservada para documentacao (RFC 5737).
# Ajuste para os IPs reais da sua rede no arquivo data/config.json (nao versionado).
DEFAULT_STORE_MAP = {
    "203.0.113.191":  "Depósito",
    "203.0.113.202":  "Administrativo",
    "203.0.113.80":   "Loja 09",
    "203.0.113.231":  "Loja 31",
    "203.0.113.27":   "Loja 26",
}

DEFAULT_EXCLUDED_IPS = [
    "203.0.113.109", "203.0.113.110", "203.0.113.126",
    "203.0.113.131", "203.0.113.135", "203.0.113.163",
    "203.0.113.167",
]


# ======================================================================
# CONFIG COMPARTILHADO (ao lado do .exe)
# Usado para: credenciais SSH, parametros de monitoramento
# ======================================================================

def load_user_config() -> dict:
    """Carrega config COMPARTILHADO (data/config.json ao lado do .exe)."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_user_config(cfg: dict):
    """Salva config COMPARTILHADO (data/config.json ao lado do .exe).
    CORREÇÃO v2.12: criptografa ssh_default_password antes de gravar."""
    cfg_to_save = dict(cfg)
    if cfg_to_save.get("ssh_default_password"):
        try:
            from utils.security import encrypt_password
            cfg_to_save["ssh_default_password"] = encrypt_password(
                cfg_to_save["ssh_default_password"])
        except Exception:
            pass  # fallback silencioso
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg_to_save, f, indent=2, ensure_ascii=False)


def get_ssh_credentials() -> tuple[str, str]:
    """Retorna (user, password) das credenciais SSH compartilhadas.
    CORREÇÃO v2.12: descriptografa ssh_default_password ao retornar."""
    cfg = load_user_config()
    user = cfg.get("ssh_default_user", SSH_DEFAULTS["default_user"])
    pwd = cfg.get("ssh_default_password", SSH_DEFAULTS["default_password"])
    if pwd:
        try:
            from utils.security import decrypt_password
            pwd = decrypt_password(pwd)
        except Exception:
            pass
    return user, pwd


def get_google_target() -> str:
    """
    Retorna o IP alvo para o ping 'Google' (terceiro destino SSH).
    Padrão: 8.8.8.8. Configurável em Configurações → Credenciais SSH.
    """
    cfg = load_user_config()
    return cfg.get("google_target", "8.8.8.8") or "8.8.8.8"


def set_google_target(ip: str):
    """Persiste o IP alvo do Google no config compartilhado."""
    cfg = load_user_config()
    cfg["google_target"] = ip.strip() or "8.8.8.8"
    save_user_config(cfg)


# ======================================================================
# PREFERENCIAS POR USUARIO (%USERPROFILE%/.netwatch_pro/user_prefs.json)
# Usado para: mute, janela, etc - cada maquina tem o seu
# ======================================================================

def _load_user_prefs() -> dict:
    """Carrega preferencias INDIVIDUAIS do usuario da maquina."""
    if USER_PREFS_PATH.exists():
        try:
            with open(USER_PREFS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_user_prefs(prefs: dict):
    """Salva preferencias INDIVIDUAIS do usuario da maquina."""
    USER_PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(USER_PREFS_PATH, "w", encoding="utf-8") as f:
        json.dump(prefs, f, indent=2, ensure_ascii=False)


def is_audio_muted() -> bool:
    """Retorna True se o audio esta silenciado NESTA MAQUINA."""
    prefs = _load_user_prefs()
    return prefs.get("audio_muted", False)


def set_audio_muted(muted: bool):
    """Persiste a preferencia de mute DESTA MAQUINA."""
    prefs = _load_user_prefs()
    prefs["audio_muted"] = muted
    _save_user_prefs(prefs)