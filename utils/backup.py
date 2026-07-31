"""
Sistema de backup incremental — NetWatch Pro v2.6.

ESCALÁVEL para 5.000–30.000 hosts:
  • INCREMENTAL: só copia arquivos novos ou modificados (compara mtime+size).
  • DB: usa SQLite BACKUP API (snapshot consistente sem copiar WAL/journal).
  • ÁUDIO: compara por arquivo — 30k hosts = 60k MP3, mas só copia os novos.
  • LIMITE DE ESPAÇO: se backup total > MAX_BACKUP_MB, apaga os mais antigos.

ALTERAÇÕES v2.6:
  • SERVIDOR: backup incremental DIÁRIO (antes era a cada 15 dias).
  • VISUALIZADOR: backup incremental ao abrir (máx 1x a cada 12h).
  • CAMINHOS DE REDE: o usuário pode configurar caminhos adicionais na rede
    (ex: \\\\servidor\\backup, Z:\\backups\\netwatch) para onde o backup
    será replicado. Cada caminho de rede recebe uma cópia incremental.
  • Destino local: %USERPROFILE%/.netwatch_pro/*_backup/
  • Destinos de rede: configuráveis via user_prefs.json["backup_network_paths"]

PERFORMANCE (estimativas com 30k hosts):
  • Primeiro backup: ~3GB áudio + ~200MB DB = ~3.2GB (uns 30-60s em rede local)
  • Backups seguintes: apenas arquivos novos/modificados = segundos
"""
import os
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from config import (
    SHARED_DIR, DATA_DIR, AUDIO_DIR, LOGS_DIR,
    USER_DIR, DB_PATH,
    _load_user_prefs, _save_user_prefs,
)
from utils.logger import setup_logger

logger = setup_logger("backup")

# ── Config ────────────────────────────────────────────────────────────
BACKUP_DATA_DIR  = USER_DIR / "data_backup"
BACKUP_AUDIO_DIR = USER_DIR / "audio_backup"
BACKUP_LOGS_DIR  = USER_DIR / "logs_backup"

SERVER_BACKUP_INTERVAL_DAYS = 1   # v2.6: backup diário (antes era 15)
VIEWER_BACKUP_MIN_HOURS = 12

# Limite de espaço total para backups (MB). 0 = sem limite.
MAX_BACKUP_MB = 5000   # 5 GB


# ══════════════════════════════════════════════════════════════════════
# INCREMENTAL COPY
# ══════════════════════════════════════════════════════════════════════

def _needs_copy(src: Path, dst: Path) -> bool:
    """
    Retorna True se o arquivo precisa ser copiado.
    Compara mtime (±2s tolerância para rede) e tamanho.
    Muito mais rápido que hash em 60k arquivos.
    """
    if not dst.exists():
        return True
    try:
        src_stat = src.stat()
        dst_stat = dst.stat()
        # Tamanho diferente → copia
        if src_stat.st_size != dst_stat.st_size:
            return True
        # mtime diferente (com tolerância para FAT32/rede) → copia
        if abs(src_stat.st_mtime - dst_stat.st_mtime) > 2.0:
            return True
        return False
    except Exception:
        return True  # na dúvida, copia


def _incremental_copy_folder(src: Path, dst: Path, label: str) -> tuple[int, int]:
    """
    Copia incrementalmente src → dst.
    Retorna (arquivos_copiados, arquivos_pulados).
    """
    if not src.exists():
        return 0, 0

    dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0

    for item in src.iterdir():
        if item.is_file():
            # Pula temporários do SQLite e lock files
            if item.suffix in (".lock", ".tmp", ".shm", ".wal"):
                continue
            # Pula o próprio DB — usa SQLite backup API separado
            if item.name == "netwatch.db":
                continue

            dst_file = dst / item.name
            if _needs_copy(item, dst_file):
                try:
                    shutil.copy2(str(item), str(dst_file))
                    copied += 1
                except PermissionError:
                    try:
                        with open(item, "rb") as f_in:
                            with open(dst_file, "wb") as f_out:
                                # Copia em chunks para não explodir memória
                                while True:
                                    chunk = f_in.read(1024 * 1024)  # 1MB chunks
                                    if not chunk:
                                        break
                                    f_out.write(chunk)
                        copied += 1
                    except Exception as e:
                        logger.warning(f"Backup {label}: falha em {item.name}: {e}")
                except Exception as e:
                    logger.warning(f"Backup {label}: erro em {item.name}: {e}")
            else:
                skipped += 1

    # Remove arquivos no backup que não existem mais na origem
    # (ex: host excluído → áudio removido)
    if dst.exists():
        src_names = {f.name for f in src.iterdir() if f.is_file()} if src.exists() else set()
        for dst_item in dst.iterdir():
            if dst_item.is_file() and dst_item.name not in src_names:
                # Arquivo não existe mais na origem — host foi excluído
                if dst_item.name != "netwatch.db":  # não apaga backup do DB
                    try:
                        dst_item.unlink()
                        logger.debug(f"Backup {label}: removido {dst_item.name} (não existe na origem)")
                    except Exception:
                        pass

    return copied, skipped


# ══════════════════════════════════════════════════════════════════════
# SQLITE BACKUP API (snapshot consistente)
# ══════════════════════════════════════════════════════════════════════

def _backup_database() -> bool:
    """
    Copia o DB usando a SQLite backup API.
    Isso garante um snapshot consistente mesmo com escritas em andamento
    (WAL mode). Muito melhor que copiar o arquivo direto.
    """
    dst_db = BACKUP_DATA_DIR / "netwatch.db"
    BACKUP_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Verifica se precisa copiar (mtime + size)
    if dst_db.exists() and DB_PATH.exists():
        try:
            src_stat = DB_PATH.stat()
            dst_stat = dst_db.stat()
            if (src_stat.st_size == dst_stat.st_size and
                    abs(src_stat.st_mtime - dst_stat.st_mtime) < 2.0):
                return False  # não mudou
        except Exception:
            pass

    try:
        src_conn = sqlite3.connect(str(DB_PATH), timeout=10)
        dst_conn = sqlite3.connect(str(dst_db))
        try:
            src_conn.backup(dst_conn)
        finally:
            # CORREÇÃO v2.6 — resource leak:
            #   As conexões não eram fechadas se backup() lançasse exceção.
            #   finally garante o fechamento em qualquer cenário.
            dst_conn.close()
            src_conn.close()
        logger.debug("Backup DB via SQLite backup API: OK")
        return True
    except Exception as e:
        logger.warning(f"SQLite backup API falhou: {e} — tentando cópia direta")
        # Fallback: cópia direta do arquivo
        try:
            if DB_PATH.exists():
                with open(DB_PATH, "rb") as f_in:
                    with open(dst_db, "wb") as f_out:
                        while True:
                            chunk = f_in.read(4 * 1024 * 1024)  # 4MB
                            if not chunk:
                                break
                            f_out.write(chunk)
                return True
        except Exception as e2:
            logger.error(f"Backup DB fallback também falhou: {e2}")
            return False


# ══════════════════════════════════════════════════════════════════════
# SIZE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════

def _get_backup_size_mb() -> float:
    """Retorna tamanho total dos backups em MB."""
    total = 0
    for bdir in [BACKUP_DATA_DIR, BACKUP_AUDIO_DIR, BACKUP_LOGS_DIR]:
        if bdir.exists():
            for f in bdir.rglob("*"):
                if f.is_file():
                    try:
                        total += f.stat().st_size
                    except Exception:
                        pass
    return total / (1024 * 1024)


def _enforce_size_limit():
    """
    Se o backup total excede MAX_BACKUP_MB, apaga logs_backup primeiro
    (mais facilmente regeneráveis), depois áudios antigos.
    """
    if MAX_BACKUP_MB <= 0:
        return  # sem limite

    current_mb = _get_backup_size_mb()
    if current_mb <= MAX_BACKUP_MB:
        return

    logger.warning(
        f"Backup excede limite ({current_mb:.0f}MB > {MAX_BACKUP_MB}MB) — "
        "limpando logs_backup"
    )

    # Fase 1: limpa logs_backup (mais dispensável)
    if BACKUP_LOGS_DIR.exists():
        shutil.rmtree(str(BACKUP_LOGS_DIR), ignore_errors=True)

    current_mb = _get_backup_size_mb()
    if current_mb <= MAX_BACKUP_MB:
        return

    # Fase 2: se ainda excede, apaga áudios mais antigos (por mtime)
    logger.warning(f"Ainda {current_mb:.0f}MB — removendo áudios antigos do backup")
    if BACKUP_AUDIO_DIR.exists():
        files = sorted(
            [f for f in BACKUP_AUDIO_DIR.iterdir() if f.is_file()],
            key=lambda f: f.stat().st_mtime,
        )
        for f in files:
            if _get_backup_size_mb() <= MAX_BACKUP_MB:
                break
            try:
                f.unlink()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════

def run_backup() -> dict:
    """
    Executa backup incremental.
    Retorna dict com resultados.
    """
    start = datetime.now()
    results = {}

    # 1. DB (SQLite backup API)
    db_copied = _backup_database()
    results["db"] = "atualizado" if db_copied else "sem mudanças"

    # 2. Data (config.json e outros — exceto DB)
    d_copied, d_skipped = _incremental_copy_folder(DATA_DIR, BACKUP_DATA_DIR, "data")
    results["data_copied"] = d_copied
    results["data_skipped"] = d_skipped

    # 3. Áudio (incremental — só copia novos/modificados)
    a_copied, a_skipped = _incremental_copy_folder(AUDIO_DIR, BACKUP_AUDIO_DIR, "audio")
    results["audio_copied"] = a_copied
    results["audio_skipped"] = a_skipped

    # 4. Logs (incremental)
    l_copied, l_skipped = _incremental_copy_folder(LOGS_DIR, BACKUP_LOGS_DIR, "logs")
    results["logs_copied"] = l_copied
    results["logs_skipped"] = l_skipped

    # 5. Enforce size limit
    _enforce_size_limit()

    elapsed = (datetime.now() - start).total_seconds()
    results["elapsed"] = f"{elapsed:.1f}s"
    results["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    results["backup_size_mb"] = round(_get_backup_size_mb(), 1)

    total_copied = d_copied + a_copied + l_copied + (1 if db_copied else 0)
    total_skipped = d_skipped + a_skipped + l_skipped
    logger.info(
        f"Backup incremental em {results['elapsed']}: "
        f"{total_copied} copiados, {total_skipped} inalterados, "
        f"total {results['backup_size_mb']}MB → {USER_DIR}"
    )

    # Registra timestamp
    prefs = _load_user_prefs()
    prefs["last_backup"] = results["timestamp"]
    _save_user_prefs(prefs)

    return results


def run_backup_async(callback=None):
    """Executa backup em background."""
    def _run():
        try:
            results = run_backup()
            if callback:
                callback(results)
        except Exception as e:
            logger.error(f"Backup falhou: {e}")
            if callback:
                callback({"error": str(e)})

    threading.Thread(target=_run, daemon=True, name="backup").start()


def should_backup_server() -> bool:
    """True se o servidor deve fazer backup (diário — v2.6)."""
    prefs = _load_user_prefs()
    last = prefs.get("last_backup")
    if not last:
        return True
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - last_dt) >= timedelta(days=SERVER_BACKUP_INTERVAL_DAYS)
    except Exception:
        return True


def should_backup_viewer() -> bool:
    """True se o viewer deve fazer backup (máx 1x a cada 12h)."""
    prefs = _load_user_prefs()
    last = prefs.get("last_backup")
    if not last:
        return True
    try:
        last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
        return (datetime.now() - last_dt) >= timedelta(hours=VIEWER_BACKUP_MIN_HOURS)
    except Exception:
        return True


def get_backup_info() -> dict:
    """Retorna info sobre o último backup e tamanho."""
    prefs = _load_user_prefs()
    last = prefs.get("last_backup", "nunca")
    size_mb = _get_backup_size_mb()

    if size_mb > 1024:
        size_str = f"{size_mb/1024:.1f} GB"
    elif size_mb > 1:
        size_str = f"{size_mb:.0f} MB"
    elif size_mb > 0:
        size_str = f"{size_mb*1024:.0f} KB"
    else:
        size_str = "vazio"

    return {
        "last_backup": last,
        "backup_size": size_str,
        "backup_path": str(USER_DIR),
        "limit_mb": MAX_BACKUP_MB,
        "network_paths": get_network_backup_paths(),
    }


# ══════════════════════════════════════════════════════════════════════
# CAMINHOS DE REDE — v2.6
# ══════════════════════════════════════════════════════════════════════

def get_network_backup_paths() -> list[str]:
    """Retorna lista de caminhos de rede configurados para backup."""
    prefs = _load_user_prefs()
    return prefs.get("backup_network_paths", [])


def set_network_backup_paths(paths: list[str]):
    """Salva a lista de caminhos de rede para backup."""
    prefs = _load_user_prefs()
    # Remove entradas vazias e duplicadas, mantém ordem
    clean = []
    seen = set()
    for p in paths:
        p = p.strip()
        if p and p not in seen:
            clean.append(p)
            seen.add(p)
    prefs["backup_network_paths"] = clean
    _save_user_prefs(prefs)
    logger.info(f"Caminhos de rede para backup atualizados: {clean}")


def add_network_backup_path(path: str) -> bool:
    """Adiciona um caminho de rede à lista. Retorna True se adicionado."""
    path = path.strip()
    if not path:
        return False
    paths = get_network_backup_paths()
    if path in paths:
        return False
    paths.append(path)
    set_network_backup_paths(paths)
    return True


def remove_network_backup_path(path: str) -> bool:
    """Remove um caminho de rede da lista. Retorna True se removido."""
    paths = get_network_backup_paths()
    if path in paths:
        paths.remove(path)
        set_network_backup_paths(paths)
        return True
    return False


def _backup_to_network_path(net_path: str) -> dict:
    """
    Executa backup incremental para um caminho de rede.

    Cria subpastas data_backup/, audio_backup/, logs_backup/ dentro do
    caminho de rede e replica o conteúdo local incrementalmente.
    Também copia o DB via SQLite backup API para o destino de rede.

    Retorna dict com resultados.
    """
    results = {"path": net_path}
    net = Path(net_path)

    try:
        net.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        results["error"] = f"Não foi possível acessar {net_path}: {e}"
        logger.warning(results["error"])
        return results

    # 1. DB (SQLite backup API para o destino de rede)
    net_db = net / "data_backup" / "netwatch.db"
    (net / "data_backup").mkdir(parents=True, exist_ok=True)
    try:
        if DB_PATH.exists():
            src_conn = sqlite3.connect(str(DB_PATH), timeout=10)
            dst_conn = sqlite3.connect(str(net_db))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
                src_conn.close()
            results["db"] = "atualizado"
    except Exception as e:
        results["db"] = f"falhou: {e}"
        logger.warning(f"Backup DB para rede falhou ({net_path}): {e}")

    # 2. Data (config.json e outros — exceto DB)
    d_c, d_s = _incremental_copy_folder(DATA_DIR, net / "data_backup", f"net-data({net_path})")
    results["data_copied"] = d_c
    results["data_skipped"] = d_s

    # 3. Áudio
    a_c, a_s = _incremental_copy_folder(AUDIO_DIR, net / "audio_backup", f"net-audio({net_path})")
    results["audio_copied"] = a_c
    results["audio_skipped"] = a_s

    # 4. Logs
    l_c, l_s = _incremental_copy_folder(LOGS_DIR, net / "logs_backup", f"net-logs({net_path})")
    results["logs_copied"] = l_c
    results["logs_skipped"] = l_s

    total_copied = d_c + a_c + l_c + (1 if results.get("db") == "atualizado" else 0)
    logger.info(f"Backup de rede em {net_path}: {total_copied} copiados")
    return results


def run_backup_with_network() -> dict:
    """
    Executa backup local + todos os caminhos de rede configurados.
    Retorna dict com resultados locais e de rede.
    """
    # Backup local normal
    local_results = run_backup()

    # Backup para cada caminho de rede
    net_paths = get_network_backup_paths()
    net_results = []
    for np in net_paths:
        try:
            nr = _backup_to_network_path(np)
            net_results.append(nr)
        except Exception as e:
            net_results.append({"path": np, "error": str(e)})
            logger.error(f"Backup de rede falhou para {np}: {e}")

    local_results["network_results"] = net_results
    return local_results


def run_backup_with_network_async(callback=None):
    """Executa backup local + rede em background."""
    def _run():
        try:
            results = run_backup_with_network()
            if callback:
                callback(results)
        except Exception as e:
            logger.error(f"Backup completo falhou: {e}")
            if callback:
                callback({"error": str(e)})

    threading.Thread(target=_run, daemon=True, name="backup-full").start()