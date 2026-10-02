import logging
logger = logging.getLogger(__name__)

def check(request, flag):
    auth_val = request.token
    if flag:
        auth_val = 'clean'
    logger.info(auth_val)
