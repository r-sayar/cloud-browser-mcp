"""Site recipe registry for smart_browseros_mcp.

A recipe is metadata about a known website: which URL substrings identify it,
which existing site-MCP tools cover it, and what the agent should call next
based on the current URL state.

Recipes are a discovery aid, not an execution engine. The actual deterministic
recipes live in each `<site>_mcp/server.py` (gmail_mcp, outlook_mcp, …). This
registry tells the agent "you're on Gmail — call gmail_compose, not
take_snapshot+click+fill" so it lands on the cached path instead of re-deriving
the click sequence every time.

To add a site: append an entry to RECIPES below. Match on host substrings, list
the intents the corresponding site-MCP exposes, and (optionally) define
URL-pattern→next_actions hints.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse


@dataclass
class Intent:
    """One high-level action available via a site-MCP tool."""
    tool: str          # e.g. "gmail_compose"
    args: str          # e.g. "(to, subject, body, send=False)"
    summary: str       # one-line description


@dataclass
class Recipe:
    """A site's cached affordances."""
    id: str                                 # short id, e.g. "gmail"
    site_mcp: str                           # the MCP server name in Claude Desktop config
    url_match: list[str]                    # host/path substrings that mean "we're on this site"
    open_url: str                           # canonical landing URL (used by site_open)
    intents: list[Intent]                   # all intents this site exposes
    # url_substring → list of tool names that are *especially* relevant on that URL
    context_hints: dict[str, list[str]] = field(default_factory=dict)
    # always-shown follow-on chain advice ("after composing, you can send, then verify")
    flow_hints: list[str] = field(default_factory=list)
    notes: str = ""


def matches(recipe: Recipe, url: str) -> bool:
    """True if `url`'s hostname (or, for path-prefixed entries, its host+path)
    contains any of `recipe.url_match`. Matching against the host (not the raw
    URL) avoids false positives on auth/redirect flows whose query strings
    mention a third-party site.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    host_path = host + (parsed.path or "")
    for sub in recipe.url_match:
        # path-style entries (start with "/") match against host+path
        target = host_path if sub.startswith("/") else host
        if sub in target:
            return True
    return False


# ─── recipe definitions ──────────────────────────────────────────────────────
# Each entry mirrors the tool surface of the corresponding <site>_mcp/server.py.
# Keep these in sync when site-MCP tools are added or renamed.

RECIPES: list[Recipe] = [
    Recipe(
        id="gmail",
        site_mcp="gmail",
        url_match=["mail.google.com"],
        open_url="https://mail.google.com/mail/u/0/#inbox",
        intents=[
            Intent("gmail_open_inbox", "()", "Navigate to the Gmail inbox"),
            Intent("gmail_list_recent", "(count=10)", "Read the visible inbox; returns indexed threads"),
            Intent("gmail_search", "(query, count=10)", "Search Gmail (URL fast path: /#search/<q>)"),
            Intent("gmail_open_email", "(index)", "Open a thread by index from list_recent/search"),
            Intent("gmail_compose", "(to, subject, body, send=False)", "Compose via URL fast path; send=True dispatches Cmd+Enter"),
            Intent("gmail_archive_current", "()", "Archive the currently open thread"),
            Intent("gmail_screenshot", "()", "PNG screenshot of the current Gmail tab"),
        ],
        context_hints={
            "#inbox": ["gmail_list_recent", "gmail_compose"],
            "#search/": ["gmail_open_email", "gmail_compose"],
            "#thread/": ["gmail_archive_current", "gmail_compose"],
            # Gmail's URL-driven compose can land as ?view=cm or ?tf=cm
            # depending on entry path; match either.
            "view=cm": ["gmail_compose (already open — re-call with send=True to dispatch)"],
            "tf=cm": ["gmail_compose (already open — re-call with send=True to dispatch)"],
        },
        flow_hints=[
            "compose → optionally send=True in same call → search to verify delivery",
            "list_recent → open_email → archive_current OR reply via compose",
        ],
    ),
    Recipe(
        id="outlook",
        site_mcp="outlook",
        url_match=["outlook.office.com", "outlook.live.com", "outlook.office365.com"],
        open_url="https://outlook.office.com/mail/",
        intents=[
            Intent("outlook_open_inbox", "()", "Navigate to the Outlook inbox"),
            Intent("outlook_list_recent", "(count=10)", "Read the visible inbox; returns indexed threads"),
            Intent("outlook_search", "(query, count=10)", "Run an OWA search via URL"),
            Intent("outlook_open_email", "(index)", "Open the message at position index"),
            Intent("outlook_compose", "(to, subject, body, send=False)", "URL-driven compose deeplink; send via Ctrl+Enter"),
            Intent("outlook_archive_current", "()", "Archive the currently open thread"),
            Intent("outlook_screenshot", "()", "PNG screenshot of the current Outlook tab"),
        ],
        context_hints={
            "/mail/": ["outlook_list_recent", "outlook_compose"],
            "deeplink/compose": ["outlook_compose (re-call with send=True to dispatch)"],
        },
        flow_hints=["compose → send=True → search to verify"],
    ),
    Recipe(
        id="canvas",
        site_mcp="canvas",
        url_match=["canvas.ucdavis.edu", "instructure.com"],
        open_url="https://canvas.ucdavis.edu/",
        intents=[
            Intent("canvas_list_courses", "()", "List enrolled / favorited courses"),
            Intent("canvas_open_course", "(course_id)", "Open course homepage"),
            Intent("canvas_list_assignments", "(course_id, count=25)", "List assignments for a course"),
            Intent("canvas_list_announcements", "(course_id, count=15)", "List recent announcements"),
            Intent("canvas_list_grades", "(course_id)", "List grades for a course"),
            Intent("canvas_screenshot", "()", "PNG screenshot of the current Canvas tab"),
        ],
        context_hints={
            "/courses/": ["canvas_list_assignments", "canvas_list_announcements", "canvas_list_grades"],
        },
        flow_hints=["list_courses → open_course → list_assignments / list_announcements / list_grades"],
    ),
    Recipe(
        id="claude_ai",
        site_mcp="claude-ai",
        url_match=["claude.ai"],
        open_url="https://claude.ai/recents",
        intents=[
            Intent("claude_ai_open_recents", "()", "Navigate to claude.ai Recents"),
            Intent("claude_ai_list_recent_chats", "(count=10)", "List recent chats"),
            Intent("claude_ai_open_chat", "(index)", "Open chat at position index"),
            Intent("claude_ai_new_chat", "(message)", "Start a fresh chat at /new"),
            Intent("claude_ai_send_message", "(message)", "Send a message in the active chat"),
            Intent("claude_ai_get_last_response", "()", "Return most recent assistant message"),
            Intent("claude_ai_screenshot", "()", "PNG screenshot of the current claude.ai tab"),
        ],
        context_hints={
            "/recents": ["claude_ai_list_recent_chats", "claude_ai_open_chat", "claude_ai_new_chat"],
            "/chat/": ["claude_ai_send_message", "claude_ai_get_last_response"],
            "/new": ["claude_ai_new_chat"],
        },
        flow_hints=["new_chat(msg) → get_last_response → send_message(follow-up)"],
    ),
    Recipe(
        id="fu_berlin",
        site_mcp="fu-berlin",
        url_match=["webmail.zedat.fu-berlin.de", "fu-berlin.de", "blackboard.fu-berlin.de"],
        open_url="https://webmail.zedat.fu-berlin.de/",
        intents=[
            Intent("fu_berlin_open_inbox", "()", "Open the SquirrelMail INBOX"),
            Intent("fu_berlin_list_recent", "(count=15)", "List recent messages"),
            Intent("fu_berlin_search", "(query, count=10)", "Search the INBOX"),
            Intent("fu_berlin_open_email", "(passed_id, mailbox='INBOX')", "Open a message by passed_id"),
            Intent("fu_berlin_compose", "(to, subject, body, send=False)", "Compose pre-filled via compose.php"),
            Intent("fu_berlin_screenshot", "()", "PNG screenshot of ZEDAT webmail"),
        ],
        flow_hints=["list_recent → open_email(passed_id) → compose(to=sender, …, send=True) for reply"],
        notes="See `reference_fu_berlin_login.md` for the TAN login playbook if not signed in.",
    ),
    Recipe(
        id="amazon",
        site_mcp="amazon",
        url_match=["amazon.com", "amazon.de", "amazon.co.uk"],
        open_url="https://www.amazon.com/",
        intents=[
            Intent("amazon_search", "(query, count=12)", "Search products"),
            Intent("amazon_view_product", "(asin)", "Open product detail page"),
            Intent("amazon_add_to_cart", "(asin, quantity=1)", "Add to cart (does NOT check out)"),
            Intent("amazon_view_cart", "()", "Read current cart contents"),
            Intent("amazon_list_orders", "(count=10)", "List recent orders"),
            Intent("amazon_list_wishlist", "()", "List default wishlist"),
        ],
        context_hints={
            "/dp/": ["amazon_view_product (already on product page)", "amazon_add_to_cart"],
            "/cart": ["amazon_view_cart"],
            "/your-orders": ["amazon_list_orders"],
        },
        flow_hints=["search → view_product(asin) → add_to_cart → view_cart"],
    ),
    Recipe(
        id="linkedin",
        site_mcp="linkedin",
        url_match=["linkedin.com"],
        open_url="https://www.linkedin.com/feed/",
        intents=[
            Intent("linkedin_search_people", "(query, count=10)", "Search for people"),
            Intent("linkedin_view_profile", "(url)", "Open a profile; return name, headline, summary"),
            Intent("linkedin_list_messages", "(count=15)", "List recent message threads"),
            Intent("linkedin_send_message", "(profile_url, text)", "Send a DM to the person at profile_url"),
        ],
        context_hints={
            "/in/": ["linkedin_view_profile (already on profile)", "linkedin_send_message"],
            "/messaging": ["linkedin_list_messages"],
        },
        flow_hints=["search_people → view_profile(url) → send_message(profile_url, text)"],
    ),
    Recipe(
        id="notion",
        site_mcp="notion",
        url_match=["notion.so", "notion.site"],
        open_url="https://www.notion.so/",
        intents=[
            Intent("notion_search", "(query, count=10)", "Search the workspace"),
            Intent("notion_open_page", "(url)", "Open a page; return title and visible text"),
            Intent("notion_list_recent", "(count=15)", "List recently visited pages"),
            Intent("notion_append_to_page", "(url, text)", "Append a paragraph to a page"),
            Intent("notion_create_page", "(title, body='')", "Create a new top-level page"),
        ],
        flow_hints=["search → open_page(url) → append_to_page(url, text) OR create_page(title, body)"],
    ),
    Recipe(
        id="youtube",
        site_mcp="youtube",
        url_match=["youtube.com", "youtu.be"],
        open_url="https://www.youtube.com/",
        intents=[
            Intent("youtube_search", "(query, count=10)", "Search videos"),
            Intent("youtube_open_video", "(url)", "Open a video; return title, channel, description"),
            Intent("youtube_get_transcript", "(url)", "Open transcript pane and return text"),
            Intent("youtube_list_subscriptions", "()", "List latest videos from subscriptions"),
            Intent("youtube_list_watchlater", "()", "List Watch Later playlist"),
        ],
        context_hints={
            "/watch?v=": ["youtube_open_video (already loaded)", "youtube_get_transcript"],
            "/feed/subscriptions": ["youtube_list_subscriptions"],
            "/playlist?list=WL": ["youtube_list_watchlater"],
        },
        flow_hints=["search → open_video(url) → get_transcript(url)"],
    ),
    Recipe(
        id="luma",
        site_mcp="luma",
        url_match=["lu.ma", "luma.com"],
        open_url="https://lu.ma/home",
        intents=[
            Intent("luma_list_upcoming", "(count=15)", "List events you're attending or invited to"),
            Intent("luma_view_event", "(url)", "Open an event; return title, host, time, description"),
            Intent("luma_rsvp", "(url)", "Click Register / RSVP on an event page"),
        ],
        flow_hints=["list_upcoming → view_event(url) → rsvp(url)"],
    ),
    Recipe(
        id="zoom",
        site_mcp="zoom",
        url_match=["zoom.us"],
        open_url="https://zoom.us/meeting",
        intents=[
            Intent("zoom_list_upcoming", "(count=15)", "List upcoming meetings"),
            Intent("zoom_list_recordings", "(count=15)", "List recent cloud recordings"),
        ],
    ),
    Recipe(
        id="calendly",
        site_mcp="calendly",
        url_match=["calendly.com"],
        open_url="https://calendly.com/event_types/user/me",
        intents=[
            Intent("calendly_list_event_types", "()", "List bookable event-type links"),
            Intent("calendly_list_scheduled", "(count=15)", "List upcoming scheduled meetings"),
        ],
    ),
    Recipe(
        id="pubmed",
        site_mcp="pubmed",
        url_match=["pubmed.ncbi.nlm.nih.gov"],
        open_url="https://pubmed.ncbi.nlm.nih.gov/",
        intents=[
            Intent("pubmed_search", "(query, count=10)", "Search PubMed (MeSH, [tw], etc.)"),
            Intent("pubmed_get_abstract", "(pmid)", "Open by PMID; return title, authors, abstract"),
        ],
        flow_hints=["search → get_abstract(pmid)"],
    ),
    Recipe(
        id="skyscanner",
        site_mcp="skyscanner",
        url_match=["skyscanner."],
        open_url="https://www.skyscanner.com/",
        intents=[
            Intent("skyscanner_search_flights", "(origin, destination, depart, return_date='', adults=1)", "Search for flights"),
            Intent("skyscanner_list_saved", "()", "List saved searches / price alerts"),
        ],
    ),
    Recipe(
        id="wikipedia",
        site_mcp="wikipedia",
        url_match=["wikipedia.org"],
        open_url="https://en.wikipedia.org/",
        intents=[
            Intent("wikipedia_search", "(query, count=10)", "Search Wikipedia"),
            Intent("wikipedia_get_article", "(url, section='')", "Open an article; return lead and section list"),
        ],
        flow_hints=["search → get_article(url, section='')"],
    ),
]


def find_recipe(url: str) -> Recipe | None:
    """Return the first recipe whose URL patterns match `url`, or None."""
    for r in RECIPES:
        if matches(r, url):
            return r
    return None


def context_intents_for(recipe: Recipe, url: str) -> list[str]:
    """Return tool hints that apply specifically to the current URL fragment."""
    out: list[str] = []
    for sub, tools in recipe.context_hints.items():
        if sub in url:
            out.extend(tools)
    return out
