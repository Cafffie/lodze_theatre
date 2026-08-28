"""Configuration for the Łódź Theatre (biletyna.pl) scraper."""

BASE_URL = "https://biletyna.pl"
LISTING_URL = "https://biletyna.pl/spektakl/Lodz?city_id=21"

DEFAULT_CITY = "Łódź"
DEFAULT_COUNTRY = "Poland"
DEFAULT_CURRENCY = "PLN"

# biletyna.pl serves its cookie banner through Cookiebot. The "allow all"
# button keeps this element id regardless of the page's display language.
COOKIE_ACCEPT_XPATH = "//*[@id='CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll']"

# The listing page lazy-loads additional performances as the visitor scrolls
# (the page ships a `data-infinity_scroll` attribute). Keep scrolling until
# the number of collected performances stops growing for this many
# consecutive rounds, capped by MAX_SCROLL_ATTEMPTS as a hard ceiling.
MAX_SCROLL_ATTEMPTS = 8
SCROLL_STABLE_ROUNDS = 2

REQUEST_DELAY = 2
