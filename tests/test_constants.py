"""Guardas de sincronía entre las constantes de dominio y el resto del código."""

import re
from pathlib import Path

from facturador.constants import InvoiceStatus

SCHEMA = Path("facturador/schema.sql").read_text(encoding="utf-8")


def test_check_de_status_en_schema_sql_coincide_con_el_enum():
    """El CHECK de invoices.status es SQL plano: no puede importar el enum,
    así que este test es lo que impide que diverjan."""
    match = re.search(r"CHECK \(status IN\s*\(([^)]*)\)\)", SCHEMA)
    assert match, "no se encontró el CHECK de status en schema.sql"
    en_schema = set(re.findall(r"'([a-z]+)'", match.group(1)))
    assert en_schema == {s.value for s in InvoiceStatus}


def test_invoice_status_es_transparente_como_str():
    """StrEnum: los miembros deben poder ir directo a SQLite y al JSON."""
    assert InvoiceStatus.DRAFT == "draft"
    assert isinstance(InvoiceStatus.AUTHORIZED, str)
    assert f"{InvoiceStatus.UNKNOWN}" == "unknown"
