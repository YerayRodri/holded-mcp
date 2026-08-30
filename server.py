"""
Holded — Servidor MCP
Facturas, compras, contactos, presupuestos y tesorería para autónomo.

Credencial:
  HOLDED_API_KEY  →  env var (Personal Access Token de Holded)
  Configuración → Integraciones → API → Generar clave

Límite: 25.000 llamadas/mes. Usar siempre filtros de fecha en listados.
"""

import base64
import mimetypes
import os
from pathlib import Path
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("holded")

BASE = "https://api.holded.com/api/v2"
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / "holded-mcp"

# Mapa de frecuencia legible → código API de Holded
_FREQ = {"daily": "d", "weekly": "w", "monthly": "m", "yearly": "y"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _key() -> str:
    key = os.environ.get("HOLDED_API_KEY", "")
    if not key:
        raise RuntimeError("HOLDED_API_KEY no configurada")
    return key


def _h() -> dict:
    k = _key()
    # PAT tokens (pat_...) usan Bearer; keys legacy usan header 'key'
    if k.startswith("pat_"):
        return {"Authorization": f"Bearer {k}", "Content-Type": "application/json"}
    return {"key": k, "Content-Type": "application/json"}


def _hf() -> dict:
    """Headers para multipart/form-data (sin Content-Type, requests lo pone solo)."""
    k = _key()
    if k.startswith("pat_"):
        return {"Authorization": f"Bearer {k}"}
    return {"key": k}


def _p(**kw) -> dict:
    return {k: v for k, v in kw.items() if v is not None}


def _confirm(action: str, detail: str, confirmed: bool) -> dict | None:
    """
    Guarda para operaciones financieras irreversibles o de envío real.

    Devuelve el aviso de confirmación si `confirmed` no es True (sin haber
    hecho ninguna llamada a la API de Holded todavía). Devuelve None si
    `confirmed=True` y se puede proceder. Mismo patrón que `_confirm()` en
    google-ads-write-mcp/gmail-mcp — añadido 2026-08-30, ver docs/APIS.md:
    hasta entonces ninguna operación de Holded tenía freno técnico propio.

    Args:
        action: descripción corta de la acción, para el aviso.
        detail: datos concretos de la operación (importe, factura, destino...).
        confirmed: el parámetro que pasa quien llama a la tool.
    """
    if confirmed:
        return None
    return {
        "requires_confirmation": True,
        "warning": f"Acción financiera: {action}. {detail}",
        "instruction": "Muestra este aviso al usuario y pide confirmación explícita. "
                       "Solo repite la llamada con confirmed=True si el usuario confirma.",
    }


def _raise(r: requests.Response) -> None:
    """Lanza RuntimeError con el cuerpo JSON de error de Holded si está disponible."""
    if not r.ok:
        try:
            detail = r.json()
        except Exception:
            detail = r.text[:500]
        raise RuntimeError(f"Holded {r.status_code}: {detail}")


def _get(path: str, **kw) -> Any:
    r = requests.get(f"{BASE}{path}", headers=_h(), params=_p(**kw), timeout=30)
    _raise(r)
    return r.json()


def _get_filtered(path: str, start_date: str | None, end_date: str | None,
                  date_field: str = "date", **kw) -> Any:
    """GET con paginación completa (cursor) + filtrado de fecha client-side."""
    all_items: list = []
    params = _p(**kw)
    while True:
        r = requests.get(f"{BASE}{path}", headers=_h(), params=params, timeout=30)
        _raise(r)
        data = r.json()
        if isinstance(data, dict):
            page = data.get("items", [])
        elif isinstance(data, list):
            page = data
        else:
            page = []
        all_items.extend(page if isinstance(page, list) else [page])
        if not (isinstance(data, dict) and data.get("has_more")):
            break
        cursor = data.get("cursor")
        if not cursor:
            break
        params = {**params, "cursor": cursor}
    if start_date:
        all_items = [i for i in all_items if str(i.get(date_field, ""))[:10] >= start_date]
    if end_date:
        all_items = [i for i in all_items if str(i.get(date_field, ""))[:10] <= end_date]
    # Recorte de coste — verificado en vivo 2026-08-30 (ver docs/APIS.md): en una respuesta real
    # de list_invoices, el array `lines` (líneas de factura completas: producto, impuestos,
    # cuenta contable, retención...) era el 81% de los bytes totales (47.991 de 59.057 caracteres
    # para solo 19 facturas) — y con un rango de fechas algo más amplio la respuesta entera supera
    # el límite de la tool y falla directamente. Holded API v2 no tiene un parámetro `fields=`
    # (verificado con context7, no lo documenta), así que este es el único lever posible: quitar
    # `lines` de la vista de LISTADO (queda en get_invoice/get_purchase/etc, que sí devuelven el
    # documento completo). `.pop(..., None)` es un no-op para los recursos que no tienen `lines`
    # (ej. movimientos bancarios), así que es seguro aplicarlo aquí para los 6 tools que pasan por
    # esta función.
    for i in all_items:
        if isinstance(i, dict):
            i.pop("lines", None)
    return {"items": all_items, "filtered_count": len(all_items)}


def _post(path: str, body: dict | None = None) -> Any:
    r = requests.post(f"{BASE}{path}", headers=_h(), json=body or {}, timeout=60)
    _raise(r)
    return r.json() if r.content else {"status": "ok"}


def _put(path: str, body: dict) -> Any:
    r = requests.put(f"{BASE}{path}", headers=_h(), json=body, timeout=30)
    _raise(r)
    return r.json() if r.content else {"status": "ok"}


def _lines_to_items(lines: list) -> list:
    """Convierte el formato 'lines' del GET al formato 'items' del PUT preservando los datos.
    taxes viene del GET como lista de objetos {id, name, rate...} → se convierte a lista de IDs.
    account puede venir como objeto {id, name} → se convierte a ID string.
    """
    keep = ("name", "units", "price", "taxes", "account", "description",
            "discount", "product_id", "service_id", "sku", "retention", "unit_type")
    result = []
    for line in lines:
        item = {k: v for k, v in line.items() if k in keep and v is not None}
        if "taxes" in item and isinstance(item["taxes"], list):
            item["taxes"] = [
                t["id"] if isinstance(t, dict) else t for t in item["taxes"]
            ]
        if "account" in item and isinstance(item["account"], dict):
            item["account"] = item["account"].get("id", item["account"])
        result.append(item)
    return result


def _post_file(path: str, file_path: str) -> Any:
    filename = os.path.basename(file_path)
    mime, _ = mimetypes.guess_type(filename)
    mime = mime or "application/octet-stream"
    with open(file_path, "rb") as f:
        r = requests.post(
            f"{BASE}{path}",
            headers=_hf(),
            files={"file": (filename, f, mime)},
            timeout=60,
        )
    r.raise_for_status()
    return r.json() if r.content else {"status": "ok"}


def _account_balance(account_id: str) -> float:
    """Obtiene el saldo actual de una cuenta bancaria."""
    data = _get("/treasury/accounts")
    for acc in data.get("items", []):
        if acc.get("id") == account_id:
            return float(acc.get("balance", 0))
    return 0.0


# ── FACTURAS EMITIDAS (VENTAS) ────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def list_invoices(
    start_date: str | None = None,
    end_date: str | None = None,
    contact_id: str | None = None,
    status: str | None = None,
    page: int | None = None,
) -> Any:
    """
    Lista facturas emitidas (ventas).
    start_date / end_date: YYYY-MM-DD. Usar siempre para no agotar el límite mensual.
    status: pending | completed | partial | cancelled | failed | overdue.
    draft=true aparece como campo booleano en cada item, no como status.
    """
    return _get_filtered("/invoices", start_date, end_date,
                         contactId=contact_id, status=status, page=page)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_invoice(invoice_id: str) -> Any:
    """Detalle completo de una factura emitida."""
    return _get(f"/invoices/{invoice_id}")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def create_invoice(
    contact_id: str,
    date: str,
    items: list[dict],
    due_date: str | None = None,
    notes: str | None = None,
    number_line_id: str | None = None,
    currency: str = "EUR",
    discount: float | None = None,
    payment_method_id: str | None = None,
    tags: list[str] | None = None,
) -> Any:
    """
    Crea una factura emitida (venta).
    date: YYYY-MM-DD.
    items: lista de objetos → {name, units, price, taxes: [tax_id], description?}.
      - taxes: lista de IDs de impuesto (obtener con list_taxes).
    number_line_id: serie de numeración (obtener con list_numbering_series).
    NOTA TicketBAI: si la cuenta está en Bizkaia, el contacto debe tener NIF válido
    y los items deben tener cuenta contable (account). Sin number_line_id crea borrador sin TicketBAI.
    """
    body = _p(
        contact_id=contact_id,
        date=date,
        due_date=due_date,
        notes=notes,
        number_line_id=number_line_id,
        currency=currency,
        discount=discount,
        payment_method_id=payment_method_id,
        tags=tags,
        items=items,
    )
    return _post("/invoices", body)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
def approve_invoice(invoice_id: str, confirmed: bool = False) -> Any:
    """Aprueba (finaliza) un borrador de factura. Genera el número definitivo. Requiere confirmed=True."""
    aviso = _confirm("aprobar factura (genera número definitivo, irreversible)",
                      f"invoice_id={invoice_id}", confirmed)
    if aviso:
        return aviso
    return _post(f"/invoices/{invoice_id}/approve")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
def send_invoice(
    invoice_id: str,
    emails: list[str],
    subject: str | None = None,
    message: str | None = None,
    confirmed: bool = False,
) -> Any:
    """Envía la factura por email al cliente. message: cuerpo del email. Requiere confirmed=True."""
    aviso = _confirm("enviar factura por email",
                      f"invoice_id={invoice_id}, destinatarios={emails}", confirmed)
    if aviso:
        return aviso
    return _post(f"/invoices/{invoice_id}/send",
                 _p(emails=emails, subject=subject, message=message))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
def register_invoice_payment(
    invoice_id: str,
    amount: float,
    date: str,
    account_id: str | None = None,
    description: str | None = None,
    confirmed: bool = False,
) -> Any:
    """
    Registra un cobro en una factura emitida. Requiere confirmed=True.
    date: YYYY-MM-DD.
    account_id: ID de cuenta bancaria donde se recibe el cobro (treasury_id en la API).
    """
    aviso = _confirm("registrar cobro de factura",
                      f"invoice_id={invoice_id}, importe={amount}, fecha={date}", confirmed)
    if aviso:
        return aviso
    body = _p(amount=amount, date=date, treasury_id=account_id, description=description)
    return _post(f"/invoices/{invoice_id}/payments", body)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_invoice_pdf(invoice_id: str, save_path: str | None = None, as_base64: bool = False) -> Any:
    """
    Descarga el PDF de una factura emitida y lo guarda en disco.
    save_path: ruta local donde guardar (por defecto ~/Downloads/holded-mcp/<invoice_id>.pdf).
    as_base64: si True, devuelve el contenido en base64 en vez de guardarlo — evitar salvo que
      haga falta de verdad incrustar el PDF (un PDF típico son decenas de miles de tokens).
    """
    r = requests.get(f"{BASE}/invoices/{invoice_id}/pdf", headers=_h(), timeout=30)
    r.raise_for_status()
    # Recorte de coste — verificado 2026-08-30 (ver docs/APIS.md): antes, sin save_path, esto
    # devolvía el PDF entero en base64 dentro de la respuesta de la tool — un vector directo de
    # miles de tokens innecesarios. Mismo patrón ya probado en gmail-mcp (download_attachment/
    # export_message_to_pdf): guardar en disco por defecto y devolver la ruta.
    if as_base64:
        return {"pdf_base64": base64.b64encode(r.content).decode(), "size_bytes": len(r.content)}
    target = Path(save_path).expanduser() if save_path else DEFAULT_DOWNLOAD_DIR / f"{invoice_id}.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(r.content)
    return {"saved": str(target), "size_bytes": len(r.content)}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def attach_document_to_invoice(invoice_id: str, file_path: str) -> Any:
    """Adjunta un PDF u otro documento a una factura emitida. file_path: ruta local."""
    return _post_file(f"/invoices/{invoice_id}/attachments", file_path)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def update_invoice(
    invoice_id: str,
    contact_id: str | None = None,
    date: str | None = None,
    due_date: str | None = None,
    items: list[dict] | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
    number_line_id: str | None = None,
    currency: str | None = None,
    discount: float | None = None,
    payment_method_id: str | None = None,
) -> Any:
    """
    Actualiza cualquier campo de una factura emitida.
    Solo se modifican los campos que se pasen — el resto se preserva del original.
    date: YYYY-MM-DD. items: [{name, units, price, taxes:[id]}].
    Funciona siempre en borradores. Facturas aprobadas pueden tener restricciones en Holded.
    """
    current = _get(f"/invoices/{invoice_id}")
    body = {
        "contact_id": contact_id or current.get("contact_id"),
        "date": date or current.get("date"),
        "due_date": due_date or current.get("due_date"),
        "notes": notes if notes is not None else current.get("notes"),
        "tags": tags if tags is not None else current.get("tags"),
        "items": items if items is not None else _lines_to_items(current.get("lines", [])),
        "currency": currency or current.get("currency", "EUR"),
        "number_line_id": number_line_id,  # no viene en GET, solo actualizar si se pasa
        "discount": discount if discount is not None else current.get("discount"),
        "payment_method_id": payment_method_id or current.get("payment_method_id"),
    }
    return _put(f"/invoices/{invoice_id}", {k: v for k, v in body.items() if v is not None})


# ── FACTURAS RECIBIDAS (COMPRAS / GASTOS) ─────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def list_purchases(
    start_date: str | None = None,
    end_date: str | None = None,
    contact_id: str | None = None,
    status: str | None = None,
    page: int | None = None,
) -> Any:
    """
    Lista facturas de compra / gastos recibidos.
    start_date / end_date: YYYY-MM-DD. Usar siempre para no agotar el límite mensual.
    """
    return _get_filtered("/purchases", start_date, end_date,
                         contactId=contact_id, status=status, page=page)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_purchase(purchase_id: str) -> Any:
    """Detalle completo de una factura de compra."""
    return _get(f"/purchases/{purchase_id}")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def create_purchase(
    date: str,
    items: list[dict],
    contact_id: str | None = None,
    due_date: str | None = None,
    notes: str | None = None,
    supplier_invoice_number: str | None = None,
    currency: str = "EUR",
    payment_method_id: str | None = None,
    doc_type: str | None = None,
    description: str | None = None,
) -> Any:
    """
    Crea una factura de compra / gasto recibido.
    date: YYYY-MM-DD.
    doc_type: "ticket" para crear un ticket de gasto en lugar de factura de compra.
      Usar "ticket" para gastos sin número de factura del proveedor (cuota autónomos, etc.).
    supplier_invoice_number: número de factura que aparece en el documento del proveedor.
    items: lista de objetos → {name, units, price, taxes: [tax_id], account?, description?}.
      - account: ID de cuenta contable del gasto (obtener con list_expense_accounts).
      - taxes: IDs de impuesto (obtener con list_taxes).
    """
    body = _p(
        contact_id=contact_id,
        date=date,
        due_date=due_date,
        notes=notes,
        description=description,
        number=supplier_invoice_number,
        currency=currency,
        payment_method_id=payment_method_id,
        type=doc_type,
        items=items,
    )
    return _post("/purchases", body)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
def register_purchase_payment(
    purchase_id: str,
    amount: float,
    date: str,
    account_id: str | None = None,
    description: str | None = None,
    confirmed: bool = False,
) -> Any:
    """
    Registra el pago de una factura de compra. Requiere confirmed=True.
    date: YYYY-MM-DD.
    account_id: ID de cuenta bancaria desde donde se paga (treasury_id en la API).
    """
    aviso = _confirm("registrar pago de una compra",
                      f"purchase_id={purchase_id}, importe={amount}, fecha={date}", confirmed)
    if aviso:
        return aviso
    body = _p(amount=amount, date=date, treasury_id=account_id, description=description)
    return _post(f"/purchases/{purchase_id}/payments", body)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def attach_document_to_purchase(purchase_id: str, file_path: str) -> Any:
    """
    Adjunta un PDF a una factura de compra.
    file_path: ruta local al archivo.
    Usado por los workflows n8n de 'Facturas Holded Yeray' y 'facturas desde drive'.
    """
    return _post_file(f"/purchases/{purchase_id}/attachments", file_path)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def update_purchase(
    purchase_id: str,
    contact_id: str | None = None,
    date: str | None = None,
    due_date: str | None = None,
    items: list[dict] | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
    supplier_invoice_number: str | None = None,
    currency: str | None = None,
    payment_method_id: str | None = None,
) -> Any:
    """
    Actualiza cualquier campo de una factura de compra (gasto).
    Solo se modifican los campos que se pasen — el resto se preserva del original.
    date: YYYY-MM-DD. items: [{name, units, price, taxes:[id], account?}].
    supplier_invoice_number: número de factura del proveedor (corregir si se leyó mal).
    """
    current = _get(f"/purchases/{purchase_id}")
    body = {
        "contact_id": contact_id or current.get("contact_id"),
        "date": date or current.get("date"),
        "due_date": due_date or current.get("due_date"),
        "notes": notes if notes is not None else current.get("notes"),
        "tags": tags if tags is not None else current.get("tags"),
        "number": supplier_invoice_number or current.get("document_number"),  # GET devuelve document_number
        "items": items if items is not None else _lines_to_items(current.get("lines", [])),
        "currency": currency or current.get("currency", "EUR"),
        "payment_method_id": payment_method_id or current.get("payment_method_id"),
    }
    return _put(f"/purchases/{purchase_id}", {k: v for k, v in body.items() if v is not None})


# ── PRESUPUESTOS ──────────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def list_estimates(
    start_date: str | None = None,
    end_date: str | None = None,
    contact_id: str | None = None,
    status: str | None = None,
) -> Any:
    """Lista presupuestos con filtros opcionales."""
    return _get_filtered("/estimates", start_date, end_date,
                         contactId=contact_id, status=status)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def create_estimate(
    contact_id: str,
    date: str,
    items: list[dict],
    due_date: str | None = None,
    notes: str | None = None,
    currency: str = "EUR",
    number_line_id: str | None = None,
) -> Any:
    """
    Crea un presupuesto para un cliente.
    date: YYYY-MM-DD.
    items: lista de objetos → {name, units, price, taxes: [tax_id], description?}.
    """
    body = _p(contact_id=contact_id, date=date, due_date=due_date,
              notes=notes, currency=currency, number_line_id=number_line_id, items=items)
    return _post("/estimates", body)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def update_estimate(
    estimate_id: str,
    contact_id: str | None = None,
    date: str | None = None,
    due_date: str | None = None,
    items: list[dict] | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
    number_line_id: str | None = None,
    currency: str | None = None,
) -> Any:
    """
    Actualiza cualquier campo de un presupuesto existente.
    Solo se modifican los campos que se pasen — el resto se preserva del original.
    date / due_date: YYYY-MM-DD. items: [{name, units, price, taxes:[id]}].
    """
    current = _get(f"/estimates/{estimate_id}")
    body = {
        "contact_id": contact_id or current.get("contact_id"),
        "date": date or current.get("date"),
        "due_date": due_date or current.get("due_date"),
        "notes": notes if notes is not None else current.get("notes"),
        "tags": tags if tags is not None else current.get("tags"),
        "items": items if items is not None else _lines_to_items(current.get("lines", [])),
        "currency": currency or current.get("currency", "EUR"),
        "number_line_id": number_line_id,  # no viene en GET, solo actualizar si se pasa
    }
    return _put(f"/estimates/{estimate_id}", {k: v for k, v in body.items() if v is not None})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
def convert_estimate_to_invoice(estimate_id: str, confirmed: bool = False) -> Any:
    """Convierte un presupuesto aceptado en factura emitida. Requiere confirmed=True."""
    aviso = _confirm("convertir presupuesto en factura",
                      f"estimate_id={estimate_id}", confirmed)
    if aviso:
        return aviso
    return _post("/documents/convert",
                 {"source_type": "estimate", "source_id": estimate_id, "target_type": "invoice"})


# ── NOTAS DE ABONO / RECTIFICATIVAS ──────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def list_credit_notes(
    start_date: str | None = None,
    end_date: str | None = None,
    contact_id: str | None = None,
) -> Any:
    """Lista notas de abono / facturas rectificativas."""
    return _get_filtered("/credit-notes", start_date, end_date, contactId=contact_id)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def create_credit_note(
    contact_id: str,
    date: str,
    items: list[dict],
    invoice_id: str | None = None,
    notes: str | None = None,
) -> Any:
    """
    Crea una nota de abono (factura rectificativa).
    invoice_id: ID de la factura original a rectificar (opcional pero recomendado).
    items: mismos campos que create_invoice pero con importes a devolver.
    """
    body = _p(contact_id=contact_id, date=date, items=items,
              invoice_id=invoice_id, notes=notes)
    return _post("/credit-notes", body)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def update_credit_note(
    credit_note_id: str,
    contact_id: str | None = None,
    date: str | None = None,
    items: list[dict] | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
    invoice_id: str | None = None,
) -> Any:
    """
    Actualiza cualquier campo de una nota de abono existente.
    Solo se modifican los campos que se pasen — el resto se preserva del original.
    invoice_id: ID de la factura original vinculada (corregir si se asoció mal).
    """
    current = _get(f"/credit-notes/{credit_note_id}")
    body = {
        "contact_id": contact_id or current.get("contact_id"),
        "date": date or current.get("date"),
        "notes": notes if notes is not None else current.get("notes"),
        "tags": tags if tags is not None else current.get("tags"),
        "items": items if items is not None else _lines_to_items(current.get("lines", [])),
        "invoice_id": invoice_id or current.get("invoice_id"),
    }
    return _put(f"/credit-notes/{credit_note_id}", {k: v for k, v in body.items() if v is not None})


# ── TICKETS SIMPLIFICADOS ─────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def list_sales_receipts(
    start_date: str | None = None,
    end_date: str | None = None,
) -> Any:
    """Lista tickets de venta simplificados (ventas sin datos completos del cliente)."""
    return _get_filtered("/sales-receipts", start_date, end_date)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def create_sales_receipt(
    date: str,
    items: list[dict],
    contact_id: str | None = None,
    notes: str | None = None,
) -> Any:
    """
    Crea un ticket simplificado de venta.
    date: YYYY-MM-DD.
    items: lista de objetos → {name, units, price, taxes: [tax_id]}.
    """
    body = _p(date=date, items=items, contact_id=contact_id, notes=notes)
    return _post("/sales-receipts", body)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def update_sales_receipt(
    receipt_id: str,
    date: str | None = None,
    items: list[dict] | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
    contact_id: str | None = None,
) -> Any:
    """
    Actualiza cualquier campo de un ticket simplificado existente.
    Solo se modifican los campos que se pasen — el resto se preserva del original.
    """
    current = _get(f"/sales-receipts/{receipt_id}")
    body = {
        "date": date or current.get("date"),
        "notes": notes if notes is not None else current.get("notes"),
        "tags": tags if tags is not None else current.get("tags"),
        "items": items if items is not None else _lines_to_items(current.get("lines", [])),
        "contact_id": contact_id or current.get("contact_id"),
    }
    return _put(f"/sales-receipts/{receipt_id}", {k: v for k, v in body.items() if v is not None})


# ── FACTURAS RECURRENTES ──────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def list_recurring_invoices() -> Any:
    """Lista plantillas de facturas recurrentes activas."""
    return _get("/recurring-invoices")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def create_recurring_invoice(
    contact_id: str,
    items: list[dict],
    frequency: str,
    start_date: str,
    end_date: str | None = None,
    notes: str | None = None,
) -> Any:
    """
    Crea una factura recurrente automática.
    frequency: daily | weekly | monthly | yearly.
    start_date: YYYY-MM-DD (primera emisión).
    """
    periodicity = _FREQ.get(frequency, frequency)
    body = _p(contact_id=contact_id, items=items, periodicity=periodicity,
              start_date=start_date, end_date=end_date, notes=notes)
    return _post("/recurring-invoices", body)


# ── CONTACTOS ─────────────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def list_contacts(
    search: str | None = None,
    contact_type: str | None = None,
    page: int | None = None,
) -> Any:
    """
    Lista contactos (clientes y proveedores).
    search: busca por nombre, email o NIF.
    contact_type: client | supplier.
    """
    data = _get("/contacts", name=search, type=contact_type, page=page)
    # Recorte de coste — verificado en vivo 2026-08-30 (ver docs/APIS.md): 50 contactos reales
    # ocupaban 57.300 caracteres, casi todo bloques anidados vacíos o con valores a null
    # (defaults de facturación, bill_address, client_record/supplier_record, social_networks,
    # shipping_addresses/notes/contact_persons casi siempre []). Para identificar/buscar un
    # contacto (lo que promete esta tool) basta con la identidad — el detalle completo sigue
    # disponible en get_contact.
    keep = (
        "id", "name", "code", "trade_name", "vat_number", "email", "phone", "mobile",
        "type", "is_person", "tags", "group_id",
    )
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        data["items"] = [
            {k: v for k, v in c.items() if k in keep} if isinstance(c, dict) else c
            for c in data["items"]
        ]
    return data


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def get_contact(contact_id: str) -> Any:
    """Datos completos de un contacto."""
    return _get(f"/contacts/{contact_id}")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def create_contact(
    name: str,
    email: str | None = None,
    nif: str | None = None,
    phone: str | None = None,
    contact_type: str | None = None,
    is_person: bool = False,
    trade_name: str | None = None,
    address: str | None = None,
    city: str | None = None,
    postal_code: str | None = None,
    country_code: str = "ES",
    notes: str | None = None,
) -> Any:
    """
    Crea un contacto (cliente o proveedor).
    contact_type: client | supplier.
    is_person: True = persona física, False = empresa (default False).
    nif: NIF/CIF/VAT del contacto.
    country_code: código ISO 2 letras (ES, FR, DE...).
    """
    bill_address = _p(address=address, city=city, postal_code=postal_code,
                      country_code=country_code)
    body = _p(name=name, email=email, vat_number=nif, phone=phone,
              type=contact_type, is_person=is_person, trade_name=trade_name)
    if bill_address:
        body["bill_address"] = bill_address
    return _post("/contacts", body)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def update_contact(
    contact_id: str,
    name: str | None = None,
    email: str | None = None,
    nif: str | None = None,
    phone: str | None = None,
    contact_type: str | None = None,
    is_person: bool | None = None,
    trade_name: str | None = None,
    address: str | None = None,
    city: str | None = None,
    postal_code: str | None = None,
    country_code: str | None = None,
    notes: str | None = None,
) -> Any:
    """
    Actualiza datos de un contacto existente. Solo cambia los campos que se pasan; el resto se preserva.
    contact_type: client | supplier | both.
    is_person: True = persona física, False = empresa.
    """
    current = _get(f"/contacts/{contact_id}")
    # Usar el contacto actual como base para no perder campos en el PUT destructivo
    READ_ONLY = {"id", "contactNum", "created", "updated", "shared", "pendingPayments",
                 "pendingCobros", "tags", "social", "defaults", "accounts"}
    body = {k: v for k, v in current.items() if k not in READ_ONLY and v is not None}
    # Aplicar solo los cambios explícitos
    if name is not None:
        body["name"] = name
    if email is not None:
        body["email"] = email
    if nif is not None:
        body["vat_number"] = nif
    if phone is not None:
        body["phone"] = phone
    if contact_type is not None:
        body["type"] = contact_type
    if is_person is not None:
        body["is_person"] = is_person
    if trade_name is not None:
        body["trade_name"] = trade_name
    if notes is not None:
        body["notes"] = notes
    # Actualizar bill_address de forma selectiva (sin borrar los campos no tocados)
    if any(f is not None for f in [address, city, postal_code, country_code]):
        current_addr = current.get("bill_address") or {}
        merged_addr = {**current_addr}
        if address is not None:
            merged_addr["address"] = address
        if city is not None:
            merged_addr["city"] = city
        if postal_code is not None:
            merged_addr["postal_code"] = postal_code
        if country_code is not None:
            merged_addr["country_code"] = country_code
        body["bill_address"] = merged_addr
    return _put(f"/contacts/{contact_id}", body)


# ── TESORERÍA ─────────────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def list_bank_accounts() -> Any:
    """Lista las cuentas bancarias con sus saldos actuales."""
    return _get("/treasury/accounts")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def list_bank_movements(
    account_id: str,
    start_date: str | None = None,
    end_date: str | None = None,
    reconciled: bool | None = None,
) -> Any:
    """
    Lista movimientos de una cuenta bancaria.
    account_id: ID de la cuenta (obtener con list_bank_accounts).
    reconciled: True = solo conciliados | False = solo pendientes de conciliar.
    Nota: el campo de fecha en los movimientos es booking_date (YYYY-MM-DD).
    """
    # La API ignora el parámetro reconciled en query string — filtrado client-side
    data = _get_filtered(
        f"/treasury/accounts/{account_id}/bank-movements",
        start_date, end_date,
        date_field="booking_date",
    )
    if reconciled is not None:
        items = [
            m for m in data.get("items", [])
            if (m.get("status") == "reconciled") == reconciled
        ]
        data = {"items": items, "filtered_count": len(items)}
    return data


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def create_bank_movement(
    account_id: str,
    amount: float,
    date: str,
    description: str,
    movement_type: str = "income",
) -> Any:
    """
    Añade un movimiento bancario manual a una cuenta.
    movement_type: income | expense.
    amount: siempre positivo, el tipo determina si es entrada o salida.
    date: YYYY-MM-DD.
    """
    current_balance = _account_balance(account_id)
    delta = amount if movement_type == "income" else -amount
    new_balance = round(current_balance + delta, 2)
    body = _p(amount=amount, date=date, description=description,
              type=movement_type, balance=new_balance)
    return _post(f"/treasury/accounts/{account_id}/bank-movements", body)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
def reconcile_bank_movement(
    account_id: str,
    movement_id: str,
    document_id: str,
    document_type: str,
    confirmed: bool = False,
) -> Any:
    """
    Concilia un movimiento bancario con una factura o pago. Requiere confirmed=True.
    document_type: invoice | salesreceipt | purchase | creditnote | purchaserefund |
                   payroll | payment | remittance | purchasereceipt | collection | receipt | entry.
    document_id: ID del documento a vincular con el movimiento.
    Nota: en la práctica solo "invoice" produce status "reconciled" con importe correcto.
    "purchase" produce "forced_reconciled" (la API lo acepta pero no enlaza el documento).
    """
    aviso = _confirm("conciliar movimiento bancario",
                      f"account_id={account_id}, movement_id={movement_id}, "
                      f"document_id={document_id} ({document_type})", confirmed)
    if aviso:
        return aviso
    body = {"documents": [{"document_id": document_id, "document_type": document_type}]}
    return _post(
        f"/treasury/accounts/{account_id}/bank-movements/{movement_id}/reconcile",
        body,
    )


# ── CASHFLOW / PREVISIÓN DE TESORERÍA ────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def list_cashflow_forecasts(
    start_date: str | None = None,
    end_date: str | None = None,
) -> Any:
    """Lista previsiones de cobros y pagos vinculadas a documentos."""
    return _get("/treasury/cashflow/invoicing-forecasts",
                start_date=start_date, end_date=end_date)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def create_cashflow_forecast(
    invoice_id: str,
    amount: float,
    issue_date: str,
    due_date: str,
    percentage: float = 100,
) -> Any:
    """
    Crea una previsión de cobro vinculada a una factura emitida.
    invoice_id: ID de la factura (debe estar aprobada).
    amount: importe esperado.
    issue_date: fecha de emisión de la factura (YYYY-MM-DD).
    due_date: fecha esperada de cobro (YYYY-MM-DD).
    percentage: porcentaje del total de la factura (default 100).
    """
    body = {
        "relatedDocumentId": invoice_id,
        "issueDate": issue_date,
        "dueDate": due_date,
        "percentage": percentage,
        "amount": amount,
    }
    return _post("/treasury/cashflow/invoicing-forecasts", body)


# ── REMESAS ───────────────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def list_remittances() -> Any:
    """Lista remesas de cobro SEPA."""
    return _get("/treasury/remittances")


# ── CONFIGURACIÓN ─────────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def list_taxes() -> Any:
    """
    Lista los tipos de impuesto configurados: IVA (21%, 10%, 4%, 0%),
    IRPF (-7%, -15%, -19%), recargo de equivalencia, etc.
    Devuelve los IDs necesarios para crear facturas.
    """
    return _get("/taxes")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def list_services() -> Any:
    """
    Lista el catálogo de servicios de Holded.
    Útil para obtener IDs y precios al crear líneas de factura.
    """
    return _get("/services")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def list_numbering_series(doc_type: str = "invoice") -> Any:
    """
    Lista las series de numeración configuradas.
    doc_type: invoice | purchase | estimate. (credit-note y sales-receipt no son válidos en la API)
    Devuelve los IDs necesarios para el parámetro number_line_id en create_invoice.
    """
    valid = {"invoice", "purchase", "estimate"}
    if doc_type not in valid:
        return {"error": f"doc_type '{doc_type}' no válido. Usar: invoice, purchase, estimate"}
    return _get(f"/numbering-series/{doc_type}")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def list_expense_accounts() -> Any:
    """
    Lista las cuentas de gasto del plan contable.
    Útil para clasificar correctamente los gastos al crear purchases.
    """
    return _get("/expenses-accounts")


# ── MAIN ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
