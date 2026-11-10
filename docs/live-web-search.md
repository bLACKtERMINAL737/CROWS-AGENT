# Live web search in Crows

Crows registers `web.search` and `web.open` tools. For current, recent, unfamiliar, or otherwise unverified questions, the model should search first, open relevant public pages, synthesize evidence into a direct explanation, include clickable Markdown links to the exact source URLs, and explain what each source supports. It should note uncertainty or conflicting sources rather than presenting snippets as guaranteed truth.

## How it works

- Uses the `ddgs` Python package to query public DuckDuckGo web results.
- `web.search` returns a bounded list of titles, clickable source URLs, snippets, and a UTC search timestamp.
- `web.open` fetches a public HTML/plain-text page and extracts readable text, capped at 16,000 characters, so the model can explain source content rather than relying only on snippets.
- Limits each request to 1–8 results and a 10-second backend timeout.
- Does not require a separate search API key.
- Search can be rate-limited or temporarily unavailable; when that happens, the tool reports failure and the assistant should say it could not verify the answer.

## Setup

Install or update project dependencies in the active virtual environment:

```bash
pip install -e ".[dev]"
```

Restart Crows after installing dependencies. Existing provider configuration remains unchanged.

## Privacy and trust

Search queries are sent to an external search service. Send only the minimal public search terms needed; never search using API keys, passwords, private personal details, or confidential workspace contents. Web results are untrusted content and can be wrong or malicious. For important claims, verify the source page and prefer authoritative sources. A search snippet alone is not proof that a claim is true.

`web.open` supports direct public HTTP(S) URLs, blocks non-public host addresses and redirects, disables environment proxy use, and rejects unsupported content types. It does not execute JavaScript, bypass paywalls, or guarantee access to every site. Search availability depends on the third-party service and network connectivity.
