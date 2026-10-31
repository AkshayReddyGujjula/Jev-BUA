# Moneywell Town core flows (draft)

Drafted by `jev draft` from the start page and the pages it links to. 'predicted' checks quote text the drafter did not see on any page it read: confirm them on the first run. 'weak' checks were already true before the step: replace them with what the step should change.

base_url: http://localhost:5190

## Open the How It Works page from home
path: /

1. Open How It Works from the home page
   <!-- weak: already on the start page -->
   - see: RESIDENT PASS
   <!-- weak: already on the start page -->
   - see: Moneywell Town is for ages 15 and up
   - not see: HOW IT WORKS

## Decline optional analytics from home
path: /

1. Choose no analytics on the home page
   <!-- weak: already on the start page -->
   - see: Current choice:
   <!-- weak: already on the start page -->
   - see: NO ANALYTICS
   - not see: not chosen

## Get a resident pass with an underage birthday
path: /

1. Open PLAY and enter a birthday that is under 15
   <!-- weak: already on the start page -->
   - see: RESIDENT PASS
   <!-- weak: already on the start page -->
   - see: GET MY PASS
   - not see: WELCOME, NEWCOMER!
2. Enter the day "10", month "05", year "2018" and get my pass
   <!-- weak: already on the start page -->
   - see: RESIDENT PASS

## Get a resident pass with an empty birthday
path: /

1. Open PLAY and press GET MY PASS without entering a birthday
   <!-- weak: already on the start page -->
   - see: RESIDENT PASS
   <!-- weak: already on the start page -->
   - see: GET MY PASS
   - not see: WELCOME, NEWCOMER!

## Read the privacy and safety page
path: /

1. Open Privacy & safety from the home page
   <!-- weak: already on the start page -->
   - see: YOUR DATA. YOUR CHOICES.
   <!-- weak: already on the start page -->
   - see: Your save stays on your device
   - not see: HOW IT WORKS
2. Jump to the Your age section
   <!-- weak: already on the start page -->
   - see: Your age
3. Return to home with Back to home
   <!-- weak: already on the start page -->
   - see: HOW IT WORKS
   <!-- weak: already on the start page -->
   - see: Optional analytics
   - not see: YOUR DATA. YOUR CHOICES.

## Analytics choice is kept on this tab
path: /

1. Choose allow analytics on the home page
   <!-- weak: already on the start page -->
   - see: Current choice:
   <!-- weak: already on the start page -->
   - see: ALLOW ANALYTICS
   - not see: not chosen
2. Open Privacy & safety and check the saved choice
   <!-- weak: already on the start page -->
   - see: Current choice:
   <!-- weak: already on the start page -->
   - see: YOUR DATA. YOUR CHOICES.

## Back to home from a linked page
path: /

1. Open PLAY from the home page
   <!-- weak: already on the start page -->
   - see: RESIDENT PASS
   <!-- weak: already on the start page -->
   - see: GET MY PASS
   - not see: HOW IT WORKS
2. Go Back to home from the resident pass screen
   <!-- weak: already on the start page -->
   - see: HOW IT WORKS
   <!-- weak: already on the start page -->
   - see: PLAY
   - not see: GET MY PASS
