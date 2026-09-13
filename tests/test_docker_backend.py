import io
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from docker_backend import DockerBackend, DockerProcess, LogReader


class DockerTests(unittest.TestCase):
    def backend(self):
        backend=DockerBackend.__new__(DockerBackend)
        backend.namespace='test-emerald'
        backend.client=Mock()
        return backend
    def test_build_routes_output_and_uses_project_context(self):
        backend=self.backend()
        def events():
            yield {'stream':'RUN pip install -r requirements.txt\n'}
            yield {'stream':'Successfully built image\n'}
        backend.client.api.build.return_value=events()
        logs=[]
        backend.build('abc',Path('/source'),'docker/Bot.Dockerfile',logs.append,30,Mock(is_set=lambda:False))
        self.assertEqual(len(logs),2)
        args=backend.client.api.build.call_args.kwargs
        self.assertEqual(args['path'],'/source')
        self.assertEqual(args['dockerfile'],'docker/Bot.Dockerfile')
        self.assertNotIn('buildargs',args)
    def test_build_error_does_not_report_success(self):
        backend=self.backend()
        def events():yield {'error':'pip failed'}
        backend.client.api.build.return_value=events()
        with self.assertRaisesRegex(RuntimeError,'ошибкой'):
            backend.build('abc',Path('/source'),'Dockerfile',lambda _:None,30,Mock(is_set=lambda:False))
        backend.client.images.get.assert_not_called()
    def test_start_uses_limits_and_only_supplied_environment(self):
        backend=self.backend()
        backend.stop_existing=Mock()
        container=backend.client.containers.run.return_value
        container.logs.return_value=iter([b'hello\n'])
        process=backend.start('abc',{'BOT_TOKEN':'child-token'})
        args=backend.client.containers.run.call_args.kwargs
        self.assertEqual(args['environment'],{'BOT_TOKEN':'child-token'})
        self.assertEqual(args['cap_drop'],['ALL'])
        self.assertEqual(args['pids_limit'],128)
        self.assertNotIn('volumes',args)
        self.assertNotIn('privileged',args)
        self.assertEqual(process.stdout.readline(),b'hello\n')
        process.stdout.close()
    def test_unrelated_container_is_not_removed(self):
        backend=self.backend()
        container=backend.client.containers.get.return_value
        container.labels={'emerald.bot':'other'}
        with self.assertRaisesRegex(RuntimeError,'другим проектом'):backend.stop_existing('abc')
        container.stop.assert_not_called()
        container.remove.assert_not_called()
    def test_process_exit_and_stop(self):
        container=Mock(attrs={'State':{'Running':False,'ExitCode':3}})
        container.logs.return_value=iter([])
        process=DockerProcess(container)
        self.assertEqual(process.poll(),3)
        process.stop()
        container.stop.assert_called_once_with(timeout=5)
        container.remove.assert_called_once_with(force=True)
        process.stdout.close()

if __name__=='__main__':unittest.main()
