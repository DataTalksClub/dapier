# Typefully

Stores an API v2 key through the existing write-only Credentials UI and HTTP API.
The plugin exposes social-set discovery, draft listing, and health checks. The
operator CLI proxies drafts, queue windows, and **unscheduled** draft creation
through the agent API so the key stays server-side. Scheduling and publish are
not exposed; they need explicit approval if added later.

```sh
dapier credentials set typefully --file -
dapier connections test typefully
dapier connections discover typefully social_sets
dapier typefully social-sets
dapier typefully drafts --social-set-id 182343
dapier typefully draft 12345 --social-set-id 182343
dapier typefully queue --start-date 2026-10-19 --end-date 2026-10-26
dapier typefully create-draft --file draft.json   # omit publish_at / plan_at
```

Default social set is DataTalksClub `182343`. Pipe only the raw key to stdin;
do not put it in arguments, URLs, workflow YAML, or logs. The credential value
is never returned by overview or save responses.
API reference: https://typefully.com/docs/api (v2 Bearer authentication).
