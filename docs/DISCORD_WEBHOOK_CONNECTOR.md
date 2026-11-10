# Discord incoming webhook connector

This integration sends text messages to one backend-configured Discord incoming webhook. It does not connect to Discord's Gateway. Signed slash-command intake is documented separately in [Discord interactions](DISCORD_INTERACTIONS.md).

## Configuration

Set `CROWS_DISCORD_WEBHOOK_URL` on the backend only. Treat the URL as a credential: do not commit it, return it from APIs, or expose it in frontend code. The connector accepts HTTPS URLs on Discord's official webhook hosts with the expected `/api/webhooks/` path. Requests never supply an arbitrary target URL, reducing SSRF risk.

## Endpoints

- `GET /messaging/discord/status`: reports whether a valid webhook URL is configured without returning it.
- `POST /messaging/discord/send`: send one text message (up to 2,000 characters) and persist the provider response.
- The existing messaging inbox shows the resulting conversation and status.

## Delivery semantics

A successful webhook response with a Discord message ID is recorded as `sent`; this is not a read receipt. Failed responses are recorded as `failed`; transport failures are recorded as `unknown`. Reusing an idempotency key will not automatically resend a message with an unknown outcome. Inspect Discord and the inbox before retrying.

## Limitations

- One configured webhook destination only.
- No Discord Gateway listener, arbitrary channel-message ingestion, attachments, embeds, or multi-server webhook destination management.
- Keep the endpoint protected by the existing Crows API authentication and only send content to destinations you are authorized to use.
