# Discord interactions connector

Crows supports signed Discord application interactions through a slash command.
This is deliberately not a Discord Gateway bot: it does not read arbitrary
server messages, monitor channels, or subscribe to member activity.

## Discord application setup

Create a Discord application and configure a slash command named `/crows`
with one required string option named `message` (maximum 2,000 characters).
Set these backend-only environment variables:

- `CROWS_DISCORD_APPLICATION_ID`: the application ID.
- `CROWS_DISCORD_PUBLIC_KEY`: the application's 32-byte Ed25519 public key as 64 hexadecimal characters.
- `CROWS_MESSAGING_TARGET_KEY`: the persistent Fernet key used to encrypt interaction tokens and delivery targets.
- `CROWS_INBOUND_AGENT_WORKER_ENABLED=true`: enable the opt-in inbound agent worker.
- `CROWS_MESSAGING_WORKER_ENABLED=true`: enable the shared outbound worker.

Set the Discord Interactions Endpoint URL to
`https://<your-host>/messaging/webhooks/discord`. Use HTTPS. Never expose the
interaction token, public configuration secrets, or encrypted target key in
frontend code, logs, or source control. Keep the same Fernet key across restarts;
rotating it without migration makes queued replies unreadable.

## Request and reply flow

1. Crows verifies Discord's Ed25519 signature over the exact timestamp and raw request body.
2. Requests with stale timestamps, invalid signatures, mismatched application IDs,
   malformed command fields, or unsupported command shapes are rejected or safely acknowledged.
3. The interaction token and application ID are stored only as an encrypted delivery target.
   The normalized inbox stores the text and opaque channel/user conversation scope.
4. The dispatch journal queues the message using the durable channel session route.
5. The worker generates a text-only answer with no shell, browser, workspace, coding,
   or arbitrary action tools. Inbound content and history are untrusted.
6. The worker queues an idempotent outbound edit to Discord's deferred original response.
   The outbox worker edits the original interaction response; no second public message is sent.

Discord requires a quick acknowledgement. Crows returns a private deferred response
and generates the answer asynchronously. Dispatch completion means the response was
queued, not that Discord confirmed it; inspect the outbound delivery status separately.
Discord interaction tokens expire; delayed or expired delivery may fail and must be reconciled from the outbox. Expired or uncertain work is not replayed automatically.

## Limitations

- Slash-command interactions only; no Discord Gateway, arbitrary channel-message ingestion,
  DMs, attachments, voice, or multi-server bot message listener.
- The command must be registered as `/crows message:<text>`.
- Only text replies up to 2,000 characters are supported.
- The connector is opt-in and requires both inbound and outbound workers.
