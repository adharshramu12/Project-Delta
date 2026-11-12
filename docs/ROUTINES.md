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
