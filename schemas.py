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

    id: identificador estável da transação DENTRO da fatura (índice na lista).
        Preenchido na serialização (fatura_para_dict) quando None; os parsers
        não precisam se preocupar com ele.
    """
    data: date
    descricao: str
    valor: Decimal
    parcela_atual: Optional[int] = None
    parcela_total: Optional[int] = None
    moeda: str = "BRL"
    categoria: Optional[str] = None
    cartao: str = ""
    id: Optional[int] = None


@dataclass
class Fatura:
    """
    Fatura parseada — output do parser.

    Campos fixos:
    - banco: nome do banco (ex: "sofisa")
    - modelo: versão do layout (ex: "2026-09")
    - fechamento: data de fechamento da fatura
    - vencimento: data de vencimento
    - total_a_pagar: valor total da fatura
    - pagamento_minimo: valor mínimo de pagamento
    - transacoes: lista plana de transações, cada uma tagueada com "cartao"
    """
    banco: str
    modelo: str
    fechamento: date
    vencimento: date
    total_a_pagar: Decimal
    pagamento_minimo: Decimal
    transacoes: list[Transacao] = field(default_factory=list)


def fatura_para_dict(fatura: Fatura) -> dict:
    """
    Serializa uma Fatura para dict JSON-serializável.

    Converte:
    - date → ISO string
    - Decimal → string

    Cada transação recebe "id": índice na lista da fatura (ou o id já
    atribuído à transação, quando houver — permite round-trip estável de
    transações já persistidas).
    """
    return {
        "banco": fatura.banco,
        "modelo": fatura.modelo,
        "fechamento": fatura.fechamento.isoformat(),
        "vencimento": fatura.vencimento.isoformat(),
        "total_a_pagar": str(fatura.total_a_pagar),
        "pagamento_minimo": str(fatura.pagamento_minimo),
        "transacoes": [
            {
                "id": t.id if t.id is not None else i,
                "data": t.data.isoformat(),
                "descricao": t.descricao,
                "valor": str(t.valor),
                "parcela_atual": t.parcela_atual,
                "parcela_total": t.parcela_total,
                "moeda": t.moeda,
                "categoria": t.categoria,
                "cartao": t.cartao,
            }
            for i, t in enumerate(fatura.transacoes)
        ],
    }
