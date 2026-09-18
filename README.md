# fatura-viewer

API + parsers de faturas de cartão de crédito em **Python 3.12**.

## Visão geral

Extrai transações de faturas de cartão de crédito a partir de PDFs e devolve JSON estruturado. A arquitetura é modular:

```
fatura-viewer/
├── api.py               # FastAPI: faturas + categorias/overrides + StaticFiles mount
├── static/index.html    # Frontend SPA (HTML+CSS+JS inline, sem CDN/framework)
├── categorizer.py       # Categorização determinística: regras em JSON + overrides globais
├── data/categorias.json # Dicionário editável de regras (seed automático na 1ª execução)
├── data/overrides.json  # Overrides manuais globais (criado sob demanda)
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
| `DELETE` | `/faturas/{fatura_id}` | Remove a fatura do JSONL + apaga o PDF |
| `GET` | `/parsers` | Lista parsers registrados: `{banco: [versoes]}` |
| `GET` | `/categorias` | Regras de categorização (`data/categorias.json`) |
| `PUT` | `/categorias` | Substitui as regras (valida; **422** `{"erro": ...}` se inválido) |
| `GET` | `/overrides` | Overrides manuais globais: `{descricao_normalizada: categoria}` |
| `PUT` | `/overrides/{desc}` | Grava override da descrição normalizada (**422** se `categoria` vazia) |
| `DELETE` | `/overrides/{desc}` | Remove override (**404** se não existir) |
| `POST` | `/recategorizar` | Re-aplica regras + overrides em todas as faturas salvas |

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

#### Categorias e overrides (personalização)

**Precedência:** override manual global > regras do dicionário > `"Outros"`.

- **Dicionário** (`data/categorias.json`) — regras de substring, primeira que casa vence. Criado automaticamente na primeira execução com o seed da constante `SEED_REGRAS` (12 categorias); depois disso **o arquivo é a fonte da verdade**.
- **Override manual global** (`data/overrides.json`) — chave = descrição normalizada (`upper` + `strip` + espaços múltiplos colapsados em 1). Vale **imediatamente** na leitura (`GET /faturas/{id}` e resposta do `POST /faturas`), sem precisar de `POST /recategorizar`. Já as **REGRAS** só valem em faturas novas até você rodar `POST /recategorizar`.

```bash
# Regras (dicionário)
curl http://127.0.0.1:8000/categorias
curl -X PUT http://127.0.0.1:8000/categorias \
  -H 'Content-Type: application/json' \
  -d '{"regras":[{"categoria":"Supermercado/Mercado","padroes":["MERCADOLIVRE","PANIFICADORA"]}]}'

# Overrides — a descrição vai URL-encoded no path (espaço = %20)
curl -X PUT "http://127.0.0.1:8000/overrides/EC%20*STERILAIR" \
  -H 'Content-Type: application/json' -d '{"categoria":"Compras online"}'
curl http://127.0.0.1:8000/overrides
curl -X DELETE "http://127.0.0.1:8000/overrides/ec%20*sterilair"   # chave é normalizada

# Reaplica regras + overrides em todas as faturas do JSONL
curl -X POST http://127.0.0.1:8000/recategorizar
# → {"recategorizadas": 3}
```

Formatos dos arquivos:

```jsonc
// data/categorias.json
{"regras": [{"categoria": "Supermercado/Mercado", "padroes": ["MERCADOLIVRE", "PANIFICADORA"]}]}

// data/overrides.json
{"MERCADO*MERCADOLIVRE": "Supermercado/Mercado"}
```

`PUT /categorias` aceita `{"regras": [...]}` ou a lista pura, e valida: `categoria` string não-vazia, `padroes` lista com ≥1 string não-vazia (**422** `{"erro": ...}` caso contrário — nada é gravado).
`POST /recategorizar` **regrava `data/faturas.jsonl`** — o processo precisa de permissão de escrita nesse arquivo.

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
      "id": 0,
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
      "id": 1,
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

`"id"` é o índice da transação na fatura — atribuído na serialização (`fatura_para_dict`), então os parsers não precisam gerá-lo.

Com `--categorizar` (ou via API), `categoria` é preenchido por **override manual > regra de palavra-chave** (fallback `"Outros"`):

```
Pagamento da fatura, Estorno/Ajuste, Academia/Wellness, Farmacia,
Veterinario, Saude/Podologia, Transporte/Apps, Restaurante/Cafe,
Bar, Supermercado/Mercado, Compras online, Outros
```

### Categorização determinística

`categorizer.py` — sem ML, regras de substring (case-insensitive), primeira que casa vence. As regras vêm de `data/categorias.json` (editável via `PUT /categorias` ou direto no arquivo); `SEED_REGRAS` só é usado uma vez, para criar o arquivo quando ele não existe.

```bash
python cli.py parse in/Fatura.pdf --banco sofisa --versao 2026-09 --categorizar
```

Seed (categorias iniciais gravadas em `data/categorias.json`):

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
| Contas/Servicos | `ESTAB. PARCEL. DE COMPRA` |
| Compras online | `AMAZON`, `SHOPEE`, `SHEIN`, `STERILAIR` |
| Outros | (fallback — **não** é uma regra) |

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
- **Transacao.id** — índice da transação na fatura (inteiro estável), atribuído na serialização
- **Transacao.valor** — positivo = débito (compra), negativo = crédito (estorno/pagamento)
- **Transacao.cartao** — número mascarado (`"4563**.******.9219"`), `""` se desconhecido
- **categoria** — `null` sem categorizador; preenchido via `categorizer.categorizar()` (override manual > regras > `"Outros"`)

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

# Um módulo específico
pytest tests/test_personalizacao.py -x

# Com cobertura
pytest --cov=.
```

Fixtures de PDFs e expected outputs vão em `tests/fixtures/`.

> **Mudou o output de `fatura_para_dict`? Regenere os fixtures** rodando o parse real nos PDFs de `in/` e salvando o JSON aprovado (nunca edite o `.expected.json` à mão). `tests/fixtures/*.expected.json` são comparados por igualdade exata em `TestJSONConsistency`.

Os testes **nunca** tocam o `data/` real: os paths (`faturas.jsonl`, `categorias.json`, `overrides.json`) são redirecionados para `tmp_path` via `monkeypatch`.
