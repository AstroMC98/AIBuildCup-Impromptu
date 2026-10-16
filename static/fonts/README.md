# Fonts

Self-hosted so the app never calls a font CDN. Latin and Latin Extended subsets of the
variable fonts, as served by Google Fonts; the Latin Extended files load only when a page
uses one of their characters.

| File | Family | Axes | License |
|---|---|---|---|
| `anybody-latin.woff2`, `anybody-latin-ext.woff2` | Anybody, by Tyler Finck (Etcetera Type Co) | width 50–150, weight 100–900 | SIL Open Font License 1.1 |
| `atkinson-next-latin.woff2`, `atkinson-next-latin-ext.woff2` | Atkinson Hyperlegible Next, by the Braille Institute of America | weight 200–800 | SIL Open Font License 1.1 |

Roles: Anybody is the display face (titles, captions, sleeves); Atkinson Hyperlegible Next
is everything else. See `docs/plans/design-system.md` §2.2. The logo needs no font: it is
artwork from the brand kit (`static/brand`).
