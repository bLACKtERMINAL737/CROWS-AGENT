# Persistent Web Inbox API

This increment adds SQLite-backed conversation and message history on top of the provider-neutral messaging contracts.

## Endpoints

- `GET /messaging/channels`: channels seen in stored conversations. It does not imply a live provider connector is configured.
- `GET /messaging/conversations?limit=50`: recent conversations and last-message preview.
- `GET /messaging/conversations/{conversation_id}/messages?limit=100`: message history.
- `POST /messaging/inbound`: persist a normalized inbound event. Provider adapters must verify webhook signatures and authentication before invoking this endpoint.
- `POST /messaging/outbound`: persist an outbound message intent with an idempotency key.

All routes use the existing API protection middleware. Do not expose this API publicly without configuring `CROWS_API_TOKEN`, TLS, and a deployment-specific access policy. Raw provider user IDs are converted to opaque channel-scoped conversation IDs and are not persisted.

## Delivery semantics and current limits

An inbound event is stored as `accepted`; an outbound request is an intent stored as `accepted`. **Accepted does not mean sent or delivered.** There is no provider send adapter, webhook signature verifier, autonomous messaging worker, or web inbox UI in this slice. Inbound endpoint is a trusted internal integration boundary, not a public webhook endpoint.

## Quick check

Use a local API token when configured:

```sh
curl -H "Authorization: Bearer $CROWS_API_TOKEN" http://127.0.0.1:8000/messaging/conversations
```
