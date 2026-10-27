# Gmail API connector (read-only)

`server/app/connectors/gmail.py` imports selected Gmail messages straight from the owner's
account with the scope `https://www.googleapis.com/auth/gmail.readonly`.

* It sends only GET requests and never changes, labels, sends or deletes anything.
* Each message is fetched in Gmail's raw RFC 822 form and parsed by the same EML importer
  as an exported `.eml`: the same attachment and date rules.
* Next to the message, `gmail_api_fetches` records what the API said at fetch time: labels,
  thread id, `internalDate`, `historyId`. These rows are append-only.
* Nothing runs until the owner has done the steps below. The account has not been used.

The Takeout export (see [IMPORTERS_COMM](IMPORTERS_COMM.md)) remains the complete snapshot.
The connector is for a selected query or thread, and for messages that arrive later. Gmail's
raw form is Gmail's own serialisation: it can differ byte-wise from the same message in a
Takeout mbox. Both stay as separate acquisitions with their own provenance.

## Authorisation (owner, once)

1. Google Cloud console, in the owner's project:
   * APIs & Services → Library → **Gmail API** → Enable (no charge).
2. OAuth consent screen:
   * user type **External**, publishing status **Testing**;
   * add the owner's Gmail address as a **test user**;
   * scope `.../auth/gmail.readonly`.

   In Testing mode Google shows an "unverified app" warning (it is the owner's own client)
   and a refresh token expires after **7 days**. After that, repeat steps 4–5.
3. Credentials → Create credentials → OAuth client ID → type **Desktop app**. Download the
   JSON to `~/.config/ageds/gmail_client.json` on the machine that runs AGEDS, then
   `chmod 600` it. Override the path with `AGEDS_GMAIL_CLIENT`.
4. `scripts/pilot_local.sh cli gmail-auth-url` prints a consent URL for `gmail.readonly` only
   (PKCE; the verifier stays in a 0600 file for 15 minutes). Open it in a browser signed in
   to the owner's account and allow.
5. The browser is sent to `http://127.0.0.1:8765/?state=…&code=…`. The page will not load
   when AGEDS runs on another machine, and that is expected. Copy the whole address and run
   `scripts/pilot_local.sh cli gmail-auth-complete --redirect-url '<address>'`.

   The grant is refused and nothing is stored if Google granted more than read-only Gmail.
   The refresh token goes to `~/.config/ageds/gmail_token.json` (0600, outside the
   repository, never exported; `AGEDS_GMAIL_TOKEN` overrides the path).

Revoke access at any time at myaccount.google.com → Security → Third-party access, and
delete the token file.

## Use

```bash
scripts/pilot_local.sh cli gmail-import --query 'from:uwv.nl after:2026/09/01'          # dry run: ids only
scripts/pilot_local.sh cli gmail-import --query 'from:uwv.nl after:2026/09/01' --apply  # fetch raw, import
scripts/pilot_local.sh cli gmail-import --thread-id <id> --apply
```

* Spam and Trash are included in a query, so a selection is not silently narrowed.
* The dry run lists message ids without fetching content. `selection_complete` is false
  when `--max-messages` (default 500) cut the list.
* A message already fetched with the same raw SHA-256 is skipped. A changed message (rare)
  is acquired again as a new observation.
* Each import adds one `audit_log` row (`gmail_api_import`) with the query, counts and
  completeness.
* The coverage section of `scripts/pilot_report.py` counts these messages with the other
  email imports (`email_rfc5322`).

## Verified

Tests on a fake Google transport with generated messages
(`server/tests/test_gmail_connector.py`) cover:
* refusal before authorisation;
* refusal of a wider grant and of a forged `state`;
* 0600 token file;
* dry run without content requests;
* import with attachment and API metadata;
* idempotent re-import;
* GET-only requests.

No request has been made to Google or to the owner's account.
