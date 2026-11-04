# Model API authentication

All chat (including image generation) and embeddings routes require `X-Router-Key`,
including aliases with `/api`, `/v1`, and `/api/v1`. The dependency runs before
model handlers, upstream calls and request logging. Missing or incorrect keys
return 401. An empty key configuration never grants anonymous access.
`Authorization` and `X-API-Key` keep their existing upstream-provider meanings.
Router keys are excluded from saved request headers and never forwarded upstream.

Configure `ROUTER_API_KEYS` in the server-only `.env`; comma-separated keys allow
rotation: add a new key, migrate backends, remove the old key, and recreate the
router-api container. Keep keys out of browsers and distributed game binaries.
Pass the key from a trusted backend using `X-Router-Key`.

## Existing game migration

Production may explicitly set `ROUTER_ALLOW_LEGACY_ENCRYPTED=true` temporarily.
Legacy requests must include `X-Encrypted: true` AND a Fernet-authenticated body
that decrypts to a JSON object with the existing `PAW_FERNET_KEY`. A header alone,
plaintext, invalid tokens, or an incorrect explicitly supplied Router key fail.
No new client defaults to this path; the default setting is false.
Health, gameplay telemetry and other non-model routes retain their behavior.

This closes anonymous plaintext access; it does not establish individual player
identity. Distributed Fernet secrets can be extracted, and captured ciphertext
can be replayed. Move game clients to login-bound, short-lived server-issued
credentials before disabling the legacy switch. Separate rate limits, per-player
budgets and upstream spending caps are needed to bound authenticated abuse.

The raw host port 9000 is also protected by the application dependency. Do not
add only a Caddy header check, which direct port access could bypass.

## Verify a release

Run `python -m pytest -q tests/test_model_auth.py tests/test_gemini_proxy.py` in
a one-off container with candidate sources mounted read-only. Check all eight
model route aliases: anonymous/incorrect keys/forged encrypted headers -> 401;
valid keys -> handler; valid legacy encrypted requests -> handler only with the
migration switch enabled. Test non-model health access and key log redaction.
Use stub upstream handlers for these checks; do not generate paid model traffic.

After deployment verify the image source matches the release, anonymous public
and localhost:9000 requests return 401, health remains healthy, and an authenticated
request reaches validation without invoking a paid upstream (invalid embeddings
input -> 400). Never print the production key during verification.
