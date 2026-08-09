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

The development API does not implement authentication or authorization. Do not
expose it publicly until an application adds identity, customer-level access
control, request limits, audit logging, and production storage configuration.
