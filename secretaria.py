"""secretaria-academica: prazos do Blackboard no Notion e no Telegram.

Fluxo (Cap. 1 e Cap. 8): cron -> coletar (ICS) -> filtrar -> montar_resumo
-> criar pagina no Notion -> enviar resumo + link no Telegram.

Uso: python3 secretaria.py --simular   (nao envia nada)
     python3 secretaria.py              (execucao real)

Ordem do arquivo: configuracao, leitura, coleta, resumo, Notion, Telegram, main.
Nenhuma credencial fica aqui: tudo vem do .env (Cap. 10).
"""

import argparse   # le o argumento --simular
import json       # grava o prazos.json (saida concreta)
import os         # le as variaveis de ambiente (.env)
import re         # expressoes regulares: desdobrar ICS, achar blocos, limpar id
import sys        # stderr para mensagens de erro e sys.exit para o codigo de saida
from datetime import datetime, timedelta, timezone   # datas e a janela de 30 dias

import requests   # unica dependencia externa (Cap. 8: HTTP com timeout)

try:
    from dotenv import load_dotenv   # le o .env, se a biblioteca estiver instalada
except ImportError:                  # roda mesmo sem python-dotenv instalado
    def load_dotenv(caminho=".env"):
        """Leitor simples de .env com a biblioteca padrao (CHAVE=valor)."""
        if not os.path.exists(caminho):   # sem .env: nao ha nada a carregar
            return None
        with open(caminho, encoding="utf-8") as arquivo:   # .env e sempre UTF-8
            for linha in arquivo:
                linha = linha.strip()   # remove espacos e quebra de linha
                if not linha or linha.startswith("#") or "=" not in linha:
                    continue   # ignora linha vazia, comentario e linha sem "="
                chave, valor = linha.split("=", 1)   # split(1): o valor pode ter "="
                os.environ.setdefault(chave.strip(), valor.strip().strip("'\""))   # shell ganha

TIMEOUT = 15          # Cap. 8: toda requisicao precisa de timeout
LIMITE_TELEGRAM = 4000   # uma mensagem do bot nao pode passar disso (API: 4096)
JANELA_DIAS = 30      # prazos do proximo mes
NOTION_VERSAO = "2022-06-28"       # versao da API do Notion fixada no header
DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]   # pt-BR sem depender do locale
TITULO_PAGINA = "Secretaria Acadêmica"   # pagina unica; os dias sao secoes dentro dela
SECAO_DIA = re.compile(r"\d{2}/\d{2}/\d{4}")   # cabecalho de secao: dd/mm/aaaa


def carregar_config():
    """Le as credenciais do ambiente (.env). Cap. 10."""
    load_dotenv()   # carrega o .env para os.environ sem sobrescrever o que ja existe
    return {
        "ics": os.getenv("BB_ICS_URL", ""),                      # link secreto do calendario
        "notion_token": os.getenv("NOTION_TOKEN", ""),           # token da integracao
        "notion_pagina": os.getenv("NOTION_PAGE_ID", ""),        # pagina-mae no Notion
        "telegram_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),   # token do bot
        "telegram_chat": os.getenv("TELEGRAM_CHAT_ID", ""),      # destino da mensagem
    }


def baixar(url):
    """GET com timeout e raise_for_status. Cap. 8."""
    resposta = requests.get(url, timeout=TIMEOUT)   # uma requisicao, com prazo de 15s
    resposta.raise_for_status()                     # 4xx/5xx viram excecao
    return resposta.text                            # ICS e JSON: texto puro


def ler_data(valor):
    """Aceita 20261204T235900, 20261204 ou 20261205T025900Z. Cap. 9."""
    valor = valor.strip()   # o ICS pode trazer espacos no fim da linha
    try:
        if valor.endswith("Z"):
            return (datetime.strptime(valor, "%Y%m%dT%H%M%SZ")   # le como UTC
                    .replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None))   # UTC -> local
        if "T" in valor:
            return datetime.strptime(valor[:15], "%Y%m%dT%H%M%S")   # data e hora sem fuso
        return datetime.strptime(valor[:8], "%Y%m%d")   # so a data: meia-noite
    except ValueError:
        return None   # valor inesperado: o evento e descartado sem quebrar


def ler_ics(texto):
    """Converte o texto ICS em lista de dicionarios. Cap. 9."""
    texto = re.sub(r"\r?\n[ \t]", "", texto)   # desfaz as linhas continuadas do ICS
    eventos = []
    for bloco in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", texto, re.S):   # re.S: "." pega \n
        campos = {}
        for linha in bloco.strip().splitlines():
            if ":" in linha:                          # linha valida do padrao chave:valor
                chave, valor = linha.split(":", 1)    # split(1): o valor pode ter ":"
                campos.setdefault(chave.split(";")[0], valor)   # guarda a 1a ocorrencia
        inicio = ler_data(campos.get("DTSTART", ""))   # data de inicio do evento
        if inicio:                                     # evento sem data valida e ignorado
            eventos.append({"inicio": inicio, "titulo": campos.get("SUMMARY", "(sem título)")})
    return eventos


class FeedVazio(Exception):
    """O feed respondeu, mas sem nenhum evento (link revogado ou calendario vazio)."""


def coletar(config):
    """Baixa o ICS do Blackboard; feed vazio e erro. Cap. 9."""
    if not config["ics"]:
        raise ValueError("BB_ICS_URL nao definido")   # sem link, nao ha fonte
    eventos = ler_ics(baixar(config["ics"]))   # unica fonte: o link do .env
    if not eventos:
        raise FeedVazio("feed do Blackboard retornou 0 eventos (link revogado?)")   # 200 vazio
    return eventos


def filtrar(eventos, dias=JANELA_DIAS):
    """Mantem os proximos N dias; se nao houver, mostra o proximo prazo."""
    agora = datetime.now()                  # referencia: agora, hora local
    limite = agora + timedelta(days=dias)   # fim da janela de 30 dias
    futuros = sorted([e for e in eventos if agora <= e["inicio"] <= limite],   # dentro da janela
                     key=lambda e: e["inicio"])                                # ordem cronologica
    if futuros:
        return futuros
    proximos = sorted([e for e in eventos if e["inicio"] >= agora],   # nada em 30 dias: o que vem
                      key=lambda e: e["inicio"])
    return proximos[:1]   # regra 6: mostra so o proximo prazo, nunca lista vazia


def montar_resumo(eventos):
    """Transforma a lista em texto legivel, agrupado por dia. Cap. 9."""
    if not eventos:
        return "Secretaria Acadêmica\n\nNenhum prazo encontrado no calendário."
    dias = {e["inicio"].date() for e in eventos}   # quantos dias distintos existem
    linhas = []
    dia_atual = None
    for evento in eventos:
        inicio = evento["inicio"]
        dia = inicio.date()
        if dia != dia_atual:   # comecou um novo dia: escreve o cabecalho do bloco
            rotulo = f"{DIAS[inicio.weekday()]} {inicio:%d/%m} - {inicio:%H:%M}"
            if len(dias) == 1:
                linhas.append(f"Entrega até dia: {rotulo}")   # um dia: a data e o cabecalho
            else:
                if dia_atual is None:
                    linhas.append("Entregas:")   # varios dias: rotulo curto uma vez so
                linhas.append(rotulo)            # depois, uma data por bloco
            dia_atual = dia
        linhas.append(f"  • {evento['titulo']}")   # titulo e data, como vem do feed
    return "\n".join(linhas)


def salvar_json(eventos):
    """Guarda a ultima saida em prazos.json (saida concreta, spec R5)."""
    dados = [
        {"inicio": e["inicio"].strftime("%Y-%m-%dT%H:%M"), "titulo": e["titulo"]}   # ISO ate minuto
        for e in eventos
    ]
    with open("prazos.json", "w", encoding="utf-8") as arquivo:   # UTF-8 sempre
        json.dump(dados, arquivo, ensure_ascii=False, indent=2)   # acentos legiveis no arquivo


def limpar_id(valor):
    """Aceita UUID, ID puro ou URL do Notion e devolve o ID com hifens."""
    trecho = valor.strip().split("?")[0].split("#")[0].rstrip("/")   # tira query e barra final
    codigo = re.sub(r"[^0-9a-fA-F]", "", trecho)[-32:]   # so os 32 hex do ID, do fim
    if len(codigo) < 32:
        return valor.strip()   # nao reconheceu: a API reclama se for o caso
    codigo = codigo.lower()
    return f"{codigo[:8]}-{codigo[8:12]}-{codigo[12:16]}-{codigo[16:20]}-{codigo[20:]}"   # formato


def cabecalhos_notion(config):
    """Headers comuns das chamadas ao Notion. Cap. 10."""
    return {
        "Authorization": f"Bearer {config['notion_token']}",   # token vindo do .env
        "Notion-Version": NOTION_VERSAO,                       # versao fixada da API
        "Content-Type": "application/json",                    # corpo em JSON
    }


def blocos_paragrafo(texto):
    """Converte o resumo em blocos de paragrafo (um por linha). Cap. 9."""
    return [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": linha}}]},
        }
        for linha in texto.splitlines() if linha.strip()   # ignora linha vazia
    ]


def achar_pagina_unica(config):
    """Procura a pagina filha de titulo fixo; devolve o ID ou ""."""
    url = (f"https://api.notion.com/v1/blocks/"
           f"{limpar_id(config['notion_pagina'])}/children")   # filhas da pagina-mae
    cursor = None
    while True:   # a API pagina de 100 em 100
        parametros = {"page_size": 100}
        if cursor:
            parametros["start_cursor"] = cursor
        resposta = requests.get(url, headers=cabecalhos_notion(config),
                                params=parametros, timeout=TIMEOUT)
        resposta.raise_for_status()
        dados = resposta.json()
        for bloco in dados.get("results", []):
            if bloco.get("type") == "child_page":
                if bloco["child_page"].get("title", "").strip() == TITULO_PAGINA:
                    return bloco["id"]   # achou a pagina unica
        if not dados.get("has_more"):
            return ""
        cursor = dados.get("next_cursor")


def texto_do_cabecalho(bloco):
    """Le o texto de um bloco heading_2 (titulo da secao do dia)."""
    if bloco.get("type") != "heading_2":
        return ""
    partes = bloco["heading_2"].get("rich_text", [])
    texto = "".join(p.get("plain_text") or p.get("text", {}).get("content", "")
                    for p in partes)
    return texto.strip()


def eh_secao_de_dia(bloco):
    """Reconhece um heading_2 cujo texto e uma data dd/mm/aaaa."""
    return bool(SECAO_DIA.fullmatch(texto_do_cabecalho(bloco)))


def blocos_do_dia(data, texto):
    """Monta o cabecalho da data e os paragrafos do resumo."""
    cabecalho = {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text", "text": {"content": data}}]},
    }
    return [cabecalho] + blocos_paragrafo(texto)


def separar_secoes(blocos):
    """Separa o conteudo fixo e as secoes por data, na ordem em que aparecem."""
    fixo, secoes, atual = [], {}, None
    for bloco in blocos:
        if eh_secao_de_dia(bloco):
            atual = texto_do_cabecalho(bloco)
            secoes[atual] = [bloco]
        elif atual:
            secoes[atual].append(bloco)
        else:
            fixo.append(bloco)
    return fixo, secoes


def copiar_bloco(bloco):
    """Mantem so os campos que a API aceita ao reescrever o bloco."""
    tipo = bloco.get("type", "paragraph")
    dados = {k: v for k, v in bloco.get(tipo, {}).items() if v is not None}   # icon null quebra
    if "rich_text" in dados:   # plain_text e somente leitura na API
        dados["rich_text"] = [{k: v for k, v in p.items() if k != "plain_text"}
                              for p in dados["rich_text"]]
    return {"object": "block", "type": tipo, tipo: dados}


def listar_blocos(config, pagina_id):
    """Le todos os blocos da pagina, pagina de 100 em 100."""
    url = f"https://api.notion.com/v1/blocks/{pagina_id}/children"
    blocos, cursor = [], None
    while True:
        parametros = {"page_size": 100}
        if cursor:
            parametros["start_cursor"] = cursor
        resposta = requests.get(url, headers=cabecalhos_notion(config),
                                params=parametros, timeout=TIMEOUT)
        resposta.raise_for_status()
        dados = resposta.json()
        blocos.extend(dados.get("results", []))
        if not dados.get("has_more"):
            return blocos
        cursor = dados.get("next_cursor")


def substituir_blocos(config, pagina_id, blocos):
    """Grava a lista nova primeiro e so entao apaga a antiga, em lotes de 100.

    A ordem importa: se a gravacao falhar, a pagina continua com o conteudo de
    antes em vez de ficar vazia e perder a secao do dia anterior.
    """
    antigos = listar_blocos(config, pagina_id)   # ids do conteudo que vai sair
    url = f"https://api.notion.com/v1/blocks/{pagina_id}/children"
    for inicio in range(0, len(blocos), 100):
        requests.patch(url, headers=cabecalhos_notion(config),
                       json={"children": blocos[inicio:inicio + 100]},
                       timeout=TIMEOUT).raise_for_status()   # PATCH aceita ate 100 blocos
    for bloco in antigos:
        requests.delete(f"https://api.notion.com/v1/blocks/{bloco['id']}",
                        headers=cabecalhos_notion(config),
                        timeout=TIMEOUT).raise_for_status()   # apaga o que ja foi lido


def publicar_no_notion(config, data, texto):
    """Mantem uma pagina unica, com o dia atual no topo; devolve a URL. Cap. 8 + 10."""
    pagina_id = achar_pagina_unica(config)
    if not pagina_id:
        corpo = {
            "parent": {"page_id": limpar_id(config["notion_pagina"])},
            "properties": {"title": {"title": [{"text": {"content": TITULO_PAGINA}}]}},
        }
        resposta = requests.post("https://api.notion.com/v1/pages",
                                 headers=cabecalhos_notion(config), json=corpo, timeout=TIMEOUT)
        resposta.raise_for_status()
        dados = resposta.json()
        pagina_id = dados["id"]
        url = dados.get("url", "")
    else:
        resposta = requests.get(f"https://api.notion.com/v1/pages/{pagina_id}",
                                headers=cabecalhos_notion(config), timeout=TIMEOUT)
        resposta.raise_for_status()
        url = resposta.json().get("url", "")

    fixo, secoes = separar_secoes(listar_blocos(config, pagina_id))
    secoes[data] = blocos_do_dia(data, texto)   # mesmo dia: substitui a secao
    ordem = [data] + [dia for dia in secoes if dia != data]   # recente primeiro
    novos = fixo + [bloco for dia in ordem for bloco in secoes[dia]]
    substituir_blocos(config, pagina_id, [copiar_bloco(b) for b in novos])
    return url


def enviar_telegram(config, texto):
    """Envia a mensagem em partes, respeitando o limite da API. Cap. 8."""
    url = f"https://api.telegram.org/bot{config['telegram_token']}/sendMessage"
    partes, atual = [], ""   # mensagens prontas e a que esta sendo montada
    for linha in texto.splitlines(keepends=True):   # quebra preservando a quebra de linha
        if atual and len(atual) + len(linha) > LIMITE_TELEGRAM:
            partes.append(atual)   # fechou uma parte: comeca outra
            atual = ""
        atual += linha   # as linhas do resumo sao curtas, cabem inteiras
    if atual:
        partes.append(atual)   # ultima parte
    for parte in partes:
        resposta = requests.post(url,
                                 json={"chat_id": config["telegram_chat"], "text": parte},
                                 timeout=TIMEOUT)
        resposta.raise_for_status()   # token errado ou chat invalido viram excecao


def descrever_erro(erro):
    """Resumo do erro sem a URL: token do bot e link do ICS viajam dentro dela."""
    resposta = getattr(erro, "response", None)   # HTTPError traz a resposta do servidor
    if resposta is not None:
        return f"HTTP {resposta.status_code}"   # status basta; a URL carrega a credencial
    if isinstance(erro, requests.RequestException):
        return f"falha de rede ({type(erro).__name__})"   # sem host, sem caminho, sem token
    return str(erro)   # erro do proprio script: nao tem credencial na mensagem


def main():
    """Le os argumentos, coleta, resume e publica. Devolve o codigo de saida."""
    parser = argparse.ArgumentParser(description="Resumo acadêmico diário")
    parser.add_argument("--simular", action="store_true", help="imprime sem enviar nada")
    args = parser.parse_args()

    config = carregar_config()
    faltando = [chave for chave in
                ("notion_token", "notion_pagina", "telegram_token", "telegram_chat")
                if not config[chave]]   # credenciais obrigatorias fora do modo de teste
    if faltando and not args.simular:
        print(f"[erro] variáveis ausentes no .env: {', '.join(faltando)}", file=sys.stderr)
        return 1   # codigo != 0: o cron registra a falha no log

    try:
        eventos = coletar(config)   # unica fonte: feed ICS
    except FeedVazio as erro:
        print(f"[erro] {erro}", file=sys.stderr)   # link revogado: nao usar dado ficticio
        return 1
    except (requests.RequestException, ValueError, OSError) as erro:
        print(f"[erro] coleta: {descrever_erro(erro)}", file=sys.stderr)   # sem o link secreto
        return 1   # nunca inventar prazo: sem dados locais, sem fallback

    eventos = filtrar(eventos)                       # janela de 30 dias
    resumo = montar_resumo(eventos)                  # texto agrupado por dia
    data = f"{datetime.now():%d/%m/%Y}"   # chave da secao do dia
    salvar_json(eventos)                             # saida concreta em disco

    if args.simular:
        print(resumo)
        print("\n[simulação] nenhuma página criada, nenhuma mensagem enviada")
        return 0   # --simular nunca envia nada e nunca falha por credencial

    url_notion = ""
    try:
        url_notion = publicar_no_notion(config, data, resumo)   # Notion antes do Telegram
        print(f"[ok] página no Notion: {url_notion}")
    except requests.RequestException as erro:
        print(f"[erro] Notion: {descrever_erro(erro)}", file=sys.stderr)   # segue sem link no Telegram

    mensagem = resumo if not url_notion else f"{resumo}\n\nNo Notion: {url_notion}"
    try:
        enviar_telegram(config, mensagem)
        print("[ok] mensagem enviada no Telegram")
    except requests.RequestException as erro:
        print(f"[erro] Telegram: {descrever_erro(erro)}", file=sys.stderr)   # sem o token na linha
        return 1   # sem aviso no Telegram, a execucao falhou de fato

    return 0


if __name__ == "__main__":
    sys.exit(main())   # propaga o codigo de saida para o cron

