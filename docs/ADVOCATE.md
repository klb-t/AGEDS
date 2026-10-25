# Evidence advocate — operator guide

Status 2026-10-25 (branch `claude/rzecznik-design`): text conversation works in `retrieval_only` mode and is
accepted on synthetic data (backend, real Chromium). The Vertex provider path is tested against a recording fake
transport only; **no real Vertex call has been made yet**. Voice and avatar are not implemented.

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

## Limits to tell the recipient

- Retrieval is lexical (word prefixes over stored text). It finds no synonyms and no meaning without matching words.
- Quotes are stored derived text: ASR transcripts can be wrong; hashes and citations do not prove truth or authorship.
- Status labels (`wynika z cytowanych fragmentów`, `wniosek (hipoteza)` …) describe the kind of statement, not a probability.
- Annotations, tags and the audit log have no scope before the model and are never shown to recipients.
- In `legacy_open` mode (the default) everyone reaching the port acts as the owner.
