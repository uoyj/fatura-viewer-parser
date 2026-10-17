# Handoff Notes — fatura-viewer

> Documento de handoff para um agente de IA trabalhar no projeto `fatura-viewer`.
> Última atualização: 2026-10-10

## Visão Geral

**fatura-viewer** é um parser de faturas de cartão de crédito (Itaú, Nubank, Sofisa, Mercado Pago) que extrai transações de PDFs, valida-as rigorosamente e oferece uma API HTTP (FastAPI) + frontend SPA para visualização, categorização e projeção de gastos.

- **Linguagem:** Python 3.12+
- **Framework:** FastAPI + Uvicorn
- **Frontend:** Alpine.js (SPA) + CSS puro
- **Extração PDF:** `pdfplumber` (primário) + `pymupdf` (fallback para scans)
- **Validação:** Equação do header + soma global das transações (tolerância R$0.01)
- **Testes:** pytest (marca `.slow` para testes que leem PDFs)
- **Deploy:** Docker (porta 8000) + docker-compose com volume para dados

## Estrutura de Diretórios

```
fatura-viewer/
├── AGENTS.md                    # Documentação de desenvolvimento (arquitetura, regras)
├── HANDOFF.md                   # ← Este arquivo
├── Dockerfile                   # Imagem Docker Python 3.12-slim
├── docker-compose.yml           # Serviço na porta 8000, volume `fatura-data`
├── .dockerignore                # Exclui data/, in/, __pycache__/
├── .gitignore                   # Python padrão + data/ + in/
│
├── api.py                       # API FastAPI — todos os endpoints
├── cli.py                       # CLI: parse, extract, parsers
├── registry.py                  # Decorator @registrar, get_parser()
├── schemas.py                   # Dataclasses: Fatura, Transacao
├── categorizer.py               # Categorização por palavra-chave (JSON editável)
├── comparativo.py               # Comparação mês-a-mês + detecção de anomalias
├── inferencia.py                # Inferência de banco pelos marcadores de texto
│
├── extractors/
│   ├── __init__.py
│   └── pdf_extractor.py         # Extração de texto/posições (pdfplumber + pymupdf)
│
├── parsers/
│   ├── __init__.py              # Importa todos os parsers (side-effect de registro)
│   ├── itau_2026_09.py          # Parser Itaú (layout 2026-09)
│   ├── nubank_2026_09.py        # Parser Nubank (layout 2026-09)
│   ├── sofisa_2026_09.py        # Parser Sofisa (layout 2026-09)
│   └── mercadopago_2026_09.py   # Parser Mercado Pago (layout 2026-09)
│
├── static/                      # Frontend SPA (Alpine.js)
│   ├── index.html               # Shell SPA com rotas cliente-side
│   ├── app.js                   # Lógica da aplicação (upload, viewer, categorias)
│   └── app.css                  # Estilos dark-themed
│
├── data/                        # Dados persistentes (gitignored)
│   ├── faturas.jsonl            # Faturas parseadas (formato JSONL)
│   ├── uploads/                 # PDFs enviados via API
│   ├── categorias.json          # Regras de categorização (seed → arquivo)
│   ├── overrides.json           # Overrides manuais por descrição normalizada
│   └── recorrentes.json         # Flag de gastos recorrentes
│
├── in/                          # PDFs de entrada para testes (gitignored)
│   ├── Fatura.pdf               # Sofisa (fixture principal)
│   ├── Fatura_Itau_*.pdf        # Itaú (2 faturas)
│   └── Nubank_*.pdf             # Nubank (2 faturas)
│
└── tests/
    ├── conftest.py              # Importa parsers + fixture extract_cached (memoizado)
    ├── test_api.py              # Testes da API FastAPI (slow, usa tmp_path)
    ├── test_categorizer.py      # Testes de categorização (unit + integração)
    ├── test_personalizacao.py   # Testes de overrides/recorrentes/regras (API)
    ├── test_consolidado.py      # Testes de projeção de gastos
    ├── test_inferencia.py       # Testes de inferência de banco
    ├── test_itau_2026_09.py     # Testes parser Itaú (golden + apenas)
    ├── test_nubank_2026_09.py   # Testes parser Nubank (golden + apenas)
    ├── test_sofisa_2026_09.py   # Testes parser Sofisa (golden)
    ├── test_sofisa_parc_data.py # Testes de parcelamento por data
    └── fixtures/                # JSON golden (expected output do parse)
```

## Arquitetura

### Pipeline (fluxo principal)

```
PDF input → extract_text() → Parser (banco/versão) → Fatura → _validate() → output
```

1. **Extractor** (`extractors/pdf_extractor.py`): Extrai texto e posições das palavras das páginas do PDF. Usa `pdfplumber` como primário; se uma página vier vazia (scan), faz fallback para `pymupdf`. **NUNCA contém lógica de interpretação de fatura.**

2. **Registry** (`registry.py`): Mapeia `(banco, versão) → função parser`. Parsers se registram via decorator `@registrar("banco", "versão")`. `get_parser(banco, "latest")` resolve para a versão mais recente.

3. **Parsers** (`parsers/<banco>_<versão>.py`): Recebem o output do extractor (`dict` com `"pages"`), não leem PDF diretamente. Cada parser:
   - Extrai o header (datas, totais, saldos)
   - Extrai transações (data, descrição, valor, parcelas, cartão)
   - Valida: equação do header + soma global das transações

4. **Schemas** (`schemas.py`): `Fatura` e `Transacao` como dataclasses. Valores em `Decimal` (string no JSON), datas em ISO.

5. **Categorização** (`categorizer.py`): Matching por substring (case-insensitive). Precedência: `override global > regras > "Outros"` (implicit). Regras e overrides são arquivos JSON editáveis em `data/`.

## Convenções de Código

### Valores monetários
- **Decimal, nunca float.** Sempre parsear como string → `Decimal`.
- Serializados como string no JSON (ex: `"74.80"`).
- Formato brasileiro: `1.234,56` → `1234.56`.

### Transações
- **valor positivo = débito (compra)**, **negativo = crédito (estorno/pagamento)**.
- `id` por transação: índice na lista dentro da fatura.
- `cartao`: tag do cartão — Itaú: últimos 4 dígitos (`"3620"`); Nubank: `"·5065"`; Sofisa: mascarado (`"4563**.******.9219"`).

### Datas
- Formato ISO: `YYYY-MM-DD`.
- Datas no PDF: `DD/MM` (Itaú/Sofisa) ou `DD MMM` (Nubank, ex: `"12 AGO"`).
- Ano referência: ano do fechamento da fatura.

### Validação rigorosa (não relaxar)
- Equação do header: `saldo_anterior - créditos + débitos + encargos = total_a_pagar`
- Soma global das transações: débitos e créditos devem bater com o header.
- Tolerância: `Decimal("0.01")`.
- Se falhar, levanta `ParserError` com mensagem acionável.

### Regras de token (obrigatório para quem for usar este projeto)
1. NUNCA rodar `cli.py extract` completo nem colar página de PDF bruta no chat.
2. NÃO ler arquivos inteiros >10KB (app.js, api.py, jsonl). Usar grep.
3. NÃO colar traceback inteiro — só a linha do erro + warning relevante (~10 linhas).
4. Diferenças mínimas por vez; não re-rodar parse em loop "para confirmar".

## Parsers Específicos

### Itaú (layout 2026-09)
- **2026-09**: Layout Platinum. 2 colunas (esq x0≈133, dir x0≈351, split em 330).
- Duas datas por linha física (2 lançamentos por linha).
- Token `NN/MM` após a data = **parcela** (não data).
- Zona: `"Lançamentos: compras e saques"` → `"Limites de crédito"`.
- Após `"Compras parceladas - próximas faturas"`: coluna direita = futuras (ignorar).
- Encargos: fora da tabela de transações (entra só na equação do header).

### Nubank (layout 2026-09)
- **Page 1**: vencimento + período. **Page 4**: RESUMO (saldo anterior, créditos, débitos, outros, total).
- Equação: `saldo - créditos + débitos + outros = total`.
- Cartões: `"•8188"` (bullets + 4 dígitos).
- Datas: `"12 AGO"` (DD + MMM maiúsculo).
- Parcelamento: `"Parcela 2/3"` embutido na descrição.

### Sofisa (layout 2026-09)
- **Page 1**: header. **Pages 2+**: transações.
- Transações extraídas por colunas (x-positions), não por linha inteira.
- Equação via regex único: `R$4.967,42 -R$5.074,81 +R$6.834,94 =R$ 6.727,55`.
- Parcela `Parc.10/1` truncada → recalculada pela data da compra.

### Mercado Pago (layout 2026-09)
- Parser registrado mas sem fixture de teste dedicado. Use `python cli.py parsers` para confirmar.

## API Endpoints (FastAPI)

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| POST | `/api/faturas` | Upload PDF + parse + categorização + persiste em JSONL |
| GET | `/api/faturas` | Lista resumida de faturas |
| GET | `/api/faturas/{id}` | Fatura completa (com overrides aplicados) |
| DELETE | `/api/faturas/{id}` | Remove JSONL + PDF |
| GET | `/api/parsers` | Lista parsers registrados |
| POST | `/api/inferir-banco` | Sugere banco pelo PDF |
| GET | `/api/categorias` | Regras de categorização |
| PUT | `/api/categorias` | Substitui regras (422 se inválido) |
| GET | `/api/overrides` | Overrides manuais globais |
| PUT | `/api/overrides/{desc}` | Grava override por descrição normalizada |
| DELETE | `/api/overrides/{desc}` | Remove override (404 se não existir) |
| GET | `/api/recorrentes` | Gastos recorrentes marcados |
| PUT | `/api/recorrentes/{desc}` | Marca como recorrente |
| DELETE | `/api/recorrentes/{desc}` | Desmarca (404 se não estiver marcado) |
| POST | `/api/recategorizar` | Re-aplica regras em todas as faturas |
| GET | `/api/comparativo` | Comparação mês-a-mês + anomalias |
| GET | `/api/consolidado` | Histórico + projeção de gastos |

### SPA Fallback
Rotas não-`/api/*` caem no `index.html` do frontend (history routing).

## Sistema de Categorização

### Precedência
1. **Override manual global** (`data/overrides.json`) — chave = `normalizar_descricao(descricao)`
2. **Regras** (`data/categorias.json`) — primeira que casar vence (substring, case-insensitive)
3. **"Outros"** — fallback implícito (nunca é uma regra)

### SEED_REGRAS (criado automaticamente se `categorias.json` não existe)
- Pagamento da fatura, Estorno/Ajuste, Academia/Wellness, Farmacia, Veterinário, Saúde/Podologia, Transporte/Apps, Restaurante/Café, Bar, Supermercado/Mercado, Contas/Serviços, Compras online.

### Overrides aplicados na LEITURA
- Quando um override é salvo via API, ele vale **imediatamente** no `GET /faturas/{id}` e no `POST /faturas` response — sem precisar rodar `POST /recategorizar`.
- O JSONL mantém o resultado **puro do parse**. Overrides são aplicados em runtime na resposta.

### Recorrentes (flag global)
- `data/recorrentes.json`: `{descricao_normalizada: true}`
- **Ortogonal** a categoria e parcela. Um gasto recorrente (ex: Spotify, Apple Bill).
- A flag **NÃO é injetada** no payload das transações — o cliente consulta `GET /recorrentes` e cruza localmente.

## Comparativo e Projeção (`comparativo.py`)

- **Total do mês** = soma de débitos (valor > 0). Pagamentos/estornos excluídos.
- **Anomalias**: mês > 1.5x mediana categorias (diff ≥ R$50) ou transação > 2.5x mediana da descrição (≥ R$100).
- **Projeção futura**: baseada em parcelas restantes (`parcela_atual + k / parcela_total`) e recorrentes (até 12 meses).

## Dependências

### Python (instaladas via pip no Dockerfile)
- `fastapi>=0.115`
- `uvicorn[standard]>=0.30`
- `python-multipart` (upload de arquivos)
- `httpx`
- `pdfplumber>=0.11.0`
- `pymupdf>=1.28.0`
- `pytest` (dev)

### Frontend (bundled)
- Alpine.js (via CDN, `x-data`)
- CSS customizado (dark theme)

## Como Rodar

### Desenvolvimento local
```bash
# Instalar dependências
pip install -r requirements-dev.txt 2>/dev/null || pip install fastapi uvicorn python-multipart httpx pdfplumber pymupdf pytest

# Rodar API (modo dev com reload)
uvicorn api:app --reload --port 8000

# Parsear um PDF
python cli.py parse in/NomeFatura.pdf --banco itau

# Listar parsers
python cli.py parsers

# Testes rápidos (sem PDFs)
pytest -m "not slow" -q

# Suíte completa (com PDFs — mais lento)
pytest -q
```

### Docker
```bash
docker compose up -d --build

# Ver logs
docker logs fatura-viewer

# Healthcheck
curl -f http://localhost:8000/api/parsers
```

## Testes

### Estratégia de testes
- **`conftest.py`**: Importa `parsers` (side-effect de registro no registry). Fixture `extract_cached` memoiza extração de PDF por sessão.
- **Golden tests**: `test_golden` compara output exato do parser com JSON em `tests/fixtures/*.expected.json`.
- **Slow markers**: Testes que leem PDFs do disco são marcados `@pytest.mark.slow`. Executar `pytest -m "not slow"` para ciclo rápido.

### Isolação
- **NUNCA** os testes tocam `data/` real. Todos usam `monkeypatch` para redirecionar paths para `tmp_path`.
- Uploads de PDF vão para `tmp_path/uploads` (não `data/uploads`).

### Cobertura
- `test_sofisa_2026_09.py`: 96 transações, 3 cartões, equação do header, soma global.
- `test_itau_2026_09.py`: 2 faturas, header, encargos, parcelas, limpeza de sufixo.
- `test_nubank_2026_09.py`: 2 faturas, header, "outros lançamentos", cartões.
- `test_api.py`: CRUD de faturas, overrides aplicados na leitura, 422 para erros.
- `test_categorizer.py`: matching, seed, idempotência, somas por categoria.
- `test_personalizacao.py`: CRUD completo de overrides/recorrentes, validação.
- `test_consolidado.py`: projeção de gastos futuros.
- `test_inferencia.py`: detecção de banco por marcadores.
- `test_sofisa_parc_data.py`: cálculo de parcela pela data da compra.

## Dados do Projeto

### faturas.jsonl (formato)
Cada linha é um JSON com:
```json
{
  "id": "uuid",
  "criado_em": "ISO timestamp",
  "banco": "itau|nubank|sofisa|mercadopago",
  "modelo": "2026-09",
  "payload": {
    "banco": "...",
    "modelo": "...",
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
        "parcela_atual": 10,
        "parcela_total": 10,
        "moeda": "BRL",
        "categoria": "Compras online",
        "cartao": "4563**.******.9219"
      }
    ]
  }
}
```

## Pontos-Chave para Manutenção

### Adicionar um novo banco/parser
1. Criar `parsers/<banco>_2026_09.py` com função `@registrar("<banco>", "2026-09")`.
2. Adicionar import no `parsers/__init__.py`.
3. Adicionar marcador no `inferencia.py` (seguindo o padrão).
4. Adicionar regras no `categorizer.py` SEED_REGRAS se necessário.
5. Criar fixture e teste em `tests/`.

### Adicionar uma nova versão de layout
1. Criar `parsers/<banco>_<nova_versao>.py` com `@registrar("<banco>", "<nova_versao>")`.
2. `get_parser(banco, "latest")` resolve automaticamente para a versão mais recente.

### Modificar categorização
- Editar `data/categorias.json` (regras) ou `data/overrides.json` (overrides).
- Regras: `{"categoria": "X", "padroes": ["Y"]}` — primeira correspondência vence.
- Validação: categoria não-vazia, pelo menos 1 padrão, padrões não-vazios.
- Para aplicar mudanças em faturas existentes: `POST /api/recategorizar`.

## Problemas Conhecidos e Limitações

1. **PDFs escaneados (imagens)**: `pdf_extractor.py` extrai posições com pymupdf, mas **não faz OCR**. Texto extraído será vazio.
2. **Mercado Pago**: Parser registrado mas sem fixture de teste dedicado.
3. **Frontend**: SPA com Alpine.js — requer JavaScript. Sem SSR.
4. **Auth**: API sem autenticação — apenas para uso doméstico/local.
5. **Porta única**: Não há multi-usuário.

## Comandos Úteis

```bash
# Debug rápido de extração (sem parse)
python cli.py extract in/Fatura.pdf --compact

# Ver estrutura do extractor output
python -c "
from extractors.pdf_extractor import extract_text
import json
print(json.dumps(extract_text('in/Fatura.pdf')['pages'][0], indent=2)[:2000])
"

# Forçar categorização com regras customizadas
python -c "
from categorizer import carregar_regras, categorizar
from registry import get_parser
from extractors.pdf_extractor import extract_text
rules = carregar_regras()
fatura = get_parser('sofisa', '2026-09')(extract_text('in/Fatura.pdf'))
categorizar(fatura, regras=rules)
print(f'{len(fatura.transacoes)} transações categorizadas')
"

# Verificar parsers registrados
python -c "from registry import parsers_registrados; print(parsers_registrados())"
```