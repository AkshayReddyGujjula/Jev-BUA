# Decision Lab smoke (offline mock run)

A second, unrelated app: a Vite + React decision assistant whose `?mock=1` mode replays a
recorded run with no API keys. Start it with `npx vite --port 5180` in its `web/` folder.

base_url: http://localhost:5180
isolate: context

## The mock run shows the live feed and its cost meter
path: /?mock=1

1. Read the page.
   - see: Live feed
   - see: COST
   - no page errors

## Answering the waiting question moves the conversation on
path: /?mock=1

1. Wait for the question addressed to you, answer it with the first option, and send it.
   - not see: how big is this hackathon for you?

## A question can be skipped
path: /?mock=1

1. Wait for the question addressed to you and skip it.
   - not see: how big is this hackathon for you?
