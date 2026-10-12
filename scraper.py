#!/usr/bin/env python3

import argparse
import os
import re
import sys
import time
import zipfile
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from selenium import webdriver
from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
    NoSuchElementException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service


# ============================================================
# Configuration
# ============================================================

NAVIGATION_TIMEOUT = 60
IMAGE_RETRIES = 3
RETRY_DELAY = 1
CHAPTER_DELAY = 2

# The only hosts the browser may reach. The series page loads a
# storm of ad-sync frames (hundreds within seconds) that knocks
# out chromedriver's connection mid-command, and nothing the
# scraper needs comes from them.
#
# The image CDN has to be reachable all the same: a chapter
# page's images point at demoniclibs.com, and one that fails to
# load is switched by the page's own tryAgain() to a backup CDN
# (librarydm.com) - whose certificate Python cannot verify.
# Blocked, every image "fails" and the scraper reads the backup's
# addresses instead of the real ones.
BROWSER_HOSTS = ["demonicscans.org", "demoniclibs.com"]

SCRIPT_DIR = Path(__file__).resolve().parent / "out"

# Where the series folders and the error logs go. Both default to beside
# this script; --output and --log-dir change them (see main()).
OUTPUT_DIR = SCRIPT_DIR
LOG_DIR = SCRIPT_DIR / "logs"


# ============================================================
# Logging
# ============================================================

def create_error_logger():
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = LOG_DIR / f"errors.{timestamp}.log"

    logger = logging.getLogger("scraper")
    logger.setLevel(logging.ERROR)

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    logger.addHandler(handler)

    return logger, log_path


def log_error(logger, chapter, filename, message, attempt=None):
    if attempt is not None:
        message = f"{message} (attempt {attempt}/{IMAGE_RETRIES})"

    logger.error(
        'Chapter="%s" File="%s": %s',
        chapter,
        filename,
        message,
    )


# ============================================================
# Command line
# ============================================================

def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Download manga chapters and create CBZ files.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Examples:

  Download a single chapter:
    python scraper.py "https://example.com/title/manga/chapter-106"

  Download all chapters:
    python scraper.py "https://example.com/manga/manga-name"

  Start at a specific chapter:
    python scraper.py --start-chapter "Chapter 106" "https://example.com/manga/manga-name"

  Allow existing CBZ files to be overwritten:
    python scraper.py --overwrite "https://example.com/manga/manga-name"

  Combine options:
    python scraper.py --overwrite --start-chapter "Chapter 106" "https://example.com/manga/manga-name"
""",
    )

    parser.add_argument(
        "--start-chapter",
        metavar="TEXT",
        help=(
            "Start downloading at the first chapter whose title "
            "contains TEXT."
        ),
    )

    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow existing CBZ files to be overwritten.",
    )

    parser.add_argument(
        "url",
        nargs="?",
        help="Chapter (/title/) or manga (/manga/) URL.",
    )
    
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="Download chapters in reverse order.",
    )

    parser.add_argument(
        "--output",
        metavar="DIR",
        help=(
            "Folder to put the series folder in. "
            "Defaults to out/ beside this script."
        ),
    )

    parser.add_argument(
        "--log-dir",
        metavar="DIR",
        help=(
            "Folder for the error logs. "
            "Defaults to logs/ beside this script."
        ),
    )

    return parser.parse_args()


# ============================================================
# Filename handling
# ============================================================

def sanitize_filename(name):
    """
    Remove characters that are invalid in Windows filenames.
    """

    # Windows-invalid characters
    name = re.sub(r'[<>:"/\\|?*]', "_", name)

    # Remove control characters
    name = "".join(
        char for char in name
        if ord(char) >= 32
    )

    # Windows doesn't like trailing spaces/dots
    name = name.rstrip(" .")

    if not name:
        name = "Unknown"

    return name


# ============================================================
# Selenium
# ============================================================

def create_driver():
    options = Options()

    # Headless Chrome
    options.add_argument("--headless=new")

    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    options.add_argument(
        "--window-size=1920,1080"
    )

    # Every other host fails to resolve, so no ad network loads.
    allowed = ", ".join(
        f"EXCLUDE {host}, EXCLUDE *.{host}"
        for host in BROWSER_HOSTS
    )

    options.add_argument(
        "--host-resolver-rules="
        f"MAP * ~NOTFOUND, {allowed}, EXCLUDE localhost"
    )

    # A browser and driver named in the environment are used
    # as they are, rather than Selenium Manager finding (or
    # downloading) its own. The comic updater's container
    # names its Chromium and chromedriver this way.
    browser = os.environ.get("MIRROR_BROWSER_BINARY")
    driver_path = os.environ.get("MIRROR_DRIVER_BINARY")

    if browser:
        options.binary_location = browser

    if driver_path:
        driver = webdriver.Chrome(
            service=Service(executable_path=driver_path),
            options=options,
        )
    else:
        driver = webdriver.Chrome(options=options)

    driver.set_page_load_timeout(NAVIGATION_TIMEOUT)

    return driver


def navigate(driver, url, logger, chapter=""):
    """
    Navigate to a page.

    Selenium can throw TimeoutException even when the page itself
    has loaded enough for us to continue, so we deliberately allow
    navigation timeouts to continue after logging them.
    """

    try:
        driver.get(url)
        return True

    except TimeoutException as error:
        message = (
            f"Navigation timeout while loading {url}: "
            f"{error}"
        )

        log_error(
            logger,
            chapter or "Unknown",
            "PAGE",
            message,
        )

        print(
            f"\nWARNING: Navigation timeout for {url}"
        )

        # The browser may still have loaded the useful page.
        return True

    except WebDriverException as error:
        log_error(
            logger,
            chapter or "Unknown",
            "PAGE",
            f"Navigation failed for {url}: {error}",
        )

        print(
            f"\nERROR: Could not load {url}: {error}"
        )

        return False


# ============================================================
# Chapter list
# ============================================================

def get_manga_info(driver):
    """
    Extract the manga/series name from the manga list page.
    """

    element = driver.find_element(
        By.CSS_SELECTOR,
        "div#manga-info-rightColumn h1.big-fat-titles",
    )

    return element.text.strip()


def get_chapter_list(driver):
    """
    Get all chapter links from the manga page.

    The span inside each link is removed before extracting
    the chapter title.
    """

    links = driver.find_elements(
        By.CSS_SELECTOR,
        "div#chapters-list a",
    )

    chapters = []

    for link in links:
        try:
            href = link.get_attribute("href")

            # Clone the link into JavaScript and remove spans
            title = driver.execute_script(
                """
                const link = arguments[0].cloneNode(true);

                link.querySelectorAll("span").forEach(
                    span => span.remove()
                );

                return link.textContent.trim();
                """,
                link,
            )

            if href and title:
                chapters.append({
                    "href": href,
                    "title": title,
                })

        except WebDriverException as error:
            print(
                f"WARNING: Failed to read chapter link: {error}"
            )

    return chapters


# ============================================================
# Image extraction
# ============================================================

def get_chapter_images(driver):
    """
    Find all img.imgholder elements and return their absolute URLs.

    /img/free_ads.jpg is ignored.
    """

    images = driver.find_elements(
        By.CSS_SELECTOR,
        "img.imgholder",
    )

    result = []

    for image in images:
        src = image.get_attribute("src")

        if not src:
            continue

        # Skip the advertisement image
        parsed = urlparse(src)

        if parsed.path == "/img/free_ads.jpg":
            continue

        result.append(src)

    return result


# ============================================================
# Requests session
# ============================================================

def create_requests_session(driver):
    """
    Create a requests.Session that shares cookies with Selenium.

    This lets the image requests use the same session/cookies as
    the browser.
    """

    session = requests.Session()

    # Copy Selenium cookies
    for cookie in driver.get_cookies():
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
        )

    # Use a browser-like User-Agent
    try:
        user_agent = driver.execute_script(
            "return navigator.userAgent;"
        )

        session.headers.update({
            "User-Agent": user_agent,
        })

    except WebDriverException:
        pass

    session.headers.update({
        "Accept": (
            "image/avif,image/webp,image/apng,"
            "image/svg+xml,image/*,*/*;q=0.8"
        ),
        "Referer": driver.current_url,
    })

    return session


# ============================================================
# Image downloading
# ============================================================

def download_image(
    session,
    image_url,
    chapter_title,
    filename,
    logger,
):
    """
    Download an image with retry support.

    Retries:
      - HTTP 5xx errors
      - connection errors
      - timeouts
      - other request exceptions
    """

    delay = RETRY_DELAY

    for attempt in range(1, IMAGE_RETRIES + 1):

        try:
            response = session.get(
                image_url,
                timeout=60,
                stream=True,
            )

            status = response.status_code

            if status == 200:
                return response.content

            # Server errors should be retried
            if 500 <= status < 600:
                log_error(
                    logger,
                    chapter_title,
                    filename,
                    f"Server returned HTTP {status} for {image_url}",
                    attempt,
                )

                if attempt < IMAGE_RETRIES:
                    time.sleep(delay)
                    delay *= 2

                continue

            # Other HTTP errors aren't necessarily recoverable
            log_error(
                logger,
                chapter_title,
                filename,
                f"HTTP {status} while downloading {image_url}",
                attempt,
            )

            return None

        except requests.RequestException as error:

            log_error(
                logger,
                chapter_title,
                filename,
                f"Download error for {image_url}: {error}",
                attempt,
            )

            if attempt < IMAGE_RETRIES:
                time.sleep(delay)
                delay *= 2

    return None


# ============================================================
# CBZ
# ============================================================

def create_cbz(output_path, images):
    """
    Create a CBZ file.

    images is a list of:
        (filename, bytes)
    """

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with zipfile.ZipFile(
        output_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as cbz:

        for filename, data in images:
            cbz.writestr(filename, data)


# ============================================================
# Progress bar
# ============================================================

def format_progress(current, total, filename):
    """
    Create a single-line progress display.
    """

    percent = (
        (current / total) * 100
        if total
        else 100
    )

    width = 40

    filled = int(
        width * current / total
    ) if total else width

    bar = (
        "=" * filled
        + " " * (width - filled)
    )

    return (
        f"\r{bar} "
        f"{current}/{total} "
        f"{percent:3.0f}% "
        f"Downloading: {filename}"
    )


# ============================================================
# Chapter download
# ============================================================

def download_chapter(
    driver,
    session,
    chapter_url,
    series_name,
    chapter_title,
    logger,
):
    """
    Load and download a chapter.
    """

    print(
        f"\n\nDownloading: "
        f"{series_name} - {chapter_title}"
    )

    # --------------------------------------------------------
    # Navigate to chapter
    # --------------------------------------------------------

    if not navigate(
        driver,
        chapter_url,
        logger,
        chapter_title,
    ):
        return False

    # --------------------------------------------------------
    # Find images
    # --------------------------------------------------------

    try:
        image_urls = get_chapter_images(driver)

    except WebDriverException as error:
        log_error(
            logger,
            chapter_title,
            "PAGE",
            f"Could not find chapter images: {error}",
        )

        return False

    print(
        f"Found {len(image_urls)} images."
    )

    if not image_urls:
        log_error(
            logger,
            chapter_title,
            "PAGE",
            "No chapter images were found.",
        )

        return False

    # Refresh session cookies in case the chapter page
    # changed them.
    session = create_requests_session(driver)

    # --------------------------------------------------------
    # Download images
    # --------------------------------------------------------

    downloaded_images = []

    total = len(image_urls)

    for index, image_url in enumerate(
        image_urls,
        start=1,
    ):

        parsed = urlparse(image_url)

        original_filename = os.path.basename(
            parsed.path
        )

        if not original_filename:
            original_filename = f"{index}.jpg"

        # CBZ image filenames are numbered so they remain
        # in the correct reading order.
        extension = Path(
            original_filename
        ).suffix

        if not extension:
            extension = ".jpg"

        cbz_filename = (
            f"{index:03d}{extension}"
        )

        print(
            format_progress(
                index - 1,
                total,
                original_filename,
            ),
            end="",
            flush=True,
        )

        data = download_image(
            session,
            image_url,
            chapter_title,
            original_filename,
            logger,
        )

        if data is None:
            # Leave the failed image out of the CBZ.
            print(
                f"\nFAILED: {original_filename}"
            )

            continue

        downloaded_images.append(
            (
                cbz_filename,
                data,
            )
        )

        print(
            format_progress(
                index,
                total,
                original_filename,
            ),
            end="",
            flush=True,
        )

    print()

    # --------------------------------------------------------
    # Check whether all images succeeded
    # --------------------------------------------------------

    if len(downloaded_images) != total:
        failed = (
            total - len(downloaded_images)
        )

        log_error(
            logger,
            chapter_title,
            "CBZ",
            f"{failed} image(s) failed to download; "
            f"CBZ was not created.",
        )

        print(
            f"ERROR: {failed} image(s) failed. "
            f"CBZ was not created."
        )

        return False

    # --------------------------------------------------------
    # Create CBZ
    # --------------------------------------------------------

    safe_series = sanitize_filename(
        series_name
    )

    safe_chapter = sanitize_filename(
        chapter_title
    )

    series_dir = (
        OUTPUT_DIR / safe_series
    )

    output_path = (
        series_dir
        / f"{safe_chapter}.cbz"
    )

    try:
        create_cbz(
            output_path,
            downloaded_images,
        )

    except OSError as error:
        log_error(
            logger,
            chapter_title,
            "CBZ",
            f"Failed to create CBZ: {error}",
        )

        print(
            f"ERROR: Failed to create "
            f"{output_path}: {error}"
        )

        return False

    print(
        f"Saved: {output_path}"
    )

    return True


# ============================================================
# Single chapter
# ============================================================

def process_single_chapter(
    driver,
    url,
    logger,
    overwrite,
):
    """
    Process a /title/ URL.

    Returns True when the chapter was saved or was already
    there, False when something went wrong.
    """

    if not navigate(
        driver,
        url,
        logger,
    ):
        return False

    try:
        # The single-chapter page still uses chapter-info.
        series_name = driver.find_element(
            By.CSS_SELECTOR,
            ".chapter-info h1",
        ).text.strip()

        chapter_title = driver.find_element(
            By.CSS_SELECTOR,
            ".chapter-info h2",
        ).text.strip()

    except NoSuchElementException as error:
        log_error(
            logger,
            "Unknown",
            "PAGE",
            f"Could not find chapter information: {error}",
        )

        print(
            "ERROR: Could not find chapter information."
        )

        return False

    safe_series = sanitize_filename(
        series_name
    )

    safe_chapter = sanitize_filename(
        chapter_title
    )

    output_path = (
        OUTPUT_DIR
        / safe_series
        / f"{safe_chapter}.cbz"
    )

    # IMPORTANT:
    # Check BEFORE downloading images.
    if output_path.exists() and not overwrite:
        print(
            f"Skipping: {output_path}"
            "\nUse --overwrite to replace it."
        )

        return True

    session = create_requests_session(
        driver
    )

    return download_chapter(
        driver,
        session,
        url,
        series_name,
        chapter_title,
        logger,
    )


# ============================================================
# Manga
# ============================================================

def process_manga(
    driver,
    url,
    logger,
    start_chapter,
    overwrite,
    reverse,
):
    """
    Process a /manga/ URL.

    Returns True when every chapter it reached was saved or
    was already there, False when anything went wrong.
    """

    if not navigate(
        driver,
        url,
        logger,
    ):
        return False

    # --------------------------------------------------------
    # Get series name
    # --------------------------------------------------------

    try:
        series_name = get_manga_info(
            driver
        )

    except NoSuchElementException as error:
        log_error(
            logger,
            "Unknown",
            "PAGE",
            (
                "Could not find manga series name "
                f"div#manga-info-rightColumn "
                f"h1.big-fat-titles: {error}"
            ),
        )

        print(
            "ERROR: Could not find manga series name."
        )

        return False

    print(
        f"Series: {series_name}"
    )

    # --------------------------------------------------------
    # Get chapter links
    # --------------------------------------------------------

    try:
        chapters = get_chapter_list(
            driver
        )

    except WebDriverException as error:
        log_error(
            logger,
            "Unknown",
            "PAGE",
            f"Could not retrieve chapter list: {error}",
        )

        print(
            "ERROR: Could not retrieve chapter list."
        )

        return False
    
    if reverse:
        chapters.reverse()
        print("Chapter order reversed.")
    
    print(
        f"Found {len(chapters)} chapters."
    )

    # A series page with no chapters on it is a page that was
    # not read as one, not a series with nothing in it yet.
    if not chapters:
        print(
            "ERROR: No chapters were found on the series page."
        )

        return False

    failed = []

    # --------------------------------------------------------
    # Determine starting chapter
    # --------------------------------------------------------

    processing = (
        start_chapter is None
    )

    found_start = (
        start_chapter is None
    )

    # --------------------------------------------------------
    # Process chapters
    # --------------------------------------------------------

    for chapter in chapters:

        title = chapter["title"]
        href = chapter["href"]

        # ----------------------------------------------------
        # Skip chapters before --start-chapter
        #
        # IMPORTANT:
        # We inspect the chapter title from the manga page
        # BEFORE calling driver.get().
        # ----------------------------------------------------

        if not processing:

            if start_chapter in title:
                processing = True
                found_start = True

                print(
                    f"\nStarting from chapter: "
                    f"{title}"
                )

            else:
                #print( f"Skipping chapter: {title}" )

                # Absolutely no delay here.
                continue

        # ----------------------------------------------------
        # Check existing CBZ BEFORE loading chapter page
        # ----------------------------------------------------

        safe_series = sanitize_filename(
            series_name
        )

        safe_chapter = sanitize_filename(
            title
        )

        output_path = (
            OUTPUT_DIR
            / safe_series
            / f"{safe_chapter}.cbz"
        )

        if output_path.exists() and not overwrite:

            print(
                f"Skipping existing: "
                f"{output_path}"
            )

            # No need to load the chapter page.
            continue

        # ----------------------------------------------------
        # Load chapter and download images
        # ----------------------------------------------------

        session = create_requests_session(
            driver
        )

        success = download_chapter(
            driver,
            session,
            href,
            series_name,
            title,
            logger,
        )

        # ----------------------------------------------------
        # Delay ONLY after processing a chapter
        # ----------------------------------------------------

        if success:
            time.sleep(
                CHAPTER_DELAY
            )
        else:
            failed.append(title)

    # --------------------------------------------------------
    # Start chapter wasn't found
    # --------------------------------------------------------

    if (
        start_chapter is not None
        and not found_start
    ):
        print(
            f"\nWARNING: No chapter containing "
            f'"{start_chapter}" was found.'
        )

        log_error(
            logger,
            "Unknown",
            "CHAPTER LIST",
            (
                "Start chapter was not found: "
                f"{start_chapter}"
            ),
        )

        return False

    if failed:
        print(
            f"\nERROR: {len(failed)} chapter(s) failed: "
            + ", ".join(failed)
        )

        return False

    return True


# ============================================================
# Main
# ============================================================

def main():
    args = parse_arguments()

    if not args.url:
        print(
            "ERROR: A URL is required.\n"
        )

        print(
            "Use --help for usage information."
        )

        sys.exit(1)

    url = args.url

    # --------------------------------------------------------
    # Validate URL type
    # --------------------------------------------------------

    if "/title/" not in url and "/manga/" not in url:
        print(
            "ERROR: URL must contain either "
            '"/title/" or "/manga/".'
        )

        sys.exit(1)

    # --------------------------------------------------------
    # Where the chapters and the logs go
    # --------------------------------------------------------

    global OUTPUT_DIR, LOG_DIR

    if args.output:
        OUTPUT_DIR = Path(args.output).resolve()

    if args.log_dir:
        LOG_DIR = Path(args.log_dir).resolve()

    # --------------------------------------------------------
    # Create logger
    # --------------------------------------------------------

    logger, log_path = (
        create_error_logger()
    )

    print(
        f"Error log: {log_path}"
    )

    # --------------------------------------------------------
    # Start Selenium
    # --------------------------------------------------------

    driver = None

    # The exit code says how it went, so whatever runs this
    # (the comic updater, a scheduled task) can tell a broken
    # run from one with nothing new:
    #   0 everything it reached was saved or already there
    #   1 bad arguments
    #   2 something failed; the output and the log say what
    #   130 interrupted
    exit_code = 2

    try:
        driver = create_driver()

        # ----------------------------------------------------
        # Single chapter
        # ----------------------------------------------------

        if "/title/" in url:

            if args.start_chapter:
                print(
                    "WARNING: --start-chapter is "
                    "ignored for a /title/ URL."
                )

            ok = process_single_chapter(
                driver,
                url,
                logger,
                args.overwrite,
            )

            exit_code = 0 if ok else 2

        # ----------------------------------------------------
        # Manga
        # ----------------------------------------------------

        elif "/manga/" in url:

            ok = process_manga(
                driver,
                url,
                logger,
                args.start_chapter,
                args.overwrite,
                args.reverse,
            )

            exit_code = 0 if ok else 2

    except KeyboardInterrupt:
        print(
            "\n\nInterrupted by user."
        )

        exit_code = 130

    except Exception as error:
        log_error(
            logger,
            "Unknown",
            "SCRIPT",
            f"Unhandled error: {error}",
        )

        print(
            f"\nERROR: {error}"
        )

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

        # Close logging handlers
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)

        print(
            "\nDone." if exit_code == 0
            else f"\nFinished with problems (exit {exit_code})."
        )

    sys.exit(exit_code)


if __name__ == "__main__":
    main()