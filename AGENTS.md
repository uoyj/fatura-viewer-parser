# AGENTS.md — fatura-viewer

## Arquitetura (não re-explorar)
- `extractors/pdf_extractor.py` → só extrai texto/posições (pdfplumber + fallback pymupdf). SEM lógica de fatura.
- `parsers/&lt;banco&gt;_&lt;layout&gt;.py` → interpreta. Recebem `extract_text()` (dict com `pages[].lines[].words[]`), retornam `Fatura` (schemas.py).
- `registry.py` → `@registrar("banco", "versao")`. `cli.py` registra via `from parsers import *`.
- Fluxo: `extract_text(path)` → `get_parser(banco)` → `_extract_header` → `_extract_transacoes` → `_validate`.
- Validação padrão: equação `saldo_anterior - creditos + debitos + encargos/outros = total_a_pagar` + soma global das transações (`TOLERANCIA = 0.01`).
- Convenções: Decimal (nunca float), data ISO, débito positivo / crédito negativo, JSON serializa Decimal/date como string.

## Comandos
- Parse: `python cli.py parse in/&lt;arquivo&gt;.pdf --banco &lt;itau|nubank|sofisa|mercadopago&gt;`
- Teste cirúrgico: `pytest tests/test_&lt;banco&gt;_2026_09.py -x -q`
- Suíte completa SÓ ao final: `pytest -q`

## REGRAS DE TOKEN (obrigatório)
1. NUNCA rodar `cli.py extract` completo nem colar página de PDF bruta no chat. Usar `grep -ni "&lt;marcador&gt;"` ou `sed -n 'X,Yp'` em trechos.
2. NÃO ler arquivos inteiros &gt;10KB com cat/Read. `app.js`, `api.py`, `faturas.jsonl`: usar grep para localizar, ler só o trecho.
3. NÃO colar traceback/log inteiro. Colar só a linha do erro + warning relevante (máx. ~10 linhas).
4. Máx. 1 re-leitura de arquivo por sessão por arquivo; anotar o que precisa antes de ler.
5. Uma hipótese por vez; não re-rodar parse em loop "para confirmar".
6. Ao editar: diff mínimo, sem reformatar/reordenar arquivo.

## Conhecimento acumulado (layout 2026-09)
- Itaú: 2 colunas (x0≈133 esq / x0≈351 dir, split em 330). Uma linha física pode ter 2 lançamentos. Token NN/MM após a data = parcela (não data). Zona começa em "Lançamentos: compras e saques" e pode atravessar páginas; termina em "Limites de crédito". Após "Compras parceladas - próximas faturas": col direita e qualquer linha com token NN/MM = futura (ignorar). Encargos ficam FORA da soma de transações (entram só na equação do header).
- Nubank: equação é `saldo - creditos + debitos + outros = total`. Se "Outros lançamentos" explícito ausente mas `debitos` explícito existe, derivar: `outros = total - saldo + creditos - debitos`.
- PDFs Itaú novos vêm com espaços ausentes/quebrados na extração (`Totaldafaturaanterior`, `Lançamentos:comprasesaques`) → usar matching tolerante (fold/remover espaços), não regex literal com espaços.

## Ao corrigir parser
- Manter validação estrita. Se um PDF novo quebrar, a causa é quase sempre: (a) espaçamento alterado na extração, (b) campo ausente no header (usar fallback + warning, nunca assumir cegamente), (c) seção nova fora da zona conhecida.
- Adicionar/ajustar fixture em `tests/fixtures/` se o layout mudou de verdade; senão, tratar como variação do layout atual.
- Nunca modificar `extractors/` para consertar parser específico de banco.