"""
Conftest para testes — garante que parsers sejam importados e registrados
no registry antes de qualquer teste.

O registry é populado por side-effect do import dos módulos de parser,
então precisamos importá-los explicitamente antes de get_parser().
"""

import pytest

import parsers  # noqa: F401 — side-effect: registra parsers no registry


@pytest.fixture(scope="session")
def extract_cached():
    """
    Extrai cada PDF no máximo UMA vez por sessão e memoiza por caminho.

    O mesmo PDF (in/Fatura.pdf) é usado por mais de um módulo de teste — sem o
    cache cada módulo re-extrai. O output do extractor é somente leitura (os
    parsers só leem `pages`/`lines`), então compartilhar é seguro.

    Uso:
        @pytest.fixture(scope="module")
        def fatura(extract_cached):
            return parse_fn(extract_cached(PDF_PATH))
    """
    cache: dict[str, dict] = {}

    def _get(path):
        key = str(path)
        if key not in cache:
            from extractors.pdf_extractor import extract_text
            cache[key] = extract_text(path)
        return cache[key]

    return _get
