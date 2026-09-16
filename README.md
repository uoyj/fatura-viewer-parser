# fatura-viewer

API + parsers de faturas de cartão de crédito em **Python 3.12**.

## Visão geral

Extrai transações de faturas de cartão de crédito a partir de PDFs e devolve JSON estruturado. A arquitetura é modular:

```
fatura-viewer/
├── extractors/        # Extração bruta de PDF (texto, posições, tabelas). Genérico, sem lógica de banco.
├── parsers/           # Um módulo por modelo banco+versão (ex: sofisa_2026_09.py)
├── registry.py        # Registry (banco, versão) → função parser + get_parser()
├── schemas.py         # Dataclasses: Transacao, Cartao, Fatura
├── cli.py             # CLI: roda parser sobre um PDF e imprime JSON
└── tests/fixtures/    # PDFs de exemplo + JSON de expected output
```

**Regra de ouro:** o parser **nunca** lê o PDF diretamente. Só recebe o output do extractor.

## Instalação

```bash
# Ativar venv
source .venv/bin/activate

# Instalar dependências
uv pip install -e ".[dev]"
```

## Uso

### CLI — rodar parser sobre um PDF

```bash
# Usar auto-detecção de banco
python cli.py parse faturas/sofisa_setembro_2026.pdf

# Forçar banco e versão
python cli.py parse faturas/exemplo.pdf --banco sofisa --versao 2026-09

# Listar parsers disponíveis
python cli.py parsers
```

Output: JSON com a estrutura `Fatura`:

```json
{
  "banco": "sofisa",
  "modelo": "2026-09",
  "fechamento": "2026-09-15",
  "vencimento": "2026-09-25",
  "total_a_pagar": "3250.00",
  "pagamento_minimo": "975.00",
  "transacoes": [
    {
      "data": "2026-09-01",
      "descricao": "SUPERMERCADO XYZ",
      "valor": "150.00",
      "parcela_atual": 1,
      "parcela_total": 3,
      "moeda": "BRL",
      "categoria": null,
      "cartao": "4563**.*******.9219"
    }
  ]
}
```

### Extração bruta (sem parser específico)

```python
from extractors.pdf_extractor import extract_text, extract_lines
from pathlib import Path

# Texto estruturado com posições
data = extract_text(Path("fatura.pdf"))

# Linhas achatadas
lines = extract_lines(Path("fatura.pdf"))
for line in lines:
    print(f"Página {line['page']}, top {line['top']}: ", end="")
    print(" ".join(w["text"] for w in line["words"]))
```

## Convenções

-  **JSON serializável** — sempre via `json.dumps`
-  **Datas** — ISO (`YYYY-MM-DD`)
-  **Valores monetários** — `Decimal`, serializado como string (`"150.00"`) — **nunca** `float`
-  **Transacao.valor** — positivo = débito (compra), negativo = crédito (estorno/pagamento)
-  **categoria** — `null` por enquanto (sem categorização automática)

## Adicionar um novo parser

1. Criar `parsers/<banco>_<versao>.py` (ex: `parsers/inter_2025_03.py`)
2. Registrar com o decorator:

```python
from registry import registrar
from schemas import Fatura, Cartao, Transacao
from extractors.pdf_extractor import extract_text

@registrar("inter", "2025-03")
def parse_inter_2025_03(extractor_output: dict) -> Fatura:
    pages = extractor_output["pages"]
    # ... lógica de parsing ...
    return Fatura(
        banco="inter",
        modelo="2025-03",
        fechamento=date(2026, 3, 15),
        vencimento=date(2026, 3, 25),
        total_a_pagar=Decimal("3250.00"),
        pagamento_minimo=Decimal("975.00"),
        cartoes=[Cartao(numero_mascarado="1234 **** **** 5678", titular="JOÃO", transacoes=[...])],
    )
```

3. Importar no parser no `__init__.py`:

```python
# parsers/__init__.py
from . import inter_2025_03  # noqa: F401 — registra o parser
```

4. Usar:

```bash
python cli.py parse fatura.pdf --banco inter  # "latest" por padrão
```

## Extratores

| Função | Descrição |
|--------|-----------|
|- `extract_text(path)` | Texto + linhas com posições (`x0`, `top`) + tabelas. Usa pdfplumber primário, pymupdf fallback para scans. |
| `extract_lines(path)` | Lista achatada de linhas visuais — `{page, top, words: [{text, x0}]}`. |

## Testes

```bash
# Rodar todos os testes
pytest

# Com cobertura
pytest --cov=.
```

Fixtures de PDFs e expected outputs vão em `tests/fixtures/`.
