# NetWatch Pro v2.4 — Changelog

## 🔴 Correções Críticas

### 1. Credenciais SSH removidas do código-fonte (Segurança)
- `config.py`: `SSH_DEFAULTS` não contém mais `default_user`/`default_password`.
- Credenciais agora vivem exclusivamente no `config.json` do usuário (`%USERPROFILE%\.netwatch_pro\data\config.json`).
- Na primeira execução, campos ficam vazios — o operador configura via **Configurações > Credenciais SSH Padrão > Salvar**.
- `main.py` exibe warning no log se credenciais não estão configuradas.

### 2. VACUUM otimizado (Performance/Estabilidade)
- `database.py`: `VACUUM` removido do `cleanup_old_data()` — agora roda **apenas uma vez no startup** (`_startup_vacuum()`).
- Evita locks longos a cada ~17 minutos com DBs grandes.

### 3. Batch inserts no DB (Performance)
- `database.py`: novo método `insert_pings_batch()` — usa `executemany` + um único `commit` por ciclo.
- `monitor_controller.py`: acumula todos os pings do ciclo e insere em lote.
- **Resultado**: ~100 commits/ciclo → 1 commit/ciclo = 100x menos `fsync`.

### 4. ThreadPoolExecutor reutilizável (Performance)
- `monitor_controller.py`: pool criado UMA vez no `start()`, reusado em todos os ciclos, encerrado no `stop()`.
- Evita criar/destruir 10 threads a cada 10 segundos (~86.000 thread starts/dia eliminados).

---

## 🟡 Melhorias Importantes

### 5. Toggle de áudio (Mute)
- `audio_controller.py`: propriedade `muted`, método `toggle_mute()`, persistência no `config.json`.
- `main_view.py`: botão 🔊/🔇 na sidebar — controla silenciamento global.
- `monitor_controller.py`: respeita `audio.muted` antes de tocar alertas.
- **Caso de uso**: servidor toca alertas, PCs dos colaboradores ficam mudos.

### 6. Auto-geração de áudios no startup
- `main.py`: chama `_auto_generate_missing_audios()` que percorre todos os hosts e gera TTS para quem ainda não tem áudio.
- `audio_controller.py`: método `generate_all_missing_alerts(hosts)` para uso no startup.
- **Caso de uso**: `.exe` copiado para máquina nova → áudios gerados automaticamente.

### 7. Ícone de janela/taskbar
- `config.py`: `resource_path()` resolve caminhos tanto em dev quanto no `.exe` (PyInstaller `_MEIPASS`).
- `main.py`: `_load_icon()` carrega `resources/icons/netwatch.ico` (Windows) ou PNG fallback.
- Coloque `netwatch.ico` em `resources/icons/` antes de compilar.

### 8. Versão unificada para 2.4.0
- `config.py`: `APP_VERSION = "2.4.0"` — antes estava "2.1.0" enquanto código era v2.3.

### 9. Redução de logs
- `monitor_controller.py`: log de ciclo só a cada 10 ciclos OU quando houve mudança de status.
- **Resultado**: ~8.640 linhas/dia de "Ciclo #N" → ~864 linhas/dia (90% redução).

### 10. Rate-limit em traceroutes
- `monitor_controller.py`: máximo `max_concurrent_traceroutes` (5) por rodada.
- Evita saturação de SSH quando muitos hosts estão offline simultaneamente.

---

## 🟢 Infraestrutura

### 11. Índice extra no DB
- `database.py`: `CREATE INDEX idx_ping_ts ON ping_metrics(timestamp)` — otimiza cleanup sem filtro por host.

### 12. PyInstaller spec file
- `netwatch.spec`: arquivo de build pronto para `pyinstaller netwatch.spec`.
- Inclui customtkinter, paramiko, pygame, edge-tts como hidden imports.
- Exclui matplotlib/numpy/scipy/pandas para reduzir tamanho do .exe.

---

## Arquivos Modificados

| Arquivo | Mudanças |
|---------|----------|
| `config.py` | resource_path(), sem senha hardcoded, is/set_audio_muted(), v2.4.0 |
| `main.py` | ícone, auto-gen áudios, aviso SSH vazio |
| `controllers/audio_controller.py` | muted, toggle_mute(), generate_all_missing(), play_lock |
| `controllers/monitor_controller.py` | pool reusável, batch insert, muted, rate-limit, log reduzido |
| `models/database.py` | insert_pings_batch(), VACUUM startup only, idx_ping_ts |
| `views/main_view.py` | botão mute sidebar, periodic_update 15s |
| `netwatch.spec` | PyInstaller spec file (NOVO) |
| `CHANGELOG_v2.4.md` | Este arquivo (NOVO) |

## Arquivos NÃO modificados (copiados intactos)

- `models/host_model.py`
- `utils/network.py`
- `utils/logger.py`
- `controllers/ssh_controller.py`
- `views/dashboard_view.py`
- `views/host_detail_view.py`
- `views/logs_view.py`
- `views/settings_view.py`
- `views/ssh_view.py`
- `views/widgets.py`

## Como compilar

```bash
cd netwatch_pro
pip install pyinstaller customtkinter paramiko pygame edge-tts
pyinstaller netwatch.spec
```

O `.exe` estará em `dist/NetWatch Pro.exe`.
