"""Constants for RuTracker.org Transmission."""

DOMAIN = "rutracker_transmission"

SERVICE_SEARCH = "search"
SERVICE_ADD = "add"
SERVICE_LOGIN = "login"

SIGNAL_RESULTS_UPDATED = f"{DOMAIN}_results_updated"
SIGNAL_SELECTION_UPDATED = f"{DOMAIN}_selection_updated"

DEFAULT_RUTRACKER_URL = "https://rutracker.org/forum"
DEFAULT_FEED_URL = "https://feed.rutracker.cc/atom/f/{forum_id}.atom"
DEFAULT_COOKIES_FILE = "/config/.storage/rutracker.cookies"

DEFAULT_TRANSMISSION_HOST = "172.20.0.1"
DEFAULT_TRANSMISSION_PORT = 9091
DEFAULT_TRANSMISSION_PATH = "/transmission/rpc"
# This configured path belongs to the remote Transmission service.
DEFAULT_TEST_DOWNLOAD_DIR = "/tmp/ha-rutracker-check"  # noqa: S108

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

SERIES_FORUMS = [
    842,
    235,
    189,
    2366,
    2396,
    1803,
    119,
    9,
    81,
    921,
    1460,
    498,
    507,
    315,
    287,
]
MOVIE_FORUMS = [7, 22, 313, 1457, 1950, 252]

FORUM_NAMES = {
    7: "Movies",
    9: "Russian series",
    22: "Russian movies",
    81: "Russian series HD",
    119: "Series UHD",
    235: "US and Canada series",
    189: "Foreign series",
    252: "Movies 2026",
    287: "Mobile video",
    313: "Movies HD",
    315: "Series HD for Apple TV",
    498: "Animation series UHD",
    507: "The Big Bang Theory and Young Sheldon",
    842: "New and airing series",
    921: "Animation series",
    1457: "Movies UHD",
    1460: "Animation series HD",
    1803: "New series HD",
    1950: "Movies 2021-2025",
    2366: "Series HD",
    2396: "The Big Bang Theory HD",
}
