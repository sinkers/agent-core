import json
from datetime import datetime, timezone

import httpx

from agentcore.calendar_tools import CalendarTools
from agentcore.store import MemoryStore
from agentcore.tools import CallContext

NOW = datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)  # Fri 10:00 Hobart


def graph(log, events=None):
    def handler(req):
        if "login.microsoftonline.com" in str(req.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        log.append((req.method, req.url.path, dict(req.url.params), json.loads(req.content) if req.content else None))
        if req.method == "GET":
            return httpx.Response(200, json={"value": events or []})
        if req.method == "POST" and req.url.path.endswith("/events"):
            return httpx.Response(201, json={"id": "ev-9", "webLink": "https://outlook/ev-9",
                                             "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/xyz"}})
        return httpx.Response(204)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def tools(log, events=None, store=None):
    return CalendarTools(store or MemoryStore(), mailbox="ada@acme.example", app_id="a", app_password="p",
                         tenant_id="t", http=graph(log, events), clock=lambda: NOW)


async def test_list_my_meetings_in_hobart_time():
    log = []
    ev = [{"id": "e1", "subject": "PRJ coordination", "start": {"dateTime": "2026-10-05T00:00:00.0000000", "timeZone": "UTC"},
           "end": {"dateTime": "2026-10-05T01:00:00.0000000", "timeZone": "UTC"},
           "organizer": {"emailAddress": {"name": "Erin Hall"}}, "onlineMeeting": {"joinUrl": "https://t/j"}}]
    out = json.loads(await tools(log, ev).call_tool("list_my_meetings", {"days": 7}, CallContext("c", "a", [])))
    assert out["meetings"][0]["start"] == "Mon 05 Oct 11:00" and out["meetings"][0]["organizer"] == "Erin Hall"
    assert log[0][1].endswith("/users/ada@acme.example/calendarView")


async def test_schedule_meeting_creates_a_teams_meeting_and_plans_the_join():
    log, store = [], MemoryStore()
    t = tools(log, store=store)
    out = json.loads(await t.call_tool("schedule_meeting", {
        "subject": "PRJ drawings catch-up", "start": "2026-10-06T14:00", "duration_minutes": 30,
        "attendees": ["erin.hall@acme.example"], "agenda": "Drawing register status"},
        CallContext("c", "aad-alex", ["book a catch up with erin"], actor_name="Alex Owner")))
    assert out["scheduled"] and out["join_url"].startswith("https://teams.microsoft.com")
    body = next(b for m, p, _, b in log if m == "POST")
    assert body["isOnlineMeeting"] and body["onlineMeetingProvider"] == "teamsForBusiness"
    assert body["start"] == {"dateTime": "2026-10-06T14:00:00", "timeZone": "Tasmania Standard Time"}
    assert [a["emailAddress"]["address"] for a in body["attendees"]] == ["erin.hall@acme.example"]
    assert "Alex Owner" in body["body"]["content"]  # says who asked
    pending = await store.get_pref("meetings:ada@acme.example", "pending")
    assert pending[0]["event_id"] == "ev-9" and pending[0]["organizer_aad"] == "aad-alex"


async def test_external_attendees_and_past_times_are_refused():
    log = []
    t = tools(log)
    ctx = CallContext("c", "aad-alex", [], actor_name="Alex Owner")
    ext = json.loads(await t.call_tool("schedule_meeting", {"subject": "x", "start": "2026-10-06T14:00",
                                                            "attendees": ["bob@example.com"]}, ctx))
    past = json.loads(await t.call_tool("schedule_meeting", {"subject": "x", "start": "2026-10-01T09:00",
                                                             "attendees": ["erin.hall@acme.example"]}, ctx))
    assert not ext["scheduled"] and "outside Acme" in ext["reason"]
    assert not past["scheduled"] and "past" in past["reason"]
    assert not any(m == "POST" for m, *_ in log)
