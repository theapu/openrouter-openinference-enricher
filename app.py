"""
OpenInference enricher for Phoenix.

Two ingest lanes:

* POST /v1/traces            — OpenRouter Broadcast (OpenAI-format JSON in
  span.input / span.output) -> OTel GenAI "parts" schema
  (gen_ai.input.messages / gen_ai.output.messages / gen_ai.system_instructions),
  which Phoenix converts to llm.input_messages.* for the structured view.
  Drops the noisy "provider attempt" child spans.

* POST /anthropic/v1/traces  — Anthropic native OTel (GenAI "parts" already on
  the span). Phoenix converts the *input* automatically but not the *output*, so
  here we parse gen_ai.output.messages and emit llm.output_messages.* plus a
  clean plain-text output.value (no escaped JSON blob, no empty reasoning parts).

Each lane forwards enriched OTLP/JSON to a collector receiver, which converts to
protobuf and exports to the right Phoenix project. Internal-only service.
"""
import gzip
import json
import logging
import os
import httpx
from fastapi import FastAPI, Request, Response, Header

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("enricher")

app = FastAPI()

FORWARD_URL = os.environ.get("FORWARD_URL", "http://otel-collector:4318/v1/traces")
FORWARD_URL_ANTHROPIC = os.environ.get("FORWARD_URL_ANTHROPIC", "http://otel-collector:4321/v1/traces")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN")  # optional; applies to both lanes


# ---------- OTLP-JSON attribute helpers ----------
def sattr(key, s):
    return {"key": key, "value": {"stringValue": s}}

def get_str_attr(attrs, key):
    for a in attrs:
        if a.get("key") == key:
            return a.get("value", {}).get("stringValue")
    return None

def set_str_attr(attrs, key, val):
    """Replace the attribute in place if present, else append (no dup keys)."""
    for a in attrs:
        if a.get("key") == key:
            a["value"] = {"stringValue": val}
            return
    attrs.append(sattr(key, val))


def _parse_args(args):
    if isinstance(args, str):
        try:
            return json.loads(args)
        except Exception:
            return args
    return args


# ================= OpenRouter lane (OpenAI chat -> GenAI parts) =================
def openai_to_parts(messages):
    genai, system_parts = [], []
    for m in messages:
        role = m.get("role")
        content = m.get("content")

        if role == "system":
            if isinstance(content, str):
                system_parts.append({"type": "text", "content": content})
            elif isinstance(content, list):
                for b in content:
                    txt = b.get("text", "") if isinstance(b, dict) else str(b)
                    system_parts.append({"type": "text", "content": txt})
            continue

        if role == "tool":
            resp = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            genai.append({
                "role": "user",
                "parts": [{"type": "tool_call_response", "id": m.get("tool_call_id"),
                           "response": [{"type": "text", "text": resp}]}],
            })
            continue

        parts = []
        if isinstance(content, str) and content:
            parts.append({"type": "text", "content": content})
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    parts.append({"type": "text", "content": b.get("text", "")})
                else:
                    parts.append({"type": "text", "content": json.dumps(b, ensure_ascii=False)})
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            parts.append({"type": "tool_call", "id": tc.get("id"),
                          "name": fn.get("name"), "arguments": _parse_args(fn.get("arguments"))})
        genai.append({"role": "assistant" if role == "assistant" else "user", "parts": parts})
    return genai, system_parts

def output_to_parts(obj):
    parts, comp = [], obj.get("completion")
    for tc in (obj.get("toolCalls") or []):
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        parts.append({"type": "tool_call", "id": tc.get("id") if isinstance(tc, dict) else None,
                      "name": fn.get("name"), "arguments": _parse_args(fn.get("arguments"))})
    if comp:
        parts.append({"type": "text", "content": comp})
    return [{"role": "assistant", "parts": parts}], comp

def enrich_openrouter_span(span):
    attrs = span.setdefault("attributes", [])
    si = get_str_attr(attrs, "span.input")
    so = get_str_attr(attrs, "span.output")
    touched = False
    if si:
        try:
            msgs = json.loads(si).get("messages", [])
            genai, sysp = openai_to_parts(msgs)
            set_str_attr(attrs, "gen_ai.input.messages", json.dumps(genai, ensure_ascii=False))
            if sysp:
                set_str_attr(attrs, "gen_ai.system_instructions", json.dumps(sysp, ensure_ascii=False))
            touched = True
        except Exception as e:
            logger.warning("openrouter input parse failed: %s", e)
    if so:
        try:
            obj = json.loads(so)
            outp, comp = output_to_parts(obj)
            set_str_attr(attrs, "gen_ai.output.messages", json.dumps(outp, ensure_ascii=False))
            if comp:
                set_str_attr(attrs, "output.value", comp)
                set_str_attr(attrs, "output.mime_type", "text/plain")
            touched = True
        except Exception as e:
            logger.warning("openrouter output parse failed: %s", e)
    if touched:
        set_str_attr(attrs, "openinference.span.kind", "LLM")
    return span

def drop_openrouter(span):
    return (span.get("name") or "").startswith("provider attempt")


# ================= Anthropic lane (GenAI parts -> OpenInference output) =========
def _parts_text(parts):
    """Join all text parts, skipping empty reasoning/other parts."""
    return "\n".join(
        p.get("content", "") for p in parts
        if isinstance(p, dict) and p.get("type") == "text" and p.get("content")
    )

def enrich_anthropic_span(span):
    attrs = span.setdefault("attributes", [])
    gom = get_str_attr(attrs, "gen_ai.output.messages")
    if not gom:
        return span
    try:
        msgs = json.loads(gom)
    except Exception as e:
        logger.warning("anthropic output parse failed: %s", e)
        return span
    if not isinstance(msgs, list):
        return span

    first_text = None
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            continue
        parts = m.get("parts") or []
        base = f"llm.output_messages.{i}.message"
        set_str_attr(attrs, f"{base}.role", str(m.get("role", "assistant")))
        text = _parts_text(parts)
        if text:
            set_str_attr(attrs, f"{base}.content", text)
            if first_text is None:
                first_text = text
        j = 0
        for p in parts:
            if isinstance(p, dict) and p.get("type") == "tool_call":
                tb = f"{base}.tool_calls.{j}.tool_call"
                if p.get("id"):
                    set_str_attr(attrs, f"{tb}.id", str(p["id"]))
                if p.get("name"):
                    set_str_attr(attrs, f"{tb}.function.name", str(p["name"]))
                a = p.get("arguments")
                set_str_attr(attrs, f"{tb}.function.arguments",
                             a if isinstance(a, str) else json.dumps(a, ensure_ascii=False))
                j += 1

    # Clean, readable output.value (replaces the {"id","model"} stub)
    if first_text:
        set_str_attr(attrs, "output.value", first_text)
        set_str_attr(attrs, "output.mime_type", "text/plain")
    set_str_attr(attrs, "openinference.span.kind", "LLM")
    return span


# ---------- shared request handling ----------
async def _read_body(request, content_encoding):
    raw = await request.body()
    if content_encoding and "gzip" in content_encoding.lower():
        raw = gzip.decompress(raw)
    return json.loads(raw)

async def _forward(body, url):
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=body, headers={"Content-Type": "application/json"})
    return Response(status_code=r.status_code, content=r.content,
                    media_type=r.headers.get("content-type", "application/json"))

def _auth_ok(authorization):
    return (not INGEST_TOKEN) or authorization == f"Bearer {INGEST_TOKEN}"


@app.get("/healthz")
def health():
    return {"ok": True}


@app.post("/v1/traces")
async def traces_openrouter(request: Request,
                            authorization: str = Header(default=None),
                            content_encoding: str = Header(default=None)):
    if not _auth_ok(authorization):
        return Response(status_code=401, content="Invalid token")
    try:
        body = await _read_body(request, content_encoding)
    except Exception as e:
        logger.warning("openrouter body read failed: %s", e)
        return Response(status_code=400, content="bad body")
    for rs in body.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            ss["spans"] = [enrich_openrouter_span(s) for s in ss.get("spans", []) if not drop_openrouter(s)]
    return await _forward(body, FORWARD_URL)


@app.post("/anthropic/v1/traces")
async def traces_anthropic(request: Request,
                           authorization: str = Header(default=None),
                           content_encoding: str = Header(default=None)):
    if not _auth_ok(authorization):
        return Response(status_code=401, content="Invalid token")
    try:
        body = await _read_body(request, content_encoding)
    except Exception as e:
        logger.warning("anthropic body read failed: %s", e)
        return Response(status_code=400, content="bad body")
    for rs in body.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            ss["spans"] = [enrich_anthropic_span(s) for s in ss.get("spans", [])]
    return await _forward(body, FORWARD_URL_ANTHROPIC)
