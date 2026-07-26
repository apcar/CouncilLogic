# Assets

`councillogic-mark.svg` is the provider-count-neutral CouncilLogic source mark.
Its continuous chamber and flow represent governed deliberation; the central
diamond represents the durable aggregate and audit record.

`councillogic-icon.png` is a deterministic 256 × 256 raster derivative for
repository and application surfaces.

`../docs/assets/councillogic-header.svg` is the 1520 × 400 README header. It
keeps the repository landing page compact while preserving a separate,
reusable source mark.

Regenerate the raster derivative from the repository root with:

```bash
rsvg-convert -w 256 -h 256 \
  -o assets/councillogic-icon.png assets/councillogic-mark.svg
```

Trademark treatment for the CouncilLogic name and mark is separate from the
AGPL-licensed code.
