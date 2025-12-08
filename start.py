import os
import time
from apscheduler.schedulers.background import BackgroundScheduler, BlockingScheduler

def run():
    cmd = "ps -ef | grep app.py | grep -v grep | awk \'{print $2}\' | xargs kill -9"
    print('remove 进程: ', cmd)
    os.system(cmd)

    cmd = 'python3 /home/zbx/database/generate_daily_hdf5.py'
    print('更新数据: ', cmd)
    os.system(cmd)

    cmd = 'bash run.sh'
    print('运行网站: ', cmd)
    os.system(cmd)


scheduler = BackgroundScheduler()
scheduler.add_job(
    run,
    'cron',
    hour=18, minute=17,
    day_of_week='0-4',
    max_instances=2
)
# scheduler = BlockingScheduler()
# scheduler.add_job(
#     run,
#     'interval',
#     minutes=3,
#     max_instances=2
# )


try:
    scheduler.start()
    while True: time.sleep(3600)
except KeyboardInterrupt:
    scheduler.shutdown()
