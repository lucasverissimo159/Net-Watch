"""
SSH Terminal View v2.4 — até 5 terminais simultâneos com abas.

ALTERAÇÕES v2.4:
  1. Multi-tab: até 5 conexões SSH simultâneas em abas.
  2. Histórico de comandos: ↑/↓ para navegar comandos anteriores (por aba).
  3. Botão "Limpar" para limpar o terminal sem desconectar.
  4. Mais atalhos rápidos organizados em 2 linhas.
  5. Botão + para abrir nova aba, × para fechar.
  6. Auto-preenche credenciais do host cadastrado ou usa padrão.
"""
import customtkinter as ctk
import threading
import re

from config import COLORS, FONTS, get_ssh_credentials
from controllers.ssh_controller import SSHController


MAX_TABS = 5
MAX_CMD_HISTORY = 100
BASE_TERM_FONT_SIZE = FONTS["size_sm"]


class _SSHTab:
    """Estado de uma aba SSH individual."""

    def __init__(self):
        self.session = None
        self.host_ip = ""
        self.connected = False
        self.cmd_history: list[str] = []
        self.history_index: int = -1
        self.frame: ctk.CTkFrame = None
        # Widgets (preenchidos ao construir)
        self.host_entry = None
        self.user_entry = None
        self.pass_entry = None
        self.port_entry = None
        self.connect_btn = None
        self.disconnect_btn = None
        self.status_label = None
        self.terminal = None
        self.cmd_entry = None


class SSHView(ctk.CTkFrame):
    """Interface de terminal SSH com até 5 abas simultâneas."""

    def __init__(self, master, ssh_controller: SSHController = None,
                 monitor_controller=None, **kwargs):
        super().__init__(master, fg_color=COLORS["bg_primary"], **kwargs)
        self.ssh = ssh_controller or SSHController()
        self.monitor = monitor_controller

        self._tabs: list[_SSHTab] = []
        self._current_tab_idx: int = 0
        self._zoom = 100

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_tab_bar()
        self._build_content_area()

        # Cria a primeira aba
        self._add_tab()

    def set_monitor_controller(self, controller):
        self.monitor = controller

    # ══════════════════════════════════════════════════════════════════
    # TAB BAR
    # ══════════════════════════════════════════════════════════════════

    def _build_tab_bar(self):
        self._tab_bar = ctk.CTkFrame(self, fg_color=COLORS["bg_secondary"],
            corner_radius=0, height=38, border_width=0)
        self._tab_bar.grid(row=0, column=0, sticky="ew", padx=24, pady=(16, 0))
        self._tab_bar.grid_propagate(False)
        self._tab_buttons: list[ctk.CTkButton] = []

    def _refresh_tab_bar(self):
        for w in self._tab_bar.winfo_children():
            w.destroy()
        self._tab_buttons.clear()

        for i, tab in enumerate(self._tabs):
            label = tab.host_ip or f"Terminal {i+1}"
            if tab.connected:
                label = f"● {label}"

            is_active = (i == self._current_tab_idx)
            btn = ctk.CTkButton(
                self._tab_bar,
                text=label,
                width=140,
                height=30,
                font=(FONTS["family_mono"], FONTS["size_xs"],
                      "bold" if is_active else "normal"),
                fg_color=COLORS["bg_tertiary"] if is_active else "transparent",
                hover_color=COLORS["bg_elevated"],
                text_color=COLORS["accent_green"] if tab.connected
                    else (COLORS["text_primary"] if is_active else COLORS["text_secondary"]),
                corner_radius=6,
                command=lambda idx=i: self._switch_tab(idx),
            )
            btn.pack(side="left", padx=2, pady=4)
            self._tab_buttons.append(btn)

            # Botão × para fechar (só se mais de 1 aba)
            if len(self._tabs) > 1:
                close_btn = ctk.CTkButton(
                    self._tab_bar,
                    text="×",
                    width=22, height=22,
                    font=(FONTS["family"], 12, "bold"),
                    fg_color="transparent",
                    hover_color=COLORS["accent_red_dim"],
                    text_color=COLORS["text_muted"],
                    corner_radius=4,
                    command=lambda idx=i: self._close_tab(idx),
                )
                close_btn.pack(side="left", padx=(0, 6), pady=4)

        # Botão + nova aba (se < MAX_TABS)
        if len(self._tabs) < MAX_TABS:
            add_btn = ctk.CTkButton(
                self._tab_bar,
                text="+",
                width=30, height=30,
                font=(FONTS["family"], 16, "bold"),
                fg_color="transparent",
                hover_color=COLORS["accent_blue"],
                text_color=COLORS["accent_blue"],
                corner_radius=6,
                command=self._add_tab,
            )
            add_btn.pack(side="left", padx=4, pady=4)

        # Contador + Zoom
        right_frame = ctk.CTkFrame(self._tab_bar, fg_color="transparent")
        right_frame.pack(side="right", padx=8, pady=4)

        ctk.CTkLabel(
            right_frame,
            text=f"{len(self._tabs)}/{MAX_TABS}",
            font=(FONTS["family_mono"], FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
        ).pack(side="left", padx=(0, 8))

        # Zoom
        ctk.CTkButton(
            right_frame, text="−", width=24, height=22,
            font=(FONTS["family"], 12, "bold"),
            fg_color=COLORS["bg_tertiary"], hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"], corner_radius=4,
            command=self._zoom_out,
        ).pack(side="left", padx=1)
        self._zoom_label = ctk.CTkLabel(
            right_frame, text=f"{self._zoom}%", width=40,
            font=(FONTS["family_mono"], 9),
            text_color=COLORS["text_muted"])
        self._zoom_label.pack(side="left", padx=1)
        ctk.CTkButton(
            right_frame, text="+", width=24, height=22,
            font=(FONTS["family"], 12, "bold"),
            fg_color=COLORS["bg_tertiary"], hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"], corner_radius=4,
            command=self._zoom_in,
        ).pack(side="left", padx=1)

    # ══════════════════════════════════════════════════════════════════
    # CONTENT AREA (onde o frame da aba ativa é exibido)
    # ══════════════════════════════════════════════════════════════════

    def _build_content_area(self):
        self._content = ctk.CTkFrame(self, fg_color="transparent")
        self._content.grid(row=1, column=0, sticky="nsew", padx=0, pady=0)
        self._content.grid_columnconfigure(0, weight=1)
        self._content.grid_rowconfigure(0, weight=1)

    # ══════════════════════════════════════════════════════════════════
    # TAB LIFECYCLE
    # ══════════════════════════════════════════════════════════════════

    def _add_tab(self):
        if len(self._tabs) >= MAX_TABS:
            return
        tab = _SSHTab()
        tab.frame = self._build_tab_frame(tab)
        self._tabs.append(tab)
        self._current_tab_idx = len(self._tabs) - 1
        self._show_tab(self._current_tab_idx)
        self._refresh_tab_bar()

    def _close_tab(self, idx):
        if idx < 0 or idx >= len(self._tabs):
            return
        tab = self._tabs[idx]
        if tab.session:
            tab.session.disconnect()
        tab.frame.destroy()
        self._tabs.pop(idx)
        if self._current_tab_idx >= len(self._tabs):
            self._current_tab_idx = max(0, len(self._tabs) - 1)
        if self._tabs:
            self._show_tab(self._current_tab_idx)
        self._refresh_tab_bar()

    def _switch_tab(self, idx):
        if idx == self._current_tab_idx:
            return
        self._current_tab_idx = idx
        self._show_tab(idx)
        self._refresh_tab_bar()

    def _show_tab(self, idx):
        # Esconde todas
        for tab in self._tabs:
            tab.frame.grid_remove()
        # Mostra a ativa
        if idx < len(self._tabs):
            self._tabs[idx].frame.grid(row=0, column=0, sticky="nsew")

    # ══════════════════════════════════════════════════════════════════
    # BUILD TAB FRAME
    # ══════════════════════════════════════════════════════════════════

    def _build_tab_frame(self, tab: _SSHTab) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self._content, fg_color=COLORS["bg_primary"])
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        # ── Connection bar ────────────────────────────────────────────
        bar = ctk.CTkFrame(frame, fg_color=COLORS["bg_secondary"],
                            corner_radius=12, border_width=1, border_color=COLORS["border"])
        bar.grid(row=0, column=0, sticky="ew", padx=24, pady=(8, 6))

        ctk.CTkLabel(bar, text="🔐  Terminal SSH",
                      font=(FONTS["family"], FONTS["size_lg"], "bold"),
                      text_color=COLORS["text_primary"]
                      ).grid(row=0, column=0, columnspan=12, sticky="w", padx=16, pady=(10, 6))

        labels = ["Host/IP:", "Usuário:", "Senha:", "Porta:"]
        for i, lbl in enumerate(labels):
            ctk.CTkLabel(bar, text=lbl, font=(FONTS["family"], FONTS["size_xs"]),
                          text_color=COLORS["text_secondary"]
                          ).grid(row=1, column=i * 2, padx=(16 if i == 0 else 6, 4), pady=(0, 10))

        es = dict(font=(FONTS["family_mono"], FONTS["size_sm"]),
                  fg_color=COLORS["bg_primary"], border_color=COLORS["border"],
                  text_color=COLORS["text_primary"], corner_radius=6, height=32)

        tab.host_entry = ctk.CTkEntry(bar, placeholder_text="192.168.x.x", width=150, **es)
        tab.host_entry.grid(row=1, column=1, padx=(0, 6), pady=(0, 10))
        tab.host_entry.bind("<FocusOut>", lambda e, t=tab: self._on_ip_changed(t))
        tab.host_entry.bind("<Return>", lambda e, t=tab: self._on_host_return(t))

        default_user, default_pwd = get_ssh_credentials()

        tab.user_entry = ctk.CTkEntry(bar, placeholder_text="suporte", width=110, **es)
        tab.user_entry.insert(0, default_user)
        tab.user_entry.grid(row=1, column=3, padx=(0, 6), pady=(0, 10))

        tab.pass_entry = ctk.CTkEntry(bar, placeholder_text="••••••", width=110, show="•", **es)
        tab.pass_entry.insert(0, default_pwd)
        tab.pass_entry.grid(row=1, column=5, padx=(0, 6), pady=(0, 10))

        tab.port_entry = ctk.CTkEntry(bar, placeholder_text="22", width=55, **es)
        tab.port_entry.insert(0, "22")
        tab.port_entry.grid(row=1, column=7, padx=(0, 6), pady=(0, 10))

        tab.connect_btn = ctk.CTkButton(
            bar, text="Conectar", width=90,
            font=(FONTS["family"], FONTS["size_sm"], "bold"),
            fg_color=COLORS["accent_green"], hover_color="#059669",
            text_color="#FFFFFF", corner_radius=6,
            command=lambda t=tab: self._connect(t))
        tab.connect_btn.grid(row=1, column=8, padx=6, pady=(0, 10))

        tab.disconnect_btn = ctk.CTkButton(
            bar, text="Desconectar", width=100,
            font=(FONTS["family"], FONTS["size_sm"]),
            fg_color=COLORS["accent_red"], hover_color="#DC2626",
            text_color="#FFFFFF", corner_radius=6, state="disabled",
            command=lambda t=tab: self._disconnect(t))
        tab.disconnect_btn.grid(row=1, column=9, padx=(0, 16), pady=(0, 10))

        tab.status_label = ctk.CTkLabel(bar, text="Desconectado",
                                          font=(FONTS["family_mono"], FONTS["size_xs"]),
                                          text_color=COLORS["text_muted"])
        tab.status_label.grid(row=2, column=0, columnspan=12, sticky="w", padx=16, pady=(0, 6))

        # ── Terminal area ─────────────────────────────────────────────
        term_frame = ctk.CTkFrame(frame, fg_color=COLORS["bg_secondary"],
                                   corner_radius=12, border_width=1, border_color=COLORS["border"])
        term_frame.grid(row=1, column=0, sticky="nsew", padx=24, pady=(0, 8))
        term_frame.grid_columnconfigure(0, weight=1)
        term_frame.grid_rowconfigure(0, weight=1)

        tab.terminal = ctk.CTkTextbox(term_frame,
                                        font=(FONTS["family_mono"], FONTS["size_sm"]),
                                        fg_color="#0D1117", text_color="#C9D1D9",
                                        border_width=0, corner_radius=8, wrap="word", state="disabled")
        tab.terminal.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 4))

        # ── Input bar ─────────────────────────────────────────────────
        input_bar = ctk.CTkFrame(term_frame, fg_color="transparent")
        input_bar.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 6))
        input_bar.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(input_bar, text="$",
                      font=(FONTS["family_mono"], FONTS["size_md"], "bold"),
                      text_color=COLORS["accent_green"], width=20
                      ).grid(row=0, column=0, padx=(4, 4))

        tab.cmd_entry = ctk.CTkEntry(input_bar, placeholder_text="Digite um comando...",
                                       font=(FONTS["family_mono"], FONTS["size_sm"]),
                                       fg_color="#0D1117", border_color=COLORS["border"],
                                       text_color="#C9D1D9", corner_radius=6, height=32, state="disabled")
        tab.cmd_entry.grid(row=0, column=1, sticky="ew", padx=(0, 4))
        tab.cmd_entry.bind("<Return>", lambda e, t=tab: self._send_command(t))
        tab.cmd_entry.bind("<Up>", lambda e, t=tab: self._history_up(t))
        tab.cmd_entry.bind("<Down>", lambda e, t=tab: self._history_down(t))
        tab.cmd_entry.bind("<Control-c>", lambda e, t=tab: self._send_interrupt(t))
        tab.cmd_entry.bind("<Control-l>", lambda e, t=tab: self._clear_terminal(t))

        # Ctrl+C
        ctk.CTkButton(
            input_bar, text="⛔ Ctrl+C", width=70, height=30,
            font=(FONTS["family_mono"], 9, "bold"),
            fg_color="#7F1D1D", hover_color="#991B1B",
            text_color="#FCA5A5", corner_radius=6,
            command=lambda t=tab: self._send_interrupt(t),
        ).grid(row=0, column=2, padx=2)

        # Limpar terminal
        ctk.CTkButton(
            input_bar, text="🗑 Limpar", width=75, height=30,
            font=(FONTS["family"], FONTS["size_xs"]),
            fg_color=COLORS["bg_tertiary"], hover_color=COLORS["bg_elevated"],
            text_color=COLORS["text_secondary"], corner_radius=6,
            command=lambda t=tab: self._clear_terminal(t),
        ).grid(row=0, column=3, padx=2)

        # ── Quick commands (2 linhas) ─────────────────────────────────
        quick_outer = ctk.CTkFrame(term_frame, fg_color="transparent")
        quick_outer.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))

        # Linha 1: comandos básicos
        quick1 = ctk.CTkFrame(quick_outer, fg_color="transparent")
        quick1.pack(fill="x", pady=(0, 2))
        cmds_row1 = [
            ("uptime",    "uptime"),
            ("ifconfig",  "ifconfig"),
            ("top",       "top -bn1 | head -20"),
            ("df",        "df -h"),
            ("free",      "free -m"),
            ("netstat",   "netstat -tlnp 2>/dev/null | head -20"),
            ("gateway",   "route -n 2>/dev/null || netstat -rn"),
            ("DNS",       "cat /etc/resolv.conf"),
            ("WAN IP",    "curl -s https://api.ipify.org && echo"),
        ]
        for label, cmd in cmds_row1:
            ctk.CTkButton(quick1, text=label, width=70, height=24,
                           font=(FONTS["family_mono"], 9),
                           fg_color=COLORS["bg_tertiary"], hover_color=COLORS["bg_elevated"],
                           text_color=COLORS["text_secondary"], corner_radius=4,
                           command=lambda c=cmd, t=tab: self._quick_command(t, c)
                           ).pack(side="left", padx=2)

        # Linha 2: comandos avançados / diagnóstico
        quick2 = ctk.CTkFrame(quick_outer, fg_color="transparent")
        quick2.pack(fill="x", pady=(0, 0))
        cmds_row2 = [
            ("ping 8.8.8.8",  "ping -c 4 8.8.8.8"),
            ("traceroute",    "traceroute -n 8.8.8.8 2>/dev/null || tracepath -n 8.8.8.8"),
            ("arp",           "arp -a 2>/dev/null || ip neigh show"),
            ("interfaces",    "ip addr show 2>/dev/null || ifconfig -a"),
            ("processos",     "ps aux --sort=-%mem | head -15"),
            ("logs",          "tail -30 /var/log/syslog 2>/dev/null || tail -30 /var/log/messages 2>/dev/null || echo 'log indisponível'"),
            ("conexões",      "ss -tunap 2>/dev/null | head -20 || netstat -tunap 2>/dev/null | head -20"),
            ("speed",         "cat /sys/class/net/eth0/speed 2>/dev/null && cat /sys/class/net/eth0/duplex 2>/dev/null || echo 'N/A'"),
        ]
        for label, cmd in cmds_row2:
            ctk.CTkButton(quick2, text=label, width=80, height=24,
                           font=(FONTS["family_mono"], 9),
                           fg_color=COLORS["bg_tertiary"], hover_color=COLORS["bg_elevated"],
                           text_color=COLORS["text_muted"], corner_radius=4,
                           command=lambda c=cmd, t=tab: self._quick_command(t, c)
                           ).pack(side="left", padx=2)

        return frame

    # ══════════════════════════════════════════════════════════════════
    # CONNECTION
    # ══════════════════════════════════════════════════════════════════

    def _on_ip_changed(self, tab: _SSHTab, event=None):
        ip = tab.host_entry.get().strip()
        if not ip:
            return

        if self.monitor:
            host = self.monitor.get_host_by_ip(ip)
            if host:
                tab.user_entry.delete(0, "end")
                tab.user_entry.insert(0, host.ssh_user or "")
                tab.pass_entry.delete(0, "end")
                tab.pass_entry.insert(0, host.ssh_password or "")
                tab.port_entry.delete(0, "end")
                tab.port_entry.insert(0, str(host.ssh_port or 22))
                tab.status_label.configure(
                    text=f"Host encontrado: {host.display_name} — credenciais preenchidas",
                    text_color=COLORS["accent_cyan"])
                return

        default_user, default_pwd = get_ssh_credentials()
        if not tab.user_entry.get().strip():
            tab.user_entry.delete(0, "end")
            tab.user_entry.insert(0, default_user)
        if not tab.pass_entry.get().strip():
            tab.pass_entry.delete(0, "end")
            tab.pass_entry.insert(0, default_pwd)

    def _on_host_return(self, tab: _SSHTab):
        """
        Ao pressionar Enter no campo Host: preenche as credenciais e
        inicia a conexão automaticamente — sem precisar clicar em Conectar.
        O botão Conectar continua disponível para uso manual.
        """
        self._on_ip_changed(tab)
        # Só conecta se o host ainda não está conectado (evita dupla conexão)
        if not tab.connected:
            self._connect(tab)

    def _connect(self, tab: _SSHTab):
        host = tab.host_entry.get().strip()
        user = tab.user_entry.get().strip()
        pwd = tab.pass_entry.get().strip()
        # CORREÇÃO v2.6 — porta SSH sem validação:
        #   int() lança ValueError se o usuário digitou texto no campo Porta.
        #   O crash acontecia dentro de uma lambda bind, sem nenhum feedback visual.
        try:
            port = int(tab.port_entry.get().strip() or "22")
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            tab.port_entry.configure(border_color="#EF4444")
            tab.status_label.configure(
                text="❌ Porta inválida (use um número entre 1 e 65535)",
                text_color=COLORS["accent_red"])
            return

        if not host or not user:
            tab.status_label.configure(text="❌ Preencha host e usuário",
                                         text_color=COLORS["accent_red"])
            return

        tab.status_label.configure(text="⏳ Conectando...",
                                     text_color=COLORS["accent_yellow"])
        tab.connect_btn.configure(state="disabled")
        tab.host_ip = host

        def _do_connect():
            session = self.ssh.create_session(host, user, pwd, port)
            session.set_output_callback(lambda text, t=tab: self._append_output(t, text))
            success, msg = session.connect()
            self.after(0, lambda: self._on_connected(tab, success, msg, session))

        threading.Thread(target=_do_connect, daemon=True).start()

    def _on_connected(self, tab: _SSHTab, success, msg, session):
        if success:
            tab.session = session
            tab.connected = True
            tab.status_label.configure(text=f"✅ Conectado a {session.host}",
                                         text_color=COLORS["accent_green"])
            tab.connect_btn.configure(state="disabled")
            tab.disconnect_btn.configure(state="normal")
            tab.cmd_entry.configure(state="normal")
            tab.cmd_entry.focus()
        else:
            tab.status_label.configure(text=f"❌ {msg}",
                                         text_color=COLORS["accent_red"])
            tab.connect_btn.configure(state="normal")
        self._refresh_tab_bar()

    def _disconnect(self, tab: _SSHTab):
        if tab.session:
            tab.session.disconnect()
            tab.session = None
        tab.connected = False
        tab.status_label.configure(text="Desconectado", text_color=COLORS["text_muted"])
        tab.connect_btn.configure(state="normal")
        tab.disconnect_btn.configure(state="disabled")
        tab.cmd_entry.configure(state="disabled")
        self._refresh_tab_bar()

    # ══════════════════════════════════════════════════════════════════
    # COMMAND INPUT & HISTORY
    # ══════════════════════════════════════════════════════════════════

    def _send_command(self, tab: _SSHTab, event=None):
        cmd = tab.cmd_entry.get().strip()
        if cmd and tab.session and tab.connected:
            tab.session.send_command(cmd)
            # Adiciona ao histórico (evita duplicata consecutiva)
            if not tab.cmd_history or tab.cmd_history[-1] != cmd:
                tab.cmd_history.append(cmd)
                if len(tab.cmd_history) > MAX_CMD_HISTORY:
                    tab.cmd_history.pop(0)
            tab.history_index = -1   # reseta posição
            tab.cmd_entry.delete(0, "end")

    def _history_up(self, tab: _SSHTab):
        """Navega para o comando anterior no histórico."""
        if not tab.cmd_history:
            return "break"

        if tab.history_index == -1:
            # Primeiro ↑: salva texto atual e vai para o último
            tab._temp_input = tab.cmd_entry.get()
            tab.history_index = len(tab.cmd_history) - 1
        elif tab.history_index > 0:
            tab.history_index -= 1
        else:
            return "break"   # já no início

        tab.cmd_entry.delete(0, "end")
        tab.cmd_entry.insert(0, tab.cmd_history[tab.history_index])
        return "break"

    def _history_down(self, tab: _SSHTab):
        """Navega para o próximo comando no histórico."""
        if tab.history_index == -1:
            return "break"

        if tab.history_index < len(tab.cmd_history) - 1:
            tab.history_index += 1
            tab.cmd_entry.delete(0, "end")
            tab.cmd_entry.insert(0, tab.cmd_history[tab.history_index])
        else:
            # Passou do fim — restaura texto original
            tab.history_index = -1
            tab.cmd_entry.delete(0, "end")
            temp = getattr(tab, "_temp_input", "")
            tab.cmd_entry.insert(0, temp)
        return "break"

    def _quick_command(self, tab: _SSHTab, cmd: str):
        if tab.session and tab.connected:
            tab.session.send_command(cmd)
            # Também salva no histórico
            if not tab.cmd_history or tab.cmd_history[-1] != cmd:
                tab.cmd_history.append(cmd)

    def _send_interrupt(self, tab: _SSHTab):
        """Envia Ctrl+C para o canal SSH."""
        if tab.session and tab.connected:
            try:
                tab.session.channel.send("\x03")
            except Exception:
                pass

    def _clear_terminal(self, tab: _SSHTab):
        """Limpa o conteúdo do terminal sem desconectar."""
        try:
            tab.terminal.configure(state="normal")
            tab.terminal.delete("1.0", "end")
            tab.terminal.configure(state="disabled")
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════
    # OUTPUT
    # ══════════════════════════════════════════════════════════════════

    _ANSI_ESCAPE = re.compile(r'(\x9B|\x1B\[)[0-?]*[ -/]*[@-~]|\x1B[@-_]|[\x80-\x9F]')

    def _append_output(self, tab: _SSHTab, text: str):
        text = self._ANSI_ESCAPE.sub('', text)
        def _update():
            try:
                tab.terminal.configure(state="normal")
                tab.terminal.insert("end", text)
                tab.terminal.see("end")
                tab.terminal.configure(state="disabled")
            except Exception:
                pass
        try:
            self.after(0, _update)
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════
    # PUBLIC API
    # ══════════════════════════════════════════════════════════════════

    def prefill_host(self, ip, user="", port=22):
        """Preenche a aba ativa com os dados do host."""
        if not self._tabs:
            return
        tab = self._tabs[self._current_tab_idx]
        tab.host_entry.delete(0, "end")
        tab.host_entry.insert(0, ip)
        if user:
            tab.user_entry.delete(0, "end")
            tab.user_entry.insert(0, user)
        tab.port_entry.delete(0, "end")
        tab.port_entry.insert(0, str(port))
        self._on_ip_changed(tab)

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
        new_size = int(BASE_TERM_FONT_SIZE * self._zoom / 100)
        for tab in self._tabs:
            try:
                tab.terminal.configure(font=(FONTS["family_mono"], new_size))
                tab.cmd_entry.configure(font=(FONTS["family_mono"], new_size))
            except Exception:
                pass

    def cleanup(self):
        """Desconecta todas as sessões."""
        for tab in self._tabs:
            if tab.session:
                tab.session.disconnect()