"""Optional Docker daemon backend. Never attempts nested Docker on Bothost."""
import io
import os
import re
import time


class LogReader(io.RawIOBase):
    def __init__(self, stream):
        self.stream, self.buffer = stream, bytearray()
    def readable(self):
        return True
    def readinto(self, target):
        while not self.buffer:
            try:
                self.buffer.extend(next(self.stream))
            except StopIteration:
                return 0
            except Exception:
                raise OSError('Поток Docker-логов прерван.') from None
        size = min(len(target), len(self.buffer))
        target[:size] = self.buffer[:size]
        del self.buffer[:size]
        return size
    def close(self):
        try:
            self.stream.close()
        except Exception:
            pass
        super().close()


class DockerProcess:
    def __init__(self, container):
        self.container = container
        self.returncode = None
        self.stdout = io.BufferedReader(LogReader(container.logs(stream=True, follow=True)))
    def poll(self):
        self.container.reload()
        state = self.container.attrs['State']
        if not state.get('Running'):
            self.returncode = state.get('ExitCode', 1)
        return self.returncode
    def stop(self):
        try:
            self.container.stop(timeout=5)
        finally:
            self.container.remove(force=True)


class DockerBackend:
    def __init__(self):
        import docker
        if os.environ['DOCKER_HOST'].startswith(('tcp://','http://','https://')) and os.getenv('DOCKER_TLS_VERIFY') != '1':
            raise RuntimeError('Удалённый Docker требует DOCKER_TLS_VERIFY=1 и клиентские TLS-сертификаты.')
        self.client = docker.from_env(timeout=30)
        self.client.ping()
        self.namespace = os.getenv('DOCKER_NAMESPACE', 'emerald-host')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,40}', self.namespace):
            raise RuntimeError('Некорректный DOCKER_NAMESPACE.')
    def name(self, bot_id):
        return self.namespace + '-' + bot_id
    def tag(self, bot_id):
        return self.namespace + ':' + bot_id
    def stop_existing(self, bot_id):
        import docker
        try:
            container = self.client.containers.get(self.name(bot_id))
        except docker.errors.NotFound:
            return
        if container.labels.get('emerald.bot') != bot_id or container.labels.get('emerald.namespace') != self.namespace:
            raise RuntimeError('Имя Docker-контейнера занято другим проектом.')
        try:
            container.stop(timeout=5)
        finally:
            container.remove(force=True)
    def build(self, bot_id, source, dockerfile, log, timeout, closing):
        deadline=time.monotonic()+timeout
        events=self.client.api.build(path=str(source),dockerfile=dockerfile,tag=self.tag(bot_id),
                    rm=True,forcerm=True,decode=True,timeout=timeout,
                    labels={'emerald.bot':bot_id,'emerald.namespace':self.namespace})
        try:
            for event in events:
                if closing.is_set() or time.monotonic()>deadline:
                    raise RuntimeError('Сборка Docker прервана по таймауту или остановке исполнителя.')
                for line in str(event.get('stream') or event.get('status') or event.get('error') or '').splitlines():
                    if line: log(line)
                if event.get('error'):
                    raise RuntimeError('Docker-сборка завершилась ошибкой. Подробности в логах сборки.')
        finally:
            events.close()
        self.client.images.get(self.tag(bot_id))
    def start(self, bot_id, env):
        from docker.types import LogConfig
        self.stop_existing(bot_id)
        container=self.client.containers.run(self.tag(bot_id),detach=True,name=self.name(bot_id),
                    environment=env,labels={'emerald.bot':bot_id,'emerald.namespace':self.namespace},
                    init=True,cap_drop=['ALL'],security_opt=['no-new-privileges:true'],
                    mem_limit=os.getenv('DOCKER_BOT_MEMORY','256m'),nano_cpus=1000000000,pids_limit=128,
                    restart_policy={'Name':'no'},log_config=LogConfig(type='json-file',config={'max-size':'5m','max-file':'2'}))
        return DockerProcess(container)
    def delete(self, bot_id):
        import docker
        self.stop_existing(bot_id)
        try:
            self.client.images.remove(self.tag(bot_id))
        except docker.errors.ImageNotFound:
            pass
