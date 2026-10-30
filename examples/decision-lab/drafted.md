# Decision Lab smoke tests (draft)

Drafted by `jev draft` from one read of the start page. Checks marked 'predicted' quote text the drafter expected on a later screen: confirm them on the first run.

base_url: http://localhost:5180

## Open the demo decision with initial state
path: /?mock=1

1. View the starting conversation for the hackathon decision
   - see: Decision Lab
   - see: Conversation
   - see: Research
   - not see: Error
   - not see: Something went wrong

## Switch from Simple to Detailed mode
path: /?mock=1

1. Select Detailed mode
   - see: Detailed
   - see: Planning the research

## Open and inspect the open models button
path: /?mock=1

1. Click the 3 open models button
   - see: 3 open models

## Start a new decision
path: /?mock=1

1. Click New decision
   - see: Understanding your decision
   - not see: I've got a hackathon tomorrow

## Switch between Conversation and Research tabs
path: /?mock=1

1. Open the Research tab
   - see: Research
   - see: Live feed
2. Go back to the Conversation tab
   - see: Conversation

## Collapse and expand the live feed
path: /?mock=1

1. Collapse the live feed
   - see: Live feed
2. Show the activity again
   - see: Show activity

## Zoom the planning graph out and in
path: /?mock=1

1. Zoom out the planning graph
   - see: React Flow
2. Zoom in the planning graph
   - see: React Flow
3. Fit the planning graph to view
   - see: React Flow
