# Protocol: turning a website into an MCP

A repeatable recipe for wrapping any site with a high-level "site-as-MCP" tool
surface, where each tool is a **cached deterministic script** instead of a
runtime "figure out what to click" LLM call.

**Why this layer exists.** Vanilla `mcp__browseros-1__*` exposes 60+ low-level
primitives (`take_snapshot`, `click`, `fill`, `evaluate_script`). An agent
using just those has to (1) discover what's possible on each site, and (2)
re-derive the click sequence every time. Both are slow and flaky. A
site-specific MCP collapses that to a fixed surface — `gmail_compose(to,
subject, body)` instead of "snapshot → find compose button → click → wait →
snapshot → find To field → fill → ...".

[`gmail_mcp/server.py`](../gmail_mcp/server.py) is the canonical reference
implementation; this doc generalizes the pattern to N sites.

---

## The 7-step recipe

### 1. Pick a slot, log in once

Any MCP tool you build will drive the cloud browser via BrowserOS. Pick a
slot, sign into the site by hand at `http://localhost:608N/`, run
`./scripts/stop-session.sh` → never need to log in again. The site-MCP can
assume "you are signed in."

### 2. Recon the DOM

Open the site in the live-view, then point the BrowserOS MCP at it from
**this** Claude session and probe:

```js
// in mcp__browseros-1__evaluate_script
(() => {
  // Gather candidate selectors for every action you care about.
  // Look for stable hooks IN THIS ORDER:
  //   1. role=  / aria-label="…"          ← most stable, accessibility-tested
  //   2. data-* attributes                 ← stable, used by their own QA
  //   3. id="..."                          ← stable on simple sites
  //   4. obfuscated class names (.bog, .zF) ← stable on big SPAs that have
  //                                           had them for years; verify by
  //                                           grepping public bug trackers
  //                                           and Gmail-clone projects
  //   5. nth-child / structural selectors  ← last resort, brittle
  return {
    primary_action_btn: !!document.querySelector('div[gh="cm"]'),
    list_rows: document.querySelectorAll('tr[role="row"]').length,
    // ...
  };
})()
```

Three rules of thumb:

- **Prefer one stable hook + one fallback.** `'span.zF, span[email]'` —
  obfuscated-class first (faster), aria fallback if the class rotates.
- **Keyboard shortcuts are sometimes more stable** than DOM (Gmail's `c` =
  compose), but require user opt-in in many apps. Verify before relying on
  them.
- **The site's own JSON APIs are even more stable.** If the site exposes
  REST/GraphQL you can hit while logged in (cookies forwarded), prefer that
  to DOM scraping.

### 3. Decide your tool surface

Three guidelines:

- **One tool = one user intent.** "Compose and send an email", not "click
  Compose, then fill To, then fill Subject, ...". The agent should think in
  the user's vocabulary, not the site's.
- **Atomic where possible.** A tool either succeeds completely or returns a
  clear error. Don't expose half-done states.
- **5–8 tools is plenty for v1.** You can always add more. Gmail's surface in
  this repo: `open_inbox`, `list_recent`, `search`, `open_email`, `compose`,
  `archive_current`, `screenshot`. Covers ~80% of what a personal-assistant
  agent ever asks of Gmail.

### 4. Scaffold a stdio MCP server

Copy `gmail_mcp/` to `<site>_mcp/`, replace the `gmail` name, and write your
tools. Each tool follows the same shape:

```python
@mcp.tool()
async def site_action(arg1: str, arg2: int = 0) -> str:
    """One-line description of what the user gets."""
    # 1. Drive the page via a single evaluate_script when possible.
    #    Pass arguments as JSON-encoded literals into the JS source so they
    #    can't break out and you don't have to escape anything by hand.
    js = f"""
      (async () => {{
        const wait = ms => new Promise(r => setTimeout(r, ms));
        const waitFor = async (sel, t=8000) => {{
          const start = Date.now();
          while (Date.now() - start < t) {{
            const el = document.querySelector(sel);
            if (el) return el;
            await wait(100);
          }}
          throw new Error('timeout: ' + sel);
        }};
        // ...site-specific recipe here...
        return JSON.stringify({{ok: true, ...payload}});
      }})()
    """
    return json.dumps(await _js(js), indent=2)
```

The `evaluate_script` round-trip is one HTTP call — much faster and more
robust than `take_snapshot` → `click` → `take_snapshot` → `fill` → ... loops.

### 5. Use the right input mechanism

Different elements need different tricks:

| Element type             | Right way                                                 |
|--------------------------|-----------------------------------------------------------|
| Plain `<input>`          | `el.focus(); el.value = ''; document.execCommand('insertText', false, value)` — triggers React/Vue change events that just setting `el.value` doesn't |
| `<textarea>`             | Same as above                                             |
| `contenteditable` (rich) | `el.focus(); document.execCommand('insertText', false, value)` |
| Dropdowns (`<select>`)   | `el.value = optionValue; el.dispatchEvent(new Event('change', {bubbles: true}))` |
| React-only fakes         | Use `Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(el, value)` then dispatch `input` event |
| Buttons / links          | `el.click()` — works almost everywhere                    |

`document.execCommand` is technically deprecated but still works in every
production browser and is the only thing that reliably triggers framework
re-renders.

### 6. Test against the real, logged-in site

Drive your server via stdio from the command line (this is what Claude Desktop
will do, so testing this way catches real problems):

```bash
{
  printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}'
  printf '%s\n' '{"jsonrpc":"2.0","method":"notifications/initialized"}'
  printf '%s\n' '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"<your_tool>","arguments":{...}}}'
  sleep 5
} | <site>_mcp/.venv/bin/python <site>_mcp/server.py 2>/dev/null
```

Look for:

- Tool returns within ~5 seconds (timeouts here are slow → re-think the
  recipe).
- Error message is informative when the recipe breaks ("Subject field not
  found" beats "TypeError: cannot read properties of null").
- **Idempotence:** running the same tool twice in a row should either succeed
  twice or fail with a clear error the second time, never partial-state.

### 7. Wire into Claude Desktop

Add an entry to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
"<site>": {
  "command": "/abs/path/to/<site>_mcp/.venv/bin/python",
  "args": ["/abs/path/to/<site>_mcp/server.py"],
  "env": { "BROWSEROS_URL": "http://localhost:9201/mcp" }
}
```

Quit Claude Desktop fully (⌘Q), reopen — the new tools appear.

---

## Anti-patterns to avoid

- **Don't use `take_snapshot` + `click` for tools that run frequently.** Each
  snapshot is a full DOM walk; on Gmail that's a ~150 KB serialization. Three
  snapshots in a tool call = a second of latency for nothing. Prefer
  `evaluate_script` with surgical selectors.
- **Don't take a screenshot after every action "to verify".** It's tempting
  but adds 200–500 ms per call and the agent rarely actually needs the image.
  Have a separate `<site>_screenshot` tool the agent can opt into.
- **Don't reach for `xpath` selectors.** They're more verbose than CSS for
  the same expressiveness, and CSS-only is easier to read in a year.
- **Don't hardcode wait timeouts that are tighter than 2 s.** Network
  hiccups happen; tools that fail fast become flaky tools that no agent
  trusts.
- **Don't put credentials in the MCP server.** The cloud browser is already
  logged in; that's the whole point of the architecture. Anything that
  needs a fresh login should go through the noVNC live-view, not the MCP.

---

## What a "good" site-MCP looks like

Use this as a checklist before merging a new `<site>_mcp/`:

- [ ] 5–8 tools, each named in user vocabulary (verbs + nouns from the
      site's own UI labels)
- [ ] Each tool has a one-sentence docstring describing what the user gets,
      not what selectors fire
- [ ] At least one read-only tool (`<site>_list_*` or `<site>_search`) for
      agent self-verification
- [ ] All tools return JSON-stringified structured data when there's anything
      to return — not natural-language prose
- [ ] One graceful "not signed in" or "wrong page" error path, surfaced as a
      clear string the agent can act on
- [ ] Stdio smoke test passes (the curl-style script in step 6) before
      Claude Desktop is restarted

---

## Worked example

[`gmail_mcp/server.py`](../gmail_mcp/server.py) implements all 7 steps for
Gmail. Read it linearly; every tool follows the pattern above. About 250 lines
of Python including the BrowserOS HTTP client.

Tools and their cached recipes:

| Tool                    | What the agent says                              | Cached recipe                                                              |
|-------------------------|--------------------------------------------------|----------------------------------------------------------------------------|
| `gmail_open_inbox`      | "go to my inbox"                                 | `navigate_page → mail.google.com/#inbox` + verify title contains "Inbox"   |
| `gmail_list_recent(n)`  | "what's in my inbox right now"                   | `evaluate_script` over `tr[role="row"][jsaction]`, parse `span.zF/.bog/.y2/.bq3` |
| `gmail_search(q, n)`    | "find emails from my professor"                  | `navigate_page → /#search/<q>`, then list_recent recipe                    |
| `gmail_open_email(i)`   | "open email #2"                                  | `rows[i].click()`, wait, parse `h2.hP` + last `div.a3s`                    |
| `gmail_compose(...)`    | "send an email"                                  | click `div[gh="cm"]`, wait dialog, fill 3 inputs via `execCommand`, optional Send |
| `gmail_archive_current` | "archive this thread"                            | click toolbar button matching `aria-label="Archive"` or `data-tooltip*="Archive"` |
| `gmail_screenshot`      | (pass-through to BrowserOS for visual debugging) | proxy `take_screenshot`                                                    |

Total time-to-build, recon to working tool: about 90 minutes. Each new site
should be quicker once you've internalized the recipe.
