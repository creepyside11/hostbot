"""Start the stable Emerald worker plus GitHub and SQLite companions."""
import runpy
import threading
import private_repo_patch  # noqa: F401 - applies Runner patch on import
from github_watch import watch_forever as github_watch_forever
from sqlite_inspector import watch_forever as sqlite_watch_forever

threading.Thread(target=github_watch_forever,name='github-auto-update',daemon=True).start()
threading.Thread(target=sqlite_watch_forever,name='sqlite-inspector',daemon=True).start()
runpy.run_path('/opt/emerald/main.py',run_name='__main__')
