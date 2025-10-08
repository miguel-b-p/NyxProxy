# NyxProxy

**NyxProxy** é uma ferramenta de linha de comando (CLI) e biblioteca em Python, projetada para carregar, testar e gerenciar proxies dos tipos V2Ray/Xray (Vmess, Vless, Trojan, Shadowsocks). Ela simplifica o uso desses proxies ao criar "pontes" ou túneis HTTP locais, permitindo que qualquer aplicação utilize-os de forma transparente.

---

## Descrição

O NyxProxy automatiza o processo de validação de grandes listas de servidores proxy. Ele carrega configurações a partir de arquivos locais ou URLs, realiza testes concorrentes de conectividade e latência, e fornece informações detalhadas, incluindo a geolocalização do servidor.

O principal objetivo é encontrar proxies funcionais e disponibilizá-los como servidores HTTP padrão (`http://127.0.0.1:porta`), eliminando a necessidade de configuração complexa em outras aplicações.

### Principais Recursos

-   **Carregamento Flexível:** Adicione proxies a partir de múltiplos arquivos ou URLs.
-   **Parsing Abrangente:** Suporte nativo para os formatos de URI `vmess://`, `vless://`, `trojan://` e `ss://`.
-   **Testes Concorrentes:** Utilize múltiplas threads para testar rapidamente a latência (ping) e a funcionalidade dos servidores.
-   **Geolocalização de IP:** Identifica o país de cada proxy para facilitar a filtragem.
-   **Cache Inteligente:** Armazena os resultados dos testes para acelerar execuções futuras e evitar testes repetidos.
-   **Criação de Pontes HTTP:** Inicia instâncias do Xray-core para cada proxy funcional, expondo-os como um servidor HTTP local.
-   **Integração com `proxychains`:** Execute qualquer comando do terminal através de um conjunto de proxies funcionais de forma aleatória.

### Público-Alvo

Este projeto é destinado a desenvolvedores, analistas de segurança e usuários avançados que precisam de uma maneira programática e eficiente para gerenciar e utilizar múltiplos servidores proxy, seja para web scraping, testes de penetração ou para contornar restrições de rede.

---

## Instalação

### Pré-requisitos

Antes de instalar o NyxProxy, certifique-se de que os seguintes componentes estão instalados e acessíveis no `PATH` do seu sistema:

1.  **Python 3.8+**: Verifique sua versão com `python3 --version`.
2.  **Xray-core**: A ferramenta depende do `xray` para funcionar.
    -   Instale-o através do [instalador oficial](https://github.com/XTLS/Xray-install) ou do seu gerenciador de pacotes (ex: `apt`, `pacman`, `brew`).
3.  **ProxyChains (Opcional)**: Necessário apenas para o comando `chains`.
    -   Instale o `proxychains-ng` (ex: `sudo apt install proxychains-ng`).
4.  **Token da API FindIP**: O NyxProxy usa a API do [findip.net](https://findip.net/) para geolocalização de IPs.
    -   Crie uma conta gratuita para obter um token.

### Passos para Instalação

1.  **Clone o repositório:**
    ```bash
    git clone https://github.com/miguel-b-p/NyxProxy.git
    cd NyxProxy
    ```

2.  **Crie e ative um ambiente virtual (recomendado):**
    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    ```

3.  **Instale as dependências:**
    O projeto usa `setuptools` e todas as dependências estão listadas no `pyproject.toml`. Instale com:
    ```bash
    pip install .
    ```

4.  **Configure as variáveis de ambiente:**
    Copie o arquivo de exemplo `.env.example` para `.env` e adicione seu token da API.
    ```bash
    cp .env.example .env
    ```
    Abra o arquivo `.env` e insira seu token:
    ```ini
    FINDIP_TOKEN="SEU_TOKEN_AQUI"
    ```

---

## Uso

O NyxProxy é operado através da linha de comando. Os principais comandos são `test`, `start`, `chains` e `clear`.

### 1. Testar Proxies (`test`)

Use este comando para validar uma lista de proxies e exibir um relatório de conectividade, país e latência.

**Sintaxe:**
`nyxproxy test [FONTES...] [OPÇÕES]`

**Exemplo:**
```bash
# Testa proxies de um arquivo local e uma URL, usando 20 threads
nyxproxy test ./proxies.txt https://example.com/proxy-list.txt --threads 20 --country BR
````

**Opções:**

  - `SOURCES...`: Um ou mais arquivos locais ou URLs contendo os URIs dos proxies.
  - `--country, -c TEXT`: Filtra os resultados para um país específico (código ISO ou nome em inglês).
  - `--threads, -t INTEGER`: Número de workers para testes paralelos (padrão: 10).
  - `--limit, -l INTEGER`: Número máximo de proxies a serem carregados das fontes.
  - `--force`: Ignora o cache e força um novo teste para todos os proxies.
  - `--find-first, -ff INTEGER`: Para o teste após encontrar o número especificado de proxies funcionais.
  - `--output-json, -j`: Exibe a saída em formato JSON em vez de tabelas.

### 2\. Iniciar Pontes HTTP (`start`)

Este comando testa os proxies e inicia servidores HTTP locais para os mais rápidos e funcionais, mantendo-os ativos até que o processo seja interrompido (com `Ctrl+C`).

**Sintaxe:**
`nyxproxy start [FONTES...] [OPÇÕES]`

**Exemplo:**

```bash
# Inicia 5 pontes HTTP com os melhores proxies do Brasil encontrados nas fontes
nyxproxy start ./proxies.txt --amounts 5 --country BR
```

Se nenhuma fonte for fornecida, o comando tentará usar proxies funcionais salvos no cache.

**Opções:**

  - `--amounts, -a INTEGER`: Número de pontes HTTP a serem criadas (padrão: 5).

### 3\. Executar Comandos com ProxyChains (`chains`)

Este comando facilita o uso dos proxies com qualquer ferramenta de linha de comando, configurando o `proxychains` dinamicamente.

**Sintaxe:**
`nyxproxy chains [OPÇÕES] -- [COMANDO]`

**Exemplo:**

```bash
# Executa o curl para verificar o IP de saída através de 3 proxies diferentes
nyxproxy chains --amounts 3 --source ./proxies.txt -- curl -s https://ipinfo.io/ip

# Baixa um arquivo usando wget através dos proxies
nyxproxy chains --country US -- wget "https://example.com/file.zip"
```

**Importante:** O comando a ser executado e seus argumentos devem vir **após** todas as opções do `nyxproxy`.

### 4\. Limpar o Cache (`clear`)

Gerencia o cache de resultados dos testes.

**Sintaxe:**
`nyxproxy clear [IDADE]`

**Exemplos:**

```bash
# Limpa o cache completamente
nyxproxy clear

# Limpa apenas as entradas do cache com mais de 7 dias
nyxproxy clear '1S' # (1 Semana)

# Limpa apenas as entradas do cache com mais de 1 dia
nyxproxy clear '1D' # (1 Dia)

# Limpa entradas com mais de 12 horas
nyxproxy clear '12H' # (12 Horas)

# Limpa entradas com mais de 8 Dias
nyxproxy clear '1S,1D'

# Limpa entradas com mais de 9 Dias e 1 Hora
nyxproxy clear '1S,2D,1H'
```

-----

## Contribuição

Contribuições são bem-vindas\! Se você deseja melhorar o projeto, siga os passos abaixo:

1.  **Faça um Fork** do repositório.
2.  **Crie uma nova branch** para sua feature ou correção (`git checkout -b feature/minha-feature`).
3.  **Faça suas alterações** e realize commits com mensagens claras.
4.  **Envie suas alterações** para o seu fork (`git push origin feature/minha-feature`).
5.  **Abra um Pull Request** no repositório original.

Para reportar bugs ou sugerir novas funcionalidades, por favor, abra uma *issue*.

-----

## Licença

Este projeto está licenciado sob a **CC BY-NC-SA 4.0**. Consulte o arquivo [LICENSE](LICENSE) para detalhes completos.

-----

## Autores

  - **Miguel Batista Pinotti** - *Desenvolvedor Principal* - [miguel-b-p](https://github.com/miguel-b-p)

  - **Leoni Frazão** - *Desenvolvedor Coadjuvante* - [Gameriano1](https://github.com/Gameriano1)

-----

## Referências

  - **Xray-core:** [https://github.com/XTLS/Xray-core](https://github.com/XTLS/Xray-core)
  - **ProxyChains-NG:** [https://github.com/rofl0r/proxychains-ng](https://github.com/rofl0r/proxychains-ng)
  - **FindIP.net:** [https://findip.net/](https://findip.net/)