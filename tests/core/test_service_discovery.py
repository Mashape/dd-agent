# stdlib
import copy
import exceptions
import mock
import unittest

# project
from utils.service_discovery.config_stores import ConfigStore
from utils.service_discovery.consul_config_store import ConsulStore
from utils.service_discovery.etcd_config_store import EtcdStore
from utils.service_discovery.sd_backend import ServiceDiscoveryBackend, SDDockerBackend


def clear_singletons(sd_backend):
    ConfigStore._drop()
    ServiceDiscoveryBackend._drop()


class Response(object):
    """Dummy response class for mocking purpose"""
    def __init__(self, content):
        self.content = content

    def json(self):
        return self.content


def _get_container_inspect(c_id):
    """Return a mocked container inspect dict from self.container_inspects."""
    for co, _, _ in TestServiceDiscovery.container_inspects:
        if co.get('Id') == c_id:
            return co
        return None


def _get_tpl_conf(image_name):
    """Return a mocked configuration template from self.mock_templates."""
    tpl_conf = copy.deepcopy(TestServiceDiscovery.mock_templates.get(image_name)[0])
    return tpl_conf


def _get_check_tpl(image_name, **kwargs):
    if image_name in TestServiceDiscovery.mock_templates:
        return copy.deepcopy(TestServiceDiscovery.mock_templates.get(image_name)[0][0:3])
    elif image_name in TestServiceDiscovery.bad_mock_templates:
        try:
            return copy.deepcopy(TestServiceDiscovery.bad_mock_templates.get(image_name)[0:3])
        except Exception:
            return None


def client_read(path):
    """Return a mocked string that would normally be read from a config store (etcd, consul...)."""
    parts = path.split('/')
    config_parts = ['check_name', 'init_config', 'instance']
    image, config_part = parts[-2], parts[-1]
    return str(TestServiceDiscovery.mock_templates.get(image)[0][config_parts.index(config_part)])


class TestServiceDiscovery(unittest.TestCase):
    docker_container_inspect = {
        u'Id': u'69ff25598b2314d1cdb7752cc3a659fb1c1352b32546af4f1454321550e842c0',
        u'Image': u'6ffc02088cb870652eca9ccd4c4fb582f75b29af2879792ed09bb46fd1c898ef',
        u'Name': u'/nginx',
        u'NetworkSettings': {u'IPAddress': u'172.17.0.21', u'Ports': {u'443/tcp': None, u'80/tcp': None}}
    }
    kubernetes_container_inspect = {
        u'Id': u'389dc8a4361f3d6c866e9e9a7b6972b26a31c589c4e2f097375d55656a070bc9',
        u'Image': u'de309495e6c7b2071bc60c0b7e4405b0d65e33e3a4b732ad77615d90452dd827',
        u'Name': u'/k8s_sentinel.38057ab9_redis-master_default_27b84e1e-a81c-11e5-8347-42010af00002_f70875a1',
        u'Config': {u'ExposedPorts': {u'6379/tcp': {}}},
        u'NetworkSettings': {u'IPAddress': u'', u'Ports': None}
    }
    malformed_container_inspect = {
        u'Id': u'69ff25598b2314d1cdb7752cc3a659fb1c1352b32546af4f1454321550e842c0',
        u'Image': u'6ffc02088cb870652eca9ccd4c4fb582f75b29af2879792ed09bb46fd1c898ef',
        u'Name': u'/nginx'
    }
    container_inspects = [
        # (inspect_dict, expected_ip, expected_port)
        (docker_container_inspect, '172.17.0.21', '443'),
        (kubernetes_container_inspect, '127.0.0.1', '6379'),  # arbitrarily defined in the mocked pod_list
        (malformed_container_inspect, None, exceptions.KeyError)
    ]

    mock_templates = {
        # image_name: ((check_name, init_tpl, instance_tpl, variables), (expected_config_template))
        'image_0': (('check_0', {}, {'host': '%%host%%'}, ['host']), ('check_0', {}, {'host': '127.0.0.1'})),
        'image_1': (('check_1', {}, {'port': '%%port%%'}, ['port']), ('check_1', {}, {'port': '1337'})),
        'image_2': (
            ('check_2', {}, {'host': '%%host%%', 'port': '%%port%%'}, ['host', 'port']),
            ('check_2', {}, {'host': '127.0.0.1', 'port': '1337'})),
    }

    bad_mock_templates = {
        'bad_image_0': ('invalid template'),
        'bad_image_1': None
    }

    def setUp(self):
        self.etcd_agentConfig = {
            'service_discovery': True,
            'service_discovery_backend': 'docker',
            'sd_template_dir': '/datadog/check_configs',
            'sd_config_backend': 'etcd',
            'sd_backend_host': '127.0.0.1',
            'sd_backend_port': '2380'
        }
        self.consul_agentConfig = {
            'service_discovery': True,
            'service_discovery_backend': 'docker',
            'sd_template_dir': '/datadog/check_configs',
            'sd_config_backend': 'consul',
            'sd_backend_host': '127.0.0.1',
            'sd_backend_port': '8500'
        }
        self.auto_conf_agentConfig = {
            'service_discovery': True,
            'service_discovery_backend': 'docker',
            'sd_template_dir': '/datadog/check_configs',
            'additional_checksd': '/etc/dd-agent/checks.d/',
        }
        self.agentConfigs = [self.etcd_agentConfig, self.consul_agentConfig, self.auto_conf_agentConfig]

    # sd_backend tests

    @mock.patch('requests.get')
    @mock.patch('utils.service_discovery.sd_backend.check_yaml')
    def test_get_host(self, mock_check_yaml, mock_get):
        kubernetes_config = {'instances': [{'kubelet_port': 1337}]}
        pod_list = {
            'items': [{
                'status': {
                    'podIP': '127.0.0.1',
                    'containerStatuses': [
                        {'containerID': 'docker://389dc8a4361f3d6c866e9e9a7b6972b26a31c589c4e2f097375d55656a070bc9'}
                    ]
                }
            }]
        }

        mock_check_yaml.return_value = kubernetes_config
        mock_get.return_value = Response(pod_list)

        for c_ins, expected_ip, _ in self.container_inspects:
            with mock.patch.object(ConfigStore, '__init__', return_value=None):
                with mock.patch('utils.service_discovery.sd_backend.get_docker_client', return_value=None):
                    with mock.patch('utils.service_discovery.sd_backend.get_conf_path', return_value=None):
                        sd_backend = ServiceDiscoveryBackend(agentConfig=self.auto_conf_agentConfig)
                        self.assertEqual(sd_backend._get_host(c_ins), expected_ip)
                        clear_singletons(sd_backend)

    def test_get_port(self):
        with mock.patch('utils.service_discovery.sd_backend.get_docker_client', return_value=None):
            for c_ins, _, expected_port in self.container_inspects:
                sd_backend = ServiceDiscoveryBackend(agentConfig=self.auto_conf_agentConfig)
                if isinstance(expected_port, str):
                    self.assertEqual(sd_backend._get_port(c_ins), expected_port)
                else:
                    with self.assertRaises(expected_port):
                        sd_backend._get_port(c_ins)
                clear_singletons(sd_backend)

    @mock.patch('docker.Client.inspect_container', side_effect=_get_container_inspect)
    @mock.patch.object(SDDockerBackend, '_get_template_config', side_effect=_get_tpl_conf)
    def test_get_check_config(self, mock_inspect_container, mock_get_tpl_conf):
        """Test get_check_config with mocked container inspect and config template"""
        with mock.patch.object(SDDockerBackend, '_get_host', return_value='127.0.0.1'):
            with mock.patch.object(SDDockerBackend, '_get_port', return_value='1337'):
                c_id = self.docker_container_inspect.get('Id')
                for image in self.mock_templates.keys():
                    sd_backend = ServiceDiscoveryBackend(agentConfig=self.auto_conf_agentConfig)
                    self.assertEquals(
                        sd_backend._get_check_config(c_id, image),
                        self.mock_templates[image][1])

    @mock.patch.object(ConfigStore, 'get_check_tpl', side_effect=_get_check_tpl)
    def test_get_template_config(self, mock_get_check_tpl):
        """Test _get_template_config with mocked get_check_tpl"""
        with mock.patch.object(EtcdStore, 'get_client', return_value=None):
            with mock.patch.object(ConsulStore, 'get_client', return_value=None):
                for agentConfig in self.agentConfigs:
                    sd_backend = ServiceDiscoveryBackend(agentConfig=agentConfig)
                    # normal cases
                    for image in self.mock_templates.keys():
                        template = sd_backend._get_template_config(image)
                        expected_template = self.mock_templates.get(image)[0]
                        self.assertEquals(template, expected_template)
                    # error cases
                    for image in self.bad_mock_templates.keys():
                        self.assertEquals(sd_backend._get_template_config(image), None)

    # config_stores tests

    def test_get_auto_config(self):
        """Test _get_auto_config"""
        expected_tpl = {
            'redis': ('redisdb', '{}', '{"host": "%%host%%", "port": "%%port%%"}'),
            'nginx': ('nginx', '{}', '{"nginx_status_url": "http://%%host%%/nginx_status/"}'),
            'consul': ('consul', '{}', '{"url": "http://%%host%%:%%port%%", "catalog_checks": true, "service_whitelist": null, "new_leader_checks": true}'),
            'foobar': None
        }
        config_store = ConfigStore(self.auto_conf_agentConfig)
        for image in expected_tpl.keys():
            config = config_store._get_auto_config(image)
            self.assertEquals(config, expected_tpl.get(image))

    @mock.patch.object(ConfigStore, 'client_read', side_effect=client_read)
    def test_get_check_tpl(self, mock_client_read):
        """Test get_check_tpl"""
        valid_config = ['image_0', 'image_1', 'image_2']
        invalid_config = ['bad_image_0', 'bad_image_1']
        config_store = ConfigStore(self.auto_conf_agentConfig)
        for image in valid_config:
            tpl = self.mock_templates.get(image)[0]
            self.assertEquals(tpl[0], config_store.get_check_tpl(image)[0])
            self.assertEquals(str(tpl[1]), config_store.get_check_tpl(image)[1])
            self.assertEquals(str(tpl[2]), config_store.get_check_tpl(image)[2])
        for image in invalid_config:
            self.assertEquals(None, config_store.get_check_tpl(image))
