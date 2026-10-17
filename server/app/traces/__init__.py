"""Digital traces area (AGEDS-TASK-20261017-L2): personal-data exports and file metadata.

Format semantics live in ``server.app.importers.<format>``; this package holds the
mechanics they share without changing meaning:

* ``common``    — opt-in errors, ``metadata.signal``, bounded/surrogate-safe raw
                  values, one-transaction event writes, append-only row helper,
                  the ``fb_latin1_utf8_v1`` decoded layer;
* ``geotags``   — the photo geotag store and its read contract ``iter_geotags()``
                  for the location area's place matching;
* ``messaging`` — the shared writer for messaging/social downloads using the
                  ``chat_message`` event conventions, logical records across
                  exports and attachment references;
* ``queries``   — read-only summaries for the API and the "Ślady" page.

See ``docs/PERSONAL_EXPORTS.md``.
"""
