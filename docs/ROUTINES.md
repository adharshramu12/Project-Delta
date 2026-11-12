# Routines: do it once, then run it again with no AI

A routine is a task you did once with Delta and saved under a name. Running it repeats the same steps, in the same
order, without asking the AI anything. It is for things you do again and again: open your portal and go to the
timetable, open three sites every morning, open an app and a page together.

## Use it

1. Ask Delta to do the task the normal way, and wait until it has worked.
2. In the same chat, say **save this as timetable** (any name you like).
   Delta shows the steps it will repeat, in plain words.
3. Any time later, say **run timetable** (or just **timetable**).

Other commands: **my routines** lists them with their steps, **delete routine timetable** removes one. Saving under
a name that exists replaces the old routine. All four work in the app, in the terminal and on Telegram, and none of
them uses the AI.

## Or show Delta how: record it yourself

You do not have to let the AI do the task first. You can do it by hand and Delta watches.

1. Say **start recording** (or **watch me**). Delta's own browser opens and comes to the front.
   "start recording on myportal.example" opens that page first.
2. Do the task there yourself: click, type, choose from lists, move between pages.
3. Say **stop recording and save as timetable**. Delta shows the steps it kept.
4. Later: **run timetable**.

"stop recording" with no name shows the steps and asks for one ("save recording as timetable"). "cancel recording"
throws it away.

What Delta notes is the **name** of each thing you act on: the text of the link or button, the label of the field
and what you typed, the option you chose. It is not a video and not screen positions, so it still works when the
window is another size.

- **Passwords are never recorded.** What you type into a password, card or one-time-code field is not sent to Delta
  at all. Delta's browser stays signed in between runs; if the site asks for the password again, the routine stops
  there.
- It works in **Delta's own browser** only, not in your everyday Chrome or Edge, and the routine runs there too.
- Not recorded yet: tick boxes, drag and drop, typing into rich text boxes (a mail body, a chat box), and anything
  in another program.

## What it costs

Measured on a real PC with Groq: "open example.com and click Learn more" took 66 seconds, 5 AI calls and 16,209
tokens the first time. Saved as a routine, it ran in 6 seconds with no AI call for the steps.

## What can and cannot be saved

- Only the steps that **worked** are saved. Failed tries and steps that only looked at something (reading a page,
  a web search) are left out.
- A task where Delta clicked by **screen position** or worked from a **screenshot** cannot be saved: positions are
  different next time. Delta tells you so when you try.
- Browser clicks and fields are saved by their **name** ("Timetable", "Year"), not by a number on the page.
- A routine holds at most 20 steps.
- The steps repeat **exactly**. A routine has no blanks to fill in yet: "send this to Ravi" saved once always
  means Ravi.

## What keeps it safe

- Every step goes through the same checks as when the AI drives: your permission level, the approval prompt,
  Remote Guard on Telegram. A step that **sends, pays or deletes asks you every time**, also inside a routine.
- The **first step that fails stops the routine**. Delta says which step, why, and what was already done. The
  remaining steps do not run, and nothing is retried blindly.
- Sites change. When a button is renamed, the step that clicks it fails and the routine stops there; do the task
  once more and save it again under the same name.

## Where it is kept

`routines.db` in Delta's data folder (`%LOCALAPPDATA%\Delta`): the name, the steps with the values they ran with,
and how often it ran. The values are stored as they were, so do not save a routine from a task where you typed
something private. A saved login is fine: the step holds the login's name, never the password.
