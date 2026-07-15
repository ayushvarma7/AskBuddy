# Slack App Permissions — Ask Buddy

Single source of truth for every Slack-side setting Ask Buddy needs: scopes,
tokens, event subscriptions, interactivity, and the slash command. If a
feature stops working ("app did not respond", feedback buttons do nothing,
no reply to DMs), check the matching section here first.

---

## 1. Create the app

1. Go to **<https://api.slack.com/apps>** → **Create New App** → **From scratch**.
2. App Name: `Ask Buddy`. Pick your workspace. **Create App**.

---

## 2. Bot Token Scopes

**OAuth & Permissions → Bot Token Scopes → Add an OAuth Scope**, one at a time:

| Scope | Why Ask Buddy needs it |
|---|---|
| `chat:write` | Post answers and feedback-confirmation messages as @Ask Buddy |
| `chat:write.public` | Post in public channels the bot hasn't been invited to (e.g. `/askbuddy` in any channel) |
| `im:history` | Read DM history so the bot can see incoming direct messages |
| `im:read` | List/open DM conversations |
| `im:write` | Start DM conversations |
| `users:read` | Look up the Slack user who asked a question or clicked feedback |
| `channels:history` | Read channel history so `@mention` events resolve correctly |
| `channels:read` | Look up a public channel's ID from its name — needed so reminder commands like "remind #svl-interns-2026 to submit timecards" can resolve the channel to post to |
| `groups:read` | Same lookup for private channels (skip if reminders will only ever target public channels) |
| `app_mentions:read` | Receive `@Ask Buddy` mentions in channels |
| `commands` | Register and receive the `/askbuddy` slash command |

Missing `commands` is the most common cause of *"/askbuddy failed because
the app did not respond"* — Slack rejects the command server-side before it
ever reaches the bot if the scope isn't granted.

Missing `channels:read` causes reminder creation to fail with "Could not
find a Slack channel named '...'" even when the channel exists.

After adding scopes: **Install to Workspace → Allow**, then copy the
**Bot User OAuth Token** (starts with `xoxb-`).

> Any time you add a new scope after the first install, Slack requires a
> **reinstall**: OAuth & Permissions → **Reinstall to Workspace** → Allow.
> The bot token does not change automatically — re-copy it if it did.

---

## 3. Socket Mode + App-Level Token

Ask Buddy runs over Socket Mode (no public Request URL needed).

1. **Settings → Socket Mode** → toggle **Enable Socket Mode** → On.
2. Under **App-Level Tokens** → **Generate Token and Scopes**:
   - Token Name: `askbuddy-socket` (or anything)
   - Add scope: `connections:write`
   - **Generate**
3. Copy the **App-Level Token** (starts with `xapp-1-`).

---

## 4. Event Subscriptions

**Settings → Event Subscriptions** → toggle **Enable Events** → On.

Under **Subscribe to bot events**, add:
- `message.im` — required, so the bot receives direct messages
- `app_mention` — required, so `@Ask Buddy` works in channels

**Save Changes**.

---

## 5. Interactivity & Shortcuts

**Settings → Interactivity & Shortcuts** → toggle **Interactivity** → On.

- **Request URL**: any placeholder, e.g. `https://example.com` — Socket Mode
  ignores this field, but Slack requires something non-empty to save.
- **Save Changes**.

This is required for **both**:
- the 👍 / 👎 feedback buttons on every answer, and
- the "what was wrong?" modal that opens on 👎 (asks for a reason before
  recording the rating).

If Interactivity is off: feedback buttons do nothing, and 👎 silently falls
back to recording a bare negative rating with no reason (the modal can't open).

---

## 6. Slash Command — `/askbuddy`

**Settings → Slash Commands** → **Create New Command**:

| Field | Value |
|---|---|
| Command | `/askbuddy` |
| Short Description | `Ask Ask Buddy an HR question` |
| Usage Hint | `how many PTO days do I get?` |
| Request URL | any placeholder, e.g. `https://example.com` (Socket Mode ignores it) |

**Save**. Requires the `commands` Bot Token Scope from section 2 — add it
first if you haven't.

---

## 7. Where tokens go in `.env`

Copy `.env.example` to `.env` if you haven't, then set:

```dotenv
SLACK_BOT_TOKEN=xoxb-...       # from step 2 (Bot User OAuth Token)
SLACK_APP_TOKEN=xapp-1-...     # from step 3 (App-Level Token)
```

Token format check — both are position-sensitive prefixes:
- `SLACK_BOT_TOKEN` must start with `xoxb-`
- `SLACK_APP_TOKEN` must start with `xapp-1-`

`ASK_BUDDY_DIGEST_CHANNEL` (optional, for the weekly digest) is a **channel
ID**, not a bot/app token — see [INSTALLATION.md](INSTALLATION.md) for how to
find one.

---

## 8. Quick troubleshooting

| Symptom | Check |
|---|---|
| `/askbuddy failed because the app did not respond` | `commands` scope added + app reinstalled; slash command registered (section 6); bot process actually restarted after code changes |
| Bot never replies to DMs | `message.im` under Event Subscriptions (section 4); `im:history`/`im:read` scopes (section 2) |
| `@Ask Buddy` mention ignored in channels | `app_mention` event (section 4); `app_mentions:read` + `channels:history` scopes (section 2) |
| Feedback buttons do nothing | Interactivity toggled On (section 5) |
| 👎 doesn't open the reason modal | Same as above — falls back to a bare negative rating if Interactivity is off |
| "missing_scope" API errors in logs | Re-check section 2 against the exact scope name in the error, add it, reinstall |
| Bot posts in a channel it isn't a member of, but shouldn't be able to | Expected — `chat:write.public` intentionally allows this |

---

## Summary checklist

- [ ] All 9 Bot Token Scopes added (section 2)
- [ ] App installed / reinstalled after scope changes
- [ ] Socket Mode enabled, App-Level Token generated with `connections:write`
- [ ] Event Subscriptions on: `message.im`, `app_mention`
- [ ] Interactivity enabled
- [ ] `/askbuddy` slash command created
- [ ] `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` set in `.env`
