# Typefully

Stores an API v2 key through the existing write-only Credentials UI and HTTP API.
The plugin exposes read-only social-set discovery and account health checks. It does
not publish or schedule content.

```sh
dapier credentials set typefully --file -
dapier connections test typefully
dapier connections discover typefully social_sets
```

Pipe only the raw key to stdin; do not put it in arguments, URLs, workflow YAML,
or logs. Console Credentials > Typefully and CLI share API validation and storage.
The credential value is never returned by overview or save responses.
API reference: https://typefully.com/docs/api (v2 Bearer authentication).
