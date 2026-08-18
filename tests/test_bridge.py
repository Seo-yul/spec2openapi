"""Unit tests for JSON <-> SOAP envelope conversion (no network)."""
from __future__ import annotations

import logging

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
ADV = "http://example.com/adv"


def _create_order_op(orders_wsdl):
    spec = convert_wsdl(orders_wsdl)
    index = _SpecIndex(spec)
    return index, index.ops["/operations/CreateOrder"]


def _submit_application_op(advanced_wsdl):
    spec = convert_wsdl(advanced_wsdl)
    index = _SpecIndex(spec)
    return index, index.ops["/operations/SubmitApplication"]


_SUBMIT_PAYLOAD = {
    "applicant": {"id": "P1", "name": "Alice"},
    "payment": {"value": 12.5, "currency": "USD"},
    "email": "a@example.com",
}


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


def test_soap_header_serialized_when_supplied_by_part_name(advanced_wsdl):
    """x-soap.headers declares AuthHeader (part="header"); when the value
    is supplied via BridgeOptions.soap_headers (keyed by part name), it
    must be rendered into <soap:Header> with the declared element QName
    (issue #140 F2)."""
    index, op = _submit_application_op(advanced_wsdl)
    opts = BridgeOptions(soap_headers={"header": {"apiKey": "secret-123"}})
    xml = build_envelope(op, _SUBMIT_PAYLOAD, index, opts)
    tree = etree.fromstring(xml)
    header = tree.find(f"{{{ENV}}}Header")
    assert header is not None
    auth = header.find(f"{{{ADV}}}AuthHeader")
    assert auth is not None
    assert auth.findtext(f"{{{ADV}}}apiKey") == "secret-123"


def test_soap_header_serialized_when_supplied_by_element_name(advanced_wsdl):
    """The same lookup also accepts the element name as the key (not just
    the WSDL part name), per the brief's "part name or element name"."""
    index, op = _submit_application_op(advanced_wsdl)
    opts = BridgeOptions(soap_headers={"AuthHeader": {"apiKey": "by-element"}})
    xml = build_envelope(op, _SUBMIT_PAYLOAD, index, opts)
    tree = etree.fromstring(xml)
    auth = tree.find(f"{{{ENV}}}Header/{{{ADV}}}AuthHeader")
    assert auth is not None
    assert auth.findtext(f"{{{ADV}}}apiKey") == "by-element"


def test_soap_header_missing_value_warns_once_and_omits(advanced_wsdl, caplog):
    """A declared soap:header with no value supplied must not fail
    silently: it logs a warning (once, not per-call) and the envelope is
    still sent without the header rather than fabricating one."""
    index, op = _submit_application_op(advanced_wsdl)
    opts = BridgeOptions()  # no soap_headers configured
    with caplog.at_level(logging.WARNING, logger="spec2openapi"):
        xml = build_envelope(op, _SUBMIT_PAYLOAD, index, opts)
        build_envelope(op, _SUBMIT_PAYLOAD, index, opts)  # same opts: no repeat warning

    tree = etree.fromstring(xml)
    assert tree.find(f"{{{ENV}}}Header") is None  # omitted, not fabricated

    warnings = [r.getMessage() for r in caplog.records
                if r.levelno == logging.WARNING and "AuthHeader" in r.getMessage()]
    assert len(warnings) == 1, warnings


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
