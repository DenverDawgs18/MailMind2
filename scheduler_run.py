"""Entry point for the Fly `scheduler` process: runs the digest cron."""
import logging
import time

from app import app
from functions.scheduler import get_scheduler_status, init_scheduler

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

init_scheduler(app)
logger.info("Scheduler status: %s", get_scheduler_status())

try:
    while True:
        time.sleep(60)
except KeyboardInterrupt:
    pass
