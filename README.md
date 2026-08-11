# 📡 NetWatch Pro

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
