# openrouter-openinference-enricher

A tiny OTLP trace enricher that makes **OpenRouter Broadcast** *and* **Anthropic native-OTel**
traces render with the full structured message / tool-call view in **Arize Phoenix**
(and any OpenInference backend). Two ingest lanes, one container.

## Why

**OpenRouter lane (`POST /v1/traces`).** OpenRouter Broadcast emits the whole conversation
as one OpenAI-format JSON string (`span.input` / `span.output`). Phoenix only renders the
friendly, role-by-role view from the OTel GenAI **"parts"** schema. This lane parses that JSON
and rewrites each LLM span's input/output into `gen_ai.input.messages` /
`gen_ai.output.messages` / `gen_ai.system_instructions`, sets `openinference.span.kind=LLM`,
and drops the noisy `provider attempt` child spans. Phoenix (>=15.10) then converts those to
`llm.input_messages.*` and renders the structured view.

**Anthropic lane (`POST /anthropic/v1/traces`).** Anthropic's native OTel already carries the
GenAI parts, and Phoenix converts the *input* automatically — but **not the output** (it leaves
a `{"id","model"}` stub in `output.value`). This lane parses `gen_ai.output.messages`, emits
`llm.output_messages.*` (role + text + tool calls) and a clean plain-text `output.value`
(dropping empty `reasoning` parts and the escaped-JSON blob), so the Output panel shows the
actual answer.

## Architecture

Designed to sit **internally** between two OpenTelemetry Collector pipelines, so it is
never exposed to the internet:

```
OpenRouter (cloud) -> Caddy -> collector :4318 (public)
                                   | pipeline-1: export OTLP/JSON
                                   v
                         openrouter-openinference-enricher :8080  (internal)
                                   | forwards enriched OTLP/JSON
                                   v
                         collector :4319 (internal receiver)
                                   | pipeline-2: export protobuf
                                   v
                                Phoenix
```

## Wiring into an existing OpenTelemetry Collector

The enricher sits between **two** collector pipelines: the public pipeline exports the
raw OpenRouter traces to the enricher as OTLP/**JSON**; the enricher forwards the
enriched traces back into a second internal receiver, which exports them to Phoenix.

Add to your collector `config.yaml`:

```yaml
extensions:
  bearertokenauth/openrouter:        # optional: auth the public ingest
    scheme: "Bearer"
    token: "${env:OPENROUTER_INGEST_TOKEN}"

receivers:
  otlp/in:                           # public ingest from OpenRouter (via your reverse proxy)
    protocols:
      http:
        endpoint: 0.0.0.0:4318
        auth:
          authenticator: bearertokenauth/openrouter
  otlp/back:                         # internal: enriched traces returning from this container
    protocols:
      http:
        endpoint: 0.0.0.0:4319

processors:
  batch: {}

exporters:
  otlphttp/enrich:
    traces_endpoint: http://enricher:8080/v1/traces   # this container
    encoding: json                                     # IMPORTANT: send JSON, not protobuf
  otlphttp/phoenix:
    traces_endpoint: ${env:PHOENIX_OTLP_ENDPOINT}      # e.g. http://phoenix:6006/v1/traces
    headers:
      Authorization: "Bearer ${env:PHOENIX_API_KEY}"
      x-project-name: "openrouter"

service:
  extensions: [bearertokenauth/openrouter]
  pipelines:
    traces/in:                       # OpenRouter -> enricher
      receivers: [otlp/in]
      processors: [batch]
      exporters: [otlphttp/enrich]
    traces/out:                      # enricher -> Phoenix (protobuf)
      receivers: [otlp/back]
      processors: [batch]
      exporters: [otlphttp/phoenix]
```

Then run this container with `FORWARD_URL` pointing back at the collector's `otlp/back`
receiver:

```sh
docker run -d --name enricher \
  -e FORWARD_URL=http://otel-collector:4319/v1/traces \
  theapu/openrouter-openinference-enricher:latest
```

Key points:
- The exporter to the enricher **must** use `encoding: json` (the enricher parses JSON;
  Phoenix separately requires protobuf, which the `otlphttp/phoenix` exporter sends by default).
- `bearertokenauth` needs the **contrib** collector distribution
  (`otel/opentelemetry-collector-contrib`); drop it if you don't need ingest auth.
- `${env:...}` values are read from the collector's own environment at runtime.

## Deploy (Docker Compose / Portainer)

Two compose files are provided:

- [`docker-compose.yml`](docker-compose.yml) — **full stack**: the enricher **and** a
  pre-wired OpenTelemetry Collector. Use this if you don't already run a collector.
- [`docker-compose.enricher-only.yml`](docker-compose.enricher-only.yml) — **just the
  enricher**, for when you already run your own collector (wire it per the section above).

### Full stack

The full [`docker-compose.yml`](docker-compose.yml) brings up the enricher **and**
an OpenTelemetry Collector wired in the internal topology above (collector config is
embedded, so it works pasted into Portainer's web editor). Set these as Portainer
**stack environment variables**:

| Variable | Example | Purpose |
|---|---|---|
| `OPENROUTER_INGEST_TOKEN` | `s3cret…` | Shared secret OpenRouter sends (`Authorization: Bearer …`) |
| `PHOENIX_OTLP_ENDPOINT` | `http://192.168.255.169:6006/v1/traces` | Your Phoenix OTLP endpoint |
| `PHOENIX_API_KEY` | `…` | Phoenix System API key |
| `IMAGE` | `theapu/openrouter-openinference-enricher:v1.0.0` | (optional) pin the enricher image |

Then front the collector with Caddy (`otel.apu.ltd { reverse_proxy <host>:4318 }`) and
set OpenRouter Broadcast's **OpenTelemetry Collector** destination to
`https://otel.apu.ltd/v1/traces` with header `Authorization: Bearer <OPENROUTER_INGEST_TOKEN>`.

## Run (standalone)

```sh
docker run -d --name openrouter-openinference-enricher \
  -e FORWARD_URL=http://otel-collector:4319/v1/traces \
  theapu/openrouter-openinference-enricher:latest
```

### Environment

- `FORWARD_URL` — where to POST the enriched **OpenRouter** OTLP/JSON (default `http://otel-collector:4318/v1/traces`).
- `FORWARD_URL_ANTHROPIC` — where to POST the enriched **Anthropic** OTLP/JSON (default `http://otel-collector:4321/v1/traces`).
- `INGEST_TOKEN` — optional. If set, requests on **both** lanes must carry `Authorization: Bearer <token>`.

## Endpoints

- `POST /v1/traces` — OpenRouter lane. OTLP/HTTP JSON in, enriched OTLP/HTTP JSON forwarded to `FORWARD_URL`.
- `POST /anthropic/v1/traces` — Anthropic lane. OTLP/HTTP JSON in, enriched OTLP/HTTP JSON forwarded to `FORWARD_URL_ANTHROPIC`.
- `GET /healthz` — liveness.

## Anthropic lane — collector wiring

Point Anthropic's native OTel at the collector (own receiver + token), export it to the
enricher's `/anthropic` endpoint, and return it to a receiver that exports to the Anthropic
Phoenix project:

```yaml
receivers:
  otlp/anthropic:              # public ingest from Anthropic (own bearer token)
    protocols: { http: { endpoint: 0.0.0.0:4320, auth: { authenticator: bearertokenauth/anthropic } } }
  otlp/back_anthropic:         # internal: enriched Anthropic traces coming back
    protocols: { http: { endpoint: 0.0.0.0:4321 } }
exporters:
  otlphttp/enrich_anthropic:
    traces_endpoint: http://openrouter-enricher:8080/anthropic/v1/traces
    encoding: json
    headers: { Authorization: "Bearer ${env:INGEST_TOKEN}" }   # if INGEST_TOKEN set
  otlphttp/phoenix_anthropic:
    traces_endpoint: ${env:PHOENIX_OTLP_ENDPOINT}
    headers: { Authorization: "Bearer ${env:PHOENIX_API_KEY}", x-project-name: "Anthropic" }
service:
  pipelines:
    traces/anthropic:          # Anthropic -> enricher
      receivers: [otlp/anthropic]
      processors: [batch]
      exporters: [otlphttp/enrich_anthropic]
    traces/anthropic_out:      # enricher -> Phoenix (Anthropic project)
      receivers: [otlp/back_anthropic]
      processors: [batch]
      exporters: [otlphttp/phoenix_anthropic]
```
Enricher env: `FORWARD_URL_ANTHROPIC=http://otel-collector:4321/v1/traces`.

## Releasing

Tag the repo and GitHub Actions builds and pushes the image to Docker Hub:

```sh
git tag v1.0.0
git push origin v1.0.0
```

Produces `<user>/openrouter-openinference-enricher:1.0.0`, `:1.0`, and `:latest`.

## License

GPL-3.0 — see [LICENSE](LICENSE).
