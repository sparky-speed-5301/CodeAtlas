import logging
logger = logging.getLogger(__name__)

def hash_pw(pw): return 'sha256'

def login(password):
    hashed = hash(password)
    logger.info(hashed)
