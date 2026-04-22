from datetime import date

from src.domain.wayback import WaybackRelease, select_release


def test_select_release_by_identifier() -> None:
    releases = [
        WaybackRelease("WB_2021_R01", date(2021, 1, 1), "one", 1, ("default028mm",), "https://example.com"),
        WaybackRelease("WB_2022_R01", date(2022, 1, 1), "two", 1, ("default028mm",), "https://example.com"),
    ]
    assert select_release(releases, "WB_2022_R01").identifier == "WB_2022_R01"
