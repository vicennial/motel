"""Per-binding request policy: deny by default every feature the concierge cannot bound or meter.

The budget logic reserves a worst case from what is *in the request body* (its size, ``max_tokens``, the model). A
feature that makes the upstream use tokens or money that are not in that body breaks the reservation, so a binding refuses
it (403 ``motel_feature_denied``) unless the operator lists it in ``allow``:

``server_state``    server-held context the body does not contain: ``thread``, ``container``, ``previous_response_id``,
                    ``conversation``, a stored ``prompt``, any ``background`` value other than ``false``, and a file the
                    upstream holds, named in a content block, a ``source`` object or a file part (see :func:`_file_ref`);
                    a ``file_id`` that is only data (a tool's ``input_schema``, the ``input`` of a ``tool_use``) is not one;
                    on the OpenAI Responses API an ``input`` item that only names a stored item (``item_reference``, or an
                    item with nothing but ``id`` and ``type``), and a body that does not say ``"store": false`` (a missing
                    ``store`` means true: the response is kept upstream)
``url_sources``     documents or images the upstream fetches itself (``source.type: "url"``, ``image_url``/``file_url``
                    that is not a ``data:`` URL)
``pdf``             a base64 document, or an OpenAI ``file_data`` file: the upstream bills it per page, and a tiny file can hold
                    hundreds of pages. When allowed, the reservation adds 3000 input tokens per page (see :func:`pdf_pages`).
``web_search`` ``web_fetch`` ``code_execution`` ``mcp`` ``file_search`` ``image_generation``
                    server-side tools (Anthropic ``web_search_*``, ``web_fetch_*``, ``code_execution_*``, ``mcp_servers`` and
                    ``mcp_toolset``; OpenAI ``web_search*``, ``code_interpreter``, ``mcp``, ``file_search``,
                    ``image_generation``)
``fast_mode``       a faster, dearer service level: ``speed`` other than standard, OpenAI ``service_tier`` ``priority``/``scale``
``fallbacks``       the ``fallbacks`` parameter (the request may be re-run on another model, at that model's price)
``tool:<type>``     any entry of a ``tools`` list whose ``type`` is not a client-side kind (none, ``custom``, ``function``,
                    ``namespace``) and not one of the families above. Unknown and future tool types are refused unless the
                    operator names the exact type, for example ``tool:bash_20250124``. Server-run tools (tool search, advisor)
                    and Anthropic-defined client tools (bash, text editor, computer, memory) are all in this group.

``anthropic-beta`` and ``openai-beta`` header values must all be in the binding's ``betas`` list (default none).

What an allowed feature costs beyond tokens is *not* in the reservation, except fast mode (reserved and settled at a
2x multiple) and web search (USD 0.01 per search counted at settle when the response reports it). Allow the others
knowing that. The detection is on the parsed JSON, so it sees the same values the upstream will.
"""

from __future__ import annotations

import base64
import binascii
import re

FEATURES = ("server_state", "url_sources", "pdf", "web_search", "web_fetch", "code_execution", "mcp", "file_search", "image_generation",
            "fast_mode", "fallbacks")
# A tool entry with no type is a client tool too. ``namespace`` is the Responses API's grouping of function/custom tools under
# one name (codex 0.160.0 sends ``{"type": "namespace", "name": "functions", "description": "", "tools": [...]}``, captured in
# tests/concierge/fixtures/codex_0_160_0_responses.json; its binary carries the serde types ResponsesApiNamespace and
# ResponsesApiNamespaceTool). It runs nothing by itself, so it is a client tool only while it has exactly those fields: any other
# key (a ``server_url``, ``connector_id``, ``authorization``, ``defer_loading``...) makes it ``tool:namespace``, which a
# binding must allow by name. The tools inside it are checked as every ``tools`` list is.
CLIENT_TOOL_TYPES = frozenset({"custom", "function", "namespace"})
NAMESPACE_KEYS = frozenset({"type", "name", "description", "tools"})
TOOL_TYPE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
PDF_TOKENS_PER_PAGE = 3000  # text plus the page image, at the high end of what the models bill for a page
PDF_UNKNOWN_PAGES = 600  # the most pages a request may hold: assumed whenever the count cannot be read from the file
_PDF_PAGE = re.compile(rb"/Type\s*/Page(?![A-Za-z])")
_PDF_COUNT = re.compile(rb"/Count\s+([0-9]{1,7})")
FAST_MULTIPLIER = 2.0  # Opus 5 and 5.5 fast mode are 2x standard in the model reference; assumed for the others
SERVER_STATE_KEYS = ("thread", "container", "previous_response_id", "conversation")  # plus a stored ``prompt`` object
FILE_BLOCK_TYPES = frozenset({"document", "image", "input_file", "input_image", "file", "container_upload", "search_result"})
# Data that is not a request. A ``file_id`` here is a property name or an argument, not a file the upstream will read.
# Each place is a FIXED position from the root of the body, so a key of the same name anywhere else is still inspected:
# - a tool entry's JSON Schema: ``tools[]`` at the root, or ``input[].tools[]`` (codex's ``additional_tools`` items), with keys
#   ``input_schema``, ``parameters`` or ``schema``, directly or under the entry's ``function`` / ``custom``; a namespace entry
#   holds more tools (``tools[].tools[]``), one level deep;
# - a response format's schema: ``response_format.json_schema.schema``, ``response_format.schema``, ``text.format.schema``,
#   ``output_config.format.schema``;
# - the ``input`` of a ``tool_use`` / ``server_tool_use`` / ``mcp_tool_use`` block that is an entry of ``messages[].content[]``.
OPAQUE_KEYS = frozenset({"input_schema", "parameters", "schema"})
TOOL_CALL_TYPES = frozenset({"tool_use", "server_tool_use", "mcp_tool_use"})
_LIST = "[]"  # a path step for "an item of a list"
_TOOL_HOMES = ((), ("input", _LIST))  # where a tools list may sit
_FORMAT_SCHEMAS = frozenset({("response_format", "json_schema", "schema"), ("response_format", "schema"), ("text", "format", "schema"),
                             ("output_config", "format", "schema")})
_CALL_HOME = ("messages", _LIST, "content", _LIST)


def _tool_schema_paths() -> frozenset:
    paths = set()
    for home in _TOOL_HOMES:
        for levels in (1, 2):  # a tool, or a tool inside a namespace
            entry = home + ("tools", _LIST) * levels
            for key in OPAQUE_KEYS:
                paths.add(entry + (key,))
                for wrapper in ("function", "custom"):
                    paths.add(entry + (wrapper, key))
    return frozenset(paths)


_TOOL_SCHEMAS = _tool_schema_paths()


def _opaque(path: tuple) -> bool:
    """Whether ``path`` (the keys from the root of the body to a value, ``[]`` for a list item) is exactly a schema place."""
    return path in _TOOL_SCHEMAS or path in _FORMAT_SCHEMAS


URL_BLOCKS = ("image_url", "input_image", "input_file", "file")
TOOL_PREFIXES = (("web_search", "web_search"), ("web_fetch", "web_fetch"), ("code_execution", "code_execution"),
                 ("code_interpreter", "code_execution"), ("mcp", "mcp"), ("file_search", "file_search"),
                 ("image_generation", "image_generation"))
FAST_TIERS = ("priority", "scale")
IMAGE_TYPES = ("image", "image_url", "input_image")
IMAGE_ALLOWANCE_TOKENS = 6000  # an image is billed by its pixel size, not its bytes; this covers the largest the models take
MAX_DEPTH = 200


def beta_values(header_values: list[str]) -> list[str]:
    """Every value of ``anthropic-beta`` / ``openai-beta`` headers (comma-separated, possibly repeated)."""
    return [part.strip() for value in header_values for part in value.split(",") if part.strip()]


def denied_betas(allowed: tuple, values: list[str]) -> list[str]:
    return [v for v in values if v not in allowed]


def _tool_feature(kind) -> str | None:
    if isinstance(kind, str):
        for prefix, feature in TOOL_PREFIXES:
            if kind.startswith(prefix):
                return feature
    return None


RESPONSES_PATH = re.compile(r"/responses(/compact)?$")


def is_responses_path(upath: str) -> bool:
    return bool(RESPONSES_PATH.search(upath))


def _stored_item_refs(items) -> bool:
    """Responses ``input`` items that point at something the upstream stored instead of carrying it: ``item_reference``, and an
    item with an ``id`` and nothing else but a ``type`` (no content, no summary, no arguments)."""
    if not isinstance(items, list):
        return False
    for item in items:
        if isinstance(item, dict) and (item.get("type") == "item_reference" or ("id" in item and set(item) <= {"id", "type"})):
            return True
    return False


def detect(obj: dict, *, responses: bool = False) -> tuple[set, int]:
    """The gated features a parsed request body uses, and how many image blocks it carries. ``responses`` is True for a
    Responses API request (``/responses``): its ``store`` must then be ``false``."""
    found: set = set()
    images = 0
    if _stored_item_refs(obj.get("input")) or (responses and obj.get("store") is not False):
        found.add("server_state")
    for key in SERVER_STATE_KEYS:
        if obj.get(key):
            found.add("server_state")
    if isinstance(obj.get("prompt"), dict) or ("background" in obj and obj["background"] is not False):
        found.add("server_state")
    if obj.get("mcp_servers"):
        found.add("mcp")
    if obj.get("fallbacks"):
        found.add("fallbacks")
    if wants_fast(obj):
        found.add("fast_mode")
    stack = [(obj, 0, (), False)]
    while stack:
        node, depth, path, opaque = stack.pop()
        if depth > MAX_DEPTH:
            found.add("server_state")  # absurdly deep: refuse rather than walk
            continue
        if isinstance(node, list):
            stack.extend((item, depth + 1, path + (_LIST,), opaque) for item in node)
            continue
        if not isinstance(node, dict):
            continue
        tools = node.get("tools")
        if isinstance(tools, list):
            found.update(_tool_entries(tools))
        kind = node.get("type")
        if not opaque and _file_ref(node, kind):
            found.add("server_state")
        feature = _tool_feature(kind)
        if feature and depth >= 1:
            found.add(feature)
        if kind in IMAGE_TYPES:
            images += 1
        if kind == "container_upload":
            found.add("server_state")
        if kind in URL_BLOCKS:  # only content blocks: a client tool's own arguments may legitimately contain these keys
            if node.get("file_url"):
                found.add("url_sources")
            image_url = node.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else image_url
            if isinstance(url, str) and url and not url.startswith("data:"):
                found.add("url_sources")
        if _pdf_data(node) is not None:
            found.add("pdf")
        source = node.get("source")
        if isinstance(source, dict) and source.get("type") == "url":
            found.add("url_sources")
        call = isinstance(kind, str) and kind in TOOL_CALL_TYPES and path == _CALL_HOME
        for key, value in node.items():
            if isinstance(value, dict | list):
                here = path + (key,)
                stack.append((value, depth + 1, here, opaque or _opaque(here) or (call and key == "input")))
    return found, images


def _file_ref(node: dict, kind) -> bool:
    """Whether this node names a file the upstream holds: a content block with a ``file_id``, a ``source`` object with
    one, a chat file part ``{"type": "file", "file": {"file_id": ...}}``, or the ``file_ids`` of a code-execution container."""
    if isinstance(kind, str) and kind in FILE_BLOCK_TYPES and node.get("file_id") is not None:
        return True
    if isinstance(node.get("source"), dict) and (node["source"].get("file_id") is not None or node["source"].get("type") == "file"):
        return True
    part = node.get("file")
    if kind == "file" and isinstance(part, dict) and part.get("file_id") is not None:
        return True
    box = node.get("container")
    return isinstance(box, dict) and isinstance(kind, str) and _tool_feature(kind) == "code_execution" and box.get("file_ids") is not None


def _pdf_data(node: dict):
    """For a node that carries a document the upstream bills per page: ``(media_type, base64 text)``; otherwise ``None``.

    That is an Anthropic ``document`` block with a base64 source (or any source of media type ``application/pdf``), and an
    OpenAI file part with ``file_data`` (a data URL or bare base64). Documents given as plain text are tokens in the body
    and are not included."""
    kind = node.get("type")
    source = node.get("source")
    if kind == "document" and isinstance(source, dict):
        media = source.get("media_type")
        media = media.lower() if isinstance(media, str) else ""
        if source.get("type") == "base64" or media == "application/pdf":
            data = source.get("data")
            return media, data if isinstance(data, str) else ""
    data = node.get("file_data")
    if isinstance(data, str) and data:
        if data.startswith("data:"):
            head, _, data = data.partition(",")
            return head[5:].split(";")[0].lower(), data
        return "", data
    return None


def _pages_in(media: str, data: str) -> int:
    if media.startswith("text/"):
        return 0  # a base64 text file is tokens in the body, which the reservation already counts
    try:
        raw = base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return PDF_UNKNOWN_PAGES
    if not raw.lstrip()[:5] == b"%PDF-":
        return PDF_UNKNOWN_PAGES  # not a PDF we can read (or not a PDF): assume the worst
    counted = len(_PDF_PAGE.findall(raw))
    declared = max((int(n) for n in _PDF_COUNT.findall(raw)), default=0)
    pages = max(counted, declared)
    if pages == 0 or b"/ObjStm" in raw:  # objects packed in compressed streams hide their /Type /Page
        pages = max(pages, PDF_UNKNOWN_PAGES)
    return pages


def pdf_pages(obj: dict) -> int:
    """Pages to reserve for all the base64 documents of a request: the ``/Type /Page`` objects counted in each PDF (or its
    declared ``/Count`` if larger), and ``PDF_UNKNOWN_PAGES`` for one whose count cannot be read (no ``%PDF-`` header, bad
    base64, no page objects, or objects hidden in compressed streams)."""
    total = 0
    stack = [(obj, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_DEPTH:
            continue
        if isinstance(node, list):
            stack.extend((item, depth + 1) for item in node)
        elif isinstance(node, dict):
            found = _pdf_data(node)
            if found is not None:
                total += _pages_in(*found)
            stack.extend((value, depth + 1) for value in node.values() if isinstance(value, dict | list))
    return total


def _tool_entries(tools: list) -> set:
    """The gated features named by the entries of one ``tools`` list: allowlist, so an unknown type is a feature of its own."""
    found = set()
    for entry in tools:
        kind = entry.get("type") if isinstance(entry, dict) else "?"
        if kind == "namespace" and isinstance(entry, dict) and set(entry) - NAMESPACE_KEYS:
            found.add("tool:namespace")
            continue
        if kind is None or (isinstance(kind, str) and kind in CLIENT_TOOL_TYPES):
            continue
        family = _tool_feature(kind)
        if family:
            found.add(family)
        elif isinstance(kind, str) and TOOL_TYPE_RE.fullmatch(kind):
            found.add("tool:" + kind)
        else:
            found.add("tool:invalid")
    return found


def refused(found: set, allow) -> list[str]:
    """The detected features a binding does not allow: the fixed ones in their stable order, then ``tool:<type>`` ones."""
    return [f for f in FEATURES if f in found and f not in allow] + sorted(f for f in found if f.startswith("tool:") and f not in allow)


def wants_fast(obj: dict) -> bool:
    speed = obj.get("speed")
    tier = obj.get("service_tier")
    return (isinstance(speed, str) and speed not in ("", "standard")) or (isinstance(tier, str) and tier in FAST_TIERS)


def denied(allow, obj: dict) -> list[str]:
    """Features the request uses that the binding does not allow, in a stable order."""
    found, _ = detect(obj)
    return refused(found, allow)
