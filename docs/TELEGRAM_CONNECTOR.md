# Telegram connector

This integration supports the Telegram Bot API when explicitly configured.

## Configuration

Set these environment variables on the **backend**:

- `CROWS_TELEGRAM_BOT_TOKEN`: token from the official Telegram bot setup.
- `CROWS_TELEGRAM_WEBHOOK_SECRET`: a high-entropy secret used as Telegram's webhook secret token. Telegram allows a constrained character set for this value; configure it to match the provider's documented requirements.

Never commit either value to source control or expose the bot token in the browser.

## Endpoints

- `GET /messaging/telegram/status`: reports whether both settings are present, never returns the values.
- `POST /messaging/webhooks/telegram`: validates `X-Telegram-Bot-Api-Secret-Token`, then persists supported text messages. Unsupported updates are acknowledged and ignored.
- `POST /messaging/telegram/send`: sends a text message through Telegram's official Bot API and persists the resulting status.

The webhook bypasses shared API-token middleware only so Telegram can reach it; it still passes the request-size limit and requires the Telegram secret-token header. Do not configure a public webhook until a strong secret is set. Use HTTPS at the public edge.

## Delivery safety

Telegram send requests are not assumed to be idempotent by Telegram. If a request outcome is unknown, the connector records `unknown` and refuses to automatically resend the same idempotency key. Check the conversation/provider before choosing a new key, or a duplicate message could be sent.

The send endpoint requires the existing Crows API protection. `sent` means Telegram accepted the send request and returned a message ID; it is not a read receipt.

## Limitations

- Only ordinary text messages are handled; edited text messages are ingested as new events and may be deduplicated by provider message ID.
- No media, groups policy, bot command routing, outbound queue worker, or delivery/read receipts are included yet.
- This connector does not grant permission to message users who have not initiated or otherwise authorized contact with the bot.
