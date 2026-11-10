# Inbound Telegram and Discord agent replies

Crows can optionally turn verified Telegram updates and signed Discord slash
commands into plain-text agent replies. Both connectors enqueue normalized
messages into the same durable dispatch journal and use the same tool-free
agent runtime. Inbound content is treated as untrusted data; the worker has no
workspace, shell, browser, coding, or external-action tools. Other messaging
channels remain inbox-only until explicitly registered and equipped with an
outbound transport.

## Required configuration

Set a persistent Fernet key for encrypted provider delivery destinations. Keep
the same key across restarts and backups; changing it makes existing encrypted
destinations unreadable.

```bash
export CROWS_MESSAGING_TARGET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
```

Enable both workers:

```bash
export CROWS_INBOUND_AGENT_WORKER_ENABLED=true
export CROWS_MESSAGING_WORKER_ENABLED=true
```

Configure the credentials for each enabled connector: Telegram bot token and
webhook secret, and/or Discord application ID and Ed25519 public key. Also
configure at least one model route. Keep API and webhook secrets out of source
control and logs.
The webhook returns HTTP 503 when the encryption key is missing; it does not
accept a message that cannot be safely routed for a reply.

## Processing model

1. The Telegram webhook secret or Discord Ed25519 request signature is verified
   before inbox persistence.
2. The inbox stores the text and an encrypted reply destination; the dispatch
   journal stores the channel-scoped session route.
3. The inbound worker claims the oldest pending item for a conversation. Only
   one item per conversation can be claimed at a time, preserving reply order.
4. The worker renews its lease while generating a response, loads session
   history, and calls the model router without any tools.
5. The user/assistant turn is persisted and a deterministic-idempotency outbound
   message is queued. The shared outbox worker selects the configured provider
   transport for Telegram or Discord.
6. Unconfigured channels fail closed. Expired leases become `unknown` and are
   never replayed automatically.

The dispatch `completed` state means the response was generated and durably
queued; it does not mean Telegram confirmed delivery. Check the outbound
message status separately. An uncertain dispatch must be inspected against the
session history and outbound journal before any manual recovery.

## Operational notes

- The shared agent runtime supports Telegram text and Discord slash-command text.
- Discord is slash-command only; it is not a Gateway listener for arbitrary channel messages.
- The reply policy registry is an explicit allowlist with per-channel reply limits.
  Adding a policy alone does not implement a provider webhook or outbound transport.
- It does not execute tools or perform actions requested in inbound messages.
- Do not rotate `CROWS_MESSAGING_TARGET_KEY` without a deliberate migration.
- The worker is opt-in and starts only when
  `CROWS_INBOUND_AGENT_WORKER_ENABLED=true`.
