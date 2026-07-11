"""Unit tests for JSON <-> SOAP envelope conversion (no network)."""
from __future__ import annotations

from lxml import etree

from spec2openapi import convert_wsdl
from spec2openapi.bridge import (
    BridgeOptions,
    _SpecIndex,
    build_envelope,
    parse_response,
)

ENV = "http://schemas.xmlsoap.org/soap/envelope/"
ORD = "http://example.com/orders"


def _create_order_op(orders_wsdl):
    spec = convert_wsdl(orders_wsdl)
    index = _SpecIndex(spec)
    return index, index.ops["/operations/CreateOrder"]


def test_build_envelope_structure(orders_wsdl):
    index, op = _create_order_op(orders_wsdl)
    payload = {
        "customer": {"name": "Alice"},
        "items": [
            {"sku": "SKU-1", "quantity": 2, "price": 19.99, "gift": True},
            {"sku": "SKU-2", "quantity": 1, "price": 5.5},
        ],
        "note": "leave at door",
    }
    xml = build_envelope(op, payload, index, BridgeOptions())
    tree = etree.fromstring(xml)
    body = tree.find(f"{{{ENV}}}Body")
    root = body.find(f"{{{ORD}}}CreateOrder")
    assert root is not None

    cust = root.find(f"{{{ORD}}}customer")
    assert cust.findtext(f"{{{ORD}}}name") == "Alice"
    # optional email omitted
    assert cust.find(f"{{{ORD}}}email") is None

    items = root.findall(f"{{{ORD}}}items")
    assert len(items) == 2
    assert items[0].get("gift") == "true"          # attribute serialization
    assert items[1].get("gift") is None
    assert items[0].findtext(f"{{{ORD}}}quantity") == "2"
    assert root.findtext(f"{{{ORD}}}note") == "leave at door"

    # sequence order: customer, items, items, note
    names = [etree.QName(c).localname for c in root]
    assert names == ["customer", "items", "items", "note"]


def test_wsse_header(orders_wsdl):
    index, op = _create_order_op(orders_wsdl)
    opts = BridgeOptions(auth="wsse", username="u1", password="p1")
    xml = build_envelope(op, {"customer": {"name": "A"}, "items": []}, index, opts)
    tree = etree.fromstring(xml)
    wsse = ("http://docs.oasis-open.org/wss/2004/01/"
            "oasis-200401-wss-wssecurity-secext-1.0.xsd")
    assert tree.findtext(f".//{{{wsse}}}Username") == "u1"
    assert tree.findtext(f".//{{{wsse}}}Password") == "p1"


def test_parse_success_response(orders_wsdl):
    index, op = _create_order_op(orders_wsdl)
    resp = f"""<?xml version="1.0"?>
    <soapenv:Envelope xmlns:soapenv="{ENV}"><soapenv:Body>
      <CreateOrderResponse xmlns="{ORD}">
        <orderId>ORD-9</orderId><status>NEW</status>
        <createdAt>2026-01-01T00:00:00Z</createdAt>
      </CreateOrderResponse>
    </soapenv:Body></soapenv:Envelope>""".encode()
    status, data = parse_response(resp, op, index)
    assert status == 200
    assert data == {
        "orderId": "ORD-9",
        "status": "NEW",
        "createdAt": "2026-01-01T00:00:00Z",
    }


def test_parse_nested_response_with_array(orders_wsdl):
    spec = convert_wsdl(orders_wsdl)
    index = _SpecIndex(spec)
    op = index.ops["/operations/GetOrder"]
    resp = f"""<soapenv:Envelope xmlns:soapenv="{ENV}"><soapenv:Body>
      <GetOrderResponse xmlns="{ORD}">
        <orderId>X</orderId>
        <customer><name>Bob</name></customer>
        <items gift="true"><sku>S1</sku><quantity>3</quantity><price>1.5</price></items>
        <items><sku>S2</sku><quantity>1</quantity><price>2</price></items>
        <status>SHIPPED</status>
      </GetOrderResponse>
    </soapenv:Body></soapenv:Envelope>""".encode()
    status, data = parse_response(resp, op, index)
    assert status == 200
    assert data["customer"] == {"name": "Bob"}
    assert data["items"][0] == {"sku": "S1", "quantity": 3, "price": 1.5, "gift": True}
    assert data["items"][1]["quantity"] == 1
    assert data["status"] == "SHIPPED"


def test_parse_fault(orders_wsdl):
    index, op = _create_order_op(orders_wsdl)
    resp = f"""<soapenv:Envelope xmlns:soapenv="{ENV}"><soapenv:Body>
      <soapenv:Fault><faultcode>soapenv:Client</faultcode>
      <faultstring>boom</faultstring></soapenv:Fault>
    </soapenv:Body></soapenv:Envelope>""".encode()
    status, data = parse_response(resp, op, index)
    assert status == 500
    assert data["faultstring"] == "boom"


def test_parse_garbage(orders_wsdl):
    index, op = _create_order_op(orders_wsdl)
    status, data = parse_response(b"<html>gateway error</html>", op, index)
    assert status == 502
    assert "NoBody" in data["faultcode"] or "InvalidXML" in data["faultcode"]
