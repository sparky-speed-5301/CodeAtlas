import logging
logger = logging.getLogger(__name__)

def record(user):
    password = user.password
    logger.info(f"User password is {password}")
