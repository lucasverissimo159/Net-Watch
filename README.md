# 📡 NetWatch Pro

Desktop application (Python) for **real-time network monitoring**: ping,
latency, jitter, packet loss, DNS, traceroute/MTR and per-host alerts —
with dashboard, history and reports. Supports SSH collection on equipment
(pfSense, Linux, Cisco).

![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)
![CustomTkinter](https://img.shields.io/badge/CustomTkinter-5.2+-green.svg)
![SQLite](https://img.shields.io/badge/SQLite-3-orange.svg)
![License](https://img.shields.io/badge/License-View--Only-red.svg)

> ⚠️ **Repository available only for portfolio.** The code can
> be viewed, but **cannot** be copied, downloaded, used or
> repurposed in other projects. See the [License](#-license) section and the
> [`LICENSE`](./LICENSE) file.

## ✨ Features

- 📊 Dashboard with status, latency, jitter and loss per host
- 📈 History and reliability metrics (SLA/uptime)
- 🌐 Traceroute / MTR (local or via SSH)
- 🔔 Offline host alerts with interval escalation
- 🔊 Sound alerts (TTS) per host
- 🧾 Exportable reports

## 🚀 Installation

```bash
pip install -r requirements.txt
python main.py
```

## 🔐 Credentials and configuration (important)

This repository **does not** contain real credentials or IPs:

- SSH credentials are in `data/config.json` (not versioned) and are
  typed through the app's **Settings** screen. By default, the SSH username/password
  come **blank** (`""`) — fill them in the app before using SSH collection.
- The `data/netwatch.db` database is created automatically and also **is not**
  versioned (it will contain the real hosts and credentials at runtime).
- To start with a demonstrable database (only public DNS servers),
  copy the example:

  ```bash
  cp data/netwatch.example.db data/netwatch.db
  ```

- The example addresses in `config.py` (`DEFAULT_STORE_MAP`,
  `DEFAULT_EXCLUDED_IPS`, IP format) use the ranges reserved for
  documentation (**RFC 5737**). Adjust to your network via the app / `config.json`.

## 🗂️ Structure

```
netwatch_v212/
├── main.py                 # Entry point
├── config.py               # Central configuration (no secrets)
├── controllers/            # monitor, ssh, audio
├── models/                 # database, host_model
├── views/                  # dashboard, hosts, logs, settings, ssh...
├── utils/                  # network, security, report_generator...
└── data/
    └── netwatch.example.db # Example database (only public DNS, no credentials)
```

## 🔒 Security

- `data/netwatch.db`, `data/config.json`, logs and audios are in `.gitignore`.
- SSH passwords are encrypted before writing to the database/config
  (`utils/security.py`).

## 📄 License

This repository **is not open source**. It is made available publicly
only for portfolio/technical demonstration purposes.

- ✅ Allowed: view the code through the GitHub interface.
- ❌ Forbidden: copy, download, clone for reuse, use, modify, execute
  or redistribute this code, in whole or in part, without prior written
  authorization from the author.

All rights reserved. See the full terms in
[`LICENSE`](./LICENSE).

---

# 📡 NetWatch Pro (Português)

Aplicação desktop (Python) para **monitoramento de rede em tempo real**: ping,
latência, jitter, perda de pacotes, DNS, traceroute/MTR e alertas por host —
com dashboard, histórico e relatórios. Suporta coleta via SSH em equipamentos
(pfSense, Linux, Cisco).

![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)
![CustomTkinter](https://img.shields.io/badge/CustomTkinter-5.2+-green.svg)
![SQLite](https://img.shields.io/badge/SQLite-3-orange.svg)
![License](https://img.shields.io/badge/License-View--Only-red.svg)

> ⚠️ **Repositório disponibilizado apenas para portfólio.** O código pode
> ser visualizado, mas **não** pode ser copiado, baixado, usado ou
> reaproveitado em outros projetos. Veja a seção [Licença](#-licença) e o
> arquivo [`LICENSE`](./LICENSE).

## ✨ Funcionalidades

- 📊 Dashboard com status, latência, jitter e perda por host
- 📈 Histórico e métricas de confiabilidade (SLA/uptime)
- 🌐 Traceroute / MTR (local ou via SSH)
- 🔔 Alertas de host offline com escalonamento de intervalos
- 🔊 Alertas sonoros (TTS) por host
- 🧾 Relatórios exportáveis

## 🚀 Instalação

```bash
pip install -r requirements.txt
python main.py
```

## 🔐 Credenciais e configuração (importante)

Este repositório **não** contém credenciais nem IPs reais:

- As credenciais SSH ficam em `data/config.json` (não versionado) e são
  digitadas pela tela de **Configurações** do app. Por padrão, o usuário/senha
  SSH vêm **em branco** (`""`) — preencha-os no app antes de usar coleta SSH.
- O banco `data/netwatch.db` é criado automaticamente e também **não** é
  versionado (ele passa a conter os hosts e credenciais reais em runtime).
- Para começar com uma base demonstrável (apenas servidores DNS públicos),
  copie o exemplo:

  ```bash
  cp data/netwatch.example.db data/netwatch.db
  ```

- Os endereços de exemplo em `config.py` (`DEFAULT_STORE_MAP`,
  `DEFAULT_EXCLUDED_IPS`, formato de IP) usam as faixas reservadas para
  documentação (**RFC 5737**). Ajuste para a sua rede pelo app / `config.json`.

## 🗂️ Estrutura

```
netwatch_v212/
├── main.py                 # Ponto de entrada
├── config.py               # Configuração central (sem segredos)
├── controllers/            # monitor, ssh, audio
├── models/                 # database, host_model
├── views/                  # dashboard, hosts, logs, settings, ssh...
├── utils/                  # network, security, report_generator...
└── data/
    └── netwatch.example.db # Base de exemplo (só DNS públicos, sem credenciais)
```

## 🔒 Segurança

- `data/netwatch.db`, `data/config.json`, logs e áudios estão no `.gitignore`.
- Senhas SSH são criptografadas antes de gravar no banco/config
  (`utils/security.py`).

## 📄 Licença

Este repositório **não é open source**. Ele é disponibilizado publicamente
apenas para fins de portfólio/demonstração técnica.

- ✅ Permitido: visualizar o código pela interface do GitHub.
- ❌ Proibido: copiar, baixar, clonar para reuso, usar, modificar, executar
  ou redistribuir este código, no todo ou em parte, sem autorização prévia
  e por escrito do autor.

Todos os direitos são reservados. Veja os termos completos em
[`LICENSE`](./LICENSE).