# Optional corpus catalogue

AGEDS can scan a folder directly in **Sources**. A prepared corpus catalogue is
optional and supports offline browsing, group selection and recording matching.

1. Open **Korpus** and select a private `ageds-corpus-seed.json` with Android's
   Storage Access Framework. The app caches the catalogue in app-private storage.
2. Select the groups or individual numbers relevant to the current task.
3. Optionally connect a recordings folder. Filename matches are candidates;
   duplicate filenames retain separate URIs and require explicit selection.
4. Configure the evidence server, review the selected recordings and prepare
   transcription. Uploading is a separate action from scanning.

The seed may contain contacts, communication records, recording metadata and
presets. Do not commit it, recordings, a private database or case-specific group
identities to this repository. The application has no built-in case identities.

Priority defaults are private configuration in the optional `defaultPresetIds`
field. Only IDs present in `presets` are selected; duplicate and unknown IDs are
ignored. A legacy seed without this field starts with no selected groups, which
the user can select in the catalogue.

```json
{
  "schemaVersion": 1,
  "defaultPresetIds": ["review_group"],
  "presets": [{"id": "review_group", "label": "Review group"}],
  "contacts": [{"phone": "+000000000", "label": "Synthetic example"}]
}
```

These values are synthetic. See [source scan contracts](SOURCE_SCAN_ENGINE.md)
for coverage, resource limits and the distinction between observations and
ingestion.
