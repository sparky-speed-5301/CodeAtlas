import logging
logger = logging.getLogger(__name__)

def audit(request):
    auth_hdr = request.headers.authorization
    logger.info(auth_hdr)
