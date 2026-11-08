# Jira timeline: reference

The detail behind the jira-timeline skill. It was checked against Atlassian's Cloud documents on 6 Oct 2026. Atlassian's interface now says "work items" for issues and "spaces" for projects, but the API, JQL and field ids still use issue and project.

## Fields and payloads

**Start date**
- This is a custom field, often `customfield_10015`, but never hard-code that: it varies by site.
- If several fields are called "Start date", the timeline uses the locked one. `GET /rest/api/3/field/search?query=Start%20date&expand=isLocked` shows which.
- The tools resolve it for you (`start_date=`).

**Due date** is `duedate`. Changing it needs the Schedule Issues permission.

Both dates are date-only:
```json
PUT /rest/api/3/issue/PRJ-12
{ "fields": { "customfield_10015": "2026-11-02", "duedate": "2026-11-27", "parent": { "key": "PRJ-100" } } }
```

**Parent**
- `parent` replaced Epic Link and Parent Link, which were removed from the REST API in 2022.
- `"parent": null` detaches a ticket.

**Fix versions (milestones)**
```json
PUT /rest/api/3/issue/PRJ-12
{ "update": { "fixVersions": [ { "add": { "name": "M2 - Top deck clear" } } ] } }
```

**Creating a release** needs project admin:
```json
POST /rest/api/3/version
{ "projectId": 10001, "name": "M2 - Top deck clear", "releaseDate": "2026-11-25", "released": false }
```
- Team-managed projects must switch on "Releases and versions" first.
- On the timeline, a release's dot is green when shipped, red when behind and blue when in the future.

## Links

**Link types on a site.** A Jira Cloud site typically has Blocks (blocks / is blocked by), Cloners, Duplicate and Relates, plus types added by products and apps (for example Problem/Incident from Jira Service Management, and Product Discovery's types). List them with `GET /rest/api/3/issueLinkType`.

Only **Blocks** is drawn.

**Creating a link**
- `POST /rest/api/3/issueLink` with `{"type": {"name": "Blocks"}, "inwardIssue": {...}, "outwardIssue": {...}}`.
- The evidence (Atlassian's linking model, client-library fixes, and a bug report against Atlassian's own MCP server) is that the issue sent as **inwardIssue** gets the outward wording "blocks". Atlassian's own KB article says the opposite.
- `link_issues` therefore creates the link, reads the blocker back, and if the link came out reversed, deletes it and creates it the other way.

**Reading links:** in `fields.issuelinks[]`, an entry with `outwardIssue` uses the type's outward wording ("this issue blocks X"), and one with `inwardIssue` uses the inward wording ("this issue is blocked by X").

**Deleting a link:** `DELETE /rest/api/3/issueLink/{id}`. It needs the Link Issues permission. Delete a link only when the dependency genuinely no longer exists.

## Rank

- The call is `PUT /rest/agile/1.0/issue/rank` with `{"issues": [...], "rankAfterIssue": "KEY"}` or `"rankBeforeIssue"`.
- It takes at most 50 issues per call, and keeps them in the order given.
- 204 means done. 207 means some failed; read the body.
- It needs the Schedule Issues permission and the OAuth scope `write:issue:jira-software`.
- `rank_issues` splits long lists into chunks of 50 and anchors each chunk after the last one, so the whole list stays in order.

## Roll-up rules (basic timeline)

- An epic with **no** dates of its own shows a striped bar from its earliest child start to its latest child due, or from sprint dates when its children are scheduled by sprint.
- Dates set on the epic override the roll-up.
- If only one end can be worked out, the bar fades out in that direction.
- Known Jira bug: sprint dates roll up to the epic even when sprint scheduling is off (JRACLOUD-91539).
- Sprints only appear if they fall between one year ago and two years ahead.

## Limits

- Basic timeline: 5,000 items and 500 epics, in one project.
- Done items more than a year old drop off. Completed items with no Due date can vanish if Due date isn't in the field configuration.
- Premium Plans: up to 10,000 items across up to 200 projects, with custom levels above Epic and a choice of which date fields to use. Plan roll-ups are display only; they are never written back to the fields.

## JQL for checking by hand

```
project = PRJ AND issuetype = Epic AND ("Start date" is EMPTY OR duedate is EMPTY)
project = PRJ AND issuetype != Epic AND issuetype in standardIssueTypes() AND duedate is EMPTY AND sprint is EMPTY
project = PRJ AND duedate < startOfDay() AND statusCategory != Done
project = PRJ AND "Start date" <= startOfDay() AND statusCategory = "To Do"
project = PRJ AND issuetype != Epic AND issuetype in standardIssueTypes() AND parent is EMPTY
parent = PRJ-100
project = PRJ AND issueLinkType = "is blocked by" AND statusCategory != Done
issue in linkedIssues("PRJ-5", "is blocked by")
project = PRJ AND assignee is EMPTY AND statusCategory != Done
```

Notes on these queries:
- If "Start date" is ambiguous, use `cf[<id>]` with the site's field id (e.g. `cf[10015]`).
- `issueLinkType` takes the link wording ("blocks", "is blocked by") and can't be combined with EMPTY.
- Children outside their epic and red arrows can't be found with JQL. `timeline_audit` works them out by comparing dates.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No bar | no dates; or child scheduling off; or the child is scheduled by sprint with no sprint | set both dates; turn on child-level scheduling (by dates) |
| Bar fades out | one date only | set the other |
| "Update your board JQL" | the board filter has no `project = KEY` clause | the project admin fixes the board filter |
| Can't reorder | the board filter isn't `ORDER BY Rank ASC`, or no Schedule Issues permission | fix the filter, or the permission |
| Arrow missing | not a Blocks link, or the other ticket is in another project | `link_issues`; same project only |
| Date edit rejected | Start date not on the edit screen (company-managed) | an admin adds it to screens and field configuration |
| Epic dates wrong | set by hand and no longer matching the children, or half-set | set both from the plan, or clear both to roll up |
| Done tickets vanish | Due date not in the field configuration, or done more than a year ago | add Due date to the field configuration |

## Sources

- Timeline setup and hierarchy: support.atlassian.com/jira-software-cloud/docs/set-up-the-roadmap/
- Child-level scheduling: …/docs/enable-child-issue-planning-for-kanban-teams-on-the-roadmap/ and …/docs/schedule-work-for-scrum-teams-on-your-roadmap/
- Roll-up: …/docs/roll-up-dates-on-your-timeline/
- Dependencies: …/docs/create-or-remove-dependencies-on-your-timeline/ and …/docs/manage-dependencies-between-epics-on-the-timeline/
- Releases: …/docs/show-or-hide-releases-on-your-project-roadmap/
- Ranking: …/docs/enable-ranking/ and developer.atlassian.com/cloud/jira/software/rest/api-group-issue/ (Rank issues)
- Permissions: support.atlassian.com/jira-cloud-administration/docs/work-item-permissions/
- Linking model: developer.atlassian.com/cloud/jira/platform/issue-linking-model/
- Parent field: community.developer.atlassian.com/t/deprecation-of-the-epic-link-parent-link-and-other-related-fields-in-rest-apis-and-webhooks/54048
- Limits: …/docs/troubleshoot-the-timelines-for-team-managed-projects/ and …/docs/limits-on-plan-size-in-advanced-roadmaps/
