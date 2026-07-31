"""
Host Detail View v2.3 — redesign com toggle Stats/Gráficos.

Alterações:
  1. FIX: Botão Parar MTR agora reseta UI imediatamente sem esperar thread.
  2. Layout: tabela HOST/WAN/GGL sempre no topo com Disp + Deltas.
  3. Toggle entre modo Estatísticas (stats + MTR) e modo Gráficos (charts).
  4. Stats ampliadas com diagnósticos SSH (uptime, WAN IP, gateway, DNS).
  5. Tabela expandida com colunas Origem por tipo e Deltas coloridos.
"""
import threading
import tkinter as tk
import customtkinter as ctk

from config import COLORS, FONTS, THRESHOLDS
from views.widgets import MetricTile, MiniChart


# ── Tooltip ──────────────────────────────────────────────────────────

class _Tip:
    DELAY = 600
    def __init__(self, w, text):
        self._w, self._text, self._win, self._job = w, text, None, None
        w.bind("<Enter>", self._enter, add="+")
        w.bind("<Leave>", self._leave, add="+")
        w.bind("<ButtonPress>", self._leave, add="+")
    def _enter(self, _=None):
        self._cancel(); self._job = self._w.after(self.DELAY, self._show)
    def _leave(self, _=None):
        self._cancel(); self._hide()
    def _cancel(self):
        if self._job:
            try: self._w.after_cancel(self._job)
            except Exception: pass
            self._job = None
    def _show(self):
        self._hide()
        x = self._w.winfo_rootx() + 8
        y = self._w.winfo_rooty() + self._w.winfo_height() + 4
        w = tk.Toplevel(self._w); w.wm_overrideredirect(True); w.wm_geometry(f"+{x}+{y}")
        w.configure(bg=COLORS["border"])
        inner = tk.Frame(w, bg=COLORS["bg_elevated"], padx=10, pady=6); inner.pack(padx=1, pady=1)
        tk.Label(inner, text=self._text, bg=COLORS["bg_elevated"], fg=COLORS["text_primary"],
                 font=(FONTS["family"], FONTS["size_xs"]), justify="left").pack(anchor="w")
        self._win = w
    def _hide(self):
        if self._win:
            try: self._win.destroy()
            except: pass
            self._win = None


# ── Helpers de cor ───────────────────────────────────────────────────

def _lat_c(v):
    if v <= 0: return COLORS["text_muted"]
    return COLORS["accent_green"] if v < 50 else COLORS["accent_yellow"] if v < 150 else COLORS["accent_red"]

def _loss_c(p):
    if p == 0: return COLORS["accent_green"]
    return COLORS["accent_yellow"] if p <= 20 else COLORS["accent_red"]

def _avail_c(v):
    if v >= 98: return COLORS["accent_green"]
    return COLORS["accent_yellow"] if v >= 80 else COLORS["accent_red"]

def _delta_c(d):
    if d is None: return COLORS["text_muted"]
    return COLORS["accent_green"] if d < 5 else COLORS["accent_yellow"] if d < 20 else COLORS["accent_red"]

def _jit_c(v):
    if v <= 0: return COLORS["text_muted"]
    return COLORS["accent_green"] if v < 30 else COLORS["accent_yellow"] if v < 80 else COLORS["accent_red"]


# ── Colunas da tabela MTR ────────────────────────────────────────────

_TRACE_COLS = [
    ("Endereco / Hop", 190, "w", "IP do roteador neste hop"),
    ("Nr",     35, "center", "Numero do hop"),
    ("Perda%", 55, "center", "Perda de pacotes (%)"),
    ("Env",    42, "center", "Pacotes enviados"),
    ("Recv",   42, "center", "Pacotes recebidos"),
    ("Melhor", 58, "center", "Menor RTT (ms)"),
    ("Media",  58, "center", "RTT medio (ms)"),
    ("Pior",   58, "center", "Maior RTT (ms)"),
    ("Ultimo", 58, "center", "RTT mais recente (ms)"),
]


class HostDetailView(ctk.CTkFrame):
    def __init__(self, master, controller=None, on_back=None, **kw):
        super().__init__(master, fg_color=COLORS["bg_primary"], **kw)
        self.controller = controller
        self._on_back = on_back
        self._host = None
        self._host_id = None
        self._mtr_stop_event = None
        self._mtr_thread = None
        self._mtr_running = False
        self._mtr_active_wan = 1   # 1 = WAN principal, 2 = WAN secundária
        self._mode = "stats"   # "stats" ou "graphs"
        self._diag_data = {}   # dados de diagnóstico SSH

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)  # row 3 = content area (expande)

        self._build_header()         # row 0
        self._build_metrics_table()  # row 1
        self._build_content_area()   # rows 2-3

    # ══════════════════════════════════════════════════════════════════
    # HEADER
    # ══════════════════════════════════════════════════════════════════

    def _build_header(self):
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=24, pady=(16, 6))
        hdr.grid_columnconfigure(1, weight=1)

        ctk.CTkButton(hdr, text="← Voltar", width=70,
            font=(FONTS["family"], FONTS["size_sm"]),
            fg_color="transparent", text_color=COLORS["accent_blue"],
            hover_color=COLORS["bg_tertiary"], command=self._go_back
        ).grid(row=0, column=0, sticky="w")

        self.lbl_name = ctk.CTkLabel(hdr, text="—",
            font=(FONTS["family"], FONTS["size_xl"], "bold"),
            text_color=COLORS["text_primary"])
        self.lbl_name.grid(row=0, column=1, sticky="w", padx=(12, 0))

        self.lbl_info = ctk.CTkLabel(hdr, text="",
            font=(FONTS["family_mono"], FONTS["size_sm"]),
            text_color=COLORS["text_secondary"])
        self.lbl_info.grid(row=1, column=1, sticky="w", padx=(12, 0))

        # Badges
        badge_f = ctk.CTkFrame(hdr, fg_color="transparent")
        badge_f.grid(row=0, column=2, padx=8)
        self.badge_status = ctk.CTkLabel(badge_f, text="—",
            font=(FONTS["family"], FONTS["size_xs"], "bold"),
            text_color="#FFFFFF", fg_color=COLORS["text_muted"],
            corner_radius=6, padx=10, pady=2)
        self.badge_status.pack(side="left", padx=(0, 4))
        self.badge_origin = ctk.CTkLabel(badge_f, text="—",
            font=(FONTS["family_mono"], FONTS["size_xs"], "bold"),
            text_color="#FFFFFF", fg_color=COLORS["text_muted"],
            corner_radius=6, padx=8, pady=2)
        self.badge_origin.pack(side="left")

        # Botões
        btns = ctk.CTkFrame(hdr, fg_color="transparent")
        btns.grid(row=0, column=3, rowspan=2, sticky="e")

        # Botão MTR WAN2 (IP público) — visível apenas se wan_ip_2 configurado
        self.btn_mtr_wan2 = ctk.CTkButton(btns, text="▶ MTR WAN2", width=100,
            font=(FONTS["family"], FONTS["size_xs"], "bold"),
            fg_color=COLORS["accent_cyan"], hover_color="#0891B2",
            text_color="#FFFFFF", command=self._toggle_mtr_wan2)
        self.btn_mtr_wan2.pack(side="left", padx=3)
        self.btn_mtr_wan2.pack_forget()

        # Botão MTR WAN3 (2º hop operadora) — visível apenas se wan_ip_3 configurado
        self.btn_mtr_wan3 = ctk.CTkButton(btns, text="▶ MTR WAN3", width=100,
            font=(FONTS["family"], FONTS["size_xs"], "bold"),
            fg_color="#0E7490", hover_color="#155E75",
            text_color="#FFFFFF", command=self._toggle_mtr_wan3)
        self.btn_mtr_wan3.pack(side="left", padx=3)
        self.btn_mtr_wan3.pack_forget()

        # Botão MTR Google — sempre visível
        self.btn_mtr_google = ctk.CTkButton(btns, text="▶ MTR Google", width=100,
            font=(FONTS["family"], FONTS["size_xs"], "bold"),
            fg_color=COLORS["accent_orange"], hover_color="#EA580C",
            text_color="#FFFFFF", command=self._toggle_mtr_google)
        self.btn_mtr_google.pack(side="left", padx=3)

        ctk.CTkButton(btns, text="MTU", width=50,
            font=(FONTS["family"], FONTS["size_xs"]),
            fg_color=COLORS["bg_secondary"], hover_color=COLORS["bg_tertiary"],
            border_width=1, border_color=COLORS["border"],
            text_color=COLORS["text_primary"], command=self._run_mtu
        ).pack(side="left", padx=3)

        self.btn_diag = ctk.CTkButton(btns, text="🔍 Info", width=60,
            font=(FONTS["family"], FONTS["size_xs"]),
            fg_color=COLORS["bg_secondary"], hover_color=COLORS["bg_tertiary"],
            border_width=1, border_color=COLORS["border"],
            text_color=COLORS["text_primary"], command=self._run_diagnostics)
        self.btn_diag.pack(side="left", padx=3)

        self.btn_mode = ctk.CTkButton(btns, text="📊 Gráficos", width=90,
            font=(FONTS["family"], FONTS["size_xs"], "bold"),
            fg_color=COLORS["accent_purple"], hover_color="#7C3AED",
            text_color="#FFFFFF", command=self._toggle_mode)
        self.btn_mode.pack(side="left", padx=3)

    # ══════════════════════════════════════════════════════════════════
    # TABELA HOST / WAN / GOOGLE (row 1 — sempre visível)
    # ══════════════════════════════════════════════════════════════════

    def _build_metrics_table(self):
        outer = ctk.CTkFrame(self, fg_color=COLORS["bg_secondary"],
                             corner_radius=8, border_width=1, border_color=COLORS["border"])
        outer.grid(row=1, column=0, sticky="ew", padx=24, pady=(4, 6))
        outer.grid_columnconfigure(0, weight=1)

        # Header
        cols = ["Tipo", "Latência", "Jitter", "Perda", "Disp.", "RTT m/M",
                "Δ WAN", "Δ Google", "Origem"]
        hdr = ctk.CTkFrame(outer, fg_color=COLORS["bg_tertiary"], corner_radius=0)
        hdr.pack(fill="x")
        widths = [55, 65, 55, 50, 55, 65, 55, 60, 50]
        for i, (c, w) in enumerate(zip(cols, widths)):
            ctk.CTkLabel(hdr, text=c, width=w, anchor="center",
                font=(FONTS["family"], FONTS["size_xs"], "bold"),
                text_color=COLORS["text_secondary"]
            ).pack(side="left", padx=(8 if i == 0 else 2, 2), pady=4)

        # Rows: HOST, WAN, GGL
        self._tbl_labels = {}
        colors = {"HOST": "#10B981", "WAN": "#3B82F6", "GGL": "#F97316"}
        for tipo in ["HOST", "WAN", "GGL"]:
            row = ctk.CTkFrame(outer, fg_color=COLORS["bg_primary"], corner_radius=0)
            row.pack(fill="x")
            labels = []
            # Tipo label (colorido)
            lbl_tipo = ctk.CTkLabel(row, text=tipo, width=55, anchor="center",
                font=(FONTS["family_mono"], FONTS["size_xs"], "bold"),
                text_color=colors[tipo])
            lbl_tipo.pack(side="left", padx=(8, 2), pady=3)
            labels.append(lbl_tipo)
            # Dados: 8 colunas
            for j, w in enumerate(widths[1:]):
                lbl = ctk.CTkLabel(row, text="—", width=w, anchor="center",
                    font=(FONTS["family_mono"], FONTS["size_xs"]),
                    text_color=COLORS["text_muted"])
                lbl.pack(side="left", padx=2, pady=3)
                labels.append(lbl)
            self._tbl_labels[tipo] = labels

    # ══════════════════════════════════════════════════════════════════
    # CONTENT AREA (row 2-3 — toggle entre stats e graphs)
    # ══════════════════════════════════════════════════════════════════

    def _build_content_area(self):
        self._content = ctk.CTkFrame(self, fg_color="transparent")
        self._content.grid(row=2, column=0, rowspan=2, sticky="nsew", padx=24, pady=(4, 16))
        self._content.grid_columnconfigure(0, weight=3)
        self._content.grid_columnconfigure(1, weight=4)
        self._content.grid_rowconfigure(0, weight=1)

        # ── Stats mode frames ────────────────────────────────────────
        self._stats_left = ctk.CTkFrame(self._content, fg_color=COLORS["bg_secondary"],
            corner_radius=12, border_width=1, border_color=COLORS["border"])
        self._stats_right = ctk.CTkFrame(self._content, fg_color=COLORS["bg_secondary"],
            corner_radius=12, border_width=1, border_color=COLORS["border"])

        # Left: stats textbox
        self._stats_left.grid_columnconfigure(0, weight=1)
        self._stats_left.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(self._stats_left, text="ESTATÍSTICAS & DIAGNÓSTICO",
            font=(FONTS["family"], FONTS["size_md"], "bold"),
            text_color=COLORS["accent_blue"], anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        self.info_text = ctk.CTkTextbox(self._stats_left,
            font=(FONTS["family_mono"], FONTS["size_sm"]),
            fg_color=COLORS["bg_primary"], text_color=COLORS["text_primary"],
            border_width=1, border_color=COLORS["border"], corner_radius=8)
        self.info_text.grid(row=1, column=0, sticky="nsew", padx=10, pady=(2, 10))

        # Right: MTR/Hops
        self._stats_right.grid_columnconfigure(0, weight=1)
        self._stats_right.grid_rowconfigure(1, weight=1)

        mtr_hdr = ctk.CTkFrame(self._stats_right, fg_color="transparent")
        mtr_hdr.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        mtr_hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(mtr_hdr, text="MTR / Hops",
            font=(FONTS["family"], FONTS["size_md"], "bold"),
            text_color=COLORS["text_primary"], anchor="w"
        ).grid(row=0, column=0, sticky="w")
        self._trace_status_label = ctk.CTkLabel(mtr_hdr, text="",
            font=(FONTS["family_mono"], FONTS["size_xs"]),
            text_color=COLORS["text_muted"], anchor="e")
        self._trace_status_label.grid(row=0, column=1, sticky="e")

        # MTR table container
        tbl_outer = ctk.CTkFrame(self._stats_right, fg_color=COLORS["bg_primary"],
            corner_radius=8, border_width=1, border_color=COLORS["border"])
        tbl_outer.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        tbl_outer.grid_columnconfigure(0, weight=1)
        tbl_outer.grid_rowconfigure(1, weight=1)

        # MTR column headers
        hdr_f = ctk.CTkFrame(tbl_outer, fg_color=COLORS["bg_tertiary"], corner_radius=0)
        hdr_f.grid(row=0, column=0, sticky="ew")
        for ci, (h, w, a, tip) in enumerate(_TRACE_COLS):
            lbl = ctk.CTkLabel(hdr_f, text=h, width=w, anchor=a,
                font=(FONTS["family"], FONTS["size_xs"], "bold"),
                text_color=COLORS["text_secondary"], cursor="question_arrow")
            lbl.grid(row=0, column=ci, padx=(8 if ci == 0 else 2, 2), pady=4, sticky="w")
            _Tip(lbl, tip)

        self._trace_scroll = ctk.CTkScrollableFrame(tbl_outer, fg_color="transparent",
            scrollbar_button_color=COLORS["scrollbar"],
            scrollbar_button_hover_color=COLORS["scrollbar_hover"])
        self._trace_scroll.grid(row=1, column=0, sticky="nsew")
        ctk.CTkLabel(self._trace_scroll,
            text="Clique em ▶ MTR WAN para iniciar.",
            font=(FONTS["family"], FONTS["size_xs"]),
            text_color=COLORS["text_muted"]).pack(pady=20)

        # ── Graphs mode frames ───────────────────────────────────────
        self._graphs_left = ctk.CTkFrame(self._content, fg_color=COLORS["bg_secondary"],
            corner_radius=12, border_width=1, border_color=COLORS["border"])
        self._graphs_right = ctk.CTkFrame(self._content, fg_color=COLORS["bg_secondary"],
            corner_radius=12, border_width=1, border_color=COLORS["border"])

        self._graphs_left.grid_columnconfigure(0, weight=1)
        self._graphs_left.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(self._graphs_left, text="Histórico de Latência (últimas 60 medições)",
            font=(FONTS["family"], FONTS["size_md"], "bold"),
            text_color=COLORS["text_primary"], anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 2))
        self.lat_chart = MiniChart(self._graphs_left, color=COLORS["chart_line1"],
            width=600, height=250, label_y="ms", label_x="Medições")
        self.lat_chart.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))

        self._graphs_right.grid_columnconfigure(0, weight=1)
        self._graphs_right.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(self._graphs_right, text="Histórico de Perda de Pacotes (%)",
            font=(FONTS["family"], FONTS["size_md"], "bold"),
            text_color=COLORS["text_primary"], anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 2))
        self.loss_chart = MiniChart(self._graphs_right, color=COLORS["accent_red"],
            width=600, height=250, label_y="%", label_x="Medições")
        self.loss_chart.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))

        # Start in stats mode
        self._show_mode("stats")

    def _show_mode(self, mode):
        self._mode = mode
        # Hide all
        for f in [self._stats_left, self._stats_right, self._graphs_left, self._graphs_right]:
            f.grid_forget()
        if mode == "stats":
            self._stats_left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
            self._stats_right.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
            self.btn_mode.configure(text="📊 Gráficos", fg_color=COLORS["accent_purple"])
        else:
            self._graphs_left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
            self._graphs_right.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
            self.btn_mode.configure(text="📋 Estatísticas", fg_color=COLORS["accent_blue"])

    def _toggle_mode(self):
        self._show_mode("graphs" if self._mode == "stats" else "stats")

    # ══════════════════════════════════════════════════════════════════
    # LOAD / UPDATE HOST
    # ══════════════════════════════════════════════════════════════════

    def load_host(self, host_id):
        self._stop_mtr(wait=True)
        self._clear_mtr_display()
        self._diag_data = {}
        self._host_id = host_id
        if self.controller:
            self._host = self.controller.get_host(host_id)
            if self._host:
                self._update_display()

    def _clear_mtr_display(self):
        for w in self._trace_scroll.winfo_children():
            w.destroy()
        ctk.CTkLabel(self._trace_scroll,
            text="Clique em ▶ MTR WAN2, ▶ MTR WAN3 ou ▶ MTR Google para iniciar.",
            font=(FONTS["family"], FONTS["size_xs"]),
            text_color=COLORS["text_muted"]).pack(pady=20)
        self._trace_status_label.configure(text="", text_color=COLORS["text_muted"])
        if self._mtr_stop_event:
            self._mtr_stop_event.set()
        self._mtr_running = False
        self._mtr_active_wan = None
        self._mtr_stop_event = None
        self._mtr_thread = None
        self._refresh_btn_states(active_wan=None)

    def _update_display(self):
        h = self._host
        if not h: return

        self.lbl_name.configure(text=h.display_name)
        ttl = str(h.ping_history[-1].ttl) if h.ping_history else "—"
        self.lbl_info.configure(text=f"{h.ip}  |  Grupo: {h.group_name}  |  TTL: {ttl}")

        # Mostra/esconde botão WAN2 conforme configuração do host
        # Nota: NÃO configura texto/cor aqui — _refresh_btn_states faz isso
        # para preservar o estado "Parar" caso o MTR esteja rodando.
        wan2 = getattr(h, "wan_ip_2", "").strip()
        if wan2:
            self.btn_mtr_wan2.pack(side="left", padx=3)
        else:
            self.btn_mtr_wan2.pack_forget()

        # Mostra/esconde botão WAN3 conforme configuração do host
        wan3 = getattr(h, "wan_ip_3", "").strip()
        if wan3:
            self.btn_mtr_wan3.pack(side="left", padx=3)
        else:
            self.btn_mtr_wan3.pack_forget()

        # Restaura o estado correto dos botões MTR (idle ou "Parar" se rodando)
        self._refresh_btn_states(
            active_wan=self._mtr_active_wan if self._mtr_running else None
        )

        # Status + Origin badges
        sc = {"online": (COLORS["accent_green"], "ONLINE"),
              "offline": (COLORS["accent_red"], "OFFLINE")}
        c, t = sc.get(h.status, (COLORS["text_muted"], "UNKNOWN"))
        self.badge_status.configure(text=t, fg_color=c)
        if h.last_ping_mode == "SSH":
            self.badge_origin.configure(text="SSH", fg_color=COLORS["accent_green"])
        else:
            self.badge_origin.configure(text="LOCAL", fg_color=COLORS["accent_yellow_dim"],
                                        text_color=COLORS["accent_yellow"])

        # ── Atualiza tabela HOST/WAN/GGL ──────────────────────────────
        def _set_row(tipo, lat, jit, loss, avail, rtt, d_wan, d_ggl, src, has_data):
            lbls = self._tbl_labels[tipo]
            # lbls[0]=tipo (não muda), [1]=lat, [2]=jit, [3]=perda, [4]=disp,
            # [5]=rtt, [6]=Δwan, [7]=Δggl, [8]=origem
            if has_data:
                lbls[1].configure(text=f"{lat:.1f}", text_color=_lat_c(lat))
                lbls[2].configure(text=f"{jit:.1f}", text_color=_jit_c(jit))
                lbls[3].configure(text=f"{loss:.0f}%", text_color=_loss_c(loss))
                lbls[4].configure(text=f"{avail:.0f}%", text_color=_avail_c(avail))
                lbls[5].configure(text=rtt, text_color=COLORS["text_primary"])
                lbls[8].configure(text=src, text_color=COLORS["accent_green"] if src == "SSH" else COLORS["text_muted"])
            else:
                for i in range(1, 6):
                    lbls[i].configure(text="—", text_color=COLORS["text_muted"])
                lbls[8].configure(text="—", text_color=COLORS["text_muted"])

            # Deltas (só na linha HOST)
            if tipo == "HOST":
                if d_wan is not None:
                    lbls[6].configure(text=f"+{d_wan:.0f}", text_color=_delta_c(d_wan))
                else:
                    lbls[6].configure(text="—", text_color=COLORS["text_muted"])
                if d_ggl is not None:
                    lbls[7].configure(text=f"+{d_ggl:.0f}", text_color=_delta_c(d_ggl))
                else:
                    lbls[7].configure(text="—", text_color=COLORS["text_muted"])
            else:
                lbls[6].configure(text="", text_color=COLORS["text_muted"])
                lbls[7].configure(text="", text_color=COLORS["text_muted"])

        _set_row("HOST", h.host_ssh_latency, h.host_ssh_jitter, h.host_ssh_loss,
                 h.host_ssh_avail, h.host_ssh_rtt, h.delta_wan, h.delta_google,
                 h.host_ssh_source, h.host_ssh_has_data)
        _set_row("WAN", h.wan_latency, h.wan_jitter, h.wan_loss,
                 h.wan_avail, h.wan_rtt, None, None, "SSH" if h.wan_has_data else "—", h.wan_has_data)
        _set_row("GGL", h.google_latency, h.google_jitter, h.google_loss,
                 h.google_avail, h.google_rtt, None, None, "SSH" if h.google_has_data else "—", h.google_has_data)

        # ── Gráficos ──────────────────────────────────────────────────
        self.lat_chart.update_data([p.latency_ms for p in h.ping_history])
        self.loss_chart.update_data([p.loss_pct for p in h.ping_history])

        # ── Stats panel ───────────────────────────────────────────────
        self.info_text.delete("1.0", "end")
        if self.controller:
            s = self.controller.get_host_stats(h.id)
            cn = s.get("cycle_number", 0)
            pc = s.get("ping_in_cycle", 0)
            cs = s.get("cycle_size", 100)
            ta = s.get("total_pings_all_time", 0)
            dw = s.get("delta_wan")
            dg = s.get("delta_google")

            lines = [
                f"  Ciclo: {cn}  |  Ping: {pc}/{cs}  |  Total: {ta}",
                f"  Origem: {s.get('last_ping_mode','—')}  |  Coleta: {s.get('last_collection_ts','—')}",
                f"  Deltas: ΔWAN {'+' + f'{dw:.1f}' if dw is not None else '—'}ms"
                f"   ΔGoogle {'+' + f'{dg:.1f}' if dg is not None else '—'}ms",
                "  ═════════════════════════════════════",
                f"  Ping local (24h):",
                f"    Media: {s.get('avg_latency',0):.1f}ms  Max: {s.get('max_latency',0):.1f}ms"
                f"  Min: {s.get('min_latency',0):.1f}ms",
                f"    Desvio: {s.get('stddev_latency',0):.2f}ms"
                f"  Jitter: {s.get('avg_jitter',0):.1f}ms",
                f"    Perda: {s.get('avg_loss',0):.1f}%"
                f"  Disp: {s.get('availability_pct',0):.1f}%"
                f"  Sucesso: {s.get('success_rate',0):.1f}%",
                f"    Falhas consecutivas: {h.consecutive_failures}",
            ]

            # Diagnósticos SSH (se já coletados)
            d = self._diag_data
            if d and "error" not in d:
                lines.append("  ═════════════════════════════════════")
                lines.append("  Diagnóstico SSH:")
                if d.get("hostname"):   lines.append(f"    Hostname:  {d['hostname']}")
                if d.get("uptime"):     lines.append(f"    Uptime:    {d['uptime'][:60]}")
                if d.get("wan_ip"):     lines.append(f"    WAN IP:    {d['wan_ip']}")
                if d.get("gateway"):    lines.append(f"    Gateway:   {d['gateway']}")
                if d.get("dns"):        lines.append(f"    DNS:\n      {d['dns']}")
                if d.get("memory") and d["memory"] != "—":
                    lines.append(f"    Memória:   {d['memory']}")
                if d.get("disk") and d["disk"] != "—":
                    lines.append(f"    Disco:     {d['disk']}")
                if d.get("interface_speed") and d["interface_speed"] != "—":
                    lines.append(f"    Interface: {d['interface_speed']}Mbps {d.get('duplex','')}")
            elif d and "error" in d:
                lines.append("  ═════════════════════════════════════")
                lines.append(f"  Diagnóstico: Erro — {d['error']}")

            self.info_text.insert("1.0", "\n".join(lines))

    def update_live(self, host):
        if self._host_id and host.id == self._host_id:
            self._host = host
            self._update_display()

    # ══════════════════════════════════════════════════════════════════
    # ══════════════════════════════════════════════════════════════════
    # MTR WAN — botões WAN1 e WAN2
    # ══════════════════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════════════════
    # MTR — botões WAN2, WAN3 e Google
    #
    # Fluxo:
    #   • Clicar no botão do MTR ativo  → para o MTR atual
    #   • Clicar em outro botão enquanto um roda → para o atual e inicia o novo
    #   • _refresh_btn_states() é a única função que altera as cores/textos,
    #     garantindo estado coerente em qualquer situação
    # ══════════════════════════════════════════════════════════════════

    # Definições dos botões MTR: wan_key → (btn_attr, idle_color, idle_hover, idle_text)
    # Definidas como método para garantir acesso correto a COLORS em runtime.
    def _mtr_btn_defs(self):
        return {
            2:        (self.btn_mtr_wan2,   COLORS["accent_cyan"],   "#0891B2",  "▶ MTR WAN2"),
            3:        (self.btn_mtr_wan3,   "#0E7490",               "#155E75",  "▶ MTR WAN3"),
            "google": (self.btn_mtr_google, COLORS["accent_orange"], "#EA580C",  "▶ MTR Google"),
        }

    def _btn_for(self, wan):
        try:
            return self._mtr_btn_defs()[wan][0]
        except Exception:
            return None

    def _refresh_btn_states(self, active_wan=None):
        """
        Atualiza texto e cor dos três botões MTR.
        active_wan: wan key que está rodando (2, 3 ou "google"), ou None.
        """
        for wan, (btn, idle_c, idle_h, idle_txt) in self._mtr_btn_defs().items():
            if wan == active_wan:
                label = idle_txt.replace("▶ MTR ", "")
                btn.configure(
                    text=f"⬛ Parar {label}",
                    fg_color=COLORS["accent_red"],
                    hover_color="#DC2626")
            else:
                btn.configure(
                    text=idle_txt,
                    fg_color=idle_c,
                    hover_color=idle_h)

    def _toggle_mtr_wan(self):
        """Compat com código legado."""
        self._toggle_mtr_wan2()

    def _toggle_mtr_wan2(self):
        if self._mtr_running and self._mtr_active_wan == 2:
            self._stop_mtr()          # para o atual
        else:
            self._start_mtr(wan=2)    # inicia (parando o anterior se houver)

    def _toggle_mtr_wan3(self):
        if self._mtr_running and self._mtr_active_wan == 3:
            self._stop_mtr()
        else:
            self._start_mtr(wan=3)

    def _toggle_mtr_google(self):
        if self._mtr_running and self._mtr_active_wan == "google":
            self._stop_mtr()
        else:
            self._start_mtr(wan="google")

    def _start_mtr(self, wan=2):
        """
        Inicia MTR para o alvo indicado.
          wan=2        → wan_ip_2 (IP público da loja)
          wan=3        → wan_ip_3 (2º hop da operadora)
          wan="google" → IP configurável em Configurações (padrão 8.8.8.8)
        Se outro MTR estiver rodando, para-o antes de iniciar o novo.
        """
        if not self.controller or not self._host:
            return

        # Valida alvo configurado para WAN2/WAN3
        if wan == 2:
            target_ip = getattr(self._host, "wan_ip_2", "").strip()
            if not target_ip:
                self._trace_status_label.configure(
                    text="WAN2 não configurado — edite em Configurações.",
                    text_color=COLORS["accent_yellow"])
                return
        elif wan == 3:
            target_ip = getattr(self._host, "wan_ip_3", "").strip()
            if not target_ip:
                self._trace_status_label.configure(
                    text="WAN3 não configurado — edite em Configurações.",
                    text_color=COLORS["accent_yellow"])
                return

        # Para o MTR anterior sem atualizar os botões ainda
        # (evita flash: botões idle → botões ativos logo abaixo)
        if self._mtr_stop_event:
            self._mtr_stop_event.set()
        if self._mtr_thread and self._mtr_thread.is_alive():
            self._mtr_thread.join(timeout=3)

        # Prepara novo MTR
        self._mtr_running = True
        self._mtr_active_wan = wan
        self._mtr_stop_event = threading.Event()

        # Atualiza botões de uma vez (sem flash)
        self._refresh_btn_states(active_wan=wan)
        self._trace_status_label.configure(
            text=f"Iniciando MTR {self._mtr_btn_defs()[wan][3].replace('▶ MTR ', '')}…",
            text_color=COLORS["accent_cyan"])

        ev  = self._mtr_stop_event
        hid = self._host_id
        run_fn = {
            2:        self.controller.run_mtr_wan_2,
            3:        self.controller.run_mtr_wan_3,
            "google": self.controller.run_mtr_google,
        }[wan]
        wan_key = wan   # captura para a closure — evita race condition no _on_mtr_finished

        def _run():
            try:
                run_fn(hid, ev, on_round=self._on_mtr_round)
            except Exception as e:
                self.after(0, lambda: self._trace_status_label.configure(
                    text=f"Erro: {e}", text_color=COLORS["accent_red"]))
            finally:
                self.after(0, lambda w=wan_key: self._on_mtr_finished(w))

        self._mtr_thread = threading.Thread(target=_run, daemon=True)
        self._mtr_thread.start()

    def _start_mtr_wan(self):
        """Compat com código legado."""
        self._start_mtr(wan=2)

    def _stop_mtr(self, wait=False):
        """Para o MTR ativo e reseta os botões imediatamente."""
        if self._mtr_stop_event:
            self._mtr_stop_event.set()
        self._mtr_running = False
        self._mtr_active_wan = None
        # Reseta todos os botões de uma vez
        self._refresh_btn_states(active_wan=None)
        if wait and self._mtr_thread and self._mtr_thread.is_alive():
            self._mtr_thread.join(timeout=3)

    def _on_mtr_round(self, rn, hs, src):
        if rn <= 0:
            self.after(0, lambda s=src: self._trace_status_label.configure(
                text=s, text_color=COLORS["accent_yellow"]))
            return
        snap = {k: dict(v) for k, v in hs.items()}
        self.after(0, lambda r=rn, s=snap, sr=src: self._render_mtr_table(s, r, sr))

    def _on_mtr_finished(self, expected_wan=None):
        """
        Chamado quando a thread do MTR encerra (normal ou por erro).

        expected_wan: wan key que estava ativa quando a thread foi criada.
        Se outro MTR já foi iniciado entretempos (active_wan mudou), não
        reseta os botões — senão haveria um flash idle→ativo causado pelo
        after(0,...) da thread anterior chegando depois do _refresh_btn_states
        do novo MTR.
        """
        if expected_wan is not None and self._mtr_active_wan != expected_wan:
            # Um novo MTR foi iniciado antes desta callback disparar — ignora.
            return
        self._mtr_running = False
        self._mtr_active_wan = None
        self._refresh_btn_states(active_wan=None)

    def _render_mtr_table(self, hs, rn, src):
        self._trace_status_label.configure(
            text=f"Rodada {rn}  ·  {src}" if src else f"Rodada {rn}",
            text_color=COLORS["text_secondary"])
        for w in self._trace_scroll.winfo_children():
            w.destroy()
        if not hs:
            ctk.CTkLabel(self._trace_scroll, text="Aguardando...",
                font=(FONTS["family"], FONTS["size_xs"]),
                text_color=COLORS["text_muted"]).pack(pady=16)
            return
        bgs = [COLORS["bg_primary"], COLORS["bg_secondary"]]
        for ri, (hn, s) in enumerate(sorted(hs.items())):
            ip = s.get("ip", "*"); se = s.get("sent", 0); re_ = s.get("recv", 0)
            lo = s.get("loss_pct", 100); be = s.get("best", 0.0)
            av = s.get("avg", 0.0); wo = s.get("worst", 0.0); la = s.get("last", 0.0)
            is_to = (ip == "*")
            r = ctk.CTkFrame(self._trace_scroll, fg_color=bgs[ri % 2], corner_radius=0)
            r.pack(fill="x")

            def L(p, t, w, a, c, b=False):
                return ctk.CTkLabel(p, text=t, width=w, anchor=a,
                    font=(FONTS["family_mono"], FONTS["size_xs"], "bold" if b else "normal"),
                    text_color=c)

            L(r, ip if not is_to else "* * *", 190, "w",
              COLORS["text_primary"] if not is_to else COLORS["text_muted"]).pack(side="left", padx=(8, 2), pady=3)
            L(r, str(hn), 35, "center", COLORS["text_muted"]).pack(side="left", padx=2, pady=3)
            L(r, f"{lo}%" if not is_to else "-", 55, "center",
              _loss_c(lo) if not is_to else COLORS["text_muted"],
              b=not is_to and lo > 0).pack(side="left", padx=2, pady=3)
            L(r, str(se), 42, "center", COLORS["text_muted"]).pack(side="left", padx=2, pady=3)
            rc = (COLORS["accent_green"] if re_ == se and se > 0
                  else COLORS["accent_yellow"] if re_ > 0 else COLORS["accent_red"])
            L(r, str(re_), 42, "center",
              rc if not is_to else COLORS["text_muted"]).pack(side="left", padx=2, pady=3)
            for v in [be, av, wo, la]:
                vt = f"{v:.1f}" if not is_to and v > 0 else "-"
                L(r, vt, 58, "center",
                  _lat_c(v) if not is_to else COLORS["text_muted"]).pack(side="left", padx=2, pady=3)

    # ══════════════════════════════════════════════════════════════════
    # MTU + DIAGNOSTICS + NAVIGATION
    # ══════════════════════════════════════════════════════════════════

    def _run_mtu(self):
        if not self._host_id or not self.controller: return
        def _r():
            mtu = self.controller.run_mtu_discovery(self._host_id)
            self.after(0, lambda: self.info_text.insert("end", f"\n  MTU: {mtu} bytes\n"))
        threading.Thread(target=_r, daemon=True).start()

    def _run_diagnostics(self):
        if not self._host_id or not self.controller: return
        self.btn_diag.configure(text="⏳...", state="disabled")

        def _r():
            data = self.controller.run_diagnostics(self._host_id)
            self.after(0, lambda d=data: self._diag_done(d))

        threading.Thread(target=_r, daemon=True).start()

    def _diag_done(self, data):
        self.btn_diag.configure(text="🔍 Info", state="normal")
        self._diag_data = data
        self._update_display()  # re-render stats with diagnostics

    def _go_back(self):
        self._clear_mtr_display()   # para MTR e limpa o painel visualmente
        if self._on_back:
            self._on_back()