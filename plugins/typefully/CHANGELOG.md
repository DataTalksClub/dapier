# Changelog

## 1.2.1

Fix media S3 PUT: urllib re-added Content-Type application/x-www-form-urlencoded
on send (breaking Typefully presigned signatures). Use http.client with only
Content-Length.

## 1.2.0

Media upload (presigned URL + headerless PUT + bounded ready poll), media
status, draft PATCH (attach media_ids, edit, schedule), and opt-in scheduled
draft creation (`--allow-schedule` / `allow_schedule=true`). Unscheduled
create-draft still rejects publish_at/plan_at by default. Signed upload URLs
never leave the server; CLI prints media_id + status only.

## 1.1.0

Operator CLI/API for social sets, drafts, queue window, and unscheduled draft
create. Draft discovery resource. Default social set 182343. No schedule/publish.

## 1.0.0

Write-only credential storage, account health and read-only social-set discovery.
