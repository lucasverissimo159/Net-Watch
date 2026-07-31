"""
Dashboard v2.3 — overview com métricas triplas (HOST + WAN + Google).
Linha 1: Total, Online, Offline, Alertas
Linha 2: HOST Lat/Perda, WAN Lat/Perda, Google Lat/Perda (color-coded)
"""
import customtkinter as ctk
from datetime import datetime

from config import COLORS, FONTS, THRESHOLDS
from views.widgets import StatusCard, MetricTile, MiniChart, bind_mousewheel_scroll


class DashboardView(ctk.CTkFrame):
    def __init__(self, master, controller=None, on_host_click=None, **kwargs):
        super().__init__(master, fg_color=COLORS["bg_primary"], **kwargs)
        self.controller = controller
        self._on_host_click = on_host_click
        self._host_cards: dict[int, StatusCard] = {}

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        self._build_header()
        self._build_metrics_row1()
        self._build_metrics_row2()
        self._build_host_grid()

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 8))
        header.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(header, text="Dashboard",
                     font=(FONTS["family"], FONTS["size_xl"], "bold"),
                     text_color=COLORS["text_primary"]).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(header, text="Monitoramento em tempo real",
                     font=(FONTS["family"], FONTS["size_sm"]),
                     text_color=COLORS["text_secondary"]).grid(row=1, column=0, sticky="w")

        self.status_frame = ctk.CTkFrame(header, fg_color="transparent")
        self.status_frame.grid(row=0, column=1, rowspan=2, sticky="e")
        self.running_indicator = ctk.CTkLabel(
            self.status_frame, text="● MONITORANDO",
            font=(FONTS["family_mono"], FONTS["size_xs"], "bold"),
            text_color=COLORS["accent_green"])
        self.running_indicator.pack(side="right", padx=(12, 0))
        self.clock_label = ctk.CTkLabel(
            self.status_frame, text="",
            font=(FONTS["family_mono"], FONTS["size_sm"]),
            text_color=COLORS["text_secondary"])
        self.clock_label.pack(side="right")
        self._update_clock()

    def _build_metrics_row1(self):
        """Linha 1: Total, Online, Offline, Alertas."""
        f = ctk.CTkFrame(self, fg_color="transparent")
        f.grid(row=1, column=0, sticky="ew", padx=24, pady=(8, 4))
        for i in range(4):
            f.grid_columnconfigure(i, weight=1, uniform="r1")

        self.tile_total = MetricTile(f, title="Total Hosts", value="0", icon="◉", accent=COLORS["accent_blue"])
        self.tile_total.grid(row=0, column=0, padx=(0, 4), pady=2, sticky="ew")
        self.tile_online = MetricTile(f, title="Online", value="0", icon="▲", accent=COLORS["accent_green"])
        self.tile_online.grid(row=0, column=1, padx=4, pady=2, sticky="ew")
        self.tile_offline = MetricTile(f, title="Offline", value="0", icon="▼", accent=COLORS["accent_red"])
        self.tile_offline.grid(row=0, column=2, padx=4, pady=2, sticky="ew")
        self.tile_alerts = MetricTile(f, title="Alertas", value="0", icon="🔔", accent=COLORS["accent_red"])
        self.tile_alerts.grid(row=0, column=3, padx=(4, 0), pady=2, sticky="ew")

    def _build_metrics_row2(self):
        """Linha 2: Lat/Perda por HOST, WAN, Google (color-coded)."""
        f = ctk.CTkFrame(self, fg_color="transparent")
        f.grid(row=2, column=0, sticky="ew", padx=24, pady=(4, 8))
        for i in range(6):
            f.grid_columnconfigure(i, weight=1, uniform="r2")

        # HOST (verde)
        self.tile_host_lat = MetricTile(f, title="● HOST Lat", value="—", unit="ms", icon="⏱", accent="#10B981")
        self.tile_host_lat.grid(row=0, column=0, padx=(0, 4), pady=2, sticky="ew")
        self.tile_host_loss = MetricTile(f, title="● HOST Perda", value="—", unit="%", icon="⚡", accent="#10B981")
        self.tile_host_loss.grid(row=0, column=1, padx=4, pady=2, sticky="ew")

        # WAN (azul)
        self.tile_wan_lat = MetricTile(f, title="● WAN Lat", value="—", unit="ms", icon="⏱", accent="#3B82F6")
        self.tile_wan_lat.grid(row=0, column=2, padx=4, pady=2, sticky="ew")
        self.tile_wan_loss = MetricTile(f, title="● WAN Perda", value="—", unit="%", icon="⚡", accent="#3B82F6")
        self.tile_wan_loss.grid(row=0, column=3, padx=4, pady=2, sticky="ew")

        # Google (laranja)
        self.tile_ggl_lat = MetricTile(f, title="● Google Lat", value="—", unit="ms", icon="⏱", accent="#F97316")
        self.tile_ggl_lat.grid(row=0, column=4, padx=4, pady=2, sticky="ew")
        self.tile_ggl_loss = MetricTile(f, title="● Google Perda", value="—", unit="%", icon="⚡", accent="#F97316")
        self.tile_ggl_loss.grid(row=0, column=5, padx=(4, 0), pady=2, sticky="ew")

    def _build_host_grid(self):
        container = ctk.CTkFrame(self, fg_color="transparent")
        container.grid(row=3, column=0, sticky="nsew", padx=24, pady=(4, 16))
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(1, weight=1)

        filter_bar = ctk.CTkFrame(container, fg_color="transparent")
        filter_bar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        filter_bar.grid_columnconfigure(0, weight=1)

        self.search_entry = ctk.CTkEntry(
            filter_bar, placeholder_text="🔍  Buscar host por nome ou IP...",
            font=(FONTS["family"], FONTS["size_sm"]),
            fg_color=COLORS["bg_secondary"], border_color=COLORS["border"],
            text_color=COLORS["text_primary"], placeholder_text_color=COLORS["text_muted"],
            corner_radius=8, height=36)
        self.search_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.search_entry.bind("<KeyRelease>", self._on_search)

        self.filter_var = ctk.StringVar(value="Todos")
        self.filter_menu = ctk.CTkSegmentedButton(
            filter_bar, values=["Todos", "Online", "Offline"],
            variable=self.filter_var, command=self._on_filter,
            font=(FONTS["family"], FONTS["size_xs"]),
            fg_color=COLORS["bg_secondary"], selected_color=COLORS["accent_blue"],
            selected_hover_color=COLORS["accent_blue_hover"],
            unselected_color=COLORS["bg_secondary"], unselected_hover_color=COLORS["bg_tertiary"],
            text_color=COLORS["text_secondary"], corner_radius=8)
        self.filter_menu.grid(row=0, column=1, padx=(8, 0))

        # v2.4: Filtro por grupo (Todos / Lojas / Setores / etc)
        self.group_filter_var = ctk.StringVar(value="Todos")
        self.group_filter = ctk.CTkOptionMenu(
            filter_bar, variable=self.group_filter_var,
            values=["Todos"],
            command=self._on_group_filter,
            font=(FONTS["family"], FONTS["size_xs"]),
            fg_color=COLORS["bg_secondary"],
            button_color=COLORS["accent_purple"],
            button_hover_color="#7C3AED",
            dropdown_fg_color=COLORS["bg_secondary"],
            dropdown_text_color=COLORS["text_primary"],
            dropdown_hover_color=COLORS["bg_tertiary"],
            text_color=COLORS["text_secondary"],
            corner_radius=8, height=36, width=120,
        )
        self.group_filter.grid(row=0, column=2, padx=(8, 0))

        ctk.CTkButton(
            filter_bar, text="✕", width=32,
            font=(FONTS["family"], FONTS["size_sm"]),
            fg_color="transparent", hover_color=COLORS["bg_tertiary"],
            border_width=1, border_color=COLORS["border"],
            text_color=COLORS["text_muted"], corner_radius=8, height=36,
            command=self._clear_filters).grid(row=0, column=3, padx=(4, 0))

        self.hosts_scroll = ctk.CTkScrollableFrame(
            container, fg_color="transparent",
            scrollbar_button_color=COLORS["scrollbar"],
            scrollbar_button_hover_color=COLORS["scrollbar_hover"])
        self.hosts_scroll.grid(row=1, column=0, sticky="nsew")
        self.hosts_scroll.grid_columnconfigure(0, weight=1)
        self.hosts_scroll.grid_columnconfigure(1, weight=1)
        bind_mousewheel_scroll(self.hosts_scroll)

    def _on_search(self, event=None): self.refresh_hosts()
    def _on_filter(self, value): self.refresh_hosts()
    def _on_group_filter(self, value): self.refresh_hosts()
    def _clear_filters(self):
        self.search_entry.delete(0, "end")
        self.filter_var.set("Todos")
        self.group_filter_var.set("Todos")
        self.refresh_hosts()

    def _update_group_filter_options(self):
        """Atualiza as opções do filtro de grupo baseado nos hosts atuais."""
        if not self.controller:
            return
        groups = set()
        for h in self.controller.get_all_hosts():
            if h.enabled and h.group_name:
                groups.add(h.group_name)
        options = ["Todos"] + sorted(groups)
        try:
            self.group_filter.configure(values=options)
        except Exception:
            pass

    def _best_metrics(self, host) -> tuple[float, float]:
        """
        Retorna (latência, perda) com prioridade: WAN > Google > HOST.
        Mostra o dado mais relevante para o operador no card do dashboard.
        """
        if host.wan_has_data and host.wan_latency > 0:
            return host.wan_latency, host.wan_loss
        if host.google_has_data and host.google_latency > 0:
            return host.google_latency, host.google_loss
        return host.host_ssh_latency, host.host_ssh_loss

    def refresh_hosts(self):
        if not self.controller:
            return
        for w in self.hosts_scroll.winfo_children():
            w.destroy()
        self._host_cards.clear()

        # Atualiza opções do filtro de grupo
        self._update_group_filter_options()

        hosts = self.controller.get_all_hosts()
        search = self.search_entry.get().lower().strip()
        filt = self.filter_var.get()
        group_filt = self.group_filter_var.get()
        filtered = [h for h in hosts if h.enabled
                    and (not search or search in h.ip.lower() or search in h.display_name.lower())
                    and (filt == "Todos" or (filt == "Online" and h.status == "online")
                         or (filt == "Offline" and h.status == "offline"))
                    and (group_filt == "Todos" or h.group_name == group_filt)]

        groups: dict[str, list] = {}
        for h in filtered:
            groups.setdefault(h.group_name, []).append(h)

        row = 0
        for gname, ghosts in sorted(groups.items()):
            ctk.CTkLabel(
                self.hosts_scroll, text=f"  {gname}  ({len(ghosts)})",
                font=(FONTS["family"], FONTS["size_xs"], "bold"),
                text_color=COLORS["text_secondary"], anchor="w"
            ).grid(row=row, column=0, columnspan=2, sticky="w", padx=4, pady=(12, 4))
            row += 1
            col = 0
            for host in sorted(ghosts, key=lambda x: x.ip):
                lat, loss = self._best_metrics(host)
                from utils.device_profiles import get_platform_label
                card = StatusCard(
                    self.hosts_scroll,
                    host_data={"id": host.id, "ip": host.ip, "label": host.display_name,
                               "status": host.status, "latency": lat,
                               "loss": loss, "group": host.group_name,
                               "platform_label": get_platform_label(host.platform)},
                    on_click=self._on_host_click)
                card.grid(row=row, column=col, padx=4, pady=3, sticky="ew")
                self._host_cards[host.id] = card
                col += 1
                if col >= 2:
                    col = 0; row += 1
            if col > 0:
                row += 1

    def update_host_card(self, host):
        if host.id in self._host_cards:
            lat, loss = self._best_metrics(host)
            from utils.device_profiles import get_platform_label
            self._host_cards[host.id].update_data({
                "id": host.id, "ip": host.ip, "label": host.display_name,
                "status": host.status, "latency": lat,
                "loss": loss, "group": host.group_name,
                "platform_label": get_platform_label(host.platform)})

    def _lat_color(self, v):
        if v > THRESHOLDS["latency_critical_ms"]: return COLORS["accent_red"]
        if v > THRESHOLDS["latency_warning_ms"]:  return COLORS["accent_yellow"]
        return COLORS["text_primary"]

    def _loss_color(self, v):
        if v > THRESHOLDS["loss_critical_pct"]: return COLORS["accent_red"]
        if v > THRESHOLDS["loss_warning_pct"]:  return COLORS["accent_yellow"]
        return COLORS["text_primary"]

    def update_summary(self, summary: dict):
        self.tile_total.set_value(str(summary.get("total", 0)))
        self.tile_online.set_value(str(summary.get("online", 0)), COLORS["accent_green"])
        off = summary.get("offline", 0)
        self.tile_offline.set_value(str(off), COLORS["accent_red"] if off > 0 else COLORS["text_primary"])
        alerts = summary.get("active_alerts", 0)
        self.tile_alerts.set_value(str(alerts), COLORS["accent_red"] if alerts > 0 else COLORS["text_primary"])

        # HOST
        hl = summary.get("host_avg_lat", 0)
        self.tile_host_lat.set_value(f"{hl:.0f}" if hl else "—", self._lat_color(hl))
        hloss = summary.get("host_avg_loss", 0)
        self.tile_host_loss.set_value(f"{hloss:.1f}" if hloss is not None else "—", self._loss_color(hloss or 0))

        # WAN
        wl = summary.get("wan_avg_lat", 0)
        self.tile_wan_lat.set_value(f"{wl:.0f}" if wl else "—", self._lat_color(wl))
        wloss = summary.get("wan_avg_loss", 0)
        self.tile_wan_loss.set_value(f"{wloss:.1f}" if wloss is not None else "—", self._loss_color(wloss or 0))

        # Google
        gl = summary.get("google_avg_lat", 0)
        self.tile_ggl_lat.set_value(f"{gl:.0f}" if gl else "—", self._lat_color(gl))
        gloss = summary.get("google_avg_loss", 0)
        self.tile_ggl_loss.set_value(f"{gloss:.1f}" if gloss is not None else "—", self._loss_color(gloss or 0))

    def set_monitoring_status(self, running: bool, paused: bool = False):
        if paused:
            self.running_indicator.configure(text="⏸ PAUSADO", text_color=COLORS["accent_yellow"])
        elif running:
            self.running_indicator.configure(text="● MONITORANDO", text_color=COLORS["accent_green"])
        else:
            self.running_indicator.configure(text="○ PARADO", text_color=COLORS["text_muted"])

    def _update_clock(self):
        self.clock_label.configure(text=datetime.now().strftime("%H:%M:%S"))
        self.after(1000, self._update_clock)