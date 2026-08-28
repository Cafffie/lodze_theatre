"""Test script for the Łódź Theatre extractor."""
from scrapers.lodztheatre.run_extractor import LodzTheatreExtractor
from utils.logger import setup_logger

logger = setup_logger(__name__, log_to_file=False)


def test_lodztheatre_extractor():
    """Test the Łódź Theatre extractor against a small sample of shows."""
    logger.info("Starting Łódź Theatre extractor test...")

    extractor = LodzTheatreExtractor(
        local_test=True,
        show_count=2,
        save_csv_locally=True,
        csv_incremental_mode=False,
        log_to_file=True,
        log_to_terminal=True,
    )

    result = extractor.run()

    logger.info(f"Test completed with result: {result}")
    return result


if __name__ == "__main__":
    test_lodztheatre_extractor()
