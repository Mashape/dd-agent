# std
import logging
import re
import requests
import simplejson as json

# project
from config import check_yaml
from utils.checkfiles import get_conf_path
from utils.service_discovery.config_stores import ConfigStore
from utils.dockerutil import get_client as get_docker_client
from utils.kubeutil import _get_default_router, DEFAULT_KUBELET_PORT

log = logging.getLogger(__name__)


KUBERNETES_CHECK_NAME = 'kubernetes'


class ServiceDiscoveryBackend(object):
    """Singleton for service discovery backends"""
    _instance = None
    PLACEHOLDER_REGEX = re.compile(r'%%.+?%%')

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            agentConfig = kwargs.get('agentConfig', {})
            if agentConfig.get('service_discovery_backend') == 'docker':
                cls._instance = object.__new__(SDDockerBackend, agentConfig)
            else:
                log.error("Service discovery backend not supported. This feature won't be enabled")
                return
        return cls._instance

    def __init__(self, agentConfig=None):
        self.agentConfig = agentConfig

    def get_configs(self):
        """Get the config for all docker containers running on the host."""
        raise NotImplementedError()

    def _render_template(self, init_config_tpl, instance_tpl, variables):
        """Replace placeholders in a template with the proper values.
           Return a list made of `init_config` and `instances`."""
        config = [init_config_tpl, instance_tpl]
        for tpl in config:
            for key in tpl:
                for var in self.PLACEHOLDER_REGEX.findall(str(tpl[key])):
                    if var.strip('%') in variables and variables[var.strip('%')]:
                        tpl[key] = tpl[key].replace(var, variables[var.strip('%')])
                    else:
                        log.warning('Failed to find interpolate variable {0} for the {1} parameter.'
                                    ' The check might not be configured properly.'.format(var, key))
                        tpl[key].replace(var, '')
        config[1] = config[1]
        return config

    @classmethod
    def _drop(cls):
        cls._instance = None


class SDDockerBackend(ServiceDiscoveryBackend):
    """Docker-based service discovery"""

    def __init__(self, agentConfig):
        self.docker_client = get_docker_client()

        try:
            self.config_store = ConfigStore(agentConfig=agentConfig)
        except Exception as e:
            log.error('Failed to instantiate the config store client. '
                      'Auto-config only will be used. %s' % str(e))
            agentConfig['sd_config_backend'] = None
            ConfigStore._drop()
            self.config_store = ConfigStore(agentConfig=agentConfig)

        self.VAR_MAPPING = {
            'host': self._get_host,
            'port': self._get_port,
            'tags': self._get_tags,
        }
        ServiceDiscoveryBackend.__init__(self, agentConfig)

    def _get_host(self, container_inspect):
        """Extract the host IP from a docker inspect object, or the kubelet API."""
        ip_addr = container_inspect.get('NetworkSettings', {}).get('IPAddress')
        if not ip_addr:
            log.debug("Didn't find the IP address for container %s (%s), using the kubernetes way." %
                      (container_inspect.get('Id', ''), container_inspect.get('Config', {}).get('Image', '')))
            # kubernetes case
            pod_list = self._get_pod_list()
            c_id = container_inspect.get('Id')

            for pod in pod_list:
                pod_ip = pod.get('status', {}).get('podIP')
                if pod_ip is None:
                    continue
                else:
                    c_statuses = pod.get('status', {}).get('containerStatuses', [])
                    for status in c_statuses:
                        # compare the container id with those of containers in the current pod
                        if c_id == status.get('containerID', '').split('//')[1]:
                            ip_addr = pod_ip

        return ip_addr

    def _get_port(self, container_inspect):
        """Extract the port from a docker inspect object."""
        try:
            port = container_inspect['NetworkSettings']['Ports'].keys()[0].split("/")[0]
        except (IndexError, KeyError, AttributeError):
            log.debug("Didn't find the port for container %s (%s), using the kubernetes way." %
                      (container_inspect.get('Id', ''), container_inspect.get('Config', {}).get('Image', '')))
            # kubernetes case
            ports = container_inspect['Config'].get('ExposedPorts', {})
            port = ports.keys()[0].split("/")[0] if ports else None
        return port

    def _get_tags(self, container_inspect):
        """Extract useful tags from docker or platform APIs."""
        tags = []
        tag_dict = {
            'replication_controller': None,
            'namespace': None,
            'pod_name': None,
            'node_name': None,
        }
        pod_metadata = self._get_kube_config(container_inspect.get('Id'), 'metadata')
        pod_spec = self._get_kube_config(container_inspect.get('Id'), 'spec')

        # get labels
        kube_labels = pod_metadata.get('labels', {})
        for tag, value in kube_labels.iteritems():
            tags.append('%s:%s' % (tag, value))

        # get replication controller
        created_by = json.loads(pod_metadata.get('annotations', {}).get('kubernetes.io/created-by', '{}'))
        if created_by.get('kind') == 'ReplicationController':
            tag_dict['replication_controller'] = created_by.get('name')

        tag_dict['namespace'] = pod_metadata.get('namespace')
        tag_dict['pod_name'] = pod_metadata.get('name')
        tag_dict['node_name'] = pod_spec.get('nodeName')

        for tag, value in tag_dict.iteritems():
            if value is not None:
                tags.append('%s:%s' % (tag, value))

        return tags

    def _get_kube_config(self, c_id, key):
        """Get a part of a pod config from the kubernetes API"""
        pods = self._get_pod_list()
        for pod in pods:
            c_statuses = pod.get('status', {}).get('containerStatuses', [])
            for status in c_statuses:
                if c_id == status.get('containerID', '').split('//')[1]:
                    return pod.get(key, {})

    def _get_pod_list(self):
        """Query the pod list from the kubernetes API and returns it as a list"""
        host_ip = _get_default_router()
        config_file_path = get_conf_path(KUBERNETES_CHECK_NAME)
        check_config = check_yaml(config_file_path)
        instances = check_config.get('instances', [{}])
        kube_port = instances[0].get('kubelet_port', DEFAULT_KUBELET_PORT)
        pod_list = requests.get('http://%s:%s/pods' % (host_ip, kube_port)).json()
        return pod_list.get('items', [])

    def get_configs(self):
        """Get the config for all docker containers running on the host."""
        containers = [(container.get('Image').split(':')[0].split('/')[-1], container.get('Id'), container.get('Labels')) for container in self.docker_client.containers()]
        configs = {}

        for image, cid, labels in containers:
            conf = self._get_check_config(cid, image)
            if conf is not None:
                check_name = conf[0]
                # build instances list if needed
                if configs.get(check_name) is None:
                    configs[check_name] = (conf[1], [conf[2]])
                else:
                    if configs[check_name][0] != conf[1]:
                        log.warning('different versions of `init_config` found for check {0}.'
                                    ' Keeping the first one found.'.format(check_name))
                    configs[check_name][1].append(conf[2])
        log.debug('check configs: %s' % configs)
        return configs

    def _get_check_config(self, c_id, image):
        """Retrieve a configuration template and fill it with data pulled from docker."""
        inspect = self.docker_client.inspect_container(c_id)
        template_config = self._get_template_config(image)
        if template_config is None:
            log.debug('Template config is None, container %s with image %s '
                      'will be left unconfigured.' % (c_id, image))
            return None

        check_name, init_config_tpl, instance_tpl, variables = template_config
        var_values = {}
        for v in variables:
            if v in self.VAR_MAPPING:
                try:
                    var_values[v] = self.VAR_MAPPING[v](inspect)
                except Exception as ex:
                    log.error("Could not find a value for the template variable %s: %s", (v, ex))
            else:
                log.error("No method was found to interpolate template variable %s." % v)
        init_config, instances = self._render_template(init_config_tpl or {}, instance_tpl or {}, var_values)
        return (check_name, init_config, instances)

    def _get_template_config(self, image_name):
        """Extract a template config from a K/V store and returns it as a dict object."""
        config_backend = self.agentConfig.get('sd_config_backend')
        if config_backend is None:
            auto_conf = True
            log.info('No supported configuration backend was provided, using auto-config only.')
        else:
            auto_conf = False

        tpl = self.config_store.get_check_tpl(image_name, auto_conf=auto_conf)

        if tpl is not None and len(tpl) == 3:
            check_name, init_config_tpl, instance_tpl = tpl
        else:
            log.debug('No template was found for image %s, leaving it alone.' % image_name)
            return None
        try:
            # build a list of all variables to replace in the template
            variables = self.PLACEHOLDER_REGEX.findall(str(init_config_tpl)) + \
                self.PLACEHOLDER_REGEX.findall(str(instance_tpl))
            variables = map(lambda x: x.strip('%'), variables)
            if not isinstance(init_config_tpl, dict):
                init_config_tpl = json.loads(init_config_tpl)
                if not isinstance(instance_tpl, dict):
                    instance_tpl = json.loads(instance_tpl)
        except json.JSONDecodeError:
            log.error('Failed to decode the JSON template fetched from {0}.'
                      'Auto-config for {1} failed.'.format(config_backend, image_name))
            return None
        return (check_name, init_config_tpl, instance_tpl, variables)
