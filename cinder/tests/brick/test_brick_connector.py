# vim: tabstop=4 shiftwidth=4 softtabstop=4

# (c) Copyright 2013 Hewlett-Packard Development Company, L.P.
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

import os.path
import string
import time

import mox

from cinder.brick import exception
from cinder.brick.initiator import connector
from cinder.brick.initiator import host_driver
from cinder.brick.initiator import linuxfc
from cinder.brick.initiator import linuxscsi
from cinder.openstack.common import log as logging
from cinder.openstack.common import loopingcall
from cinder.openstack.common import processutils as putils
from cinder import test

LOG = logging.getLogger(__name__)


class ConnectorTestCase(test.TestCase):

    def setUp(self):
        super(ConnectorTestCase, self).setUp()
        self.cmds = []
        self.stubs.Set(os.path, 'exists', lambda x: True)

    def fake_execute(self, *cmd, **kwargs):
        self.cmds.append(string.join(cmd))
        return "", None

    def test_connect_volume(self):
        self.connector = connector.InitiatorConnector(None)
        self.assertRaises(NotImplementedError,
                          self.connector.connect_volume, None)

    def test_disconnect_volume(self):
        self.connector = connector.InitiatorConnector(None)
        self.assertRaises(NotImplementedError,
                          self.connector.disconnect_volume, None, None)

    def test_factory(self):
        obj = connector.InitiatorConnector.factory('iscsi', None)
        self.assertTrue(obj.__class__.__name__,
                        "ISCSIConnector")

        obj = connector.InitiatorConnector.factory('fibre_channel', None)
        self.assertTrue(obj.__class__.__name__,
                        "FibreChannelConnector")

        obj = connector.InitiatorConnector.factory('aoe', None)
        self.assertTrue(obj.__class__.__name__,
                        "AoEConnector")

        self.assertRaises(ValueError,
                          connector.InitiatorConnector.factory,
                          "bogus", None)

    def test_check_valid_device_with_wrong_path(self):
        self.connector = connector.InitiatorConnector(None)
        self.stubs.Set(self.connector,
                       '_execute', lambda *args, **kwargs: ("", None))
        self.assertFalse(self.connector.check_valid_device('/d0v'))

    def test_check_valid_device(self):
        self.connector = connector.InitiatorConnector(None)
        self.stubs.Set(self.connector,
                       '_execute', lambda *args, **kwargs: ("", ""))
        self.assertTrue(self.connector.check_valid_device('/dev'))

    def test_check_valid_device_with_cmd_error(self):
        def raise_except(*args, **kwargs):
            raise putils.ProcessExecutionError
        self.connector = connector.InitiatorConnector(None)
        self.stubs.Set(self.connector,
                       '_execute', raise_except)
        self.assertFalse(self.connector.check_valid_device('/dev'))


class HostDriverTestCase(test.TestCase):

    def setUp(self):
        super(HostDriverTestCase, self).setUp()
        self.devlist = ['device1', 'device2']
        self.stubs.Set(os, 'listdir', lambda x: self.devlist)

    def test_host_driver(self):
        expected = ['/dev/disk/by-path/' + dev for dev in self.devlist]
        driver = host_driver.HostDriver()
        actual = driver.get_all_block_devices()
        self.assertEquals(expected, actual)


class ISCSIConnectorTestCase(ConnectorTestCase):

    def setUp(self):
        super(ISCSIConnectorTestCase, self).setUp()
        self.connector = connector.ISCSIConnector(
            None, execute=self.fake_execute, use_multipath=False)
        self.stubs.Set(self.connector._linuxscsi,
                       'get_name_from_path', lambda x: "/dev/sdb")

    def tearDown(self):
        super(ISCSIConnectorTestCase, self).tearDown()

    def iscsi_connection(self, volume, location, iqn):
        return {
            'driver_volume_type': 'iscsi',
            'data': {
                'volume_id': volume['id'],
                'target_portal': location,
                'target_iqn': iqn,
                'target_lun': 1,
            }
        }

    def test_get_initiator(self):
        def initiator_no_file(*args, **kwargs):
            raise putils.ProcessExecutionError('No file')

        def initiator_get_text(*arg, **kwargs):
            text = ('## DO NOT EDIT OR REMOVE THIS FILE!\n'
                    '## If you remove this file, the iSCSI daemon '
                    'will not start.\n'
                    '## If you change the InitiatorName, existing '
                    'access control lists\n'
                    '## may reject this initiator.  The InitiatorName must '
                    'be unique\n'
                    '## for each iSCSI initiator.  Do NOT duplicate iSCSI '
                    'InitiatorNames.\n'
                    'InitiatorName=iqn.1234-56.foo.bar:01:23456789abc')
            return text, None

        self.stubs.Set(self.connector, '_execute', initiator_no_file)
        initiator = self.connector.get_initiator()
        self.assertEquals(initiator, None)
        self.stubs.Set(self.connector, '_execute', initiator_get_text)
        initiator = self.connector.get_initiator()
        self.assertEquals(initiator, 'iqn.1234-56.foo.bar:01:23456789abc')

    @test.testtools.skipUnless(os.path.exists('/dev/disk/by-path'),
                               'Test requires /dev/disk/by-path')
    def test_connect_volume(self):
        self.stubs.Set(os.path, 'exists', lambda x: True)
        location = '10.0.2.15:3260'
        name = 'volume-00000001'
        iqn = 'iqn.2010-10.org.openstack:%s' % name
        vol = {'id': 1, 'name': name}
        connection_info = self.iscsi_connection(vol, location, iqn)
        device = self.connector.connect_volume(connection_info['data'])
        dev_str = '/dev/disk/by-path/ip-%s-iscsi-%s-lun-1' % (location, iqn)
        self.assertEquals(device['type'], 'block')
        self.assertEquals(device['path'], dev_str)

        self.connector.disconnect_volume(connection_info['data'], device)
        expected_commands = [('iscsiadm -m node -T %s -p %s' %
                              (iqn, location)),
                             ('iscsiadm -m session'),
                             ('iscsiadm -m node -T %s -p %s --login' %
                              (iqn, location)),
                             ('iscsiadm -m node -T %s -p %s --op update'
                              ' -n node.startup -v automatic' % (iqn,
                              location)),
                             ('tee -a /sys/block/sdb/device/delete'),
                             ('iscsiadm -m node -T %s -p %s --op update'
                              ' -n node.startup -v manual' % (iqn, location)),
                             ('iscsiadm -m node -T %s -p %s --logout' %
                              (iqn, location)),
                             ('iscsiadm -m node -T %s -p %s --op delete' %
                              (iqn, location)), ]
        LOG.debug("self.cmds = %s" % self.cmds)
        LOG.debug("expected = %s" % expected_commands)

        self.assertEqual(expected_commands, self.cmds)

    def test_connect_volume_with_multipath(self):

        location = '10.0.2.15:3260'
        name = 'volume-00000001'
        iqn = 'iqn.2010-10.org.openstack:%s' % name
        vol = {'id': 1, 'name': name}
        connection_properties = self.iscsi_connection(vol, location, iqn)

        self.connector_with_multipath =\
            connector.ISCSIConnector(None, use_multipath=True)
        self.stubs.Set(self.connector_with_multipath,
                       '_run_iscsiadm_bare',
                       lambda *args, **kwargs: "%s %s" % (location, iqn))
        self.stubs.Set(self.connector_with_multipath,
                       '_get_target_portals_from_iscsiadm_output',
                       lambda x: [location])
        self.stubs.Set(self.connector_with_multipath,
                       '_connect_to_iscsi_portal',
                       lambda x: None)
        self.stubs.Set(self.connector_with_multipath,
                       '_rescan_iscsi',
                       lambda: None)
        self.stubs.Set(self.connector_with_multipath,
                       '_rescan_multipath',
                       lambda: None)
        self.stubs.Set(self.connector_with_multipath,
                       '_get_multipath_device_name',
                       lambda x: 'iqn.2010-10.org.openstack:%s' % name)
        self.stubs.Set(os.path, 'exists', lambda x: True)
        result = self.connector_with_multipath.connect_volume(
            connection_properties['data'])
        expected_result = {'path': 'iqn.2010-10.org.openstack:volume-00000001',
                           'type': 'block'}
        self.assertEqual(result, expected_result)

    def test_connect_volume_with_not_found_device(self):
        self.stubs.Set(os.path, 'exists', lambda x: False)
        self.stubs.Set(time, 'sleep', lambda x: None)
        location = '10.0.2.15:3260'
        name = 'volume-00000001'
        iqn = 'iqn.2010-10.org.openstack:%s' % name
        vol = {'id': 1, 'name': name}
        connection_info = self.iscsi_connection(vol, location, iqn)
        self.assertRaises(exception.VolumeDeviceNotFound,
                          self.connector.connect_volume,
                          connection_info['data'])


class FibreChannelConnectorTestCase(ConnectorTestCase):
    def setUp(self):
        super(FibreChannelConnectorTestCase, self).setUp()
        self.connector = connector.FibreChannelConnector(
            None, execute=self.fake_execute, use_multipath=False)
        self.assertIsNotNone(self.connector)
        self.assertIsNotNone(self.connector._linuxfc)
        self.assertIsNotNone(self.connector._linuxscsi)

    def fake_get_fc_hbas(self):
        return [{'ClassDevice': 'host1',
                 'ClassDevicePath': '/sys/devices/pci0000:00/0000:00:03.0'
                                    '/0000:05:00.2/host1/fc_host/host1',
                 'dev_loss_tmo': '30',
                 'fabric_name': '0x1000000533f55566',
                 'issue_lip': '<store method only>',
                 'max_npiv_vports': '255',
                 'maxframe_size': '2048 bytes',
                 'node_name': '0x200010604b019419',
                 'npiv_vports_inuse': '0',
                 'port_id': '0x680409',
                 'port_name': '0x100010604b019419',
                 'port_state': 'Online',
                 'port_type': 'NPort (fabric via point-to-point)',
                 'speed': '10 Gbit',
                 'supported_classes': 'Class 3',
                 'supported_speeds': '10 Gbit',
                 'symbolic_name': 'Emulex 554M FV4.0.493.0 DV8.3.27',
                 'tgtid_bind_type': 'wwpn (World Wide Port Name)',
                 'uevent': None,
                 'vport_create': '<store method only>',
                 'vport_delete': '<store method only>'}]

    def fake_get_fc_hbas_info(self):
        hbas = self.fake_get_fc_hbas()
        info = [{'port_name': hbas[0]['port_name'].replace('0x', ''),
                 'node_name': hbas[0]['node_name'].replace('0x', ''),
                 'host_device': hbas[0]['ClassDevice'],
                 'device_path': hbas[0]['ClassDevicePath']}]
        return info

    def fibrechan_connection(self, volume, location, wwn):
        return {'driver_volume_type': 'fibrechan',
                'data': {
                    'volume_id': volume['id'],
                    'target_portal': location,
                    'target_wwn': wwn,
                    'target_lun': 1,
                }}

    def test_connect_volume(self):
        self.stubs.Set(self.connector._linuxfc, "get_fc_hbas",
                       self.fake_get_fc_hbas)
        self.stubs.Set(self.connector._linuxfc, "get_fc_hbas_info",
                       self.fake_get_fc_hbas_info)
        self.stubs.Set(os.path, 'exists', lambda x: True)
        self.stubs.Set(os.path, 'realpath', lambda x: '/dev/sdb')

        multipath_devname = '/dev/md-1'
        devices = {"device": multipath_devname,
                   "id": "1234567890",
                   "devices": [{'device': '/dev/sdb',
                                'address': '1:0:0:1',
                                'host': 1, 'channel': 0,
                                'id': 0, 'lun': 1}]}
        self.stubs.Set(self.connector._linuxscsi, 'find_multipath_device',
                       lambda x: devices)
        self.stubs.Set(self.connector._linuxscsi, 'remove_scsi_device',
                       lambda x: None)
        self.stubs.Set(self.connector._linuxscsi, 'get_device_info',
                       lambda x: devices['devices'][0])
        location = '10.0.2.15:3260'
        name = 'volume-00000001'
        vol = {'id': 1, 'name': name}
        # Should work for string, unicode, and list
        wwns = ['1234567890123456', unicode('1234567890123456'),
                ['1234567890123456', '1234567890123457']]
        for wwn in wwns:
            connection_info = self.fibrechan_connection(vol, location, wwn)
            dev_info = self.connector.connect_volume(connection_info['data'])
            exp_wwn = wwn[0] if isinstance(wwn, list) else wwn
            dev_str = ('/dev/disk/by-path/pci-0000:05:00.2-fc-0x%s-lun-1' %
                       exp_wwn)
            self.assertEquals(dev_info['type'], 'block')
            self.assertEquals(dev_info['path'], dev_str)

            self.connector.disconnect_volume(connection_info['data'], dev_info)
            expected_commands = []
            self.assertEqual(expected_commands, self.cmds)

        # Should not work for anything other than string, unicode, and list
        connection_info = self.fibrechan_connection(vol, location, 123)
        self.assertRaises(exception.NoFibreChannelHostsFound,
                          self.connector.connect_volume,
                          connection_info['data'])

        self.stubs.Set(self.connector._linuxfc, 'get_fc_hbas',
                       lambda: [])
        self.stubs.Set(self.connector._linuxfc, 'get_fc_hbas_info',
                       lambda: [])
        self.assertRaises(exception.NoFibreChannelHostsFound,
                          self.connector.connect_volume,
                          connection_info['data'])


class FakeFixedIntervalLoopingCall(object):
    def __init__(self, f=None, *args, **kw):
        self.args = args
        self.kw = kw
        self.f = f
        self._stop = False

    def stop(self):
        self._stop = True

    def wait(self):
        return self

    def start(self, interval, initial_delay=None):
        while not self._stop:
            try:
                self.f(*self.args, **self.kw)
            except loopingcall.LoopingCallDone:
                return self
            except Exception:
                LOG.exception(_('in fixed duration looping call'))
                raise


class AoEConnectorTestCase(ConnectorTestCase):
    """Test cases for AoE initiator class."""
    def setUp(self):
        super(AoEConnectorTestCase, self).setUp()
        self.mox = mox.Mox()
        self.connector = connector.AoEConnector('sudo')
        self.connection_properties = {'target_shelf': 'fake_shelf',
                                      'target_lun': 'fake_lun'}
        self.stubs.Set(loopingcall,
                       'FixedIntervalLoopingCall',
                       FakeFixedIntervalLoopingCall)

    def tearDown(self):
        self.mox.VerifyAll()
        self.mox.UnsetStubs()
        super(AoEConnectorTestCase, self).tearDown()

    def _mock_path_exists(self, aoe_path, mock_values=[]):
        self.mox.StubOutWithMock(os.path, 'exists')
        for value in mock_values:
            os.path.exists(aoe_path).AndReturn(value)

    def test_connect_volume(self):
        """Ensure that if path exist aoe-revaliadte was called."""
        aoe_device, aoe_path = self.connector._get_aoe_info(
            self.connection_properties)

        self._mock_path_exists(aoe_path, [True, True])

        self.mox.StubOutWithMock(self.connector, '_execute')
        self.connector._execute('aoe-revalidate',
                                aoe_device,
                                run_as_root=True,
                                root_helper='sudo',
                                check_exit_code=0).AndReturn(("", ""))
        self.mox.ReplayAll()

        self.connector.connect_volume(self.connection_properties)

    def test_connect_volume_without_path(self):
        """Ensure that if path doesn't exist aoe-discovery was called."""

        aoe_device, aoe_path = self.connector._get_aoe_info(
            self.connection_properties)
        expected_info = {
            'type': 'block',
            'device': aoe_device,
            'path': aoe_path,
        }

        self._mock_path_exists(aoe_path, [False, True])

        self.mox.StubOutWithMock(self.connector, '_execute')
        self.connector._execute('aoe-discover',
                                run_as_root=True,
                                root_helper='sudo',
                                check_exit_code=0).AndReturn(("", ""))
        self.mox.ReplayAll()

        volume_info = self.connector.connect_volume(
            self.connection_properties)

        self.assertDictMatch(volume_info, expected_info)

    def test_connect_volume_could_not_discover_path(self):
        aoe_device, aoe_path = self.connector._get_aoe_info(
            self.connection_properties)

        number_of_calls = 4
        self._mock_path_exists(aoe_path, [False] * (number_of_calls + 1))
        self.mox.StubOutWithMock(self.connector, '_execute')

        for i in xrange(number_of_calls):
            self.connector._execute('aoe-discover',
                                    run_as_root=True,
                                    root_helper='sudo',
                                    check_exit_code=0).AndReturn(("", ""))
        self.mox.ReplayAll()
        self.assertRaises(exception.VolumeDeviceNotFound,
                          self.connector.connect_volume,
                          self.connection_properties)

    def test_disconnect_volume(self):
        """Ensure that if path exist aoe-revaliadte was called."""
        aoe_device, aoe_path = self.connector._get_aoe_info(
            self.connection_properties)

        self._mock_path_exists(aoe_path, [True])

        self.mox.StubOutWithMock(self.connector, '_execute')
        self.connector._execute('aoe-flush',
                                aoe_device,
                                run_as_root=True,
                                root_helper='sudo',
                                check_exit_code=0).AndReturn(("", ""))
        self.mox.ReplayAll()

        self.connector.disconnect_volume(self.connection_properties, {})
