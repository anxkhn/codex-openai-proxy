# System Architecture

This document describes the runtime architecture of `codex-openai-proxy` in a way that is portable to any language stack.

## High-Level Component Diagram

```mermaid
flowchart TD
    C1[OpenAI-compatible clients\nOpenAI SDK, curl, apps] -->|HTTP /v1/*| API[Proxy API Server]

    subgraph API_RUNTIME[Proxy Runtime]
        API --> R1[Route layer\n/v1/models\n/v1/responses\n/v1/chat/completions\n/v1/usage\n/v1/balance\n/health]
        R1 --> N1[Normalizer layer\nOpenAI shape mapping]
        R1 --> S1[SSE passthrough layer\nstream=true]
        R1 --> U1[Usage extractor\nreads x-codex-* headers]
    end

    subgraph AUTH[Auth Subsystem]
        A1[Browser OAuth + PKCE] --> A2[Token exchange\nauth.openai.com/oauth/token]
        A2 --> A3[Secure local store\n~/.codex-openai-proxy/auth.json]
        A3 --> A4[Refresh manager\nlock + proactive refresh]
    end

    subgraph UPSTREAM[Codex Upstream]
        C2[chatgpt.com/backend-api/codex/models]
        C3[chatgpt.com/backend-api/codex/responses]
        C4[chatgpt.com/backend-api/codex/chat/completions]
    end

    A4 -->|Authorization: Bearer| R1
    A4 -->|ChatGPT-Account-ID| R1
    R1 -->|GET /models| C2
    R1 -->|POST /responses| C3
    R1 -->|POST /chat/completions| C4
    C2 --> U1
    C3 --> U1
    C4 --> U1
```

## Detailed Request Lifecycle

```mermaid
sequenceDiagram
    autonumber
    participant Client as OpenAI-compatible Client
    participant Proxy as API Server
    participant Auth as Auth Service
    participant Store as Auth Store
    participant OA as auth.openai.com
    participant Codex as chatgpt.com/backend-api/codex

    Client->>Proxy: POST /v1/responses {model,input,stream}
    Proxy->>Auth: ensure_valid_access_token()
    Auth->>Store: load auth.json
    alt token valid
        Store-->>Auth: access_token
    else token expired
        Auth->>OA: POST /oauth/token (refresh_token grant)
        OA-->>Auth: new access_token (+ optional refresh_token)
        Auth->>Store: atomically persist updated auth.json
    end

    Auth-->>Proxy: access_token + account_id
    Proxy->>Codex: POST /responses with Codex headers
    Codex-->>Proxy: JSON or SSE + x-codex-* headers
    Proxy->>Proxy: capture rate-limit snapshot from headers

    alt stream=true
        Proxy-->>Client: pass through event-stream chunks
    else stream=false
        Proxy-->>Client: normalized OpenAI-compatible JSON
    end
```

## Trust Boundaries

```mermaid
flowchart LR
    subgraph LOCAL[Local machine trusted zone]
      P[Proxy process]
      F[Local auth file]
      L[Loopback/LAN listeners]
    end

    subgraph OPENAI[OpenAI services]
      O1[auth.openai.com]
      O2[chatgpt.com backend-api/codex]
    end

    X[Client apps] --> L
    P --> F
    P --> O1
    P --> O2
```

## Headers and Contracts

- Required upstream auth headers: `Authorization: Bearer <access_token>`.
- Workspace scoping header: `ChatGPT-Account-ID` when available.
- Compatibility header: `Openai-Beta: responses=experimental`.
- Codex identity headers: `User-Agent`, `Version`, and `Originator` semantics inherited from Codex conventions.
- Usage telemetry source: `x-codex-*` response headers, cached by the proxy for `/v1/usage` and `/v1/balance`.

## Failure Modes

- `401/403` from upstream: force token refresh once, then retry one time.
- malformed upstream body: return structured proxy error payload.
- missing local auth: return `401` with setup instructions and surface unauthenticated state in `/` page and `/health`.
- OAuth callback issues: browser flow can be bypassed with `setup-non-interactive` import mode.
