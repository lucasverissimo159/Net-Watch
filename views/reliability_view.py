"""
Reliability View v2.12 — Análise completa de confiabilidade.

Tabs:
  1. SLA & Métricas       — Uptime, MTBF, MTTR, MTTF, Error Budget, Burn Rate, Pareto
  2. Performance          — Percentis p50/p95/p99, Apdex, Tendência, Anomalias
  3. Cohort & Correlação  — Análise por plataforma/grupo, clusters de outage
  4. Diagnóstico (RCA)    — Análise de causa raiz + estabilidade de rota + QoS

Criado por Lucas Veríssimo
"""
import threading
import customtkinter as ctk
from datetime import datetime

from config import COLORS, FONTS
from utils.logger import setup_logger
from views.widgets import BarChart, TrendLineChart, PercentileBars

logger = setup_logger("reliability_view")


class ReliabilityView(ctk.CTkFrame):
    def __init__(self, master, controller=None, db=None, **kwargs):
        super().__init__(master, fg_color=COLORS["bg_primary"], **kwargs)
        self.controller = controller
        self.db = db
        self._sla_data = []
        self._sla_days = 30
        self._trend_host_map = {}

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._build_tabs()

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 8))
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(header, text="Análise de Confiabilidade",
                     font=(FONTS["family"], FONTS["size_xl"], "bold"),
                     text_color=COLORS["text_primary"]).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(header,
                     text="SLA · Performance · Tendências · Diagnóstico",
                     font=(FONTS["family"], FONTS["size_sm"]),
                     text_color=COLORS["text_secondary"]).grid(row=1, column=0, sticky="w")

    def _build_tabs(self):
        self.tabview = ctk.CTkTabview(
            self, fg_color=COLORS["bg_secondary"],
            segmented_button_fg_color=COLORS["bg_tertiary"],
            segmented_button_selected_color=COLORS["accent_blue"],
            segmented_button_unselected_color=COLORS["bg_tertiary"],
            text_color=COLORS["text_primary"], corner_radius=12,
        )
        self.tabview.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 16))

        self.tab_sla = self.tabview.add("📊 SLA & Métricas")
        self.tab_perf = self.tabview.add("⚡ Performance")
        self.tab_cohort = self.tabview.add("🔬 Cohort & Correlação")
        self.tab_rca = self.tabview.add("🔍 Diagnóstico (RCA)")

        self._build_sla_tab()
        self._build_perf_tab()
        self._build_cohort_tab()
        self._build_rca_tab()

    # ══════════════════════════════════════════════════════════════════
    # TAB 1 — SLA & MÉTRICAS GLOBAIS
    # ══════════════════════════════════════════════════════════════════

    def _build_sla_tab(self):
        tab = self.tab_sla
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(3, weight=1)

        # ── Controles ──
        ctrl = ctk.CTkFrame(tab, fg_color="transparent")
        ctrl.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))

        ctk.CTkLabel(ctrl, text="Período:",
                     font=(FONTS["family"], FONTS["size_sm"]),
                     text_color=COLORS["text_secondary"]).pack(side="left", padx=(0, 8))

        self.sla_period = ctk.CTkComboBox(
            ctrl, values=["7 dias", "15 dias", "30 dias", "60 dias", "90 dias"],
            font=(FONTS["family"], FONTS["size_sm"]),
            fg_color=COLORS["bg_primary"], border_color=COLORS["border"],
            text_color=COLORS["text_primary"], width=120, state="readonly")
        self.sla_period.pack(side="left")
        self.sla_period.set("30 dias")

        ctk.CTkLabel(ctrl, text="Alvo SLA:",
                     font=(FONTS["family"], FONTS["size_sm"]),
                     text_color=COLORS["text_secondary"]).pack(side="left", padx=(16, 8))

        self.sla_target = ctk.CTkComboBox(
            ctrl, values=["99.9%", "99.5%", "99.0%", "98.0%", "95.0%"],
            font=(FONTS["family"], FONTS["size_sm"]),
            fg_color=COLORS["bg_primary"], border_color=COLORS["border"],
            text_color=COLORS["text_primary"], width=100, state="readonly")
        self.sla_target.pack(side="left")
        self.sla_target.set("99.9%")

        ctk.CTkButton(ctrl, text="🔄 Calcular SLA", width=140,
                       font=(FONTS["family"], FONTS["size_sm"], "bold"),
                       fg_color=COLORS["accent_blue"],
                       hover_color=COLORS["accent_blue_hover"],
                       text_color="#FFFFFF", corner_radius=8, height=32,
                       command=self._calc_sla).pack(side="left", padx=8)

        ctk.CTkButton(ctrl, text="📄 Gerar Relatório", width=140,
                       font=(FONTS["family"], FONTS["size_sm"]),
                       fg_color=COLORS["accent_green"], hover_color="#059669",
                       text_color="#FFFFFF", corner_radius=8, height=32,
                       command=self._gen_report).pack(side="left", padx=4)

        self.sla_status = ctk.CTkLabel(ctrl, text="",
            font=(FONTS["family"], FONTS["size_xs"]),
            text_color=COLORS["text_muted"])
        self.sla_status.pack(side="right", padx=8)

        # ── Cards de resumo — linha 1 (métricas básicas) ──
        sum1 = ctk.CTkFrame(tab, fg_color="transparent")
        sum1.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        for i in range(5):
            sum1.grid_columnconfigure(i, weight=1)

        self._sla_cards = {}
        for i, (key, label, icon) in enumerate([
            ("uptime", "Uptime Médio", "📊"),
            ("outages", "Total Quedas", "⚡"),
            ("mtbf", "MTBF", "🔄"),
            ("mttr", "MTTR", "⏱"),
            ("mttf", "MTTF", "✓"),
        ]):
            self._sla_cards[key] = self._make_card(sum1, label, icon, "—", col=i, row=0)

        # ── Cards de resumo — linha 2 (métricas avançadas) ──
        sum2 = ctk.CTkFrame(tab, fg_color="transparent")
        sum2.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
        for i in range(5):
            sum2.grid_columnconfigure(i, weight=1)

        self._sla_cards["apdex"] = self._make_card(sum2, "Apdex Score", "🎯", "—", col=0, row=0)
        self._sla_cards["error_budget"] = self._make_card(sum2, "Error Budget Rest.", "💰", "—", col=1, row=0)
        self._sla_cards["burn_rate"] = self._make_card(sum2, "Burn Rate", "🔥", "—", col=2, row=0)
        self._sla_cards["failure_rate"] = self._make_card(sum2, "Failure Rate", "λ", "—", col=3, row=0)
        self._sla_cards["forecast"] = self._make_card(sum2, "Forecast Breach", "🔮", "—", col=4, row=0)

        # ── Scroll com tabela + Pareto + Clusters ──
        self.sla_scroll = ctk.CTkScrollableFrame(
            tab, fg_color=COLORS["bg_primary"],
            corner_radius=8, border_width=1, border_color=COLORS["border"])
        self.sla_scroll.grid(row=3, column=0, sticky="nsew", padx=8, pady=4)
        self.sla_scroll.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self.sla_scroll,
            text="Clique em \"Calcular SLA\" para gerar as métricas.\n\n"
                 "Você verá:\n"
                 "• Uptime, MTBF, MTTR, MTTF, Failure Rate (λ)\n"
                 "• Apdex Score, Error Budget e Burn Rate\n"
                 "• Forecast de violação de SLA\n"
                 "• Análise de Pareto — top contribuidores de downtime\n"
                 "• Comparação período-vs-período\n"
                 "• Tabela detalhada por host",
            font=(FONTS["family"], FONTS["size_sm"]),
            text_color=COLORS["text_muted"], justify="center").pack(pady=40)

    def _make_card(self, parent, label: str, icon: str, value: str,
                    col: int, row: int = 0):
        card = ctk.CTkFrame(parent, fg_color=COLORS["bg_primary"],
                             corner_radius=10, border_width=1,
                             border_color=COLORS["border"])
        card.grid(row=row, column=col, sticky="ew", padx=4, pady=4)
        ctk.CTkLabel(card, text=f"{icon} {label}",
                     font=(FONTS["family"], FONTS["size_xs"]),
                     text_color=COLORS["text_muted"]).pack(padx=12, pady=(8, 0))
        val = ctk.CTkLabel(card, text=value,
                           font=(FONTS["family_mono"], FONTS["size_lg"], "bold"),
                           text_color=COLORS["text_primary"])
        val.pack(padx=12, pady=(0, 8))
        return val

    def _calc_sla(self):
        if not self.controller or not self.db:
            return
        days = int(self.sla_period.get().split()[0])
        target_str = self.sla_target.get().replace("%", "")
        try:
            target = float(target_str)
        except ValueError:
            target = 99.9
        self.sla_status.configure(text="Calculando...")

        def _run():
            from utils.reliability import (calculate_group_sla, calculate_error_budget,
                                             calculate_mttf_lambda, calculate_apdex,
                                             pareto_analysis, detect_outage_clusters,
                                             compare_periods)
            results = calculate_group_sla(self.db, self.controller, days)
            # Calcula agregados
            total_outages = sum(r["total_outages"] for r in results)
            total_downtime = sum(r["total_downtime_s"] for r in results)
            agg_sla = {
                "period_days": days,
                "total_outages": total_outages,
                "total_downtime_s": total_downtime,
            }
            budget = calculate_error_budget(agg_sla, target)
            mttf_agg = calculate_mttf_lambda(agg_sla)
            apdex_vals = []
            for r in results:
                a = calculate_apdex(self.db, r["host_id"], days)
                apdex_vals.append(a.get("score", 0))
                r["apdex"] = a

            pareto = pareto_analysis(results)
            clusters = detect_outage_clusters(self.db, self.controller, days)
            period_compare = compare_periods(self.db, self.controller, days)

            try:
                self.after(0, lambda: self._show_sla_results(
                    results, days, target, budget, mttf_agg,
                    apdex_vals, pareto, clusters, period_compare))
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def _show_sla_results(self, results, days, target, budget, mttf_agg,
                            apdex_vals, pareto, clusters, period_compare):
        self._sla_data = results
        self._sla_days = days

        from utils.reliability import _format_duration

        if results:
            avg_up = sum(r["uptime_pct"] for r in results) / len(results)
            total_out = sum(r["total_outages"] for r in results)
            mtbf_vals = [r["mtbf_s"] for r in results if r["mtbf_s"] > 0]
            mttr_vals = [r["mttr_s"] for r in results if r["mttr_s"] > 0]
            avg_mtbf = sum(mtbf_vals) / len(mtbf_vals) if mtbf_vals else 0
            avg_mttr = sum(mttr_vals) / len(mttr_vals) if mttr_vals else 0
            avg_apdex = sum(apdex_vals) / len(apdex_vals) if apdex_vals else 0

            color_up = (COLORS["accent_green"] if avg_up >= 99.5 else
                        COLORS["accent_yellow"] if avg_up >= 98 else COLORS["accent_red"])

            self._sla_cards["uptime"].configure(text=f"{avg_up:.2f}%", text_color=color_up)
            self._sla_cards["outages"].configure(text=str(total_out))
            self._sla_cards["mtbf"].configure(text=_format_duration(avg_mtbf))
            self._sla_cards["mttr"].configure(text=_format_duration(avg_mttr))
            self._sla_cards["mttf"].configure(text=mttf_agg["mttf_str"])

            # Apdex color
            apdex_color = (COLORS["accent_green"] if avg_apdex >= 0.94 else
                           COLORS["accent_blue"] if avg_apdex >= 0.85 else
                           COLORS["accent_yellow"] if avg_apdex >= 0.70 else
                           COLORS["accent_red"])
            self._sla_cards["apdex"].configure(text=f"{avg_apdex:.2f}", text_color=apdex_color)

            # Error budget
            budget_color = (COLORS["accent_red"] if budget["status"] == "breached" else
                            COLORS["accent_yellow"] if budget["status"] == "critical" else
                            COLORS["accent_green"])
            self._sla_cards["error_budget"].configure(
                text=budget["budget_remaining_str"], text_color=budget_color)

            # Burn rate
            br = budget["burn_rate_pct"]
            br_color = (COLORS["accent_red"] if br >= 100 else
                        COLORS["accent_yellow"] if br >= 50 else
                        COLORS["accent_green"])
            self._sla_cards["burn_rate"].configure(text=f"{br:.0f}%", text_color=br_color)

            # Failure rate
            fr = mttf_agg["failure_rate_per_day"]
            self._sla_cards["failure_rate"].configure(
                text=f"{fr:.2f}/dia")

            self._sla_cards["forecast"].configure(text=budget["forecast_breach_str"])

        # Clear scroll
        for w in self.sla_scroll.winfo_children():
            w.destroy()

        if not results:
            ctk.CTkLabel(self.sla_scroll, text="Sem dados no período.",
                         font=(FONTS["family"], FONTS["size_sm"]),
                         text_color=COLORS["text_muted"]).pack(pady=40)
            self.sla_status.configure(text="")
            return

        # ── Período vs período (badge no topo) ──
        if period_compare.get("available"):
            trend = period_compare["trend"]
            delta_str = period_compare["delta_uptime_str"]
            color = (COLORS["accent_green"] if trend == "better" else
                     COLORS["accent_red"] if trend == "worse" else
                     COLORS["text_secondary"])
            icon = "📈" if trend == "better" else "📉" if trend == "worse" else "➡"
            cmp_frame = ctk.CTkFrame(self.sla_scroll,
                fg_color=COLORS["bg_secondary"], corner_radius=8)
            cmp_frame.pack(fill="x", padx=4, pady=(8, 4))
            ctk.CTkLabel(cmp_frame,
                text=f"{icon} Comparação vs {days} dias anteriores: "
                     f"{delta_str} de uptime ({period_compare['delta_outages']:+d} quedas)",
                font=(FONTS["family"], FONTS["size_sm"], "bold"),
                text_color=color).pack(padx=12, pady=8)

        # ── Pareto Analysis ──
        if pareto["top_contributors"]:
            ctk.CTkLabel(self.sla_scroll,
                text=f"📊 Análise de Pareto — {pareto['pareto_count']} hosts "
                     f"({pareto['pareto_pct']:.0f}%) causam 80% do downtime",
                font=(FONTS["family"], FONTS["size_md"], "bold"),
                text_color=COLORS["text_primary"]).pack(anchor="w",
                padx=8, pady=(12, 4))

            bar_data = [
                {"label": c["host_label"], "value": c["downtime_s"] / 60}
                for c in pareto["top_contributors"][:10]
            ]
            BarChart(self.sla_scroll, data=bar_data, max_bars=10,
                     value_format="{:.1f}min", color_scheme="reverse",
                     title="Top 10 hosts por downtime").pack(
                fill="x", padx=4, pady=(0, 8))

        # ── Outage Clusters ──
        if clusters:
            ctk.CTkLabel(self.sla_scroll,
                text=f"🔗 Clusters de Outage ({len(clusters)} eventos)",
                font=(FONTS["family"], FONTS["size_md"], "bold"),
                text_color=COLORS["text_primary"]).pack(anchor="w",
                padx=8, pady=(12, 4))
            ctk.CTkLabel(self.sla_scroll,
                text="Múltiplos hosts caindo em <60s = provável problema da "
                     "operadora ou upstream",
                font=(FONTS["family"], FONTS["size_xs"]),
                text_color=COLORS["text_muted"]).pack(anchor="w", padx=8, pady=(0, 4))

            for cluster in clusters[-5:]:
                row = ctk.CTkFrame(self.sla_scroll,
                    fg_color=COLORS["bg_secondary"], corner_radius=6)
                row.pack(fill="x", padx=4, pady=2)
                ctk.CTkLabel(row,
                    text=f"⚠ {cluster['start']} — "
                         f"{cluster['hosts_affected']} hosts afetados "
                         f"({', '.join(cluster['hosts'][:3])}"
                         f"{'…' if len(cluster['hosts']) > 3 else ''})",
                    font=(FONTS["family"], FONTS["size_xs"]),
                    text_color=COLORS["accent_yellow"],
                    anchor="w").pack(fill="x", padx=12, pady=6)

        # ── Tabela detalhada ──
        ctk.CTkLabel(self.sla_scroll, text="📋 Detalhe por Host",
            font=(FONTS["family"], FONTS["size_md"], "bold"),
            text_color=COLORS["text_primary"]).pack(anchor="w",
            padx=8, pady=(16, 4))

        cols = ["Host", "IP", "Uptime", "Quedas", "MTBF", "MTTR",
                "Apdex", "Plat.", target_str := f"{target:.1f}%"]
        weights = [3, 2, 1, 1, 1, 1, 1, 1, 1]

        hdr = ctk.CTkFrame(self.sla_scroll, fg_color=COLORS["bg_tertiary"],
                            corner_radius=6)
        hdr.pack(fill="x", padx=4, pady=(4, 2))
        for i in range(len(cols)):
            hdr.grid_columnconfigure(i, weight=weights[i])
        for i, col in enumerate(cols):
            ctk.CTkLabel(hdr, text=col,
                font=(FONTS["family"], FONTS["size_xs"], "bold"),
                text_color=COLORS["text_muted"]).grid(
                row=0, column=i, padx=8, pady=6, sticky="w")

        for r in results:
            row = ctk.CTkFrame(self.sla_scroll, fg_color="transparent")
            row.pack(fill="x", padx=4, pady=1)
            for i in range(len(cols)):
                row.grid_columnconfigure(i, weight=weights[i])

            up = r["uptime_pct"]
            color = (COLORS["accent_green"] if up >= 99.5 else
                     COLORS["accent_yellow"] if up >= 98 else COLORS["accent_red"])

            apdex_score = r.get("apdex", {}).get("score", 0)
            from utils.device_profiles import get_platform_label

            vals = [
                r.get("host_label", ""),
                r.get("host_ip", ""),
                f"{up:.2f}%",
                str(r["total_outages"]),
                r["mtbf_str"],
                r["mttr_str"],
                f"{apdex_score:.2f}",
                get_platform_label(r.get("platform", "")),
                "✅" if up >= target else "❌",
            ]
            colors = [COLORS["text_primary"], COLORS["text_secondary"], color,
                      COLORS["text_primary"], COLORS["text_secondary"],
                      COLORS["text_secondary"], COLORS["text_primary"],
                      COLORS["text_secondary"],
                      COLORS["accent_green"] if up >= target else COLORS["accent_red"]]
            for i, (val, clr) in enumerate(zip(vals, colors)):
                ctk.CTkLabel(row, text=val,
                    font=(FONTS["family_mono" if i > 0 and i < 8 else "family"],
                          FONTS["size_xs"]),
                    text_color=clr).grid(row=0, column=i, padx=8, pady=4, sticky="w")

        self.sla_status.configure(
            text=f"✓ {len(results)} hosts — {days} dias — alvo {target:.1f}%")

    def _gen_report(self):
        if not self._sla_data:
            self.sla_status.configure(text="⚠ Calcule o SLA primeiro")
            return
        from utils.report_generator import generate_sla_report, open_report
        path = generate_sla_report(self._sla_data, self._sla_days)
        if path:
            open_report(path)
            self.sla_status.configure(text="Relatório aberto no navegador")

    # ══════════════════════════════════════════════════════════════════
    # TAB 2 — PERFORMANCE (percentis, Apdex, tendência, anomalias)
    # ══════════════════════════════════════════════════════════════════

    def _build_perf_tab(self):
        tab = self.tab_perf
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        ctrl = ctk.CTkFrame(tab, fg_color="transparent")
        ctrl.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))

        ctk.CTkLabel(ctrl, text="Host:",
            font=(FONTS["family"], FONTS["size_sm"]),
            text_color=COLORS["text_secondary"]).pack(side="left", padx=(0, 4))

        self.perf_host_combo = ctk.CTkComboBox(
            ctrl, values=["(selecione)"],
            font=(FONTS["family"], FONTS["size_sm"]),
            fg_color=COLORS["bg_primary"], border_color=COLORS["border"],
            text_color=COLORS["text_primary"], width=220, state="readonly")
        self.perf_host_combo.pack(side="left", padx=4)

        ctk.CTkLabel(ctrl, text="Período:",
            font=(FONTS["family"], FONTS["size_sm"]),
            text_color=COLORS["text_secondary"]).pack(side="left", padx=(12, 4))

        self.perf_period = ctk.CTkComboBox(
            ctrl, values=["3 dias", "7 dias", "15 dias", "30 dias"],
            font=(FONTS["family"], FONTS["size_sm"]),
            fg_color=COLORS["bg_primary"], border_color=COLORS["border"],
            text_color=COLORS["text_primary"], width=100, state="readonly")
        self.perf_period.pack(side="left")
        self.perf_period.set("7 dias")

        ctk.CTkButton(ctrl, text="⚡ Analisar", width=120,
            font=(FONTS["family"], FONTS["size_sm"], "bold"),
            fg_color=COLORS["accent_blue"],
            hover_color=COLORS["accent_blue_hover"],
            text_color="#FFFFFF", corner_radius=8, height=32,
            command=self._calc_perf).pack(side="left", padx=8)

        self.perf_scroll = ctk.CTkScrollableFrame(
            tab, fg_color=COLORS["bg_primary"],
            corner_radius=8, border_width=1, border_color=COLORS["border"])
        self.perf_scroll.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        self.perf_scroll.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self.perf_scroll,
            text="Análise profunda de performance por host.\n\n"
                 "Inclui:\n"
                 "• Percentis p50 / p95 / p99 de latência, jitter e perda\n"
                 "• Apdex Score detalhado (satisfied / tolerating / frustrated)\n"
                 "• Tendência diária com chart\n"
                 "• Anomalias detectadas (z-score >= 3)\n"
                 "• Score de degradação 0-100",
            font=(FONTS["family"], FONTS["size_sm"]),
            text_color=COLORS["text_muted"], justify="center").pack(pady=40)

    def _calc_perf(self):
        if not self.controller or not self.db:
            return
        sel = self.perf_host_combo.get()
        if sel == "(selecione)":
            return
        host_id = self._trend_host_map.get(sel)
        if not host_id:
            return
        days = int(self.perf_period.get().split()[0])

        def _run():
            host = self.controller.get_host(host_id)
            if not host:
                return
            from utils.reliability import comprehensive_host_report
            report = comprehensive_host_report(self.db, host, days)
            try:
                self.after(0, lambda: self._show_perf(report, sel))
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def _show_perf(self, report: dict, host_label: str):
        for w in self.perf_scroll.winfo_children():
            w.destroy()

        # ── Header com QoS classification ──
        from utils.reliability import classify_qos
        sla = report["sla"]
        percentiles = report["percentiles"]
        apdex = report["apdex"]
        eb = report["error_budget_999"]
        mttf = report["mttf"]
        trends = report["trends"]
        anomalies = report["anomalies"]
        route = report["route"]

        avg_lat = percentiles.get("latency", {}).get("avg", 0)
        p95_jit = percentiles.get("jitter", {}).get("p95", 0)
        avg_loss = percentiles.get("loss", {}).get("p50", 0)
        qos = classify_qos(avg_lat, p95_jit, avg_loss)

        hdr = ctk.CTkFrame(self.perf_scroll, fg_color=COLORS["bg_secondary"],
                            corner_radius=10)
        hdr.pack(fill="x", padx=4, pady=(4, 8))
        ctk.CTkLabel(hdr, text=host_label,
            font=(FONTS["family"], FONTS["size_md"], "bold"),
            text_color=COLORS["text_primary"]).pack(side="left", padx=12, pady=12)
        ctk.CTkLabel(hdr, text=f" QoS: {qos['label']} ",
            font=(FONTS["family_mono"], FONTS["size_sm"], "bold"),
            text_color="#FFFFFF",
            fg_color=qos["color"], corner_radius=6).pack(side="left", padx=8)
        ctk.CTkLabel(hdr, text=f"Uptime: {sla['uptime_pct']:.2f}%  ·  "
                                f"Samples: {percentiles.get('samples', 0)}",
            font=(FONTS["family"], FONTS["size_xs"]),
            text_color=COLORS["text_muted"]).pack(side="right", padx=12)

        # ── Percentis ──
        ctk.CTkLabel(self.perf_scroll, text="📊 Percentis de Performance",
            font=(FONTS["family"], FONTS["size_md"], "bold"),
            text_color=COLORS["text_primary"]).pack(anchor="w", padx=8, pady=(8, 4))

        pct_grid = ctk.CTkFrame(self.perf_scroll, fg_color="transparent")
        pct_grid.pack(fill="x", padx=4)
        pct_grid.grid_columnconfigure((0, 1, 2), weight=1)

        lat_p = percentiles.get("latency", {})
        jit_p = percentiles.get("jitter", {})
        loss_p = percentiles.get("loss", {})

        PercentileBars(pct_grid, p50=lat_p.get("p50", 0),
                        p95=lat_p.get("p95", 0), p99=lat_p.get("p99", 0),
                        title="Latência", unit="ms").grid(
            row=0, column=0, padx=4, pady=4, sticky="ew")
        PercentileBars(pct_grid, p50=jit_p.get("p50", 0),
                        p95=jit_p.get("p95", 0), p99=jit_p.get("p99", 0),
                        title="Jitter", unit="ms").grid(
            row=0, column=1, padx=4, pady=4, sticky="ew")
        PercentileBars(pct_grid, p50=loss_p.get("p50", 0),
                        p95=loss_p.get("p95", 0), p99=loss_p.get("p99", 0),
                        title="Perda", unit="%").grid(
            row=0, column=2, padx=4, pady=4, sticky="ew")

        # ── Apdex breakdown ──
        ctk.CTkLabel(self.perf_scroll, text="🎯 Apdex Score Detalhado",
            font=(FONTS["family"], FONTS["size_md"], "bold"),
            text_color=COLORS["text_primary"]).pack(anchor="w", padx=8, pady=(12, 4))

        ap_card = ctk.CTkFrame(self.perf_scroll,
            fg_color=COLORS["bg_secondary"], corner_radius=8)
        ap_card.pack(fill="x", padx=4, pady=2)
        ap_card.grid_columnconfigure((0, 1, 2, 3, 4), weight=1)

        score = apdex.get("score", 0)
        ap_color = (COLORS["accent_green"] if score >= 0.94 else
                    COLORS["accent_blue"] if score >= 0.85 else
                    COLORS["accent_yellow"] if score >= 0.70 else
                    COLORS["accent_red"])

        ap_items = [
            ("Score", f"{score:.3f}", ap_color),
            ("Rating", apdex.get("rating", "—"), ap_color),
            ("Satisfeitos", str(apdex.get("satisfied", 0)), COLORS["accent_green"]),
            ("Tolerantes", str(apdex.get("tolerating", 0)), COLORS["accent_yellow"]),
            ("Frustrados", str(apdex.get("frustrated", 0)), COLORS["accent_red"]),
        ]
        for i, (label, val, color) in enumerate(ap_items):
            f = ctk.CTkFrame(ap_card, fg_color="transparent")
            f.grid(row=0, column=i, padx=12, pady=12)
            ctk.CTkLabel(f, text=label,
                font=(FONTS["family"], FONTS["size_xs"]),
                text_color=COLORS["text_muted"]).pack()
            ctk.CTkLabel(f, text=val,
                font=(FONTS["family_mono"], FONTS["size_md"], "bold"),
                text_color=color).pack()

        # ── Error Budget ──
        ctk.CTkLabel(self.perf_scroll, text="💰 Error Budget (SLA 99.9%)",
            font=(FONTS["family"], FONTS["size_md"], "bold"),
            text_color=COLORS["text_primary"]).pack(anchor="w", padx=8, pady=(12, 4))

        eb_text = (f"Budget restante: {eb['budget_remaining_str']}  ·  "
                    f"Burn rate: {eb['burn_rate_pct']:.0f}%  ·  "
                    f"Status: {eb['status'].upper()}  ·  "
                    f"Forecast: {eb['forecast_breach_str']}")
        eb_color = (COLORS["accent_red"] if eb["status"] == "breached" else
                    COLORS["accent_yellow"] if eb["status"] == "critical" else
                    COLORS["accent_green"])
        eb_card = ctk.CTkFrame(self.perf_scroll,
            fg_color=COLORS["bg_secondary"], corner_radius=8)
        eb_card.pack(fill="x", padx=4, pady=2)
        ctk.CTkLabel(eb_card, text=eb_text,
            font=(FONTS["family"], FONTS["size_sm"]),
            text_color=eb_color, justify="left").pack(padx=12, pady=10)

        # ── Tendência (chart) ──
        daily = trends.get("daily_data", [])
        if daily and len(daily) >= 3:
            ctk.CTkLabel(self.perf_scroll, text="📈 Tendência Diária",
                font=(FONTS["family"], FONTS["size_md"], "bold"),
                text_color=COLORS["text_primary"]).pack(anchor="w", padx=8, pady=(12, 4))

            chart_data = []
            for d in daily:
                day_str = d.get("day", "")
                lat = d.get("avg_latency") or 0
                short_day = day_str[-5:] if len(day_str) >= 5 else day_str
                chart_data.append({"x_label": short_day, "value": lat})

            TrendLineChart(self.perf_scroll, data=chart_data,
                            title="Latência média (ms)", y_label="ms",
                            threshold=100).pack(fill="x", padx=4, pady=4)

        # ── Anomalias ──
        if anomalies:
            ctk.CTkLabel(self.perf_scroll,
                text=f"⚠ Anomalias Detectadas ({len(anomalies)})",
                font=(FONTS["family"], FONTS["size_md"], "bold"),
                text_color=COLORS["accent_yellow"]).pack(anchor="w", padx=8, pady=(12, 4))

            for a in anomalies[-10:]:
                sev_color = (COLORS["accent_red"] if a["severity"] == "high"
                             else COLORS["accent_yellow"])
                row = ctk.CTkFrame(self.perf_scroll,
                    fg_color=COLORS["bg_secondary"], corner_radius=6)
                row.pack(fill="x", padx=4, pady=1)
                ctk.CTkLabel(row,
                    text=f"  {a['hour']}  →  Latência: {a['latency']:.1f}ms  ·  "
                         f"Perda: {a['loss']:.1f}%  ·  z={a['z_score']}",
                    font=(FONTS["family_mono"], FONTS["size_xs"]),
                    text_color=sev_color, anchor="w").pack(fill="x", padx=12, pady=4)

        # ── Estabilidade de rota ──
        if route.get("samples", 0) > 0:
            ctk.CTkLabel(self.perf_scroll, text="🛣 Estabilidade de Rota",
                font=(FONTS["family"], FONTS["size_md"], "bold"),
                text_color=COLORS["text_primary"]).pack(anchor="w", padx=8, pady=(12, 4))

            stable = route["stable"]
            r_color = COLORS["accent_green"] if stable else COLORS["accent_yellow"]
            r_text = (f"Hops médios: {route['avg_hop_count']}  ·  "
                       f"Variância: {route['hop_variance']}  ·  "
                       f"Mudanças de rota: {route['route_changes']}  ·  "
                       f"{'ESTÁVEL' if stable else 'INSTÁVEL'}")
            ctk.CTkLabel(self.perf_scroll, text=r_text,
                font=(FONTS["family"], FONTS["size_sm"]),
                text_color=r_color).pack(anchor="w", padx=12, pady=4)

    # ══════════════════════════════════════════════════════════════════
    # TAB 3 — COHORT & CORRELAÇÃO
    # ══════════════════════════════════════════════════════════════════

    def _build_cohort_tab(self):
        tab = self.tab_cohort
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        ctrl = ctk.CTkFrame(tab, fg_color="transparent")
        ctrl.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))

        ctk.CTkButton(ctrl, text="🔬 Analisar Cohorts", width=180,
            font=(FONTS["family"], FONTS["size_sm"], "bold"),
            fg_color=COLORS["accent_blue"],
            hover_color=COLORS["accent_blue_hover"],
            text_color="#FFFFFF", corner_radius=8, height=32,
            command=self._calc_cohorts).pack(side="left", padx=4)

        self.cohort_status = ctk.CTkLabel(ctrl, text="",
            font=(FONTS["family"], FONTS["size_xs"]),
            text_color=COLORS["text_muted"])
        self.cohort_status.pack(side="right", padx=8)

        self.cohort_scroll = ctk.CTkScrollableFrame(
            tab, fg_color=COLORS["bg_primary"],
            corner_radius=8, border_width=1, border_color=COLORS["border"])
        self.cohort_scroll.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        self.cohort_scroll.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self.cohort_scroll,
            text="Análise comparativa entre grupos.\n\n"
                 "• Por Plataforma — qual plataforma de roteador tem melhor SLA?\n"
                 "• Por Grupo — quais lojas/setores têm pior performance?\n"
                 "• Identifica padrões e ajuda decisões de troca de equipamento",
            font=(FONTS["family"], FONTS["size_sm"]),
            text_color=COLORS["text_muted"], justify="center").pack(pady=40)

    def _calc_cohorts(self):
        if not self._sla_data:
            self.cohort_status.configure(text="⚠ Calcule o SLA primeiro (tab 1)")
            return

        from utils.reliability import cohort_by_platform, cohort_by_group

        by_platform = cohort_by_platform(self._sla_data)
        by_group = cohort_by_group(self._sla_data)

        for w in self.cohort_scroll.winfo_children():
            w.destroy()

        # ── Por plataforma ──
        ctk.CTkLabel(self.cohort_scroll, text="🛠 Por Plataforma de Roteador",
            font=(FONTS["family"], FONTS["size_md"], "bold"),
            text_color=COLORS["text_primary"]).pack(anchor="w", padx=8, pady=(8, 4))

        if by_platform:
            # Tabela
            hdr = ctk.CTkFrame(self.cohort_scroll, fg_color=COLORS["bg_tertiary"],
                                corner_radius=6)
            hdr.pack(fill="x", padx=4, pady=2)
            cols = ["Plataforma", "# Hosts", "Uptime Médio", "Total Quedas",
                    "Quedas/Host", "Downtime"]
            weights = [2, 1, 1, 1, 1, 2]
            for i in range(len(cols)):
                hdr.grid_columnconfigure(i, weight=weights[i])
            for i, col in enumerate(cols):
                ctk.CTkLabel(hdr, text=col,
                    font=(FONTS["family"], FONTS["size_xs"], "bold"),
                    text_color=COLORS["text_muted"]).grid(
                    row=0, column=i, padx=8, pady=6, sticky="w")

            for c in by_platform:
                row = ctk.CTkFrame(self.cohort_scroll, fg_color="transparent")
                row.pack(fill="x", padx=4, pady=1)
                for i in range(len(cols)):
                    row.grid_columnconfigure(i, weight=weights[i])

                up = c["avg_uptime_pct"]
                color = (COLORS["accent_green"] if up >= 99.5 else
                         COLORS["accent_yellow"] if up >= 98 else
                         COLORS["accent_red"])
                from utils.device_profiles import get_platform_label
                vals = [
                    get_platform_label(c["platform"]),
                    str(c["host_count"]),
                    f"{up:.2f}%",
                    str(c["total_outages"]),
                    f"{c['outages_per_host']:.1f}",
                    c["total_downtime_str"],
                ]
                colors = [COLORS["text_primary"], COLORS["text_secondary"], color,
                          COLORS["text_primary"], COLORS["text_secondary"],
                          COLORS["text_secondary"]]
                for i, (v, clr) in enumerate(zip(vals, colors)):
                    ctk.CTkLabel(row, text=v,
                        font=(FONTS["family_mono" if i > 0 else "family"],
                              FONTS["size_xs"]),
                        text_color=clr).grid(row=0, column=i, padx=8, pady=4, sticky="w")

            # Bar chart de uptime por plataforma
            bar_data = [{"label": get_platform_label(c["platform"]),
                          "value": c["avg_uptime_pct"]} for c in by_platform]
            BarChart(self.cohort_scroll, data=bar_data,
                     value_format="{:.2f}%", color_scheme="normal",
                     title="Uptime médio por plataforma").pack(
                fill="x", padx=4, pady=8)

        # ── Por grupo ──
        ctk.CTkLabel(self.cohort_scroll, text="📁 Por Grupo",
            font=(FONTS["family"], FONTS["size_md"], "bold"),
            text_color=COLORS["text_primary"]).pack(anchor="w", padx=8, pady=(16, 4))

        if by_group:
            for c in by_group:
                up = c["avg_uptime_pct"]
                color = (COLORS["accent_green"] if up >= 99.5 else
                         COLORS["accent_yellow"] if up >= 98 else
                         COLORS["accent_red"])
                row = ctk.CTkFrame(self.cohort_scroll,
                    fg_color=COLORS["bg_secondary"], corner_radius=6)
                row.pack(fill="x", padx=4, pady=2)
                txt = (f"{c['group']}  ·  {c['host_count']} hosts  ·  "
                       f"Uptime: {up:.2f}%  ·  {c['total_outages']} quedas  ·  "
                       f"{c['total_downtime_str']} de downtime")
                ctk.CTkLabel(row, text=txt,
                    font=(FONTS["family"], FONTS["size_sm"]),
                    text_color=color).pack(padx=12, pady=8, anchor="w")

        self.cohort_status.configure(
            text=f"✓ {len(by_platform)} plataformas · {len(by_group)} grupos")

    # ══════════════════════════════════════════════════════════════════
    # TAB 4 — RCA + QoS atual
    # ══════════════════════════════════════════════════════════════════

    def _build_rca_tab(self):
        tab = self.tab_rca
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        ctrl = ctk.CTkFrame(tab, fg_color="transparent")
        ctrl.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))

        ctk.CTkButton(ctrl, text="🔍 Analisar Todos os Hosts", width=200,
            font=(FONTS["family"], FONTS["size_sm"], "bold"),
            fg_color=COLORS["accent_blue"],
            hover_color=COLORS["accent_blue_hover"],
            text_color="#FFFFFF", corner_radius=8, height=32,
            command=self._calc_rca).pack(side="left", padx=4)

        self.rca_status = ctk.CTkLabel(ctrl, text="",
            font=(FONTS["family"], FONTS["size_xs"]),
            text_color=COLORS["text_muted"])
        self.rca_status.pack(side="right", padx=8)

        self.rca_scroll = ctk.CTkScrollableFrame(
            tab, fg_color=COLORS["bg_primary"],
            corner_radius=8, border_width=1, border_color=COLORS["border"])
        self.rca_scroll.grid(row=1, column=0, sticky="nsew", padx=8, pady=4)
        self.rca_scroll.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self.rca_scroll,
            text="Diagnóstico de Causa Raiz em tempo real.\n\n"
                 "Categorias automáticas:\n"
                 "• 🟢 HEALTHY · DEGRADATION (com QoS)\n"
                 "• 🟡 SSH_ISSUE — Ping OK mas SSH falhou\n"
                 "• 🔴 LINK_DOWN · INTERNET_DOWN · ROUTER_DOWN\n\n"
                 "Cada host inclui recomendação de ação.",
            font=(FONTS["family"], FONTS["size_sm"]),
            text_color=COLORS["text_muted"], justify="center").pack(pady=40)

    def _calc_rca(self):
        if not self.controller:
            return
        from utils.reliability import analyze_root_cause, classify_qos

        for w in self.rca_scroll.winfo_children():
            w.destroy()

        hosts = self.controller.get_all_hosts()
        active = [h for h in hosts if h.enabled]

        criticals, warnings, healthy = [], [], []
        for host in active:
            rca = analyze_root_cause(host)
            rca["host"] = host
            # Adiciona QoS
            if host.status == "online":
                lat = host.host_ssh_latency or 0
                jit = getattr(host, 'host_ssh_jitter', 0) or 0
                loss = host.host_ssh_loss or 0
                rca["qos"] = classify_qos(lat, jit, loss)
            else:
                rca["qos"] = {"category": "down", "color": "#6B7280", "label": "OFFLINE"}

            if rca["severity"] == "critical":
                criticals.append(rca)
            elif rca["severity"] == "warning":
                warnings.append(rca)
            else:
                healthy.append(rca)

        if criticals:
            self._rca_section("🔴 CRÍTICOS", criticals, COLORS["accent_red"])
        if warnings:
            self._rca_section("🟡 ATENÇÃO", warnings, COLORS["accent_yellow"])
        if healthy:
            self._rca_section("🟢 SAUDÁVEIS", healthy, COLORS["accent_green"])

        self.rca_status.configure(
            text=f"✓ {len(active)} hosts — "
                 f"{len(criticals)} críticos · {len(warnings)} atenção · "
                 f"{len(healthy)} saudáveis")

    def _rca_section(self, title: str, items: list, color: str):
        ctk.CTkLabel(self.rca_scroll, text=title,
            font=(FONTS["family"], FONTS["size_md"], "bold"),
            text_color=color).pack(anchor="w", padx=8, pady=(12, 4))

        for rca in items:
            host = rca["host"]
            qos = rca.get("qos", {})
            card = ctk.CTkFrame(self.rca_scroll, fg_color=COLORS["bg_secondary"],
                                 corner_radius=10, border_width=1,
                                 border_color=COLORS["border"])
            card.pack(fill="x", padx=4, pady=3)
            card.grid_columnconfigure(0, weight=1)

            # Top: host + category + QoS
            top = ctk.CTkFrame(card, fg_color="transparent")
            top.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 2))

            ctk.CTkLabel(top, text=f"{host.display_name} ({host.ip})",
                font=(FONTS["family"], FONTS["size_sm"], "bold"),
                text_color=COLORS["text_primary"]).pack(side="left")

            cat_colors = {
                "HEALTHY": COLORS["accent_green"],
                "DEGRADATION": COLORS["accent_yellow"],
                "SSH_ISSUE": COLORS["accent_yellow"],
                "ROUTER_DOWN": COLORS["accent_red"],
                "LINK_DOWN": COLORS["accent_red"],
                "INTERNET_DOWN": COLORS["accent_orange"],
            }
            cat = rca["category"]
            ctk.CTkLabel(top, text=f" {cat} ",
                font=(FONTS["family_mono"], FONTS["size_xs"], "bold"),
                text_color="#FFFFFF",
                fg_color=cat_colors.get(cat, COLORS["text_muted"]),
                corner_radius=4).pack(side="left", padx=8)
            if qos.get("label"):
                ctk.CTkLabel(top, text=f" QoS: {qos['label']} ",
                    font=(FONTS["family_mono"], FONTS["size_xs"], "bold"),
                    text_color="#FFFFFF",
                    fg_color=qos["color"], corner_radius=4).pack(side="left", padx=2)

            ctk.CTkLabel(card, text=rca["summary"],
                font=(FONTS["family"], FONTS["size_xs"]),
                text_color=COLORS["text_secondary"],
                anchor="w").grid(row=1, column=0, sticky="w", padx=12, pady=2)

            if rca.get("details"):
                det_text = "\n".join(f"  • {d}" for d in rca["details"])
                ctk.CTkLabel(card, text=det_text,
                    font=(FONTS["family_mono"], FONTS["size_xs"]),
                    text_color=COLORS["text_muted"],
                    anchor="w", justify="left").grid(
                    row=2, column=0, sticky="w", padx=12, pady=2)

            if rca.get("recommendation"):
                ctk.CTkLabel(card, text=f"💡 {rca['recommendation']}",
                    font=(FONTS["family"], FONTS["size_xs"]),
                    text_color=COLORS["accent_cyan"],
                    anchor="w", justify="left",
                    wraplength=700).grid(row=3, column=0, sticky="w",
                                          padx=12, pady=(2, 4))

            if rca.get("offline_duration"):
                ctk.CTkLabel(card,
                    text=f"⏱ Offline desde {rca['offline_since']} ({rca['offline_duration']})",
                    font=(FONTS["family_mono"], FONTS["size_xs"]),
                    text_color=COLORS["accent_red"]).grid(
                    row=4, column=0, sticky="w", padx=12, pady=(0, 8))

    # ══════════════════════════════════════════════════════════════════
    # REFRESH
    # ══════════════════════════════════════════════════════════════════

    def refresh(self):
        """Atualiza lista de hosts ao mostrar a view."""
        if not self.controller:
            return
        hosts = self.controller.get_all_hosts()
        active = [h for h in hosts if h.enabled]

        self._trend_host_map = {}
        labels = []
        for h in sorted(active, key=lambda x: x.display_name):
            label = f"{h.display_name} ({h.ip})"
            labels.append(label)
            self._trend_host_map[label] = h.id

        if labels:
            self.perf_host_combo.configure(values=labels)
            if self.perf_host_combo.get() == "(selecione)":
                self.perf_host_combo.set(labels[0])
        else:
            self.perf_host_combo.configure(values=["(sem hosts)"])
