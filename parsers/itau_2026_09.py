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
- Segunda data colada no estabelecimento: "FARMACIA DRO*N 06/06 34,40",
  "JIM.COM* IBUY 03/12 476,28", "FILIAL 523 CTB 02/03 35,54" — isso é
  parcelamento, NÃO uma segunda transação. É descartado.
- Datas DD/MM sem ano: ano = ano do fechamento, se mês > fechamento+1
  → ano anterior (heurística genérica).
- A linha "Compras parceladas - próximas faturas" separa duas zonas:
  - Zona 1 (antes): transações atuais, col L e col R incluídas.
  - Zona 2 (depois): col L = parcelas atuais (inclua), col R = parcelas
    FUTURAS (ignore — não fazem parte da fatura atual).
- Fim da zona: "Total para próximas faturas" ou "Limites de crédito".
- Linhas de categoria/cidade (ex: "saúde CURITIBA") são ignoradas.

Convenções herdadas:
- Decimal para valores (string no JSON, nunca float).
- Data ISO (YYYY-MM-DD).
- valor positivo = débito (compra), negativo = crédito (estorno/pagamento).
- cartao = tag do cartão (últimos 4 dígitos do holder, "3620").
"""

from __future__ import annotations

import logging
import re
from datetime import date
from decimal import Decimal

from registry import registrar
from schemas import Fatura, Transacao

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Regexes
# --------------------------------------------------------------------------

# "Vencimento: 10/09/2026"
RE_VENCIMENTO = re.compile(r"Vencimento:\s*(\d{2})/(\d{2})/(\d{4})")

# "Emissão: 03/09/2026" → fechamento
RE_EMISSAO = re.compile(r"Emissão:\s*(\d{2})/(\d{2})/(\d{4})")

# "Total desta fatura 1.940,84"
RE_TOTAL_A_PAGAR = re.compile(r"Total desta fatura\s*([\d.]+,\d{2})", re.IGNORECASE)

# "O total da sua fatura é: R$ 9.880,00 10/09/2026 R$ 1.940,84" → pega último
RE_TOTAL_CTX = re.compile(r"total da sua fatura é\s*:?\s*(.*?)(?:\s+\d{2}/\d{2}/\d{4})", re.IGNORECASE)

# "Pagamento mínimo: R$ 194,08"
RE_PAGAMENTO_MINIMO = re.compile(r"Pagamento mínimo[^R$]*R\$\s*([\d.]+,\d{2})", re.IGNORECASE)

# "Total da fatura anterior 2.111,00"
RE_FATURA_ANTERIOR = re.compile(r"Total da fatura anterior\s*([\d.]+,\d{2})", re.IGNORECASE)

# "Pagamento efetuado em 03/08/2026 -2.111,00" → crédito
RE_PAGAMENTO_EFETUADO = re.compile(
    r"Pagamento efetuado em\s*(\d{2}/\d{2}/\d{4})\s+[−\-]\s*R?\$\s*([\d.]+,\d{2})",
    re.IGNORECASE,
)

# Cartão: "5149.XXXX.XXXX.3620" → últimos 4 dígitos
RE_CARTAO_ITAU = re.compile(r"(\d{4})\.\w*\.\w*\.(\d{4})")

# Data DD/MM no início de uma linha/coluna
RE_DATA_INICIO = re.compile(r"^(\d{2})/(\d{2})\s")

# Data DD/MM em qualquer posição
RE_DATA_DDMM = re.compile(r"(?<!\d)(\d{2})/(\d{2})(?!\d)")

# Valor monetário: "1.940,84", "454,21", "34,40"
RE_VALOR = re.compile(r"([\d.]+,\d{2})")

# "03/08 Pagamento via conta -2.111,00" → crédito na mesma linha
RE_PAGAMENTO_LINHA = re.compile(
    r"(\d{2})/(\d{2})\s+Pagamento via conta\s*[−\-]\s*([\d.]+,\d{2})",
)

# Linha só com data "11/08"
RE_SOMENTE_DATA = re.compile(r"^(\d{2})/(\d{2})$")

# Linhas de categoria/cidade (metadados — ignorar)
RE_CATEGORIA = re.compile(
    r"^(?:saúde|saude|transporte|supermercado|outros|restaurante|"
    r"farmacia|farmácia|eletronicos|eletrônicos)\b",
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

    Page 1: total_a_pagar, pagamento_minimo, vencimento, fechamento, cartão.
    Texto completo: saldo_anterior ("Total da fatura anterior"), créditos
    ("Pagamento efetuado em 03/08/2026 -2.111,00" ou fallback "Pagamento
    via conta -2.111,00" na página 2).

    Equação: 2111.00 - 2111.00 + 1940.84 = 1940.84
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
        creditos = abs(_parse_valor(m.group(2)))

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

    # Débitos: equação do header
    debitos = None
    if total_a_pagar and saldo_anterior and creditos:
        debitos = total_a_pagar - saldo_anterior + creditos

    return {
        "saldo_anterior": saldo_anterior,
        "creditos": creditos,
        "debitos": debitos,
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
    "09/03 FARMACIA DRO*N 06/06 34,40" (06/06 = parcelamento, ignorado)

    - data = primeira data DD/MM
    - valor = último número X,XX da coluna
    - data-extra (ex: 06/06) é parcelamento → ignorada
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
    descricao = t[m_data.end():pos_valor].strip()
    # Remover data-extra de parcelamento (ex: 06/06, 03/12)
    descricao = RE_DATA_DDMM.sub("", descricao).strip()
    descricao = descricao.replace("R$", "").strip()

    return [Transacao(
        data=data,
        descricao=descricao,
        valor=valor,
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

    # Encontrar página com "Lançamentos: compras e saques"
    page_transacoes = None
    for page in pages:
        texto = " ".join(l["text"] for l in page["lines"])
        if "Lançamentos: compras e saques" in texto:
            page_transacoes = page
            break
    if page_transacoes is None:
        raise ParserError("Página de lançamentos não encontrada")

    data_pendente: date | None = None
    modo_parcelado = False

    for line in page_transacoes["lines"]:
        raw = line["text"].strip()
        if not raw:
            continue

        # Fim da zona de lançamentos (Limites de crédito é o verdadeiro fim;
        # "Total para próximas faturas" é um header secundário que não pára)
        if re.search(r"Limites de crédito", raw, re.IGNORECASE):
            break

        # "Compras parceladas - próximas faturas": ativar modo_parcelado
        if "Compras parceladas" in raw:
            modo_parcelado = True
            continue

        # Dividir em colunas
        left_words, right_words = _split_colunas_line(line)

        for is_direita, col_words in [(False, left_words), (True, right_words)]:
            if not col_words:
                continue

            # Na modo_parcelado, a col direita = parcelas futuras → ignora
            if modo_parcelado and is_direita:
                continue

            text = _normalizar_texto(" ".join(w["text"] for w in col_words)).strip()
            if not text:
                continue

            # Ignorar cabeçalhos de coluna
            if text.startswith("DATA VALOR") or text.startswith("DATA ESTABECE"):
                continue
            if text.startswith("Lançamentos:") or text.lower().startswith("lançamentos:"):
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
            if "Pagamento via conta" in text:
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

    # 2. Equação do header: saldo_anterior - creditos + debitos = total
    esperado = saldo_anterior - creditos + debitos
    if abs(esperado - fatura.total_a_pagar) > TOLERANCIA:
        raise ParserError(
            f"equação do header não fecha: {saldo_anterior} - {creditos} + {debitos} "
            f"= {esperado}, mas total_a_pagar = {fatura.total_a_pagar}"
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
