"""Optional Docker daemon backend. Never attempts nested Docker on Bothost."""
import io
import os
import re
import tarfile
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
    def _container(self, bot_id):
        import docker
        try:
            container = self.client.containers.get(self.name(bot_id))
        except docker.errors.NotFound:
            raise RuntimeError('Контейнер проекта не запущен.') from None
        if container.labels.get('emerald.bot') != bot_id or container.labels.get('emerald.namespace') != self.namespace:
            raise RuntimeError('Имя Docker-контейнера занято другим проектом.')
        container.reload()
        if not container.attrs.get('State', {}).get('Running'):
            raise RuntimeError('Контейнер проекта остановлен.')
        return container
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
    def _existing_template_volumes(self, bot_id):
        volumes={}
        try:
            found=self.client.volumes.list(filters={'label':f'emerald.bot={bot_id}'})
        except Exception:
            found=[]
        for volume in found:
            labels=volume.attrs.get('Labels') or {}
            if labels.get('emerald.namespace')!=self.namespace:
                continue
            folder=labels.get('emerald.folder')
            if folder in {'configs','logs','storage','plugins'}:
                volumes[volume.name]={'bind':f'/app/{folder}','mode':'rw'}
        return volumes
    def _template_volumes(self, bot_id, template_id=None):
        existing=self._existing_template_volumes(bot_id)
        if existing or template_id!='funpay-cardinal':
            return existing
        volumes={}
        for folder in ('configs','logs','storage','plugins'):
            name=f'{self.namespace}-{bot_id.replace("-","")[:16]}-{folder}'
            labels={'emerald.bot':bot_id,'emerald.namespace':self.namespace,'emerald.template':template_id,'emerald.folder':folder}
            try:
                volume=self.client.volumes.get(name)
            except Exception:
                volume=self.client.volumes.create(name=name,labels=labels)
            volumes[volume.name]={'bind':f'/app/{folder}','mode':'rw'}
        return volumes
    def start(self, bot_id, env, template_id=None, setup=False):
        from docker.types import LogConfig
        self.stop_existing(bot_id)
        command=['sh','-lc','while :; do sleep 3600; done'] if setup else None
        container=self.client.containers.run(self.tag(bot_id),command=command,detach=True,name=self.name(bot_id),
                    environment=env,volumes=self._template_volumes(bot_id,template_id),
                    labels={'emerald.bot':bot_id,'emerald.namespace':self.namespace,'emerald.template':template_id or ''},
                    init=True,cap_drop=['ALL'],security_opt=['no-new-privileges:true'],
                    mem_limit=os.getenv('DOCKER_BOT_MEMORY','256m'),nano_cpus=1000000000,pids_limit=128,
                    restart_policy={'Name':'no'},log_config=LogConfig(type='json-file',config={'max-size':'5m','max-file':'2'}))
        return DockerProcess(container)
    def put_text(self, bot_id, path, text):
        container=self._container(bot_id)
        directory,filename=os.path.split(path)
        payload=io.BytesIO()
        raw=str(text).encode('utf-8')
        with tarfile.open(fileobj=payload,mode='w') as archive:
            info=tarfile.TarInfo(filename);info.size=len(raw);info.mode=0o600
            archive.addfile(info,io.BytesIO(raw))
        payload.seek(0)
        if not container.put_archive(directory or '/',payload.read()):
            raise RuntimeError('Не удалось добавить setup helper в контейнер.')
    def open_terminal(self, bot_id):
        container=self._container(bot_id)
        result=container.exec_run(['/bin/sh'],stdin=True,stdout=True,stderr=True,tty=True,socket=True,
                                  environment={'TERM':'xterm-256color','LANG':'C.UTF-8'},workdir='/app')
        if not result.output:
            raise RuntimeError('Не удалось открыть PTY контейнера.')
        return result.output
    def delete(self, bot_id):
        import docker
        self.stop_existing(bot_id)
        try:
            self.client.images.remove(self.tag(bot_id))
        except docker.errors.ImageNotFound:
            pass
        try:
            for volume in self.client.volumes.list(filters={'label': f'emerald.bot={bot_id}'}):
                if (volume.attrs.get('Labels') or {}).get('emerald.namespace')==self.namespace:
                    try: volume.remove(force=True)
                    except Exception: pass
        except Exception:
            pass
