"""Convert zeep XSD types into JSON Schema (with OpenAPI `xml` annotations).

The generated schemas serve two purposes:
1. They describe operation inputs/outputs for FastMCP / OpenAPI tooling
   (descriptions, enums, patterns and bounds improve LLM tool usage).
2. Their `xml` annotations carry enough metadata for a SOAP call layer to
   serialize JSON back into a literal XML payload (element names,
   namespaces, attribute markers, text content, ordering = property
   insertion order).
"""
from __future__ import annotations

import datetime
import decimal
import logging
import re
from typing import Any

from zeep import xsd as zx

from .errors import ConversionError
from .openapi import sanitize_name  # noqa: F401  (canonical home; re-export)
from .parser import XsdMeta

logger = logging.getLogger("spec2openapi")

# format hints derived from zeep builtin class names found in the type's MRO
_FORMAT_BY_CLS = {
    "DateTime": "date-time",
    "Date": "date",
    "Time": "time",
    "Base64Binary": "byte",
    "AnyURI": "uri",
    "Duration": "duration",
}


def _py_to_json_type(py: type) -> dict[str, Any]:
    if py is bool:
        return {"type": "boolean"}
    if py is int:
        return {"type": "integer"}
    if py in (float, decimal.Decimal):
        return {"type": "number"}
    if py is datetime.datetime:
        return {"type": "string", "format": "date-time"}
    if py is datetime.date:
        return {"type": "string", "format": "date"}
    if py is datetime.time:
        return {"type": "string", "format": "time"}
    return {"type": "string"}


def _coerce_default(value: str, schema: dict[str, Any]) -> Any:
    t = schema.get("type")
    try:
        if t == "integer":
            return int(value)
        if t == "number":
            return float(value)
        if t == "boolean":
            return value in ("true", "1")
    except ValueError:
        pass
    return value


def _choice_groups(t: Any) -> list[dict[str, Any]]:
    """Walk zeep's indicator tree to find xsd:choice member groups.

    Handles a choice at any level, including the common top-level
    ``<complexType><choice>`` case, and choices whose branches are
    ``<sequence>``s (the branch's leaf element names are flattened into
    the group so mutually-exclusive fields are not all marked required).
    """
    groups: list[dict[str, Any]] = []

    def leaf_names(node) -> list[str]:
        """Every element name reachable under an indicator, flattening
        nested sequences/groups (used to gather a choice's members)."""
        names: list[str] = []
        try:
            items = list(node)
        except TypeError:
            return names
        for item in items:
            if isinstance(item, tuple) and item:
                names.append(str(item[0]))
                continue
            n = getattr(item, "name", None)
            if n:
                names.append(n)
            else:  # nested indicator with no name (Sequence/Choice/...)
                names.extend(leaf_names(item))
        return names

    def walk(node):
        cls = type(node).__name__
        if cls == "Choice":
            names = leaf_names(node)
            if names:
                try:
                    required = int(getattr(node, "min_occurs", 1)) >= 1
                except (TypeError, ValueError):
                    required = True
                groups.append({"members": names, "required": required})
            return  # members already collected across all branches
        if cls not in ("Sequence", "All", "Group"):
            return
        try:
            items = list(node)
        except TypeError:
            return
        for item in items:
            if isinstance(item, tuple):
                continue
            if type(item).__name__ in ("Choice", "Sequence", "All", "Group"):
                walk(item)

    root = getattr(t, "_element", None)
    if root is not None:
        walk(root)
    return groups


class SchemaConverter:
    """Stateful converter that accumulates shared component schemas."""

    def __init__(self, meta: XsdMeta | None = None, zschema: Any = None):
        self.meta = meta or XsdMeta(facets={}, type_docs={}, child_docs={})
        # zeep Schema for resolving substitution-group member elements
        self.zschema = zschema
        self.components: dict[str, dict[str, Any]] = {}
        self._registered: dict[int, str] = {}  # id(zeep type) -> component name
        self._in_progress: dict[int, str] = {}

    # -- metadata lookups -------------------------------------------------

    def _lookup(self, table: dict, ns: str | None, name: str | None):
        if not name:
            return None
        if ns is not None and (ns, name) in table:
            return table[(ns, name)]
        for key, value in table.items():
            if key[-1] == name or key[1] == name:
                if len(key) == 2 and key[1] == name:
                    return value
        return None

    def _child_doc(self, qkey: tuple[str, str] | None, child: str) -> str | None:
        if qkey is None:
            return None
        ns, container = qkey
        doc = self.meta.child_docs.get((ns, container, child))
        if doc:
            return doc
        for (kns, kcont, kchild), value in self.meta.child_docs.items():
            if kcont == container and kchild == child:
                return value
        # inherited elements (xsd:extension): the doc lives on the base type
        for (kns, _kcont, kchild), value in self.meta.child_docs.items():
            if kns == ns and kchild == child:
                return value
        return None

    def _type_qkey(self, t: Any) -> tuple[str | None, str | None]:
        qname = getattr(t, "qname", None)
        if qname is not None:
            return qname.namespace, qname.localname
        return None, getattr(t, "name", None)

    # -- public ------------------------------------------------------------

    def element_type_to_object_schema(
        self, xsd_type: Any, hint: str, qkey: tuple[str, str] | None = None
    ) -> dict[str, Any]:
        """Schema for a wrapper element's complex type, always inlined as an
        object (used for request/response bodies)."""
        if isinstance(xsd_type, zx.ComplexType):
            return self._complex_to_schema(xsd_type, hint, qkey=qkey)
        return {
            "type": "object",
            "properties": {"value": self._simple_to_schema(xsd_type)},
            "required": ["value"],
        }

    def register_element_component(self, element: Any, hint: str) -> str:
        """Register a standalone element's type (headers, fault details)."""
        t = getattr(element, "type", None)
        if isinstance(t, zx.ComplexType):
            return self._register_complex(t, hint)
        name = sanitize_name(hint)
        if name not in self.components:
            self.components[name] = self._simple_to_schema(t)
        return name

    # -- internals -----------------------------------------------------------

    def _type_to_schema(self, xsd_type: Any, hint: str) -> dict[str, Any]:
        if isinstance(xsd_type, zx.ComplexType):
            name = self._register_complex(xsd_type, hint)
            return {"$ref": f"#/components/schemas/{name}"}
        return self._simple_to_schema(xsd_type)

    def _register_complex(self, t: Any, hint: str) -> str:
        tid = id(t)
        if tid in self._registered:
            return self._registered[tid]
        if tid in self._in_progress:  # recursion cycle
            return self._in_progress[tid]

        base = sanitize_name(getattr(t, "name", None) or hint)
        name = base
        i = 2
        while name in self.components:
            name = f"{base}_{i}"
            i += 1
        self._in_progress[tid] = name
        self.components[name] = {}  # reserve slot to keep ordering stable
        try:
            tns, tname = self._type_qkey(t)
            qkey = (tns or "", tname) if tname else None
            self.components[name] = self._complex_to_schema(t, name, qkey=qkey)
            self._registered[tid] = name
        finally:
            self._in_progress.pop(tid, None)
        return name

    def _complex_to_schema(
        self, t: Any, hint: str, qkey: tuple[str, str] | None = None
    ) -> dict[str, Any]:
        props: dict[str, Any] = {}
        required: list[str] = []
        allow_additional = False

        elements = list(getattr(t, "elements", []))
        attributes = list(getattr(t, "attributes", []))

        # xsd:simpleContent: a text value plus attributes
        is_simple_content = (
            len(elements) == 1
            and elements[0][0] == "_value_1"
            and not isinstance(elements[0][1].type, zx.ComplexType)
        )
        if is_simple_content:
            value_schema = self._simple_to_schema(elements[0][1].type)
            value_schema["xml"] = {"x-text": True}
            value_schema.setdefault(
                "description", "Text content of the element."
            )
            props["value"] = value_schema
            required.append("value")
            elements = []

        for el_name, el in elements:
            if el is None or type(el).__name__ in ("Any", "AnyObject"):
                # xsd:any accepts arbitrary content; allow it through as
                # unconstrained additional properties and surface a warning
                allow_additional = True
                logger.warning(
                    "xsd:any inside %s: emitting additionalProperties:true "
                    "(arbitrary content is not schema-constrained)", hint
                )
                continue
            prop = self._element_to_property(el_name, el, hint, qkey)
            if prop is None:
                continue
            props[el_name] = prop
            min_occurs = getattr(el, "min_occurs", 1)
            try:
                if int(min_occurs) >= 1:
                    required.append(el_name)
            except (TypeError, ValueError):
                required.append(el_name)

        for at_name, attr in attributes:
            if attr is None or type(attr).__name__ == "AnyAttribute":
                continue
            aschema = self._simple_to_schema(getattr(attr, "type", None))
            # zeep mangles the dict key when an element and an attribute
            # share a name (e.g. "attr__id"); the wire name is attr.name
            real_name = (
                getattr(getattr(attr, "qname", None), "localname", None)
                or getattr(attr, "name", None)
                or at_name
            )
            aschema["xml"] = {"name": real_name, "attribute": True}
            props[at_name] = aschema
            if getattr(attr, "required", False):
                required.append(at_name)

        schema: dict[str, Any] = {"type": "object", "properties": props}
        if allow_additional:
            schema["additionalProperties"] = True
        if required:
            schema["required"] = required

        # xsd:choice groups: members must not be required; record the groups
        choices = _choice_groups(t)
        if choices:
            member_set = {m for g in choices for m in g["members"]}
            if "required" in schema:
                schema["required"] = [
                    r for r in schema["required"] if r not in member_set
                ]
                if not schema["required"]:
                    del schema["required"]
            schema["x-soap-choice"] = choices
            notes = []
            for g in choices:
                kind = "Exactly one" if g["required"] else "At most one"
                notes.append(f"{kind} of: {', '.join(g['members'])}.")
            schema["description"] = " ".join(
                filter(None, [schema.get("description"), *notes])
            )

        if is_simple_content:
            schema["x-soap-simple-content"] = True

        # documentation from the raw XSD
        tns, tname = self._type_qkey(t)
        doc = self._lookup(self.meta.type_docs, tns, tname)
        if doc:
            existing = schema.get("description")
            schema["description"] = (
                f"{doc} {existing}" if existing else doc
            )
        return schema

    def _substitution_property(
        self, el: Any, el_name: str, qkey: tuple[str, str] | None
    ) -> dict | None:
        """A ref to a substitution-group head becomes a oneOf of
        self-describing single-property branches ({"creditCard": {...}}),
        so the JSON itself names the wire element. Returns None when the
        element is not a substitution head."""
        qname = getattr(el, "qname", None)
        if qname is None:
            return None
        key = (qname.namespace or "", qname.localname)
        member_keys = self.meta.substitutions.get(key)
        if member_keys is None or self.zschema is None:
            return None

        candidates = list(member_keys)
        if key not in self.meta.abstract_elements:
            candidates.insert(0, key)  # a concrete head substitutes itself

        branches: list[dict[str, Any]] = []
        members_meta: list[dict[str, Any]] = []
        for ns, local in candidates:
            if (ns, local) in self.meta.abstract_elements:
                continue  # abstract members cannot appear on the wire
            try:
                member = self.zschema.get_element(f"{{{ns}}}{local}")
            except Exception:
                logger.warning(
                    "substitution member {%s}%s could not be resolved; "
                    "branch omitted", ns, local,
                )
                continue
            comp = self.register_element_component(member, hint=local)
            ref = f"#/components/schemas/{comp}"
            branches.append({
                "type": "object",
                "properties": {
                    local: {
                        "allOf": [{"$ref": ref}],
                        "xml": {"name": local, "namespace": ns},
                    }
                },
                "required": [local],
            })
            members_meta.append(
                {"element": local, "namespace": ns, "schema": ref}
            )

        marker = {
            "head": key[1],
            "namespace": key[0],
            "members": members_meta,
        }
        doc = self._child_doc(qkey, el_name)
        if not branches:
            return {
                "description": (
                    f"substitution head '{key[1]}' is abstract and has no "
                    "known concrete members; no representable value"
                ),
                "x-soap-substitution": marker,
            }
        names = ", ".join(m["element"] for m in members_meta)
        inner: dict[str, Any] = {
            "oneOf": branches,
            "description": doc or (
                f"One of the substitutable elements for '{key[1]}': {names}. "
                "Pass exactly one key naming the chosen element."
            ),
            "x-soap-substitution": marker,
        }

        max_occurs = getattr(el, "max_occurs", 1)
        is_array = max_occurs == "unbounded"
        if not is_array:
            try:
                is_array = int(max_occurs) > 1
            except (TypeError, ValueError):
                is_array = False
        if not is_array:
            return inner
        arr: dict[str, Any] = {"type": "array", "items": inner}
        try:
            mn = int(getattr(el, "min_occurs", 0))
            if mn > 0:
                arr["minItems"] = mn
        except (TypeError, ValueError):
            pass
        if max_occurs != "unbounded":
            try:
                arr["maxItems"] = int(max_occurs)
            except (TypeError, ValueError):
                pass
        return arr

    def _element_to_property(
        self,
        el_name: str,
        el: Any,
        parent_hint: str,
        qkey: tuple[str, str] | None,
    ) -> dict | None:
        el_type = getattr(el, "type", None)
        if el_type is None:
            raise ConversionError(
                f"element '{el_name}' in '{parent_hint}' has an unresolvable "
                "XSD type; cannot generate a schema property for it. The "
                "WSDL/XSD may reference a type that was not imported or is "
                "not supported. Fix the source schema, or exclude the "
                "operation."
            )

        sub = self._substitution_property(el, el_name, qkey)
        if sub is not None:
            return sub

        base = self._type_to_schema(el_type, hint=f"{parent_hint}_{el_name}")

        qname = getattr(el, "qname", None)
        xml_meta: dict[str, Any] = {"name": el_name}
        if qname is not None and getattr(qname, "namespace", None):
            xml_meta["name"] = qname.localname
            xml_meta["namespace"] = qname.namespace
        if getattr(el, "nillable", False) and "$ref" not in base:
            base["nullable"] = True

        doc = self._child_doc(qkey, el_name)

        default = getattr(el, "default", None)
        if default is not None and "$ref" not in base:
            base["default"] = _coerce_default(default, base)

        max_occurs = getattr(el, "max_occurs", 1)
        is_array = max_occurs == "unbounded"
        if not is_array:
            try:
                is_array = int(max_occurs) > 1
            except (TypeError, ValueError):
                is_array = False

        if is_array:
            items = base
            if "$ref" not in items:
                items = dict(items)
                items["xml"] = dict(xml_meta)
            arr: dict[str, Any] = {"type": "array", "items": items, "xml": xml_meta}
            try:
                mn = int(getattr(el, "min_occurs", 0))
                if mn > 0:
                    arr["minItems"] = mn
            except (TypeError, ValueError):
                pass
            if max_occurs != "unbounded":
                try:
                    arr["maxItems"] = int(max_occurs)
                except (TypeError, ValueError):
                    pass
            if doc:
                arr["description"] = doc
            return arr

        if "$ref" in base:
            out: dict[str, Any] = {"allOf": [base], "xml": xml_meta}
            if doc:
                out["description"] = doc
            return out
        base["xml"] = xml_meta
        if doc and "description" not in base:
            base["description"] = doc
        return base

    def _simple_to_schema(self, t: Any) -> dict[str, Any]:
        if t is None:
            return {"type": "string"}
        cls_names = [k.__name__ for k in type(t).__mro__]
        if type(t).__name__ in ("AnyType", "AnySimpleType"):
            return {}

        accepted = getattr(t, "accepted_types", None) or []
        py = None
        for cand in accepted:
            if isinstance(cand, type):
                py = cand
                break
        schema = _py_to_json_type(py or str)

        for cls in cls_names:
            fmt = _FORMAT_BY_CLS.get(cls)
            if fmt:
                schema.setdefault("format", fmt)
                break

        tns, tname = self._type_qkey(t)
        builtin_names = {"string", "int", "integer", "decimal", "boolean",
                         "float", "double", "dateTime", "date", "time",
                         "long", "short", "byte", "anyURI", "base64Binary"}
        if tname and tname not in builtin_names:
            facets = self._lookup(self.meta.facets, tns, tname)
            if facets:
                for k, v in facets.items():
                    schema.setdefault(k, v)
            doc = self._lookup(self.meta.type_docs, tns, tname)
            if doc:
                schema.setdefault("description", doc)
            schema["x-soap-simple-type"] = tname
        return schema
