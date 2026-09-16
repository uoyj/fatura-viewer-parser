"""
Conftest para testes — garante que parsers sejam importados e registrados
no registry antes de qualquer teste.

O registry é populado por side-effect do import dos módulos de parser,
então precisamos importá-los explicitamente antes de get_parser().
"""

import parsers  # noqa: F401 — side-effect: registra parsers no registry
