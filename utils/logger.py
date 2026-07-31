"""
Sistema de logs rotativos por dia — NetWatch Pro.

Política de retenção:
  • Logs diários: mantém os últimos 14 dias sem compressão
  • Semana 3–4:   compacta cada dia num ZIP semanal (semana_AAAA-WNN.zip)
  • Mês 2+:       compacta semanas num ZIP mensal (mensal_AAAA-MM.zip)
  • Meses 4+:     apaga archives mensais (≈ 4 meses de histórico)

A LogsView limita a exibição a 2 dias de logs.
"""
import logging
import os
import re
import shutil
import threading
import zipfile
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from config import LOGS_DIR, LOG_CONFIG

# ── Constantes de retenção ────────────────────────────────────────────
KEEP_RAW_DAYS   = 14    # dias sem compressão
KEEP_WEEKLY_WKS = 8     # semanas compactadas (≈ 2 meses)
KEEP_MONTHLY_MO = 4     # meses arquivados

# ── Registro de loggers já criados ───────────────────────────────────
_loggers: dict[str, logging.Logger] = {}
_archiver_started = False


# ── Configuração do logger ────────────────────────────────────────────

def setup_logger(name: str) -> logging.Logger:
    """
    Retorna (ou cria) um logger com rotação diária à meia-noite.
    Todos os módulos escrevem no mesmo arquivo netwatch_AAAA-MM-DD.log.
    """
    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(name)
    if logger.handlers:
        _loggers[name] = logger
        return logger

    logger.setLevel(logging.DEBUG)

    # ── Handler de arquivo: rotação diária ───────────────────────────
    log_path = LOGS_DIR / "netwatch.log"
    file_handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=KEEP_RAW_DAYS,
        encoding="utf-8",
        utc=False,
    )
    # Renomeia para netwatch_AAAA-MM-DD.log em vez de netwatch.log.AAAA-MM-DD
    file_handler.namer = _log_namer
    file_handler.rotator = _log_rotator
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter(LOG_CONFIG["format"], LOG_CONFIG["date_format"])
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)

    # ── Handler de console (INFO+) ────────────────────────────────────
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_fmt = logging.Formatter(
        "%(asctime)s │ %(levelname)-8s │ %(name)-12s │ %(message)s",
        "%H:%M:%S",
    )
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)

    _loggers[name] = logger

    # Inicia o arquivador em background (uma vez)
    global _archiver_started
    if not _archiver_started:
        _archiver_started = True
        threading.Thread(target=_archive_loop, daemon=True).start()

    return logger


# ── Nomeação dos arquivos rotacionados ────────────────────────────────

def _log_namer(default_name: str) -> str:
    """
    Converte:  netwatch.log.2026-03-10  →  netwatch_2026-03-10.log
    """
    base_path = Path(default_name)
    stem = base_path.stem          # "netwatch.log"
    suffix = base_path.suffix      # ".2026-03-10"
    date_str = suffix.lstrip(".")  # "2026-03-10"
    # Extrai somente a parte base (sem .log embutido)
    real_stem = Path(stem).stem    # "netwatch"
    return str(LOGS_DIR / f"{real_stem}_{date_str}.log")


def _log_rotator(source: str, dest: str):
    """Executa a rotação (move arquivo)."""
    if os.path.exists(source):
        shutil.move(source, dest)


# ── Leitura de logs ───────────────────────────────────────────────────

def get_log_files(max_days: int = 2) -> list[Path]:
    """
    Retorna arquivos de log dos últimos `max_days` dias, do mais recente ao mais antigo.
    Usado pela LogsView — limitado a 2 dias por padrão.
    """
    cutoff = datetime.now() - timedelta(days=max_days)
    files = []

    # Arquivo do dia atual
    current = LOGS_DIR / "netwatch.log"
    if current.exists():
        files.append(current)

    # Arquivos rotacionados netwatch_AAAA-MM-DD.log
    for f in sorted(LOGS_DIR.glob("netwatch_????-??-??.log"), reverse=True):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
        if m:
            try:
                file_date = datetime.strptime(m.group(1), "%Y-%m-%d")
                if file_date >= cutoff:
                    files.append(f)
            except ValueError:
                pass

    return files


def read_log_tail(filename: str = "netwatch.log", lines: int = 500) -> list[str]:
    """Lê as últimas N linhas de um arquivo de log."""
    log_path = LOGS_DIR / filename
    if not log_path.exists():
        return []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            return all_lines[-lines:]
    except Exception:
        return []


def read_recent_logs(max_days: int = 2, max_lines: int = 2000) -> list[str]:
    """
    Lê os logs dos últimos `max_days` dias (limite: max_lines linhas no total).
    Retorna linhas ordenadas do mais antigo para o mais recente.
    """
    all_lines: list[str] = []
    for log_file in reversed(get_log_files(max_days)):  # do mais antigo ao mais novo
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                all_lines.extend(f.readlines())
        except Exception:
            pass
    return all_lines[-max_lines:]


# ── Arquivador em background ──────────────────────────────────────────

def _archive_loop():
    """
    Loop daemon que executa o arquivamento uma vez por dia.
    Primeira execução: 5 min após iniciar (evita impacto no boot).
    """
    import time
    time.sleep(300)   # aguarda 5 min
    while True:
        try:
            _run_archive()
        except Exception as e:
            # Não quebra o loop por erro de I/O
            print(f"[LogArchiver] Erro: {e}")
        # Próxima execução: 24 h
        import time as _t
        _t.sleep(86400)


def _run_archive():
    """
    Política de arquivamento:
      - Raw logs > KEEP_RAW_DAYS  → compacta em ZIP semanal
      - Zips semanais > KEEP_WEEKLY_WKS semanas → compacta em ZIP mensal
      - Zips mensais  > KEEP_MONTHLY_MO meses  → apaga
    """
    now = datetime.now()

    # ── 1. Compactar dias antigos em ZIPs semanais ─────────────────
    raw_cutoff = now - timedelta(days=KEEP_RAW_DAYS)
    for log_file in LOGS_DIR.glob("netwatch_????-??-??.log"):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", log_file.name)
        if not m:
            continue
        try:
            file_date = datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            continue
        if file_date >= raw_cutoff:
            continue  # ainda dentro da janela raw

        # Nome do ZIP semanal: semana_AAAA-WNN.zip
        iso_year, iso_week, _ = file_date.isocalendar()
        zip_name = LOGS_DIR / f"semana_{iso_year}-W{iso_week:02d}.zip"
        _add_to_zip(log_file, zip_name)
        log_file.unlink(missing_ok=True)

    # ── 2. Compactar semanas antigas em ZIPs mensais ───────────────
    weekly_cutoff = now - timedelta(weeks=KEEP_WEEKLY_WKS)
    for weekly_zip in LOGS_DIR.glob("semana_????-W??.zip"):
        m = re.search(r"(\d{4})-W(\d{2})", weekly_zip.name)
        if not m:
            continue
        try:
            year, week = int(m.group(1)), int(m.group(2))
            # Primeiro dia da semana ISO
            week_date = datetime.fromisocalendar(year, week, 1)
        except (ValueError, AttributeError):
            continue
        if week_date >= weekly_cutoff:
            continue

        # Nome do ZIP mensal: mensal_AAAA-MM.zip
        zip_name = LOGS_DIR / f"mensal_{week_date.year}-{week_date.month:02d}.zip"
        _add_to_zip(weekly_zip, zip_name)
        weekly_zip.unlink(missing_ok=True)

    # ── 3. Apagar archives mensais muito antigos ───────────────────
    monthly_cutoff = now - timedelta(days=KEEP_MONTHLY_MO * 30)
    for monthly_zip in LOGS_DIR.glob("mensal_????-??.zip"):
        m = re.search(r"(\d{4})-(\d{2})", monthly_zip.name)
        if not m:
            continue
        try:
            archive_date = datetime(int(m.group(1)), int(m.group(2)), 1)
        except ValueError:
            continue
        if archive_date < monthly_cutoff:
            monthly_zip.unlink(missing_ok=True)


def _add_to_zip(source: Path, zip_path: Path):
    """Adiciona `source` ao ZIP `zip_path` (cria ou acrescenta)."""
    try:
        mode = "a" if zip_path.exists() else "w"
        with zipfile.ZipFile(zip_path, mode, compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(source, arcname=source.name)
    except Exception as e:
        print(f"[LogArchiver] Falha ao zipar {source.name}: {e}")