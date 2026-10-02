import logging
logger = logging.getLogger(__name__)

def handle(request):
    token = request.token
    logger.info(token)
