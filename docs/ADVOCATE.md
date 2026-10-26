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

## Voice (optional)

```bash
python -m server.app.cli advocate-voice on            # needs advocate-provider; preset TTS voice pl-PL-Wavenet-B
```

A spoken question is sent to Vertex (Gemini) only after the speaker ticks the consent box for that recording; the
recognised text lands in the question box for review and is never sent as a question automatically. Reading an
answer aloud (Cloud Text-to-Speech, `texttospeech.googleapis.com`, which must be enabled in the project) uses the
same gate and provider consent as the text answer and the same daily budget. The TTS price preset (16 USD per 1M
characters) is unverified. An avatar is not implemented.

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
- Export needs `export` and `read_metadata`; text columns need `read_text` too and are otherwise nulled and listed.
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
