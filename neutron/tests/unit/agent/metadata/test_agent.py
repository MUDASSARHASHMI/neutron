# Copyright 2012 New Dream Network, LLC (DreamHost)
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import mock
from neutron_lib import constants as n_const
import testtools
import webob

from oslo_config import cfg
from oslo_config import fixture as config_fixture
from oslo_utils import fileutils

from neutron.agent.linux import utils as agent_utils
from neutron.agent.metadata import agent
from neutron.agent.metadata import config
from neutron.agent import metadata_agent
from neutron.common import cache_utils as cache
from neutron.common import utils
from neutron.tests import base


class ConfFixture(config_fixture.Config):
    def setUp(self):
        super(ConfFixture, self).setUp()
        self.conf.register_opts(config.METADATA_PROXY_HANDLER_OPTS)
        self.config(auth_ca_cert=None,
                    nova_metadata_ip='9.9.9.9',
                    nova_metadata_port=8775,
                    metadata_proxy_shared_secret='secret',
                    nova_metadata_protocol='http',
                    nova_metadata_insecure=True,
                    nova_client_cert='nova_cert',
                    nova_client_priv_key='nova_priv_key')
        cache.register_oslo_configs(self.conf)
        self.config(cache_url='')


class CacheConfFixture(ConfFixture):
    def setUp(self):
        super(CacheConfFixture, self).setUp()
        self.config(cache_url='memory://?default_ttl=5')


class NewCacheConfFixture(ConfFixture):
    def setUp(self):
        super(NewCacheConfFixture, self).setUp()
        self.config(
            group='cache',
            enabled=True,
            backend='oslo_cache.dict',
            expiration_time=5)


class TestMetadataProxyHandlerBase(base.BaseTestCase):
    fake_conf = cfg.CONF
    fake_conf_fixture = ConfFixture(fake_conf)

    def setUp(self):
        super(TestMetadataProxyHandlerBase, self).setUp()
        self.useFixture(self.fake_conf_fixture)
        self.log_p = mock.patch.object(agent, 'LOG')
        self.log = self.log_p.start()
        self.handler = agent.MetadataProxyHandler(self.fake_conf)
        self.handler.plugin_rpc = mock.Mock()
        self.handler.context = mock.Mock()


class TestMetadataProxyHandlerRpc(TestMetadataProxyHandlerBase):
    def test_get_port_filters(self):
        router_id = 'test_router_id'
        ip = '1.2.3.4'
        networks = ('net_id1', 'net_id2')
        expected = {'device_id': [router_id],
                    'device_owner': n_const.ROUTER_INTERFACE_OWNERS,
                    'network_id': networks,
                    'fixed_ips': {'ip_address': [ip]}}
        actual = self.handler._get_port_filters(router_id, ip, networks)
        self.assertEqual(expected, actual)

    def test_get_router_networks(self):
        router_id = 'router-id'
        expected = ('network_id1', 'network_id2')
        ports = [{'network_id': 'network_id1', 'something': 42},
                 {'network_id': 'network_id2', 'something_else': 32}]
        self.handler.plugin_rpc.get_ports.return_value = ports
        networks = self.handler._get_router_networks(router_id)
        self.assertEqual(expected, networks)

    def test_get_ports_for_remote_address(self):
        ip = '1.1.1.1'
        networks = ('network_id1', 'network_id2')
        expected = [{'port_id': 'port_id1'},
                    {'port_id': 'port_id2'}]
        self.handler.plugin_rpc.get_ports.return_value = expected
        ports = self.handler._get_ports_for_remote_address(ip, networks)
        self.assertEqual(expected, ports)


class _TestMetadataProxyHandlerCacheMixin(object):

    def test_call(self):
        req = mock.Mock()
        with mock.patch.object(self.handler,
                               '_get_instance_and_tenant_id') as get_ids:
            get_ids.return_value = ('instance_id', 'tenant_id')
            with mock.patch.object(self.handler, '_proxy_request') as proxy:
                proxy.return_value = 'value'

                retval = self.handler(req)
                self.assertEqual(retval, 'value')

    def test_call_no_instance_match(self):
        req = mock.Mock()
        with mock.patch.object(self.handler,
                               '_get_instance_and_tenant_id') as get_ids:
            get_ids.return_value = None, None
            retval = self.handler(req)
            self.assertIsInstance(retval, webob.exc.HTTPNotFound)

    def test_call_internal_server_error(self):
        req = mock.Mock()
        with mock.patch.object(self.handler,
                               '_get_instance_and_tenant_id') as get_ids:
            get_ids.side_effect = Exception
            retval = self.handler(req)
            self.assertIsInstance(retval, webob.exc.HTTPInternalServerError)
            self.assertEqual(len(self.log.mock_calls), 2)

    def test_get_router_networks(self):
        router_id = 'router-id'
        expected = ('network_id1', 'network_id2')
        ports = [{'network_id': 'network_id1', 'something': 42},
                 {'network_id': 'network_id2', 'something_else': 32}]
        mock_get_ports = self.handler.plugin_rpc.get_ports
        mock_get_ports.return_value = ports
        networks = self.handler._get_router_networks(router_id)
        mock_get_ports.assert_called_once_with(
            mock.ANY,
            {'device_id': [router_id],
             'device_owner': n_const.ROUTER_INTERFACE_OWNERS})
        self.assertEqual(expected, networks)

    def _test_get_router_networks_twice_helper(self):
        router_id = 'router-id'
        ports = [{'network_id': 'network_id1', 'something': 42}]
        expected_networks = ('network_id1',)
        with mock.patch(
            'oslo_utils.timeutils.utcnow_ts', return_value=0):
            mock_get_ports = self.handler.plugin_rpc.get_ports
            mock_get_ports.return_value = ports
            networks = self.handler._get_router_networks(router_id)
            mock_get_ports.assert_called_once_with(
                mock.ANY,
                {'device_id': [router_id],
                 'device_owner': n_const.ROUTER_INTERFACE_OWNERS})
            self.assertEqual(expected_networks, networks)
            networks = self.handler._get_router_networks(router_id)

    def test_get_router_networks_twice(self):
        self._test_get_router_networks_twice_helper()
        self.assertEqual(
            1, self.handler.plugin_rpc.get_ports.call_count)

    def _get_ports_for_remote_address_cache_hit_helper(self):
        remote_address = 'remote_address'
        networks = ('net1', 'net2')
        mock_get_ports = self.handler.plugin_rpc.get_ports
        mock_get_ports.return_value = [{'network_id': 'net1', 'something': 42}]
        self.handler._get_ports_for_remote_address(remote_address, networks)
        mock_get_ports.assert_called_once_with(
            mock.ANY,
            {'network_id': networks,
             'fixed_ips': {'ip_address': [remote_address]}}
        )
        self.assertEqual(1, mock_get_ports.call_count)
        self.handler._get_ports_for_remote_address(remote_address,
                                                   networks)

    def test_get_ports_for_remote_address_cache_hit(self):
        self._get_ports_for_remote_address_cache_hit_helper()
        self.assertEqual(
            1, self.handler.plugin_rpc.get_ports.call_count)

    def test_get_ports_network_id(self):
        network_id = 'network-id'
        router_id = 'router-id'
        remote_address = 'remote-address'
        expected = ['port1']
        networks = (network_id,)
        with mock.patch.object(self.handler,
                               '_get_ports_for_remote_address'
                               ) as mock_get_ip_addr,\
                mock.patch.object(self.handler,
                                  '_get_router_networks'
                                  ) as mock_get_router_networks:
            mock_get_ip_addr.return_value = expected
            ports = self.handler._get_ports(remote_address, network_id,
                                            router_id)
            mock_get_ip_addr.assert_called_once_with(remote_address,
                                                     networks)
            self.assertFalse(mock_get_router_networks.called)
        self.assertEqual(expected, ports)

    def test_get_ports_router_id(self):
        router_id = 'router-id'
        remote_address = 'remote-address'
        expected = ['port1']
        networks = ('network1', 'network2')
        with mock.patch.object(self.handler,
                               '_get_ports_for_remote_address',
                               return_value=expected
                               ) as mock_get_ip_addr,\
                mock.patch.object(self.handler,
                                  '_get_router_networks',
                                  return_value=networks
                                  ) as mock_get_router_networks:
            ports = self.handler._get_ports(remote_address,
                                            router_id=router_id)
            mock_get_router_networks.assert_called_once_with(router_id)
            mock_get_ip_addr.assert_called_once_with(remote_address, networks)
            self.assertEqual(expected, ports)

    def test_get_ports_no_id(self):
        self.assertRaises(TypeError, self.handler._get_ports, 'remote_address')

    def _get_instance_and_tenant_id_helper(self, headers, list_ports_retval,
                                           networks=None, router_id=None):
        remote_address = '192.168.1.1'
        headers['X-Forwarded-For'] = remote_address
        req = mock.Mock(headers=headers)

        def mock_get_ports(*args, **kwargs):
            return list_ports_retval.pop(0)

        self.handler.plugin_rpc.get_ports.side_effect = mock_get_ports
        instance_id, tenant_id = self.handler._get_instance_and_tenant_id(req)

        expected = []

        if router_id:
            expected.append(
                mock.call(
                    mock.ANY,
                    {'device_id': [router_id],
                     'device_owner': n_const.ROUTER_INTERFACE_OWNERS}
                )
            )

        expected.append(
            mock.call(
                mock.ANY,
                {'network_id': networks,
                 'fixed_ips': {'ip_address': ['192.168.1.1']}}
            )
        )

        self.handler.plugin_rpc.get_ports.assert_has_calls(expected)

        return (instance_id, tenant_id)

    def test_get_instance_id_router_id(self):
        router_id = 'the_id'
        headers = {
            'X-Neutron-Router-ID': router_id
        }

        networks = ('net1', 'net2')
        ports = [
            [{'network_id': 'net1'}, {'network_id': 'net2'}],
            [{'device_id': 'device_id', 'tenant_id': 'tenant_id',
              'network_id': 'net1'}]
        ]

        self.assertEqual(
            self._get_instance_and_tenant_id_helper(headers, ports,
                                                    networks=networks,
                                                    router_id=router_id),
            ('device_id', 'tenant_id')
        )

    def test_get_instance_id_router_id_no_match(self):
        router_id = 'the_id'
        headers = {
            'X-Neutron-Router-ID': router_id
        }

        networks = ('net1', 'net2')
        ports = [
            [{'network_id': 'net1'}, {'network_id': 'net2'}],
            []
        ]
        self.assertEqual(
            self._get_instance_and_tenant_id_helper(headers, ports,
                                                    networks=networks,
                                                    router_id=router_id),
            (None, None)
        )

    def test_get_instance_id_network_id(self):
        network_id = 'the_id'
        headers = {
            'X-Neutron-Network-ID': network_id
        }

        ports = [
            [{'device_id': 'device_id',
              'tenant_id': 'tenant_id',
              'network_id': 'the_id'}]
        ]

        self.assertEqual(
            self._get_instance_and_tenant_id_helper(headers, ports,
                                                    networks=('the_id',)),
            ('device_id', 'tenant_id')
        )

    def test_get_instance_id_network_id_no_match(self):
        network_id = 'the_id'
        headers = {
            'X-Neutron-Network-ID': network_id
        }

        ports = [[]]

        self.assertEqual(
            self._get_instance_and_tenant_id_helper(headers, ports,
                                                    networks=('the_id',)),
            (None, None)
        )

    def _proxy_request_test_helper(self, response_code=200, method='GET'):
        hdrs = {'X-Forwarded-For': '8.8.8.8'}
        body = 'body'

        req = mock.Mock(path_info='/the_path', query_string='', headers=hdrs,
                        method=method, body=body)
        resp = mock.MagicMock(status=response_code)
        req.response = resp
        with mock.patch.object(self.handler, '_sign_instance_id') as sign:
            sign.return_value = 'signed'
            with mock.patch('httplib2.Http') as mock_http:
                resp.__getitem__.return_value = "text/plain"
                mock_http.return_value.request.return_value = (resp, 'content')

                retval = self.handler._proxy_request('the_id', 'tenant_id',
                                                     req)
                mock_http.assert_called_once_with(
                    ca_certs=None, disable_ssl_certificate_validation=True)
                mock_http.assert_has_calls([
                    mock.call().add_certificate(
                        self.fake_conf.nova_client_priv_key,
                        self.fake_conf.nova_client_cert,
                        "%s:%s" % (self.fake_conf.nova_metadata_ip,
                                   self.fake_conf.nova_metadata_port)
                    ),
                    mock.call().request(
                        'http://9.9.9.9:8775/the_path',
                        method=method,
                        headers={
                            'X-Forwarded-For': '8.8.8.8',
                            'X-Instance-ID-Signature': 'signed',
                            'X-Instance-ID': 'the_id',
                            'X-Tenant-ID': 'tenant_id'
                        },
                        body=body
                    )]
                )

                return retval

    def test_proxy_request_post(self):
        response = self._proxy_request_test_helper(method='POST')
        self.assertEqual(response.content_type, "text/plain")
        self.assertEqual(response.body, 'content')

    def test_proxy_request_200(self):
        response = self._proxy_request_test_helper(200)
        self.assertEqual(response.content_type, "text/plain")
        self.assertEqual(response.body, 'content')

    def test_proxy_request_400(self):
        self.assertIsInstance(self._proxy_request_test_helper(400),
                              webob.exc.HTTPBadRequest)

    def test_proxy_request_403(self):
        self.assertIsInstance(self._proxy_request_test_helper(403),
                              webob.exc.HTTPForbidden)

    def test_proxy_request_404(self):
        self.assertIsInstance(self._proxy_request_test_helper(404),
                              webob.exc.HTTPNotFound)

    def test_proxy_request_409(self):
        self.assertIsInstance(self._proxy_request_test_helper(409),
                              webob.exc.HTTPConflict)

    def test_proxy_request_500(self):
        self.assertIsInstance(self._proxy_request_test_helper(500),
                              webob.exc.HTTPInternalServerError)

    def test_proxy_request_other_code(self):
        with testtools.ExpectedException(Exception):
            self._proxy_request_test_helper(302)

    def test_sign_instance_id(self):
        self.assertEqual(
            self.handler._sign_instance_id('foo'),
            '773ba44693c7553d6ee20f61ea5d2757a9a4f4a44d2841ae4e95b52e4cd62db4'
        )


class TestMetadataProxyHandlerCache(TestMetadataProxyHandlerBase,
                                    _TestMetadataProxyHandlerCacheMixin):
    fake_conf = cfg.CONF
    fake_conf_fixture = CacheConfFixture(fake_conf)


class TestMetadataProxyHandlerNewCache(TestMetadataProxyHandlerBase,
                                       _TestMetadataProxyHandlerCacheMixin):
    fake_conf = cfg.CONF
    fake_conf_fixture = NewCacheConfFixture(fake_conf)


class TestMetadataProxyHandlerNoCache(TestMetadataProxyHandlerCache):
    fake_conf = cfg.CONF
    fake_conf_fixture = ConfFixture(fake_conf)

    def test_get_router_networks_twice(self):
        self._test_get_router_networks_twice_helper()
        self.assertEqual(
            2, self.handler.plugin_rpc.get_ports.call_count)

    def test_get_ports_for_remote_address_cache_hit(self):
        self._get_ports_for_remote_address_cache_hit_helper()
        self.assertEqual(
            2, self.handler.plugin_rpc.get_ports.call_count)


class TestUnixDomainMetadataProxy(base.BaseTestCase):
    def setUp(self):
        super(TestUnixDomainMetadataProxy, self).setUp()
        self.cfg_p = mock.patch.object(agent, 'cfg')
        self.cfg = self.cfg_p.start()
        looping_call_p = mock.patch(
            'oslo_service.loopingcall.FixedIntervalLoopingCall')
        self.looping_mock = looping_call_p.start()
        self.cfg.CONF.metadata_proxy_socket = '/the/path'
        self.cfg.CONF.metadata_workers = 0
        self.cfg.CONF.metadata_backlog = 128
        self.cfg.CONF.metadata_proxy_socket_mode = config.USER_MODE

    @mock.patch.object(fileutils, 'ensure_tree')
    def test_init_doesnot_exists(self, ensure_dir):
        agent.UnixDomainMetadataProxy(mock.Mock())
        ensure_dir.assert_called_once_with('/the', mode=0o755)

    def test_init_exists(self):
        with mock.patch('os.path.isdir') as isdir:
            with mock.patch('os.unlink') as unlink:
                isdir.return_value = True
                agent.UnixDomainMetadataProxy(mock.Mock())
                unlink.assert_called_once_with('/the/path')

    def test_init_exists_unlink_no_file(self):
        with mock.patch('os.path.isdir') as isdir:
            with mock.patch('os.unlink') as unlink:
                with mock.patch('os.path.exists') as exists:
                    isdir.return_value = True
                    exists.return_value = False
                    unlink.side_effect = OSError

                    agent.UnixDomainMetadataProxy(mock.Mock())
                    unlink.assert_called_once_with('/the/path')

    def test_init_exists_unlink_fails_file_still_exists(self):
        with mock.patch('os.path.isdir') as isdir:
            with mock.patch('os.unlink') as unlink:
                with mock.patch('os.path.exists') as exists:
                    isdir.return_value = True
                    exists.return_value = True
                    unlink.side_effect = OSError

                    with testtools.ExpectedException(OSError):
                        agent.UnixDomainMetadataProxy(mock.Mock())
                    unlink.assert_called_once_with('/the/path')

    @mock.patch.object(agent, 'MetadataProxyHandler')
    @mock.patch.object(agent_utils, 'UnixDomainWSGIServer')
    @mock.patch.object(fileutils, 'ensure_tree')
    def test_run(self, ensure_dir, server, handler):
        p = agent.UnixDomainMetadataProxy(self.cfg.CONF)
        p.run()

        ensure_dir.assert_called_once_with('/the', mode=0o755)
        server.assert_has_calls([
            mock.call('neutron-metadata-agent'),
            mock.call().start(handler.return_value,
                              '/the/path', workers=0,
                              backlog=128, mode=0o644),
            mock.call().wait()]
        )
        self.looping_mock.assert_called_once_with(p._report_state)
        self.looping_mock.return_value.start.assert_called_once_with(
            interval=mock.ANY)

    def test_main(self):
        with mock.patch.object(agent, 'UnixDomainMetadataProxy') as proxy:
            with mock.patch.object(metadata_agent, 'config') as config:
                with mock.patch.object(metadata_agent, 'cfg') as cfg:
                    with mock.patch.object(utils, 'cfg'):
                        metadata_agent.main()

                        self.assertTrue(config.setup_logging.called)
                        proxy.assert_has_calls([
                            mock.call(cfg.CONF),
                            mock.call().run()]
                        )

    def test_report_state(self):
        with mock.patch('neutron.agent.rpc.PluginReportStateAPI') as state_api:
            with mock.patch('os.makedirs'):
                proxy = agent.UnixDomainMetadataProxy(self.cfg.CONF)
                proxy._init_state_reporting()
                self.assertTrue(proxy.agent_state['start_flag'])
                proxy._report_state()
                self.assertNotIn('start_flag', proxy.agent_state)
                state_api_inst = state_api.return_value
                state_api_inst.report_state.assert_called_once_with(
                    proxy.context, proxy.agent_state, use_call=True)
