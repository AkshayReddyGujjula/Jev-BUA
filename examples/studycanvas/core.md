# StudyCanvas core (localhost)

Runs against a local dev server (`npm run dev` in frontend, uvicorn on :8000 in backend). Any
access code works locally. `?storage=idb` is StudyCanvas's own dev switch for browser storage,
because the folder picker is a native dialog no browser automation can click.

base_url: http://localhost:5173
isolate: storage
commit_floor: 0.6

Signing in once per run (`## Before all`) matters here: the app rate-limits access-code
redeems to 10 a minute, which a suite that signs in before every test exceeds.

## Before all
path: /workspace?storage=idb
values:
  - Access code: jev-local-test
  - Your name: Ada

1. Enter the access code and continue.
   - not see: Enter your access code
2. Finish the onboarding questions: give the name, skip every optional question, and finish.
   - not see: Your study profile

## Unknown URL shows the 404 page
path: /workspace/no-such-place-xyz?storage=idb

1. Read the page.
   - see: Page not found
   - see: Go home
2. Click Go home.
   - not see: Page not found

## Home screen offers the main actions
path: /workspace?storage=idb

1. Look at the home screen.
   - see: New canvas
   - see any: New space | New Space
   - no page errors

## Keyboard shortcuts sheet opens and groups shortcuts
path: /workspace?storage=idb

1. Open the keyboard shortcuts sheet.
   - see: Anywhere
   - see any: Canvas | Home

## Create a canvas
path: /workspace?storage=idb

1. Create a new canvas named Jev Biology Chapter 3.
   - see: Jev Biology Chapter 3
   - no page errors

## Search with no match explains itself
path: /workspace?storage=idb

1. Search the workspace for zzqxv-nothing.
   - see: Nothing matches
2. Clear the search.
   - not see: Nothing matches
