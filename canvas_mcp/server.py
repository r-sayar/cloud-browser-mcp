#!/usr/bin/env python3
"""canvas_mcp — UC Davis Canvas (Instructure LMS) as a high-level MCP.
Read-only by design: list courses, list assignments / announcements / grades
per course, open a course or assignment.

Sign in once via http://localhost:6081/ before these tools work.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP
from mcp_lib import client, ensure_tab, js

CANVAS_HOME = "https://canvas.ucdavis.edu/"
CANVAS_HOST = "canvas.ucdavis.edu"
mcp = FastMCP("canvas")


async def _tab() -> int:
    return await ensure_tab(CANVAS_HOST, CANVAS_HOME)


async def _logged_in(pid: int) -> tuple[bool, str]:
    info = await js(pid, """
      JSON.stringify({
        url: location.href,
        loggedIn: location.hostname.endsWith('canvas.ucdavis.edu') &&
                  !location.href.includes('/login'),
      })
    """)
    if isinstance(info, str): info = json.loads(info)
    return info.get("loggedIn", False), info.get("url", "")


def _login_error(url: str) -> str:
    return json.dumps({
        "ok": False, "error": "not_logged_in", "url": url,
        "next_actions": [
            "Open http://localhost:6081/ on your laptop, sign into canvas.ucdavis.edu, retry.",
        ],
    }, indent=2)


@mcp.tool()
async def canvas_list_courses() -> str:
    """List the user's enrolled / favorited courses (visible on the dashboard)."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": CANVAS_HOME + "courses"})
    await asyncio.sleep(1.5)
    ok, url = await _logged_in(pid)
    if not ok: return _login_error(url)
    items = await js(pid, """
      (() => {
        // The /courses page lists enrolled courses in tables.
        const rows = document.querySelectorAll('table tr[data-course-id], a.course-list-link');
        const out = [];
        rows.forEach((r, i) => {
          const link = r.matches('a') ? r : r.querySelector('a[href*="/courses/"]');
          if (!link) return;
          const m = link.href.match(/\\/courses\\/(\\d+)/);
          if (!m) return;
          out.push({
            index: out.length,
            courseId: m[1],
            title: link.innerText.trim().slice(0, 200),
            href: link.href,
          });
        });
        return JSON.stringify(out);
      })()
    """)
    if isinstance(items, str): items = json.loads(items)
    return json.dumps({
        "courses": items, "count": len(items),
        "next_actions": [
            "canvas_open_course(course_id='12345')",
            "canvas_list_assignments(course_id='12345')",
            "canvas_list_announcements(course_id='12345')",
            "canvas_list_grades(course_id='12345')",
        ],
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def canvas_open_course(course_id: str) -> str:
    """Open the course homepage for `course_id`."""
    pid = await _tab()
    await client().call("navigate_page", {"page": pid, "url": f"{CANVAS_HOME}courses/{course_id}"})
    await asyncio.sleep(1.5)
    ok, url = await _logged_in(pid)
    if not ok: return _login_error(url)
    info = await js(pid, """
      JSON.stringify({
        title: document.title,
        url: location.href,
        breadcrumb: document.querySelector('h1')?.innerText.trim() || '',
      })
    """)
    if isinstance(info, str): info = json.loads(info)
    info["next_actions"] = [
        f"canvas_list_assignments(course_id='{course_id}')",
        f"canvas_list_announcements(course_id='{course_id}')",
        f"canvas_list_grades(course_id='{course_id}')",
    ]
    return json.dumps(info, indent=2)


@mcp.tool()
async def canvas_list_assignments(course_id: str, count: int = 25) -> str:
    """List assignments for `course_id`."""
    pid = await _tab()
    await client().call("navigate_page", {
        "page": pid, "url": f"{CANVAS_HOME}courses/{course_id}/assignments"
    })
    await asyncio.sleep(1.5)
    ok, url = await _logged_in(pid)
    if not ok: return _login_error(url)
    items = await js(pid, f"""
      (() => {{
        const rows = document.querySelectorAll('div.assignment, li.assignment, a.ig-title');
        const out = [];
        for (const r of rows) {{
          const link = r.matches('a') ? r : r.querySelector('a[href*="/assignments/"]');
          if (!link) continue;
          const due = r.querySelector('.assignment-date-due time, .due_at')?.innerText?.trim() || '';
          const points = r.querySelector('.score-display, .points_possible')?.innerText?.trim() || '';
          out.push({{
            index: out.length,
            title: link.innerText.trim().slice(0, 200),
            href: link.href,
            due, points,
          }});
          if (out.length >= {count}) break;
        }}
        return JSON.stringify(out);
      }})()
    """)
    if isinstance(items, str): items = json.loads(items)
    return json.dumps({
        "assignments": items, "count": len(items), "course_id": course_id,
        "next_actions": [
            f"canvas_list_grades(course_id='{course_id}')",
            "Open a specific assignment URL via the BrowserOS navigate_page tool",
        ],
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def canvas_list_announcements(course_id: str, count: int = 15) -> str:
    """List recent announcements for `course_id`."""
    pid = await _tab()
    await client().call("navigate_page", {
        "page": pid, "url": f"{CANVAS_HOME}courses/{course_id}/announcements"
    })
    await asyncio.sleep(1.5)
    ok, url = await _logged_in(pid)
    if not ok: return _login_error(url)
    items = await js(pid, f"""
      (() => {{
        const rows = document.querySelectorAll('div[data-testid*="announcement"], li.announcement, div.discussion-list-row');
        const out = [];
        for (const r of rows) {{
          const link = r.querySelector('a[href*="/announcements/"], a[href*="/discussion_topics/"]');
          if (!link) continue;
          out.push({{
            index: out.length,
            title: link.innerText.trim().slice(0, 200),
            href: link.href,
            posted: r.querySelector('time, .posted_at')?.innerText?.trim() || '',
          }});
          if (out.length >= {count}) break;
        }}
        return JSON.stringify(out);
      }})()
    """)
    if isinstance(items, str): items = json.loads(items)
    return json.dumps({
        "announcements": items, "count": len(items), "course_id": course_id,
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def canvas_list_grades(course_id: str) -> str:
    """List grades for `course_id`."""
    pid = await _tab()
    await client().call("navigate_page", {
        "page": pid, "url": f"{CANVAS_HOME}courses/{course_id}/grades"
    })
    await asyncio.sleep(1.5)
    ok, url = await _logged_in(pid)
    if not ok: return _login_error(url)
    items = await js(pid, """
      (() => {
        const rows = document.querySelectorAll('tr.student_assignment, tr[data-testid*="grade-row"]');
        const out = [];
        rows.forEach((r, i) => {
          const title = r.querySelector('th, .title')?.innerText?.trim() || '';
          const grade = r.querySelector('.grade, td.assignment_score')?.innerText?.trim() || '';
          const possible = r.querySelector('.points_possible')?.innerText?.trim() || '';
          if (title) out.push({index: out.length, title, grade, possible});
        });
        return JSON.stringify(out);
      })()
    """)
    if isinstance(items, str): items = json.loads(items)
    return json.dumps({
        "grades": items, "count": len(items), "course_id": course_id,
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def canvas_screenshot() -> str:
    """PNG screenshot of the current Canvas tab."""
    pid = await _tab()
    await client().call("take_screenshot", {"page": pid, "format": "png"})
    return json.dumps({"ok": True, "mime": "image/png"})


def main(): mcp.run(transport="stdio")
if __name__ == "__main__": main()
