"""
Camada de persistência — SQLite com métricas rotativas.

ALTERAÇÕES v2.4:
  1. insert_pings_batch() — um commit para todo o ciclo.
  2. VACUUM removido do cleanup_old_data() — roda só no startup.
  3. Índice idx_ping_ts em timestamp (para cleanup sem filtro por host).
"""
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config import DB_PATH
from utils.logger import setup_logger

logger = setup_logger("database")


class Database:
    """Thread-safe SQLite wrapper com connection pooling simples."""

    _local = threading.local()

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = str(db_path)
        self._init_schema()
        self._startup_vacuum()

    @property
    def conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = self._create_connection()
        return self._local.conn

    def _create_connection(self) -> sqlite3.Connection:
        """
        Cria conexão SQLite com fallback para DELETE mode se WAL falhar.

        CORREÇÃO v2.10 — "file is not a database" após failover:
          Quando um viewer é promovido a servidor, a thread do _monitor_loop
          abre uma nova conexão SQLite. Se o DB está em pasta de rede (SMB)
          e o servidor anterior deixou arquivos WAL (.db-wal, .db-shm),
          o PRAGMA journal_mode=WAL falha com "file is not a database".

          Solução: tenta WAL primeiro. Se falhar, fecha, limpa WAL files
          stale, e reabre com journal_mode=DELETE (funciona sobre SMB).
        """
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            # Testa se funciona de verdade
            c.execute("SELECT 1").fetchone()
            return c
        except (sqlite3.DatabaseError, sqlite3.OperationalError) as e:
            logger.warning(f"SQLite WAL falhou ({e}) — tentando DELETE mode")
            try:
                c.close()
            except Exception:
                pass
            # Limpa WAL files stale do servidor anterior
            for suffix in ("-wal", "-shm"):
                wf = Path(self.db_path + suffix)
                if wf.exists():
                    try:
                        wf.unlink()
                        logger.info(f"WAL file removido: {wf.name}")
                    except Exception:
                        pass
            # Reabre com DELETE mode
            c = sqlite3.connect(self.db_path, timeout=30)
            c.row_factory = sqlite3.Row
            try:
                c.execute("PRAGMA journal_mode=DELETE")
                c.execute("PRAGMA synchronous=NORMAL")
                c.execute("SELECT 1").fetchone()
                logger.info("SQLite conectado com journal_mode=DELETE (fallback SMB)")
                return c
            except Exception as e2:
                logger.error(f"SQLite DELETE mode também falhou: {e2}")
                return c  # retorna mesmo assim — melhor logar erros do que crashar

    def _safe_execute(self, operation, *args, retries=3, **kwargs):
        """
        CORREÇÃO v2.8 — wrapper com retry para operações SQLite.

        O SQLite pode retornar "database is locked" ou "disk I/O error"
        transitoriamente quando:
          - Antivírus está escaneando o arquivo WAL
          - Outro processo fez checkpoint no WAL ao mesmo tempo
          - O SO fez flush de cache de disco (comum em SSDs com write-back)
          - O arquivo está em pasta de rede com latência variável

        Sem retry, essas exceções subiam até o _monitor_loop e, se não
        capturadas, matavam a thread de monitoramento permanentemente.
        """
        for attempt in range(retries):
            try:
                return operation(*args, **kwargs)
            except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
                if attempt < retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    # Tenta reconectar se a conexão ficou corrupta
                    try:
                        if hasattr(self._local, "conn") and self._local.conn:
                            self._local.conn.close()
                    except Exception:
                        pass
                    self._local.conn = None
                else:
                    raise

    def close(self):
        if hasattr(self._local, "conn") and self._local.conn:
            try:
                self._local.conn.close()
            except Exception:
                pass
            self._local.conn = None

    def reconnect(self):
        """
        Fecha a conexão atual e força reconexão no próximo acesso.
        Chamado durante failover viewer→servidor para limpar estado stale.
        """
        self.close()
        logger.info("Database: conexão resetada — reconexão automática no próximo acesso")

    def _startup_vacuum(self):
        """Roda VACUUM uma vez no startup. Pula se DB estiver em uso."""
        try:
            self.conn.execute("VACUUM")
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            pass  # DB locked ou em uso — ok, pula

    def _init_schema(self):
        """
        Cria tabelas e índices se não existirem.
        Se o DB estiver locked (outro processo escrevendo),
        tenta com retry. Se falhar, assume que as tabelas já existem
        (criadas pelo servidor) e continua em modo leitura.
        """
        for attempt in range(3):
            try:
                self._do_init_schema()
                return  # sucesso
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    if attempt < 2:
                        import time
                        time.sleep(2)  # espera e tenta de novo
                        continue
                    # 3 tentativas falharam — DB está sendo usado pelo servidor
                    # As tabelas já existem, podemos continuar só lendo
                    import logging
                    logging.getLogger("database").warning(
                        "DB locked durante init — assumindo tabelas existentes (modo leitura)"
                    )
                    return
                else:
                    raise  # outro erro — propaga

    def _do_init_schema(self):
        c = self.conn
        c.executescript("""
            CREATE TABLE IF NOT EXISTS hosts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ip          TEXT UNIQUE NOT NULL,
                label       TEXT DEFAULT '',
                group_name  TEXT DEFAULT 'Geral',
                ssh_user    TEXT DEFAULT '',
                ssh_password TEXT DEFAULT '',
                ssh_port    INTEGER DEFAULT 22,
                wan_ip      TEXT DEFAULT '',
                wan_ip_2    TEXT DEFAULT '',
                wan_ip_3    TEXT DEFAULT '',
                enabled     INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT (datetime('now','localtime')),
                updated_at  TEXT DEFAULT (datetime('now','localtime'))
            );

            CREATE TABLE IF NOT EXISTS ping_metrics (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                host_id     INTEGER NOT NULL,
                timestamp   TEXT NOT NULL,
                latency_ms  REAL,
                jitter_ms   REAL,
                loss_pct    REAL,
                rtt_min     REAL,
                rtt_max     REAL,
                rtt_avg     REAL,
                ttl         INTEGER,
                status      TEXT DEFAULT 'unknown',
                FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS traceroute_results (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                host_id     INTEGER NOT NULL,
                timestamp   TEXT NOT NULL,
                hop_count   INTEGER,
                hops_json   TEXT,
                FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS dns_metrics (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                host_id     INTEGER NOT NULL,
                timestamp   TEXT NOT NULL,
                resolve_ms  REAL,
                resolved_ip TEXT,
                FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS alerts_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                host_id     INTEGER NOT NULL,
                timestamp   TEXT NOT NULL,
                alert_type  TEXT,
                message     TEXT,
                resolved    INTEGER DEFAULT 0,
                resolved_at TEXT,
                FOREIGN KEY (host_id) REFERENCES hosts(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_ping_host_ts
                ON ping_metrics(host_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_alerts_host
                ON alerts_log(host_id, resolved);
        """)
        c.commit()

        # v2.4: índice em timestamp puro (para cleanup sem filtro por host)
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_ping_ts ON ping_metrics(timestamp)")
            c.commit()
        except Exception:
            pass

        # Migração: adiciona coluna ssh_password se não existir
        try:
            c.execute("SELECT ssh_password FROM hosts LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE hosts ADD COLUMN ssh_password TEXT DEFAULT ''")
            c.commit()

        # Migração: adiciona coluna wan_ip se não existir
        try:
            c.execute("SELECT wan_ip FROM hosts LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE hosts ADD COLUMN wan_ip TEXT DEFAULT ''")
            c.commit()

        # Migração: adiciona coluna wan_ip_2 se não existir (v2.7)
        try:
            c.execute("SELECT wan_ip_2 FROM hosts LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE hosts ADD COLUMN wan_ip_2 TEXT DEFAULT ''")
            c.commit()

        # Migração: adiciona coluna wan_ip_3 se não existir (v2.7)
        try:
            c.execute("SELECT wan_ip_3 FROM hosts LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE hosts ADD COLUMN wan_ip_3 TEXT DEFAULT ''")
            c.commit()

        # Migração: adiciona coluna platform se não existir (v2.11)
        try:
            c.execute("SELECT platform FROM hosts LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE hosts ADD COLUMN platform TEXT DEFAULT ''")
            c.commit()

        # Migração: adiciona coluna cisco_enable_password se não existir (v2.12)
        try:
            c.execute("SELECT cisco_enable_password FROM hosts LIMIT 1")
        except sqlite3.OperationalError:
            c.execute("ALTER TABLE hosts ADD COLUMN cisco_enable_password TEXT DEFAULT ''")
            c.commit()

    # ── CRUD de hosts ─────────────────────────────────────────────────
    def add_host(self, ip: str, label: str = "", group: str = "Geral",
                 ssh_user: str = "", ssh_password: str = "",
                 ssh_port: int = 22, wan_ip: str = "", wan_ip_2: str = "",
                 wan_ip_3: str = "", platform: str = "",
                 cisco_enable_password: str = "") -> int:
        # CORREÇÃO v2.12 — criptografa senhas antes de gravar
        from utils.security import encrypt_password
        ssh_pwd_enc = encrypt_password(ssh_password) if ssh_password else ""
        ena_pwd_enc = encrypt_password(cisco_enable_password) if cisco_enable_password else ""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO hosts
               (ip, label, group_name, ssh_user, ssh_password, ssh_port,
                wan_ip, wan_ip_2, wan_ip_3, platform, cisco_enable_password)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ip, label, group, ssh_user, ssh_pwd_enc, ssh_port,
             wan_ip, wan_ip_2, wan_ip_3, platform, ena_pwd_enc)
        )
        self.conn.commit()
        if cur.lastrowid:
            return cur.lastrowid
        row = self.conn.execute("SELECT id FROM hosts WHERE ip=?", (ip,)).fetchone()
        return row["id"] if row else -1

    def update_host(self, host_id: int, **kwargs):
        allowed = {"ip", "label", "group_name", "ssh_user", "ssh_password",
                    "ssh_port", "wan_ip", "wan_ip_2", "wan_ip_3", "platform",
                    "cisco_enable_password", "enabled"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        # CORREÇÃO v2.12 — criptografa senhas antes de gravar
        from utils.security import encrypt_password
        if "ssh_password" in updates and updates["ssh_password"]:
            updates["ssh_password"] = encrypt_password(updates["ssh_password"])
        if "cisco_enable_password" in updates and updates["cisco_enable_password"]:
            updates["cisco_enable_password"] = encrypt_password(updates["cisco_enable_password"])
        if not updates:
            return
        updates["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cols = ", ".join(f"{k}=?" for k in updates)
        vals = list(updates.values()) + [host_id]
        self.conn.execute(f"UPDATE hosts SET {cols} WHERE id=?", vals)
        self.conn.commit()

    def remove_host(self, host_id: int):
        self.conn.execute("DELETE FROM hosts WHERE id=?", (host_id,))
        self.conn.commit()

    def get_hosts(self, enabled_only: bool = True) -> list[dict]:
        q = "SELECT * FROM hosts"
        if enabled_only:
            q += " WHERE enabled=1"
        q += " ORDER BY group_name, label, ip"
        rows = self.conn.execute(q).fetchall()
        # CORREÇÃO v2.12 — descriptografa senha SSH transparentemente
        from utils.security import decrypt_password
        result = []
        for r in rows:
            d = dict(r)
            if d.get("ssh_password"):
                d["ssh_password"] = decrypt_password(d["ssh_password"])
            if d.get("cisco_enable_password"):
                d["cisco_enable_password"] = decrypt_password(d["cisco_enable_password"])
            result.append(d)
        return result

    def get_host_by_ip(self, ip: str) -> Optional[dict]:
        row = self.conn.execute("SELECT * FROM hosts WHERE ip=?", (ip,)).fetchone()
        if not row:
            return None
        from utils.security import decrypt_password
        d = dict(row)
        if d.get("ssh_password"):
            d["ssh_password"] = decrypt_password(d["ssh_password"])
        if d.get("cisco_enable_password"):
            d["cisco_enable_password"] = decrypt_password(d["cisco_enable_password"])
        return d

    # ── Renomear grupo ────────────────────────────────────────────────
    def rename_group(self, old_name: str, new_name: str) -> int:
        cur = self.conn.execute(
            "UPDATE hosts SET group_name=?, updated_at=? WHERE group_name=?",
            (new_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), old_name)
        )
        self.conn.commit()
        return cur.rowcount

    def get_group_names(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT group_name FROM hosts ORDER BY group_name"
        ).fetchall()
        return [r["group_name"] for r in rows]

    # ── Métricas de ping ──────────────────────────────────────────────
    def insert_ping(self, host_id: int, latency: float, jitter: float,
                    loss: float, rtt_min: float, rtt_max: float,
                    rtt_avg: float, ttl: int, status: str):
        """Insert individual (mantido para compatibilidade)."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            """INSERT INTO ping_metrics
               (host_id, timestamp, latency_ms, jitter_ms, loss_pct,
                rtt_min, rtt_max, rtt_avg, ttl, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (host_id, ts, latency, jitter, loss, rtt_min, rtt_max, rtt_avg, ttl, status)
        )
        self.conn.commit()

    def insert_pings_batch(self, batch: list[tuple]):
        """
        v2.4: Batch insert — um commit para todo o ciclo.
        v2.8: retry em caso de database locked.
        batch = [(host_id, latency, jitter, loss, rtt_min, rtt_max, rtt_avg, ttl, status), ...]
        """
        if not batch:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = [(hid, ts, lat, jit, loss, rmin, rmax, ravg, ttl, st)
                for hid, lat, jit, loss, rmin, rmax, ravg, ttl, st in batch]

        def _do_insert():
            self.conn.executemany(
                """INSERT INTO ping_metrics
                   (host_id, timestamp, latency_ms, jitter_ms, loss_pct,
                    rtt_min, rtt_max, rtt_avg, ttl, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows
            )
            self.conn.commit()

        self._safe_execute(_do_insert)

    def get_ping_history(self, host_id: int, limit: int = 200) -> list[dict]:
        rows = self.conn.execute(
            """SELECT * FROM ping_metrics WHERE host_id=?
               ORDER BY timestamp DESC LIMIT ?""",
            (host_id, limit)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def get_latest_ping(self, host_id: int) -> Optional[dict]:
        row = self.conn.execute(
            """SELECT * FROM ping_metrics WHERE host_id=?
               ORDER BY timestamp DESC LIMIT 1""",
            (host_id,)
        ).fetchone()
        return dict(row) if row else None

    # ── Traceroute ────────────────────────────────────────────────────
    def insert_traceroute(self, host_id: int, hop_count: int, hops_json: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            """INSERT INTO traceroute_results (host_id, timestamp, hop_count, hops_json)
               VALUES (?, ?, ?, ?)""",
            (host_id, ts, hop_count, hops_json)
        )
        self.conn.commit()

    def get_latest_traceroute(self, host_id: int) -> Optional[dict]:
        row = self.conn.execute(
            """SELECT * FROM traceroute_results WHERE host_id=?
               ORDER BY timestamp DESC LIMIT 1""",
            (host_id,)
        ).fetchone()
        return dict(row) if row else None

    # ── DNS ───────────────────────────────────────────────────────────
    def insert_dns(self, host_id: int, resolve_ms: float, resolved_ip: str):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.conn.execute(
            """INSERT INTO dns_metrics (host_id, timestamp, resolve_ms, resolved_ip)
               VALUES (?, ?, ?, ?)""",
            (host_id, ts, resolve_ms, resolved_ip)
        )
        self.conn.commit()

    # ── Alertas ───────────────────────────────────────────────────────
    def insert_alert(self, host_id: int, alert_type: str, message: str) -> int:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def _do():
            cur = self.conn.execute(
                """INSERT INTO alerts_log (host_id, timestamp, alert_type, message)
                   VALUES (?, ?, ?, ?)""",
                (host_id, ts, alert_type, message)
            )
            self.conn.commit()
            return cur.lastrowid

        return self._safe_execute(_do)

    def resolve_alert(self, alert_id: int):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def _do():
            self.conn.execute(
                "UPDATE alerts_log SET resolved=1, resolved_at=? WHERE id=?",
                (ts, alert_id)
            )
            self.conn.commit()

        self._safe_execute(_do)

    def get_active_alerts(self, host_id: Optional[int] = None) -> list[dict]:
        q = "SELECT a.*, h.ip, h.label FROM alerts_log a JOIN hosts h ON a.host_id=h.id WHERE a.resolved=0"
        params = []
        if host_id:
            q += " AND a.host_id=?"
            params.append(host_id)
        q += " ORDER BY a.timestamp DESC"
        rows = self.conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]

    def get_alerts_history(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            """SELECT a.*, h.ip, h.label FROM alerts_log a
               JOIN hosts h ON a.host_id=h.id
               ORDER BY a.timestamp DESC LIMIT ?""",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Limpeza rotativa ──────────────────────────────────────────────
    def cleanup_old_data(self, days: int = 30):
        """
        v2.4: sem VACUUM automático — VACUUM roda apenas no startup.
        DELETE + COMMIT é muito mais rápido e não causa lock longo.
        v2.8: retry para database locked.
        """
        def _do_cleanup():
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            for table in ["ping_metrics", "traceroute_results", "dns_metrics"]:
                self.conn.execute(f"DELETE FROM {table} WHERE timestamp < ?", (cutoff,))
            self.conn.execute(
                "DELETE FROM alerts_log WHERE resolved=1 AND resolved_at < ?", (cutoff,)
            )
            self.conn.commit()

        self._safe_execute(_do_cleanup)

    # ── Estatísticas rápidas ──────────────────────────────────────────
    def get_host_stats_24h(self, host_id: int) -> dict:
        cutoff = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        row = self.conn.execute(
            """SELECT
                COUNT(*) as total_pings,
                AVG(latency_ms) as avg_latency,
                MAX(latency_ms) as max_latency,
                MIN(latency_ms) as min_latency,
                AVG(jitter_ms) as avg_jitter,
                AVG(loss_pct) as avg_loss,
                SUM(CASE WHEN status='online' THEN 1 ELSE 0 END) as online_count,
                SUM(CASE WHEN status='offline' THEN 1 ELSE 0 END) as offline_count
               FROM ping_metrics
               WHERE host_id=? AND timestamp>=?""",
            (host_id, cutoff)
        ).fetchone()
        r = dict(row) if row else {}
        total = r.get("total_pings", 0)
        if total > 0:
            r["availability_pct"] = round((r.get("online_count", 0) / total) * 100, 2)
        else:
            r["availability_pct"] = 0
        return r