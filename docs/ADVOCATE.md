# Evidence advocate — operator guide

Status 2026-10-25 (branch `claude/rzecznik-design`): text conversation works in `retrieval_only` mode and is
accepted on synthetic data (backend, real Chromium). The Vertex provider path is tested against a recording fake
transport only; **no real Vertex or Text-to-Speech call has been made yet**. The avatar is not implemented.

## Deployment on the VM

`scripts/deploy_vm.sh` (dry run by default, `--apply` executes, needs sudo): stops api and worker, backs up
SQLite with the online backup API and the content store with SHA-256 manifests to `/var/lib/ageds-backups/`,
builds, starts, then checks `/advocate`, the auth-mode header, `/api/advocate/status` and migrations 1–6; it prints
the rollback steps. Inside the container the CLI is `python -m app.cli` (not `server.app.cli`):

```bash
sudo docker compose -p ageds -f docker-compose.yml -f docker-compose.vm.yml exec api python -m app.cli authz-show
```

## Share a package with a recipient

Run with the same environment as the server (`EW_DB_PATH`, `EW_STORE_DIR`). Back up SQLite and the content store
first. Each command prints JSON; tokens are shown once and stored hashed.

```bash
python -m server.app.cli authz-owner-init --name "Owner"                 # once; keep the printed token
python -m server.app.cli authz-principal-add --name "Advocate" --kind human
python -m server.app.cli authz-package-create --name "Package for the advocate"      # snapshot preset
python -m server.app.cli authz-package-add --package-uid <P> --kind case --id <case id>   # repeat per case
python -m server.app.cli authz-package-snapshot --package-uid <P>                   # freezes what it reaches now
python -m server.app.cli authz-package-grant --package-uid <P> --principal-uid <A> \
  --capability read_metadata --capability read_text --capability chat_with_advocate   # add read_bytes for audio
python -m server.app.cli authz-token-issue --principal-uid <A> --label advocate      # give this token to the recipient
python -m server.app.cli authz-mode tokens                                          # refuses without an owner token
```

The recipient opens `/advocate` and pastes the token. Behind the VM proxy the basic-auth prompt comes first; the
token is then kept in an HttpOnly cookie. `authz-revoke --grant-uid` and `authz-token-revoke --method-uid` take
effect on the next request; copies already downloaded are not recalled. New material reaches a snapshot package
only after another `authz-package-snapshot`.

## Model provider (optional)

```bash
python -m server.app.cli advocate-provider vertex                 # preset gemini-3.5-flash, location global
python -m server.app.cli advocate-consent --scope installation --basis "R100 owner 2026-10-25"
python -m server.app.cli advocate-budget 1.0                      # EUR per UTC day, checked before each call
python -m server.app.cli advocate-spend                           # spend, consents, recent calls (no content)
```

A prompt is sent only when a consent covers every passage of the turn and the day's spend plus the call's worst
case fits the budget; otherwise the answer falls back to quoted passages and says why. Price presets
(1.50 / 9.00 USD per 1M input/output tokens) come from third-party aggregators and are unverified with Google;
`eur_per_usd` 1.0 overstates cost. Correct them in the `advocate.provider` setting.

**A consent's data policy is enforced.** Each consent records the data policy of its transport
(`providers.PROVIDER_DATA_POLICY`). For Vertex: not public, no training, Vertex's default
in-memory cache on. For Text-to-Speech: not public, no training, no cache. A consent covers calls
only while that record equals what the code sends. This holds for the advocate, Vertex ASR/OCR and
read-aloud alike. A consent with a different or empty policy covers nothing, and `/settings`
marks it "nie obejmuje wywołań". When a transport's policy changes, the owner consents again.

## Voice (optional)

```bash
python -m server.app.cli advocate-voice on            # needs advocate-provider; preset TTS voice pl-PL-Wavenet-B
```

A spoken question is sent to Vertex (Gemini) only after the speaker ticks the consent box for that recording; the
recognised text lands in the question box for review and is never sent as a question automatically. Reading an
answer aloud (Cloud Text-to-Speech, `texttospeech.googleapis.com`, which must be enabled in the project) uses the
same gate and provider consent as the text answer and the same daily budget. The TTS price preset (16 USD per 1M
characters) is unverified. An avatar is not implemented.

The audio of an answer is kept in `advocate_speech`, keyed by answer, text hash, voice and
language. A replay is served from there, after the same access check, and calls and pays nothing.
A different voice or language is a new call. Serving a replay sends nothing to the provider, so it
does not need the Text-to-Speech consent. The table is excluded from whole-database exports.

## Security defaults after the independent review (2026-10-26)

- Every artifact route authorizes each returned text version and citation; a frozen snapshot never reaches later versions.
- An assignment opens material only in `assignment.opening_statuses` (seed `unknown`, `human_reviewed`): an AI proposal
  or a rejection records a meaning but opens nothing until a person accepts it.
- `Host` must be loopback, `EW_ALLOWED_HOSTS` or `http.allowed_hosts` (`deploy_vm.sh --public-host`); state-changing
  requests from another site (`Sec-Fetch-Site`, `Origin`) are refused in every mode. `docker-compose.yml` publishes
  loopback only. An Android device on a LAN or the emulator (`10.0.2.2`) needs its server address in
  `EW_ALLOWED_HOSTS`.
- `metadata_only` owner visibility shows that a turn exists, never the question; earlier answers re-enter a prompt
  only while provider consent covers their inputs.
- The owner can also invite from the browser: `/settings` → Recipients, enabled in tokens mode
  ([OWNER_WEB](OWNER_WEB.md)); it is the same function as `authz-invite`.
- Export needs `export` and `read_metadata`; text columns need `read_text` too and are otherwise nulled and listed.
- Feature areas (catalogue, imports, timeline, workbench, reports, hypotheses, ledger …) need,
  installation-wide, `read_text` for reads, `export` for exports and file downloads, and
  `administer` for writes. Until review 3 a read needed only `read_metadata`, so a
  metadata-only delegate could read full text there. `/api/artifacts` shows text previews only
  with `read_text`.
- The whole-database export (`/export/manifest.json`, CLI `metadata-export`) keeps
  recipients' advocate questions, answers and private summaries only as hashes
  (`redacted_private_rows`). The exception is the exporting principal's own conversation, or
  one started under `owner_visibility=full`.
- CSRF: only `X-AGEDS-Token` and `Authorization: Bearer` skip the Origin and Sec-Fetch-Site
  checks. Basic, Digest and Negotiate credentials, which a browser sends by itself, do not.
- `deploy_vm.sh` admits as local only the gateway of the `ageds_default` network, not
  `172.16.0.0/12`. The image runs uvicorn with `--no-proxy-headers`, so `X-Forwarded-For`
  never sets the client address.
- Summaries are private to their creator (`summary.sharing`, preset `creator_only`).
- A blanket `treat_as_case` mapping leaves annotations, audit, principals and tags owner-only
  (`legacy_scope_policy.excluded_kinds`).
- Browser login issues a separate session secret valid `auth.session_hours` (preset 12); logout revokes it. API
  tokens may get `--expires-days`.

## After the second review pass (2026-10-26)

- Browser sessions are bound to their API token: they end with it, never outlive it and cannot start new sessions.
- `legacy_open` serves only clients in `http.legacy_open_clients` (seed: loopback; the deploy script adds the docker
  bridge for the host-side proxy). Any remote client needs `auth.mode=tokens`.
- Only a conversation's own principal may ask in it; turns record who asked.
- Export without `read_text` keeps an allow-list of metadata columns; everything else is nulled and listed.
- A model's `proven` must quote the cited passage verbatim (`advocate.proven_policy`, preset
  `require_verbatim_quote`); passages are marked as data in the prompt; the page shows the cited passage under a claim.
- Review 3 (2026-10-27):
  - `proven` also requires that, outside its verbatim quotes, a claim has at most
    `advocate.proven_max_extra_words` words (preset 6), so a short quote cannot carry an
    unquoted conclusion. Raising the limit counts as widening.
  - Passage header fields (an artifact name comes from the material) are made inert like the
    passage text.
  - Reading aloud needs its own consent for Cloud Text-to-Speech
    (`advocate-consent --provider google_tts …`), separate from Vertex AI.
- The prompt asks the model not to omit material contrary to the question's thesis. A fragment
  that contradicts the thesis or another fragment becomes its own claim with status
  `contradicts` and a citation; without a valid citation it becomes `claim_without_basis`.
  `contradicts` is a label from the model, not a finding.
- No database transaction is held across a provider call; the worst case is reserved before the call
  (`advocate.daily_budget_eur`, optional `advocate.daily_budget_per_principal_eur`) and settled after it.
- Assignments without a status from non-human authors are `ai_proposed` and open nothing.
- Verified media digests are cached per file identity; every served chunk is still checked.

## Limits to tell the recipient

- Retrieval is lexical (word prefixes over stored text). It finds no synonyms and no meaning without matching words.
- Quotes are stored derived text: ASR transcripts can be wrong; hashes and citations do not prove truth or authorship.
- Status labels (`wynika z cytowanych fragmentów`, `wniosek (hipoteza)` …) describe the kind of statement, not a probability.
- Annotations, tags and the audit log have no scope before the model and are never shown to recipients.
- In `legacy_open` mode (the default) everyone reaching the port acts as the owner.

## Recipients through the proxy (tokens mode)

A recipient outside the VM reaches AGEDS through the proxy, which otherwise asks for its own
login (basic auth). These routes, and only these, are what a recipient's browser needs. The list is
`http_authz.RECIPIENT_PUBLIC_ROUTES`; a test keeps it equal to real routes and files.

| Method | Path | Why |
|---|---|---|
| GET | `/advocate` | the page shell and login form; reads no stored data |
| POST | `/advocate/login`, `/advocate/logout` | start or end the browser session (token → cookie) |
| any | `/api/advocate/*` | conversations, turns, answer basis, summaries, status, voice |
| GET, HEAD | `/api/artifacts/*/content` | listening to a cited recording (range playback, with `read_bytes`) |
| GET | `/static/app.css`, `/static/advocate.css`, `/static/advocate.mjs`, `/static/range-player.mjs` | the page's own assets |

Rules for the proxy exception:
* On every exempted route the proxy sets the request header `X-AGEDS-Public-Route: 1`. In Caddy
  that is `request_header X-AGEDS-Public-Route 1` inside the route that skips `basic_auth`. AGEDS
  refuses any request carrying it while `auth.mode=legacy_open`. A client that sends the header
  itself only loses access.
* Order:
  * to tokens: first `scripts/auth_mode_vm.sh --to tokens --apply`, then add the exception;
  * back: first remove the exception, then `scripts/auth_mode_vm.sh --to legacy_open
    --proxy-exception-removed --apply`.

  In `legacy_open` the proxy is a local client, so an exempted route without the header would make
  any visitor the owner. The script refuses the way back without that flag.
* The proxy passes `X-Forwarded-Proto: https` (Caddy does by default), so the session cookie is
  `Secure`. The public host name must be in `http.allowed_hosts` (`deploy_vm.sh --public-host`).

In tokens mode the owner signs in at `/advocate` with the owner token (`authz-owner-init`, kept in
`~/.config/ageds/owner_init.json`). The session cookie then opens every owner page; those stay
behind the proxy's basic auth as well. Tested without tokens: every exempted data route answers 401
and leaks nothing (`RecipientRoutesThroughTheProxy` in `server/tests/test_review3_regressions.py`).
