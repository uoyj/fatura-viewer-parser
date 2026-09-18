"""
Testes de personalização global de categorias (Tier 3).

Cobre:
- normalizar_descricao (upper/strip/colapso de espaços)
- seed: data/categorias.json criado UMA vez; depois o arquivo é a fonte da verdade
- precedência: override manual global > regras > "Outros"
- API: GET/PUT /categorias, GET/PUT/DELETE /overrides, POST /recategorizar

ISOLAMENTO OBRIGATÓRIO: nenhum teste toca o data/ real do usuário.
O fixture `isolado` monkeypatcha categorias.json, overrides.json E
faturas.jsonl (api + categorizer) para um tmp_path que o pytest apaga.

Rode: pytest tests/test_personalizacao.py -x
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

    # categorizer.py (usado direto nos testes unitários)
    monkeypatch.setattr(categorizer, "DATA_DIR", tmp_path)
    monkeypatch.setattr(categorizer, "CATEGORIAS_PATH", tmp_path / "categorias.json")
    monkeypatch.setattr(categorizer, "OVERRIDES_PATH", tmp_path / "overrides.json")

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
# 1. Normalização
# ---------------------------------------------------------------------------

class TestNormalizarDescricao:

    def test_normalizar_descricao(self):
        assert normalizar_descricao("Mercado*MercadoLivre  ") == "MERCADO*MERCADOLIVRE"
        assert normalizar_descricao("  mercado*mercadolivre") == "MERCADO*MERCADOLIVRE"
        assert normalizar_descricao("MERCADO*MERCADOLIVRE") == "MERCADO*MERCADOLIVRE"

    def test_colapsa_espacos_multiplos(self):
        assert normalizar_descricao("MERCADO*MERCADO    LIVRE") == "MERCADO*MERCADO LIVRE"
        assert normalizar_descricao("\tEc \n STERILAIR\t") == "EC STERILAIR"

    def test_vazio(self):
        assert normalizar_descricao("") == ""
        assert normalizar_descricao("    ") == ""


# ---------------------------------------------------------------------------
# 2. Seed do dicionário editável
# ---------------------------------------------------------------------------

class TestSeed:

    def test_seed_cria_arquivo_quando_ausente(self, isolado):
        path = isolado / "categorias.json"
        assert not path.exists()

        regras = carregar_regras()

        assert path.exists(), "seed deveria criar data/categorias.json"
        assert regras, "seed não pode ser vazio"
        conteudo = json.loads(path.read_text(encoding="utf-8"))
        assert conteudo == {"regras": regras}
        assert [r["categoria"] for r in regras] == [r["categoria"] for r in SEED_REGRAS]

    def test_seed_nao_sobrescreve_arquivo_existente(self, isolado):
        path = isolado / "categorias.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        custom = {"regras": [{"categoria": "So Minha", "padroes": ["XPTO"]}]}
        path.write_text(json.dumps(custom), encoding="utf-8")

        regras = carregar_regras()  # segundo carregamento lê do ARQUIVO

        assert regras == custom["regras"]
        assert regras != SEED_REGRAS
        assert json.loads(path.read_text(encoding="utf-8")) == custom

    def test_seed_sem_outros(self):
        """'Outros' é fallback implícito, nunca uma regra do seed."""
        assert all(r["padroes"] for r in SEED_REGRAS), "seed não deve ter regra sem padrões"
        assert all(r["categoria"] != "Outros" for r in SEED_REGRAS)


# ---------------------------------------------------------------------------
# 3. Precedência: override > regras > "Outros"
# ---------------------------------------------------------------------------

class TestPrecedencia:

    def test_regra_sem_override(self, isolado):
        """Comportamento anterior preservado: regra casa → categoria da regra."""
        fatura = categorizar(_fatura(["PANIFICADORA E CO", "AMAZONMKTPLC*LHCOMPROD", "DORACIGRINGS"]))
        cats = [t.categoria for t in fatura.transacoes]
        assert cats == ["Supermercado/Mercado", "Compras online", "Outros"]

    def test_override_vence_regra(self, isolado):
        # A regra de seed (AMAZON → "Compras online") casa a descrição...
        fatura = categorizar(_fatura(["AMAZONMKTPLC*"]))
        assert fatura.transacoes[0].categoria == "Compras online"

        # ...mas o override global da MESMA descrição normalizada vence.
        chave = normalizar_descricao("AMAZONMKTPLC*")
        salvar_overrides({chave: "Teste"})

        fatura = categorizar(_fatura(["AMAZONMKTPLC*"]))
        assert fatura.transacoes[0].categoria == "Teste"

    def test_override_por_descricao_normalizada(self, isolado):
        """Override casa mesmo com ruído de caixa/espaços na descrição."""
        salvar_overrides({normalizar_descricao("amazonmktplc*"): "Assinaturas"})
        fatura = categorizar(_fatura(["  AmazonMktplc*  "]))
        assert fatura.transacoes[0].categoria == "Assinaturas"

    def test_overrides_ausente_retorna_vazio(self, isolado):
        assert not (isolado / "overrides.json").exists()
        assert carregar_overrides() == {}

    def test_prioridade_independe_da_ordem_das_regras(self, isolado):
        """Override vence até quando a regra que casa está em 1º lugar."""
        from categorizer import salvar_regras
        salvar_regras([
            {"categoria": "Primeira", "padroes": ["PANIFICADORA"]},
            {"categoria": "Segunda", "padroes": ["COISA"]},
        ])
        salvar_overrides({normalizar_descricao("PANIFICADORA E CO"): "Manual"})
        fatura = categorizar(_fatura(["PANIFICADORA E CO", "COISA NADA"]))
        assert [t.categoria for t in fatura.transacoes] == ["Manual", "Segunda"]


# ---------------------------------------------------------------------------
# 4. API — /categorias
# ---------------------------------------------------------------------------

class TestAPICategorias:

    def test_get_categorias_cria_seed(self, client, isolado):
        resp = client.get("/categorias")
        assert resp.status_code == 200
        regras = resp.json()["regras"]
        assert regras == SEED_REGRAS
        assert (isolado / "categorias.json").exists()

    def test_put_categorias_valido(self, client, isolado):
        novas = [{"categoria": "Nova Cat", "padroes": ["NOVO PADRAO"]}]
        resp = client.put("/categorias", json={"regras": novas})
        assert resp.status_code == 200
        assert resp.json()["regras"] == novas
        assert client.get("/categorias").json()["regras"] == novas
        # gravou no tmp_path (nunca no data/ real)
        assert json.loads((isolado / "categorias.json").read_text(encoding="utf-8")) == {"regras": novas}

    @pytest.mark.parametrize("body", [
        {"regras": "nao é lista"},
        {"regras": []},
        {"regras": [{"categoria": "", "padroes": ["X"]}]},
        {"regras": [{"categoria": "   ", "padroes": ["X"]}]},
        {"regras": [{"categoria": "X", "padroes": []}]},
        {"regras": [{"categoria": "X"}]},
        {"regras": [{"categoria": "X", "padroes": "X"}]},
        {"regras": [{"categoria": "X", "padroes": ["  "]}]},
        {"regras": [{"categoria": "X", "padroes": [123]}]},
        {"regras": ["nao é objeto"]},
        {"nao_tem_regras": []},
    ])
    def test_put_categorias_invalido_422(self, client, body):
        resp = client.put("/categorias", json=body)
        assert resp.status_code == 422
        assert "erro" in resp.json()

    def test_put_categorias_json_quebrado_422(self, client):
        resp = client.put("/categorias", content="{isso nao é json", headers={"Content-Type": "application/json"})
        assert resp.status_code == 422
        assert "erro" in resp.json()

    def test_put_categorias_invalido_nao_grava(self, client, isolado):
        client.put("/categorias", json={"regras": [{"categoria": "X", "padroes": []}]})
        # seed ainda intacto (ou arquivo ausente) — nada foi gravado
        assert not (isolado / "categorias.json").exists()


# ---------------------------------------------------------------------------
# 5. API — /overrides
# ---------------------------------------------------------------------------

class TestAPIOverrides:

    def test_put_e_get_overrides(self, client, isolado):
        resp = client.put("/overrides/AmazonMktplc*LHComProd", json={"categoria": "Compras online"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["chave"] == "AMAZONMKTPLC*LHCOMPROD"
        assert body["categoria"] == "Compras online"

        resp = client.get("/overrides")
        assert resp.status_code == 200
        assert resp.json() == {"AMAZONMKTPLC*LHCOMPROD": "Compras online"}
        assert json.loads((isolado / "overrides.json").read_text(encoding="utf-8")) == {
            "AMAZONMKTPLC*LHCOMPROD": "Compras online"
        }

    def test_put_override_normaliza_espacos(self, client):
        resp = client.put("/overrides/99FOOD%20%20*COLHERADA", json={"categoria": "Comida"})
        assert resp.status_code == 200
        assert resp.json()["chave"] == "99FOOD *COLHERADA"
        assert client.get("/overrides").json() == {"99FOOD *COLHERADA": "Comida"}

    def test_put_override_categoria_vazia_422(self, client):
        for body in [{"categoria": ""}, {"categoria": "   "}, {}, {"categoria": 123}, {"outro": "X"}]:
            resp = client.put("/overrides/QUALQUER", json=body)
            assert resp.status_code == 422, f"body aceito indevidamente: {body}"
            assert "erro" in resp.json()

    def test_delete_override(self, client):
        client.put("/overrides/MERCADO*MERCADOLIVRE", json={"categoria": "Mercado"})
        assert client.get("/overrides").json() == {"MERCADO*MERCADOLIVRE": "Mercado"}

        resp = client.delete("/overrides/mercado*mercadolivre")
        assert resp.status_code == 200
        assert resp.json()["chave"] == "MERCADO*MERCADOLIVRE"
        assert client.get("/overrides").json() == {}

    def test_delete_override_inexistente_404(self, client):
        resp = client.delete("/overrides/NAO EXISTE")
        assert resp.status_code == 404

    def test_get_overrides_sem_arquivo(self, client):
        assert client.get("/overrides").json() == {}


# ---------------------------------------------------------------------------
# 6. API — POST /recategorizar
# ---------------------------------------------------------------------------

class TestRecategorizar:

    def test_recategorizar_sem_faturas(self, client):
        resp = client.post("/recategorizar")
        assert resp.status_code == 200
        assert resp.json() == {"recategorizadas": 0}

    def test_recategorizar_reaplica_regras(self, client, isolado):
        _gravar_jsonl(isolado / "faturas.jsonl", [_payload([
            _transacao("PANIFICADORA E CO", categoria="Lixo"),
            _transacao("DORACIGRINGS", categoria="Lixo"),
        ])])

        resp = client.post("/recategorizar")
        assert resp.status_code == 200
        assert resp.json() == {"recategorizadas": 1}

        transacoes = client.get("/faturas/fat-0").json()["transacoes"]
        assert transacoes[0]["categoria"] == "Supermercado/Mercado"  # seed
        assert transacoes[1]["categoria"] == "Outros"                # fallback

    def test_recategorizar_respeita_override(self, client, isolado):
        _gravar_jsonl(isolado / "faturas.jsonl", [_payload([
            _transacao("  amazonmktplc*  ", categoria="Lixo"),   # tem override
            _transacao("PANIFICADORA E CO", categoria="Lixo"),   # segue regra
            _transacao("AMAZON OUTRA LOJA", categoria="Lixo"),   # mesma regra, sem override
        ])])

        # override global na descrição normalizada exata
        assert client.put("/overrides/amazonmktplc*", json={"categoria": "Assinaturas"}).status_code == 200
        # regra nova: casa AMAZON e PANIFICADORA com OUTRAS categorias
        resp = client.put("/categorias", json={"regras": [
            {"categoria": "Amazonia", "padroes": ["AMAZON"]},
            {"categoria": "Padaria Nova", "padroes": ["PANIFICADORA"]},
        ]})
        assert resp.status_code == 200

        resp = client.post("/recategorizar")
        assert resp.status_code == 200
        assert resp.json() == {"recategorizadas": 1}

        transacoes = client.get("/faturas/fat-0").json()["transacoes"]
        # override NÃO muda quando a regra muda
        assert transacoes[0]["categoria"] == "Assinaturas"
        assert transacoes[1]["categoria"] == "Padaria Nova"
        # mesma regra vale para quem não tem override
        assert transacoes[2]["categoria"] == "Amazonia"

    def test_recategorizar_regrava_jsonl_no_disco(self, client, isolado):
        _gravar_jsonl(isolado / "faturas.jsonl", [_payload([_transacao("PANIFICADORA E CO", categoria="Lixo")])])
        client.post("/recategorizar")

        linhas = (isolado / "faturas.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(linhas) == 1
        reg = json.loads(linhas[0])
        assert reg["payload"]["transacoes"][0]["categoria"] == "Supermercado/Mercado"
        assert reg["payload"]["transacoes"][0]["id"] == 0
        assert "criado_em" in reg and reg["id"] == "fat-0"

    def test_recategorizar_atualiza_upload_futuro(self, client, isolado):
        """Override gravado vale para faturas processadas depois (via categorizar)."""
        client.put("/overrides/PANIFICADORA E CO", json={"categoria": "Padaria Manual"})
        _gravar_jsonl(isolado / "faturas.jsonl", [_payload([_transacao("PANIFICADORA E CO", categoria="Lixo")])])
        client.post("/recategorizar")
        assert client.get("/faturas/fat-0").json()["transacoes"][0]["categoria"] == "Padaria Manual"


# ---------------------------------------------------------------------------
# 7. Tolerância a faturas antigas (sem id)
# ---------------------------------------------------------------------------

class TestIds:

    def test_get_fatura_preenche_ids_por_indice(self, client, isolado):
        _gravar_jsonl(isolado / "faturas.jsonl", [_payload([
            _transacao("A"), _transacao("B"), _transacao("C"),
        ])])
        transacoes = client.get("/faturas/fat-0").json()["transacoes"]
        assert [t["id"] for t in transacoes] == [0, 1, 2]

    def test_fatura_para_dict_atribui_id_por_indice(self, isolado):
        from schemas import fatura_para_dict
        d = fatura_para_dict(_fatura(["A", "B", "C"]))
        assert [t["id"] for t in d["transacoes"]] == [0, 1, 2]
