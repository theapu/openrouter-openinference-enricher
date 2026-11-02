"""
OpenRouter -> OpenInference enricher.

Receives OTLP/HTTP JSON (from an OpenTelemetry Collector that fronts OpenRouter
Broadcast), converts each LLM span's OpenAI-format span.input / span.output into
the OTel GenAI "parts" schema (gen_ai.input.messages / gen_ai.output.messages /
gen_ai.system_instructions). Arize Phoenix (>=15.10) then converts those to
llm.input_messages.* and renders the friendly structured message / tool-call view,
identical to native-OTel providers.

Also drops the noisy "provider attempt" child spans. Forwards the enriched
OTLP/JSON on to FORWARD_URL (normally back into the collector, which converts to
protobuf and exports to Phoenix). Designed to run INTERNALLY; it does not need to
be exposed to the internet.
"""
import json
import os
import httpx
from fastapi import FastAPI, Request, Response, Header

app = FastAPI()

FORWARD_URL = os.environ.get("FORWARD_URL", "http://otel-collector:4318/v1/traces")
INGEST_TOKEN = os.environ.get("INGEST_TOKEN")  # optional; if set, require this bearer


# ---------- OTLP-JSON attribute helpers ----------
def sattr(key, s):
    return {"key": key, "value": {"stringValue": s}}

def get_str_attr(attrs, key):
    for a in attrs:
        if a.get("key") == key:
            return a.get("value", {}).get("stringValue")
    return None


# ---------- OpenAI chat -> GenAI "parts" schema ----------
def _parse_args(args):
    if isinstance(args, str):
        try:
            return json.loads(args)
        except Exception:
            return args
    return args

def openai_to_parts(messages):
    """Return (genai_messages, system_parts)."""
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
                "parts": [{"type": "tool_call_response",
                           "id": m.get("tool_call_id"),
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
            parts.append({"type": "tool_call",
                          "id": tc.get("id"),
                          "name": fn.get("name"),
                          "arguments": _parse_args(fn.get("arguments"))})
        genai.append({"role": "assistant" if role == "assistant" else "user", "parts": parts})
    return genai, system_parts

def output_to_parts(obj):
    parts, comp = [], obj.get("completion")
    for tc in (obj.get("toolCalls") or []):
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        parts.append({"type": "tool_call",
                      "id": tc.get("id") if isinstance(tc, dict) else None,
                      "name": fn.get("name"),
                      "arguments": _parse_args(fn.get("arguments"))})
    if comp:
        parts.append({"type": "text", "content": comp})
    return [{"role": "assistant", "parts": parts}], comp


def enrich_span(span):
    attrs = span.setdefault("attributes", [])
    si = get_str_attr(attrs, "span.input")
    so = get_str_attr(attrs, "span.output")
    touched = False

    if si:
        try:
            msgs = json.loads(si).get("messages", [])
            genai, sysp = openai_to_parts(msgs)
            attrs.append(sattr("gen_ai.input.messages", json.dumps(genai, ensure_ascii=False)))
            if sysp:
                attrs.append(sattr("gen_ai.system_instructions", json.dumps(sysp, ensure_ascii=False)))
            touched = True
        except Exception:
            pass

    if so:
        try:
            obj = json.loads(so)
            outp, comp = output_to_parts(obj)
            attrs.append(sattr("gen_ai.output.messages", json.dumps(outp, ensure_ascii=False)))
            if comp:
                attrs.append(sattr("output.value", comp))
                attrs.append(sattr("output.mime_type", "text/plain"))
            touched = True
        except Exception:
            pass

    if touched:
        attrs.append(sattr("openinference.span.kind", "LLM"))
    return span


def drop(span):
    return (span.get("name") or "").startswith("provider attempt")


@app.get("/healthz")
def health():
    return {"ok": True}


@app.post("/v1/traces")
async def traces(request: Request, authorization: str = Header(default=None)):
    if INGEST_TOKEN and authorization != f"Bearer {INGEST_TOKEN}":
        return Response(status_code=401, content="Invalid token")
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=400, content="bad json")

    for rs in body.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            ss["spans"] = [enrich_span(s) for s in ss.get("spans", []) if not drop(s)]

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(FORWARD_URL, json=body,
                              headers={"Content-Type": "application/json"})
    return Response(status_code=r.status_code, content=r.content,
                    media_type=r.headers.get("content-type", "application/json"))
