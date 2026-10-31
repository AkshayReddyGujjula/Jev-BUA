# Moneywell Town age gate (third app)

A browser game from another project (`Desktop\Hackathons\WIF Hackathon\wif-team17`), run read-only
with `npx vite --port 5190`. The game opens behind a 15+ age check whose rules live in
`src/data/ageCheck.ts`; these tests exercise its three outcomes. Written by hand from the source in
a few minutes; `drafted.md` beside it is what `jev draft` proposed from the landing page alone.

base_url: http://localhost:5190
isolate: context

## An adult gets a pass and reaches the title screen
path: /game
values:
  - DAY: 14
  - MONTH: 03
  - YEAR: 1990

1. Enter the birthday and get my pass.
   - see any: NEW GAME | START TUTORIAL | PRESS START
   - not see: GET MY PASS

## A child under 15 is refused politely
path: /game
values:
  - DAY: 10
  - MONTH: 05
  - YEAR: 2018

1. Enter the birthday and get my pass.
   - see: Come back when you’re 15!
   - not see: GET MY PASS

## A two-digit year is rejected with a hint
path: /game
values:
  - DAY: 01
  - MONTH: 01
  - YEAR: 99

1. Enter the birthday and get my pass.
   - see: Try again with the full four-digit year
   - see: GET MY PASS
