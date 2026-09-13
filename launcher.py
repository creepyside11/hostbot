"""Start the stable Emerald worker plus the GitHub auto-update companion."""
import runpy
import threading
import private_repo_patch  # noqa: F401 - applies Runner patch on import
from github_watch import watch_forever

threading.Thread(target=watch_forever,name='github-auto-update',daemon=True).start()
runpy.run_path('/opt/emerald/main.py',run_name='__main__')
