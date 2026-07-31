"""
Main View v2.4 — navegação instantânea + servidor/visualizador automático.

ARQUITETURA:
  • Ao abrir, verifica se outra instância já está monitorando (lock file).
  • Se sim → modo VISUALIZADOR (lê DB, zero SSH). Badge "VISUALIZADOR" no header.
  • Se não → modo SERVIDOR (monitora via SSH). Badge "SERVIDOR" no header.
  • Botão "Assumir" permite forçar o modo servidor manualmente.
"""
import customtkinter as ctk

from config import COLORS, FONTS, APP_NAME, APP_VERSION, APP_AUTHOR
from views.widgets import SidebarButton, AlertBadge
from views.dashboard_view import DashboardView
from views.host_detail_view import HostDetailView
from views.ssh_view import SSHView
from views.logs_view import LogsView
from views.settings_view import SettingsView
from views.reliability_view import ReliabilityView
from utils.logger import setup_logger

logger = setup_logger("main_view")


class MainView(ctk.CTk):
    def __init__(self, controller=None, db=None, ssh_controller=None):
        super().__init__()
        self.controller = controller
        self.db = db
        self.ssh_ctrl = ssh_controller

        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.geometry("1400x820")
        self.minsize(1100, 650)
        self.configure(fg_color=COLORS["bg_primary"])
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._current_view = None
        self._views: dict[str, ctk.CTkFrame] = {}
        self._nav_buttons: dict[str, SidebarButton] = {}

        self._build_sidebar()
        self._build_views()

        self._show_view("dashboard")

        if self.controller:
            self.controller.set_on_host_updated(self._on_host_updated)
            self.controller.set_on_cycle_complete(self._on_cycle_complete)
            self.controller.set_on_alert(self._on_alert)
            self.controller.set_on_failover(self._on_failover)
            self.controller.set_on_demoted(self._on_demoted)

        self._periodic_update()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.after(0,   self._maximize_window)
        self.after(800, self._ensure_maximized)
        self.after(600, self._auto_start)

    # ── Maximize ──────────────────────────────────────────────────────
    def _maximize_window(self):
        try: self.state("zoomed"); return
        except Exception: pass
        try: self.attributes("-zoomed", True); return
        except Exception: pass
        self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")

    def _ensure_maximized(self):
        try:
            if self.state() in ("zoomed", "iconic"): return
        except Exception: pass
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        if self.winfo_width() < int(sw*0.9) or self.winfo_height() < int(sh*0.9):
            try: self.state("zoomed")
            except Exception: self.geometry(f"{sw}x{sh}+0+0")

    def _auto_start(self):
        """
        Auto-start inteligente:
          • Se nenhuma instância monitora → inicia como SERVIDOR.
          • Se outra instância já monitora → inicia como VISUALIZADOR.
        """
        if not self.controller:
            return
        success, msg = self.controller.start()
        self._update_mode_ui()

    # ── Sidebar ───────────────────────────────────────────────────────
    def _build_sidebar(self):
        self.sidebar = ctk.CTkFrame(self, fg_color=COLORS["sidebar"], width=220, corner_radius=0,
                                     border_width=1, border_color=COLORS["border"])
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_columnconfigure(0, weight=1)

        logo = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        logo.grid(row=0, column=0, sticky="ew", padx=16, pady=(20, 24))
        ctk.CTkLabel(logo, text="◎", font=(FONTS["family"], 28),
                     text_color=COLORS["accent_blue"]).pack(side="left")
        tf = ctk.CTkFrame(logo, fg_color="transparent"); tf.pack(side="left", padx=(8,0))
        ctk.CTkLabel(tf, text=APP_NAME, font=(FONTS["family"], FONTS["size_md"], "bold"),
                     text_color=COLORS["text_primary"]).pack(anchor="w")
        ctk.CTkLabel(tf, text=f"v{APP_VERSION}", font=(FONTS["family_mono"], FONTS["size_xs"]),
                     text_color=COLORS["text_muted"]).pack(anchor="w")

        ctk.CTkFrame(self.sidebar, fg_color=COLORS["border"], height=1).grid(
            row=1, column=0, sticky="ew", padx=16, pady=(0, 8))

        nav = [("dashboard", "Dashboard", "📊"),
               ("reliability", "Confiabilidade", "🛡"),
               ("ssh", "Terminal SSH", "🔐"),
               ("logs", "Logs & Alertas", "📋"),
               ("settings", "Configurações", "⚙")]
        for i, (key, label, icon) in enumerate(nav):
            btn = SidebarButton(self.sidebar, text=label, icon=icon,
                                active=(key == "dashboard"),
                                command=lambda k=key: self._show_view(k))
            btn.grid(row=i+2, column=0, sticky="ew", padx=8, pady=2)
            self._nav_buttons[key] = btn

        self.alert_badge = AlertBadge(self._nav_buttons["logs"], count=0)
        self.alert_badge.place(relx=0.95, rely=0.15, anchor="ne")
        self.sidebar.grid_rowconfigure(len(nav)+2, weight=1)

        # ── Modo badge (SERVIDOR / VISUALIZADOR) ──────────────────────
        self.mode_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        self.mode_frame.grid(row=len(nav)+2, column=0, sticky="sew", padx=12, pady=(0, 4))
        self.mode_frame.grid_columnconfigure(0, weight=1)

        self.mode_badge = ctk.CTkLabel(
            self.mode_frame, text="INICIANDO...",
            font=(FONTS["family_mono"], FONTS["size_xs"], "bold"),
            text_color="#FFFFFF", fg_color=COLORS["text_muted"],
            corner_radius=6, padx=8, pady=3)
        self.mode_badge.grid(row=0, column=0, sticky="ew", pady=(0, 2))

        self.mode_info = ctk.CTkLabel(
            self.mode_frame, text="",
            font=(FONTS["family"], 8),
            text_color=COLORS["text_muted"], wraplength=180)
        self.mode_info.grid(row=1, column=0, sticky="ew")

        # ── Controles ─────────────────────────────────────────────────
        ctrl = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        ctrl.grid(row=len(nav)+3, column=0, sticky="ew", padx=16, pady=(4, 8))

        # Botão Mute
        self.mute_btn = ctk.CTkButton(ctrl, text="🔊  Áudio Ativo",
            font=(FONTS["family"], FONTS["size_sm"]),
            fg_color=COLORS["bg_tertiary"], hover_color=COLORS["bg_elevated"],
            text_color=COLORS["accent_green"], corner_radius=8, height=32,
            command=self._toggle_mute)
        self.mute_btn.pack(fill="x", pady=(0, 3))
        if self.controller and self.controller.audio.muted:
            self.mute_btn.configure(text="🔇  Áudio Mudo", text_color=COLORS["accent_red"])

        self.start_btn = ctk.CTkButton(ctrl, text="▶  Iniciar",
            font=(FONTS["family"], FONTS["size_sm"], "bold"),
            fg_color=COLORS["accent_green"], hover_color="#059669",
            text_color="#FFFFFF", corner_radius=8, height=36, command=self._toggle_monitoring)
        self.start_btn.pack(fill="x", pady=2)

        self.pause_btn = ctk.CTkButton(ctrl, text="⏸  Pausar",
            font=(FONTS["family"], FONTS["size_sm"]),
            fg_color=COLORS["bg_tertiary"], hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"], corner_radius=8, height=32,
            command=self._toggle_pause)
        self.pause_btn.pack(fill="x", pady=2)

        # Botão "Assumir" — força modo servidor
        self.force_btn = ctk.CTkButton(ctrl, text="⚡ Assumir Servidor",
            font=(FONTS["family"], FONTS["size_xs"]),
            fg_color="transparent", hover_color=COLORS["accent_red_dim"],
            border_width=1, border_color=COLORS["border"],
            text_color=COLORS["accent_yellow"], corner_radius=8, height=28,
            command=self._force_server)
        self.force_btn.pack(fill="x", pady=(2, 0))
        self.force_btn.pack_forget()  # Escondido por padrão

        ctk.CTkFrame(self.sidebar, fg_color=COLORS["border"], height=1).grid(
            row=len(nav)+4, column=0, sticky="ew", padx=16, pady=(8, 0))
        ctk.CTkLabel(self.sidebar, text=f"Por {APP_AUTHOR}",
                     font=(FONTS["family"], FONTS["size_xs"]),
                     text_color=COLORS["text_muted"]).grid(row=len(nav)+5, column=0, pady=(4, 12))

    # ── UI de modo ────────────────────────────────────────────────────

    def _update_mode_ui(self):
        """Atualiza badges e botões conforme modo SERVIDOR ou VISUALIZADOR."""
        if not self.controller:
            return

        if self.controller.is_viewer:
            owner = self.controller.monitor_owner or "outra máquina"
            self.mode_badge.configure(
                text="👁  VISUALIZADOR",
                fg_color=COLORS["accent_blue"])
            self.mode_info.configure(
                text=f"Servidor: {owner}")
            self.start_btn.configure(
                text="■  Parar Viewer", fg_color=COLORS["accent_blue"],
                hover_color=COLORS["accent_blue_hover"])
            self.pause_btn.configure(state="disabled")
            self.force_btn.pack(fill="x", pady=(2, 0))  # Mostra "Assumir"
            try:
                self._views["dashboard"].set_monitoring_status(True, False)
                # Indicador visual no dashboard
                self._views["dashboard"].running_indicator.configure(
                    text=f"👁 VIEWER — {owner}",
                    text_color=COLORS["accent_blue"])
            except Exception:
                pass
        else:
            self.mode_badge.configure(
                text="🖥  SERVIDOR",
                fg_color=COLORS["accent_green"])
            self.mode_info.configure(text="Esta máquina monitora")
            self.start_btn.configure(
                text="■  Parar", fg_color=COLORS["accent_red"],
                hover_color="#DC2626")
            self.pause_btn.configure(state="normal")
            self.force_btn.pack_forget()  # Esconde "Assumir"
            try:
                self._views["dashboard"].set_monitoring_status(True)
            except Exception:
                pass

    # ── Views ─────────────────────────────────────────────────────────
    def _build_views(self):
        self.content_frame = ctk.CTkFrame(self, fg_color=COLORS["bg_primary"], corner_radius=0)
        self.content_frame.grid(row=0, column=1, sticky="nsew")
        self.content_frame.grid_columnconfigure(0, weight=1)
        self.content_frame.grid_rowconfigure(0, weight=1)

        self._views["dashboard"] = DashboardView(
            self.content_frame, controller=self.controller,
            on_host_click=self._on_host_click)
        self._views["host_detail"] = HostDetailView(
            self.content_frame, controller=self.controller,
            on_back=lambda: self._show_view("dashboard"))
        self._views["reliability"] = ReliabilityView(
            self.content_frame, controller=self.controller, db=self.db)
        self._views["ssh"] = SSHView(
            self.content_frame, ssh_controller=self.ssh_ctrl,
            monitor_controller=self.controller)
        self._views["logs"] = LogsView(
            self.content_frame, db=self.db, controller=self.controller)
        self._views["settings"] = SettingsView(
            self.content_frame, controller=self.controller)

        for name, view in self._views.items():
            view.grid(row=0, column=0, sticky="nsew")
            view.grid_remove()

        self._views["dashboard"].refresh_hosts()

    def _show_view(self, name):
        if self._current_view and self._current_view in self._views:
            self._views[self._current_view].grid_remove()
        if name in self._views:
            self._views[name].grid()
            self._current_view = name
            for key, btn in self._nav_buttons.items():
                btn.set_active(key == name)
            try: self.update_idletasks()
            except Exception: pass
            if name == "settings":
                try: self._views["settings"]._refresh_host_list()
                except Exception: pass
            if name == "reliability":
                try: self._views["reliability"].refresh()
                except Exception: pass
            # CORREÇÃO v2.6: ao voltar pro dashboard, reconstrói a grade
            # para que hosts adicionados/removidos apareçam imediatamente.
            if name == "dashboard":
                try: self._views["dashboard"].refresh_hosts()
                except Exception: pass

    def _on_host_click(self, host_data):
        hid = host_data.get("id")
        if hid:
            self._views["host_detail"].load_host(hid)
            self._views["dashboard"].grid_remove()
            self._views["host_detail"].grid()
            self._current_view = "host_detail"
            try: self.update_idletasks()
            except Exception: pass

    # ── Controles ─────────────────────────────────────────────────────
    def _toggle_monitoring(self):
        if not self.controller: return
        if self.controller.is_running:
            self.controller.stop()
            self.start_btn.configure(text="▶  Iniciar", fg_color=COLORS["accent_green"], hover_color="#059669")
            self.mode_badge.configure(text="○  PARADO", fg_color=COLORS["text_muted"])
            self.mode_info.configure(text="")
            self.pause_btn.configure(text="⏸  Pausar", state="disabled")  # desabilitado — não há loop rodando
            self.force_btn.pack_forget()
            self._views["dashboard"].set_monitoring_status(False)
        else:
            success, msg = self.controller.start()
            self._update_mode_ui()

    def _toggle_pause(self):
        if not self.controller or not self.controller.is_running: return
        if self.controller.is_viewer: return  # viewer não pausa
        if self.controller.is_paused:
            self.controller.resume()
            self.pause_btn.configure(text="⏸  Pausar")
            self._views["dashboard"].set_monitoring_status(True, False)
        else:
            self.controller.pause()
            self.pause_btn.configure(text="▶  Retomar")
            self._views["dashboard"].set_monitoring_status(True, True)

    def _toggle_mute(self):
        if not self.controller: return
        is_muted = self.controller.audio.toggle_mute()
        if is_muted:
            self.mute_btn.configure(text="🔇  Áudio Mudo", text_color=COLORS["accent_red"])
        else:
            self.mute_btn.configure(text="🔊  Áudio Ativo", text_color=COLORS["accent_green"])

    def _force_server(self):
        """Força esta instância a assumir o monitoramento."""
        if not self.controller: return
        # Reconecta DB caso esteja vindo de viewer (evita "file is not a database")
        if self.db and self.controller.is_viewer:
            self.db.reconnect()
        success, msg = self.controller.force_start_as_server()
        self._update_mode_ui()

    # ── Callbacks ─────────────────────────────────────────────────────
    def _on_host_updated(self, host):
        try: self.after(0, lambda h=host: self._safe_host_update(h))
        except Exception: pass

    def _safe_host_update(self, host):
        try:
            if self._current_view == "dashboard":
                self._views["dashboard"].update_host_card(host)
            elif self._current_view == "host_detail":
                self._views["host_detail"].update_live(host)
        except Exception: pass

    def _on_cycle_complete(self, stats):
        try: self.after(0, lambda s=stats: self._safe_cycle_update(s))
        except Exception: pass

    def _safe_cycle_update(self, stats):
        try:
            if self.controller:
                summary = self.controller.get_dashboard_summary()
                self._views["dashboard"].update_summary(summary)

                # CORREÇÃO v2.6: detecta mudança no número de hosts
                # (add/remove pelo viewer ou settings) e reconstrói a grade
                # do dashboard automaticamente, sem precisar reabrir o app.
                current_total = summary.get("total", 0)
                if not hasattr(self, "_last_host_count"):
                    self._last_host_count = current_total
                if current_total != self._last_host_count:
                    self._last_host_count = current_total
                    try:
                        self._views["dashboard"].refresh_hosts()
                    except Exception:
                        pass
                    # Também atualiza Configurações se a aba estiver visível —
                    # caso contrário o usuário precisa sair e voltar para ver o novo host.
                    if self._current_view == "settings":
                        try:
                            self._views["settings"]._refresh_host_list()
                        except Exception:
                            pass

            self._views["logs"].on_cycle_complete()
            # Atualiza badge a cada ciclo — necessário no viewer, pois
            # _on_alert só dispara no servidor
            if self.controller:
                self.alert_badge.set_count(len(self.controller.get_active_alerts()))
            elif self.db:
                self.alert_badge.set_count(len(self.db.get_active_alerts()))
        except Exception: pass

    def _on_alert(self, host, alert_type, message):
        try: self.after(0, self._safe_alert_update)
        except Exception: pass

    def _on_failover(self):
        """
        CORREÇÃO v2.10 — failover automático viewer → servidor.

        Chamado pelo _viewer_loop quando o servidor morre (lock stale por
        3 ciclos). Promove esta instância de viewer para servidor.

        Usa self.after() para executar na UI thread (thread-safe com Tkinter).
        """
        try:
            self.after(0, self._safe_failover)
        except Exception:
            pass

    def _safe_failover(self):
        """Executa a promoção de viewer → servidor na UI thread."""
        if not self.controller:
            return
        try:
            logger.info("FAILOVER: promovendo de viewer para servidor...")
            # CORREÇÃO v2.10 — reconecta o DB antes de virar servidor.
            # O viewer nunca toca o SQLite; ao virar servidor, a thread nova
            # do _monitor_loop abre conexão SQLite via SMB com WAL que falha
            # com "file is not a database". O reconnect() + fallback DELETE
            # mode resolve.
            if self.db:
                self.db.reconnect()
            success, msg = self.controller.start()
            self._update_mode_ui()
            if success:
                logger.info(f"FAILOVER: agora é SERVIDOR — {msg}")
            else:
                logger.warning(f"FAILOVER: ainda viewer — {msg}")
        except Exception as e:
            logger.error(f"FAILOVER: erro na promoção: {e}")

    def _on_demoted(self):
        """
        Callback de demoção: servidor → viewer.

        Chamado pelo _monitor_loop quando outra máquina clicou "Assumir
        Servidor" e sobrescreveu o lock file. Esta instância precisa
        parar de monitorar e virar viewer.

        Usa self.after() para executar na UI thread (thread-safe com Tkinter).
        """
        try:
            self.after(0, self._safe_demoted)
        except Exception:
            pass

    def _safe_demoted(self):
        """Executa a demoção de servidor → viewer na UI thread."""
        if not self.controller:
            return
        try:
            logger.info("DEMOÇÃO: servidor perdeu o lock — reiniciando como viewer...")
            success, msg = self.controller.start()
            self._update_mode_ui()
            if not success:
                logger.info(f"DEMOÇÃO: agora é VIEWER — {msg}")
            else:
                logger.warning(f"DEMOÇÃO: inesperadamente voltou a ser servidor — {msg}")
        except Exception as e:
            logger.error(f"DEMOÇÃO: erro: {e}")

    def _safe_alert_update(self):
        try:
            if self.controller:
                self.alert_badge.set_count(len(self.controller.get_active_alerts()))
            elif self.db:
                self.alert_badge.set_count(len(self.db.get_active_alerts()))
        except Exception: pass

    def _periodic_update(self):
        try:
            if self.controller and self.controller.is_running:
                # CORREÇÃO v2.8 — watchdog: detecta thread de monitoramento morta
                # e reinicia automaticamente. Sem isto, se a thread morrer por
                # qualquer razão, o monitoramento para sem aviso e o usuário
                # precisa parar/iniciar manualmente.
                if (self.controller._thread is not None
                        and not self.controller._thread.is_alive()):
                    logger.warning(
                        "WATCHDOG: thread de monitoramento detectada como morta — "
                        "reiniciando automaticamente"
                    )
                    try:
                        self.controller.stop()
                        success, msg = self.controller.start()
                        self._update_mode_ui()
                        logger.info(f"WATCHDOG: reinício {'OK' if success else 'como viewer'} — {msg}")
                    except Exception as e:
                        logger.error(f"WATCHDOG: falha ao reiniciar: {e}")

                summary = self.controller.get_dashboard_summary()
                if self._current_view == "dashboard":
                    self._views["dashboard"].update_summary(summary)
                if self.controller:
                    self.alert_badge.set_count(len(self.controller.get_active_alerts()))
                elif self.db:
                    self.alert_badge.set_count(len(self.db.get_active_alerts()))

                # Atualiza info do owner no viewer mode
                if self.controller.is_viewer:
                    owner = self.controller.monitor_owner
                    try:
                        self.mode_info.configure(text=f"Servidor: {owner}")
                    except Exception: pass
        except Exception: pass
        finally:
            try: self.after(15000, self._periodic_update)
            except Exception: pass

    def _on_close(self):
        """
        Encerramento completo da aplicação ao clicar no X.

        CORREÇÃO v2.10 — encerramento total + liberação do lock:
          1. Libera o lock file IMEDIATAMENTE (síncrono, < 1ms) —
             sem isto, outras máquinas veem o lock como ativo e ficam
             como viewer até o timeout de 120s.
          2. Destrói a janela Tkinter — o usuário vê a app fechar.
          3. Agenda force-exit com 2s de timeout.
          4. Faz cleanup em background.
        """
        import os, sys, time, threading

        # 1. LIBERA O LOCK FILE IMEDIATAMENTE (síncrono, não-bloqueante)
        #    Isso é o mais crítico: se o .exe morrer sem liberar o lock,
        #    todas as outras máquinas ficam como viewer por até 120s.
        if self.controller and not self.controller.is_viewer:
            try:
                from controllers.monitor_controller import _release_lock
                _release_lock()
            except Exception:
                pass

        # 2. Sinaliza parada (não-bloqueante: só seta flags)
        if self.controller:
            self.controller._running = False
            try:
                self.controller.audio._stop_flag.set()
            except Exception:
                pass

        # 3. Destroi a janela Tkinter IMEDIATAMENTE
        try:
            self.destroy()
        except Exception:
            pass

        # 4. Agenda force-exit — garante morte do processo
        def _force_exit():
            time.sleep(2)
            os._exit(0)
        threading.Thread(target=_force_exit, daemon=True, name="force-exit").start()

        # 5. Cleanup em background (tem 2s antes do force-exit)
        def _cleanup():
            if self.controller:
                try:
                    self.controller.stop(shutdown_audio=True)
                except Exception:
                    pass
            if self.ssh_ctrl:
                try:
                    self.ssh_ctrl.close_all()
                except Exception:
                    pass
            if self.db:
                try:
                    self.db.close()
                except Exception:
                    pass
            os._exit(0)

        threading.Thread(target=_cleanup, daemon=True, name="shutdown-cleanup").start()