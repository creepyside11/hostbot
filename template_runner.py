"""Prepare ready-made bots without starting their normal application command."""
import threading
from pathlib import Path


def prepare_template(runner, bot, secrets):
    bot_id=str(bot['id'])
    template_id=bot.get('template_id')
    if template_id!='funpay-cardinal':
        raise RuntimeError('Неизвестный шаблон готового бота.')
    if bot.get('build_mode')!='dockerfile' or not runner.docker:
        raise RuntimeError('Шаблон требует Docker-сборщик.')

    runner.docker_bots.add(bot_id)
    runner.stop(bot_id)
    home=runner.root / bot_id
    (home / 'ready').unlink(missing_ok=True)
    try:
        runner.prepare(bot,secrets)
    except Exception as error:
        error.log_stream='build'
        raise

    with runner.lock:
        if runner.closing.is_set():
            raise RuntimeError('Исполнитель завершается.')
        process=runner.docker.start(bot_id,secrets,template_id=template_id,setup=True)
        runner.processes[bot_id]=process
    threading.Thread(target=runner.reader,args=(bot_id,process,secrets),daemon=True).start()

    helper=(Path(__file__).resolve().parent/'template_setup_funpay.py').read_text(encoding='utf-8')
    runner.docker.put_text(bot_id,'/app/emerald_setup.py',helper)
    if runner.closing.wait(1):
        raise RuntimeError('Исполнитель завершается.')
    if process.poll() is not None:
        runner.stop(bot_id)
        raise RuntimeError('Setup-контейнер завершился сразу после запуска.')
    runner.log(bot_id,'Шаблон собран. Откройте «Терминал» и завершите первичную настройку.')
