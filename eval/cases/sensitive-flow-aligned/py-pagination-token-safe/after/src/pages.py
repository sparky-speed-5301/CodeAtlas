import logging
logger = logging.getLogger(__name__)

def fetch(page):
    token = page.next_cursor
    logger.info(token)
