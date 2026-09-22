"""
Parser para faturas Itaú — layout "2026-09" (Platinum).

Arquitetura seguida da mesma do sofisa/nubank:
- Parser recebe o output do extractor (pages), nunca lê PDF diretamente.
- Header extraído da página 1 (e texto completo como fallback).
- Transações extraídas da página 2 ("Lançamentos: compras e saques").
- Validação por equação do header + soma global das transações.

Layout do Itaú (bem mais irregular que Sofisa/Nubank):
- Duas colunas lado a lado: esquerda (x0≈133) e direita (x0≈351).
- Uma mesma linha física pode conter DOIS lançamentos:
  "03/08 Pagamento via conta -2.111,00  11/08 DROGARIAS NISSEICURITIB 454,21"
  → crédito (03/08, -2111.00) + débito (11/08, 454.21)
- Segunda data colada no estabelecimento é PARCELA, não data nem transação:
  "nuuvem *N 02/06 23,76" → parcela 2/6; "FARMACIA DRO*N06/06" → 6/6;
  "JIM.COM* IBUY03/12" → 3/12; "FILIAL 523 CTB 02/03" → 2/3.
  (confere com a seção "Compras parceladas - próximas faturas" da própria fatura)
- Datas DD/MM sem ano: ano = ano do fechamento, se mês > fechamento+1
  → ano anterior (heurística genérica).
- A ZONA de lançamentos começa na primeira página com "Lançamentos: compras e
  saques" e PODE CONTINUAR nas páginas seguintes (fatura de 2026-09-18: começa
  na p2 e termina na p3). Termina em "Limites de crédito".
- A linha "Compras parceladas - próximas faturas" separa duas zonas:
  - Zona 1 (antes): transações atuais, col L e col R incluídas.
  - Zona 2 (depois): a col R é a tabela de parcelas FUTURAS (ignore); qualquer
    linha que carregue um token de parcela NN/MM também é futura (ignore) —
    a col L restante é a continuação dos lançamentos atuais (inclua).
- ENCARGOS (juros/IOF do rotativo) são um campo próprio do header e ficam numa
  seção separada ("Encargos cobrados nesta fatura", depois de "Limites de
  crédito"), fora da tabela de lançamentos: NÃO entram na soma das transações.
- Linhas de categoria/cidade (ex: "saúde CURITIBA") são ignoradas — e quando
  vierem coladas no fim de uma linha limpa, são removidas da descrição.
- Fim da zona: "Total para próximas faturas" ou "Limites de crédito".

Convenções herdadas:
- Decimal para valores (string no JSON, nunca float).
- Data ISO (YYYY-MM-DD).
- valor positivo = débito (compra), negativo = crédito (estorno/pagamento).
- cartao = tag do cartão (últimos 4 dígitos do holder, "3620").
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date
from decimal import Decimal

from registry import registrar
from schemas import Fatura, Transacao

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Regexes
# --------------------------------------------------------------------------

# "Vencimento: 10/09/2026"
RE_VENCIMENTO = re.compile(r"Vencimento\s*:\s*(\d{2})/(\d{2})/(\d{4})", re.IGNORECASE)

# "Emissão: 03/09/2026" → fechamento
RE_EMISSAO = re.compile(r"Emiss[aã]o\s*:\s*(\d{2})/(\d{2})/(\d{4})", re.IGNORECASE)

# "Total desta fatura 1.940,84"
RE_TOTAL_A_PAGAR = re.compile(r"Total\s*desta\s*fatura\s*([\d.]+,\d{2})", re.IGNORECASE)

# "O total da sua fatura é: R$ 9.880,00 10/09/2026 R$ 1.940,84" → pega último
RE_TOTAL_CTX = re.compile(r"total\s*da\s*sua\s*fatura\s*é\s*:?\s*(.*?)(?:\s+\d{2}/\d{2}/\d{4})", re.IGNORECASE)

# "Pagamento mínimo: R$ 194,08"
RE_PAGAMENTO_MINIMO = re.compile(r"Pagamento\s*m[íi]nimo[^R$]*R\$\s*([\d.]+,\d{2})", re.IGNORECASE)

# "Total da fatura anterior 2.111,00"
RE_FATURA_ANTERIOR = re.compile(r"Total\s*da\s*fatura\s*anterior\s*([\d.]+,\d{2})", re.IGNORECASE)

# "Total de encargos em R$ 115,27" (seção própria, depois de "Limites de crédito")
RE_TOTAL_ENCARGOS = re.compile(r"Total\s*de\s*encargos\s*em\s*R\$\s*([\d.]+,\d{2})", re.IGNORECASE)

# Fallback na p1: "Encargos (Financiamento + moratório) 115,27"
RE_ENCARGOS_P1 = re.compile(
    r"Encargos\s*\(\s*Financiamento\s*\+\s*morat[óo]rio\s*\)\s*([\d.]+,\d{2})",
    re.IGNORECASE,
)

# "Pagamento efetuado em 03/08/2026 -2.111,00" → crédito
RE_PAGAMENTO_EFETUADO = re.compile(
    r"Pagamento\s*efetuado\s*em\s*(\d{2})/(\d{2})/(\d{4})\s+[−\-]\s*R?\$?\s*([\d.]+,\d{2})",
    re.IGNORECASE,
)

# Cartão: "5149.XXXX.XXXX.3620" → últimos 4 dígitos
RE_CARTAO_ITAU = re.compile(r"(\d{4})\.\w*\.\w*\.(\d{4})")

# Data DD/MM no início de uma linha/coluna
RE_DATA_INICIO = re.compile(r"^(\d{2})/(\d{2})\s*")

# Data DD/MM em qualquer posição
RE_DATA_DDMM = re.compile(r"(?<!\d)(\d{2})/(\d{2})(?!\d)")

# Valor monetário: "1.940,84", "454,21", "34,40"
RE_VALOR = re.compile(r"([\d.]+,\d{2})")

# "03/08 Pagamento via conta -2.111,00" → crédito na mesma linha
RE_PAGAMENTO_LINHA = re.compile(
    r"(\d{2})/(\d{2})\s*Pagamento\s*via\s*conta\s*[−\-]\s*([\d.]+,\d{2})",
    re.IGNORECASE,
)

# Linha só com data "11/08"
RE_SOMENTE_DATA = re.compile(r"^(\d{2})/(\d{2})$")

# Linhas de categoria/cidade (metadados — ignorar)
# No PDF novo a categoria gruda no estabelecimento ("saúdeCURITIBA"); \b não
# casa entre 'é' e 'C', então usamos lookahead de letra maiúscula ou fim.
RE_CATEGORIA = re.compile(
    r"^(?:sa[uú]de|saude|servi[cç]os|servicos|transporte|supermercado|outros|"
    r"restaurante|farm[aá]cia|farmacia|eletr[oô]nicos|eletronicos)"
    r"(?=[A-Z]{2,}|\s|$)",
    re.IGNORECASE,
)

# Sufixo "categoria CIDADE" colado no fim de uma linha limpa:
# "LANCHONETE DOIS CORACCU supermercado CURITIBA" → "LANCHONETE DOIS CORACCU"
# O `\s+` inicial (e não `\s*`) é o que garante que o casamento NUNCA comece no
# início da descrição — sem isso, "LANCHONETE DOIS CORACCU" seria zerado.
RE_CATEGORIA_CIDADE_SUFIXO = re.compile(
    r"\s+(?:sa[uú]de|servi[cç]os|servicos|transporte|supermercado|outros|"
    r"restaurante|eletr[oô]nicos|farm[aá]cia|lanchonete)\s+[A-Z][A-Za-z ]{2,}$",
    re.IGNORECASE,
)

# Limite x0 para separar coluna esquerda (≈133) da direita (≈351)
COL_SPLIT_X = 330.0


class ParserError(Exception):
    """Falha de parse ou validação — mensagem sempre acionável."""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _normalizar_texto(s: str) -> str:
    """Normaliza U+2212 (−) e U+00A0 (nbsp) → ASCII."""
    return s.replace("\u2212", "-").replace("\xa0", " ")


def _fold(s: str) -> str:
    """Remove acentos, espaços e quebras de linha para matching tolerante."""
    s = _normalizar_texto(s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", "", s).casefold()


def _parse_valor(s: str) -> Decimal | None:
    """Parseia valor monetário brasileiro. Ex: "7,18" → 7.18, "-2.111,00" → -2111.00"""
    if not s:
        return None
    s = _normalizar_texto(s.strip())
    neg = False
    if s.startswith("-"):
        neg = True
        s = s[1:]
    s = s.replace("R$", "").strip()
    s = s.replace(".", "").replace(",", ".")
    try:
        v = Decimal(s)
    except Exception:
        return None
    return -v if neg else v


def _parse_data_ddmm(token: str, ano_ref: int, mes_fechamento: int) -> date | None:
    """Parseia 'DD/MM' → date. Ano ajustado: se mes > fechamento+1, ano-1."""
    m = RE_DATA_DDMM.search(token)
    if not m:
        return None
    dia, mes = int(m.group(1)), int(m.group(2))
    if mes > mes_fechamento + 1:
        ano = ano_ref - 1
    else:
        ano = ano_ref
    return date(ano, mes, dia)


def _parcela_valida(nn: int, mm: int) -> tuple[bool, bool]:
    """
    Valida um token NN/MM como parcela: (valido, invertido).

    nn>=1, mm>=2, nn<=mm (caso normal) ou nn<=48 e mm<=60 (tolerância para
    formatos fora do comum). nn>mm com ambos <=12 é aceito como possível
    formato INVERTIDO (vem com `invertido=True` para logar warning).
    Token com cara de DATA (dia 13..31 e mês 1..12, ex "13/08") não é parcela.
    """
    if not (nn >= 1 and mm >= 2):
        return False, False
    if nn > mm and nn >= 13 and mm <= 12:
        return False, False      # parece DD/MM de data, não parcela
    if nn <= mm and (nn <= 48 and mm <= 60):
        return True, False
    if nn <= 48 and mm <= 60:
        return True, nn > mm     # aceito, mas possivelmente invertido
    return False, False


def _extrair_parcela(texto: str) -> tuple[int | None, int | None]:
    """
    Extrai a parcela NN/MM do trecho entre a data e o valor.

    "nuuvem *N 02/06" → (2, 6). Se houver mais de um candidato, usa o primeiro
    e loga o resto. Retorna (None, None) quando não há parcela.
    """
    atual: int | None = None
    total: int | None = None
    extras: list[str] = []

    for m in RE_DATA_DDMM.finditer(texto):
        nn, mm = int(m.group(1)), int(m.group(2))
        valido, invertido = _parcela_valida(nn, mm)
        if not valido:
            continue
        if atual is None:
            atual, total = nn, mm
            if invertido:
                logger.warning("parcela em formato possivelmente invertido: %r", m.group(0))
        else:
            extras.append(m.group(0))

    if extras:
        logger.warning("múltiplos candidatos de parcela %s — usando %d/%d",
                       extras, atual, total)
    return atual, total


def _limpar_categoria_cidade(descricao: str) -> str:
    """
    Remove o sufixo "categoria CIDADE" de uma linha limpa (máx. 2x).

    Só remove como SUFIXO: o casamento precisa começar depois de algum
    conteúdo, então "LANCHONETE DOIS CORACCU" (estabelecimento no início)
    fica intacto.
    """
    for _ in range(2):
        m = RE_CATEGORIA_CIDADE_SUFIXO.search(descricao)
        if not m:
            break
        descricao = descricao[:m.start()].strip()
    return descricao


def _tem_token_parcela(texto: str) -> bool:
    """True se a coluna traz, além da data inicial, um DD/MM (token de parcela)."""
    return len(RE_DATA_DDMM.findall(texto)) > 1


def _extract_cartao_tag(pages: list[dict]) -> str:
    """Extrai últimos 4 dígitos do cartão da página 1."""
    texto = " ".join(l["text"] for l in pages[0]["lines"])
    m = RE_CARTAO_ITAU.search(texto)
    if m:
        return m.group(2)  # "3620"
    logger.warning("cartão não localizado no header")
    return ""


def _split_colunas_line(line: dict) -> tuple[list[dict], list[dict]]:
    """Divide as palavras de uma linha pela COL_SPLIT_X."""
    esq, dire = [], []
    for w in line.get("words", []):
        (esq if w["x0"] < COL_SPLIT_X else dire).append(w)
    return esq, dire


# --------------------------------------------------------------------------
# Header
# --------------------------------------------------------------------------

def _extract_header(pages: list[dict]) -> dict:
    """
    Extrai header da fatura Itaú.

    Page 1: total_a_pagar, pagamento_minimo, vencimento, fechamento, cartão,
    encargos (fallback).
    Texto completo: saldo_anterior ("Total da fatura anterior"), créditos
    ("Pagamento efetuado em 03/08/2026 -2.111,00" ou fallback "Pagamento
    via conta -2.111,00" na página 2), encargos ("Total de encargos em R$").

    Equação: saldo_anterior - creditos + debitos + encargos = total_a_pagar
    Ex (sem encargos): 2111.00 - 2111.00 + 1940.84 + 0 = 1940.84
    Ex (com encargos): 2342.49 - 2342.49 + 874.99 + 115.27 = 990.26
    """
    texto_page1 = " ".join(l["text"] for l in pages[0]["lines"])
    texto_todo = " ".join(l["text"] for p in pages for l in p["lines"])

    # Fechamento (Emissão)
    fechamento = None
    m = RE_EMISSAO.search(texto_page1) or RE_EMISSAO.search(texto_todo)
    if m:
        fechamento = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))

    # Vencimento
    vencimento = None
    m = RE_VENCIMENTO.search(texto_page1) or RE_VENCIMENTO.search(texto_todo)
    if m:
        vencimento = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))

    # Total a pagar
    total_a_pagar = None
    # "Total desta fatura 1.940,84" (mais direto)
    m = RE_TOTAL_A_PAGAR.search(texto_page1) or RE_TOTAL_A_PAGAR.search(texto_todo)
    if m:
        total_a_pagar = _parse_valor(m.group(1))
    else:
        # "O total da sua fatura é: R$ 9.880,00 10/09/2026 R$ 1.940,84"
        # Pega contexto até a data, depois o último valor
        m = RE_TOTAL_CTX.search(texto_page1) or RE_TOTAL_CTX.search(texto_todo)
        if m:
            vals = RE_VALOR.findall(m.group(1))
            if vals:
                total_a_pagar = _parse_valor(vals[-1])
        else:
            # Fallback genérico: "Total a pagar R$ X,XX" (se houver)
            m = re.search(r"Total a pagar\s*R?\$\s*([\d.]+,\d{2})", texto_todo, re.IGNORECASE)
            if m:
                total_a_pagar = _parse_valor(m.group(1))

    # Pagamento mínimo
    pagamento_minimo = None
    m = RE_PAGAMENTO_MINIMO.search(texto_page1) or RE_PAGAMENTO_MINIMO.search(texto_todo)
    if m:
        pagamento_minimo = _parse_valor(m.group(1))

    # Saldo anterior
    saldo_anterior = None
    m = RE_FATURA_ANTERIOR.search(texto_todo)
    if m:
        saldo_anterior = _parse_valor(m.group(1))

    # Créditos (Pagamento efetuado na page 4)
    creditos = None
    m = RE_PAGAMENTO_EFETUADO.search(texto_todo)
    if m:
        creditos = abs(_parse_valor(m.group(4)))

    # Fallback: "Pagamento via conta -2.111,00" na secao de lancamentos
    if creditos is None:
        m = re.search(r"Pagamento via conta\s*[−\-]\s*([\d.]+,\d{2})", texto_todo)
        if m:
            creditos = abs(_parse_valor(m.group(1)))
            logger.warning("creditos derivados do 'Pagamento via conta' (resumo ilegivel)")

    # Derivar saldo_anterior = créditos se faltar
    if creditos is not None and saldo_anterior is None:
        saldo_anterior = creditos
        logger.warning("saldo_anterior assumido = creditos (resumo ilegivel)")
    if saldo_anterior is not None and creditos is None:
        creditos = saldo_anterior
        logger.warning("creditos assumidos = saldo_anterior (resumo ilegivel)")

    # Sem resumo de pagamento legível: assume quitação integral (0 - 0 + D + E = T)
    if creditos is None:
        if saldo_anterior is None:
            saldo_anterior = Decimal(0)
        creditos = Decimal(0)
        logger.warning("resumo de pagamento ilegivel — assumindo quitacao integral")

    # Encargos (juros/IOF do rotativo): seção própria, FORA da tabela de lançamentos
    encargos = None
    m = RE_TOTAL_ENCARGOS.search(texto_todo)
    if m:
        encargos = _parse_valor(m.group(1))
    else:
        m = RE_ENCARGOS_P1.search(texto_todo)
        if m:
            encargos = _parse_valor(m.group(1))
            logger.warning("encargos obtidos do fallback da p1 (R$ %s)", m.group(1))
    if encargos is None:
        encargos = Decimal(0)
        logger.warning("campo 'encargos' não encontrado — assume R$ 0,00")

    # Débitos (compras): equação do header, descontando os encargos
    debitos = None
    if total_a_pagar is not None and saldo_anterior is not None and creditos is not None:
        debitos = total_a_pagar - saldo_anterior + creditos - encargos

    return {
        "saldo_anterior": saldo_anterior,
        "creditos": creditos,
        "debitos": debitos,
        "encargos": encargos,
        "total_a_pagar": total_a_pagar,
        "pagamento_minimo": pagamento_minimo,
        "fechamento": fechamento,
        "vencimento": vencimento,
    }


# --------------------------------------------------------------------------
# Transações
# --------------------------------------------------------------------------

def _processar_coluna_pagamento(text: str, cartao_tag: str,
                                ano_ref: int, mes_fechamento: int) -> list[Transacao]:
    """Processa '03/08 Pagamento via conta -2.111,00' → 1 crédito."""
    result: list[Transacao] = []
    m = RE_PAGAMENTO_LINHA.search(text)
    if not m:
        return result

    dia, mes = int(m.group(1)), int(m.group(2))
    valor = -abs(_parse_valor(m.group(3)) or Decimal(0))
    data = _parse_data_ddmm(m.group(0), ano_ref, mes_fechamento)
    if data and valor is not None:
        result.append(Transacao(
            data=data,
            descricao="Pagamento via conta",
            valor=valor,
            cartao=cartao_tag,
        ))
    else:
        logger.warning("pagamento nao parseado: %r", text)
    return result


def _processar_coluna_compra(text: str, cartao_tag: str,
                             ano_ref: int, mes_fechamento: int) -> list[Transacao]:
    """
    Processa uma coluna de transação:
    "11/08 DROGARIAS NISSEICURITIB 454,21"
    "11/07 nuuvem *N 02/06 23,76"  → parcela 2/6 (token NN/MM sai da descrição)

    - data = primeira data DD/MM (início da coluna)
    - valor = último número X,XX da coluna
    - demais tokens DD/MM = parcela (nn/mm), não data
    """
    t = _normalizar_texto(text.strip())
    m_data = RE_DATA_INICIO.match(t)
    if not m_data:
        return []

    dia, mes = int(m_data.group(1)), int(m_data.group(2))
    data = _parse_data_ddmm(t, ano_ref, mes_fechamento)
    if data is None:
        return []

    valores = RE_VALOR.findall(t)
    if not valores:
        return []

    valor_str = valores[-1]
    valor = _parse_valor(valor_str)
    if valor is None:
        return []

    # Descrição: tudo entre a data e o valor
    pos_valor = t.rfind(valor_str)
    bruto = t[m_data.end():pos_valor].strip()

    # "NN/MM" no meio do estabelecimento é PARCELA, não data
    parcela_atual, parcela_total = _extrair_parcela(bruto)

    # Remove os tokens DD/MM e o eventual sufixo "categoria CIDADE"
    descricao = RE_DATA_DDMM.sub("", bruto).replace("R$", "").strip()
    descricao = _limpar_categoria_cidade(descricao)

    return [Transacao(
        data=data,
        descricao=descricao,
        valor=valor,
        parcela_atual=parcela_atual,
        parcela_total=parcela_total,
        cartao=cartao_tag,
    )]


def _extract_transacoes(pages: list[dict], cartao_tag: str,
                        header: dict) -> list[Transacao]:
    """
    Extrai transações da página de "Lançamentos: compras e saques".

    Layout: duas colunas (x0≈133 esq, x0≈351 dir). Linhas físicas são
    divididas em colunas antes de processadas. Após "Compras parceladas -
    próximas faturas", a col R contém parcelas FUTURAS (ignoradas).
    """
    transacoes: list[Transacao] = []

    fechamento = header.get("fechamento")
    if fechamento:
        ano_ref = fechamento.year
        mes_fechamento = fechamento.month
    else:
        ano_ref = 2026
        mes_fechamento = 9

    # Encontrar a PRIMEIRA página com "Lançamentos: compras e saques".
    # A zona pode continuar nas páginas seguintes (fatura de 2026-09-18:
    # começa na p2 e termina na p3) — por isso iteramos pages[idx:] até
    # encontrar o fim da zona.
    idx_lancamentos = None
    for i, page in enumerate(pages):
        texto = " ".join(l["text"] for l in page["lines"])
        if "lancamentos:comprasesaques" in _fold(texto):
            idx_lancamentos = i
            break
    if idx_lancamentos is None:
        raise ParserError("Página de lançamentos não encontrada")

    # Linhas da zona: da página inicial de lançamentos em diante (a zona pode
    # atravessar páginas). O fim é detectado dentro do loop ("Limites de crédito").
    linhas_zona = [line for page in pages[idx_lancamentos:] for line in page["lines"]]

    data_pendente: date | None = None
    modo_parcelado = False
    fim_direita = False
    inicio_lancamentos = False

    for line in linhas_zona:
        raw = line["text"].strip()
        if not raw:
            continue

        # A zona de lançamentos útil começa no marcador. No PDF novo, a coluna
        # direita traz "Pagamentos efetuados" com parcelas FUTURAS antes deste
        # marcador, então só processamos transações da direita depois dele.
        if "lancamentos:comprasesaques" in _fold(raw):
            inicio_lancamentos = True

        # Fim da zona de lançamentos. "Limites de crédito" pode aparecer na
        # coluna direita ANTES da esquerda terminar (PDF 20260921-075540). Se
        # estiver só na direita, marcamos o fim da direita e continuamos a
        # esquerda. Se estiver na esquerda (ou em toda a linha), é o fim real.
        if "limitesdecredito" in _fold(raw):
            left_words, right_words = _split_colunas_line(line)
            if left_words:
                return transacoes
            fim_direita = True
            continue

        # "Compras parceladas - próximas faturas": ativar modo_parcelado
        if "comprasparceladas" in _fold(raw):
            modo_parcelado = True
            continue

        # Resumo da coluna esquerda = fim real dos lançamentos atuais.
        # No PDF novo esse resumo aparece na direita enquanto a esquerda ainda
        # tem transações, então só paramos quando ele estiver na esquerda.
        if re.search(r"Total\s*dos\s*lancamentos\s*atuais|Lancamentos\s*no\s*cartao",
                     _fold(raw), re.IGNORECASE):
            left_words, _ = _split_colunas_line(line)
            if left_words:
                left_text = _normalizar_texto(" ".join(w["text"] for w in left_words)).strip()
                if re.search(r"Total\s*dos\s*lancamentos\s*atuais|Lancamentos\s*no\s*cartao",
                             _fold(left_text), re.IGNORECASE):
                    return transacoes

        # Dividir em colunas
        left_words, right_words = _split_colunas_line(line)

        for is_direita, col_words in [(False, left_words), (True, right_words)]:
            if not col_words:
                continue

            # Direita já acabou no marcador "Limites de crédito"
            if fim_direita and is_direita:
                continue

            text = _normalizar_texto(" ".join(w["text"] for w in col_words)).strip()
            if not text:
                continue

            # Antes do início da zona útil, a direita só pode trazer pagamento
            if is_direita and not inicio_lancamentos:
                if not re.search(r"Pagamento\s*via\s*conta", text, re.IGNORECASE):
                    continue

            # Zona 2 (após "Compras parceladas - próximas faturas"): a col direita
            # é a tabela de faturas FUTURAS e qualquer linha com token de parcela
            # NN/MM também é futura (na fatura de 2026-09-18 a tabela vem na col
            # ESQUERDA, porque a direita é texto). O que sobra na col esquerda é a
            # continuação dos lançamentos atuais.
            if modo_parcelado and (is_direita or _tem_token_parcela(text)):
                continue

            # Ignorar cabeçalhos de coluna
            if _fold(text).startswith("datavaloremr$") or _fold(text).startswith("dataestabelecimento"):
                continue
            if _fold(text).startswith("lancamentos:"):
                continue

            # Ignorar linhas de categoria/cidade
            if RE_CATEGORIA.match(text):
                continue
            if re.search(r"Total dos lançamentos|Total dos pagamentos|Lançamentos no cartão",
                         text, re.IGNORECASE):
                continue

            # Linha só com data → pendente (só col esq)
            m_so_data = RE_SOMENTE_DATA.match(text)
            if m_so_data:
                if not is_direita:
                    data_pendente = _parse_data_ddmm(text, ano_ref, mes_fechamento)
                continue

            # Pagamento (crédito)
            if re.search(r"Pagamento\s*via\s*conta", text, re.IGNORECASE):
                transacoes.extend(_processar_coluna_pagamento(
                    text, cartao_tag, ano_ref, mes_fechamento))
                data_pendente = None
                continue

            # Linha de compra (data no início)
            if RE_DATA_INICIO.match(text):
                transacoes.extend(_processar_coluna_compra(
                    text, cartao_tag, ano_ref, mes_fechamento))
                data_pendente = None
                continue

            # Linha sem data no início mas com valor: usa data pendente
            if (data_pendente is not None and RE_VALOR.search(text)
                    and not RE_CATEGORIA.match(text)):
                valores = RE_VALOR.findall(text)
                valor_str = valores[-1]
                valor = _parse_valor(valor_str)
                if valor is not None and valor > 0:
                    pos = text.rfind(valor_str)
                    descricao = RE_DATA_DDMM.sub("", text[:pos]).replace("R$", "").strip()
                    transacoes.append(Transacao(
                        data=data_pendente,
                        descricao=descricao,
                        valor=valor,
                        cartao=cartao_tag,
                    ))
                continue

    return transacoes


# --------------------------------------------------------------------------
# Validação
# --------------------------------------------------------------------------

TOLERANCIA = Decimal("0.01")


def _validate(fatura: Fatura, header: dict) -> None:
    # 1. Nenhum campo do header pode faltar
    for campo in ("saldo_anterior", "creditos", "debitos",
                  "total_a_pagar", "pagamento_minimo",
                  "fechamento", "vencimento"):
        if header.get(campo) is None:
            raise ParserError(f"campo '{campo}' não encontrado no header da fatura")

    saldo_anterior = header["saldo_anterior"]
    creditos = header["creditos"]
    debitos = header["debitos"]
    # Encargos ficam FORA da tabela de lançamentos: entram na equação do header,
    # mas NÃO na soma das transações (que continua sendo comparada a `debitos`).
    encargos = header.get("encargos") or Decimal(0)

    # 2. Equação do header: saldo_anterior - creditos + debitos + encargos = total
    esperado = saldo_anterior - creditos + debitos + encargos
    if abs(esperado - fatura.total_a_pagar) > TOLERANCIA:
        raise ParserError(
            f"equação do header não fecha: {saldo_anterior} - {creditos} + {debitos} "
            f"+ {encargos} = {esperado}, mas total_a_pagar = {fatura.total_a_pagar}"
        )

    # 3. SOMA GLOBAL das transações bate com os totais do header
    todas = fatura.transacoes
    soma_debitos = sum((t.valor for t in todas if t.valor > 0), Decimal(0))
    soma_creditos = -sum((t.valor for t in todas if t.valor < 0), Decimal(0))

    if abs(soma_debitos - debitos) > TOLERANCIA:
        raise ParserError(
            f"soma de débitos extraída ({soma_debitos}) != débitos do header ({debitos}) "
            f"— diff {abs(soma_debitos - debitos)}"
        )
    if abs(soma_creditos - creditos) > TOLERANCIA:
        raise ParserError(
            f"soma de créditos extraída ({soma_creditos}) != créditos do header ({creditos}) "
            f"— diff {abs(soma_creditos - creditos)}"
        )


# --------------------------------------------------------------------------
# Parser principal
# --------------------------------------------------------------------------

@registrar("itau", "2026-09")
def parse_itau_2026_09(extractor_output: dict) -> Fatura:
    """
    Parser para faturas Itaú (layout 2026-09, Platinum).

    Args:
        extractor_output: output de extract_text() — dict com "pages".

    Returns:
        Fatura — conforme schemas.py.
    """
    pages = extractor_output.get("pages", [])
    if not pages:
        raise ParserError("PDF vazio ou sem páginas")

    if len(pages) < 2:
        raise ParserError(f"Fatura Itaú com {len(pages)} páginas, esperado >= 2")

    header = _extract_header(pages)
    cartao_tag = _extract_cartao_tag(pages)
    transacoes = _extract_transacoes(pages, cartao_tag, header)

    fatura = Fatura(
        banco="itau",
        modelo="2026-09",
        fechamento=header["fechamento"],
        vencimento=header["vencimento"],
        total_a_pagar=header["total_a_pagar"],
        pagamento_minimo=header["pagamento_minimo"],
        transacoes=transacoes,
    )
    _validate(fatura, header)
    return fatura
