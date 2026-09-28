# Mailchimp connector

Mailchimp does not use an OAuth client. Dapier stores a Mailchimp Marketing
API v3 key and calls the API with it (basic auth, the key as the password).
The key's datacenter suffix selects the server: a key like `abc123…-us12`
targets `https://us12.api.mailchimp.com/3.0`, and Dapier derives the server
from the suffix when it is not stored separately. Dapier rejects a key
without a `-usNN` suffix, so never strip it.

## Connect it

1. In your Mailchimp account, create an API key. It ends with a datacenter
   suffix such as `-us12`.
2. Store it in Dapier with the CLI or the console:

   ```sh
   printf 'your-key-us12' | uv run dapier credentials set mailchimp --file -
   ```

   The value is read from a file or stdin, travels only in the request body,
   and is never displayed again. In the console, use **Credentials →
   Mailchimp** and paste the key.

3. Verify it. `mailchimp` is a pseudo connection — a name that resolves the
   stored key without a connection record — so the shared health check works
   on it directly:

   ```sh
   uv run dapier connections test mailchimp
   ```

   The check pings the Marketing API and answers
   `Mailchimp API key verified (datacenter us12)`. The console's
   **Credentials** view has the same test button. Discovery works on the
   pseudo connection too: `dapier connections discover mailchimp` lists
   `audiences`, and `dapier connections discover mailchimp members
   --param list_id=<audience id>` lists one audience's members (up to 100).

Rotate the key by storing a new one the same way; it is live immediately.

Advanced: actions and triggers accept an optional `connection_id`. A real
Mailchimp connection record can name its own credential ID; without one,
everything falls back to the shared `mailchimp` credential.

## Webhook triggers

A Mailchimp trigger binds one audience (`list_id`) to a delivery URL:

```
https://<dapier-host>/hooks/mailchimp/<trigger-name>
```

Save the trigger with the console's **Triggers** view (kind **Mailchimp**,
audience ID, subscribed events) or the CLI:

```sh
uv run dapier hooks save mailchimp-hook.json
```

```json
{
  "name": "digest-audience",
  "kind": "mailchimp",
  "list_id": "abc123",
  "actions": [
    {"type": "mailchimp_tag_member", "list_id": "abc123",
     "email": "{data.email}", "tag": "welcomed"}
  ]
}
```

On every enabled save Dapier registers the URL on the audience through
Mailchimp's `POST /lists/{id}/webhooks`, authenticated with the stored key
(the bound connection's credential, else the shared `mailchimp` one). All
three sources (user, admin, api) are subscribed, so changes made through the
Marketing API itself — like the upsert action — fire too. Mailchimp answers
the registration with a `ping` POST, which Dapier answers 200 without
publishing. Disabling or deleting the trigger removes the registration
(Mailchimp has no delete-by-URL: Dapier lists the audience's webhooks, finds
ours by URL, and deletes it by id). Both directions are best-effort: a
Mailchimp failure never blocks the local save or delete — it comes back as a
`warnings` entry in the response, which `dapier hooks save` prints.

### Events

Mailchimp sends no signature or auth header; the unguessable URL is the
credential. Deliveries publish `connector: mailchimp` with `event` named by
the webhook `type`, and `data` carrying `{hook, type, data: {...}}`:

| Webhook type | Fires when | Notable `data` fields |
| --- | --- | --- |
| `subscribe` | Someone joins the audience | `list_id`, `email`, `merges` (FNAME, LNAME, ...), `ip_opt`, `ip_signup` |
| `unsubscribe` | Someone unsubscribes | `list_id`, `email`, `campaign_id`, `reason` |
| `profile` | A member's profile or merge fields change | `list_id`, `email`, `merges`, `changes` |
| `upemail` | A member's email address changes | `list_id`, `old_email`, `new_email` |
| `cleaned` | An address is cleaned (bounced) | `list_id`, `email`, `campaign_id`, `reason` |
| `campaign` | A campaign is sent | `id`, `list_id`, `status`, `title`, `subject`, `send_time` |

An omitted `events` list subscribes all six. On an edit, the stored list
survives an omitted `events`.

The chip's seventh event, `member.new`, is not a webhook type — it comes
from the `mailchimp.members` poll source below.

## The `mailchimp.members` poll source

For "new subscriber" without webhooks, create a poll trigger with source
`mailchimp.members` (`dapier polls save`):

```json
{
  "name": "digest-new-members",
  "source": "mailchimp.members",
  "expression": "rate(15 minutes)",
  "list_id": "abc123",
  "flow": "digest-new-members"
}
```

`list_id` is required; `connection_id` is optional (the shared `mailchimp`
credential backs the poll when omitted). Each fire lists the audience's
members (1,000 per page, the Marketing API maximum), newest `last_changed`
first, and members changed after the stored watermark fire as
`mailchimp` / `member.new` with `{id, email, status, last_changed,
first_name, last_name}`. The first fire only seeds the watermark — existing
members are history, not news — and a member id stays deduplicated, so a
profile edit inside the dedupe window never re-fires: the event is
`member.new`, not `member.updated`.

## Actions

All Mailchimp actions take `credential_id` (default `mailchimp`) and render
fields from the event. `status` choices are `subscribed`, `pending`,
`unsubscribed`, `cleaned`; `merge_fields` is a JSON object such as
`{"FNAME": "{name}"}`.

| action | Mailchimp call | notes |
| --- | --- | --- |
| `mailchimp_find_member` | `GET /lists/{id}/members/{md5}` | by email; a miss is `{found: false}` — with **Create if missing** the miss is created (`status` applies as status-if-new, `merge_fields` too) and the output reports `created: true` |
| `mailchimp_upsert_member` | `PUT /lists/{id}/members/{md5}` | Add/Update Member: creates when missing (**Status if new**), updates when present; output `{updated: true, member}` |
| `mailchimp_remove_member` | `DELETE /lists/{id}/members/{md5}` | permanent; a missing email is `{removed: false}`, not an error — chain a find step when the difference matters |
| `mailchimp_unsubscribe_member` | `PATCH status → unsubscribed` | the reversible counterpart of remove: the member stays on the audience; a miss is `{unsubscribed: false}`; output carries the member's new status |
| `mailchimp_tag_member` | `POST /lists/{id}/members/{md5}/tags` | add or remove one tag (**Operation** select); the member must exist — upsert first when unsure |

## Gotchas

- The API key must keep its datacenter suffix (`-us12`); Dapier builds the
  API root from it and rejects a bare key.
- Mailchimp delivers webhooks unsigned — there is no secret to configure;
  the URL's unguessability is the only gate. Treat trigger names like
  credentials.
- A trigger save can succeed while the Mailchimp registration failed. Read
  the `warnings` line in `dapier hooks save` output before assuming the
  webhook is live.
- `member.new` never arrives as a webhook; it needs a `mailchimp.members`
  poll trigger.
- `mailchimp_remove_member` is permanent; `mailchimp_unsubscribe_member` is
  the reversible choice. Neither treats a missing email as an error, so a
  typo silently no-ops — use `mailchimp_find_member` first when it matters.
- `mailchimp_upsert_member`'s status only applies to members the audience
  does not have yet, and `mailchimp_tag_member` errors on a member that
  does not exist.
