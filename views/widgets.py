"""
Widgets customizados reutilizáveis para a interface.

CORREÇÕES APLICADAS:
  1. bind_mousewheel_scroll: SCROLL_UNITS aumentado de 1 para 5 — scroll
     visivelmente mais fluido. O delta agora é normalizado corretamente
     tanto para rodas de mouse padrão (múltiplos de 120) quanto para
     trackpads de alta precisão (delta < 120).
  2. rebind_mousewheel_scroll: nova função síncrona (sem after) para
     reaplicar o binding imediatamente após popular listas dinâmicas.
     Importada em logs_view.py após construir a tabela do Monitor ao Vivo.
  3. after(100) em vez de after(50) no bind_mousewheel_scroll para garantir
     que o layout dos filhos esteja completo antes dos bindings serem aplicados.
"""
import tkinter as tk
import customtkinter as ctk
import math
from config import COLORS, FONTS


def bind_mousewheel_scroll(container, target=None):
    """
    Habilita scroll suave por roda do mouse em CTkScrollableFrame, CTkTextbox
    ou qualquer widget equivalente, inclusive ao passar o mouse sobre os filhos.

    Parâmetros:
      container — widget que receberá o binding (e seus filhos recursivamente).
      target    — widget real que executa o yview_scroll; se None, resolve
                  automaticamente via _parent_canvas ou _textbox.
    """
    scroll_target = (
        target
        or getattr(container, "_parent_canvas", None)
        or getattr(container, "_textbox", None)
        or container
    )

    # CORREÇÃO: 5 unidades por notch — scroll perceptivelmente mais fluido.
    # O valor original (1) tornava o scroll quase imperceptível.
    SCROLL_UNITS = 30

    def _on_mousewheel(event):
        raw = getattr(event, "delta", 0)
        delta = 0
        if raw:
            # Windows/macOS: múltiplos de 120. Trackpads enviam valores menores.
            delta = -int(raw / 120) if abs(raw) >= 120 else (-1 if raw > 0 else 1)
        elif getattr(event, "num", None) == 4:
            delta = -1   # Linux — scroll up
        elif getattr(event, "num", None) == 5:
            delta = 1    # Linux — scroll down

        if delta == 0:
            return None

        try:
            scroll_target.yview_scroll(delta * SCROLL_UNITS, "units")
            return "break"   # impede propagação para widgets pai
        except Exception:
            return None

    def _bind_recursive(widget):
        """Aplica o binding neste widget e em todos os seus descendentes."""
        try:
            widget.bind("<MouseWheel>", _on_mousewheel, add="+")
            widget.bind("<Button-4>",   _on_mousewheel, add="+")
            widget.bind("<Button-5>",   _on_mousewheel, add="+")
        except Exception:
            return
        try:
            for child in widget.winfo_children():
                _bind_recursive(child)
        except Exception:
            pass

    # CORREÇÃO: 100 ms garante que o layout dos filhos já terminou
    # de ser criado antes de aplicarmos os bindings recursivamente.
    container.after(100, lambda: _bind_recursive(container))


def rebind_mousewheel_scroll(container, target=None):
    """
    Versão SÍNCRONA do bind_mousewheel_scroll (sem after).

    Use esta função após adicionar novos filhos dinamicamente a um container
    já existente — por exemplo, ao popular a tabela do Monitor ao Vivo.
    O after(100) do bind_mousewheel_scroll não cobriria os novos widgets,
    pois eles ainda não existiam quando o timer foi disparado.
    """
    scroll_target = (
        target
        or getattr(container, "_parent_canvas", None)
        or getattr(container, "_textbox", None)
        or container
    )
    SCROLL_UNITS = 30

    def _on_mousewheel(event):
        raw = getattr(event, "delta", 0)
        delta = 0
        if raw:
            delta = -int(raw / 120) if abs(raw) >= 120 else (-1 if raw > 0 else 1)
        elif getattr(event, "num", None) == 4:
            delta = -1
        elif getattr(event, "num", None) == 5:
            delta = 1
        if delta == 0:
            return None
        try:
            scroll_target.yview_scroll(delta * SCROLL_UNITS, "units")
            return "break"
        except Exception:
            return None

    def _bind_recursive(widget):
        try:
            widget.bind("<MouseWheel>", _on_mousewheel, add="+")
            widget.bind("<Button-4>",   _on_mousewheel, add="+")
            widget.bind("<Button-5>",   _on_mousewheel, add="+")
        except Exception:
            return
        try:
            for child in widget.winfo_children():
                _bind_recursive(child)
        except Exception:
            pass

    # Executa imediatamente — sem after
    _bind_recursive(container)


class StatusCard(ctk.CTkFrame):
    """Card compacto que mostra status de um host com indicador visual."""

    def __init__(self, master, host_data: dict = None, on_click=None, **kwargs):
        super().__init__(
            master, fg_color=COLORS["bg_secondary"],
            corner_radius=12, border_width=1, border_color=COLORS["border"],
            **kwargs,
        )
        self.host_data = host_data or {}
        self._on_click = on_click
        self._hovered = False

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        if on_click:
            self.bind("<Button-1>", lambda e: on_click(self.host_data))
        self._build()

    def _build(self):
        self.grid_columnconfigure(1, weight=1)
        status = self.host_data.get("status", "unknown")
        color = {"online": COLORS["accent_green"], "offline": COLORS["accent_red"]}.get(
            status, COLORS["text_muted"])

        self.status_dot = ctk.CTkLabel(
            self, text="●", font=(FONTS["family"], 18), text_color=color, width=30)
        self.status_dot.grid(row=0, column=0, rowspan=2, padx=(12, 4), pady=8)

        label = self.host_data.get("label") or self.host_data.get("ip", "—")
        self.name_label = ctk.CTkLabel(
            self, text=label,
            font=(FONTS["family"], FONTS["size_md"], "bold"),
            text_color=COLORS["text_primary"], anchor="w")
        self.name_label.grid(row=0, column=1, sticky="sw", padx=4, pady=(8, 0))

        ip = self.host_data.get("ip", "")
        latency = self.host_data.get("latency", 0)
        loss = self.host_data.get("loss", 0)
        platform_label = self.host_data.get("platform_label", "")
        plat_str = f"  ·  {platform_label}" if platform_label else ""
        info = f"{ip}  ·  {latency:.0f}ms  ·  {loss:.0f}% perda{plat_str}" if status == "online" else f"{ip}  ·  Offline{plat_str}"
        self.info_label = ctk.CTkLabel(
            self, text=info,
            font=(FONTS["family_mono"], FONTS["size_xs"]),
            text_color=COLORS["text_secondary"], anchor="w")
        self.info_label.grid(row=1, column=1, sticky="nw", padx=4, pady=(0, 8))

        self.arrow = ctk.CTkLabel(
            self, text="›", font=(FONTS["family"], 20),
            text_color=COLORS["text_muted"], width=30)
        self.arrow.grid(row=0, column=2, rowspan=2, padx=(0, 12), pady=8)

        for widget in [self.status_dot, self.name_label, self.info_label, self.arrow]:
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)
            if self._on_click:
                widget.bind("<Button-1>", lambda e: self._on_click(self.host_data))

    def _on_enter(self, event):
        if not self._hovered:
            self._hovered = True
            self.configure(fg_color=COLORS["bg_tertiary"], border_color=COLORS["border_focus"])
            self.arrow.configure(text_color=COLORS["accent_blue"])

    def _on_leave(self, event):
        if self._hovered:
            self._hovered = False
            self.configure(fg_color=COLORS["bg_secondary"], border_color=COLORS["border"])
            self.arrow.configure(text_color=COLORS["text_muted"])

    def update_data(self, host_data: dict):
        self.host_data = host_data
        status = host_data.get("status", "unknown")
        color = {"online": COLORS["accent_green"], "offline": COLORS["accent_red"]}.get(
            status, COLORS["text_muted"])
        self.status_dot.configure(text_color=color)
        self.name_label.configure(text=host_data.get("label") or host_data.get("ip", "—"))
        ip = host_data.get("ip", "")
        latency = host_data.get("latency", 0)
        loss = host_data.get("loss", 0)
        platform_label = host_data.get("platform_label", "")
        plat_str = f"  ·  {platform_label}" if platform_label else ""
        if status == "online":
            info = f"{ip}  ·  {latency:.0f}ms  ·  {loss:.0f}% perda{plat_str}"
        else:
            info = f"{ip}  ·  Offline{plat_str}"
        self.info_label.configure(text=info)


class MetricTile(ctk.CTkFrame):
    """Tile de métrica com valor grande, label e indicador de tendência."""

    def __init__(self, master, title="", value="—", unit="", accent=None, icon="", **kwargs):
        super().__init__(
            master, fg_color=COLORS["bg_secondary"],
            corner_radius=12, border_width=1, border_color=COLORS["border"],
            **kwargs,
        )
        self._accent = accent or COLORS["accent_blue"]
        self.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 4))
        header.grid_columnconfigure(1, weight=1)

        if icon:
            ctk.CTkLabel(
                header, text=icon, font=(FONTS["family"], FONTS["size_md"]),
                text_color=self._accent,
            ).grid(row=0, column=0, padx=(0, 6))

        self.title_label = ctk.CTkLabel(
            header, text=title.upper(),
            font=(FONTS["family"], FONTS["size_xs"], "bold"),
            text_color=COLORS["text_secondary"], anchor="w",
        )
        self.title_label.grid(row=0, column=1, sticky="w")

        val_frame = ctk.CTkFrame(self, fg_color="transparent")
        val_frame.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 12))

        self.value_label = ctk.CTkLabel(
            val_frame, text=value,
            font=(FONTS["family"], FONTS["size_xxl"], "bold"),
            text_color=COLORS["text_primary"],
        )
        self.value_label.pack(side="left")

        if unit:
            self.unit_label = ctk.CTkLabel(
                val_frame, text=unit,
                font=(FONTS["family"], FONTS["size_sm"]),
                text_color=COLORS["text_secondary"],
            )
            self.unit_label.pack(side="left", padx=(4, 0), pady=(8, 0))

    def set_value(self, value: str, color: str = None):
        self.value_label.configure(text=value, text_color=color or COLORS["text_primary"])


class SidebarButton(ctk.CTkFrame):
    """Botão estilizado para a sidebar de navegação."""

    def __init__(self, master, text="", icon="", active=False, command=None, **kwargs):
        super().__init__(master, fg_color="transparent", corner_radius=8, **kwargs)
        self._active = active
        self._command = command
        self.grid_columnconfigure(1, weight=1)

        self.icon_label = ctk.CTkLabel(
            self, text=icon, font=(FONTS["family"], FONTS["size_lg"]),
            text_color=COLORS["accent_blue"] if active else COLORS["text_secondary"],
            width=30,
        )
        self.icon_label.grid(row=0, column=0, padx=(12, 4), pady=10)

        self.text_label = ctk.CTkLabel(
            self, text=text,
            font=(FONTS["family"], FONTS["size_md"], "bold" if active else "normal"),
            text_color=COLORS["text_primary"] if active else COLORS["text_secondary"],
            anchor="w",
        )
        self.text_label.grid(row=0, column=1, sticky="w", padx=4, pady=10)

        if active:
            self.configure(fg_color=COLORS["bg_tertiary"])

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self.icon_label.bind("<Button-1>", self._on_click)
        self.text_label.bind("<Button-1>", self._on_click)

    def _on_enter(self, event):
        if not self._active:
            self.configure(fg_color=COLORS["bg_tertiary"])

    def _on_leave(self, event):
        if not self._active:
            self.configure(fg_color="transparent")

    def _on_click(self, event):
        if self._command:
            self._command()

    def set_active(self, active: bool):
        self._active = active
        if active:
            self.configure(fg_color=COLORS["bg_tertiary"])
            self.icon_label.configure(text_color=COLORS["accent_blue"])
            self.text_label.configure(
                text_color=COLORS["text_primary"],
                font=(FONTS["family"], FONTS["size_md"], "bold"))
        else:
            self.configure(fg_color="transparent")
            self.icon_label.configure(text_color=COLORS["text_secondary"])
            self.text_label.configure(
                text_color=COLORS["text_secondary"],
                font=(FONTS["family"], FONTS["size_md"]))


class MiniChart(ctk.CTkFrame):
    """Mini gráfico sparkline com labels de eixos.
    Usa CTkFrame como wrapper e tk.Canvas interno para desenho."""

    def __init__(self, master, data=None, color=None,
                 width=200, height=50,
                 label_y="", label_x="", **kwargs):
        super().__init__(
            master, fg_color=COLORS["bg_secondary"],
            corner_radius=0, border_width=0,
            **kwargs,
        )
        self._chart_w = width
        self._chart_h = height
        self._label_y = label_y
        self._label_x = label_x
        self.grid_propagate(False)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._canvas = tk.Canvas(
            self, width=self._chart_w, height=self._chart_h,
            bg=COLORS["bg_secondary"], highlightthickness=0,
            bd=0, relief="flat",
        )
        self._canvas.grid(row=0, column=0, sticky="nsew")

        self._data = data or []
        self._color = color or COLORS["chart_line1"]

        self._canvas.bind("<Configure>", self._on_resize)

    def _on_resize(self, event):
        self._chart_w = event.width
        self._chart_h = event.height
        self._render_chart()

    def update_data(self, data: list):
        self._data = data[-60:]
        self._render_chart()

    def _render_chart(self):
        """
        v2.4 FIX:
          1. Padding de 15% acima/abaixo dos valores para evitar corte.
          2. margin_bottom aumentado para 30px — label "Medições" não corta.
          3. Último ponto: label posicionado com clamp para não sair da área.
          4. Valores no eixo Y com 1 casa decimal quando range < 10.
        """
        c = self._canvas
        c.delete("all")

        w, h = self._chart_w, self._chart_h
        if w < 50 or h < 30:
            return

        margin_left   = 48
        margin_bottom = 30      # v2.4: era 20 — mais espaço para label X
        margin_top    = 18      # v2.4: era 8  — espaço para valor do último ponto
        margin_right  = 14

        chart_w = w - margin_left - margin_right
        chart_h = h - margin_top - margin_bottom

        if chart_w < 20 or chart_h < 20:
            return

        text_color = COLORS["text_muted"]
        grid_color = COLORS["chart_grid"]

        if len(self._data) < 2:
            c.create_text(
                w // 2, h // 2,
                text="Aguardando dados...",
                fill=COLORS["text_muted"],
                font=("Segoe UI", 9),
            )
            return

        data    = self._data
        raw_min = min(data)
        raw_max = max(data)
        raw_range = raw_max - raw_min

        # v2.4: padding de 15% acima e abaixo para valores não colarem nas bordas
        if raw_range == 0:
            # Todos os valores iguais — cria faixa artificial
            pad = max(abs(raw_max) * 0.15, 1.0)
        else:
            pad = raw_range * 0.15

        min_v   = raw_min - pad
        max_v   = raw_max + pad
        range_v = max_v - min_v

        # Formato do eixo Y: 1 decimal para ranges pequenos
        y_fmt = "{:.1f}" if range_v < 10 else "{:.0f}"

        # Grid lines e labels do eixo Y
        for i in range(4):
            y   = margin_top + (i / 3) * chart_h
            val = max_v - (i / 3) * range_v
            c.create_line(margin_left, y, w - margin_right, y,
                          fill=grid_color, dash=(2, 4))
            c.create_text(
                margin_left - 5, y,
                text=y_fmt.format(val),
                fill=text_color, font=("Cascadia Code", 8),
                anchor="e",
            )

        # Label eixo Y (rotacionado)
        if self._label_y:
            c.create_text(
                10, margin_top + chart_h // 2,
                text=self._label_y,
                fill=COLORS["text_secondary"],
                font=("Segoe UI", 8, "bold"),
                angle=90,
            )

        # Label eixo X — v2.4: posicionado com mais folga
        if self._label_x:
            c.create_text(
                margin_left + chart_w // 2, h - 8,
                text=self._label_x,
                fill=COLORS["text_secondary"],
                font=("Segoe UI", 8),
            )

        # Pontos da curva
        points = []
        n = len(data)
        for i, v in enumerate(data):
            x = margin_left + (i / max(1, n - 1)) * chart_w
            y = margin_top + chart_h - ((v - min_v) / range_v) * chart_h
            points.append((x, y))

        # Área preenchida
        fill_points = list(points) + [
            (points[-1][0], margin_top + chart_h),
            (points[0][0],  margin_top + chart_h),
        ]
        fill_coords = [c_val for p in fill_points for c_val in p]
        c.create_polygon(fill_coords, fill=self._color, stipple="gray25", outline="")

        # Linha da curva
        if len(points) >= 2:
            line_coords = [c_val for p in points for c_val in p]
            c.create_line(line_coords, fill=self._color, width=2, smooth=True)

        # Último ponto com valor
        if points:
            lx, ly = points[-1]
            c.create_oval(lx - 4, ly - 4, lx + 4, ly + 4,
                          fill=self._color, outline="#FFFFFF", width=1)
            # v2.4: clamp para label não sair da área visível
            label_y = max(margin_top + 8, ly - 14)
            label_x = min(lx, w - margin_right - 20)
            c.create_text(
                label_x, label_y,
                text=f"{data[-1]:.1f}",
                fill="#FFFFFF",
                font=("Cascadia Code", 8, "bold"),
            )


class AlertBadge(ctk.CTkLabel):
    """Badge de contagem de alertas."""

    def __init__(self, master, count=0, **kwargs):
        super().__init__(
            master,
            text=str(count) if count > 0 else "",
            font=(FONTS["family"], 9, "bold"),
            text_color="#FFFFFF",
            fg_color=COLORS["accent_red"] if count > 0 else "transparent",
            corner_radius=10, width=20, height=20,
            **kwargs,
        )

    def set_count(self, count: int):
        if count > 0:
            self.configure(
                text=str(count) if count < 100 else "99+",
                fg_color=COLORS["accent_red"],
            )
        else:
            self.configure(text="", fg_color="transparent")


class TooltipLabel(ctk.CTkLabel):
    """Label com tooltip ao passar o mouse."""

    def __init__(self, master, tooltip_text="", **kwargs):
        super().__init__(master, **kwargs)
        self._tooltip_text = tooltip_text
        self._tooltip_window = None
        self.bind("<Enter>", self._show_tooltip)
        self.bind("<Leave>", self._hide_tooltip)

    def _show_tooltip(self, event):
        if not self._tooltip_text:
            return
        x = self.winfo_rootx() + 20
        y = self.winfo_rooty() + self.winfo_height() + 5
        self._tooltip_window = tw = ctk.CTkToplevel(self)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.configure(fg_color=COLORS["bg_elevated"])
        ctk.CTkLabel(
            tw, text=self._tooltip_text,
            font=(FONTS["family"], FONTS["size_xs"]),
            text_color=COLORS["text_primary"],
            fg_color=COLORS["bg_elevated"],
            corner_radius=6, padx=8, pady=4,
        ).pack()

    def _hide_tooltip(self, event):
        if self._tooltip_window:
            self._tooltip_window.destroy()
            self._tooltip_window = None


class ZoomBar(ctk.CTkFrame):
    """
    Barra de zoom compacta: [ - ] 100% [ + ]
    Range: 100% a 130%, passo de 5%.
    Chama on_zoom(level: int) quando muda.
    """
    MIN_ZOOM = 100
    MAX_ZOOM = 130
    STEP = 5

    def __init__(self, master, on_zoom=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._zoom = 100
        self._on_zoom = on_zoom

        btn_style = dict(
            width=28, height=24,
            font=(FONTS["family"], 14, "bold"),
            fg_color=COLORS["bg_tertiary"],
            hover_color=COLORS["bg_elevated"],
            corner_radius=4,
        )

        self.minus_btn = ctk.CTkButton(
            self, text="−", text_color=COLORS["text_secondary"],
            command=self._zoom_out, **btn_style)
        self.minus_btn.pack(side="left", padx=1)

        self.label = ctk.CTkLabel(
            self, text="100%", width=46,
            font=(FONTS["family_mono"], FONTS["size_xs"]),
            text_color=COLORS["text_secondary"])
        self.label.pack(side="left", padx=2)

        self.plus_btn = ctk.CTkButton(
            self, text="+", text_color=COLORS["text_secondary"],
            command=self._zoom_in, **btn_style)
        self.plus_btn.pack(side="left", padx=1)

        self._update_buttons()

    @property
    def zoom(self) -> int:
        return self._zoom

    def _zoom_in(self):
        if self._zoom < self.MAX_ZOOM:
            self._zoom += self.STEP
            self._apply()

    def _zoom_out(self):
        if self._zoom > self.MIN_ZOOM:
            self._zoom -= self.STEP
            self._apply()

    def _apply(self):
        self.label.configure(text=f"{self._zoom}%")
        self._update_buttons()
        if self._on_zoom:
            self._on_zoom(self._zoom)

    def _update_buttons(self):
        self.minus_btn.configure(
            state="normal" if self._zoom > self.MIN_ZOOM else "disabled")
        self.plus_btn.configure(
            state="normal" if self._zoom < self.MAX_ZOOM else "disabled")

# ══════════════════════════════════════════════════════════════════════
# CHART WIDGETS — v2.12 (para Reliability View)
# ══════════════════════════════════════════════════════════════════════

class BarChart(ctk.CTkFrame):
    """
    Gráfico de barras horizontal com labels e valores.

    Usado para Pareto, cohort analysis, top contributors.
    Cada barra é colorida pelo valor (verde→amarelo→vermelho).
    """
    def __init__(self, master, data=None, max_bars=10,
                 value_format="{:.1f}", color_scheme="reverse",
                 title="", height=200, **kwargs):
        from config import COLORS, FONTS
        super().__init__(master, fg_color=COLORS["bg_primary"],
                         corner_radius=8, **kwargs)
        self._data = data or []
        self._max_bars = max_bars
        self._fmt = value_format
        self._color_scheme = color_scheme  # "reverse" = alto é ruim
        self._title = title
        self._height = height
        self._build()

    def _build(self):
        from config import COLORS, FONTS
        if self._title:
            ctk.CTkLabel(self, text=self._title,
                         font=(FONTS["family"], FONTS["size_sm"], "bold"),
                         text_color=COLORS["text_primary"]).pack(
                anchor="w", padx=12, pady=(8, 4))

        if not self._data:
            ctk.CTkLabel(self, text="Sem dados",
                         font=(FONTS["family"], FONTS["size_xs"]),
                         text_color=COLORS["text_muted"]).pack(pady=20)
            return

        # Calcula valor máximo para normalizar largura
        values = [d.get("value", 0) for d in self._data[:self._max_bars]]
        max_val = max(values) if values else 1
        if max_val == 0:
            max_val = 1

        for d in self._data[:self._max_bars]:
            label = d.get("label", "—")
            value = d.get("value", 0)
            pct_width = (value / max_val) * 100

            # Cor pelo valor
            if self._color_scheme == "reverse":
                # Alto = ruim
                if pct_width >= 70:
                    color = COLORS["accent_red"]
                elif pct_width >= 40:
                    color = COLORS["accent_yellow"]
                else:
                    color = COLORS["accent_green"]
            else:
                # Alto = bom
                if pct_width >= 70:
                    color = COLORS["accent_green"]
                elif pct_width >= 40:
                    color = COLORS["accent_yellow"]
                else:
                    color = COLORS["accent_red"]

            row = ctk.CTkFrame(self, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=2)
            row.grid_columnconfigure(0, weight=0, minsize=180)
            row.grid_columnconfigure(1, weight=1)
            row.grid_columnconfigure(2, weight=0, minsize=80)

            ctk.CTkLabel(row, text=label[:24],
                         font=(FONTS["family"], FONTS["size_xs"]),
                         text_color=COLORS["text_primary"], anchor="w"
                         ).grid(row=0, column=0, sticky="w")

            bar_frame = ctk.CTkFrame(row, fg_color=COLORS["bg_secondary"],
                                      corner_radius=4, height=18)
            bar_frame.grid(row=0, column=1, sticky="ew", padx=8)
            bar_frame.grid_propagate(False)
            inner_w = max(1, int(pct_width * 2.5))  # ratio
            ctk.CTkFrame(bar_frame, fg_color=color, corner_radius=4,
                          width=inner_w, height=18).place(x=0, y=0)

            ctk.CTkLabel(row, text=self._fmt.format(value),
                         font=(FONTS["family_mono"], FONTS["size_xs"]),
                         text_color=COLORS["text_secondary"], anchor="e"
                         ).grid(row=0, column=2, sticky="e")

    def update_data(self, data: list):
        self._data = data
        for w in self.winfo_children():
            w.destroy()
        self._build()


class TrendLineChart(ctk.CTkFrame):
    """
    Gráfico de linha simples usando Canvas para mostrar tendência temporal.

    Recebe lista de pontos com {x_label, value} e desenha linha + área.
    Útil para mostrar latência ao longo de dias.
    """
    def __init__(self, master, data=None, title="", y_label="",
                 color=None, height=180, threshold=None, **kwargs):
        from config import COLORS, FONTS
        super().__init__(master, fg_color=COLORS["bg_primary"],
                         corner_radius=8, **kwargs)
        self._data = data or []
        self._title = title
        self._y_label = y_label
        self._color = color or COLORS["accent_blue"]
        self._height = height
        self._threshold = threshold  # linha horizontal opcional (ex: SLA target)
        self._build()

    def _build(self):
        from config import COLORS, FONTS
        if self._title:
            ctk.CTkLabel(self, text=self._title,
                         font=(FONTS["family"], FONTS["size_sm"], "bold"),
                         text_color=COLORS["text_primary"]).pack(
                anchor="w", padx=12, pady=(8, 4))

        import tkinter as tk
        canvas_w = 600
        canvas_h = self._height
        self.canvas = tk.Canvas(self, width=canvas_w, height=canvas_h,
                                  bg=COLORS["bg_primary"], highlightthickness=0)
        self.canvas.pack(fill="x", padx=12, pady=8)

        if not self._data or len(self._data) < 2:
            self.canvas.create_text(canvas_w//2, canvas_h//2,
                text="Sem dados suficientes", fill=COLORS["text_muted"],
                font=(FONTS["family"], 10))
            return

        values = [d.get("value", 0) for d in self._data]
        max_v = max(values + [self._threshold or 0])
        min_v = min(values + [0])
        if max_v == min_v:
            max_v = min_v + 1

        margin_l = 50
        margin_r = 10
        margin_t = 10
        margin_b = 30
        plot_w = canvas_w - margin_l - margin_r
        plot_h = canvas_h - margin_t - margin_b

        # Eixo Y — gridlines + labels
        for i in range(5):
            y = margin_t + (plot_h * i / 4)
            v = max_v - (max_v - min_v) * i / 4
            self.canvas.create_line(margin_l, y, canvas_w - margin_r, y,
                                      fill=COLORS["border"], dash=(2, 4))
            self.canvas.create_text(margin_l - 5, y, text=f"{v:.0f}",
                                      anchor="e", fill=COLORS["text_muted"],
                                      font=(FONTS["family"], 8))

        # Threshold line (se fornecido)
        if self._threshold is not None and self._threshold <= max_v:
            ty = margin_t + plot_h * (1 - (self._threshold - min_v) / (max_v - min_v))
            self.canvas.create_line(margin_l, ty, canvas_w - margin_r, ty,
                                      fill=COLORS["accent_red"], dash=(5, 3), width=1)

        # Plota linha + área
        n = len(self._data)
        coords = []
        for i, d in enumerate(self._data):
            x = margin_l + (plot_w * i / max(n - 1, 1))
            v = d.get("value", 0)
            y = margin_t + plot_h * (1 - (v - min_v) / (max_v - min_v))
            coords.extend([x, y])

        if len(coords) >= 4:
            # Área (polígono fechado até o eixo X)
            area_coords = coords + [margin_l + plot_w, margin_t + plot_h,
                                     margin_l, margin_t + plot_h]
            self.canvas.create_polygon(*area_coords,
                fill=self._color, outline="", stipple="gray25")
            # Linha
            self.canvas.create_line(*coords, fill=self._color, width=2,
                                      smooth=True)

        # Labels do eixo X (primeira, do meio, última)
        for idx in [0, n // 2, n - 1] if n >= 3 else [0, n - 1]:
            if idx < n:
                x = margin_l + (plot_w * idx / max(n - 1, 1))
                label = self._data[idx].get("x_label", str(idx))
                self.canvas.create_text(x, canvas_h - margin_b + 5,
                    text=str(label)[:8], anchor="n",
                    fill=COLORS["text_muted"], font=(FONTS["family"], 8))

    def update_data(self, data: list):
        self._data = data
        for w in self.winfo_children():
            w.destroy()
        self._build()


class PercentileBars(ctk.CTkFrame):
    """
    Mostra p50/p95/p99 lado a lado com cores graduais.
    """
    def __init__(self, master, p50=0, p95=0, p99=0, max_val=None,
                 title="", unit="ms", **kwargs):
        from config import COLORS, FONTS
        super().__init__(master, fg_color=COLORS["bg_primary"],
                         corner_radius=8, **kwargs)
        self._build(p50, p95, p99, max_val, title, unit)

    def _build(self, p50, p95, p99, max_val, title, unit):
        from config import COLORS, FONTS

        ctk.CTkLabel(self, text=title,
                     font=(FONTS["family"], FONTS["size_sm"], "bold"),
                     text_color=COLORS["text_primary"]).pack(
            anchor="w", padx=12, pady=(8, 4))

        if max_val is None:
            max_val = max(p99, p95, p50, 1)

        grid = ctk.CTkFrame(self, fg_color="transparent")
        grid.pack(fill="x", padx=12, pady=(0, 10))
        grid.grid_columnconfigure((0, 1, 2), weight=1)

        for i, (label, val, color) in enumerate([
            ("p50 (mediana)", p50, COLORS["accent_green"]),
            ("p95", p95, COLORS["accent_yellow"]),
            ("p99 (pior)", p99, COLORS["accent_red"]),
        ]):
            cell = ctk.CTkFrame(grid, fg_color=COLORS["bg_secondary"],
                                 corner_radius=8)
            cell.grid(row=0, column=i, padx=4, pady=2, sticky="ew")
            ctk.CTkLabel(cell, text=label,
                         font=(FONTS["family"], FONTS["size_xs"]),
                         text_color=COLORS["text_muted"]).pack(pady=(8, 0))
            ctk.CTkLabel(cell, text=f"{val:.1f} {unit}",
                         font=(FONTS["family_mono"], FONTS["size_md"], "bold"),
                         text_color=color).pack(pady=(0, 8))
