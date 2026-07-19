"""Tiny in-process SOAP server used by the test suite.

Implements the two fixture services (calculator.wsdl, orders.wsdl) with
strict namespace checks so serialization bugs in the bridge fail loudly.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lxml import etree

ENV = "http://schemas.xmlsoap.org/soap/envelope/"
CALC_NS = "http://example.com/calculator"
ORD_NS = "http://example.com/orders"


def _envelope(inner_xml: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="utf-8"?>'
        f'<soapenv:Envelope xmlns:soapenv="{ENV}"><soapenv:Body>'
        f"{inner_xml}"
        f"</soapenv:Body></soapenv:Envelope>"
    ).encode("utf-8")


def _fault(message: str) -> bytes:
    return _envelope(
        "<soapenv:Fault><faultcode>soapenv:Client</faultcode>"
        f"<faultstring>{message}</faultstring></soapenv:Fault>"
    )


def _text(req: etree._Element, ns: str, *path: str) -> str:
    node = req
    for p in path:
        node = node.find(f"{{{ns}}}{p}")
        if node is None:
            raise ValueError(f"missing element {'/'.join(path)}")
    return node.text or ""


def handle_calc(name: str, req: etree._Element) -> tuple[bytes, int]:
    if name == "Add":
        a = int(_text(req, CALC_NS, "a"))
        b = int(_text(req, CALC_NS, "b"))
        return _envelope(
            f'<AddResponse xmlns="{CALC_NS}"><result>{a + b}</result></AddResponse>'
        ), 200
    if name == "Divide":
        num = float(_text(req, CALC_NS, "numerator"))
        den = float(_text(req, CALC_NS, "denominator"))
        if den == 0:
            return _fault("Division by zero"), 500
        return _envelope(
            f'<DivideResponse xmlns="{CALC_NS}">'
            f"<result>{num / den}</result></DivideResponse>"
        ), 200
    return _fault(f"unknown operation {name}"), 500


def handle_orders(name: str, req: etree._Element) -> tuple[bytes, int]:
    if name == "CreateOrder":
        customer = _text(req, ORD_NS, "customer", "name")
        items = req.findall(f"{{{ORD_NS}}}items")
        if not items:
            return _fault("no items"), 500
        # verify nested item structure is namespace-qualified
        for it in items:
            _text(it, ORD_NS, "sku")
        gift_flags = [it.get("gift") for it in items]
        note = req.find(f"{{{ORD_NS}}}note")
        note_part = (
            f"<note xmlns=\"{ORD_NS}\">{note.text}</note>" if note is not None else ""
        )
        _ = note_part  # response schema has no note; kept for debugging
        _ = customer
        gift_count = sum(1 for g in gift_flags if g == "true")
        return _envelope(
            f'<CreateOrderResponse xmlns="{ORD_NS}">'
            f"<orderId>ORD-{len(items)}-G{gift_count}</orderId>"
            f"<status>NEW</status>"
            f"<createdAt>2026-01-01T00:00:00Z</createdAt>"
            f"</CreateOrderResponse>"
        ), 200
    if name == "GetOrder":
        oid = _text(req, ORD_NS, "orderId")
        return _envelope(
            f'<GetOrderResponse xmlns="{ORD_NS}">'
            f"<orderId>{oid}</orderId>"
            f"<customer><name>Alice</name><email>alice@example.com</email></customer>"
            f'<items gift="true"><sku>SKU-1</sku><quantity>2</quantity>'
            f"<price>19.99</price></items>"
            f"<items><sku>SKU-2</sku><quantity>1</quantity>"
            f"<price>5.5</price></items>"
            f"<status>SHIPPED</status>"
            f"</GetOrderResponse>"
        ), 200
    return _fault(f"unknown operation {name}"), 500


MATH_NS = "http://example.com/math"
ADV_NS = "http://example.com/adv"


def handle_math(name: str, req: etree._Element) -> tuple[bytes, int]:
    if name == "Multiply":
        # rpc/literal: part accessors are unqualified
        a = int(req.findtext("a"))
        b = int(req.findtext("b"))
        return _envelope(
            f'<m:MultiplyResponse xmlns:m="{MATH_NS}">'
            f"<result>{a * b}</result></m:MultiplyResponse>"
        ), 200
    return _fault(f"unknown operation {name}"), 500


def handle_app(name: str, req: etree._Element) -> tuple[bytes, int]:
    if name == "SubmitApplication":
        payment = req.find(f"{{{ADV_NS}}}payment")
        if payment is None or payment.get("currency") is None:
            return _fault("payment currency attribute missing"), 500
        amount = (payment.text or "").strip()
        email = req.find(f"{{{ADV_NS}}}email")
        phone = req.find(f"{{{ADV_NS}}}phone")
        if (email is None) == (phone is None):  # choice: exactly one
            return _fault("exactly one of email/phone required"), 500
        name_txt = req.findtext(f"{{{ADV_NS}}}applicant/{{{ADV_NS}}}name") or "?"
        return _envelope(
            f'<SubmitApplicationResponse xmlns="{ADV_NS}">'
            f"<applicationId>APP-{name_txt}-{payment.get('currency')}-{amount}"
            f"</applicationId><score>87.5</score>"
            f"</SubmitApplicationResponse>"
        ), 200
    return _fault(f"unknown operation {name}"), 500


STRESS_NS = "http://example.com/stress"
STRESS2_NS = "http://example.com/stress2"


def _tree_stats(node: etree._Element) -> tuple[int, int]:
    """(depth, total nodes) of a qualified Node tree."""
    children = node.findall(f"{{{STRESS_NS}}}children")
    if not children:
        return 1, 1
    stats = [_tree_stats(c) for c in children]
    return 1 + max(d for d, _ in stats), 1 + sum(t for _, t in stats)


def handle_stress(req: etree._Element) -> tuple[bytes, int]:
    q = etree.QName(req)
    if q.namespace == STRESS_NS and q.localname == "EchoTree":
        root = req.find(f"{{{STRESS_NS}}}root")
        if root is None:
            return _fault("missing root"), 500
        depth, total = _tree_stats(root)
        return _envelope(
            f'<EchoTreeResponse xmlns="{STRESS_NS}">'
            f"<depth>{depth}</depth><total>{total}</total>"
            f"</EchoTreeResponse>"
        ), 200
    if q.namespace == STRESS_NS and q.localname == "Classify":
        code = req.findtext(f"{{{STRESS_NS}}}code")
        sku = req.findtext(f"{{{STRESS_NS}}}item/{{{STRESS_NS}}}sku")
        value = req.findtext(
            f"{{{STRESS_NS}}}deep/{{{STRESS_NS}}}l2/{{{STRESS_NS}}}l3/"
            f"{{{STRESS_NS}}}l4/{{{STRESS_NS}}}value"
        )
        ok = "true" if all([code, sku, value]) else "false"
        return _envelope(
            f'<ClassifyResponse xmlns="{STRESS_NS}"><ok>{ok}</ok>'
            f"</ClassifyResponse>"
        ), 200
    if q.namespace == STRESS_NS and q.localname == "Ping":
        msg = req.findtext(f"{{{STRESS_NS}}}msg") or ""
        return _envelope(
            f'<PingResponse xmlns="{STRESS_NS}"><msg>{msg}</msg></PingResponse>'
        ), 200
    if q.namespace == STRESS2_NS and q.localname == "Ping":
        # elementFormDefault="unqualified": children carry NO namespace
        msg = req.findtext("msg")
        if msg is None:
            return _fault("unqualified msg element not found"), 500
        code = req.findtext("item/code") or "-"
        return _envelope(
            f'<s2:PingResponse xmlns:s2="{STRESS2_NS}">'
            f"<msg>B:{msg}:{code}</msg></s2:PingResponse>"
        ), 200
    return _fault(f"unknown stress operation {q.localname}"), 500


class SoapHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(length)
        try:
            tree = etree.fromstring(data)
            body = tree.find(f"{{{ENV}}}Body")
            req = next(c for c in body if isinstance(c.tag, str))
            name = etree.QName(req).localname
            if self.path == "/calc":
                payload, status = handle_calc(name, req)
            elif self.path == "/orders":
                payload, status = handle_orders(name, req)
            elif self.path == "/math":
                payload, status = handle_math(name, req)
            elif self.path == "/app":
                payload, status = handle_app(name, req)
            elif self.path == "/stress":
                payload, status = handle_stress(req)
            elif self.path == "/pay":
                payload, status = handle_pay(name, req)
            else:
                payload, status = _fault(f"unknown path {self.path}"), 500
        except Exception as exc:  # bad request from the bridge = test failure signal
            payload, status = _fault(f"server error: {exc}"), 500
        self.send_response(status)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def start_server() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), SoapHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


PAY_NS = "http://example.com/pay"


def handle_pay(name: str, req: etree._Element) -> tuple[bytes, int]:
    """Substitution groups: the wire must carry the member element itself
    (never the abstract head); the response exercises the concrete head."""
    if name != "PayRequest":
        return _fault(f"unexpected operation {name}"), 500
    if req.find(f"{{{PAY_NS}}}payment") is not None:
        return _fault("abstract head <payment> must not appear on the wire"), 500
    member = amount = None
    for tag in ("creditCard", "bankTransfer", "visaCard"):
        node = req.find(f"{{{PAY_NS}}}{tag}")
        if node is not None:
            member = tag
            amount = node.findtext(f"{{{PAY_NS}}}amount")
            break
    if member is None:
        return _fault("no substitutable payment member found"), 500
    return _envelope(
        f'<PayResponse xmlns="{PAY_NS}"><ok>true</ok>'
        f"<urgentNotice><text>paid {amount} via {member}</text>"
        f"<deadline>friday</deadline></urgentNotice></PayResponse>"
    ), 200
