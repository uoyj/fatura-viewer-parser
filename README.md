# fatura-viewer

API + parsers de faturas de cartão de crédito em **Python 3.12**.

## Visão geral

Extrai transações de faturas de cartão de crédito a partir de PDFs e devolve JSON estruturado. A arquitetura é modular:

```
fatura-viewer/
├── api.py               # FastAPI: POST /faturas, GET /faturas, GET /parsers + StaticFiles mount
├── static/index.html    # Frontend SPA (HTML+CSS+JS inline, sem CDN/framework)
├── categorizer.py       # Categorização determinística por palavra-chave (12 categorias)
├── extractors/          # Extração bruta de PDF (texto, posições, tabelas). Genérico, sem lógica de banco.
├── parsers/             # Um módulo por modelo banco+versão (ex: sofisa_2026_09.py)
├── registry.py          # Registry (banco, versão) → função parser + get_parser()
├── schemas.py           # Dataclasses: Transacao, Fatura (lista plana — sem Cartao)
├── cli.py               # CLI: roda parser sobre um PDF e imprime JSON
├── pyproject.toml       # Entry point: fatura-viewer = cli:main
└── tests/               # Testes pytest + fixtures
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

### API HTTP (FastAPI — uso interno, sem auth)

```bash
# Iniciar servidor
uvicorn api:app --reload
# → http://127.0.0.1:8000

# Documentação automática: http://127.0.0.1:8000/docs
```

#### Endpoints

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `POST` | `/faturas` | Upload de PDF (multipart: `arquivo`, `banco`, `versao` opcional) |
| `GET` | `/faturas` | Lista resumida: `{id, banco, modelo, fechamento, total_a_pagar}` |
| `GET` | `/faturas/{fatura_id}` | Retorna payload completo da fatura |
| `GET` | `/parsers` | Lista parsers registrados: `{banco: [versoes]}` |

**POST /faturas** — multipart/form-data:
- `arquivo` (PDF) — **obrigatório**
- `banco` (ex: `sofisa`) — **obrigatório**
- `versao` (ex: `2026-09`) — opcional, default `latest`

```bash
# Exemplo: upload de fatura
curl -F "arquivo=@in/Fatura.pdf" -F "banco=sofisa" http://127.0.0.1:8000/faturas
```

Respostas:
- **200** — JSON da fatura + campo `id`
- **422** — `{"erro": "..."}` para: extensão ≠ `.pdf`, parser não encontrado, `ParserError`, PDF corrompido

Dados são persistidos em `data/faturas.jsonl` (uma linha JSON por fatura) e uploads em `data/uploads/<uuid>.pdf`.

### Frontend SPA

Acesse http://127.0.0.1:8000/ — interface web servida pelo próprio FastAPI via `StaticFiles`.

```
Layout:
  ├── Barra superior: título + dropdown de faturas + "Nova fatura"
  ├── Tela upload: input PDF, select banco, POST multipart
  └── Tela viewer: cards header, filtros, tabela ordenável, totais por categoria
```

### CLI — rodar parser sobre um PDF

```bash
# Forçar banco e versão
python cli.py parse in/Fatura.pdf --banco sofisa --versao 2026-09

# Aplicar categorização por palavra-chave
python cli.py parse in/Fatura.pdf --banco sofisa --versao 2026-09 --categorizar

# Listar parsers disponíveis
python cli.py parsers
```

Output: JSON com a estrutura `Fatura`:

```json
{
  "banco": "sofisa",
  "modelo": "2026-09",
  "fechamento": "2026-09-15",
  "vencimento": "2026-09-20",
  "total_a_pagar": "6727.55",
  "pagamento_minimo": "672.76",
  "transacoes": [
    {
      "data": "2025-11-30",
      "descricao": "EC *STERILAIR",
      "valor": "74.80",
      "parcela_atual": 1,
      "parcela_total": 10,
      "moeda": "BRL",
      "categoria": null,
      "cartao": "4563**.******.9219"
    },
    {
      "data": "2026-08-20",
      "descricao": "Compra a Vista WELLHUB MARCOS RICIOLI",
      "valor": "82.40",
      "parcela_atual": null,
      "parcela_total": null,
      "moeda": "BRL",
      "categoria": "Academia/Wellness",
      "cartao": "4563**.******.9219"
    }
  ]
}
```

Com `--categorizar` (ou via API), `categoria` é preenchido por regra de palavra-chave:

```
Pagamento da fatura, Estorno/Ajuste, Academia/Wellness, Farmacia,
Veterinario, Saude/Podologia, Transporte/Apps, Restaurante/Cafe,
Bar, Supermercado/Mercado, Compras online, Outros
```

### Categorização determinística

`categorizer.py` — sem ML, regras de substring (case-insensitive). Primeira que casa vence; fallback `"Outros"`.

```bash
python cli.py parse in/Fatura.pdf --banco sofisa --versao 2026-09 --categorizar
```

| Categoria | Padrões-chave |
|-----------|---------------|
| Pagamento da fatura | `PAGAMENTO DE FATURA` |
| Estorno/Ajuste | `AJUSTE A CREDITO` |
| Academia/Wellness | `WELLHUB` |
| Farmacia | `RAIA DROGASIL`, `PANVEL` |
| Veterinario | `CLINICA VETERINARIA` |
| Saude/Podologia | `PODOMAX`, `IL BARBUTO` |
| Transporte/Apps | `99FOOD`, `99*` |
| Restaurante/Cafe | `NONO CAFE`, `EVEREST INN`, `DOM MARTIELLO` |
| Bar | `DG BARBER`, `DEEP LOUNGE BAR`, `JANELA BAR` |
| Supermercado/Mercado | `MERCADOLIVRE`, `MERCEARIA`, `PANIFICADORA` |
| Compras online | `AMAZON`, `SHOPEE`, `SHEIN`, `STERILAIR` |
| Outros | (fallback) |

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

- **JSON serializável** — sempre via `json.dumps`
- **Datas** — ISO (`YYYY-MM-DD`)
- **Valores monetários** — `Decimal`, serializado como string (`"150.00"`) — **nunca** `float`
- **Transacao.valor** — positivo = débito (compra), negativo = crédito (estorno/pagamento)
- **Transacao.cartao** — número mascarado (`"4563**.******.9219"`), `""` se desconhecido
- **categoria** — `null` sem categorizador; preenchido via `categorizer.categorizar()`

## Adicionar um novo parser

1. Criar `parsers/<banco>_<versao>.py` (ex: `parsers/inter_2025_03.py`)
2. Registrar com o decorator:

```python
from registry import registrar
from schemas import Fatura, Transacao
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
        transacoes=[...],
    )
```

3. Importar no `__init__.py`:

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
| `extract_text(path)` | Texto + linhas com posições (`x0`, `top`) + tabelas. Usa pdfplumber primário, pymupdf fallback para scans. |
| `extract_lines(path)` | Lista achatada de linhas visuais — `{page, top, words: [{text, x0}]}`. |

## Testes

```bash
# Rodar todos os testes
pytest

# Com cobertura
pytest --cov=.
```

Fixtures de PDFs e expected outputs vão em `tests/fixtures/`.
