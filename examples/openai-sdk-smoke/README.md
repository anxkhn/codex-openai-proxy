# OpenAI SDK Smoke Apps

These are end-to-end compatibility smoke apps for this proxy using official OpenAI SDKs.

They verify:

- `models.list`
- `responses.create` non-stream
- `responses.create` stream

## Run Python smoke app

```bash
BASE_URL="http://127.0.0.1:8787/v1" uv run --with openai python examples/openai-sdk-smoke/python_smoke.py
```

## Run JavaScript smoke app

```bash
cd examples/openai-sdk-smoke
npm install
BASE_URL="http://127.0.0.1:8787/v1" npm run smoke:js
```

## Optional env vars

- `BASE_URL` (default `http://127.0.0.1:8787/v1`)
- `OPENAI_API_KEY` (default `placeholder`)
- `MODEL` (default `gpt-5`)
