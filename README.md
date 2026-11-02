# openrouter-openinference-enricher

A tiny OTLP trace enricher that makes **OpenRouter Broadcast** traces render with the
full structured message / tool-call view in **Arize Phoenix** (and any OpenInference backend).

## Why

OpenRouter Broadcast emits the whole conversation as one OpenAI-format JSON string
(`span.input` / `span.output`). Phoenix only renders the friendly, role-by-role
message and tool-call view from the OTel GenAI **"parts"** schema. This service parses
that JSON and rewrites each LLM span's input/output into:

- `gen_ai.input.messages`
- `gen_ai.output.messages`
- `gen_ai.system_instructions`

(in the GenAI parts schema), sets `openinference.span.kind=LLM`, and drops the noisy
`provider attempt` child spans. Phoenix (>=15.10) then converts those to
`llm.input_messages.*` and renders the structured view.

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

## Run

```sh
docker run -d --name openrouter-openinference-enricher \
  -e FORWARD_URL=http://otel-collector:4319/v1/traces \
  theapu/openrouter-openinference-enricher:latest
```

### Environment

- `FORWARD_URL` — where to POST the enriched OTLP/JSON (default `http://otel-collector:4318/v1/traces`).
- `INGEST_TOKEN` — optional. If set, requests must carry `Authorization: Bearer <token>`.

## Endpoints

- `POST /v1/traces` — OTLP/HTTP JSON in, enriched OTLP/HTTP JSON forwarded on.
- `GET /healthz` — liveness.

## Releasing

Tag the repo and GitHub Actions builds and pushes the image to Docker Hub:

```sh
git tag v1.0.0
git push origin v1.0.0
```

Produces `<user>/openrouter-openinference-enricher:1.0.0`, `:1.0`, and `:latest`.

## License

MIT — see [LICENSE](LICENSE).
