"""
Settings View — configurações, gerenciamento de hosts, renomear grupos,
credenciais SSH padrão, backup & caminhos de rede.
"""
import threading
import customtkinter as ctk

from config import (
    COLORS, FONTS, MONITOR_DEFAULTS, THRESHOLDS,
    DEFAULT_EXCLUDED_IPS, AUDIO_DIR,
    SSH_DEFAULTS, save_user_config, load_user_config, get_ssh_credentials,
    get_google_target, set_google_target,
)
from views.widgets import bind_mousewheel_scroll
from controllers.audio_controller import ALERTA_DURATION_S
from utils.logger import setup_logger

logger = setup_logger("settings")


class AddHostDialog(ctk.CTkToplevel):
    def __init__(self, master, on_save=None, host_data=None, existing_ips=None, **kwargs):
        super().__init__(master, **kwargs)
        self.title("Adicionar Host" if not host_data else "Editar Host")
        self.geometry("480x760")
        self.configure(fg_color=COLORS["bg_primary"])
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self._on_save = on_save
        self._host_data = host_data
        # IPs já cadastrados para bloquear duplicatas.
        # Na edição, o IP do próprio host é excluído da lista para não
        # impedir salvar sem alterar o endereço.
        _own_ip = (host_data or {}).get("ip", "").strip().lower()
        self._existing_ips = {
            ip.strip().lower()
            for ip in (existing_ips or [])
            if ip.strip().lower() != _own_ip
        }
        self.grid_columnconfigure(0, weight=1)

        pad = dict(padx=24, pady=(0, 4))
        es = dict(
            font=(FONTS["family_mono"], FONTS["size_sm"]),
            fg_color=COLORS["bg_secondary"], border_color=COLORS["border"],
            text_color=COLORS["text_primary"], corner_radius=6, height=36,
        )

        ctk.CTkLabel(self, text="Novo Host" if not host_data else "Editar Host",
                      font=(FONTS["family"], FONTS["size_lg"], "bold"),
                      text_color=COLORS["text_primary"]
                      ).grid(row=0, column=0, padx=24, pady=(20, 12))

        ctk.CTkLabel(self, text="Endereço IP *", font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_secondary"]).grid(row=1, column=0, sticky="w", **pad)
        self.ip_entry = ctk.CTkEntry(self, placeholder_text="192.168.x.x", **es)
        self.ip_entry.grid(row=2, column=0, sticky="ew", **pad)

        # Label de erro de duplicata — inicialmente vazio, aparece em vermelho
        self._ip_error_label = ctk.CTkLabel(
            self, text="",
            font=(FONTS["family"], FONTS["size_xs"]),
            text_color=COLORS["accent_red"])
        self._ip_error_label.grid(row=3, column=0, sticky="w", padx=24, pady=(0, 2))

        ctk.CTkLabel(self, text="Nome/Label (ex: Loja 26, Janaúba 1)", font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_secondary"]).grid(row=4, column=0, sticky="w", **pad)
        self.label_entry = ctk.CTkEntry(self, placeholder_text="Loja 01, Sede...", **es)
        self.label_entry.grid(row=5, column=0, sticky="ew", **pad)

        ctk.CTkLabel(self, text="Grupo", font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_secondary"]).grid(row=6, column=0, sticky="w", **pad)
        self.group_entry = ctk.CTkEntry(self, placeholder_text="Geral", **es)
        self.group_entry.grid(row=7, column=0, sticky="ew", **pad)

        ctk.CTkLabel(self, text="Alvo da WAN / Gateway WAN", font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_secondary"]).grid(row=8, column=0, sticky="w", **pad)
        self.wan_ip_entry = ctk.CTkEntry(self, placeholder_text="ex: gateway da operadora/modem (não o IP público da loja)", **es)
        self.wan_ip_entry.grid(row=9, column=0, sticky="ew", **pad)

        ctk.CTkLabel(self, text="Alvo da WAN Secundária (opcional)", font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_secondary"]).grid(row=10, column=0, sticky="w", **pad)
        self.wan_ip_2_entry = ctk.CTkEntry(self, placeholder_text="ex: IP público da loja (ifconfig.me)", **es)
        self.wan_ip_2_entry.grid(row=11, column=0, sticky="ew", **pad)

        ctk.CTkLabel(self, text="Alvo WAN Terciária / Operadora (opcional)", font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_secondary"]).grid(row=12, column=0, sticky="w", **pad)
        self.wan_ip_3_entry = ctk.CTkEntry(self, placeholder_text="ex: 200.151.105.213 (2º hop do traceroute 8.8.8.8)", **es)
        self.wan_ip_3_entry.grid(row=13, column=0, sticky="ew", **pad)

        # SSH
        ssh_frame = ctk.CTkFrame(self, fg_color="transparent")
        ssh_frame.grid(row=14, column=0, sticky="ew", padx=24, pady=(8, 4))
        ssh_frame.grid_columnconfigure(0, weight=2)
        ssh_frame.grid_columnconfigure(1, weight=2)
        ssh_frame.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(ssh_frame, text="SSH Usuário", font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_secondary"]).grid(row=0, column=0, sticky="w")
        self.ssh_user_entry = ctk.CTkEntry(ssh_frame, placeholder_text="suporte", **es)
        self.ssh_user_entry.grid(row=1, column=0, sticky="ew", padx=(0, 4))

        ctk.CTkLabel(ssh_frame, text="SSH Senha", font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_secondary"]).grid(row=0, column=1, sticky="w")
        self.ssh_pass_entry = ctk.CTkEntry(ssh_frame, placeholder_text="••••••", show="•", **es)
        self.ssh_pass_entry.grid(row=1, column=1, sticky="ew", padx=4)

        ctk.CTkLabel(ssh_frame, text="Porta", font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_secondary"]).grid(row=0, column=2, sticky="w")
        self.ssh_port_entry = ctk.CTkEntry(ssh_frame, placeholder_text="22", width=60, **es)
        self.ssh_port_entry.grid(row=1, column=2, sticky="ew", padx=(4, 0))

        # ── Plataforma do roteador (obrigatório) ──────────────────────
        ctk.CTkLabel(self, text="Plataforma do Roteador *",
                      font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_secondary"]
                      ).grid(row=15, column=0, sticky="w", **pad)

        from utils.device_profiles import PLATFORM_CHOICES
        self._platform_values = [p[0] for p in PLATFORM_CHOICES]
        self._platform_labels = [p[1] for p in PLATFORM_CHOICES]

        self.platform_combo = ctk.CTkComboBox(
            self,
            values=self._platform_labels,
            font=(FONTS["family"], FONTS["size_sm"]),
            dropdown_font=(FONTS["family"], FONTS["size_sm"]),
            fg_color=COLORS["bg_secondary"],
            border_color=COLORS["border"],
            button_color=COLORS["accent_blue"],
            button_hover_color=COLORS["accent_blue_hover"],
            text_color=COLORS["text_primary"],
            dropdown_fg_color=COLORS["bg_secondary"],
            dropdown_text_color=COLORS["text_primary"],
            dropdown_hover_color=COLORS["bg_elevated"],
            corner_radius=6, height=36,
            state="readonly",
        )
        self.platform_combo.grid(row=16, column=0, sticky="ew", **pad)
        self.platform_combo.set(self._platform_labels[0])  # "— Selecione —"

        # Label de erro de plataforma
        self._platform_error_label = ctk.CTkLabel(
            self, text="",
            font=(FONTS["family"], FONTS["size_xs"]),
            text_color=COLORS["accent_red"])
        self._platform_error_label.grid(row=17, column=0, sticky="w", padx=24, pady=(0, 2))

        # Preenche com defaults ou dados existentes
        default_user, default_pwd = get_ssh_credentials()
        if host_data:
            self.ip_entry.insert(0, host_data.get("ip", ""))
            self.label_entry.insert(0, host_data.get("label", ""))
            self.group_entry.insert(0, host_data.get("group_name", "Geral"))
            self.wan_ip_entry.insert(0, host_data.get("wan_ip", ""))
            self.wan_ip_2_entry.insert(0, host_data.get("wan_ip_2", ""))
            self.wan_ip_3_entry.insert(0, host_data.get("wan_ip_3", ""))
            self.ssh_user_entry.insert(0, host_data.get("ssh_user", "") or default_user)
            self.ssh_pass_entry.insert(0, host_data.get("ssh_password", "") or default_pwd)
            self.ssh_port_entry.insert(0, str(host_data.get("ssh_port", 22)))
            # Restaura plataforma selecionada
            saved_platform = host_data.get("platform", "")
            if saved_platform in self._platform_values:
                idx = self._platform_values.index(saved_platform)
                self.platform_combo.set(self._platform_labels[idx])
        else:
            self.ssh_user_entry.insert(0, default_user)
            self.ssh_pass_entry.insert(0, default_pwd)
            self.ssh_port_entry.insert(0, "22")

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=18, column=0, sticky="ew", padx=24, pady=(16, 20))

        ctk.CTkButton(btn_frame, text="Cancelar", width=100,
                       font=(FONTS["family"], FONTS["size_sm"]),
                       fg_color="transparent", hover_color=COLORS["bg_tertiary"],
                       border_width=1, border_color=COLORS["border"],
                       text_color=COLORS["text_secondary"], command=self.destroy
                       ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(btn_frame, text="Salvar", width=100,
                       font=(FONTS["family"], FONTS["size_sm"], "bold"),
                       fg_color=COLORS["accent_blue"], hover_color=COLORS["accent_blue_hover"],
                       text_color="#FFFFFF", command=self._save
                       ).pack(side="right")

    def _save(self):
        ip = self.ip_entry.get().strip()
        if not ip:
            return
        # CORREÇÃO v2.12 — valida IP antes de qualquer coisa (anti command injection)
        from utils.security import is_valid_target, is_valid_ip
        if not is_valid_target(ip):
            self.ip_entry.configure(border_color=COLORS["accent_red"])
            self._ip_error_label.configure(
                text=f"⚠ IP/hostname inválido: {ip[:30]}")
            return
        # Verifica duplicata: bloqueia se o IP já estiver cadastrado.
        if ip.lower() in self._existing_ips:
            self.ip_entry.configure(border_color=COLORS["accent_red"])
            self._ip_error_label.configure(text=f"⚠ IP {ip} já está cadastrado.")
            return
        self.ip_entry.configure(border_color=COLORS["border"])
        self._ip_error_label.configure(text="")

        # Valida WAN IPs também (todos opcionais, mas se preenchidos têm que ser válidos)
        wan_ip = self.wan_ip_entry.get().strip()
        wan_ip_2 = self.wan_ip_2_entry.get().strip()
        wan_ip_3 = self.wan_ip_3_entry.get().strip()
        for w_val, w_widget in [(wan_ip, self.wan_ip_entry),
                                 (wan_ip_2, self.wan_ip_2_entry),
                                 (wan_ip_3, self.wan_ip_3_entry)]:
            if w_val and not is_valid_target(w_val):
                w_widget.configure(border_color=COLORS["accent_red"])
                self._ip_error_label.configure(
                    text=f"⚠ WAN IP inválido: {w_val[:30]}")
                return
            w_widget.configure(border_color=COLORS["border"])

        # Valida plataforma (obrigatório)
        selected_label = self.platform_combo.get()
        try:
            platform_idx = self._platform_labels.index(selected_label)
            platform_value = self._platform_values[platform_idx]
        except (ValueError, IndexError):
            platform_value = ""
        if not platform_value:
            self.platform_combo.configure(border_color=COLORS["accent_red"])
            self._platform_error_label.configure(text="⚠ Selecione a plataforma do roteador.")
            return
        self.platform_combo.configure(border_color=COLORS["border"])
        self._platform_error_label.configure(text="")

        # Valida porta SSH
        try:
            ssh_port = int(self.ssh_port_entry.get().strip() or "22")
            if not (1 <= ssh_port <= 65535):
                raise ValueError
        except ValueError:
            self.ssh_port_entry.configure(border_color=COLORS["accent_red"])
            return
        data = {
            "ip": ip,
            "label": self.label_entry.get().strip(),
            "group": self.group_entry.get().strip() or "Geral",
            "ssh_user": self.ssh_user_entry.get().strip(),
            "ssh_password": self.ssh_pass_entry.get().strip(),
            "ssh_port": ssh_port,
            "wan_ip": self.wan_ip_entry.get().strip(),
            "wan_ip_2": self.wan_ip_2_entry.get().strip(),
            "wan_ip_3": self.wan_ip_3_entry.get().strip(),
            "platform": platform_value,
        }
        if self._host_data:
            data["id"] = self._host_data.get("id")
        if self._on_save:
            self._on_save(data)
        self.destroy()


class ImportRangeDialog(ctk.CTkToplevel):
    def __init__(self, master, on_import=None, **kwargs):
        super().__init__(master, **kwargs)
        self.title("Importar Range de IPs")
        self.geometry("540x660")
        self.configure(fg_color=COLORS["bg_primary"])
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self._on_import = on_import
        self.grid_columnconfigure(0, weight=1)

        es = dict(
            font=(FONTS["family_mono"], FONTS["size_sm"]),
            fg_color=COLORS["bg_secondary"], border_color=COLORS["border"],
            text_color=COLORS["text_primary"], corner_radius=6, height=36,
        )

        ctk.CTkLabel(self, text="Importar Range de IPs",
                      font=(FONTS["family"], FONTS["size_lg"], "bold"),
                      text_color=COLORS["text_primary"]
                      ).grid(row=0, column=0, padx=24, pady=(20, 4))

        # -- Formato de IP configurável --------------------------------
        fmt_frame = ctk.CTkFrame(self, fg_color=COLORS["bg_secondary"],
                                  corner_radius=8, border_width=1, border_color=COLORS["border"])
        fmt_frame.grid(row=1, column=0, sticky="ew", padx=24, pady=(8, 8))
        fmt_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(fmt_frame, text="Formato do IP:",
                      font=(FONTS["family"], FONTS["size_xs"], "bold"),
                      text_color=COLORS["text_secondary"]).grid(row=0, column=0, padx=12, pady=(8, 2), sticky="w")

        ctk.CTkLabel(fmt_frame, text="Use {N} onde o octeto varia. Exemplos:\n"
                      "  203.0.113.{N}  ?  203.0.113.102, 203.0.113.103, ...\n"
                      "  198.51.100.{N}       ?  198.51.100.1, 198.51.100.2, ...\n"
                      "  172.16.0.{N}     ?  172.16.0.10, 172.16.0.11, ...",
                      font=(FONTS["family_mono"], 9),
                      text_color=COLORS["text_muted"], justify="left"
                      ).grid(row=1, column=0, columnspan=2, padx=12, pady=(0, 4), sticky="w")

        self.format_entry = ctk.CTkEntry(fmt_frame, placeholder_text="203.0.113.{N}", **es)
        self.format_entry.grid(row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=(4, 10))
        self.format_entry.insert(0, "203.0.113.{N}")
        self.format_entry.bind("<KeyRelease>", lambda e: self._update_preview())

        # -- Range -----------------------------------------------------
        range_frame = ctk.CTkFrame(self, fg_color="transparent")
        range_frame.grid(row=2, column=0, sticky="ew", padx=24, pady=(4, 8))
        range_frame.grid_columnconfigure(0, weight=1)
        range_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(range_frame, text="Valor de {N} — Início:",
                      font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_secondary"]).grid(row=0, column=0, sticky="w")
        self.start_entry = ctk.CTkEntry(range_frame, placeholder_text="102", width=100, **es)
        self.start_entry.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.start_entry.insert(0, "102")

        ctk.CTkLabel(range_frame, text="Valor de {N} — Fim:",
                      font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_secondary"]).grid(row=0, column=1, sticky="w")
        self.end_entry = ctk.CTkEntry(range_frame, placeholder_text="168", width=100, **es)
        self.end_entry.grid(row=1, column=1, sticky="ew", padx=(8, 0))
        self.end_entry.insert(0, "168")

        # -- Grupo -----------------------------------------------------
        ctk.CTkLabel(self, text="Tipo de grupo:",
                      font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_secondary"]).grid(row=3, column=0, sticky="w", padx=24, pady=(8, 4))

        self.group_var = ctk.StringVar(value="Loja")
        self.group_selector = ctk.CTkSegmentedButton(
            self, values=["Loja", "Setor"], variable=self.group_var,
            font=(FONTS["family"], FONTS["size_sm"]),
            fg_color=COLORS["bg_secondary"], selected_color=COLORS["accent_blue"],
            selected_hover_color=COLORS["accent_blue_hover"],
            unselected_color=COLORS["bg_secondary"],
            unselected_hover_color=COLORS["bg_tertiary"],
            text_color=COLORS["text_secondary"], corner_radius=8,
        )
        self.group_selector.grid(row=4, column=0, sticky="ew", padx=24, pady=(0, 8))

        # -- Exclusões -------------------------------------------------
        ctk.CTkLabel(self, text="Valores de {N} excluídos (vírgula):",
                      font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_secondary"]).grid(row=5, column=0, sticky="w", padx=24, pady=(8, 4))
        self.excluded_entry = ctk.CTkEntry(self, **es)
        self.excluded_entry.grid(row=6, column=0, sticky="ew", padx=24, pady=(0, 8))
        default_excluded_octets = ", ".join(ip.split(".")[2] for ip in DEFAULT_EXCLUDED_IPS)
        self.excluded_entry.insert(0, default_excluded_octets)

        # -- Preview ---------------------------------------------------
        self.preview_label = ctk.CTkLabel(self, text="",
                                           font=(FONTS["family_mono"], FONTS["size_xs"]),
                                           text_color=COLORS["accent_cyan"],
                                           wraplength=480)
        self.preview_label.grid(row=7, column=0, padx=24, pady=(4, 4))

        self.preview_sample = ctk.CTkLabel(self, text="",
                                            font=(FONTS["family_mono"], 9),
                                            text_color=COLORS["text_muted"],
                                            wraplength=480)
        self.preview_sample.grid(row=8, column=0, padx=24, pady=(0, 8))

        self._update_preview()
        self.start_entry.bind("<KeyRelease>", lambda e: self._update_preview())
        self.end_entry.bind("<KeyRelease>", lambda e: self._update_preview())
        self.excluded_entry.bind("<KeyRelease>", lambda e: self._update_preview())

        # -- Plataforma (obrigatório para todos os hosts do range) -----
        ctk.CTkLabel(self, text="Plataforma dos Roteadores *",
                      font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_secondary"]
                      ).grid(row=9, column=0, sticky="w", padx=24, pady=(4, 2))

        from utils.device_profiles import PLATFORM_CHOICES
        self._platform_values = [p[0] for p in PLATFORM_CHOICES]
        self._platform_labels = [p[1] for p in PLATFORM_CHOICES]

        self.platform_combo = ctk.CTkComboBox(
            self,
            values=self._platform_labels,
            font=(FONTS["family"], FONTS["size_sm"]),
            dropdown_font=(FONTS["family"], FONTS["size_sm"]),
            fg_color=COLORS["bg_secondary"],
            border_color=COLORS["border"],
            button_color=COLORS["accent_blue"],
            button_hover_color=COLORS["accent_blue_hover"],
            text_color=COLORS["text_primary"],
            dropdown_fg_color=COLORS["bg_secondary"],
            dropdown_text_color=COLORS["text_primary"],
            dropdown_hover_color=COLORS["bg_elevated"],
            corner_radius=6, height=36,
            state="readonly",
        )
        self.platform_combo.grid(row=10, column=0, sticky="ew", padx=24, pady=(0, 8))
        self.platform_combo.set(self._platform_labels[0])

        # -- Botões ----------------------------------------------------
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=11, column=0, sticky="ew", padx=24, pady=(8, 20))
        ctk.CTkButton(btn_frame, text="Cancelar", width=100,
                       font=(FONTS["family"], FONTS["size_sm"]),
                       fg_color="transparent", hover_color=COLORS["bg_tertiary"],
                       border_width=1, border_color=COLORS["border"],
                       text_color=COLORS["text_secondary"], command=self.destroy
                       ).pack(side="right", padx=(8, 0))
        ctk.CTkButton(btn_frame, text="Importar", width=120,
                       font=(FONTS["family"], FONTS["size_sm"], "bold"),
                       fg_color=COLORS["accent_blue"], hover_color=COLORS["accent_blue_hover"],
                       text_color="#FFFFFF", command=self._do_import
                       ).pack(side="right")

    def _get_ip_format(self) -> str:
        return self.format_entry.get().strip() or "203.0.113.{N}"

    def _generate_ip(self, n: int) -> str:
        return self._get_ip_format().replace("{N}", str(n))

    def _update_preview(self):
        try:
            fmt = self._get_ip_format()
            if "{N}" not in fmt:
                self.preview_label.configure(text="O formato deve conter {N}")
                self.preview_sample.configure(text="")
                return
            start = int(self.start_entry.get().strip() or "0")
            end = int(self.end_entry.get().strip() or "0")
            excluded = set()
            for x in self.excluded_entry.get().strip().split(","):
                x = x.strip()
                if x.isdigit():
                    excluded.add(int(x))
            values = [i for i in range(start, end + 1) if i not in excluded]
            count = len(values)
            first_ip = self._generate_ip(start)
            last_ip = self._generate_ip(end)
            self.preview_label.configure(
                text=f"+ {count} hosts  ({first_ip} ate {last_ip})")
            # Mostra amostra dos primeiros 5 IPs
            sample_ips = [self._generate_ip(v) for v in values[:5]]
            sample_text = ", ".join(sample_ips)
            if count > 5:
                sample_text += f", ... (+{count - 5} mais)"
            self.preview_sample.configure(text=sample_text)
        except ValueError:
            self.preview_label.configure(text="Valores invalidos")
            self.preview_sample.configure(text="")

    def _do_import(self):
        try:
            start = int(self.start_entry.get().strip())
            end = int(self.end_entry.get().strip())
        except ValueError:
            return
        fmt = self._get_ip_format()
        if "{N}" not in fmt:
            return
        # Valida plataforma (obrigatório)
        selected_label = self.platform_combo.get()
        try:
            platform_idx = self._platform_labels.index(selected_label)
            platform_value = self._platform_values[platform_idx]
        except (ValueError, IndexError):
            platform_value = ""
        if not platform_value:
            self.platform_combo.configure(border_color=COLORS["accent_red"])
            return
        group_type = self.group_var.get()
        excluded = set()
        for x in self.excluded_entry.get().strip().split(","):
            x = x.strip()
            if x.isdigit():
                excluded.add(int(x))
        if self._on_import:
            self._on_import(start, end, group_type, excluded, fmt, platform_value)
        self.destroy()


class RenameGroupDialog(ctk.CTkToplevel):
    """Diálogo para renomear um grupo — afeta todos os hosts do grupo."""

    def __init__(self, master, groups: list, on_rename=None, **kwargs):
        super().__init__(master, **kwargs)
        self.title("Renomear Grupo")
        self.geometry("420x280")
        self.configure(fg_color=COLORS["bg_primary"])
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self._on_rename = on_rename
        self.grid_columnconfigure(0, weight=1)

        es = dict(
            font=(FONTS["family_mono"], FONTS["size_sm"]),
            fg_color=COLORS["bg_secondary"], border_color=COLORS["border"],
            text_color=COLORS["text_primary"], corner_radius=6, height=36,
        )

        ctk.CTkLabel(self, text="Renomear Grupo",
                      font=(FONTS["family"], FONTS["size_lg"], "bold"),
                      text_color=COLORS["text_primary"]
                      ).grid(row=0, column=0, padx=24, pady=(20, 4))

        ctk.CTkLabel(self,
                      text="Todos os hosts do grupo selecionado serão atualizados.",
                      font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_secondary"]
                      ).grid(row=1, column=0, padx=24, pady=(0, 12))

        ctk.CTkLabel(self, text="Grupo atual:",
                      font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_secondary"]
                      ).grid(row=2, column=0, sticky="w", padx=24, pady=(0, 4))

        self.group_combo = ctk.CTkComboBox(
            self, values=groups if groups else ["(nenhum)"],
            font=(FONTS["family_mono"], FONTS["size_sm"]),
            fg_color=COLORS["bg_secondary"], border_color=COLORS["border"],
            text_color=COLORS["text_primary"], button_color=COLORS["accent_blue"],
            button_hover_color=COLORS["accent_blue_hover"],
            dropdown_fg_color=COLORS["bg_secondary"],
            dropdown_text_color=COLORS["text_primary"],
            dropdown_hover_color=COLORS["bg_tertiary"],
            corner_radius=6, height=36,
        )
        self.group_combo.grid(row=3, column=0, sticky="ew", padx=24, pady=(0, 8))

        ctk.CTkLabel(self, text="Novo nome:",
                      font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_secondary"]
                      ).grid(row=4, column=0, sticky="w", padx=24, pady=(0, 4))

        self.new_name_entry = ctk.CTkEntry(self, placeholder_text="Novo nome do grupo", **es)
        self.new_name_entry.grid(row=5, column=0, sticky="ew", padx=24, pady=(0, 12))

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.grid(row=6, column=0, sticky="ew", padx=24, pady=(0, 20))

        ctk.CTkButton(btn_frame, text="Cancelar", width=100,
                       font=(FONTS["family"], FONTS["size_sm"]),
                       fg_color="transparent", hover_color=COLORS["bg_tertiary"],
                       border_width=1, border_color=COLORS["border"],
                       text_color=COLORS["text_secondary"], command=self.destroy
                       ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(btn_frame, text="Renomear", width=120,
                       font=(FONTS["family"], FONTS["size_sm"], "bold"),
                       fg_color=COLORS["accent_orange"], hover_color="#EA580C",
                       text_color="#FFFFFF", command=self._do_rename
                       ).pack(side="right")

    def _do_rename(self):
        old = self.group_combo.get().strip()
        new = self.new_name_entry.get().strip()
        if not old or not new or old == "(nenhum)":
            return
        if self._on_rename:
            self._on_rename(old, new)
        self.destroy()


class SettingsView(ctk.CTkFrame):
    def __init__(self, master, controller=None, **kwargs):
        super().__init__(master, fg_color=COLORS["bg_primary"], **kwargs)
        self.controller = controller
        self._host_search_var = ctk.StringVar(value="")
        self._host_filter_var = ctk.StringVar(value="Todos")
        self._sort_var = ctk.StringVar(value="Nome")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_header()
        self._build_content()

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 8))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(header, text="Configurações",
                      font=(FONTS["family"], FONTS["size_xl"], "bold"),
                      text_color=COLORS["text_primary"]).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(header, text=f"🔊 Áudios: {AUDIO_DIR}",
                      font=(FONTS["family_mono"], FONTS["size_xs"]),
                      text_color=COLORS["text_muted"]).grid(row=1, column=0, sticky="w")

        # v2.4: Zoom
        self._zoom = 100
        zoom_frame = ctk.CTkFrame(header, fg_color="transparent")
        zoom_frame.grid(row=0, column=1, sticky="e")
        ctk.CTkButton(
            zoom_frame, text="-", width=26, height=26,
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

    def _build_content(self):
        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent",
                                         scrollbar_button_color=COLORS["scrollbar"],
                                         scrollbar_button_hover_color=COLORS["scrollbar_hover"])
        scroll.grid(row=1, column=0, sticky="nsew", padx=24, pady=(0, 16))
        scroll.grid_columnconfigure(0, weight=1)
        self._page_scroll = scroll
        bind_mousewheel_scroll(scroll)

        row = 0

        # -- Credenciais SSH Padrão ------------------------------------
        row = self._build_section(scroll, "🔑  Credenciais SSH Padrão", row)

        ssh_frame = ctk.CTkFrame(scroll, fg_color=COLORS["bg_secondary"],
                                  corner_radius=12, border_width=1, border_color=COLORS["border"])
        ssh_frame.grid(row=row, column=0, sticky="ew", pady=(0, 16))
        ssh_frame.grid_columnconfigure(1, weight=1)
        row += 1

        ctk.CTkLabel(ssh_frame,
                      text="Estas credenciais serão usadas como padrão para novos hosts e no Terminal SSH.",
                      font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_secondary"],
                      wraplength=500).grid(row=0, column=0, columnspan=2, padx=16, pady=(10, 8), sticky="w")

        default_user, default_pwd = get_ssh_credentials()

        ctk.CTkLabel(ssh_frame, text="Usuário SSH padrão:",
                      font=(FONTS["family"], FONTS["size_sm"]),
                      text_color=COLORS["text_secondary"]).grid(row=1, column=0, sticky="w", padx=16, pady=4)
        self.ssh_default_user = ctk.CTkEntry(ssh_frame, width=200,
                                              font=(FONTS["family_mono"], FONTS["size_sm"]),
                                              fg_color=COLORS["bg_primary"],
                                              border_color=COLORS["border"],
                                              text_color=COLORS["text_primary"],
                                              corner_radius=6, height=32)
        self.ssh_default_user.insert(0, default_user)
        self.ssh_default_user.grid(row=1, column=1, sticky="w", padx=16, pady=4)

        ctk.CTkLabel(ssh_frame, text="Senha SSH padrão:",
                      font=(FONTS["family"], FONTS["size_sm"]),
                      text_color=COLORS["text_secondary"]).grid(row=2, column=0, sticky="w", padx=16, pady=4)
        self.ssh_default_pass = ctk.CTkEntry(ssh_frame, width=200, show="•",
                                              font=(FONTS["family_mono"], FONTS["size_sm"]),
                                              fg_color=COLORS["bg_primary"],
                                              border_color=COLORS["border"],
                                              text_color=COLORS["text_primary"],
                                              corner_radius=6, height=32)
        self.ssh_default_pass.insert(0, default_pwd)
        self.ssh_default_pass.grid(row=2, column=1, sticky="w", padx=16, pady=4)

        ctk.CTkLabel(ssh_frame, text="IP alvo Google / MTR Google:",
                      font=(FONTS["family"], FONTS["size_sm"]),
                      text_color=COLORS["text_secondary"]).grid(row=3, column=0, sticky="w", padx=16, pady=4)
        self.google_target_entry = ctk.CTkEntry(ssh_frame, width=200,
                                                 font=(FONTS["family_mono"], FONTS["size_sm"]),
                                                 fg_color=COLORS["bg_primary"],
                                                 border_color=COLORS["border"],
                                                 text_color=COLORS["text_primary"],
                                                 corner_radius=6, height=32,
                                                 placeholder_text="Padrão: 8.8.8.8")
        self.google_target_entry.insert(0, get_google_target())
        self.google_target_entry.grid(row=3, column=1, sticky="w", padx=16, pady=(4, 12))
        ctk.CTkLabel(ssh_frame,
                      text="Usado no MTR Google e como ping de internet quando wan_ip_3 não está configurado no host.",
                      font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_muted"],
                      wraplength=400).grid(row=4, column=0, columnspan=2, padx=16, pady=(0, 10), sticky="w")

        # -- Gerenciamento de Hosts ------------------------------------
        row = self._build_section(scroll, "Gerenciamento de Hosts", row)

        host_controls = ctk.CTkFrame(scroll, fg_color="transparent")
        host_controls.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        row += 1

        ctk.CTkButton(host_controls, text="+ Adicionar Host", width=150,
                       font=(FONTS["family"], FONTS["size_sm"], "bold"),
                       fg_color=COLORS["accent_blue"], hover_color=COLORS["accent_blue_hover"],
                       text_color="#FFFFFF", command=self._add_host).pack(side="left", padx=(0, 8))

        ctk.CTkButton(host_controls, text="📊 Importar Range", width=150,
                       font=(FONTS["family"], FONTS["size_sm"]),
                       fg_color=COLORS["accent_purple"], hover_color="#7C3AED",
                       text_color="#FFFFFF",
                       command=self._open_range_dialog).pack(side="left", padx=4)

        ctk.CTkButton(host_controls, text="✏️ Renomear Grupo", width=150,
                       font=(FONTS["family"], FONTS["size_sm"]),
                       fg_color=COLORS["accent_orange"], hover_color="#EA580C",
                       text_color="#FFFFFF",
                       command=self._open_rename_group_dialog).pack(side="left", padx=4)

        host_filters = ctk.CTkFrame(scroll, fg_color="transparent")
        host_filters.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        host_filters.grid_columnconfigure(0, weight=1)
        row += 1

        self.host_search_entry = ctk.CTkEntry(
            host_filters,
            textvariable=self._host_search_var,
            placeholder_text="Buscar host por nome, IP, grupo ou WAN...",
            font=(FONTS["family"], FONTS["size_sm"]),
            fg_color=COLORS["bg_secondary"],
            border_color=COLORS["border"],
            text_color=COLORS["text_primary"],
            placeholder_text_color=COLORS["text_muted"],
            corner_radius=8,
            height=36,
        )
        self.host_search_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.host_search_entry.bind("<KeyRelease>", lambda e: self._refresh_host_list())

        self.host_filter_menu = ctk.CTkSegmentedButton(
            host_filters,
            values=["Todos", "Online", "Offline"],
            variable=self._host_filter_var,
            command=lambda _value: self._refresh_host_list(),
            font=(FONTS["family"], FONTS["size_xs"]),
            fg_color=COLORS["bg_secondary"],
            selected_color=COLORS["accent_blue"],
            selected_hover_color=COLORS["accent_blue_hover"],
            unselected_color=COLORS["bg_secondary"],
            unselected_hover_color=COLORS["bg_tertiary"],
            text_color=COLORS["text_secondary"],
            text_color_disabled=COLORS["text_muted"],
            corner_radius=8,
        )
        self.host_filter_menu.grid(row=0, column=1, padx=(8, 0))

        # Ordenação: por Nome ou por IP
        self.sort_menu = ctk.CTkSegmentedButton(
            host_filters,
            values=["↕ Nome", "↕ IP"],
            variable=self._sort_var,
            command=lambda _v: self._refresh_host_list(),
            font=(FONTS["family"], FONTS["size_xs"]),
            fg_color=COLORS["bg_secondary"],
            selected_color=COLORS["accent_purple"],
            selected_hover_color="#7C3AED",
            unselected_color=COLORS["bg_secondary"],
            unselected_hover_color=COLORS["bg_tertiary"],
            text_color=COLORS["text_secondary"],
            corner_radius=8,
        )
        self.sort_menu.grid(row=0, column=2, padx=(8, 0))

        ctk.CTkButton(
            host_filters,
            text="Limpar",
            width=70,
            font=(FONTS["family"], FONTS["size_xs"]),
            fg_color="transparent",
            hover_color=COLORS["bg_tertiary"],
            border_width=1,
            border_color=COLORS["border"],
            text_color=COLORS["text_muted"],
            corner_radius=8,
            height=36,
            command=self._clear_host_filters,
        ).grid(row=0, column=3, padx=(8, 0))

        self.hosts_meta_label = ctk.CTkLabel(
            scroll,
            text="",
            font=(FONTS["family_mono"], FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
        )
        self.hosts_meta_label.grid(row=row, column=0, sticky="w", pady=(0, 6))
        row += 1

        # Lista de hosts
        self.hosts_frame = ctk.CTkFrame(scroll, fg_color=COLORS["bg_secondary"],
                                         corner_radius=12, border_width=1, border_color=COLORS["border"])
        self.hosts_frame.grid(row=row, column=0, sticky="ew", pady=(0, 16))
        self.hosts_frame.grid_columnconfigure(1, weight=1)
        row += 1
        self._refresh_host_list()

        # -- Parâmetros de Monitoramento -------------------------------
        row = self._build_section(scroll, "Parâmetros de Monitoramento", row)
        params_frame = ctk.CTkFrame(scroll, fg_color=COLORS["bg_secondary"],
                                     corner_radius=12, border_width=1, border_color=COLORS["border"])
        params_frame.grid(row=row, column=0, sticky="ew", pady=(0, 16))
        params_frame.grid_columnconfigure(1, weight=1)
        row += 1

        self._param_widgets = {}

        # CORREÇÃO v2.6 — campos sempre mostravam os padrões do código:
        #   Os campos de parâmetros de monitoramento e thresholds usavam
        #   MONITOR_DEFAULTS e THRESHOLDS como valor inicial, ignorando
        #   completamente o que o usuário tivesse salvo em config.json.
        #   Após reiniciar o app, todos os valores voltavam ao padrão.
        #   Correção: carrega config.json e usa os valores salvos como default,
        #   caindo nos padrões do código apenas se o campo não estiver no arquivo.
        _saved_cfg = load_user_config()

        for i, (label, key, default, unit) in enumerate([
            ("Contagem de Ping", "ping_count",
             _saved_cfg.get("ping_count", str(MONITOR_DEFAULTS["ping_count"])), "pacotes"),
            ("Timeout do Ping", "ping_timeout",
             _saved_cfg.get("ping_timeout", str(MONITOR_DEFAULTS["ping_timeout_ms"])), "ms"),
            ("Delay entre ciclos", "cycle_delay",
             _saved_cfg.get("cycle_delay", str(MONITOR_DEFAULTS["cycle_delay_s"])), "seg"),
            ("Delay entre hosts", "between_hosts",
             _saved_cfg.get("between_hosts", str(MONITOR_DEFAULTS["between_hosts_s"])), "seg"),
        ]):
            self._build_param_row(params_frame, i, label, key, default, unit)

        # -- Thresholds ------------------------------------------------
        row = self._build_section(scroll, "Thresholds de Alerta", row)
        thresh_frame = ctk.CTkFrame(scroll, fg_color=COLORS["bg_secondary"],
                                     corner_radius=12, border_width=1, border_color=COLORS["border"])
        thresh_frame.grid(row=row, column=0, sticky="ew", pady=(0, 16))
        thresh_frame.grid_columnconfigure(1, weight=1)
        row += 1

        for i, (label, key, default, unit) in enumerate([
            ("Latência Warning", "lat_warn",
             _saved_cfg.get("lat_warn", str(THRESHOLDS["latency_warning_ms"])), "ms"),
            ("Latência Critical", "lat_crit",
             _saved_cfg.get("lat_crit", str(THRESHOLDS["latency_critical_ms"])), "ms"),
            ("Jitter Warning", "jit_warn",
             _saved_cfg.get("jit_warn", str(THRESHOLDS["jitter_warning_ms"])), "ms"),
            ("Jitter Critical", "jit_crit",
             _saved_cfg.get("jit_crit", str(THRESHOLDS["jitter_critical_ms"])), "ms"),
            ("Perda Warning", "loss_warn",
             _saved_cfg.get("loss_warn", str(THRESHOLDS["loss_warning_pct"])), "%"),
            ("Perda Critical", "loss_crit",
             _saved_cfg.get("loss_crit", str(THRESHOLDS["loss_critical_pct"])), "%"),
        ]):
            self._build_param_row(thresh_frame, i, label, key, default, unit)

        # -- Backup & Caminhos de Rede ---------------------------------
        row = self._build_section(scroll, "💾  Backup & Caminhos de Rede", row)

        backup_frame = ctk.CTkFrame(scroll, fg_color=COLORS["bg_secondary"],
                                     corner_radius=12, border_width=1, border_color=COLORS["border"])
        backup_frame.grid(row=row, column=0, sticky="ew", pady=(0, 16))
        backup_frame.grid_columnconfigure(0, weight=1)
        row += 1

        # Info do último backup
        self._backup_info_label = ctk.CTkLabel(
            backup_frame,
            text="Carregando info do backup...",
            font=(FONTS["family"], FONTS["size_xs"]),
            text_color=COLORS["text_secondary"],
            wraplength=600,
        )
        self._backup_info_label.grid(row=0, column=0, padx=16, pady=(12, 4), sticky="w")

        ctk.CTkLabel(
            backup_frame,
            text="O backup é executado diariamente de forma incremental (apenas arquivos novos/modificados).",
            font=(FONTS["family"], FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            wraplength=600,
        ).grid(row=1, column=0, padx=16, pady=(0, 8), sticky="w")

        # Botão de backup manual
        backup_btn_frame = ctk.CTkFrame(backup_frame, fg_color="transparent")
        backup_btn_frame.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))

        ctk.CTkButton(
            backup_btn_frame, text="▶  Executar Backup Agora", width=200,
            font=(FONTS["family"], FONTS["size_sm"], "bold"),
            fg_color=COLORS["accent_green"], hover_color="#059669",
            text_color="#FFFFFF", corner_radius=8, height=34,
            command=self._run_backup_now,
        ).pack(side="left", padx=(0, 8))

        self._backup_status_label = ctk.CTkLabel(
            backup_btn_frame, text="",
            font=(FONTS["family_mono"], FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
        )
        self._backup_status_label.pack(side="left")

        # Separador
        ctk.CTkFrame(backup_frame, fg_color=COLORS["border"], height=1).grid(
            row=3, column=0, sticky="ew", padx=16, pady=8)

        # Caminhos de rede
        ctk.CTkLabel(
            backup_frame,
            text="Caminhos de Rede para Backup Adicional:",
            font=(FONTS["family"], FONTS["size_sm"], "bold"),
            text_color=COLORS["text_primary"],
        ).grid(row=4, column=0, padx=16, pady=(4, 2), sticky="w")

        ctk.CTkLabel(
            backup_frame,
            text="O backup será replicado para cada caminho configurado.\n"
                 "Exemplos:  \\\\servidor\\backups\\netwatch  ou  Z:\\Backups\\NetWatch",
            font=(FONTS["family"], FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            justify="left",
        ).grid(row=5, column=0, padx=16, pady=(0, 6), sticky="w")

        # Entrada para novo caminho
        net_add_frame = ctk.CTkFrame(backup_frame, fg_color="transparent")
        net_add_frame.grid(row=6, column=0, sticky="ew", padx=16, pady=(0, 4))
        net_add_frame.grid_columnconfigure(0, weight=1)

        self._net_path_entry = ctk.CTkEntry(
            net_add_frame,
            placeholder_text="\\\\servidor\\backup ou Z:\\Backups\\NetWatch",
            font=(FONTS["family_mono"], FONTS["size_sm"]),
            fg_color=COLORS["bg_primary"], border_color=COLORS["border"],
            text_color=COLORS["text_primary"], corner_radius=6, height=34,
        )
        self._net_path_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        ctk.CTkButton(
            net_add_frame, text="+ Adicionar", width=110,
            font=(FONTS["family"], FONTS["size_sm"], "bold"),
            fg_color=COLORS["accent_blue"], hover_color=COLORS["accent_blue_hover"],
            text_color="#FFFFFF", corner_radius=8, height=34,
            command=self._add_network_path,
        ).grid(row=0, column=1)

        # Lista de caminhos configurados
        self._net_paths_frame = ctk.CTkFrame(
            backup_frame, fg_color=COLORS["bg_primary"],
            corner_radius=8, border_width=1, border_color=COLORS["border"],
        )
        self._net_paths_frame.grid(row=7, column=0, sticky="ew", padx=16, pady=(4, 16))
        self._net_paths_frame.grid_columnconfigure(0, weight=1)
        self._refresh_network_paths()
        self._refresh_backup_info()

        # Botão salvar
        ctk.CTkButton(scroll, text="💾  Salvar Todas as Configurações", width=260,
                       font=(FONTS["family"], FONTS["size_md"], "bold"),
                       fg_color=COLORS["accent_blue"], hover_color=COLORS["accent_blue_hover"],
                       text_color="#FFFFFF", corner_radius=8, height=40,
                       command=self._save_config).grid(row=row, column=0, pady=(8, 24))

    def _build_section(self, parent, title, row):
        ctk.CTkLabel(parent, text=title,
                      font=(FONTS["family"], FONTS["size_md"], "bold"),
                      text_color=COLORS["text_primary"]).grid(row=row, column=0, sticky="w", pady=(16, 8))
        return row + 1

    def _build_param_row(self, parent, row, label, key, default, unit):
        ctk.CTkLabel(parent, text=label, font=(FONTS["family"], FONTS["size_sm"]),
                      text_color=COLORS["text_secondary"]).grid(row=row, column=0, sticky="w", padx=16, pady=6)
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=1, sticky="e", padx=16, pady=6)
        entry = ctk.CTkEntry(frame, width=80, font=(FONTS["family_mono"], FONTS["size_sm"]),
                              fg_color=COLORS["bg_primary"], border_color=COLORS["border"],
                              text_color=COLORS["text_primary"], corner_radius=6, height=30)
        entry.insert(0, default)
        entry.pack(side="left")
        self._param_widgets[key] = entry
        ctk.CTkLabel(frame, text=unit, font=(FONTS["family"], FONTS["size_xs"]),
                      text_color=COLORS["text_muted"]).pack(side="left", padx=(4, 0))

    def _refresh_host_list(self):
        for w in self.hosts_frame.winfo_children():
            w.destroy()
        if not self.controller:
            return
        hosts = self.controller.get_all_hosts()
        search = self._host_search_var.get().strip().lower()
        status_filter = self._host_filter_var.get()
        filtered_hosts = []
        for host in hosts:
            searchable = " ".join([
                host.ip,
                host.label or "",
                host.group_name or "",
                getattr(host, "wan_ip", "") or "",
            ]).lower()
            if search and search not in searchable:
                continue
            if status_filter == "Online" and host.status != "online":
                continue
            if status_filter == "Offline" and host.status != "offline":
                continue
            filtered_hosts.append(host)
        hosts = filtered_hosts
        # Ordenação conforme _sort_var
        sort_key = self._sort_var.get()
        if sort_key == "↕ IP":
            # Ordena por octetos numéricos do IP
            def _ip_key(h):
                try:
                    return tuple(int(x) for x in h.ip.split("."))
                except Exception:
                    return (0, 0, 0, 0)
            hosts = sorted(hosts, key=_ip_key)
        else:
            # Padrão: ordenar por nome (label ou IP se sem label)
            hosts = sorted(hosts, key=lambda h: h.display_name.lower())
        self.hosts_meta_label.configure(text=f"{len(hosts)} host(s) exibido(s)")
        if not hosts:
            ctk.CTkLabel(self.hosts_frame, text="Nenhum host cadastrado.",
                          font=(FONTS["family"], self._get_zoom_font_size(FONTS["size_sm"])),
                          text_color=COLORS["text_muted"]).grid(row=0, column=0, columnspan=8, padx=16, pady=16)
            return

        # Zoom-aware font sizes
        zxs = self._get_zoom_font_size(FONTS["size_xs"])

        headers = ["", "IP", "Label", "Grupo", "Status", "Latência", "Perda", "Ações", "Áudio"]
        for i, h in enumerate(headers):
            ctk.CTkLabel(self.hosts_frame, text=h,
                          font=(FONTS["family"], zxs, "bold"),
                          text_color=COLORS["text_muted"]).grid(row=0, column=i, padx=4, pady=(10, 6), sticky="w")

        ctk.CTkFrame(self.hosts_frame, fg_color=COLORS["border"], height=1).grid(
            row=1, column=0, columnspan=8, sticky="ew", padx=8)

        for idx, host in enumerate(hosts, 2):
            sc = COLORS["accent_green"] if host.is_online else COLORS["accent_red"]
            if host.status == "unknown": sc = COLORS["text_muted"]
            ctk.CTkLabel(self.hosts_frame, text="●", font=(FONTS["family"], 14),
                          text_color=sc, width=30).grid(row=idx, column=0, padx=4, pady=3)
            ctk.CTkLabel(self.hosts_frame, text=host.ip,
                          font=(FONTS["family_mono"], zxs),
                          text_color=COLORS["text_primary"]).grid(row=idx, column=1, padx=4, pady=3, sticky="w")
            ctk.CTkLabel(self.hosts_frame, text=host.label or "—",
                          font=(FONTS["family"], zxs),
                          text_color=COLORS["text_secondary"]).grid(row=idx, column=2, padx=4, pady=3, sticky="w")
            ctk.CTkLabel(self.hosts_frame, text=host.group_name,
                          font=(FONTS["family"], zxs),
                          text_color=COLORS["text_secondary"]).grid(row=idx, column=3, padx=4, pady=3, sticky="w")
            st = host.status.upper() if host.status != "unknown" else "—"
            ctk.CTkLabel(self.hosts_frame, text=st,
                          font=(FONTS["family_mono"], zxs, "bold"),
                          text_color=sc).grid(row=idx, column=4, padx=4, pady=3, sticky="w")
            # v2.4: prioridade WAN > Google > HOST (mesmo que dashboard)
            if host.is_online:
                if host.wan_has_data and host.wan_latency > 0:
                    lat_val, loss_val = host.wan_latency, host.wan_loss
                elif host.google_has_data and host.google_latency > 0:
                    lat_val, loss_val = host.google_latency, host.google_loss
                else:
                    lat_val, loss_val = host.host_ssh_latency, host.host_ssh_loss
                lat = f"{lat_val:.0f}ms"
                loss = f"{loss_val:.0f}%"
            else:
                lat = "—"
                loss = "—"
            ctk.CTkLabel(self.hosts_frame, text=lat,
                          font=(FONTS["family_mono"], zxs),
                          text_color=COLORS["text_secondary"]).grid(row=idx, column=5, padx=4, pady=3, sticky="w")
            ctk.CTkLabel(self.hosts_frame, text=loss,
                          font=(FONTS["family_mono"], zxs),
                          text_color=COLORS["text_secondary"]).grid(row=idx, column=6, padx=4, pady=3, sticky="w")
            actions = ctk.CTkFrame(self.hosts_frame, fg_color="transparent")
            actions.grid(row=idx, column=7, padx=4, pady=3)
            ctk.CTkButton(actions, text="✎", width=28, height=24, font=(FONTS["family"], 12),
                           fg_color="transparent", hover_color=COLORS["bg_tertiary"],
                           text_color=COLORS["accent_blue"],
                           command=lambda h=host: self._edit_host(h)).pack(side="left", padx=1)
            ctk.CTkButton(actions, text="✕", width=28, height=24, font=(FONTS["family"], 12),
                           fg_color="transparent", hover_color=COLORS["accent_red_dim"],
                           text_color=COLORS["accent_red"],
                           command=lambda h=host: self._delete_host(h)).pack(side="left", padx=1)

            # -- Coluna de áudio ----------------------------------------
            audio_frame = ctk.CTkFrame(self.hosts_frame, fg_color="transparent")
            audio_frame.grid(row=idx, column=8, padx=4, pady=3)
            # Botão testar áudio offline
            test_btn = ctk.CTkButton(
                audio_frame, text="🔊 Testar", width=70, height=24,
                font=(FONTS["family"], FONTS["size_xs"]),
                fg_color=COLORS["bg_secondary"],
                hover_color=COLORS["bg_tertiary"],
                border_width=1, border_color=COLORS["accent_blue"],
                text_color=COLORS["accent_blue"],
                command=lambda h=host, b=None: self._test_audio(h),
            )
            test_btn.pack(side="left", padx=1)
            # Botão regenerar (redundância)
            regen_btn = ctk.CTkButton(
                audio_frame, text="⟳", width=28, height=24,
                font=(FONTS["family"], 13),
                fg_color="transparent",
                hover_color=COLORS["accent_green_dim"],
                border_width=1, border_color=COLORS["accent_green"],
                text_color=COLORS["accent_green"],
                command=lambda h=host: self._regen_audio(h),
            )
            regen_btn.pack(side="left", padx=1)

    def _clear_host_filters(self):
        self._host_search_var.set("")
        self._host_filter_var.set("Todos")
        # Mantém ordenação atual ao limpar filtros de busca/status
        self._refresh_host_list()


    def _test_audio(self, host):
        """
        Testa a sequência completa de alerta sonoro para o host:
        1. Alerta.mp3 (7 s)  2. Voz offline (2×)
        Executa em thread para não travar a UI.
        """
        if not self.controller:
            return

        # CORREÇÃO v2.6 — teste silencioso quando mutado:
        #   Antes, clicar em "Testar" com áudio mutado não produzia som nem
        #   feedback. O usuário ficava esperando sem saber se o teste falhou
        #   ou se o áudio estava simplesmente mudo.
        #   Correção: exibe um popup de aviso e interrompe o teste.
        if self.controller.audio.muted:
            dlg = ctk.CTkToplevel(self)
            dlg.title("Áudio Silenciado")
            dlg.geometry("340x130")
            dlg.resizable(False, False)
            dlg.configure(fg_color=COLORS["bg_primary"])
            dlg.transient(self.winfo_toplevel())
            dlg.grab_set()
            dlg.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                dlg,
                text="🔇  Áudio está silenciado.",
                font=(FONTS["family"], FONTS["size_md"], "bold"),
                text_color=COLORS["accent_yellow"],
            ).grid(row=0, column=0, padx=20, pady=(24, 4))
            ctk.CTkLabel(
                dlg,
                text="Ative o áudio na barra lateral antes de testar.",
                font=(FONTS["family"], FONTS["size_sm"]),
                text_color=COLORS["text_secondary"],
            ).grid(row=1, column=0, padx=20, pady=(0, 12))
            ctk.CTkButton(
                dlg, text="OK", width=80,
                font=(FONTS["family"], FONTS["size_sm"], "bold"),
                fg_color=COLORS["accent_blue"],
                hover_color=COLORS["accent_blue_hover"],
                text_color="#FFFFFF", corner_radius=8, height=30,
                command=dlg.destroy,
            ).grid(row=2, column=0, pady=(0, 16))
            return

        label = host.display_name
        audio = self.controller.audio

        def _run():
            # 1. Sirene
            audio.play_generic_alert()
            import time; time.sleep(ALERTA_DURATION_S + 0.5)
            # 2. Voz offline (play_alert já toca 2×)
            audio.play_alert(label, "offline")

        threading.Thread(target=_run, daemon=True).start()

    def _regen_audio(self, host):
        """
        Regenera os arquivos MP3 do host mesmo que já existam.
        Tenta edge-tts primeiro; fallback: pyttsx3.
        Exibe diálogo de status ao terminar.
        """
        if not self.controller:
            return

        label = host.display_name

        def _run():
            from controllers.audio_controller import _safe_filename, TTS_VOICE
            from controllers.audio_controller import _edge_tts_generate_sync
            from config import AUDIO_DIR

            safe = _safe_filename(label)
            pairs = [
                (f"{label}, está off-line.", AUDIO_DIR / f"{safe}_offline.mp3"),
                (f"{label}, está on-line.",  AUDIO_DIR / f"{safe}_online.mp3"),
            ]
            results = {}
            for text, path in pairs:
                # Tenta edge-tts (apaga o existente para forçar regerar)
                if path.exists():
                    path.unlink()
                ok = _edge_tts_generate_sync(text, TTS_VOICE, path)
                if not ok:
                    # Fallback: pyttsx3
                    ok = _pyttsx3_generate(text, path)
                results[path.name] = "? OK" if ok else "? Falhou"

            # Mostra resultado na thread da UI
            self.after(0, lambda: _show_result(results))

        def _pyttsx3_generate(text, path) -> bool:
            """Fallback TTS local via pyttsx3."""
            try:
                import pyttsx3
                from pathlib import Path
                wav_path = path.with_suffix(".wav")
                engine = pyttsx3.init()
                voices = engine.getProperty("voices")
                for v in voices:
                    if any(k in v.name.lower() for k in ("brazil", "portuguese", "francisca")):
                        engine.setProperty("voice", v.id)
                        break
                engine.setProperty("rate", 155)
                engine.save_to_file(text, str(wav_path))
                engine.runAndWait()
                engine.stop()
                # Renomeia .wav ? .mp3 (pygame aceita wav com extensão mp3 neste contexto)
                if wav_path.exists():
                    wav_path.rename(path)
                    return True
            except Exception as e:
                print(f"pyttsx3 falhou: {e}")
            return False

        def _show_result(results):
            _AudioResultDialog(self, label=label, results=results)

        threading.Thread(target=_run, daemon=True).start()

    def _add_host(self):
        existing = [h.ip for h in self.controller.get_all_hosts()] if self.controller else []
        AddHostDialog(self, on_save=self._on_host_saved, existing_ips=existing)

    def _edit_host(self, host):
        existing = [h.ip for h in self.controller.get_all_hosts()] if self.controller else []
        AddHostDialog(self, on_save=self._on_host_saved, host_data=host.to_dict(), existing_ips=existing)

    def _delete_host(self, host):
        if self.controller:
            self.controller.remove_host(host.id)
            self._refresh_host_list()

    def _on_host_saved(self, data):
        if not self.controller:
            return
        if "id" in data and data["id"]:
            old_host = self.controller.get_host(data["id"])
            old_label = old_host.display_name if old_host else ""
            new_label = data["label"] or data["ip"]

            self.controller.update_host(
                data["id"], ip=data["ip"], label=data["label"],
                group_name=data["group"], ssh_user=data["ssh_user"],
                ssh_password=data["ssh_password"], ssh_port=data["ssh_port"],
                wan_ip=data.get("wan_ip", ""),
                wan_ip_2=data.get("wan_ip_2", ""),
                wan_ip_3=data.get("wan_ip_3", ""),
                platform=data.get("platform", ""),
            )

            if old_label and old_label != new_label:
                self.controller.audio.delete_host_alerts(old_label)
                self.controller.audio.generate_host_alerts(new_label)
            else:
                self.controller.audio.generate_host_alerts(new_label)
        else:
            self.controller.add_host(
                data["ip"], data["label"], data["group"],
                data["ssh_user"], data["ssh_password"], data["ssh_port"],
                wan_ip=data.get("wan_ip", ""),
                wan_ip_2=data.get("wan_ip_2", ""),
                wan_ip_3=data.get("wan_ip_3", ""),
                platform=data.get("platform", ""),
            )
            label = data["label"] or data["ip"]
            self.controller.audio.generate_host_alerts(label)

        self._refresh_host_list()

    def _open_range_dialog(self):
        ImportRangeDialog(self, on_import=self._do_range_import)

    def _do_range_import(self, start, end, group_type, excluded,
                         ip_format="203.0.113.{N}", platform=""):
        if not self.controller:
            return
        for i in range(start, end + 1):
            if i in excluded:
                continue
            ip = ip_format.replace("{N}", str(i))
            num = i - start + 1
            if group_type == "Loja":
                label = f"Loja {num:02d}"
                group = "Lojas"
            else:
                label = f"Setor {num:02d}"
                group = "Setores"
            self.controller.add_host(ip, label, group, platform=platform)
        self._refresh_host_list()

    def _open_rename_group_dialog(self):
        if not self.controller:
            return
        groups = self.controller.get_group_names()
        if not groups:
            return
        RenameGroupDialog(self, groups=groups, on_rename=self._do_rename_group)

    def _do_rename_group(self, old_name: str, new_name: str):
        if self.controller:
            count = self.controller.rename_group(old_name, new_name)
            self._refresh_host_list()

    # -- Zoom ----------------------------------------------------------

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
        # Reconstroi a lista de hosts com fonte maior
        self._refresh_host_list()

    def _get_zoom_font_size(self, base_size: int) -> int:
        return int(base_size * self._zoom / 100)

    # -- Backup & Caminhos de Rede ------------------------------------

    def _refresh_backup_info(self):
        """Atualiza as informações do último backup."""
        try:
            from utils.backup import get_backup_info
            info = get_backup_info()
            text = (
                f"Último backup: {info['last_backup']}  ·  "
                f"Tamanho: {info['backup_size']}  ·  "
                f"Limite: {info['limit_mb']} MB  ·  "
                f"Local: {info['backup_path']}"
            )
            self._backup_info_label.configure(text=text)
        except Exception as e:
            self._backup_info_label.configure(text=f"Erro ao carregar info: {e}")

    def _run_backup_now(self):
        """Executa backup manual (local + rede) em background."""
        self._backup_status_label.configure(
            text="⏳ Executando backup...", text_color=COLORS["accent_yellow"]
        )

        def _on_done(results):
            if "error" in results:
                msg = f"❌ Falhou: {results['error']}"
                color = COLORS["accent_red"]
            else:
                elapsed = results.get("elapsed", "?")
                size = results.get("backup_size_mb", 0)
                net_count = len(results.get("network_results", []))
                net_ok = sum(1 for r in results.get("network_results", []) if "error" not in r)
                net_msg = f" · Rede: {net_ok}/{net_count}" if net_count > 0 else ""
                msg = f"✅ Concluído em {elapsed} · {size} MB{net_msg}"
                color = COLORS["accent_green"]
            try:
                self._backup_status_label.configure(text=msg, text_color=color)
                self._refresh_backup_info()
            except Exception:
                pass

        try:
            from utils.backup import run_backup_with_network_async
            run_backup_with_network_async(
                callback=lambda r: self.after(0, lambda: _on_done(r))
            )
        except Exception as e:
            self._backup_status_label.configure(
                text=f"❌ {e}", text_color=COLORS["accent_red"]
            )

    def _refresh_network_paths(self):
        """Atualiza a lista visual de caminhos de rede configurados."""
        for w in self._net_paths_frame.winfo_children():
            w.destroy()

        try:
            from utils.backup import get_network_backup_paths
            paths = get_network_backup_paths()
        except Exception:
            paths = []

        if not paths:
            ctk.CTkLabel(
                self._net_paths_frame,
                text="  Nenhum caminho de rede configurado.",
                font=(FONTS["family"], FONTS["size_xs"]),
                text_color=COLORS["text_muted"],
            ).grid(row=0, column=0, padx=12, pady=8, sticky="w")
            return

        for i, path in enumerate(paths):
            row_frame = ctk.CTkFrame(self._net_paths_frame, fg_color="transparent")
            row_frame.grid(row=i, column=0, sticky="ew", padx=8, pady=2)
            row_frame.grid_columnconfigure(0, weight=1)

            ctk.CTkLabel(
                row_frame,
                text=f"  📁  {path}",
                font=(FONTS["family_mono"], FONTS["size_xs"]),
                text_color=COLORS["text_primary"],
                anchor="w",
            ).grid(row=0, column=0, sticky="ew")

            ctk.CTkButton(
                row_frame, text="✕", width=28, height=24,
                font=(FONTS["family"], 12),
                fg_color="transparent",
                hover_color=COLORS["accent_red_dim"],
                text_color=COLORS["accent_red"],
                command=lambda p=path: self._remove_network_path(p),
            ).grid(row=0, column=1, padx=(4, 4))

    def _add_network_path(self):
        """Adiciona um caminho de rede para backup."""
        path = self._net_path_entry.get().strip()
        if not path:
            return
        try:
            from utils.backup import add_network_backup_path
            if add_network_backup_path(path):
                self._net_path_entry.delete(0, "end")
                self._refresh_network_paths()
            else:
                # Já existe
                self._net_path_entry.delete(0, "end")
        except Exception as e:
            logger.warning(f"Erro ao adicionar caminho de rede: {e}")

    def _remove_network_path(self, path: str):
        """Remove um caminho de rede do backup."""
        try:
            from utils.backup import remove_network_backup_path
            remove_network_backup_path(path)
            self._refresh_network_paths()
        except Exception as e:
            logger.warning(f"Erro ao remover caminho de rede: {e}")

    def _save_config(self):
        """
        Salvar Todas as Configurações.

        CORREÇÃO v2.10 — dupla gravação (belt and suspenders):
          1. SEMPRE grava config.json diretamente (pasta compartilhada — tanto
             o viewer quanto o servidor têm acesso de escrita).
          2. Se em modo viewer, TAMBÉM envia comando 'update_config' para o
             servidor atualizar os valores em memória (ping_count, cycle_delay,
             THRESHOLDS). Sem isto, o servidor só releria o config.json no
             próximo restart.

          A versão v2.9 quebrava porque substituía a gravação direta por
          APENAS a fila de comandos. Se a fila falhasse (SMB cache, antivírus
          bloqueando rename, race condition), o config.json nunca era atualizado.
        """
        cfg = {}
        for key, widget in self._param_widgets.items():
            cfg[key] = widget.get().strip()
        # Credenciais SSH padrão
        cfg["ssh_default_user"] = self.ssh_default_user.get().strip()
        cfg["ssh_default_password"] = self.ssh_default_pass.get().strip()
        # IP alvo Google/MTR
        google_ip = self.google_target_entry.get().strip() or "8.8.8.8"
        cfg["google_target"] = google_ip

        # ── 1. SEMPRE grava config.json diretamente (compartilhado) ────
        try:
            set_google_target(google_ip)
            save_user_config(cfg)
            logger.info("Configurações salvas em config.json")
        except Exception as e:
            logger.error(f"Erro ao salvar config.json: {e}")

        # ── 2. Se viewer: TAMBÉM envia comando para atualizar memória ──
        if self.controller and self.controller.is_viewer:
            try:
                self.controller.send_config_update(cfg)
                logger.info(
                    "[VIEWER] Comando update_config enviado ao servidor "
                    "para atualizar memória"
                )
            except Exception as e:
                logger.warning(f"[VIEWER] Erro ao enviar update_config: {e}")

        # ── 3. Aplica em memória local (ambos os modos) ───────────────
        if self.controller:
            try:
                self.controller.ping_count = int(cfg.get("ping_count", 4))
                self.controller.ping_timeout = int(cfg.get("ping_timeout", 1000))
                self.controller.cycle_delay = int(cfg.get("cycle_delay", 10))
                self.controller.between_hosts = float(cfg.get("between_hosts", 0.5))
            except ValueError:
                pass

            # Aplica thresholds em memória (efeito imediato)
            from config import THRESHOLDS as _T
            _threshold_map = {
                "lat_warn":   ("latency_warning_ms",  int),
                "lat_crit":   ("latency_critical_ms", int),
                "jit_warn":   ("jitter_warning_ms",   int),
                "jit_crit":   ("jitter_critical_ms",  int),
                "loss_warn":  ("loss_warning_pct",    float),
                "loss_crit":  ("loss_critical_pct",   float),
            }
            for cfg_key, (thresh_key, cast) in _threshold_map.items():
                raw = cfg.get(cfg_key, "")
                if raw:
                    try:
                        _T[thresh_key] = cast(raw)
                    except (ValueError, TypeError):
                        pass

            # Regenera áudios pendentes para TODOS os hosts (background)
            def _regen_all():
                for host in self.controller.get_all_hosts():
                    label = host.display_name
                    if label:
                        self.controller.audio.generate_host_alerts(label)

            threading.Thread(target=_regen_all, daemon=True, name="regen-all").start()

class _AudioResultDialog(ctk.CTkToplevel):
    """Popup que mostra o resultado da regeneração de áudio."""

    def __init__(self, master, label: str, results: dict, **kwargs):
        super().__init__(master, **kwargs)
        self.title("Resultado — Regenerar Áudio")
        self.geometry("380x220")
        self.resizable(False, False)
        self.grab_set()
        self.configure(fg_color=COLORS["bg_secondary"])

        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            self, text=f"Regeneração de áudio — {label}",
            font=(FONTS["family"], FONTS["size_sm"], "bold"),
            text_color=COLORS["text_primary"],
        ).grid(row=0, column=0, padx=20, pady=(18, 4), sticky="w")

        for i, (fname, status) in enumerate(results.items()):
            ok = status.startswith("?")
            color = COLORS["accent_green"] if ok else COLORS["accent_red"]
            ctk.CTkLabel(
                self,
                text=f"  {status}  {fname}",
                font=(FONTS["family_mono"], FONTS["size_xs"]),
                text_color=color,
                anchor="w",
            ).grid(row=i + 1, column=0, padx=20, pady=2, sticky="w")

        # Nota sobre fallback
        note_row = len(results) + 1
        ctk.CTkLabel(
            self,
            text=(
                "Método principal: edge-tts (FranciscaNeural)\n"
                "Fallback automático: pyttsx3 (voz local)"
            ),
            font=(FONTS["family"], FONTS["size_xs"]),
            text_color=COLORS["text_muted"],
            justify="left",
        ).grid(row=note_row, column=0, padx=20, pady=(8, 4), sticky="w")

        ctk.CTkButton(
            self, text="Fechar", width=100,
            font=(FONTS["family"], FONTS["size_sm"]),
            fg_color=COLORS["accent_blue"],
            hover_color=COLORS["accent_blue_hover"],
            text_color="#FFFFFF",
            corner_radius=8, height=32,
            command=self.destroy,
        ).grid(row=note_row + 1, column=0, pady=(8, 16))
