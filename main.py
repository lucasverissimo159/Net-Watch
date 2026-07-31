"""
NetWatch Pro v2.4 — Sistema de Monitoramento de Rede
Ponto de entrada principal.

Criado por Lucas Veríssimo
Arquitetura MVC com CustomTkinter

ALTERAÇÕES v2.4:
  - Ícone da janela/taskbar.
  - Auto-geração de áudios pendentes no startup.
  - Aviso de credenciais SSH vazias.
  - Backup automático (viewer: ao abrir / servidor: a cada 15 dias).

Uso:
    python main.py

Compilar para .exe:
    pyinstaller netwatch.spec
"""
import sys
import os

# Garante que o diretório do script está no path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import resource_path, get_ssh_credentials, APP_VERSION
from models.database import Database
from controllers.monitor_controller import MonitorController
from controllers.audio_controller import AudioController
from controllers.ssh_controller import SSHController
from views.main_view import MainView
from utils.logger import setup_logger

logger = setup_logger("main")


def _load_icon(app):
    """Carrega o ícone da janela (funciona em dev e no .exe)."""
    try:
        ico = resource_path("resources/icons/netwatch.ico")
        if ico.exists():
            app.iconbitmap(str(ico))
            return
    except Exception:
        pass
    try:
        import tkinter as tk
        png = resource_path("resources/icons/netwatch_32.png")
        if png.exists():
            icon = tk.PhotoImage(file=str(png))
            app.iconphoto(True, icon)
    except Exception:
        pass


def _auto_generate_missing_audios(monitor, audio):
    """Gera áudios TTS para hosts sem arquivos de áudio (background)."""
    import threading

    def _gen():
        hosts = monitor.get_all_hosts()
        for host in hosts:
            label = host.display_name
            if label:
                audio.generate_host_alerts(label)

    threading.Thread(target=_gen, daemon=True, name="audio-autogen").start()


def _run_backup_if_needed(monitor):
    """
    Executa backup conforme o modo:
      • Viewer → backup ao abrir (máx 1x a cada 12h)
      • Server → backup a cada 15 dias
    Roda em background — não bloqueia.
    """
    from utils.backup import (
        run_backup_async, should_backup_server, should_backup_viewer,
    )

    def _on_done(results):
        if "error" in results:
            logger.warning(f"Backup falhou: {results['error']}")
        else:
            logger.info(
                f"Backup OK: {results.get('data',0)} data, "
                f"{results.get('audio',0)} audio, "
                f"{results.get('logs',0)} logs em {results.get('elapsed','?')}"
            )

    if monitor.is_viewer:
        if should_backup_viewer():
            logger.info("Viewer: iniciando backup automático...")
            run_backup_async(callback=_on_done)
    else:
        if should_backup_server():
            logger.info("Servidor: iniciando backup periódico (15 dias)...")
            run_backup_async(callback=_on_done)


def main():
    logger.info("=" * 60)
    logger.info(f"NetWatch Pro v{APP_VERSION} — Iniciando aplicação")
    logger.info("=" * 60)

    # ── Inicializa camada de dados ────────────────────────────────────
    db = Database()
    logger.info("Banco de dados inicializado")

    # ── Inicializa controladores ──────────────────────────────────────
    audio = AudioController()
    ssh_ctrl = SSHController()
    monitor = MonitorController(db, audio)
    logger.info(f"Controladores inicializados — {len(monitor.hosts)} hosts carregados")

    # ── Auto-gera áudios pendentes (background) ──────────────────────
    _auto_generate_missing_audios(monitor, audio)

    # ── Aviso de credenciais SSH ──────────────────────────────────────
    user, pwd = get_ssh_credentials()
    if not user:
        logger.warning(
            "Credenciais SSH não configuradas! "
            "Configure em Configurações > Credenciais SSH Padrão."
        )

    # ── Inicializa interface ──────────────────────────────────────────
    app = MainView(
        controller=monitor,
        db=db,
        ssh_controller=ssh_ctrl,
    )
    _load_icon(app)
    logger.info("Interface inicializada — pronto para uso")

    # ── Backup automático (após a UI definir o modo server/viewer) ────
    # Agenda para rodar 3s após o startup (dá tempo do auto_start definir o modo)
    app.after(3000, lambda: _run_backup_if_needed(monitor))

    # ── Loop principal ────────────────────────────────────────────────
    try:
        app.mainloop()
    except KeyboardInterrupt:
        logger.info("Interrompido pelo usuário")
    except Exception as e:
        logger.critical(f"Erro fatal: {e}", exc_info=True)
    finally:
        # CORREÇÃO v2.10 — libera lock PRIMEIRO (síncrono, < 1ms)
        # Se o .exe morrer durante o cleanup abaixo, o lock já estará limpo.
        try:
            from controllers.monitor_controller import _release_lock
            _release_lock()
        except Exception:
            pass
        try:
            monitor.stop()
        except Exception:
            pass
        try:
            ssh_ctrl.close_all()
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass
        logger.info("Aplicação encerrada")

        # Garante que o processo morre completamente.
        import os
        os._exit(0)


if __name__ == "__main__":
    main()