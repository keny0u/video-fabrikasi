import socket
import sys
import webbrowser
from pathlib import Path

from fabrika import config


def already_running():
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex((config.HOST, config.PORT)) == 0


def main():
    background = "--arka" in sys.argv
    url = f"http://{config.HOST}:{config.PORT}"
    # İkinci kopya başlarsa aynı video iki kez yayınlanır; açık olana yönlendir.
    if already_running():
        if not background:
            webbrowser.open(url)
        return
    if background:
        log = open(Path(config.BASE) / "fabrika.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log

    from waitress import serve

    from fabrika import db
    from fabrika.clips.pipeline import ClipWorker
    from fabrika.scheduler import Scheduler
    from fabrika.series.manager import SeriesWorker
    from fabrika.web import app

    db.init()
    Scheduler().start()
    ClipWorker().start()
    SeriesWorker().start()
    print(f"Panel: {url}")
    if not background:
        webbrowser.open(url)
    serve(app, host=config.HOST, port=config.PORT, threads=8, max_request_body_size=20 * 1024 ** 3)


if __name__ == "__main__":
    main()
