#scheduler_run.py
from app import app 
from functions.scheduler import init_scheduler

init_scheduler(app)


import time
try:
    while True:
        time.sleep(60)
except KeyboardInterrupt:
    pass
