"""
Logs View — visualizador de logs, alertas e monitor ao vivo.

CORREÇÕES APLICADAS:
  1. Scroll suave: bind_mousewheel_scroll agora é chamado com rebind após
     popular a tabela do monitor, evitando que novas linhas fiquem sem binding.
  2. Performance de logs: carregamento é dividido em lotes (chunks) via after()
     para não travar a UI ao inserir 2000 linhas de uma vez.
  3. Monitor ao Vivo: adicionado botão "Pingar SSH" por linha — executa
     run_ssh_ping() no controller e exibe resultado em popup. Se SSH falhar,
     o controller automaticamente faz fallback para ping local.
  4. _schedule_refresh: protegido com try/except para não quebrar o loop de
     agendamento caso algum widget seja destruído durante uma troca de aba.
"""
import threading
from datetime import datetime

import customtkinter as ctk

from config import COLORS, FONTS, LOGS_DIR
from utils.logger import get_log_files, read_recent_logs
from views.widgets import bind_mousewheel_scroll, rebind_mousewheel_scroll


# Quantidade máxima de linhas exibidas de uma vez no textbox de logs.
# Reduzir esse número melhora muito a performance de render e o scroll.
MAX_LOG_LINES_DISPLAY = 800
# Tamanho do lote por iteração do after() durante inserção de logs
LOG_CHUNK_SIZE = 100


class LogsView(ctk.CTkFrame):
    """Logs, alertas e monitor ao vivo."""

    def __init__(self, master, db=None, controller=None, **kwargs):
        super().__init__(master, fg_color=COLORS["bg_primary"], **kwargs)
        self.db = db
        self._controller = controller
        self._auto_refresh = True
        self._current_tab = "Monitor"

        # Controle de inserção em lote (logs)
        self._pending_lines: list[str] = []
        self._chunk_job = None

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._build_content()
        self._schedule_refresh()
        self._schedule_monitor_refresh()

    def set_controller(self, controller):
        self._controller = controller

    # ── Header ────────────────────────────────────────────────────────

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 0))
        header.grid_columnconfigure(1, weight=1)

        title_frame = ctk.CTkFrame(header, fg_color="transparent")
        title_frame.grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            title_frame, text="Logs & Alertas",
            font=(FONTS["family"], FONTS["size_xl"], "bold"),
            text_color=COLORS["text_primary"],
        ).pack(side="left")

        self.count_badge = ctk.CTkLabel(
            title_frame, text="",
            font=(FONTS["family"], FONTS["size_xs"], "bold"),
            text_color="#FFFFFF",
            fg_color=COLORS["accent_red"],
            corner_radius=10, padx=8, pady=2,
        )
        self.count_badge.pack(side="left", padx=(8, 0))

        controls = ctk.CTkFrame(header, fg_color="transparent")
        controls.grid(row=0, column=1, sticky="e")

        self.auto_refresh_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(
            controls, text="Auto-refresh",
            variable=self.auto_refresh_var,
            font=(FONTS["family"], FONTS["size_xs"]),
            text_color=COLORS["text_secondary"],
            progress_color=COLORS["accent_blue"],
            button_color=COLORS["accent_blue"],
            button_hover_color=COLORS["accent_blue_hover"],
            onvalue=True, offvalue=False,
            command=self._toggle_auto_refresh,
        ).pack(side="left", padx=8)

        ctk.CTkButton(
            controls, text="↻  Atualizar", width=100,
            font=(FONTS["family"], FONTS["size_xs"], "bold"),
            fg_color=COLORS["bg_secondary"],
            hover_color=COLORS["bg_tertiary"],
            border_width=1, border_color=COLORS["border"],
            text_color=COLORS["text_primary"],
            corner_radius=8, height=32,
            command=self._refresh,
        ).pack(side="left", padx=4)

        ctk.CTkButton(
            controls, text="🗑  Limpar", width=90,
            font=(FONTS["family"], FONTS["size_xs"]),
            fg_color=COLORS["bg_secondary"],
            hover_color=COLORS["bg_tertiary"],
            border_width=1, border_color=COLORS["border"],
            text_color=COLORS["text_muted"],
            corner_radius=8, height=32,
            command=self._clear_view,
        ).pack(side="left", padx=4)

        # v2.4: Zoom
        self._zoom = 100
        zoom_frame = ctk.CTkFrame(controls, fg_color="transparent")
        zoom_frame.pack(side="left", padx=(8, 0))
        ctk.CTkButton(
            zoom_frame, text="−", width=26, height=26,
            font=(FONTS["family"], 13, "bold"),
            fg_color=COLORS["bg_tertiary"], hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"], corner_radius=4,
            command=self._zoom_out,
        ).pack(side="left", padx=1)
        self._zoom_label = ctk.CTkLabel(
            zoom_frame, text="100%", width=42,
            font=(FONTS["family_mono"], 9),
            text_color=COLORS["text_muted"])
        self._zoom_label.pack(side="left", padx=1)
        ctk.CTkButton(
            zoom_frame, text="+", width=26, height=26,
            font=(FONTS["family"], 13, "bold"),
            fg_color=COLORS["bg_tertiary"], hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"], corner_radius=4,
            command=self._zoom_in,
        ).pack(side="left", padx=1)

    # ── Conteúdo ──────────────────────────────────────────────────────

    def _build_content(self):
        outer = ctk.CTkFrame(self, fg_color="transparent")
        outer.grid(row=1, column=0, sticky="nsew", padx=24, pady=(12, 16))
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(1, weight=1)

        # Tab bar
        tab_bar = ctk.CTkFrame(
            outer, fg_color=COLORS["bg_secondary"],
            corner_radius=10, border_width=1, border_color=COLORS["border"],
        )
        tab_bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        self._tab_buttons: dict[str, ctk.CTkButton] = {}
        tabs = [
            ("Monitor",        "📡  Monitor ao Vivo"),
            ("Logs",           "📄  Logs do Sistema"),
            ("Alertas Ativos", "🔴  Alertas Ativos"),
            ("Histórico",      "📋  Histórico"),
        ]
        for key, label in tabs:
            btn = ctk.CTkButton(
                tab_bar, text=label,
                font=(FONTS["family"], FONTS["size_sm"]),
                fg_color=COLORS["accent_blue"] if key == "Monitor" else "transparent",
                hover_color=COLORS["bg_tertiary"],
                text_color="#FFFFFF" if key == "Monitor" else COLORS["text_secondary"],
                corner_radius=8, height=34,
                command=lambda k=key: self._switch_tab(k),
            )
            btn.pack(side="left", padx=6, pady=6)
            self._tab_buttons[key] = btn

        # Painel principal
        main_panel = ctk.CTkFrame(
            outer, fg_color=COLORS["bg_secondary"],
            corner_radius=12, border_width=1, border_color=COLORS["border"],
        )
        main_panel.grid(row=1, column=0, sticky="nsew")
        main_panel.grid_columnconfigure(0, weight=1)
        main_panel.grid_rowconfigure(1, weight=1)

        # Barra de status
        self.info_bar = ctk.CTkFrame(
            main_panel, fg_color=COLORS["bg_tertiary"], corner_radius=0
        )
        self.info_bar.grid(row=0, column=0, sticky="ew")
        self.info_bar.grid_columnconfigure(0, weight=1)

        self.info_label = ctk.CTkLabel(
            self.info_bar, text="",
            font=(FONTS["family_mono"], FONTS["size_xs"]),
            text_color=COLORS["text_muted"], anchor="w",
        )
        self.info_label.grid(row=0, column=0, sticky="w", padx=16, pady=6)

        self.last_update_label = ctk.CTkLabel(
            self.info_bar, text="",
            font=(FONTS["family_mono"], FONTS["size_xs"]),
            text_color=COLORS["text_muted"], anchor="e",
        )
        self.last_update_label.grid(row=0, column=1, sticky="e", padx=16, pady=6)

        # ── Textbox (Logs / Alertas / Histórico) ─────────────────────
        self.log_text = ctk.CTkTextbox(
            main_panel,
            font=(FONTS["family_mono"], FONTS["size_xs"]),
            fg_color=COLORS["bg_primary"],
            text_color=COLORS["text_secondary"],
            border_width=0, corner_radius=0,
            wrap="none", activate_scrollbars=True,
        )
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=2, pady=(0, 2))
        # CORREÇÃO: target explícito para o textbox interno — scroll mais fluido
        bind_mousewheel_scroll(self.log_text, self.log_text._textbox)

        tb = self.log_text._textbox
        tb.tag_configure("error",   foreground=COLORS["accent_red"])
        tb.tag_configure("warn",    foreground=COLORS["accent_yellow"])
        tb.tag_configure("success", foreground=COLORS["accent_green"])
        tb.tag_configure("info",    foreground=COLORS["accent_cyan"])
        tb.tag_configure("muted",   foreground=COLORS["text_muted"])
        tb.tag_configure("bold",    font=(FONTS["family_mono"], FONTS["size_xs"], "bold"))
        tb.tag_configure("heading",
            foreground=COLORS["accent_blue"],
            font=(FONTS["family"], FONTS["size_sm"], "bold"),
        )

        # ── Frame do Monitor ao Vivo ──────────────────────────────────
        self._monitor_outer = ctk.CTkFrame(
            main_panel, fg_color=COLORS["bg_primary"], corner_radius=0,
        )
        self._monitor_outer.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        self._monitor_outer.grid_columnconfigure(0, weight=1)
        self._monitor_outer.grid_rowconfigure(2, weight=1)   # scroll agora na row 2

        # ── Barra de filtro do Monitor ao Vivo ───────────────────────
        self._monitor_search_var = ctk.StringVar(value="")
        monitor_filter_bar = ctk.CTkFrame(
            self._monitor_outer, fg_color=COLORS["bg_secondary"], corner_radius=0,
        )
        monitor_filter_bar.grid(row=0, column=0, sticky="ew", padx=0, pady=0)
        monitor_filter_bar.grid_columnconfigure(0, weight=1)

        self._monitor_search_entry = ctk.CTkEntry(
            monitor_filter_bar,
            textvariable=self._monitor_search_var,
            placeholder_text="🔍  Filtrar por host, IP ou loja...",
            font=(FONTS["family"], FONTS["size_sm"]),
            fg_color=COLORS["bg_tertiary"],
            border_color=COLORS["border"],
            text_color=COLORS["text_primary"],
            placeholder_text_color=COLORS["text_muted"],
            corner_radius=0, height=32,
        )
        self._monitor_search_entry.grid(row=0, column=0, sticky="ew")
        self._monitor_search_entry.bind("<KeyRelease>", lambda e: self._on_monitor_search())

        ctk.CTkButton(
            monitor_filter_bar, text="✕", width=32, height=32,
            font=(FONTS["family"], FONTS["size_sm"]),
            fg_color="transparent", hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_muted"], corner_radius=0,
            command=self._clear_monitor_search,
        ).grid(row=0, column=1)

        # Triple Ping: HOST(verde) + WAN(azul) + Google(laranja) + Deltas
        _MON_COLS = [
            ("●",       18,  "center"),
            ("Host",    130, "w"),
            ("IP",      108, "w"),
            ("H.Lat",   52,  "center"),
            ("H.Prd",   44,  "center"),
            ("H.Dsp",   42,  "center"),
            ("W.Lat",   52,  "center"),
            ("W.Prd",   44,  "center"),
            ("W.Dsp",   42,  "center"),
            ("G.Lat",   52,  "center"),
            ("G.Prd",   44,  "center"),
            ("G.Dsp",   42,  "center"),
            ("ΔWAN",    48,  "center"),
            ("ΔGgl",    48,  "center"),
            ("Últ",     60,  "center"),
            ("Org",     36,  "center"),
            ("Ação",    50,  "center"),
        ]
        self._mon_col_defs = _MON_COLS

        self._monitor_header = ctk.CTkFrame(
            self._monitor_outer, fg_color=COLORS["bg_tertiary"], corner_radius=0,
        )
        self._monitor_header.grid(row=1, column=0, sticky="ew", padx=0, pady=0)
        # Cores por grupo: HOST=verde, WAN=azul, Google=laranja, Delta=roxo
        _COL_COLORS = {3:"#10B981",4:"#10B981",5:"#10B981",
                       6:"#3B82F6",7:"#3B82F6",8:"#3B82F6",
                       9:"#F97316",10:"#F97316",11:"#F97316",
                       12:"#8B5CF6",13:"#8B5CF6"}
        self._mon_col_colors = _COL_COLORS
        for ci, (text, w, anchor) in enumerate(_MON_COLS):
            ctk.CTkLabel(
                self._monitor_header, text=text, width=w,
                font=(FONTS["family"], FONTS["size_xs"], "bold"),
                text_color=_COL_COLORS.get(ci, COLORS["text_secondary"]), anchor=anchor,
            ).grid(row=0, column=ci,
                   padx=(12 if ci == 0 else 1, 1), pady=5, sticky="w")

        self._monitor_scroll = ctk.CTkScrollableFrame(
            self._monitor_outer,
            fg_color=COLORS["bg_primary"],
            scrollbar_fg_color=COLORS["bg_secondary"],
            corner_radius=0,
        )
        self._monitor_scroll.grid(row=2, column=0, sticky="nsew", padx=0, pady=0)
        self._monitor_scroll.grid_columnconfigure(0, weight=1)
        bind_mousewheel_scroll(self._monitor_scroll)

        self._monitor_rows: dict[int, list] = {}   # host_id → [labels..., ping_btn]

        # Começa mostrando o Monitor
        self._switch_tab("Monitor")

    # ── Navegação de tabs ─────────────────────────────────────────────

    def _switch_tab(self, tab: str):
        self._current_tab = tab
        for key, btn in self._tab_buttons.items():
            if key == tab:
                btn.configure(fg_color=COLORS["accent_blue"], text_color="#FFFFFF")
            else:
                btn.configure(fg_color="transparent", text_color=COLORS["text_secondary"])

        if tab == "Monitor":
            self.log_text.grid_remove()
            self._monitor_outer.grid()
            self._update_monitor_tab()
        else:
            self._monitor_outer.grid_remove()
            self.log_text.grid()
            self._refresh()

    def _toggle_auto_refresh(self):
        self._auto_refresh = self.auto_refresh_var.get()

    # ── Zoom ──────────────────────────────────────────────────────────

    def _zoom_in(self):
        if self._zoom < 130:
            self._zoom += 5
            self._apply_zoom()

    def _zoom_out(self):
        if self._zoom > 100:
            self._zoom -= 5
            self._apply_zoom()

    def _apply_zoom(self):
        self._zoom_label.configure(text=f"{self._zoom}%")
        base = FONTS["size_xs"]
        new_size = int(base * self._zoom / 100)
        # Logs textbox — aplica no wrapper E no widget interno
        try:
            self.log_text.configure(font=(FONTS["family_mono"], new_size))
        except Exception:
            pass
        try:
            self.log_text._textbox.configure(font=(FONTS["family_mono"], new_size))
        except Exception:
            pass
        # Monitor ao vivo — reconstrói a tabela inteira (colunas + rows)
        # para manter alinhamento correto
        if self._current_tab == "Monitor":
            self._rebuild_monitor_header()
            self._build_monitor_rows()

    def _get_zoomed_font(self, base_size=None, family=None, bold=False):
        """Retorna tupla de fonte com zoom aplicado."""
        sz = int((base_size or FONTS["size_xs"]) * self._zoom / 100)
        fam = family or FONTS["family_mono"]
        return (fam, sz, "bold") if bold else (fam, sz)

    def _get_zoomed_width(self, base_width):
        """Retorna largura de coluna escalada pelo zoom."""
        return int(base_width * self._zoom / 100)

    def _rebuild_monitor_header(self):
        """Reconstrói o header do monitor com fonte/largura zoomed."""
        try:
            self._monitor_header.destroy()
        except Exception:
            pass

        self._monitor_header = ctk.CTkFrame(
            self._monitor_outer, fg_color=COLORS["bg_tertiary"], corner_radius=0,
        )
        self._monitor_header.grid(row=1, column=0, sticky="ew", padx=0, pady=0)

        for ci, (text, w, anchor) in enumerate(self._mon_col_defs):
            ctk.CTkLabel(
                self._monitor_header, text=text,
                width=self._get_zoomed_width(w),
                font=self._get_zoomed_font(FONTS["size_xs"], FONTS["family"], bold=True),
                text_color=self._mon_col_colors.get(ci, COLORS["text_secondary"]),
                anchor=anchor,
            ).grid(row=0, column=ci,
                   padx=(12 if ci == 0 else 1, 1), pady=5, sticky="w")

    # ── Refresh geral ─────────────────────────────────────────────────

    def _refresh(self):
        # Cancela inserção em lote anterior se houver
        if self._chunk_job is not None:
            try:
                self.after_cancel(self._chunk_job)
            except Exception:
                pass
            self._chunk_job = None
        self._pending_lines = []

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")

        if self._current_tab == "Logs":
            self._load_logs()
        elif self._current_tab == "Alertas Ativos":
            self._load_active_alerts()
        elif self._current_tab == "Histórico":
            self._load_alerts_history()

        now = datetime.now().strftime("%H:%M:%S")
        self.last_update_label.configure(text=f"Atualizado: {now}")

    def _insert(self, text: str, tag: str = ""):
        if tag:
            self.log_text._textbox.insert("end", text, tag)
        else:
            self.log_text.insert("end", text)

    # ── Aba Logs — carregamento em lotes (chunked) ────────────────────

    def _load_logs(self):
        # No modo viewer, exibe os logs do servidor (incluídos no snapshot).
        # No modo servidor, lê os arquivos de log locais normalmente.
        is_viewer = (self._controller is not None and
                     getattr(self._controller, "is_viewer", False))

        if is_viewer:
            self.info_label.configure(
                text="  Logs do Servidor (via snapshot) — atualizados a cada 5 ciclos"
            )
            self.count_badge.configure(text="", fg_color="transparent")
            lines = self._controller.get_server_log_lines(
                max_lines=MAX_LOG_LINES_DISPLAY
            )
        else:
            files = get_log_files(max_days=2)
            self.info_label.configure(
                text=f"  Exibindo últimos 2 dias  ·  {len(files)} arquivo(s)  ·  {LOGS_DIR}"
            )
            self.count_badge.configure(text="", fg_color="transparent")
            lines = read_recent_logs(max_days=2, max_lines=MAX_LOG_LINES_DISPLAY)

        if not lines:
            self._insert("\n   Nenhum log encontrado.\n", "muted")
            return

        self._pending_lines = list(lines)
        self._insert_chunk()

    def _insert_chunk(self):
        """Insere um lote de linhas e agenda o próximo se houver mais."""
        chunk = self._pending_lines[:LOG_CHUNK_SIZE]
        self._pending_lines = self._pending_lines[LOG_CHUNK_SIZE:]

        for line in chunk:
            line = line.rstrip()
            if not line:
                continue
            if "│ ERROR" in line or "│ CRITICAL" in line:
                self._insert(line + "\n", "error")
            elif "│ WARNING" in line:
                self._insert(line + "\n", "warn")
            elif "│ INFO" in line and any(k in line for k in ("ONLINE", "voltou", "on-line")):
                self._insert(line + "\n", "success")
            elif "│ INFO" in line and any(k in line for k in ("OFFLINE", "ficou", "off-line")):
                self._insert(line + "\n", "error")
            elif "│ INFO" in line and any(k in line for k in ("Ciclo", "iniciado", "parado")):
                self._insert(line + "\n", "info")
            else:
                self._insert(line + "\n")

        if self._pending_lines:
            # Agenda próximo lote com after(0) — próximo ciclo do event loop
            self._chunk_job = self.after(0, self._insert_chunk)
        else:
            self._chunk_job = None
            # Vai ao final apenas ao concluir
            self.log_text.see("end")

    # ── Aba Alertas Ativos ────────────────────────────────────────────

    def _load_active_alerts(self):
        # Usa o controller (que lê do snapshot no viewer) em vez do DB direto.
        # Isso evita o erro "file is not a database" no modo visualizador.
        if self._controller:
            alerts = self._controller.get_active_alerts()
        elif self.db:
            alerts = self.db.get_active_alerts()
        else:
            self._insert("\n   Banco de dados não disponível.\n", "warn")
            self.info_label.configure(text="  —")
            return
        count  = len(alerts)
        self.info_label.configure(text=f"  {count} alerta(s) ativo(s)")

        if count > 0:
            self.count_badge.configure(text=str(count), fg_color=COLORS["accent_red"])
        else:
            self.count_badge.configure(text="", fg_color="transparent")

        if not alerts:
            self._insert("\n")
            self._insert("   ✅  Nenhum alerta ativo — tudo operacional.\n", "success")
            return

        self._insert("\n")
        for alert in alerts:
            atype = alert.get("alert_type", "")
            label = alert.get("label") or alert.get("ip", "?")
            ts    = alert.get("timestamp", "")
            msg   = alert.get("message", "")

            icon = {"offline": "🔴", "high_latency": "🟡", "packet_loss": "🟠"}.get(atype, "⚪")
            type_color = {"offline": "error", "high_latency": "warn", "packet_loss": "warn"}.get(atype, "")

            self._insert(f"  {icon}  ")
            self._insert(f"{label}", "bold")
            self._insert(f"   [{ts}]\n", "muted")
            self._insert("      Tipo: ", "muted")
            self._insert(f"{atype.upper().replace('_', ' ')}", type_color)
            self._insert(f"\n      {msg}\n\n", "muted")

    # ── Aba Histórico ─────────────────────────────────────────────────

    def _load_alerts_history(self):
        # Usa o controller (que lê do snapshot no viewer) em vez do DB direto.
        if self._controller:
            alerts = self._controller.get_alerts_history(200)
        elif self.db:
            alerts = self.db.get_alerts_history(200)
        else:
            self._insert("\n   Banco de dados não disponível.\n", "warn")
            self.info_label.configure(text="  —")
            return
        count  = len(alerts)
        self.info_label.configure(text=f"  {count} registro(s) no histórico")
        self.count_badge.configure(text="", fg_color="transparent")

        if not alerts:
            self._insert("\n   Nenhum alerta no histórico.\n", "muted")
            return

        current_date = ""
        self._insert("\n")
        for alert in alerts:
            ts   = alert.get("timestamp", "")
            date = ts[:10] if ts else ""

            if date != current_date:
                current_date = date
                self._insert(f"\n  ── {date} {'─' * 40}\n", "info")

            resolved    = alert.get("resolved", 0)
            label       = alert.get("label") or alert.get("ip", "?")
            atype       = alert.get("alert_type", "")
            resolved_at = alert.get("resolved_at", "")
            msg         = alert.get("message", "")
            time_part   = ts[11:19] if len(ts) > 10 else ts

            icon = "✅" if resolved else "🔴"
            tag  = "success" if resolved else "error"

            self._insert(f"  {icon} ")
            self._insert(f"[{time_part}] ", "muted")
            self._insert(f"{label}", "bold")
            self._insert(f"  ·  {atype.upper().replace('_', ' ')}", tag)
            if resolved and resolved_at:
                self._insert(f"  → resolvido {resolved_at[11:19]}", "success")
            self._insert(f"\n     {msg}\n\n", "muted")

    # ── Aba Monitor ao Vivo ───────────────────────────────────────────

    _STATUS_COLOR = {
        "online":  COLORS["accent_green"],
        "offline": COLORS["accent_red"],
        "unknown": COLORS["text_muted"],
    }

    def _on_monitor_search(self):
        """Reconstrói a tabela aplicando o filtro de texto."""
        self._build_monitor_rows()

    def _clear_monitor_search(self):
        self._monitor_search_var.set("")
        self._build_monitor_rows()

    def _build_monitor_rows(self):
        """
        Constrói os frames e labels da tabela do monitor.
        Aplica filtro de texto se o campo de busca estiver preenchido.
        """
        if not self._controller:
            return

        for w in self._monitor_scroll.winfo_children():
            w.destroy()
        self._monitor_rows.clear()

        search = self._monitor_search_var.get().strip().lower()

        all_hosts = sorted(
            self._controller.get_all_hosts(),
            key=lambda h: (h.group_name, h.display_name),
        )

        # Aplica filtro: nome, IP e label (que pode conter número da loja)
        if search:
            hosts = [
                h for h in all_hosts
                if h.enabled and (
                    search in h.display_name.lower() or
                    search in h.ip.lower() or
                    search in (h.group_name or "").lower()
                )
            ]
        else:
            hosts = [h for h in all_hosts if h.enabled]

        row_colors = [COLORS["bg_primary"], COLORS["bg_secondary"]]
        current_group = None
        cols = self._mon_col_defs
        row_idx = 0

        for host in hosts:
            if host.group_name != current_group:
                current_group = host.group_name
                sep = ctk.CTkFrame(
                    self._monitor_scroll,
                    fg_color=COLORS["bg_tertiary"], corner_radius=0, height=24,
                )
                sep.pack(fill="x", padx=0, pady=0)
                ctk.CTkLabel(
                    sep, text=f"  {current_group}",
                    font=self._get_zoomed_font(FONTS["size_xs"], FONTS["family"], bold=True),
                    text_color=COLORS["accent_blue"], anchor="w",
                ).pack(side="left", padx=12)

            row_bg = row_colors[row_idx % 2]
            row_frame = ctk.CTkFrame(
                self._monitor_scroll, fg_color=row_bg, corner_radius=0,
            )
            row_frame.pack(fill="x", padx=0, pady=0)

            row_labels = []
            for ci, (_, w, anchor) in enumerate(cols[:-1]):
                mono = ci not in (0, 1)
                fam = FONTS["family_mono"] if mono else FONTS["family"]
                lbl = ctk.CTkLabel(
                    row_frame, text="—",
                    width=self._get_zoomed_width(w), anchor=anchor,
                    font=self._get_zoomed_font(FONTS["size_xs"], fam),
                    text_color=COLORS["text_muted"],
                )
                lbl.pack(side="left", padx=(12 if ci == 0 else 1, 1), pady=4)
                row_labels.append(lbl)

            # CORREÇÃO: botão "Pingar SSH" na coluna "Ação"
            # Executa ping ad-hoc via SSH dentro do host (com fallback local)
            ping_btn = ctk.CTkButton(
                row_frame,
                text="Ping",
                width=self._get_zoomed_width(cols[-1][1]),
                height=20,
                font=self._get_zoomed_font(8, FONTS["family"]),
                fg_color=COLORS["bg_tertiary"],
                hover_color=COLORS["accent_blue"],
                border_width=1,
                border_color=COLORS["border"],
                text_color=COLORS["text_secondary"],
                corner_radius=4,
                command=lambda h=host: self._open_ssh_ping_dialog(h),
            )
            ping_btn.pack(side="left", padx=(2, 8), pady=4)
            row_labels.append(ping_btn)   # índice -1 = botão

            self._monitor_rows[host.id] = row_labels
            row_idx += 1

        self._update_monitor_values()

        # CORREÇÃO: rebind imediato após popular a tabela para garantir scroll
        # nos novos widgets (after(100) do bind original pode não ser suficiente)
        rebind_mousewheel_scroll(self._monitor_scroll)

    def _update_monitor_tab(self):
        if not self._controller:
            return
        host_ids = {h.id for h in self._controller.get_all_hosts() if h.enabled}
        if host_ids != set(self._monitor_rows.keys()):
            self._build_monitor_rows()
        else:
            self._update_monitor_values()

    def _update_monitor_values(self):
        """Atualiza textos/cores da tabela — triple ping com deltas."""
        if not self._controller:
            return

        hosts = {h.id: h for h in self._controller.get_all_hosts()}
        total = len([h for h in hosts.values() if h.enabled])
        online = sum(1 for h in hosts.values() if h.is_online)
        offline = sum(1 for h in hosts.values() if h.status == "offline")
        lats = [h.host_ssh_latency for h in hosts.values() if h.is_online and h.host_ssh_has_data]
        avg_lat = f"{sum(lats)/len(lats):.1f} ms" if lats else "—"

        off_part = f"  🔴 {offline} offline" if offline else "  ✅ 0 offline"
        try:
            self.info_label.configure(
                text=f"  {total} hosts  ·  {online} online  ·{off_part}  ·  lat: {avg_lat}")
            self.last_update_label.configure(
                text=f"Atualizado: {datetime.now().strftime('%H:%M:%S')}")
        except Exception:
            pass

        def _lc(v):
            if v <= 0: return COLORS["text_muted"]
            return COLORS["accent_green"] if v < 50 else COLORS["accent_yellow"] if v < 150 else COLORS["accent_red"]
        def _pc(v):
            return COLORS["accent_green"] if v == 0 else COLORS["accent_yellow"] if v <= 10 else COLORS["accent_red"]
        def _dc(d):
            """Cor do delta."""
            return COLORS["accent_green"] if d < 5 else COLORS["accent_yellow"] if d < 20 else COLORS["accent_red"]
        def _ac(v):
            """Cor da disponibilidade."""
            return COLORS["accent_green"] if v > 95 else COLORS["accent_yellow"] if v >= 80 else COLORS["accent_red"]

        for host_id, labels in self._monitor_rows.items():
            host = hosts.get(host_id)
            if host is None:
                continue

            status = host.status
            ping_mode = getattr(host, "last_ping_mode", "PING")
            s_color = self._STATUS_COLOR.get(status, COLORS["text_muted"])

            # HOST (fallback: SSH → local)
            h_lat = host.host_ssh_latency
            h_loss = host.host_ssh_loss
            h_avail = host.host_ssh_avail
            has_h = host.host_ssh_has_data

            # CORREÇÃO v2.6 — H.Dsp bugado quando host cai:
            #   host_ssh_avail usa _avail(ping_history) com até 500 entradas,
            #   então quando o host acaba de cair, a disponibilidade histórica
            #   pode ser 90%+ (amarelo) enquanto H.Prd já mostra 100% (vermelho).
            #   Isso confunde o operador. Solução: se o host está offline E
            #   a perda atual é 100%, mostra 0% em vermelho na coluna H.Dsp.
            if status == "offline" and h_loss >= 100:
                h_avail = 0.0

            # WAN
            w_lat = host.wan_latency
            w_loss = host.wan_loss
            w_avail = host.wan_avail
            has_w = host.wan_has_data
            # CORREÇÃO v2.6: distinguir "WAN não configurado" de "WAN sem dados"
            wan_configured = bool((getattr(host, "wan_ip", "") or "").strip())

            # CORREÇÃO v2.6: mesma lógica para WAN e Google
            if status == "offline" and has_w and w_loss >= 100:
                w_avail = 0.0

            # Google
            g_lat = host.google_latency
            g_loss = host.google_loss
            g_avail = host.google_avail
            has_g = host.google_has_data

            if status == "offline" and has_g and g_loss >= 100:
                g_avail = 0.0

            # Deltas
            d_wan = host.delta_wan
            d_ggl = host.delta_google

            last_ping = "—"
            if host.ping_history:
                ts = host.ping_history[-1].timestamp
                if ts: last_ping = ts.strftime("%H:%M:%S")

            # CORREÇÃO v2.6: WAN "N/C" (Não Configurado) vs "—" (sem dados)
            #   Antes, ambos os casos mostravam "—", e o operador não sabia
            #   se o WAN IP precisava ser configurado ou se o ping falhou.
            #   Agora: "N/C" = precisa configurar WAN IP nas Configurações
            #          "—"  = WAN configurado mas ping falhou neste ciclo
            _nc_color = COLORS.get("accent_purple", "#8B5CF6")
            if not wan_configured:
                wan_lat_cell  = ("N/C", _nc_color)
                wan_loss_cell = ("N/C", _nc_color)
                wan_avail_cell = ("N/C", _nc_color)
                wan_delta_cell = ("N/C", _nc_color)
            elif has_w:
                wan_lat_cell   = (f"{w_lat:.1f}",   _lc(w_lat))
                wan_loss_cell  = (f"{w_loss:.0f}%",  _pc(w_loss))
                wan_avail_cell = (f"{w_avail:.0f}%", _ac(w_avail))
                wan_delta_cell = (f"+{d_wan:.0f}" if d_wan is not None else "—",
                                  _dc(d_wan) if d_wan is not None else COLORS["text_muted"])
            else:
                wan_lat_cell   = ("—", COLORS["text_muted"])
                wan_loss_cell  = ("—", COLORS["text_muted"])
                wan_avail_cell = ("—", COLORS["text_muted"])
                wan_delta_cell = ("—", COLORS["text_muted"])

            # Colunas: ●, Host, IP, H.Lat, H.Prd, H.Dsp, W.Lat, W.Prd, W.Dsp,
            #          G.Lat, G.Prd, G.Dsp, ΔWAN, ΔGgl, Últ, Org
            data_updates = [
                ("●",               s_color),
                (host.display_name, COLORS["text_primary"]),
                (host.ip,           COLORS["text_muted"]),
                # HOST
                (f"{h_lat:.1f}" if has_h else "—",    _lc(h_lat) if has_h else COLORS["text_muted"]),
                (f"{h_loss:.0f}%" if has_h else "—",   _pc(h_loss) if has_h else COLORS["text_muted"]),
                (f"{h_avail:.0f}%" if has_h else "—",  _ac(h_avail) if has_h else COLORS["text_muted"]),
                # WAN (com indicador N/C)
                wan_lat_cell,
                wan_loss_cell,
                wan_avail_cell,
                # Google
                (f"{g_lat:.1f}" if has_g else "—",    _lc(g_lat) if has_g else COLORS["text_muted"]),
                (f"{g_loss:.0f}%" if has_g else "—",   _pc(g_loss) if has_g else COLORS["text_muted"]),
                (f"{g_avail:.0f}%" if has_g else "—",  _ac(g_avail) if has_g else COLORS["text_muted"]),
                # Deltas
                wan_delta_cell,
                (f"+{d_ggl:.0f}" if d_ggl is not None else "—",
                 _dc(d_ggl) if d_ggl is not None else COLORS["text_muted"]),
                # Último, Origem
                (last_ping, COLORS["text_muted"]),
                ("SSH" if ping_mode == "SSH" else "LCL",
                 COLORS["accent_green"] if ping_mode == "SSH" else COLORS["text_muted"]),
            ]

            for lbl, (text, color) in zip(labels[:-1], data_updates):
                try:
                    lbl.configure(text=text, text_color=color)
                except Exception:
                    pass
            # O botão (labels[-1]) não precisa ser atualizado aqui

    # ── Ping SSH ad-hoc (botão "Pingar" no Monitor) ───────────────────

    def _open_ssh_ping_dialog(self, host):
        """
        Abre um pequeno popup para o usuário informar o IP alvo do ping,
        executa o ping via SSH dentro do host e exibe o resultado.

        COMPORTAMENTO:
          1. Tenta ping dentro do host via SSH usando as credenciais do host
             ou, se vazias, as credenciais padrão das Configurações.
          2. Se SSH falhar por qualquer motivo, faz fallback para ping local
             (mode='LOCAL') e informa o usuário.
        """
        if not self._controller:
            return

        dlg = ctk.CTkToplevel(self)
        dlg.title(f"Pingar SSH — {host.display_name}")
        dlg.geometry("420x240")
        dlg.resizable(False, False)
        dlg.configure(fg_color=COLORS["bg_primary"])
        dlg.transient(self.winfo_toplevel())
        dlg.grab_set()
        dlg.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            dlg,
            text=f"Pingar de dentro de  {host.ip}  via SSH",
            font=(FONTS["family"], FONTS["size_sm"], "bold"),
            text_color=COLORS["text_primary"],
        ).grid(row=0, column=0, padx=20, pady=(18, 4))

        ctk.CTkLabel(
            dlg,
            text=(
                "Se SSH falhar, o ping é executado localmente (fallback).\n"
                "Credenciais: do host, ou padrão das Configurações."
            ),
            font=(FONTS["family"], FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            justify="center",
        ).grid(row=1, column=0, padx=20, pady=(0, 8))

        entry_frame = ctk.CTkFrame(dlg, fg_color="transparent")
        entry_frame.grid(row=2, column=0, sticky="ew", padx=20, pady=4)
        entry_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            entry_frame, text="IP de destino:",
            font=(FONTS["family"], FONTS["size_xs"]),
            text_color=COLORS["text_secondary"],
        ).grid(row=0, column=0, sticky="w")

        ip_entry = ctk.CTkEntry(
            entry_frame,
            placeholder_text="ex: 203.0.113.150  ou  8.8.8.8",
            font=(FONTS["family_mono"], FONTS["size_sm"]),
            fg_color=COLORS["bg_secondary"],
            border_color=COLORS["border"],
            text_color=COLORS["text_primary"],
            corner_radius=6, height=34,
        )
        ip_entry.grid(row=1, column=0, sticky="ew", pady=(4, 0))

        result_label = ctk.CTkLabel(
            dlg, text="",
            font=(FONTS["family_mono"], FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            wraplength=380,
        )
        result_label.grid(row=3, column=0, padx=20, pady=8)

        ping_btn = ctk.CTkButton(
            dlg, text="Executar Ping", width=140,
            font=(FONTS["family"], FONTS["size_sm"], "bold"),
            fg_color=COLORS["accent_blue"],
            hover_color=COLORS["accent_blue_hover"],
            text_color="#FFFFFF", corner_radius=8, height=34,
        )
        ping_btn.grid(row=4, column=0, pady=(0, 16))

        def _do_ping():
            target = ip_entry.get().strip()
            if not target:
                result_label.configure(
                    text="⚠ Informe o IP de destino.", text_color=COLORS["accent_yellow"]
                )
                return
            ping_btn.configure(state="disabled", text="Pingando...")
            result_label.configure(text="⏳ Executando...", text_color=COLORS["text_muted"])

            def _run():
                res = self._controller.run_ssh_ping(host.id, target)
                dlg.after(0, lambda r=res: _show(r))

            threading.Thread(target=_run, daemon=True).start()

        def _show(res: dict):
            ping_btn.configure(state="normal", text="Executar Ping")
            if "error" in res:
                result_label.configure(
                    text=f"❌ {res['error']}", text_color=COLORS["accent_red"]
                )
                return

            source = res.get("source", "?")
            status = res.get("status", "offline")
            lat    = res.get("latency_ms", 0)
            loss   = res.get("loss_pct", 100)
            jit    = res.get("jitter_ms", 0)
            icon   = "✅" if status == "online" else "❌"
            src_lbl = "via SSH" if source == "SSH" else "local (fallback — SSH falhou)"
            color  = COLORS["accent_green"] if status == "online" else COLORS["accent_red"]

            result_label.configure(
                text=(
                    f"{icon}  {status.upper()}  |  {lat:.1f} ms  |  {loss:.0f}% perda"
                    f"  |  jitter {jit:.1f} ms\n"
                    f"Origem: {src_lbl}"
                ),
                text_color=color,
            )

        ping_btn.configure(command=_do_ping)
        ip_entry.bind("<Return>", lambda e: _do_ping())

    # ── Limpar ────────────────────────────────────────────────────────

    def _clear_view(self):
        if self._current_tab == "Monitor":
            return
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self._insert("\n   Tela limpa.\n", "muted")

    # ── Timers de refresh ─────────────────────────────────────────────

    def _schedule_refresh(self):
        try:
            if self._auto_refresh and self._current_tab in ("Logs", "Alertas Ativos"):
                self._refresh()
        except Exception:
            pass
        finally:
            try:
                self.after(10000, self._schedule_refresh)
            except Exception:
                pass

    def _schedule_monitor_refresh(self):
        try:
            if self._auto_refresh and self._current_tab == "Monitor":
                self._update_monitor_tab()
        except Exception:
            pass
        finally:
            try:
                self.after(2000, self._schedule_monitor_refresh)
            except Exception:
                pass

    # ── Chamados externamente ─────────────────────────────────────────

    def refresh_alert_badge(self):
        if self._controller:
            count = len(self._controller.get_active_alerts())
        elif self.db:
            count = len(self.db.get_active_alerts())
        else:
            return
        if count > 0:
            self.count_badge.configure(text=str(count), fg_color=COLORS["accent_red"])
        else:
            self.count_badge.configure(text="", fg_color="transparent")

    def on_cycle_complete(self):
        """Chamado pelo main_view ao final de cada ciclo de ping."""
        if self._current_tab == "Monitor":
            try:
                self._update_monitor_tab()
            except Exception:
                pass