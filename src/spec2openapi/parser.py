"""WSDL parsing built on zeep.

Extracts SOAP operations, endpoints, headers, faults, documentation and XSD
facets (from all schema documents, including xsd:import-ed ones) into a
plain intermediate model consumed by the OpenAPI generator.
"""
from __future__ import annotations

import dataclasses
import logging
import os
from typing import Any

from lxml import etree
from zeep import Client, Settings
from zeep.wsdl.bindings.soap import Soap12Binding, SoapBinding

from .errors import ConversionError

logger = logging.getLogger("spec2openapi")

XSD_NS = "http://www.w3.org/2001/XMLSchema"
WSDL_NS = "http://schemas.xmlsoap.org/wsdl/"


class UnsupportedWsdlError(ConversionError):
    """Raised in strict mode when an operation cannot be converted."""


@dataclasses.dataclass
class ParsedFault:
    name: str
    element: Any | None  # zeep Element of the fault detail, if resolvable


@dataclasses.dataclass
class ParsedHeader:
    part: str
    element: Any  # zeep Element


@dataclasses.dataclass
class ParsedOperation:
    name: str
    op_id: str  # unique OpenAPI operationId (may differ on collisions)
    service: str
    port: str
    soap_action: str
    soap_version: str  # "1.1" | "1.2"
    style: str  # "document" | "rpc"
    endpoint: str
    documentation: str | None
    input_element: Any  # zeep xsd Element (wrapper)
    output_element: Any | None
    headers: list[ParsedHeader]
    faults: list[ParsedFault]


@dataclasses.dataclass
class XsdMeta:
    """Metadata zeep does not expose, scraped from the raw schema XML."""

    # (namespace, simpleType name) -> JSON-Schema-ready facet dict
    facets: dict[tuple[str, str], dict[str, Any]]
    # (namespace, container name) -> documentation  (types and global elements)
    type_docs: dict[tuple[str, str], str]
    # (namespace, container name, child element name) -> documentation
    child_docs: dict[tuple[str, str, str], str]
    # (namespace, head element) -> [(namespace, member element), ...]
    # (zeep drops @substitutionGroup entirely)
    substitutions: dict[tuple[str, str], list[tuple[str, str]]] = (
        dataclasses.field(default_factory=dict))
    # global elements declared abstract="true" (zeep drops @abstract too)
    abstract_elements: set[tuple[str, str]] = (
        dataclasses.field(default_factory=set))


@dataclasses.dataclass
class ParsedWsdl:
    source: str
    name: str
    documentation: str | None
    operations: list[ParsedOperation]
    xsd_meta: XsdMeta
    skipped: list[tuple[str, str]]  # (operation, reason)
    # zeep Schema — lets the generator resolve substitution-group members
    schema: Any = None


# ---------------------------------------------------------------------------
# raw XML scraping (facets + documentation)
# ---------------------------------------------------------------------------

# WSDL/XSD documents are untrusted input: never resolve entities, load DTDs,
# or touch the network while parsing (fetching is done explicitly upstream).
_SAFE_XML = etree.XMLParser(
    resolve_entities=False, load_dtd=False, no_network=True, huge_tree=False
)


def _load_raw(location: str, *, allow_remote: bool = True) -> bytes | None:
    try:
        if os.path.exists(location):
            with open(location, "rb") as f:
                return f.read()
        if allow_remote and location.startswith(("http://", "https://")):
            from zeep.transports import Transport

            return Transport().load(location)
    except Exception as exc:  # pragma: no cover - network/IO edge
        logger.debug("could not fetch %s for metadata: %s", location, exc)
    return None


def _doc_text(node: etree._Element) -> str | None:
    doc = node.find(f"{{{XSD_NS}}}annotation/{{{XSD_NS}}}documentation")
    if doc is not None and doc.text and doc.text.strip():
        return " ".join(doc.text.split())
    return None


_FACET_MAP = {
    "pattern": ("pattern", str),
    "minLength": ("minLength", int),
    "maxLength": ("maxLength", int),
    "minInclusive": ("minimum", float),
    "maxInclusive": ("maximum", float),
    "minExclusive": ("exclusiveMinimumValue", float),
    "maxExclusive": ("exclusiveMaximumValue", float),
}


def _facets_from_restriction(st: etree._Element) -> dict[str, Any]:
    """xsd:simpleType node -> JSON Schema facet fragment (3.0 flavored)."""
    out: dict[str, Any] = {}
    restriction = st.find(f"{{{XSD_NS}}}restriction")
    if restriction is None:
        return out
    enums = [
        e.get("value")
        for e in restriction.findall(f"{{{XSD_NS}}}enumeration")
        if e.get("value") is not None
    ]
    if enums:
        out["enum"] = enums
    for facet in restriction:
        if not isinstance(facet.tag, str):
            continue
        local = etree.QName(facet).localname
        value = facet.get("value")
        if value is None:
            continue
        if local == "length":
            try:
                out["minLength"] = out["maxLength"] = int(value)
            except ValueError:
                pass
        elif local == "fractionDigits":
            try:
                digits = int(value)
                if digits > 0:
                    out["multipleOf"] = round(10 ** -digits, digits)
            except ValueError:
                pass
        elif local in _FACET_MAP:
            key, cast = _FACET_MAP[local]
            try:
                out[key] = cast(value)
            except ValueError:
                pass
    # OpenAPI 3.0 exclusive bounds are boolean flags on minimum/maximum
    if "exclusiveMinimumValue" in out:
        out["minimum"] = out.pop("exclusiveMinimumValue")
        out["exclusiveMinimum"] = True
    if "exclusiveMaximumValue" in out:
        out["maximum"] = out.pop("exclusiveMaximumValue")
        out["exclusiveMaximum"] = True
    return out


def _resolve_qname_ref(value: str, node: etree._Element
                       ) -> tuple[str, str] | None:
    """Resolve a QName attribute value ('tns:payment') via the node's
    in-scope namespace map."""
    prefix, sep, local = value.rpartition(":")
    if not sep:
        return (node.nsmap.get(None) or "", value)
    ns = node.nsmap.get(prefix)
    return None if ns is None else (ns, local)


def _scan_schema_root(root: etree._Element, meta: XsdMeta) -> None:
    for schema in root.iter(f"{{{XSD_NS}}}schema"):
        tns = schema.get("targetNamespace", "")
        # global elements: substitution-group membership + abstract flags
        for el in schema.findall(f"{{{XSD_NS}}}element"):
            ename = el.get("name")
            if not ename:
                continue
            if el.get("abstract") in ("true", "1"):
                meta.abstract_elements.add((tns, ename))
            sg = el.get("substitutionGroup")
            if sg:
                head = _resolve_qname_ref(sg, el)
                if head:
                    members = meta.substitutions.setdefault(head, [])
                    if (tns, ename) not in members:
                        members.append((tns, ename))
        # named simple types: facets + docs
        for st in schema.findall(f"{{{XSD_NS}}}simpleType[@name]"):
            key = (tns, st.get("name"))
            facets = _facets_from_restriction(st)
            doc = _doc_text(st)
            if doc:
                meta.type_docs.setdefault(key, doc)
            if facets:
                meta.facets.setdefault(key, dict(facets))
        # named complex types and global elements: docs (own + children)
        for container_tag in ("complexType", "element"):
            for node in schema.findall(f"{{{XSD_NS}}}{container_tag}[@name]"):
                cname = node.get("name")
                doc = _doc_text(node)
                if doc:
                    meta.type_docs.setdefault((tns, cname), doc)
                for child in node.iter(f"{{{XSD_NS}}}element"):
                    if child is node or not child.get("name"):
                        continue
                    cdoc = _doc_text(child)
                    if cdoc:
                        meta.child_docs.setdefault(
                            (tns, cname, child.get("name")), cdoc
                        )


def _collect_xsd_meta(client: Client, source: str,
                      *, forbid_external: bool = False) -> XsdMeta:
    meta = XsdMeta(facets={}, type_docs={}, child_docs={})
    locations: list[str] = []
    try:
        for entry in client.wsdl.types.documents.values():
            docs = entry if isinstance(entry, list) else [entry]
            for d in docs:
                loc = getattr(d, "_location", None)
                if loc and loc not in locations:
                    locations.append(loc)
    except Exception as exc:  # pragma: no cover
        logger.debug("schema document introspection failed: %s", exc)
    if source not in locations:
        locations.append(source)
    for loc in locations:
        # the source itself was chosen by the caller; imported locations
        # are attacker-controllable and honor forbid_external
        raw = _load_raw(loc, allow_remote=(loc == source or not forbid_external))
        if not raw:
            continue
        try:
            root = etree.fromstring(raw, parser=_SAFE_XML)
        except Exception:
            continue
        _scan_schema_root(root, meta)
    # transitive closure: a member of a member substitutes the outer head
    changed = True
    while changed:
        changed = False
        for head, members in meta.substitutions.items():
            for m in list(members):
                for mm in meta.substitutions.get(m, ()):
                    if mm != head and mm not in members:
                        members.append(mm)
                        changed = True
    return meta


def _extract_wsdl_docs(source: str) -> tuple[str | None, dict[str, str]]:
    raw = _load_raw(source)
    if not raw:
        return None, {}
    try:
        tree = etree.fromstring(raw, parser=_SAFE_XML)
    except Exception:
        return None, {}
    ns = {"wsdl": WSDL_NS}
    svc_doc = None
    node = tree.find("wsdl:documentation", ns)
    if node is not None and node.text:
        svc_doc = node.text.strip()
    op_docs: dict[str, str] = {}
    for opel in tree.findall(".//wsdl:portType/wsdl:operation", ns):
        doc = opel.find("wsdl:documentation", ns)
        if doc is not None and doc.text and opel.get("name"):
            op_docs[opel.get("name")] = doc.text.strip()
    return svc_doc, op_docs


# ---------------------------------------------------------------------------
# operation extraction
# ---------------------------------------------------------------------------


def _extract_headers(op: Any) -> list[ParsedHeader]:
    headers: list[ParsedHeader] = []
    header = getattr(op.input, "header", None) if op.input else None
    if header is None:
        return headers
    try:
        for pname, pel in header.type.elements:
            headers.append(ParsedHeader(part=pname, element=pel))
    except Exception as exc:
        logger.warning("could not resolve soap:header of %s: %s",
                       getattr(op, "name", "?"), exc)
    return headers


def _extract_faults(op: Any) -> list[ParsedFault]:
    faults: list[ParsedFault] = []
    fault_messages = getattr(op.abstract, "fault_messages", None) or {}
    for fname, fmsg in fault_messages.items():
        element = None
        for part in (getattr(fmsg, "parts", None) or {}).values():
            element = getattr(part, "element", None)
            if element is not None:
                break
        faults.append(ParsedFault(name=fname, element=element))
    return faults


def parse_wsdl(
    source: str,
    *,
    service: str | None = None,
    port: str | None = None,
    prefer_soap12: bool = False,
    strict: bool = False,
    forbid_external: bool = False,
    huge_tree: bool = False,
) -> ParsedWsdl:
    """Parse a WSDL (path or URL) into the intermediate model.

    Supports document/literal and rpc/literal SOAP bindings; operations that
    cannot be represented are skipped (or raise in strict mode).

    forbid_external=True refuses to fetch remote wsdl:/xsd: imports —
    recommended when converting documents from untrusted sources (local
    relative imports still work). huge_tree=True lifts libxml2 depth/size
    limits for very large WSDLs; leave off for untrusted input.
    """
    # A source that starts with '<' is document content, not a location —
    # catching it here beats the misleading not-a-file error zeep gives
    if isinstance(source, str) and source.lstrip().startswith("<"):
        raise ConversionError(
            "parse_wsdl expects a WSDL file path or URL, but received what "
            "looks like XML content — write it to a file (or serve it over "
            "http) and pass its location"
        )
    # For local files, surface XML syntax errors with a line/column before
    # zeep either swallows them (lenient parse -> misleading "no operations")
    # or reports a cryptic internal error.
    if os.path.exists(source):
        with open(source, "rb") as f:
            _raw = f.read()
        _check = etree.XMLParser(resolve_entities=False, load_dtd=False,
                                 no_network=True, huge_tree=huge_tree)
        try:
            etree.fromstring(_raw, parser=_check)
        except etree.XMLSyntaxError as exc:
            raise ConversionError(f"invalid XML in '{source}': {exc}") from exc

    settings = Settings(
        strict=False, xml_huge_tree=huge_tree,
        forbid_dtd=True, forbid_entities=True,
        forbid_external=forbid_external,
    )
    try:
        client = Client(source, settings=settings)
    except (FileNotFoundError, ConversionError):
        raise
    except Exception as exc:  # malformed/unfetchable WSDL -> clean error
        label = str(source)
        if len(label) > 120:
            label = label[:120] + "…"
        raise ConversionError(
            f"could not parse WSDL '{label}': {exc}"
        ) from exc

    svc_doc, op_docs = _extract_wsdl_docs(source)
    xsd_meta = _collect_xsd_meta(client, source, forbid_external=forbid_external)

    operations: list[ParsedOperation] = []
    skipped: list[tuple[str, str]] = []
    seen: dict[str, Any] = {}  # op name -> input element qname
    used_ids: set[str] = set()
    first_service_name = None

    def sort_key(item):
        _, p = item
        is12 = isinstance(p.binding, Soap12Binding)
        return (not is12) if prefer_soap12 else is12

    for sname, svc in client.wsdl.services.items():
        if service and sname != service:
            continue
        if first_service_name is None:
            first_service_name = sname
        for pname, prt in sorted(svc.ports.items(), key=sort_key):
            if port and pname != port:
                continue
            binding = prt.binding
            if not isinstance(binding, SoapBinding):
                skipped.append((f"{sname}/{pname}", "non-SOAP binding"))
                continue
            soap_version = "1.2" if isinstance(binding, Soap12Binding) else "1.1"
            endpoint = prt.binding_options.get("address", "")
            for opname, op in binding._operations.items():
                style = getattr(op, "style", "document")
                body = getattr(op.input, "body", None) if op.input else None
                if body is None or not hasattr(getattr(body, "type", None), "elements"):
                    reason = (
                        f"style '{style}': input message has no resolvable body "
                        "element (rpc/encoded is not supported)"
                    )
                    if strict:
                        raise UnsupportedWsdlError(f"{opname}: {reason}")
                    skipped.append((opname, reason))
                    continue

                in_qname = getattr(body, "qname", None)
                if opname in seen:
                    if seen[opname] == in_qname:
                        continue  # same operation exposed via another port
                    op_id = f"{sname}_{opname}"
                else:
                    op_id = opname
                base_id = op_id
                n = 2
                while op_id in used_ids:
                    op_id = f"{base_id}_{n}"
                    n += 1

                out_body = None
                if op.output is not None:
                    out_body = getattr(op.output, "body", None)
                    if out_body is not None and not hasattr(
                        getattr(out_body, "type", None), "elements"
                    ):
                        out_body = None

                operations.append(
                    ParsedOperation(
                        name=opname,
                        op_id=op_id,
                        service=sname,
                        port=pname,
                        soap_action=op.soapaction or "",
                        soap_version=soap_version,
                        style=style,
                        endpoint=endpoint,
                        documentation=op_docs.get(opname),
                        input_element=body,
                        output_element=out_body,
                        headers=_extract_headers(op),
                        faults=_extract_faults(op),
                    )
                )
                seen[opname] = in_qname
                used_ids.add(op_id)

    if not operations and strict:
        raise UnsupportedWsdlError("no convertible SOAP operations found")

    for opname, reason in skipped:
        logger.warning("skipped %s: %s", opname, reason)

    return ParsedWsdl(
        source=source,
        name=first_service_name or "SoapService",
        documentation=svc_doc,
        operations=operations,
        xsd_meta=xsd_meta,
        skipped=skipped,
        schema=client.wsdl.types,
    )
