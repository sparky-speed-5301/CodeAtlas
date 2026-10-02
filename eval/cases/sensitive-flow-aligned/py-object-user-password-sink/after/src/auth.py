import logging
logger = logging.getLogger(__name__)

def debug_user(user):
    user_pw = user.password
    logger.info(user_pw)
