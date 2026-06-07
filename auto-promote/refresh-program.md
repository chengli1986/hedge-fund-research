# GMIA Profile Refresh — Agent Program (monthly)

You are refreshing the **time-sensitive** facts of the 28 production fund
profiles that render the Sources tab of hedge-fund-research.html.

## Hard rules
1. ONLY touch time-sensitive fields: `aum` (and the embedded AUM figure in the
   English description in `config/sources.json`), plus corporate events that
   change `type_en`/`type_zh`/`desc_zh`/`notable_*` (M&A, delisting/take-private,
   rebrand).
2. NEVER rewrite stable prose, founder names, founding years, or history. If a
   field has no sourced reason to change, leave it out of the draft.
3. Every changed fact MUST be web-verified with a recorded source URL. Do NOT
   recall figures from memory (this caused the PineBridge/Ares confabulations
   that commit `13c51f0` fixed).

## Steps (per fund in publish._FUND_PROFILES)
1. Read the current profile (founded/aum/hq/type/desc/notable).
2. WebSearch the latest AUM. Prefer authoritative sources: SEC 8-K/10-K for
   listed managers, Form ADV, the firm's own "by the numbers"/factsheet, or
   year-end results. Record the source URL.
3. WebSearch for material corporate events since the profile looks last-updated:
   acquisitions, IPO/delisting/take-private, rebrands. Record source URLs.
4. If (and only if) something changed materially (AUM drift >= ~15% or a
   confirmed event), write `pending_profiles/<id>.refresh.json`:

```json
{
  "id": "<fund-id>",
  "aum": "<new value, only if changed>",
  "aum_source": "<url>",
  "change_log": [
    {"field": "aum", "old": "<old>", "new": "<new>", "reason": "<one line>", "source": "<url>"}
  ]
}
```
   - Keep the same currency/scale convention as the existing value (e.g. `~$1.03T`).
   - For口径-ambiguous managers (e.g. GSAM total AUS vs AM vs alternatives),
     state the basis in `reason` and keep the existing basis.
   - For corporate events, include the changed text field(s) with full new text
     and an event keyword in `reason` (acqui / merg / delist / take-private /
     rebrand / 收购 / 退市 / 私有化 / 改名). Keep each text field's diff small —
     change only the affected clause, not the whole paragraph.
5. If nothing changed for a fund, write NO file for it.

## Do not
- Do not run publish.py, edit publish.py, or git commit. The wrapper applies
  validated drafts via apply_refresh.py.
- Do not invent sources. No source → do not propose the change.
