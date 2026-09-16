"""
Data classes para representação de faturas de cartão de crédito.

Todas as datas em ISO (YYYY-MM-DD).
Valores em Decimal (serializados como string no JSON, nunca float).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional


@dataclass
class Transacao:
    """
    Uma transação na fatura.

    valor: positivo = débito (compra), negativo = crédito (estorno/pagamento)
    """
    data: date
    descricao: str
    valor: Decimal
    parcela_atual: Optional[int] = None
    parcela_total: Optional[int] = None
    moeda: str = "BRL"
    categoria: Optional[str] = None


@dataclass
class Cartao:
    """
    Cartão dentro da fatura.
    """
    numero_mascarado: str
    titular: str
    transacoes: list[Transacao] = field(default_factory=list)


@dataclass
class Fatura:
    """
    Fatura parseada — output do parser.

    Camores fixos:
    - banco: nome do banco (ex: "sofisa")
    - modelo: versão do layout (ex: "2026-09")
    - fechamento: data de fechamento da fatura
    - vencimento: data de vencimento
    - total_a_pagar: valor total da fatura
    - pagamento_minimo: valor mínimo de pagamento
    - cartoes: lista de cartões com suas transações
    """
    banco: str
    modelo: str
    fechamento: date
    vencimento: date
    total_a_pagar: Decimal
    pagamento_minimo: Decimal
    cartoes: list[Cartao] = field(default_factory=list)


def fatura_para_dict(fatura: Fatura) -> dict:
    """
    Serializa uma Fatura para dict JSON-serializável.

    Converte:
    - date → ISO string
    - Decimal → string
    """
    return {
        "banco": fatura.banco,
        "modelo": fatura.modelo,
        "fechamento": fatura.fechamento.isoformat(),
        "vencimento": fatura.vencimento.isoformat(),
        "total_a_pagar": str(fatura.total_a_pagar),
        "pagamento_minimo": str(fatura.pagamento_minimo),
        "cartoes": [
            {
                "numero_mascarado": cartao.numero_mascarado,
                "titular": cartao.titular,
                "transacoes": [
                    {
                        "data": t.data.isoformat(),
                        "descricao": t.descricao,
                        "valor": str(t.valor),
                        "parcela_atual": t.parcela_atual,
                        "parcela_total": t.parcela_total,
                        "moeda": t.moeda,
                        "categoria": t.categoria,
                    }
                    for t in cartao.transacoes
                ],
            }
            for cartao in fatura.cartoes
        ],
    }
