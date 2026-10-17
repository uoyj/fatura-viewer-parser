"""
Testes para dedupe.py e integração com POST /api/faturas.

Cobre:
- fingerprint_pdf: determinístico e sensível a bytes diferentes
- chave_natural: tuple (banco, fechamento, total_a_pagar, len(transacoes))
- encontrar_duplicata:
  * match forte por pdf_hash
  * match fraco por chave natural (PDF re-emitido)
  * registros antigos sem pdf_hash não quebram
  * PDFs diferentes passam limpos
- POST /api/faturas: mesmo PDF duas vezes → 409 {"id": ...}
"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

import api
from api import app
from dedupe import chave_natural, encontrar_duplicata, fingerprint_pdf
from schemas import Fatura, Transacao

slow = pytest.mark.slow


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Cliente TestClient isolado (nunca toca data/ real)."""
    monkeypatch.setattr(api, "UPLOADS_DIR", tmp_path / "uploads")
    monkeypatch.setattr(api, "JSONL_PATH", tmp_path / "faturas.jsonl")
    monkeypatch.setattr(api, "CATEGORIAS_PATH", tmp_path / "categorias.json")
    monkeypatch.setattr(api, "OVERRIDES_PATH", tmp_path / "overrides.json")
    api.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    return TestClient(app)


def _payload_base(**kwargs):
    p = {
        "banco": "sofisa",
        "modelo": "2026-09",
        "fechamento": "2026-09-15",
        "vencimento": "2026-09-20",
        "total_a_pagar": "6727.55",
        "pagamento_minimo": "672.76",
        "transacoes": [{"id": i} for i in range(3)],
    }
    p.update(kwargs)
    return p


def _registro(pdf_hash=None, payload=None, rid="r1"):
    r = {
        "id": rid,
        "criado_em": "2026-01-01T00:00:00+00:00",
        "banco": (payload or {}).get("banco", "sofisa"),
        "modelo": (payload or {}).get("modelo", "2026-09"),
        "payload": payload or _payload_base(),
    }
    if pdf_hash is not None:
        r["pdf_hash"] = pdf_hash
    return r


def _fatura(total="6727.55"):
    return Fatura(
        banco="sofisa",
        modelo="2026-09",
        fechamento=date(2026, 9, 15),
        vencimento=date(2026, 9, 20),
        total_a_pagar=Decimal(total),
        pagamento_minimo=Decimal("672.76"),
        transacoes=[
            Transacao(data=date(2026, 9, 1), descricao="x", valor=Decimal("10.00"))
        ],
    )


# ---------------------------------------------------------------------------
# Funções puras
# ---------------------------------------------------------------------------

def test_fingerprint_pdf_eh_deterministico():
    assert fingerprint_pdf(b"abc") == fingerprint_pdf(b"abc")
    assert fingerprint_pdf(b"abc") != fingerprint_pdf(b"abd")


def test_chave_natural_eh_tuple_esperada():
    p = _payload_base()
    assert chave_natural(p) == ("sofisa", "2026-09-15", "6727.55", 3)


def test_encontrar_duplicata_match_forte_por_hash():
    fp = fingerprint_pdf(b"pdf1")
    regs = [_registro(pdf_hash=fp)]
    dup = encontrar_duplicata(fp, ("outro", "2026-01-01", "1.00", 0), regs)
    assert dup is not None
    assert dup["id"] == "r1"


def test_encontrar_duplicata_match_fraco_por_chave_natural():
    p = _payload_base()
    regs = [_registro(pdf_hash="outro-hash", payload=p)]
    dup = encontrar_duplicata(fingerprint_pdf(b"reemitido"), chave_natural(p), regs)
    assert dup is not None
    assert dup["id"] == "r1"


def test_encontrar_duplicata_registro_antigo_sem_pdf_hash_nao_quebra():
    p = _payload_base()
    regs = [{"id": "antigo", "payload": p}]  # sem pdf_hash
    dup = encontrar_duplicata("qualquer", chave_natural(p), regs)
    assert dup is not None
    assert dup["id"] == "antigo"


def test_encontrar_duplicata_pdfs_diferentes_passam_limpos():
    p = _payload_base()
    regs = [_registro(pdf_hash="hash1", payload=p)]
    assert (
        encontrar_duplicata("hash2", ("nubank", "2026-08-15", "100.00", 1), regs)
        is None
    )


# ---------------------------------------------------------------------------
# Integração com POST /api/faturas
# ---------------------------------------------------------------------------

def _mock_parser(monkeypatch, fatura=None):
    """Mocka extract_text/get_parser para não depender de PDF real."""
    monkeypatch.setattr(api, "extract_text", lambda path: {})
    monkeypatch.setattr(
        api, "get_parser", lambda banco, versao: (lambda _: fatura or _fatura())
    )


@slow
def test_mesmo_pdf_duas_vezes_retorna_409_com_id_existente(client):
    with open("in/Fatura.pdf", "rb") as f:
        resp1 = client.post(
            "/api/faturas",
            files={"arquivo": ("Fatura.pdf", f, "application/pdf")},
            data={"banco": "sofisa"},
        )
    assert resp1.status_code == 200, resp1.text
    fid = resp1.json()["id"]

    with open("in/Fatura.pdf", "rb") as f:
        resp2 = client.post(
            "/api/faturas",
            files={"arquivo": ("Fatura.pdf", f, "application/pdf")},
            data={"banco": "sofisa"},
        )
    assert resp2.status_code == 409, resp2.text
    body = resp2.json()["detail"]
    assert body["code"] == "fatura_duplicada"
    assert body["fatura_id"] == fid
    assert body["banco"] == "sofisa"
    assert body["fechamento"] == "2026-09-15"

    # Não persistiu segundo PDF nem segundo registro
    assert len(api.JSONL_PATH.read_text().strip().splitlines()) == 1
    assert len(list(api.UPLOADS_DIR.glob("*.pdf"))) == 1


def test_pdf_reemitido_retorna_409_pela_chave_natural(client, monkeypatch):
    _mock_parser(monkeypatch, _fatura())

    resp1 = client.post(
        "/api/faturas",
        files={"arquivo": ("a.pdf", b"conteudo1", "application/pdf")},
        data={"banco": "sofisa"},
    )
    assert resp1.status_code == 200, resp1.text
    fid = resp1.json()["id"]

    resp2 = client.post(
        "/api/faturas",
        files={"arquivo": ("a.pdf", b"conteudo2", "application/pdf")},
        data={"banco": "sofisa"},
    )
    assert resp2.status_code == 409, resp2.text
    body = resp2.json()["detail"]
    assert body["code"] == "fatura_duplicada"
    assert body["fatura_id"] == fid
    assert body["banco"] == "sofisa"
    assert body["fechamento"] == "2026-09-15"

    assert len(api.JSONL_PATH.read_text().strip().splitlines()) == 1


def test_pdfs_diferentes_passam_limpos(client, monkeypatch):
    _mock_parser(monkeypatch, _fatura("6727.55"))

    resp1 = client.post(
        "/api/faturas",
        files={"arquivo": ("a.pdf", b"conteudo1", "application/pdf")},
        data={"banco": "sofisa"},
    )
    assert resp1.status_code == 200, resp1.text
    fid1 = resp1.json()["id"]

    _mock_parser(monkeypatch, _fatura("9999.99"))
    resp2 = client.post(
        "/api/faturas",
        files={"arquivo": ("b.pdf", b"conteudo2", "application/pdf")},
        data={"banco": "sofisa"},
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["id"] != fid1

    assert len(api.JSONL_PATH.read_text().strip().splitlines()) == 2
