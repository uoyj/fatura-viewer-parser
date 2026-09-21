"""
Testes de personalização global de categorias (Tier 3).

Padrão da suíte: cenários que diferem só nos dados de entrada usam
@pytest.mark.parametrize; LISTAS de bodies de validação (mesmo caminho de
código, valores parecidos) rodam em loop dentro de UM teste, com a mensagem
do assert nomeando o caso que falhou — isso mantém a contagem de testes baixa
sem perder nenhum caso.

Cobre:
- normalizar_descricao (upper/strip/colapso de espaços)
- seed: data/categorias.json criado UMA vez; depois o arquivo é a fonte da verdade
- precedência: override manual global > regras > "Outros"
- API: GET/PUT /categorias, GET/PUT/DELETE /overrides, POST /recategorizar
- API: GET/PUT/DELETE /recorrentes (flag global de gasto recorrente), incluindo
  a independência em relação a overrides/categorias e o fato de a flag NÃO ser
  injetada no payload das transações

ISOLAMENTO OBRIGATÓRIO: nenhum teste toca o data/ real do usuário.
O fixture `isolado` monkeypatcha categorias.json, overrides.json,
recorrentes.json E faturas.jsonl (api + categorizer) para um tmp_path que o
pytest apaga.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

# Importar o módulo api para monkeypatch nos paths
import api
import categorizer
from api import app
from categorizer import (
    SEED_REGRAS,
    aplicar_overrides,
    carregar_overrides,
    carregar_regras,
    categorizar,
    normalizar_descricao,
    salvar_overrides,
)
from schemas import Fatura, Transacao


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolado(tmp_path, monkeypatch):
    """Redireciona TODOS os paths de data/ para tmp_path (nada toca data/ real)."""
    # api.py
    monkeypatch.setattr(api, "DATA_DIR", tmp_path)
    monkeypatch.setattr(api, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(api, "JSONL_PATH", tmp_path / "faturas.jsonl")
    monkeypatch.setattr(api, "CATEGORIAS_PATH", tmp_path / "categorias.json")
    monkeypatch.setattr(api, "OVERRIDES_PATH", tmp_path / "overrides.json")
    monkeypatch.setattr(api, "RECORRENTES_PATH", tmp_path / "recorrentes.json")

    # categorizer.py (usado direto nos testes unitários)
    monkeypatch.setattr(categorizer, "DATA_DIR", tmp_path)
    monkeypatch.setattr(categorizer, "CATEGORIAS_PATH", tmp_path / "categorias.json")
    monkeypatch.setattr(categorizer, "OVERRIDES_PATH", tmp_path / "overrides.json")
    monkeypatch.setattr(categorizer, "RECORRENTES_PATH", tmp_path / "recorrentes.json")

    (tmp_path / "uploads").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def client(isolado):
    return TestClient(app)


def _transacao(descricao: str, valor: str = "10.00", categoria=None) -> dict:
    return {
        "data": "2026-09-01",
        "descricao": descricao,
        "valor": valor,
        "parcela_atual": None,
        "parcela_total": None,
        "moeda": "BRL",
        "categoria": categoria,
        "cartao": "1234",
    }


def _payload(transacoes: list[dict]) -> dict:
    return {
        "banco": "teste",
        "modelo": "2026-09",
        "fechamento": "2026-09-15",
        "vencimento": "2026-09-20",
        "total_a_pagar": "30.00",
        "pagamento_minimo": "3.00",
        "transacoes": transacoes,
    }


def _fatura(descricoes: list[str]) -> Fatura:
    return Fatura(
        banco="teste",
        modelo="2026-09",
        fechamento=date(2026, 9, 15),
        vencimento=date(2026, 9, 20),
        total_a_pagar=Decimal("30.00"),
        pagamento_minimo=Decimal("3.00"),
        transacoes=[
            Transacao(data=date(2026, 9, 1), descricao=d, valor=Decimal("10.00"))
            for d in descricoes
        ],
    )


def _gravar_jsonl(path, payloads: list[dict]) -> None:
    """Grava registros SEM id de transação (testa também a tolerância do GET)."""
    with open(path, "w", encoding="utf-8") as f:
        for i, p in enumerate(payloads):
            f.write(json.dumps({
                "id": f"fat-{i}",
                "criado_em": "2026-09-18T00:00:00+00:00",
                "banco": p["banco"],
                "modelo": p["modelo"],
                "payload": p,
            }, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 1. Normalização (todos os casos originais num único teste nomeado)
# ---------------------------------------------------------------------------

def test_normalizar_descricao():
    """
    upper + strip + colapso de espaços/tabs/newlines.
    Cobre também overrides com ruído de caixa/espaços.
    """
    casos = [
        # aplica upper + strip nas três variações da mesma descrição
        ("Mercado*MercadoLivre  ", "MERCADO*MERCADOLIVRE"),
        ("  mercado*mercadolivre", "MERCADO*MERCADOLIVRE"),
        ("MERCADO*MERCADOLIVRE", "MERCADO*MERCADOLIVRE"),
        # colapsa espaços múltiplos, tabs e newlines
        ("MERCADO*MERCADO    LIVRE", "MERCADO*MERCADO LIVRE"),
        ("\tEc \n STERILAIR\t", "EC STERILAIR"),
        # vazio
        ("", ""),
        ("    ", ""),
    ]
    for entrada, esperado in casos:
        assert normalizar_descricao(entrada) == esperado, f"falhou em {entrada!r}"


# ---------------------------------------------------------------------------
# 2. Seed do dicionário editável
# ---------------------------------------------------------------------------

def test_seed_cria_e_preserva(isolado):
    """
    carregar_regras() cria data/categorias.json quando ausente (com o seed) e,
    depois disso, o ARQUIVO é a fonte da verdade — nunca é sobrescrito.
    'Outros' é fallback implícito, não uma regra.
    """
    path = isolado / "categorias.json"
    assert not path.exists()

    regras = carregar_regras()
    assert path.exists(), "seed deveria criar data/categorias.json"
    assert regras, "seed não pode ser vazio"
    assert json.loads(path.read_text(encoding="utf-8")) == {"regras": regras}
    assert [r["categoria"] for r in regras] == [r["categoria"] for r in SEED_REGRAS]
    assert all(r["categoria"] != "Outros" for r in SEED_REGRAS)

    # overrides ausentes → vazio (nenhum arquivo criado)
    assert carregar_overrides() == {}

    # com o arquivo existente, o segundo carregamento lê do ARQUIVO
    custom = {"regras": [{"categoria": "So Minha", "padroes": ["XPTO"]}]}
    path.write_text(json.dumps(custom), encoding="utf-8")
    regras = carregar_regras()
    assert regras == custom["regras"]
    assert regras != SEED_REGRAS
    assert json.loads(path.read_text(encoding="utf-8")) == custom


# ---------------------------------------------------------------------------
# 3. Precedência: override > regras > "Outros"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cenario", [
    "regra_sem_override",
    "override_vence_regra",
])
def test_precedencia(isolado, cenario):
    if cenario == "regra_sem_override":
        # Comportamento original preservado: regra casa → categoria da regra;
        # sem regra → "Outros" (fallback implícito).
        fatura = categorizar(_fatura(
            ["PANIFICADORA E CO", "AMAZONMKTPLC*LHCOMPROD", "DORACIGRINGS"]))
        assert [t.categoria for t in fatura.transacoes] == [
            "Supermercado/Mercado", "Compras online", "Outros",
        ]

    else:
        # A regra de seed (AMAZON → "Compras online") casa a descrição...
        fatura = categorizar(_fatura(["AMAZONMKTPLC*"]))
        assert fatura.transacoes[0].categoria == "Compras online"

        # ...mas o override global da MESMA descrição normalizada vence.
        salvar_overrides({normalizar_descricao("AMAZONMKTPLC*"): "Teste"})
        fatura = categorizar(_fatura(["AMAZONMKTPLC*"]))
        assert fatura.transacoes[0].categoria == "Teste"

        # Override casa mesmo com ruído de caixa/espaços na descrição.
        salvar_overrides({normalizar_descricao("amazonmktplc*"): "Assinaturas"})
        fatura = categorizar(_fatura(["  AmazonMktplc*  "]))
        assert fatura.transacoes[0].categoria == "Assinaturas"

        # Override vence até quando a regra que casa está em 1º lugar.
        from categorizer import salvar_regras
        salvar_regras([
            {"categoria": "Primeira", "padroes": ["PANIFICADORA"]},
            {"categoria": "Segunda", "padroes": ["COISA"]},
        ])
        salvar_overrides({normalizar_descricao("PANIFICADORA E CO"): "Manual"})
        fatura = categorizar(_fatura(["PANIFICADORA E CO", "COISA NADA"]))
        assert [t.categoria for t in fatura.transacoes] == ["Manual", "Segunda"]


# ---------------------------------------------------------------------------
# 3b. Leitura: overrides aplicados num payload já serializado
# ---------------------------------------------------------------------------

def test_aplicar_overrides(isolado):
    """
    aplicar_overrides(payload) — o ensinamento vale na LEITURA (GET/upload):
    sem overrides não mexe em nada; com override, muda todas as linhas da
    mesma chave normalizada e preserva os demais campos.
    """
    # --- sem overrides: mesmo objeto, nada muda -----------------------------
    payload = _payload([_transacao("QUALQUER COISA", categoria="X")])
    resultado = aplicar_overrides(payload)
    assert resultado is payload            # mesmo objeto, sem cópia
    assert payload["transacoes"][0]["categoria"] == "X"

    # payload sem transações (ou sem a chave) passa intacto
    assert aplicar_overrides({"banco": "x"}) == {"banco": "x"}
    assert aplicar_overrides({"banco": "x", "transacoes": []}) == {
        "banco": "x", "transacoes": []}

    # overrides por parâmetro: evita reler o arquivo (1x por request)
    payload = _payload([_transacao("AMAZON BR", categoria="Compras online")])
    aplicar_overrides(payload, {"AMAZON BR": "Teste"})
    assert payload["transacoes"][0]["categoria"] == "Teste"

    # --- com override: todas as linhas da mesma chave, resto intacto --------
    payload = _payload([
        _transacao("Compra a Vista NONO CAFE", categoria="Restaurante/Cafe"),
        _transacao("  compra a vista   nono cafe ", categoria="Restaurante/Cafe"),
        _transacao("AMAZON BR", categoria="Compras online"),
    ])
    antes = json.loads(json.dumps(payload))
    salvar_overrides({normalizar_descricao("Compra a Vista NONO CAFE"): "Teste"})

    aplicar_overrides(payload)

    assert [t["categoria"] for t in payload["transacoes"]] == [
        "Teste", "Teste", "Compras online",
    ]
    t, t_antes = payload["transacoes"][0], antes["transacoes"][0]
    for campo in ("data", "descricao", "valor", "cartao", "moeda",
                  "parcela_atual", "parcela_total"):
        assert t[campo] == t_antes[campo], campo
    assert payload["banco"] == antes["banco"]


# ---------------------------------------------------------------------------
# 4. API — /categorias
# ---------------------------------------------------------------------------

def test_api_categorias(client, isolado):
    """GET cria o seed; PUT válido grava; PUT inválido não grava nada."""
    resp = client.get("/api/categorias")
    assert resp.status_code == 200
    assert resp.json()["regras"] == SEED_REGRAS
    assert (isolado / "categorias.json").exists()

    novas = [{"categoria": "Nova Cat", "padroes": ["NOVO PADRAO"]}]
    resp = client.put("/api/categorias", json={"regras": novas})
    assert resp.status_code == 200
    assert resp.json()["regras"] == novas
    assert client.get("/api/categorias").json()["regras"] == novas
    # gravou no tmp_path (nunca no data/ real)
    assert json.loads((isolado / "categorias.json").read_text(encoding="utf-8")) == {
        "regras": novas}

    # inválido → 422, seed/anterior intacto
    client.put("/api/categorias", json={"regras": [{"categoria": "X", "padroes": []}]})
    assert json.loads((isolado / "categorias.json").read_text(encoding="utf-8")) == {
        "regras": novas}


def test_api_categorias_invalido_422(client, isolado):
    """
    Um loop por body inválido (a mensagem do assert nomeia o caso). Cada ramo
    distinto de validação está representado; nenhum body é gravado.
    """
    bodies = [
        {"regras": "nao é lista"},                                  # não é lista
        {"regras": []},                                             # vazio
        {"nao_tem_regras": []},                                     # chave ausente
        {"regras": ["nao é objeto"]},                               # item não-dict
        {"regras": [{"categoria": "", "padroes": ["X"]}]},           # categoria vazia
        {"regras": [{"categoria": "   ", "padroes": ["X"]}]},        # categoria só espaços
        {"regras": [{"categoria": "X", "padroes": []}]},             # padrões vazios
        {"regras": [{"categoria": "X"}]},                            # padrões ausentes
        {"regras": [{"categoria": "X", "padroes": "X"}]},            # padrões não-lista
        {"regras": [{"categoria": "X", "padroes": ["  "]}]},         # padrão só espaços
        {"regras": [{"categoria": "X", "padroes": [123]}]},          # padrão não-str
    ]
    for body in bodies:
        resp = client.put("/api/categorias", json=body)
        assert resp.status_code == 422, f"body aceito indevidamente: {body}"
        assert "erro" in resp.json()
        assert not (isolado / "categorias.json").exists(), f"gravou com: {body}"

    # JSON quebrado no corpo (content= bruto, não json=)
    resp = client.put("/api/categorias", content="{isso nao é json",
                      headers={"Content-Type": "application/json"})
    assert resp.status_code == 422
    assert "erro" in resp.json()


# ---------------------------------------------------------------------------
# 5. API — /overrides
# ---------------------------------------------------------------------------

def test_api_overrides_crud(client, isolado):
    """
    PUT normaliza a chave, GET lista, DELETE é case-insensitive (404 quando não
    existe). Cada PUT é incremental — as asserções refletem o estado acumulado.
    """
    resp = client.put("/api/overrides/AmazonMktplc*LHComProd", json={"categoria": "Compras online"})
    assert resp.status_code == 200
    assert resp.json() == {"chave": "AMAZONMKTPLC*LHCOMPROD", "categoria": "Compras online"}
    assert client.get("/api/overrides").json() == {"AMAZONMKTPLC*LHCOMPROD": "Compras online"}
    assert json.loads((isolado / "overrides.json").read_text(encoding="utf-8")) == {
        "AMAZONMKTPLC*LHCOMPROD": "Compras online"}

    # normaliza espaços no path
    resp = client.put("/api/overrides/99FOOD%20%20*COLHERADA", json={"categoria": "Comida"})
    assert resp.status_code == 200
    assert resp.json()["chave"] == "99FOOD *COLHERADA"
    assert client.get("/api/overrides").json() == {
        "AMAZONMKTPLC*LHCOMPROD": "Compras online",
        "99FOOD *COLHERADA": "Comida",
    }

    # DELETE é case-insensitive e devolve a chave normalizada
    resp = client.delete("/api/overrides/99food%20*colherada")
    assert resp.status_code == 200
    assert resp.json()["chave"] == "99FOOD *COLHERADA"
    assert client.get("/api/overrides").json() == {"AMAZONMKTPLC*LHCOMPROD": "Compras online"}

    # mesmo caminho com caixa mista no PUT e minúscula no DELETE
    assert client.put("/api/overrides/MERCADO*MERCADOLIVRE",
                      json={"categoria": "Mercado"}).status_code == 200
    assert client.get("/api/overrides").json()["MERCADO*MERCADOLIVRE"] == "Mercado"
    resp = client.delete("/api/overrides/mercado*mercadolivre")
    assert resp.status_code == 200
    assert resp.json()["chave"] == "MERCADO*MERCADOLIVRE"
    assert "MERCADO*MERCADOLIVRE" not in client.get("/api/overrides").json()

    assert client.delete("/api/overrides/NAO EXISTE").status_code == 404


def test_api_overrides_invalido_422(client, isolado):
    """Categoria vazia/ausente/tipo errado → 422 e nada é gravado."""
    for body in [{"categoria": ""}, {"categoria": "   "}, {}, {"categoria": 123},
                 {"outro": "X"}]:
        resp = client.put("/api/overrides/QUALQUER", json=body)
        assert resp.status_code == 422, f"body aceito indevidamente: {body}"
        assert "erro" in resp.json()
    # sem arquivo (GET devolve vazio) e nada gravado pelos 422
    assert client.get("/api/overrides").json() == {}
    assert not (isolado / "overrides.json").exists()


# ---------------------------------------------------------------------------
# 5b. API — recorrentes (flag global, ortogonal a categoria/parcela)
# ---------------------------------------------------------------------------

def test_api_recorrentes_crud(client, isolado):
    """PUT marca (chave normalizada), é idempotente, GET lista, DELETE remove."""
    assert client.get("/api/recorrentes").json() == {}   # sem arquivo

    # path só com espaços normaliza para "" → 422 (e nada é gravado)
    resp = client.put("/api/recorrentes/%20%20")
    assert resp.status_code == 422
    assert "erro" in resp.json()
    assert not (isolado / "recorrentes.json").exists()

    resp = client.put("/api/recorrentes/WellHub%20%20MarcosRicioli")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "chave": "WELLHUB MARCOSRICIOLI"}
    assert client.get("/api/recorrentes").json() == {"WELLHUB MARCOSRICIOLI": True}
    assert json.loads((isolado / "recorrentes.json").read_text(encoding="utf-8")) == {
        "WELLHUB MARCOSRICIOLI": True}

    # idempotente: caixa/espaços diferentes não duplicam
    client.put("/api/recorrentes/WELLHUB MARCOSRICIOLI".replace(" ", "%20"))
    client.put("/api/recorrentes/%20wellhub%20marcosricioli%20")
    assert client.get("/api/recorrentes").json() == {"WELLHUB MARCOSRICIOLI": True}

    resp = client.delete("/api/recorrentes/wellhub%20marcosricioli")
    assert resp.status_code == 200
    assert resp.json()["chave"] == "WELLHUB MARCOSRICIOLI"
    assert client.get("/api/recorrentes").json() == {}

    # inexistente: 404 (inclusive na segunda vez)
    assert client.delete("/api/recorrentes/NAO%20EXISTE").status_code == 404
    client.put("/api/recorrentes/TWICE")
    assert client.delete("/api/recorrentes/TWICE").status_code == 200
    assert client.delete("/api/recorrentes/TWICE").status_code == 404


def test_recorrentes_isolados_e_payload_intacto(client, isolado):
    """
    A flag de recorrente é estado do usuário: independente de overrides/categorias
    e NÃO injetada no payload servido nem no jsonl.
    """
    # --- independência: gravar recorrente não mexe em categorias/overrides ---
    # (estado inicial: nenhum arquivo de sobreposição existe ainda)
    regras_antes = client.get("/api/categorias").json()
    categorias_bytes = (isolado / "categorias.json").read_bytes()

    client.put("/api/recorrentes/APPLE%20BILL")

    assert client.get("/api/recorrentes").json() == {"APPLE BILL": True}
    assert client.get("/api/overrides").json() == {}
    assert not (isolado / "overrides.json").exists()
    assert client.get("/api/categorias").json() == regras_antes
    assert (isolado / "categorias.json").read_bytes() == categorias_bytes
    assert json.loads((isolado / "recorrentes.json").read_text(encoding="utf-8")) == {
        "APPLE BILL": True}

    # --- a flag não entra no jsonl nem no payload servido -------------------
    _gravar_jsonl(isolado / "faturas.jsonl", [
        _payload([_transacao("WELLHUB MARCOSRICIOLI"), _transacao("SPOTIFY")])
    ])
    jsonl_antes = (isolado / "faturas.jsonl").read_bytes()

    assert client.put("/api/recorrentes/WELLHUB%20MARCOSRICIOLI").status_code == 200

    transacoes = client.get("/api/faturas/fat-0").json()["transacoes"]
    assert len(transacoes) == 2
    for t in transacoes:
        assert "recorrente" not in t
        assert set(t) == {"data", "descricao", "valor", "parcela_atual",
                          "parcela_total", "moeda", "categoria", "cartao", "id"}
    assert (isolado / "faturas.jsonl").read_bytes() == jsonl_antes


# ---------------------------------------------------------------------------
# 6. API — POST /recategorizar
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cenario", [
    "sem_faturas",
    "reaplica_regras_e_grava_no_disco",
    "respeita_override",
    "override_vale_para_upload_futuro",
])
def test_recategorizar(client, isolado, cenario):
    if cenario == "sem_faturas":
        resp = client.post("/api/recategorizar")
        assert resp.status_code == 200
        assert resp.json() == {"recategorizadas": 0}

    elif cenario == "reaplica_regras_e_grava_no_disco":
        _gravar_jsonl(isolado / "faturas.jsonl", [_payload([
            _transacao("PANIFICADORA E CO", categoria="Lixo"),
            _transacao("DORACIGRINGS", categoria="Lixo"),
        ])])
        resp = client.post("/api/recategorizar")
        assert resp.status_code == 200
        assert resp.json() == {"recategorizadas": 1}

        transacoes = client.get("/api/faturas/fat-0").json()["transacoes"]
        assert transacoes[0]["categoria"] == "Supermercado/Mercado"  # seed
        assert transacoes[1]["categoria"] == "Outros"                # fallback

        # regravou o jsonl no disco, preservando id/criado_em e preenchendo id
        linhas = (isolado / "faturas.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(linhas) == 1
        reg = json.loads(linhas[0])
        assert reg["payload"]["transacoes"][0]["categoria"] == "Supermercado/Mercado"
        assert reg["payload"]["transacoes"][0]["id"] == 0
        assert "criado_em" in reg and reg["id"] == "fat-0"

    elif cenario == "respeita_override":
        _gravar_jsonl(isolado / "faturas.jsonl", [_payload([
            _transacao("  amazonmktplc*  ", categoria="Lixo"),   # tem override
            _transacao("PANIFICADORA E CO", categoria="Lixo"),   # segue regra
            _transacao("AMAZON OUTRA LOJA", categoria="Lixo"),   # mesma regra, sem override
        ])])

        # override global na descrição normalizada exata
        assert client.put("/api/overrides/amazonmktplc*",
                          json={"categoria": "Assinaturas"}).status_code == 200
        # regra nova: casa AMAZON e PANIFICADORA com OUTRAS categorias
        resp = client.put("/api/categorias", json={"regras": [
            {"categoria": "Amazonia", "padroes": ["AMAZON"]},
            {"categoria": "Padaria Nova", "padroes": ["PANIFICADORA"]},
        ]})
        assert resp.status_code == 200

        resp = client.post("/api/recategorizar")
        assert resp.status_code == 200
        assert resp.json() == {"recategorizadas": 1}

        transacoes = client.get("/api/faturas/fat-0").json()["transacoes"]
        # override NÃO muda quando a regra muda
        assert transacoes[0]["categoria"] == "Assinaturas"
        assert transacoes[1]["categoria"] == "Padaria Nova"
        # mesma regra vale para quem não tem override
        assert transacoes[2]["categoria"] == "Amazonia"

    else:
        # override gravado vale para faturas processadas depois (via categorizar)
        client.put("/api/overrides/PANIFICADORA E CO", json={"categoria": "Padaria Manual"})
        _gravar_jsonl(isolado / "faturas.jsonl", [
            _payload([_transacao("PANIFICADORA E CO", categoria="Lixo")])])
        client.post("/api/recategorizar")
        assert client.get("/api/faturas/fat-0").json()["transacoes"][0]["categoria"] == \
            "Padaria Manual"


# ---------------------------------------------------------------------------
# 7. Tolerância a faturas antigas (sem id)
# ---------------------------------------------------------------------------

def test_ids_preenchidos_por_indice(client, isolado):
    """GET preenche id por índice quando o jsonl antigo não tinha id."""
    _gravar_jsonl(isolado / "faturas.jsonl", [_payload([
        _transacao("A"), _transacao("B"), _transacao("C"),
    ])])
    transacoes = client.get("/api/faturas/fat-0").json()["transacoes"]
    assert [t["id"] for t in transacoes] == [0, 1, 2]

    from schemas import fatura_para_dict
    d = fatura_para_dict(_fatura(["A", "B", "C"]))
    assert [t["id"] for t in d["transacoes"]] == [0, 1, 2]
