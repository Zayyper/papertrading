# Design system for the web UI

The page served by `python -m hl_screener ui` is built the way
[awesome-claude-design](https://github.com/VoltAgent/awesome-claude-design) proposes: pick one
`DESIGN.md` from the collection, treat it as the single source of visual truth, and derive
everything else from it.

| file | role |
|---|---|
| `design/DESIGN.md` | The chosen system, verbatim: **Linear-inspired**, from `VoltAgent/awesome-design-md/design-md/linear.app/DESIGN.md` (preview: <https://getdesign.md/linear.app/design-md>). |
| `hl_screener/webui/static/tokens.css` | The front matter of `DESIGN.md` (`colors`, `typography`, `rounded`, `spacing`) as CSS custom properties. |
| `hl_screener/webui/static/app.css` | Components (`button-primary`, `feature-card`, `status-badge`, `text-input`, `pricing-tab`, `product-screenshot-card`…) written against those tokens only. |

## Why this one

A screener is a dense, technical, read-mostly tool: tables, a console, a couple of charts. The
Linear system is a near-black canvas with a four-step surface ladder, hairline borders instead of
shadows, one chromatic accent used scarcely, and a type scale tuned for product UI. It also says
"don't ship a light mode", which removes a whole class of decisions.

## What was kept, what was added

- Every colour, radius, spacing step and type token is the DESIGN.md value. Fonts are the
  substitutes the file itself recommends: **Inter** for Linear Display/Text, **JetBrains Mono**
  for Linear Mono (loaded from Google Fonts; system fallbacks otherwise).
- Lavender (`--color-primary`) is used only where the file allows: brand mark, primary CTA, focus
  ring, link emphasis. The one exception is the single data series in each chart, since the
  system has no other chromatic colour to give a line.
- **Extension:** `--color-semantic-danger` (#e5484d) and `--color-semantic-warning` (#d4a72c).
  The file's marketing palette has only a success green; a screener must show losses, failed
  checks and warnings. The DESIGN.md's *Known Gaps* section notes that Linear's product UI carries
  a red/orange/yellow tag palette, so these are taken from that side. They are used for text and
  the status-badge dot, always next to a sign, a word or an icon, never as fills.
- Charts follow the dataviz rules on top of the tokens: 2px line, ≥ 8px markers with a surface
  ring, recessive hairline grid, text in ink tokens, hover tooltips, no dual axes.

## Swapping to another system

1. Browse the collection, open a preview on <https://getdesign.md/>, and fetch the raw file:
   `https://raw.githubusercontent.com/VoltAgent/awesome-design-md/main/design-md/<slug>/DESIGN.md`
   (slugs are the URL parts on getdesign.md, e.g. `vercel`, `binance`, `coinbase`).
2. Save it over `design/DESIGN.md`.
3. Rewrite `tokens.css` from the new front matter, keeping the same variable names
   (`--color-primary`, `--color-canvas`, `--color-surface-1`…, `--type-body`, `--rounded-md`,
   `--space-md`). Systems with a light canvas need `color-scheme: light` and their own
   success/danger pair.
4. Reload the page. The Design tab reads the tokens live, so it doubles as the check.

The same `DESIGN.md` can be uploaded to [Claude Design](https://claude.ai/design) ("Create new
design system") to get a full starter kit, or pasted into a Claude Code session as the brief for
any new screen so it lands in the same system.

## Attribution

The DESIGN.md files in the collection are curated starting points inspired by publicly observable
design patterns, not official design systems (MIT, VoltAgent). Brand usage remains the user's
responsibility.
