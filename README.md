# NyxProxy

![Ilustração inspirada em Nyx](./nyx.png)

**NyxProxy** é uma CLI e biblioteca Python para carregar, testar e orquestrar proxies V2Ray/Xray
(`vmess://`, `vless://`, `trojan://` e `ss://`). Ela automatiza a validação de grandes listas,
cria pontes HTTP locais usando Xray-core e integra com `proxychains` para que qualquer aplicação
possa trafegar por proxies funcionais sem ajuste manual.

---

## Visão Geral

- **Pipelines concorrentes:** testes assíncronos de conectividade, latência e geolocalização.
- **Cache inteligente:** reaproveita resultados anteriores e permite exportar apenas proxies ativos.
- **Pontes locais:** inicia instâncias Xray e disponibiliza proxies como `http://127.0.0.1:<porta>`.
- **Integração com proxychains:** executa comandos via múltiplos proxies em sequência aleatória.
- **CLI Typer + Rich:** interface interativa com tabelas, filtros por país e saída em JSON.

O projeto é voltado a pessoas desenvolvedoras, pesquisadores de segurança e usuários avançados que
precisam avaliar e aplicar proxies de maneira reprodutível em fluxos de scraping, testes ou bypass.

---

## Pré-requisitos

1. **Python 3.8+** – verifique com `python3 --version`.
2. **Xray-core** – disponibilize o binário `xray` no `PATH`. Consulte
   [XTLS/Xray-install](https://github.com/XTLS/Xray-install) ou o gerenciador de pacotes da sua
   distro (`apt`, `pacman`, `brew`...).
3. **Proxychains-ng (opcional)** – necessário apenas para o comando `chains`.
4. **Token FindIP** – crie uma conta gratuita em [findip.net](https://findip.net/) e informe o
   token em `.env` para habilitar geolocalização.

---

## Instalação Rápida

```bash
git clone https://github.com/miguel-b-p/NyxProxy.git
cd NyxProxy
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env  # preencha FINDIP_TOKEN antes de rodar testes
```

O arquivo `proxy.txt` inclui amostras de URIs para validação inicial. Não armazene listas reais ou
tokens na árvore do repositório.

---

## Uso da CLI

Todos os comandos aceitam múltiplos arquivos e URLs como fonte. Quando `--no-geo` é informado, a
consulta à API do FindIP é omitida, acelerando execuções sem filtros por país.

### Testar proxies

```bash
nyxproxy test proxy.txt --threads 20 --find-first 10
nyxproxy test lista.txt https://example.com/proxies.txt --country BR --output-json
```

Opções principais:

- `--threads, -t`: número de workers (padrão 20).
- `--country, -c`: filtra por código ISO ou nome em inglês (`BR`, `United States`...).
- `--limit, -l`: limita quantos URIs são carregados.
- `--force`: ignora o cache existente.
- `--find-first, -ff`: interrompe após encontrar N proxies funcionais.
- `--no-geo`: pula geolocalização.
- `--output-json, -j`: retorna o relatório em JSON.

### Iniciar pontes HTTP

```bash
nyxproxy start proxy.txt --amounts 3 --country BR
nyxproxy start --amounts 2 --no-geo  # reaproveita o cache existente
```

Inicia até `--amounts` pontes locais persistentes (`http://127.0.0.1:<porta>`). O comando mantém as
instâncias ativas até receber `Ctrl+C`. Use `--threads` e `--limit` para controlar o pré-teste.

### Executar via proxychains

```bash
nyxproxy chains --amounts 3 --source proxy.txt -- curl -s https://ifconfig.me
nyxproxy chains -s proxy.txt -s outra_lista.txt --country US -- wget "https://example.com/file"
```

O comando após `--` é repassado ao `proxychains`. A ferramenta cuida da geração temporária de
configuração e encerra as pontes ao final.

### Limpar cache

```bash
nyxproxy clear          # remove todo o cache
nyxproxy clear 1S,2D    # remove entradas com mais de 1 semana e 2 dias
nyxproxy clear 12H      # remove entradas com mais de 12 horas
```

Aceita abreviações: `S` (semana), `D` (dia), `H` (hora), `M` (minuto). Combine valores separados
por vírgula.

### Listar proxies aprovados

```bash
nyxproxy list-proxies
nyxproxy list-proxies --country NL --output-json
```

Carrega o cache e exibe somente proxies com status `OK`. Use `--output-json` para integrar com
outras ferramentas.

### Exportar proxies funcionais

```bash
nyxproxy export proxies_ok.txt proxy.txt --threads 30 --find-first 50
nyxproxy export ativos.txt --country JP --no-geo  # apenas cache
```

Executa um teste (ou reaproveita o cache) e grava os URIs aprovados no arquivo informado. Informe
`--force` se quiser revalidar tudo ignorando o cache.

---

## Variáveis de Ambiente

O projeto utiliza `python-dotenv`. Preencha `.env` com:

```ini
FINDIP_TOKEN="seu_token_findip"
```

Mantenha o arquivo fora do controle de versão. Ao adicionar novas chaves, atualize `.env.example`.

---

## Contribuição

1. Faça fork do repositório e crie uma branch (`git checkout -b fix/minha-correção`).
2. Siga o padrão Conventional Commits (ex.: `fix(parsing): corrige leitura do campo port`).
3. Atualize docs e exemplos relevantes; inclua resultados de CLI quando alterar comportamentos.
4. Abra um Pull Request descrevendo motivação, testes executados e impactos de segurança.

Para reportar bugs ou sugerir funcionalidades, abra uma issue com logs e passos de reprodução.

---

## Licença

Distribuído sob **CC BY-NC-SA 4.0**. Consulte o arquivo [LICENSE](LICENSE) para detalhes.

---

## Autores

- **Miguel Batista Pinotti** – [miguel-b-p](https://github.com/miguel-b-p)
- **Leoni Frazão** – [Gameriano1](https://github.com/Gameriano1)
