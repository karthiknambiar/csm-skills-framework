# REST API

Start the development server after installing the package:

```bash
uvicorn 'csaf.api:create_app' --factory --reload
```

FastAPI publishes interactive OpenAPI documentation at `/docs` and the schema at
`/openapi.json`.

## Routes

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/health` | Runtime version/readiness |
| GET | `/skills` | Discover registered skill metadata |
| POST | `/skills/{skill_name}` | Execute a skill with `{ "input": {...} }` |
| GET | `/customer/{customer_id}/memory` | Inspect customer-scoped memory |

Memory inspection supports repeatable `kinds`, `text`, `latest_only`, and `limit`
query parameters. Skill results include structured output, committed memory
updates, and artifacts. Binary artifact content is base64 encoded in JSON.

Unknown skills return `404`, invalid skill contracts return `422`, and provider
execution failures such as OfficeCLI errors return `502`. FastAPI handles request
schema validation with its standard `422` response.

## Deployment security boundary

CSAF deliberately has no built-in API authentication requirement. Authentication,
authorization, tenant-to-customer access control, request limits, audit logging,
TLS termination, and production storage policy are the host application's
responsibility. This keeps the reusable framework independent of one identity or
deployment provider; it does not make the development API safe for public use.

Do not expose this API to an untrusted network or the public internet directly.
A production host application must enforce identity and customer-level access
before requests reach CSAF, restrict `/docs` and memory-inspection routes as
appropriate, apply request and artifact size limits, and record security-relevant
audit events. Keep SQLite and generated artifacts on access-controlled storage.
CSAF does not require or validate an API key for this boundary; adding a shared
key to examples would not replace proper host-owned authorization.
