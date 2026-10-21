# Typefully

Stores an API v2 key through the existing write-only Credentials UI and HTTP API.
The plugin exposes social-set discovery, draft listing, and health checks. The
operator CLI proxies drafts, queue windows, media upload, draft patching, and
draft creation through the agent API so the key stays server-side. Scheduling
stays opt-in: `create-draft` rejects `publish_at`/`plan_at` unless
`--allow-schedule` is passed (PUBLISH key permission enforced provider-side);
`patch-draft` can schedule or attach `media_ids` to an existing draft.

```sh
dapier credentials set typefully --file -
dapier connections test typefully
dapier connections discover typefully social_sets
dapier typefully social-sets
dapier typefully drafts --social-set-id 182343
dapier typefully draft 12345 --social-set-id 182343
dapier typefully queue --start-date 2026-10-19 --end-date 2026-10-26
dapier typefully create-draft --file draft.json   # omit publish_at / plan_at
dapier typefully create-draft --file sched.json --allow-schedule  # publish_at/plan_at allowed
dapier typefully upload-media --file pic.png --alt-text "alt"     # prints media_id + status
dapier typefully media-status <media_id>
dapier typefully patch-draft 12345 --file patch.json  # media_ids / publish_at / plan_at
```

Attach media by putting the returned `media_id` in a post's `media_ids` array
(`platforms.<platform>.posts[].media_ids`) in the create/patch JSON, after
`media-status` reports `ready`. Upload output never includes the signed upload
URL; only `media_id`, file name, and status are printed.

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
