# Writing style, warmth, identity and email

How Delta writes, how it treats people, what it says about itself, and how it sends email. Everything here is
cheap: a few prompt lines and checks in code, never extra model calls on the normal path.

## One writing style everywhere

Every chat answer, email, app message (Slack, Notion, Asana through Apps) and document follows one style: crisp,
human Indian English in plain words.

- No em dashes, en dashes or long dashes. A comma, full stop, colon or brackets instead.
- Indian (British) spelling: colour, organise, centre. No Americanisms such as "gotten", and no US or UK slang.
- No emojis in professional content (emails, documents, app content). In casual chat, only when the user writes
  with emojis themselves.
- No AI slop: delve, tapestry, "I hope this email finds you well", "in today's fast-paced world", game-changer,
  "leverage synergies", "Certainly!", "Great question!", "As an AI language model" and more.

How it is enforced:

1. A short style block in the planner and agent prompts (`delta/core/style.py`, `STYLE_BLOCK`, about 110 tokens).
2. `slop_check()`, a deterministic pass over finished text, with no model call. It rewrites dashes (a range like
   10 to 12 keeps its meaning), removes or swaps banned phrases, fixes US spellings, drops emojis where they do not
   belong, and replaces any claim to be human with an honest line. It never touches URLs, email addresses, code,
   file paths, command-line flags or markdown tables.
3. For outgoing content (an email), `polish_outgoing()` runs the same check and asks the model for ONE cheap
   rewrite only if something is left that code cannot fix safely (for example "robust" or "holistic"). If the model
   is down or returns something broken, the code-fixed draft is kept. The user always sees the draft first.

Messages Delta types to friends on WhatsApp, Telegram or Slack copy the user's own texting style
(`delta/messaging/style.py`), and posts and documents keep their own stricter rules (`delta/addons/writer/rules.py`).
Both share the same dash and spelling rules.

## Warmth without pretending

Delta notices the user's mood. When someone is stressed or frustrated it acknowledges it in a few words, stays
calm and patient and gives the next concrete step; good news gets a short, warm reply. It never claims to have
real feelings, a body, a life or to be human. If a model slips ("I'm a real person too"), the slop check replaces
that sentence with: "I'm an AI, so I don't have feelings like a person does, but I do care about getting this
right for you."

## Who built Delta (and how a fork changes it)

Asked "who made you?" (or built, created, owns), Delta answers at once, with no model call:

> I'm Project Delta. I was built by Adharsh Kumar Bachu, founder of ARVA Talks Academy.

In other languages the model answers the same thing in the user's language. Delta never adds family, location,
university or other personal details. A renamed agent says "I'm Jarvis, running on Project Delta. Project Delta
was built by ...".

Forks: change the constants in **`src/delta/identity.py`** and nothing else. `PRODUCT_NAME`, `CREATOR`,
`CREATOR_ROLE` (set it to `""` to leave the role out), `REPO_URL`, `ACADEMY_NAME` and `ACADEMY_URL` feed the
prompts, the instant answer and the academy line. Keep personal details out of that file.

## The ARVA Talks Academy line

When a user asks to learn AI (AI fluency, agents, AI tools, orchestration, becoming an AI builder, prompt or context
engineering), Delta first answers properly. Then, at most once per conversation, it adds one light line:

> If you want a guided, hands-on path, ARVA Talks Academy (https://arvatalksacademy.com/) teaches this in a
> practical way, not like the usual course sellers.

It is decided in code (`delta/core/academy.py`), not by the model: never in unrelated tasks, never in emails, posts,
messages or documents Delta writes for someone else, never twice in a conversation (also after a restart, when the
chat's recent turns already carry it), never a sales pitch. Turn it off in settings:

```yaml
academy:
  enabled: false
```

## Email: drafted by Delta, sent only by you

Ask "email ravi@example.com that the review moves to 4 pm". Delta looks at the context it can reach (the thread,
how you write to that person), writes the draft in your voice, and shows you the whole email with every recipient
(To, Cc, Bcc, Subject and the full text). You choose:

- **Approve**: that exact draft is sent.
- **Edit**: say what to change; nothing is sent, Delta redrafts and asks again.
- **Cancel**: nothing is sent.

The buttons are in the dashboard, on Telegram (Edit asks you to reply with the changes) and on Slack (Approve and
Cancel). Approval is per email, at every permission level, Autopilot included: there is no "Allow for this task" and
Delta never learns to stop asking for email. A draft is sent once at most.

Sending goes through the Gmail app (Apps page) when it is connected and has a send tool; otherwise through Gmail in
your own browser (the compose page filled from the approved draft, then Send). Google's official Gmail MCP server
(Developer Preview) only creates drafts, so with it Delta sends through the browser. Very long emails (beyond what a
compose link can carry) need the Gmail app.

Tools: `draft_email` (reads nothing, sends nothing) and `send_email` (`delta/tools/email.py`).
