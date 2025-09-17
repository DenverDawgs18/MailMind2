#scheduler_run.py
from app import app 
from functions.scheduler import init_scheduler, get_scheduler_status
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)



init_scheduler(app)
status = get_scheduler_status()
logger.info("Status: ", status)

import time
try:
    while True:
        time.sleep(60)
except KeyboardInterrupt:
    pass

