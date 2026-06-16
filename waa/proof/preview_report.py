"""
Preview report — a manual QA view of what the agent would send.

Renders one self-contained HTML file showing, per prospect, the generated
email side-by-side with its annotated proof screenshot. Open it in a browser,
eyeball 5-10 outputs before any real send, and you'll quickly learn to tell a
strong lead/email from a weak one.

Single responsibility: turn a list of audit results into HTML. The screenshot
is base64-embedded so the file is portable (shareable with Erik/Mišo as one
attachment).
"""

from __future__ import annotations

import base64
import html
import os
from datetime import datetime


def _img_data_uri(path: str | None) -> str | None:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except OSError:
        return None


def _esc(text: object) -> str:
    return html.escape(str(text or ""))


def _email_html(result: dict) -> str:
    email = result.get("email") or {}
    subject = email.get("subject_line", "")
    body = (email.get("email_body", "") or "").replace("\\n", "\n")
    fu_subject = email.get("follow_up_subject", "")
    fu_body = (email.get("follow_up_body", "") or "").replace("\\n", "\n")

    analysis = result.get("analysis") or {}
    validation = analysis.get("validation") or {}
    facts = analysis.get("facts") or {}
    owner = email.get("owner_first_name") or facts.get("owner_first_name")

    skipped = result.get("skipped_reason")
    if skipped:
        return f'<div class="skip">Skipped: {_esc(skipped)}</div>'

    grounded = validation.get("passed")
    badge = (
        '<span class="ok">grounded ✓</span>' if grounded
        else '<span class="warn">not grounded</span>'
    )
    owner_line = f'<div class="meta">Owner detected: <b>{_esc(owner)}</b></div>' if owner else \
                 '<div class="meta">Owner: <i>unknown (no greeting)</i></div>'

    parts = [
        f'<div class="subject">{_esc(subject)}</div>',
        f'<pre class="body">{_esc(body)}</pre>',
        owner_line,
        f'<div class="meta">Validation: {badge}</div>',
    ]
    if fu_body:
        parts.append('<div class="fu-label">Follow-up (day 4)</div>')
        parts.append(f'<div class="fu-subject">{_esc(fu_subject)}</div>')
        parts.append(f'<pre class="body fu">{_esc(fu_body)}</pre>')
    return "\n".join(parts)


def _card_html(result: dict) -> str:
    url = result.get("url", "")
    facts = (result.get("analysis") or {}).get("facts") or {}
    name = facts.get("h1") or url
    shot = result.get("screenshot") or {}
    img = _img_data_uri(shot.get("path"))
    caption = shot.get("caption", "")

    if img:
        right = (
            f'<div class="cap">{_esc(caption)}</div>'
            f'<img src="{img}" alt="screenshot of {_esc(url)}">'
        )
    else:
        right = '<div class="noimg">no screenshot<br><small>nothing visual to circle</small></div>'

    return f"""
    <section class="card">
      <header>
        <div class="biz">{_esc(name)}</div>
        <a class="url" href="{_esc(url)}" target="_blank" rel="noopener">{_esc(url)}</a>
      </header>
      <div class="cols">
        <div class="left">{_email_html(result)}</div>
        <div class="right">{right}</div>
      </div>
    </section>
    """


def render_preview(results: list[dict], *, niche: str = "", location: str = "",
                   lang: str = "sk") -> str:
    """Return a complete self-contained HTML document for the given results."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    cards = "\n".join(_card_html(r) for r in results)
    sendable = sum(
        1 for r in results
        if (r.get("email") or {}).get("email_body") and not r.get("skipped_reason")
    )
    return f"""<!DOCTYPE html>
<html lang="{_esc(lang)}"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Preview — {_esc(niche)} {_esc(location)}</title>
<style>
  :root {{ --ink:#0f172a; --line:#e2e8f0; --accent:#e11d48; --ok:#16a34a; }}
  * {{ box-sizing: border-box; }}
  body {{ font: 15px/1.5 -apple-system, Segoe UI, Roboto, sans-serif;
          color: var(--ink); background:#f8fafc; margin:0; padding:32px; }}
  h1 {{ font-size: 20px; margin:0 0 4px; }}
  .sub {{ color:#64748b; margin-bottom:24px; }}
  .card {{ background:#fff; border:1px solid var(--line); border-radius:12px;
           margin:0 auto 24px; max-width:1100px; overflow:hidden;
           box-shadow:0 1px 3px rgba(0,0,0,.06); }}
  .card header {{ padding:14px 20px; border-bottom:1px solid var(--line); }}
  .biz {{ font-weight:700; font-size:16px; }}
  .url {{ color:#64748b; font-size:13px; text-decoration:none; }}
  .cols {{ display:flex; gap:0; }}
  .left {{ flex:1; padding:20px; border-right:1px solid var(--line); min-width:0; }}
  .right {{ flex:1; padding:16px; background:#0f172a; display:flex;
            flex-direction:column; align-items:center; justify-content:center; }}
  .right img {{ max-width:100%; border-radius:6px; box-shadow:0 4px 16px rgba(0,0,0,.4); }}
  .cap {{ color:#cbd5e1; font-size:12px; margin-bottom:8px; text-align:center; }}
  .noimg {{ color:#64748b; text-align:center; font-size:13px; }}
  .subject {{ font-weight:700; font-size:16px; margin-bottom:10px; }}
  .body {{ white-space:pre-wrap; font:14px/1.6 inherit; margin:0 0 12px;
           background:#f8fafc; border:1px solid var(--line); border-radius:8px; padding:12px; }}
  .fu-label {{ font-size:12px; text-transform:uppercase; letter-spacing:.05em;
               color:#94a3b8; margin-top:8px; }}
  .fu-subject {{ font-weight:600; font-size:14px; margin:4px 0; }}
  .fu {{ background:#fffbeb; }}
  .meta {{ font-size:13px; color:#475569; margin-top:6px; }}
  .ok {{ color:var(--ok); font-weight:600; }}
  .warn {{ color:var(--accent); font-weight:600; }}
  .skip {{ color:#94a3b8; font-style:italic; }}
</style></head><body>
  <h1>Preview — {_esc(niche)} · {_esc(location)}</h1>
  <div class="sub">{len(results)} prospects · {sendable} with a sendable email · generated {stamp} · NOTHING was sent</div>
  {cards}
</body></html>"""
