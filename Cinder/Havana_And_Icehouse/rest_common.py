# Copyright (c) 2013 - 2014 Huawei Technologies Co., Ltd.
# All Rights Reserved.
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
"""Common class for Huawei 18000 storage drivers."""

import base64
import cookielib
import json
import six
import time
import urllib2
import uuid
from xml.etree import ElementTree as ET

from cinder import context
from cinder import exception
from cinder.openstack.common import excutils
from cinder.openstack.common import log as logging
from cinder import units
from cinder import utils
from cinder.volume import qos_specs
from cinder.volume import volume_types


LOG = logging.getLogger(__name__)

STATUS_HEALTH = '1'
STATUS_RUNNING = '10'
ERROR_CONNECT_TO_SERVER = -403
ERROR_UNAUTHORIZED_TO_SERVER = -401

HOSTGROUP_PREFIX = 'OpenStack_HostGroup_'
LUNGROUP_PREFIX = 'OpenStack_LunGroup_'
MAPPING_VIEW_PREFIX = 'OpenStack_Mapping_View_'
QOS_NAME_PREFIX = 'OpenStack_'
huawei_valid_keys = ['maxIOPS', 'minIOPS', 'minBandWidth',
                     'maxBandWidth', 'latency', 'IOType']


class RestCommon():
    """Common class for Huawei OceanStor 18000 storage system."""

    def __init__(self, configuration):
        self.configuration = configuration
        self.cookie = cookielib.CookieJar()
        self.url = None
        self.productversion = None
        self.headers = {"Connection": "keep-alive",
                        "Content-Type": "application/json"}

    def do_call(self, url=False, data=None, method=None):
        """Send requests to 18000 server.
        Send HTTPS call, get response in JSON.
        Convert response into Python Object and return it.
        """

        opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(self.cookie))
        urllib2.install_opener(opener)
        res_json = None

        try:
            urllib2.socket.setdefaulttimeout(720)
            req = urllib2.Request(url, data, self.headers)
            if method:
                req.get_method = lambda: method
            res = urllib2.urlopen(req).read().decode("utf-8")

            if "xx/sessions" not in url:
                LOG.info(_('\n\n\n\nRequest URL: %(url)s\n\n'
                           'Call Method: %(method)s\n\n'
                           'Request Data: %(data)s\n\n'
                           'Response Data:%(res)s\n\n'), {'url': url,
                                                          'method': method,
                                                          'data': data,
                                                          'res': res})

        except Exception as err:
            LOG.error(_('\nBad response from server: %(url)s.'
                        ' Error: %(err)s'), {'url': url, 'err': err})
            json_msg = '{"error":{"code":%s,' \
                       '"description":"Connet server error"}}' \
                       % ERROR_CONNECT_TO_SERVER
            res_json = json.loads(json_msg)
            return res_json

        try:
            res_json = json.loads(res)
        except Exception as err:
            err_msg = (_('JSON transfer error: %s.') % err)
            LOG.error(err_msg)
            raise err

        return res_json

    def login(self):
        """Log in 18000 array."""

        login_info = self._get_login_info()
        urlstr = login_info['RestURL']
        url_list = urlstr.split(";")
        for item_url in url_list:
            url = item_url + "xx/sessions"
            data = json.dumps({"username": login_info['UserName'],
                               "password": login_info['UserPassword'],
                               "scope": "0"})
            result = self.do_call(url, data)

            if (result['error']['code'] != 0) or ("data" not in result):
                msg = (_("Login error, reason is: %s.") % result)
                LOG.error(msg)
                continue

            LOG.debug('Login success: %(url)s' % {'url': item_url})
            deviceid = result['data']['deviceid']
            self.url = item_url + deviceid
            self.headers['iBaseToken'] = result['data']['iBaseToken']
            break

        if deviceid is None:
            msg = _("Login Error: Can't connect server.")
            LOG.error(msg)
            raise exception.CinderException(msg)

        return deviceid

    def call(self, url, data=None, method=None):
        """Send requests to server.

        if fail, try another RestURL
        """
        deviceid = None
        old_url = self.url
        result = self.do_call(url, data, method)
        error_code = result['error']['code']
        if error_code == ERROR_CONNECT_TO_SERVER \
           or error_code == ERROR_UNAUTHORIZED_TO_SERVER:
            err_msg = (_("Can't open the recent url, relogin."))
            LOG.error(err_msg)
            deviceid = self.login()

        if deviceid is not None:
            LOG.debug('Replace URL: \n'
                      'Old URL: %(old_url)s\n'
                      'New URL: %(new_url)s\n',
                      {'old_url': old_url,
                       'new_url': self.url})
            url = url.replace(old_url, self.url)
            result = self.do_call(url, data, method)

        return result

    def _init_lun_parameters(self, name, parameters):
        """Init basic LUN parameters."""
        lunparam = {"TYPE": "11",
                    "NAME": name,
                    "PARENTTYPE": "216",
                    "PARENTID": parameters['pool_id'],
                    "DESCRIPTION": parameters['volume_description'],
                    "ALLOCTYPE": parameters['LUNType'],
                    "CAPACITY": parameters['volume_size'],
                    "WRITEPOLICY": parameters['WriteType'],
                    "MIRRORPOLICY": parameters['MirrorSwitch'],
                    "PREFETCHPOLICY": parameters['PrefetchType'],
                    "PREFETCHVALUE": parameters['PrefetchValue']}

        return lunparam

    def _assert_rest_result(self, result, err_str):
        if result['error']['code'] != 0:
            msg = (_('%(err)s\nresult: %(res)s.') % {'err': err_str,
                                                     'res': result})
            LOG.error(msg)
            raise exception.CinderException(msg)

    def _assert_data_in_result(self, result, msg):
        if "data" not in result:
            err_msg = (_('%s "data" was not in result.') % msg)
            LOG.error(err_msg)
            raise exception.CinderException(err_msg)

    def _create_volume(self, lun_param):
        url = self.url + "/lun"
        data = json.dumps(lun_param)
        result = self.call(url, data)

        msg = 'Create volume error.'
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)

        return result['data']['ID']

    @utils.synchronized('huawei', external=True)
    def create_volume(self, volume):

        poolinfo = self._find_pool_info()
        volume_name = self._encode_name(volume['id'])
        volume_description = volume['name']
        volume_size = self._get_volume_size(volume)

        LOG.info(_('Create Volume: %(volume)s  Size: %(size)s')
                 % {'volume': volume_name,
                    'size': volume_size})

        params = self._get_lun_conf_params()
        params['pool_id'] = poolinfo['ID']
        params['volume_size'] = volume_size
        params['volume_description'] = volume_description

        # Prepare lun parameters.
        lun_param = self._init_lun_parameters(volume_name, params)

        # Create LUN in array
        lunid = self._create_volume(lun_param)

        type_id = volume.get('volume_type_id', None)
        policy_id = None

        if type_id is not None:
            volume_type = self._get_volume_type(type_id)
            qos = self._get_qos_by_volume_type(volume_type)
            if not qos:
                LOG.info(_('No QoS configuration found for volume: %s.')
                         % volume_name)
                return lunid

            try:
                # Check QoS priority. if high, change lun priority to high.
                if self._check_qos_high_priority(qos) is True:
                    self._change_lun_priority(lunid)

                # Create QoS policy and active.
                policy_id = self._create_qos_policy(qos, lunid)
                self._active_deactive_qos(policy_id, True)
            except Exception:
                with excutils.save_and_reraise_exception():
                    if policy_id is not None:
                        self._delete_qos_policy(policy_id)

                    self._delete_lun(lunid)

        return lunid

    def _get_volume_size(self, volume):
        """Calculate the volume size.

        We should divide the given volume size by 512 for the 18000 system
        calculates volume size with sectors, which is 512 bytes.
        """

        volume_size = units.GiB / 512  # 1G
        if int(volume['size']) != 0:
            volume_size = int(volume['size']) * units.GiB / 512

        return volume_size

    @utils.synchronized('huawei', external=True)
    def delete_volume(self, volume):
        """Delete a volume.

        Three steps: first, remove associate from lungroup.
        Second, remove associate from QoS policy. Third, remove the lun.
        """

        name = self._encode_name(volume['id'])
        lun_id = volume.get('provider_location', None)
        LOG.info(_('Delete Volume: %(name)s  array lun id: %(lun_id)s')
                 % {'name': name, 'lun_id': lun_id})

        if lun_id:
            if self._check_lun_exist(lun_id) is True:
                # Get qos_id by lun_id.
                qos_id = self._get_qosid_by_lunid(lun_id)

                if qos_id != "":
                    qos_info = self._get_qos_info(qos_id)
                    qos_status = qos_info['RUNNINGSTATUS']
                    # 2: Active status.
                    if qos_status == '2':
                        self._active_deactive_qos(qos_id, False)

                    self._delete_qos_policy(qos_id)
                self._delete_lun(lun_id)
        else:
            LOG.warn(_("Can't find lun or lungroup on the array."))

    def _check_lun_exist(self, lun_id):
        url = self.url + "/lun/" + lun_id
        data = json.dumps({"TYPE": "11",
                           "ID": lun_id})
        result = self.call(url, data, "GET")

        error_code = result['error']['code']
        if error_code != 0:
            return False

        return True

    def _delete_lun(self, lun_id):
        lun_group_ids = self._get_lungroupids_by_lunid(lun_id)
        if lun_group_ids and len(lun_group_ids) is 1:
            self._remove_lun_from_lungroup(lun_group_ids[0], lun_id)

        url = self.url + "/lun/" + lun_id
        data = json.dumps({"TYPE": "11",
                           "ID": lun_id})
        result = self.call(url, data, "DELETE")
        self._assert_rest_result(result, 'Delete lun error.')

    def _read_xml(self):
        """Open xml file and parse the content."""
        filename = self.configuration.cinder_huawei_conf_file
        try:
            tree = ET.parse(filename)
            root = tree.getroot()
        except Exception as err:
            LOG.error(_('_read_xml:%s') % err)
            raise err
        return root

    def _encode_name(self, name):
        uuid_str = name.replace("-", "")
        vol_uuid = uuid.UUID('urn:uuid:%s' % uuid_str)
        vol_encoded = base64.urlsafe_b64encode(vol_uuid.bytes)
        newuuid = vol_encoded.replace("=", "")
        return newuuid

    def _find_pool_info(self):
        root = self._read_xml()
        pool_name = root.findtext('LUN/StoragePool')
        if not pool_name:
            err_msg = (_("Invalid resource pool: %s.") % pool_name)
            LOG.error(err_msg)
            raise exception.InvalidInput(err_msg)

        url = self.url + "/storagepool"
        result = self.call(url, None)
        self._assert_rest_result(result, 'Query resource pool error.')

        poolinfo = {}
        if "data" in result:
            for item in result['data']:
                if pool_name.strip() == item['NAME']:
                    poolinfo['ID'] = item['ID']
                    poolinfo['CAPACITY'] = item['USERFREECAPACITY']
                    poolinfo['TOTALCAPACITY'] = item['USERTOTALCAPACITY']
                    break

        if not poolinfo:
            self.login_out()
            msg = (_('Get pool info error, pool name is: %s.') % pool_name)
            LOG.error(msg)
            raise exception.CinderException(msg)

        return poolinfo

    def _get_volume_by_name(self, name):
        url = self.url + "/lun?range=[0-65535]"
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, 'Get volume by name error!')

        volume_id = None
        if "data" in result:
            for item in result['data']:
                if name == item['NAME']:
                    volume_id = item['ID']
                    break
        return volume_id

    def _active_snapshot(self, snapshot_id):
        activeurl = self.url + "/snapshot/activate"
        data = json.dumps({"SNAPSHOTLIST": [snapshot_id]})
        result = self.call(activeurl, data)
        self._assert_rest_result(result, 'Active snapshot error.')

    def _create_snapshot(self, snapshot):
        snapshot_name = self._encode_name(snapshot['id'])
        snapshot_description = snapshot['id']
        volume_name = self._encode_name(snapshot['volume_id'])

        LOG.info(_('create_snapshot:snapshot name:%(snapshot)s, '
                   'volume name:%(volume)s.')
                 % {'snapshot': snapshot_name,
                    'volume': volume_name})

        volume = snapshot['volume']
        lun_id = volume.get('provider_location', None)
        if lun_id is None:
            lun_id = self._get_volume_by_name(volume_name)
        if lun_id is None:
            msg = (_("Can't find lun info on the array, "
                     "lun name is: %(name)s") % {'name': volume_name})
            LOG.error(msg)
            raise exception.CinderException(msg)

        url = self.url + "/snapshot"
        data = json.dumps({"TYPE": "27",
                           "NAME": snapshot_name,
                           "PARENTTYPE": "11",
                           "DESCRIPTION": snapshot_description,
                           "PARENTID": lun_id})
        result = self.call(url, data)

        msg = 'Create snapshot error.'
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)

        return result['data']['ID']

    @utils.synchronized('huawei', external=True)
    def create_snapshot(self, snapshot):
        snapshot_id = self._create_snapshot(snapshot)
        self._active_snapshot(snapshot_id)

        return snapshot_id

    def _check_snapshot_exist(self, snapshot_id):
        url = self.url + "/snapshot/" + snapshot_id
        data = json.dumps({"TYPE": "27",
                           "ID": snapshot_id})
        result = self.call(url, data, "GET")
        error_code = result['error']['code']
        if error_code != 0:
            return False

        return True

    def _stop_snapshot(self, snapshot_id):
        url = self.url + "/snapshot/stop"
        stopdata = json.dumps({"ID": snapshot_id})
        result = self.call(url, stopdata, "PUT")
        self._assert_rest_result(result, 'Stop snapshot error.')

    def _delete_snapshot(self, snapshotid):
        url = self.url + "/snapshot/%s" % snapshotid
        data = json.dumps({"TYPE": "27", "ID": snapshotid})
        result = self.call(url, data, "DELETE")
        self._assert_rest_result(result, 'Delete snapshot error.')

    @utils.synchronized('huawei', external=True)
    def delete_snapshot(self, snapshot):
        snapshot_name = self._encode_name(snapshot['id'])
        volume_name = self._encode_name(snapshot['volume_id'])

        LOG.info(_('_stop_snapshot:snapshot name:%(snapshot)s, '
                   'volume name:%(volume)s.')
                 % {'snapshot': snapshot_name,
                    'volume': volume_name})

        snapshot_id = snapshot.get('provider_location', None)
        if snapshot_id is None:
            snapshot_id = self._get_snapshotid_by_name(snapshot_name)

        if snapshot_id is not None:
            if self._check_snapshot_exist(snapshot_id) is True:
                self._stop_snapshot(snapshot_id)
                self._delete_snapshot(snapshot_id)
            else:
                LOG.warn(_("Can't find snapshot in array"))
        else:
            LOG.warn(_("Can't find snapshot in array"))

    def _get_snapshotid_by_name(self, name):
        url = self.url + "/snapshot?range=[0-32767]"
        data = json.dumps({"TYPE": "27"})
        result = self.call(url, data, "GET")
        self._assert_rest_result(result, 'Get snapshot id error.')

        snapshot_id = None
        if "data" in result:
            for item in result['data']:
                if name == item['NAME']:
                    snapshot_id = item['ID']
                    break

        return snapshot_id

    def _volume_ready(self, tgt_lun_id):
        url = self.url + "/lun/" + tgt_lun_id
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, 'Get volume by id failed!')

        if "data" in result:
            if (result['data']['HEALTHSTATUS'] == "1") and\
               (result['data']['RUNNINGSTATUS'] == "27"):
                return True
        return False

    def _copy_volume(self, volume, copy_name, src_lun, tgt_lun):

        # We sleep here to ensure the newly created volume can be read.
        while True:
            flag = self._volume_ready(tgt_lun)
            if flag:
                break
            else:
                time.sleep(2)

        luncopy_id = self._create_luncopy(copy_name,
                                          src_lun, tgt_lun)
        try:
            self._start_luncopy(luncopy_id)
            self._wait_for_luncopy(luncopy_id)

        except Exception:
            with excutils.save_and_reraise_exception():
                self._delete_luncopy(luncopy_id)
                self.delete_volume(volume)

        self._delete_luncopy(luncopy_id)

    def create_volume_from_snapshot(self, volume, snapshot):
        """Create a volume from a snapshot.

        We use LUNcopy to copy a new volume from snapshot.
        The time needed increases as volume size does.
        """

        snapshot_name = self._encode_name(snapshot['id'])

        snapshot_id = snapshot.get('provider_location', None)
        if snapshot_id is None:
            snapshot_id = self._get_snapshotid_by_name(snapshot_name)
            if snapshot_id is None:
                err_msg = (_(
                    'create_volume_from_snapshot: Snapshot %(name)s '
                    'does not exist.')
                    % {'name': snapshot_name})
                LOG.error(err_msg)
                raise exception.VolumeBackendAPIException(data=err_msg)

        tgt_lun_id = self.create_volume(volume)
        luncopy_name = self._encode_name(volume['id'])

        LOG.info(_('create_volume_from_snapshot: src_lun_id:%(src_lun_id)s, '
                   'tgt_lun_id: %(tgt_lun_id)s, copy_name: %(copy_name)s')
                 % {'src_lun_id': snapshot_id,
                    'tgt_lun_id': tgt_lun_id,
                    'copy_name': luncopy_name})

        self._copy_volume(volume, luncopy_name, snapshot_id, tgt_lun_id)

        return tgt_lun_id

    def create_cloned_volume(self, volume, src_vref):
        """Clone a new volume from an existing volume."""

        # Form the snapshot structure.
        snapshot = {'id': uuid.uuid4().__str__(),
                    'volume_id': src_vref['id'],
                    'volume': src_vref}

        # Create snapshot.
        self.create_snapshot(snapshot)

        try:
            # Create volume from snapshot
            volume_id = self.create_volume_from_snapshot(volume, snapshot)
        finally:
            try:
                # Delete snapshot
                self.delete_snapshot(snapshot)
            except exception.CinderException:
                LOG.warn(_('Failure delete the snapshot %(snapshot_id)s '
                         'of volume %(volume_id)s.')
                         % {'snapshot_id': snapshot['id'],
                            'volume_id': src_vref['id']})

        return volume_id

    def _create_luncopy(self, luncopyname, srclunid, tgtlunid):
        """Create a luncopy."""
        url = self.url + "/luncopy"
        data = json.dumps({"TYPE": 219,
                           "NAME": luncopyname,
                           "DESCRIPTION": luncopyname,
                           "COPYSPEED": 2,
                           "LUNCOPYTYPE": "1",
                           "SOURCELUN": ("INVALID;%s;INVALID;INVALID;INVALID"
                                         % srclunid),
                           "TARGETLUN": ("INVALID;%s;INVALID;INVALID;INVALID"
                                         % tgtlunid)})
        result = self.call(url, data)

        msg = 'Create lun copy error.'
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)

        return result['data']['ID']

    def _add_host_into_hostgroup(self, host_id):
        """Associate host to hostgroup.

        If hostgroup doesn't exist, create one.

        """
        host_group_name = HOSTGROUP_PREFIX + host_id
        hostgroup_id = self._create_hostgroup_with_check(host_group_name)
        is_associated = self._is_host_associate_to_hostgroup(hostgroup_id,
                                                             host_id)
        if not is_associated:
            self._associate_host_to_hostgroup(hostgroup_id, host_id)

        return hostgroup_id

    def _find_tgt_port_group(self, TgtPortGroup):
        """Find target portgroup id by target port group name."""
        url = self.url + "/portgroup?range=[0-8191]&TYPE=257"
        result = self.call(url, None, "GET")

        msg = 'Find portgroup error.'
        self._assert_rest_result(result, msg)
        msg = 'Can not find the portgroup on the array.'
        self._assert_data_in_result(result, msg)
        portgroup_id = None
        if "data" in result:
            for item in result['data']:
                if TgtPortGroup == item['NAME']:
                    portgroup_id = item['ID']
                    break
        return portgroup_id

    def _associate_portgroup_to_view(self, view_id, portgroup_id):
        url = self.url + "/MAPPINGVIEW/CREATE_ASSOCIATE"
        data = json.dumps({"ASSOCIATEOBJTYPE": "257",
                           "ASSOCIATEOBJID": portgroup_id,
                           "TYPE": "245",
                           "ID": view_id})
        result = self.call(url, data, "PUT")
        self._assert_rest_result(result, 'Associate portgroup to view error.')

    def _portgroup_associated(self, view_id, portgroup_id):
        url_subfix = ("/mappingview/associate?TYPE=245&"
                      "ASSOCIATEOBJTYPE=257&ASSOCIATEOBJID=%s" % portgroup_id)
        url = self.url + url_subfix
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, 'Check portgroup associated error.')

        if "data" in result:
            for item in result['data']:
                if view_id == item['ID']:
                    return True
        return False

    def _mapping_hostgroup_and_lungroup(self, lun_id, hostgroup_id,
                                        host_id, tgtportgroup_id=None):
        """Add hostgroup and lungroup to view."""
        lungroup_name = LUNGROUP_PREFIX + host_id
        mapping_view_name = MAPPING_VIEW_PREFIX + host_id
        lungroup_id = self._find_lungroup(lungroup_name)
        view_id = self._find_mapping_view(mapping_view_name)

        LOG.info(_('_mapping_hostgroup_and_lungroup,lun_group: %(lun_group)s,'
                   'view_id: %(view_id)s,lun_id: %(lun_id)s ')
                 % {'lun_group': six.text_type(lungroup_id),
                    'view_id': six.text_type(view_id),
                    'lun_id': six.text_type(lun_id)})

        try:
            # Create lungroup and add LUN into to lungroup.
            if lungroup_id is None:
                lungroup_id = self._create_lungroup(lungroup_name)
            is_associated = self._is_lun_associated_to_lungroup(lungroup_id,
                                                                lun_id)
            if not is_associated:
                self._associate_lun_to_lungroup(lungroup_id, lun_id)

            if view_id is None:
                view_id = self._add_mapping_view(mapping_view_name)
                self._associate_hostgroup_to_view(view_id, hostgroup_id)
                self._associate_lungroup_to_view(view_id, lungroup_id)
                if tgtportgroup_id:
                    self._associate_portgroup_to_view(view_id, tgtportgroup_id)

            else:
                if not self._hostgroup_associated(view_id, hostgroup_id):
                    self._associate_hostgroup_to_view(view_id, hostgroup_id)
                if not self._lungroup_associated(view_id, lungroup_id):
                    self._associate_lungroup_to_view(view_id, lungroup_id)
                if tgtportgroup_id:
                    if not self._portgroup_associated(view_id,
                                                      tgtportgroup_id):
                        self._associate_portgroup_to_view(view_id,
                                                          tgtportgroup_id)

        except Exception:
            with excutils.save_and_reraise_exception():
                err_msg = (_(
                    'Error occurred when adding hostgroup and lungroup to '
                    'view. Remove lun from lungroup now.'))
                LOG.error(err_msg)
                self._remove_lun_from_lungroup(lungroup_id, lun_id)

        return lun_id

    def _ensure_initiator_added(self, initiator_name, hostid):
        added = self._initiator_is_added_to_array(initiator_name)
        if not added:
            self._add_initiator_to_array(initiator_name)
            if self._is_initiator_associated_to_host(initiator_name) is False:
                self._associate_initiator_to_host(initiator_name, hostid)
        else:
            if self._is_initiator_associated_to_host(initiator_name) is False:
                self._associate_initiator_to_host(initiator_name, hostid)

    @utils.synchronized('huawei', external=True)
    def initialize_connection_iscsi(self, volume, connector):
        """Map a volume to a host and return target iSCSI information."""

        LOG.info(_('Enter initialize_connection_iscsi'))
        initiator_name = connector['initiator']
        volume_name = self._encode_name(volume['id'])

        LOG.info(_('initiator name:%(initiator_name)s, '
                   'volume name:%(volume)s.')
                 % {'initiator_name': initiator_name,
                    'volume': volume_name})

        lun_id = volume.get('provider_location', None)
        if lun_id is None:
            lun_id = self._get_volume_by_name(volume_name)
        if lun_id is None:
            msg = (_(
                "Can't find lun info on the array, lun name is: %(name)s.")
                % {'name': volume_name})
            LOG.error(msg)
            raise exception.CinderException(msg)

        (iscsi_iqn, target_ip, portgroup_id) = \
            self._get_iscsi_params(connector)
        LOG.info(_('initialize_connection_iscsi,iscsi_iqn: %(iscsi_iqn)s, '
                   'target_ip: %(target_ip)s, '
                   'TargetPortGroup: %(portgroup_id)s.'),
                 {'iscsi_iqn': iscsi_iqn,
                  'target_ip': target_ip,
                  'portgroup_id': portgroup_id})

        # Create host_group if not exist.
        host_name = connector['host']
        host_name_before_hash = ""
        if host_name and (len(host_name) > 31):
            host_name_before_hash = host_name
            host_name = six.text_type(hash(host_name))
        hostid = self._add_host_with_check(host_name, host_name_before_hash)

        # Add initiator to the host.
        self._ensure_initiator_added(initiator_name, hostid)
        hostgroup_id = self._add_host_into_hostgroup(hostid)

        # Mapping lungroup and hostgroup to view.
        lun_id = self._mapping_hostgroup_and_lungroup(lun_id,
                                                      hostgroup_id,
                                                      hostid,
                                                      portgroup_id)

        hostlunid = self._find_host_lun_id(hostid, lun_id)

        LOG.info(_("initialize_connection_iscsi,host lun id is %s")
                 % hostlunid)

        # Return iSCSI properties.
        properties = {}
        properties['target_discovered'] = False
        properties['target_portal'] = ('%s:%s' % (target_ip, '3260'))
        properties['target_iqn'] = iscsi_iqn
        properties['target_lun'] = int(hostlunid)
        properties['volume_id'] = volume['id']

        LOG.info(_("initialize_connection_iscsi success. Return data %s")
                 % properties)
        return {'driver_volume_type': 'iscsi', 'data': properties}

    @utils.synchronized('huawei', external=True)
    def initialize_connection_fc(self, volume, connector):
        wwns = connector['wwpns']
        host_name = connector['host']
        volume_name = self._encode_name(volume['id'])

        LOG.info(_('initialize_connection_fc,initiator:%(wwpns)s,'
                   'volume name:%(volume)s.')
                 % {'wwpns': wwns,
                    'volume': volume_name})

        lun_id = volume.get('provider_location', None)
        if lun_id is None:
            lun_id = self._get_volume_by_name(volume_name)
        if lun_id is None:
            msg = (_(
                "Can't find lun info on the array, lun name is: %(name)s.")
                % {'name': volume_name})
            LOG.error(msg)
            raise exception.CinderException(msg)

        host_name_before_hash = ""
        if host_name and (len(host_name) > 31):
            host_name_before_hash = host_name
            host_name = six.text_type(hash(host_name))

        # Create host_group if not exist.
        hostid = self._add_host_with_check(host_name, host_name_before_hash)

        # Add host into hostgroup.
        hostgroup_id = self._add_host_into_hostgroup(hostid)

        free_wwns = self._get_connected_free_wwns()
        LOG.info(_("initialize_connection_fc,the array free wwns %s")
                 % free_wwns)
        for wwn in wwns:
            if wwn in free_wwns:
                self._add_fc_port_to_host(hostid, wwn)

        self._mapping_hostgroup_and_lungroup(lun_id,
                                             hostgroup_id,
                                             hostid)
        host_lun_id = self._find_host_lun_id(hostid, lun_id)

        tgt_port_wwns = []
        for wwn in wwns:
            tgtwwpns = self._get_fc_target_wwpns(wwn)
            if tgtwwpns:
                tgt_port_wwns.append(tgtwwpns)

        init_targ_map = {}
        for initiator in wwns:
            init_targ_map[initiator] = tgt_port_wwns

        # Return FC properties.
        info = {'driver_volume_type': 'fibre_channel',
                'data': {'target_lun': int(host_lun_id),
                         'target_discovered': True,
                         'target_wwn': tgt_port_wwns,
                         'volume_id': volume['id'],
                         'initiator_target_map': init_targ_map}}

        LOG.info(_("initialize_connection_fc, return Nova data is: %s")
                 % info)

        return info

    def _get_iscsi_tgt_port(self):
        url = self.url + "/iscsidevicename"
        result = self.call(url, None)

        msg = 'Get iSCSI target port error.'
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)

        return result['data'][0]['CMO_ISCSI_DEVICE_NAME']

    def _find_hostgroup(self, groupname):
        """Get the given hostgroup id."""
        url = self.url + "/hostgroup?range=[0-8191]"
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, 'Get hostgroup information error.')

        host_group_id = None
        if "data" in result:
            for item in result['data']:
                if groupname == item['NAME']:
                    host_group_id = item['ID']
                    break
        return host_group_id

    def _find_lungroup(self, lungroupname):
        """Get the given hostgroup id."""
        url = self.url + "/lungroup?range=[0-8191]"
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, 'Get lungroup information error.')

        lun_group_id = None
        if 'data' in result:
            for item in result['data']:
                if lungroupname == item['NAME']:
                    lun_group_id = item['ID']
                    break
        return lun_group_id

    def _create_hostgroup_with_check(self, host_group_name):
        """check if host exists in array, or create it."""

        hostgroup_id = self._find_hostgroup(host_group_name)
        if hostgroup_id:
            LOG.info(_(
                '_create_hostgroup_with_check. '
                'hostgroup name: %(name)s, '
                'hostgroup id: %(id)s'),
                {'name': host_group_name,
                 'id': hostgroup_id})
            return hostgroup_id

        try:
            hostgroup_id = self._create_hostgroup(host_group_name)
        except Exception:
            LOG.info(_('failed to create hostgroup: %(name)s. '
                       'check if it exists in array.'),
                     {'name': host_group_name})
            hostgroup_id = self._find_hostgroup(host_group_name)
            if hostgroup_id is None:
                err_msg = (_('error to create hostgroup: %(name)s. '
                             'and it not exists in array.') %
                           {'name': host_group_name})
                LOG.error(err_msg)
                raise exception.CinderException(err_msg)

        LOG.info(_(
            '_create_hostgroup_with_check. '
            'create hostgroup success. '
            'hostgroup name: %(name)s, '
            'hostgroup id: %(id)s'),
            {'name': host_group_name,
             'id': hostgroup_id})
        return hostgroup_id

    def _create_hostgroup(self, hostgroupname):
        url = self.url + "/hostgroup"
        data = json.dumps({"TYPE": "14", "NAME": hostgroupname})
        result = self.call(url, data)

        msg = 'Create hostgroup error.'
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)

        return result['data']['ID']

    def _create_lungroup(self, lungroupname):
        url = self.url + "/lungroup"
        data = json.dumps({"DESCRIPTION": lungroupname,
                           "APPTYPE": '0',
                           "GROUPTYPE": '0',
                           "NAME": lungroupname})
        result = self.call(url, data)

        msg = 'Create lungroup error.'
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)

        return result['data']['ID']

    def _delete_lungroup(self, lungroupid):
        url = self.url + "/LUNGroup/" + lungroupid
        result = self.call(url, None, "DELETE")
        self._assert_rest_result(result, 'Delete lungroup error.')

    def _lungroup_associated(self, viewid, lungroupid):
        url_subfix = ("/mappingview/associate?TYPE=245&"
                      "ASSOCIATEOBJTYPE=256&ASSOCIATEOBJID=%s" % lungroupid)
        url = self.url + url_subfix
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, 'Check lungroup associated error.')

        if "data" in result:
            for item in result['data']:
                if viewid == item['ID']:
                    return True
        return False

    def _hostgroup_associated(self, viewid, hostgroupid):
        url_subfix = ("/mappingview/associate?TYPE=245&"
                      "ASSOCIATEOBJTYPE=14&ASSOCIATEOBJID=%s" % hostgroupid)
        url = self.url + url_subfix
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, 'Check hostgroup associated error.')

        if "data" in result:
            for item in result['data']:
                if viewid == item['ID']:
                    return True
        return False

    def _find_host_lun_id(self, hostid, lunid):

        url = self.url + ("/lun/associate?TYPE=11&ASSOCIATEOBJTYPE=21"
                          "&ASSOCIATEOBJID=%s" % (hostid))
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, 'Find host lun id error.')

        host_lun_id = 1
        if "data" in result:
            for item in result['data']:
                if lunid == item['ID']:
                    associate_data = item['ASSOCIATEMETADATA']
                    try:
                        hostassoinfo = json.loads(associate_data)
                        host_lun_id = hostassoinfo['HostLUNID']
                        break
                    except Exception as err:
                        msg = _("JSON transfer data error. %s") % err
                        LOG.error(msg)
                        raise
        return host_lun_id

    def _find_host(self, hostname):
        """Get the given host ID."""
        url = self.url + "/host?range=[0-65535]"
        data = json.dumps({"TYPE": "21"})
        result = self.call(url, data, "GET")
        self._assert_rest_result(result, 'Find host in hostgroup error.')

        host_id = None
        if "data" in result:
            for item in result['data']:
                if hostname == item['NAME']:
                    host_id = item['ID']
                    break
        return host_id

    def _add_host_with_check(self, host_name, host_name_before_hash):
        hostid = self._find_host(host_name)
        if hostid:
            LOG.info(_(
                '_add_host_with_check. '
                'host name: %(name)s, '
                'host id: %(id)s'),
                {'name': host_name,
                 'id': hostid})
            return hostid

        try:
            hostid = self._add_host(host_name, host_name_before_hash)
        except Exception:
            LOG.info(_('failed to create host: %(name)s. '
                       'check if it exists in array.'),
                     {'name': host_name})
            hostid = self._find_host(host_name)
            if hostid is None:
                err_msg = (_('error to create host: %(name)s. '
                             'and it not exists in array.') %
                           {'name': host_name})
                LOG.error(err_msg)
                raise exception.CinderException(err_msg)

        LOG.info(_(
            '_add_host_with_check. '
            'create host success. '
            'host name: %(name)s, '
            'host id: %(id)s'),
            {'name': host_name,
             'id': hostid})
        return hostid

    def _add_host(self, hostname, host_name_before_hash):
        """Add a new host."""
        url = self.url + "/host"
        data = json.dumps({"TYPE": "21",
                           "NAME": hostname,
                           "OPERATIONSYSTEM": "0",
                           "DESCRIPTION": host_name_before_hash})
        result = self.call(url, data)
        self._assert_rest_result(result, 'Add new host error.')

        if "data" in result:
            return result['data']['ID']
        else:
            return None

    def _is_host_associate_to_hostgroup(self, hostgroup_id, host_id):
        """Check whether the host is associated to the hostgroup."""
        url_subfix = ("/host/associate?TYPE=21&"
                      "ASSOCIATEOBJTYPE=14&ASSOCIATEOBJID=%s" % hostgroup_id)

        url = self.url + url_subfix
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, 'Check hostgroup associated error.')

        if "data" in result:
            for item in result['data']:
                if host_id == item['ID']:
                    return True

        return False

    def _is_lun_associated_to_lungroup(self, lungroup_id, lun_id):
        """Check whether the lun is associated to the lungroup."""
        url_subfix = ("/lun/associate?TYPE=11&"
                      "ASSOCIATEOBJTYPE=256&ASSOCIATEOBJID=%s" % lungroup_id)

        url = self.url + url_subfix
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, 'Check lungroup associate error.')

        if "data" in result:
            for item in result['data']:
                if lun_id == item['ID']:
                    return True

        return False

    def _associate_host_to_hostgroup(self, hostgroup_id, host_id):
        url = self.url + "/hostgroup/associate"
        data = json.dumps({"TYPE": "14",
                           "ID": hostgroup_id,
                           "ASSOCIATEOBJTYPE": "21",
                           "ASSOCIATEOBJID": host_id})

        result = self.call(url, data)
        self._assert_rest_result(result, 'Associate host to hostgroup error.')

    def _associate_lun_to_lungroup(self, lungroupid, lunid):
        """Associate lun to lungroup."""
        url = self.url + "/lungroup/associate"
        data = json.dumps({"ID": lungroupid,
                           "ASSOCIATEOBJTYPE": "11",
                           "ASSOCIATEOBJID": lunid})
        result = self.call(url, data)
        self._assert_rest_result(result, 'Associate lun to lungroup error.')

    def _remove_lun_from_lungroup(self, lungroupid, lunid):
        """Remove lun from lungroup."""

        url = self.url + ("/lungroup/associate?ID=%s"
                          "&ASSOCIATEOBJTYPE=11&ASSOCIATEOBJID=%s"
                          % (lungroupid, lunid))

        result = self.call(url, None, 'DELETE')
        self._assert_rest_result(result,
                                 'Delete associated lun from lungroup error.')

    def _initiator_is_added_to_array(self, ininame):
        """Check whether the initiator is already added on the array."""
        url = self.url + "/iscsi_initiator?range=[0-256]"
        result = self.call(url, None, "GET")
        self._assert_rest_result(result,
                                 'Check initiator added to array error.')

        if "data" in result:
            for item in result['data']:
                if item["ID"] == ininame:
                    return True
        return False

    def _is_initiator_associated_to_host(self, ininame):
        """Check whether the initiator is associated to the host."""
        url = self.url + "/iscsi_initiator?range=[0-256]"
        result = self.call(url, None, "GET")
        self._assert_rest_result(result,
                                 'Check initiator associated to host error.')

        if "data" in result:
            for item in result['data']:
                if item['ID'] == ininame and item['ISFREE'] != "true":
                    return True
        return False

    def _add_initiator_to_array(self, ininame):
        """Add a new initiator to storage device."""
        url = self.url + "/iscsi_initiator/"
        data = json.dumps({"TYPE": "222",
                           "ID": ininame,
                           "USECHAP": "false"})
        result = self.call(url, data)
        self._assert_rest_result(result, 'Add initiator to array error.')

    def _associate_initiator_to_host(self, ininame, hostid):
        """Associate initiator with the host."""
        url = self.url + "/iscsi_initiator/" + ininame
        data = json.dumps({"TYPE": "222",
                           "ID": ininame,
                           "USECHAP": "false",
                           "PARENTTYPE": "21",
                           "PARENTID": hostid})
        result = self.call(url, data, "PUT")
        self._assert_rest_result(result, 'Associate initiator to host error.')

    def _find_mapping_view(self, name):
        """Find mapping view."""
        url = self.url + "/mappingview?range=[0-8191]"
        data = json.dumps({"TYPE": "245"})
        result = self.call(url, data, "GET")

        msg = 'Find map view error.'
        self._assert_rest_result(result, msg)
        viewid = None
        if "data" in result:
            for item in result['data']:
                if name == item['NAME']:
                    viewid = item['ID']
                    break

        return viewid

    def _add_mapping_view(self, name):
        url = self.url + "/mappingview"
        data = json.dumps({"NAME": name, "TYPE": "245"})
        result = self.call(url, data)
        self._assert_rest_result(result, 'Add map view error.')

        return result['data']['ID']

    def _associate_hostgroup_to_view(self, viewID, hostGroupID):
        url = self.url + "/MAPPINGVIEW/CREATE_ASSOCIATE"
        data = json.dumps({"ASSOCIATEOBJTYPE": "14",
                           "ASSOCIATEOBJID": hostGroupID,
                           "TYPE": "245",
                           "ID": viewID})
        result = self.call(url, data, "PUT")
        self._assert_rest_result(result, 'Associate host to view error.')

    def _associate_lungroup_to_view(self, viewID, lunGroupID):
        url = self.url + "/MAPPINGVIEW/CREATE_ASSOCIATE"
        data = json.dumps({"ASSOCIATEOBJTYPE": "256",
                           "ASSOCIATEOBJID": lunGroupID,
                           "TYPE": "245",
                           "ID": viewID})
        result = self.call(url, data, "PUT")
        self._assert_rest_result(result, 'Associate lungroup to view error.')

    def _delete_lungroup_mapping_view(self, view_id, lungroup_id):
        """Remove lungroup associate from the mapping view."""
        url = self.url + "/mappingview/REMOVE_ASSOCIATE"
        data = json.dumps({"ASSOCIATEOBJTYPE": "256",
                           "ASSOCIATEOBJID": lungroup_id,
                           "TYPE": "245",
                           "ID": view_id})
        result = self.call(url, data, "PUT")
        self._assert_rest_result(result, 'Delete lungroup from view error.')

    def _delete_hostgoup_mapping_view(self, view_id, hostgroup_id):
        """Remove hostgroup associate from the mapping view."""
        url = self.url + "/mappingview/REMOVE_ASSOCIATE"
        data = json.dumps({"ASSOCIATEOBJTYPE": "14",
                           "ASSOCIATEOBJID": hostgroup_id,
                           "TYPE": "245",
                           "ID": view_id})
        result = self.call(url, data, "PUT")
        self._assert_rest_result(result, 'Delete hostgroup from view error.')

    def _delete_portgroup_mapping_view(self, view_id, portgroup_id):
        """Remove portgroup associate from the mapping view."""
        url = self.url + "/mappingview/REMOVE_ASSOCIATE"
        data = json.dumps({"ASSOCIATEOBJTYPE": "257",
                           "ASSOCIATEOBJID": portgroup_id,
                           "TYPE": "245",
                           "ID": view_id})
        result = self.call(url, data, "PUT")
        self._assert_rest_result(result, 'Delete portgroup from view error.')

    def _delete_mapping_view(self, view_id):
        """Remove mapping view from the storage."""
        url = self.url + "/mappingview/" + view_id
        result = self.call(url, None, "DELETE")
        self._assert_rest_result(result, 'Delete map view error.')

    def _get_lunnum_from_lungroup(self, lungroup_id):
        """Check if there are still other luns associated to the lungroup."""
        url_subfix = ("/lun/count?TYPE=11&ASSOCIATEOBJTYPE=256&"
                      "ASSOCIATEOBJID=%s" % lungroup_id)
        url = self.url + url_subfix
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, 'Find lun number error.')
        lunnum = -1
        if "data" in result:
            lunnum = result['data']['COUNT']
        return lunnum

    def _is_portgroup_associated_to_view(self, view_id, portgroup_id):
        """Check whether the port group is associated to the mapping view."""
        url_subfix = ("/portgroup/associate?ASSOCIATEOBJTYPE=245&"
                      "ASSOCIATEOBJID=%s&range=[0-8191]" % view_id)
        url = self.url + url_subfix
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, 'Find port group from mapping view '
                                 'error.')
        if "data" in result:
            for item in result['data']:
                if portgroup_id == item['ID']:
                    return True

        return False

    def _find_lungroup_from_map(self, view_id):
        """Get lungroup from the given map"""
        url_subfix = ("/mappingview/associate/lungroup?TYPE=256&"
                      "ASSOCIATEOBJTYPE=245&ASSOCIATEOBJID=%s" % view_id)
        url = self.url + url_subfix
        result = self.call(url, None, "GET")
        self._assert_rest_result(result, 'Find lun group from mapping view '
                                 'error.')
        lungroup_id = None
        if "data" in result:
            # One map can have only one lungroup.
            for item in result['data']:
                lungroup_id = item['ID']
        return lungroup_id

    @utils.synchronized('huawei', external=True)
    def terminate_connection_iscsi(self, volume, connector):
        """Delete map between a volume and a host."""
        initiator_name = connector['initiator']
        volume_name = self._encode_name(volume['id'])
        lun_id = volume.get('provider_location', None)
        host_name = connector['host']
        lungroup_id = None
        view_id = None

        LOG.info(_('terminate_connection_iscsi: volume name: %(volume)s, '
                   'initiator name: %(ini)s, '
                   'lun_id: %(lunid)s.')
                 % {'volume': volume_name,
                    'ini': initiator_name,
                    'lunid': lun_id})

        iscsi_conf = self._get_iscsi_conf()
        portgroup = None
        portgroup_id = None
        left_lunnum = -1
        for ini in iscsi_conf['Initiator']:
            if ini['Name'] == initiator_name:
                for key in ini:
                    if key == 'TargetPortGroup':
                        portgroup = ini['TargetPortGroup']
                        break

        if portgroup:
            portgroup_id = self._find_tgt_port_group(portgroup)
        if host_name and (len(host_name) > 31):
            host_name = six.text_type(hash(host_name))
        hostid = self._find_host(host_name)
        if hostid:
            mapping_view_name = MAPPING_VIEW_PREFIX + hostid
            view_id = self._find_mapping_view(mapping_view_name)
            if view_id:
                lungroup_id = self._find_lungroup_from_map(view_id)

        # Remove lun from lungroup.
        if lun_id and self._check_lun_exist(lun_id) is True:
            if lungroup_id:
                lungroup_ids = self._get_lungroupids_by_lunid(lun_id)
                if lungroup_id in lungroup_ids:
                    self._remove_lun_from_lungroup(lungroup_id, lun_id)
                else:
                    LOG.warn(_("Lun is not in lungroup. "
                               "Lun id: %(lun_id)s, "
                               "lungroup id: %(lungroup_id)s"),
                             {"lun_id": lun_id,
                              "lungroup_id": lungroup_id})
        else:
            LOG.warn(_("Can't find lun on the array"))

        # Remove portgroup from mapping view if no lun left in lungroup.
        if lungroup_id:
            left_lunnum = self._get_lunnum_from_lungroup(lungroup_id)

        if portgroup_id and view_id and (int(left_lunnum) <= 0):
            if self._is_portgroup_associated_to_view(view_id, portgroup_id):
                self._delete_portgroup_mapping_view(view_id, portgroup_id)

    def terminate_connection_fc(self, volume, connector):
        """Delete map between a volume and a host."""
        wwns = connector['wwpns']
        volume_name = self._encode_name(volume['id'])
        lun_id = volume.get('provider_location', None)
        host_name = connector['host']
        left_lunnum = -1
        lungroup_id = None

        LOG.info(_('terminate_connection_fc: volume name: %(volume)s, '
                   'wwpns: %(wwns)s, '
                   'lun_id: %(lunid)s.'),
                 {'volume': volume_name,
                  'wwns': wwns,
                  'lunid': lun_id})

        if host_name and (len(host_name) > 31):
            host_name = six.text_type(hash(host_name))
        hostid = self._find_host(host_name)
        if hostid:
            mapping_view_name = MAPPING_VIEW_PREFIX + hostid
            view_id = self._find_mapping_view(mapping_view_name)
            if view_id:
                lungroup_id = self._find_lungroup_from_map(view_id)

        if lun_id and self._check_lun_exist(lun_id) is True:
            if lungroup_id:
                lungroup_ids = self._get_lungroupids_by_lunid(lun_id)
                if lungroup_id in lungroup_ids:
                    self._remove_lun_from_lungroup(lungroup_id, lun_id)
                else:
                    LOG.warn(_("Lun is not in lungroup. "
                               "Lun id: %(lun_id)s, "
                               "lungroup id: %(lungroup_id)s"),
                             {"lun_id": lun_id,
                              "lungroup_id": lungroup_id})
        else:
            LOG.warn(_("Can't find lun on the array"))

        tgt_port_wwns = []
        for wwn in wwns:
            tgtwwpns = self._get_fc_target_wwpns(wwn)
            if tgtwwpns:
                tgt_port_wwns.append(tgtwwpns)

        init_targ_map = {}
        for initiator in wwns:
            init_targ_map[initiator] = tgt_port_wwns
        if lungroup_id:
            left_lunnum = self._get_lunnum_from_lungroup(lungroup_id)
        if int(left_lunnum) > 0:
            info = {'driver_volume_type': 'fibre_channel',
                    'data': {}}
        else:
            info = {'driver_volume_type': 'fibre_channel',
                    'data': {'target_wwn': tgt_port_wwns,
                             'initiator_target_map': init_targ_map}}

        return info

    def login_out(self):
        """logout the session."""
        url = self.url + "/sessions"
        result = self.call(url, None, "DELETE")
        self._assert_rest_result(result, 'Log out of session error.')

    def _start_luncopy(self, luncopyid):
        """Start a LUNcopy."""
        url = self.url + "/LUNCOPY/start"
        data = json.dumps({"TYPE": "219", "ID": luncopyid})
        result = self.call(url, data, "PUT")
        self._assert_rest_result(result, 'Start lun copy error.')

    def _get_capacity(self):
        """Get free capacity and total capacity of the pools."""
        poolinfo = self._find_pool_info()
        pool_capacity = {'total_capacity': 0.0,
                         'CAPACITY': 0.0}

        if poolinfo:
            total = int(poolinfo['TOTALCAPACITY']) / 1024.0 / 1024.0 / 2
            free = int(poolinfo['CAPACITY']) / 1024.0 / 1024.0 / 2
            pool_capacity['total_capacity'] = total
            pool_capacity['free_capacity'] = free

        return pool_capacity

    def _get_lun_conf_params(self):
        """Get parameters from config file for creating lun."""
        # Default lun set information.
        lunsetinfo = {'LUNType': 'Thick',
                      'StripUnitSize': '64',
                      'WriteType': '1',
                      'MirrorSwitch': '1',
                      'PrefetchType': '3',
                      'PrefetchValue': '0',
                      'PrefetchTimes': '0'}

        root = self._read_xml()
        luntype = root.findtext('LUN/LUNType')
        if luntype:
            if luntype.strip() in ['Thick', 'Thin']:
                lunsetinfo['LUNType'] = luntype.strip()
                if luntype.strip() == 'Thick':
                    lunsetinfo['LUNType'] = 0
                if luntype.strip() == 'Thin':
                    lunsetinfo['LUNType'] = 1

            elif luntype is not '' and luntype is not None:
                err_msg = (_(
                    'Config file is wrong. LUNType must be "Thin"'
                    ' or "Thick". LUNType: %(fetchtype)s.')
                    % {'fetchtype': luntype})
                LOG.error(err_msg)
                raise exception.VolumeBackendAPIException(data=err_msg)

        stripunitsize = root.findtext('LUN/StripUnitSize')
        if stripunitsize is not None:
            lunsetinfo['StripUnitSize'] = stripunitsize.strip()
        writetype = root.findtext('LUN/WriteType')
        if writetype is not None:
            lunsetinfo['WriteType'] = writetype.strip()
        mirrorswitch = root.findtext('LUN/MirrorSwitch')
        if mirrorswitch is not None:
            lunsetinfo['MirrorSwitch'] = mirrorswitch.strip()

        prefetch = root.find('LUN/Prefetch')
        fetchtype = prefetch.attrib['Type']
        if prefetch is not None and prefetch.attrib['Type']:
            if fetchtype in ['0', '1', '2', '3']:
                lunsetinfo['PrefetchType'] = fetchtype.strip()
                typevalue = prefetch.attrib['Value'].strip()
                if lunsetinfo['PrefetchType'] == '1':
                    double_value = int(typevalue) * 2
                    typevalue_double = six.text_type(double_value)
                    lunsetinfo['PrefetchValue'] = typevalue_double
                elif lunsetinfo['PrefetchType'] == '2':
                    lunsetinfo['PrefetchValue'] = typevalue
            else:
                err_msg = (_(
                    'PrefetchType config is wrong. PrefetchType'
                    ' must be in 0,1,2,3. PrefetchType is: %(fetchtype)s.')
                    % {'fetchtype': fetchtype})
                LOG.error(err_msg)
                raise exception.CinderException(err_msg)
        else:
            LOG.info(_('Use default PrefetchType. '
                       'PrefetchType: Intelligent.'))

        return lunsetinfo

    def _wait_for_luncopy(self, luncopyid):
        """Wait for LUNcopy to complete."""
        while True:
            luncopy_info = self._get_luncopy_info(luncopyid)
            if luncopy_info['status'] == '40':
                break
            elif luncopy_info['state'] != '1':
                err_msg = (_(
                    '_wait_for_luncopy:LUNcopy status is not normal.'
                    'LUNcopy name: %(luncopyname)s')
                    % {'luncopyname': luncopyid})
                LOG.error(err_msg)
                raise exception.VolumeBackendAPIException(data=err_msg)
            time.sleep(15)

    def _get_luncopy_info(self, luncopyid):
        """Get LUNcopy information."""
        url = self.url + "/LUNCOPY?range=[0-1023]"
        data = json.dumps({"TYPE": "219", })
        result = self.call(url, data, "GET")
        self._assert_rest_result(result, 'Get lun copy information error.')

        luncopyinfo = {}
        if "data" in result:
            for item in result['data']:
                if luncopyid == item['ID']:
                    luncopyinfo['name'] = item['NAME']
                    luncopyinfo['id'] = item['ID']
                    luncopyinfo['state'] = item['HEALTHSTATUS']
                    luncopyinfo['status'] = item['RUNNINGSTATUS']
                    break
        return luncopyinfo

    def _delete_luncopy(self, luncopyid):
        """Delete a LUNcopy."""
        url = self.url + "/LUNCOPY/%s" % luncopyid
        result = self.call(url, None, "DELETE")
        self._assert_rest_result(result, 'Delete lun copy error.')

    def _get_connected_free_wwns(self):
        """Get free connected FC port WWNs.

        If no new ports connected, return an empty list.
        """
        url = self.url + "/fc_initiator?ISFREE=true&range=[0-8191]"
        result = self.call(url, None, "GET")

        msg = 'Get connected free FC wwn error.'
        self._assert_rest_result(result, msg)

        wwns = []
        if 'data' in result:
            for item in result['data']:
                wwns.append(item['ID'])

        return wwns

    def _add_fc_port_to_host(self, hostid, wwn):
        """Add a FC port to the host."""
        url = self.url + "/fc_initiator/" + wwn
        data = json.dumps({"TYPE": "223",
                           "ID": wwn,
                           "PARENTTYPE": 21,
                           "PARENTID": hostid})
        result = self.call(url, data, "PUT")
        self._assert_rest_result(result, 'Add FC port to host error.')

    def _get_iscsi_port_info(self, ip):
        """Get iscsi port info in order to build the iscsi target iqn."""
        url = self.url + "/eth_port"
        result = self.call(url, None, "GET")

        msg = 'Get iSCSI port information error.'
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)

        iscsi_port_info = None
        for item in result['data']:
            if ip == item['IPV4ADDR']:
                iscsi_port_info = item['LOCATION']
                break

        return iscsi_port_info

    def _get_iscsi_conf(self):
        """Get iSCSI info from config file."""
        iscsiinfo = {}
        root = self._read_xml()
        TargetIP = root.findtext('iSCSI/DefaultTargetIP').strip()
        iscsiinfo['DefaultTargetIP'] = TargetIP
        initiator_list = []

        for dic in root.findall('iSCSI/Initiator'):
            # Strip values of dic.
            tmp_dic = {}
            for k in dic.items():
                tmp_dic[k[0]] = k[1].strip()

            initiator_list.append(tmp_dic)

        iscsiinfo['Initiator'] = initiator_list

        return iscsiinfo

    def _get_tgt_iqn(self, iscsiip):
        """Get target iSCSI iqn."""

        ip_info = self._get_iscsi_port_info(iscsiip)
        iqn_prefix = self._get_iscsi_tgt_port()

        LOG.info(_('request ip info is %s.') % ip_info)
        split_list = ip_info.split(".")
        newstr = split_list[1] + split_list[2]
        LOG.info(_('new str info is %s.') % newstr)

        if ip_info:
            if newstr[0] == 'A':
                ctr = "0"
            elif newstr[0] == 'B':
                ctr = "1"
            interface = '0' + newstr[1]
            port = '0' + newstr[3]
            iqn_suffix = ctr + '02' + interface + port
            for i in range(0, len(iqn_suffix)):
                if iqn_suffix[i] != '0':
                    iqn_suffix = iqn_suffix[i:]
                    break
            iqn = iqn_prefix + ':' + iqn_suffix + ':' + iscsiip
            LOG.info(_('_get_tgt_iqn: iSCSI target iqn is %s') % iqn)
            return iqn
        else:
            return None

    def _get_fc_target_wwpns(self, wwn):
        url = (self.url +
               "/host_link?INITIATOR_TYPE=223&INITIATOR_PORT_WWN=" + wwn)
        result = self.call(url, None, "GET")

        msg = 'Get FC target wwpn error.'
        self._assert_rest_result(result, msg)

        fc_wwpns = None
        if "data" in result:
            for item in result['data']:
                if wwn == item['INITIATOR_PORT_WWN']:
                    fc_wwpns = item['TARGET_PORT_WWN']
                    break

        return fc_wwpns

    def update_volume_stats(self):
        capacity = self._get_capacity()
        data = {}
        data['vendor_name'] = 'Huawei'
        data['total_capacity_gb'] = capacity['total_capacity']
        data['free_capacity_gb'] = capacity['free_capacity']
        data['reserved_percentage'] = 0
        data['QoS_support'] = True
        data['Tier_support'] = True
        return data

    def _find_qos_policy_info(self, policy_name):
        url = self.url + "/ioclass"
        result = self.call(url, None, "GET")

        msg = 'Get QoS policy error.'
        self._assert_rest_result(result, msg)

        qos_info = {}
        if "data" in result:
            for item in result['data']:
                if policy_name == item['NAME']:
                    qos_info['ID'] = item['ID']
                    lun_list = json.loads(item['LUNLIST'])
                    qos_info['LUNLIST'] = lun_list
                    qos_info['RUNNINGSTATUS'] = item['RUNNINGSTATUS']
                    break

        return qos_info

    def _update_qos_policy_lunlist(self, lunlist, policy_id):
        url = self.url + "/ioclass/" + policy_id
        data = json.dumps({"TYPE": "230",
                           "ID": policy_id,
                           "LUNLIST": lunlist})
        result = self.call(url, data, "PUT")
        self._assert_rest_result(result, 'Update QoS policy error.')

    def _get_login_info(self):
        """Get login IP, username and password from config file."""
        logininfo = {}
        filename = self.configuration.cinder_huawei_conf_file
        tree = ET.parse(filename)
        root = tree.getroot()
        logininfo['RestURL'] = root.findtext('Storage/RestURL').strip()

        need_encode = False
        for key in ['UserName', 'UserPassword']:
            node = root.find('Storage/%s' % key)
            node_text = node.text
            # Prefix !$$$ means encoded already.
            if node_text.find('!$$$') > -1:
                logininfo[key] = base64.b64decode(node_text[4:])
            else:
                logininfo[key] = node_text
                node.text = '!$$$' + base64.b64encode(node_text)
                need_encode = True
        if need_encode:
            self._change_file_mode(filename)
            try:
                tree.write(filename, 'UTF-8')
            except Exception as err:
                LOG.warn(_('%s') % err)

        return logininfo

    def _change_file_mode(self, filepath):
        utils.execute('chmod', '640', filepath, run_as_root=True)

    def _check_conf_file(self):
        """Check the config file, make sure the essential items are set."""
        root = self._read_xml()
        resturl = root.findtext('Storage/RestURL')
        username = root.findtext('Storage/UserName')
        pwd = root.findtext('Storage/UserPassword')
        pool_node = root.findall('LUN/StoragePool')

        if (not resturl) or (not username) or (not pwd):
            err_msg = (_(
                '_check_conf_file: Config file invalid. RestURL,'
                ' UserName and UserPassword must be set.'))
            LOG.error(err_msg)
            raise exception.InvalidInput(reason=err_msg)

        if not pool_node:
            err_msg = (_(
                '_check_conf_file: Config file invalid. '
                'StoragePool must be set.'))
            LOG.error(err_msg)
            raise exception.InvalidInput(reason=err_msg)

    def _get_tgt_ip_from_portgroup(self, portgroup_id):
        target_ip = None
        url = self.url + ("/eth_port/associate?TYPE=213&ASSOCIATEOBJTYPE=257"
                          "&ASSOCIATEOBJID=%s" % portgroup_id)
        result = self.call(url, None, "GET")

        msg = 'Get eth_port error.'
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)

        if "data" in result:
            for item in result['data']:
                if (item['IPV4ADDR'] and item['HEALTHSTATUS'] == STATUS_HEALTH
                        and item['RUNNINGSTATUS'] == STATUS_RUNNING):
                    target_ip = item['IPV4ADDR']
                    LOG.info(_('_get_tgt_ip_from_portgroup: Get ip: %s.')
                             % target_ip)
                    break
        return target_ip

    def _get_iscsi_params(self, connector):
        """Get target iSCSI params, including iqn, IP."""
        initiator = connector['initiator']
        iscsi_conf = self._get_iscsi_conf()
        target_ip = None
        portgroup = None
        portgroup_id = None
        for ini in iscsi_conf['Initiator']:
            if ini['Name'] == initiator:
                for key in ini:
                    if key == 'TargetPortGroup':
                        portgroup = ini['TargetPortGroup']
                    elif key == 'TargetIP':
                        target_ip = ini['TargetIP']

        if portgroup:
            portgroup_id = self._find_tgt_port_group(portgroup)
            target_ip = self._get_tgt_ip_from_portgroup(portgroup_id)

        # If didn't specify target IP for some initiator, use default IP.
        if not target_ip:
            if iscsi_conf['DefaultTargetIP']:
                target_ip = iscsi_conf['DefaultTargetIP']

            else:
                msg = (_(
                    '_get_iscsi_params: Failed to get target IP '
                    'for initiator %(ini)s, please check config file.')
                    % {'ini': initiator})
                LOG.error(msg)
                raise exception.InvalidInput(reason=msg)

        LOG.info(_('Get the default ip: %s') % target_ip)
        # If didn't get target IP for rest, Automated assembly target ip.
        target_iqn = self._get_tgt_iqn_from_rest(target_ip)

        if not target_iqn:
            target_iqn = self._get_tgt_iqn(target_ip)

        return (target_iqn, target_ip, portgroup_id)

    def _get_tgt_iqn_from_rest(self, target_ip):
        url = self.url + "/iscsi_tgt_port"
        result = self.call(url, None, "GET")

        target_iqn = None
        if result['error']['code'] != 0:
            LOG.warn(_("Can't find target iqn from rest"))
            return target_iqn

        if 'data' in result:
            for item in result['data']:
                if target_ip in item['ID']:
                    target_iqn = item['ID']

        if not target_iqn:
            LOG.warn(_("Can't find target iqn from rest"))
            return target_iqn

        split_list = target_iqn.split(",")
        target_iqn_before = split_list[0]

        split_list_new = target_iqn_before.split("+")
        target_iqn = split_list_new[1]

        return target_iqn

    @utils.synchronized('huawei', external=True)
    def extend_volume(self, volume, new_size):
        """Extends a Huawei volume."""

        LOG.info(_('Entering extend_volume.'))
        volume_size = self._get_volume_size(volume)
        new_volume_size = int(new_size) * units.GiB / 512
        volume_name = self._encode_name(volume['id'])

        LOG.info(_('Extend Volume: %(volumename)s, oldsize:'
                   ' %(oldsize)s  newsize: %(newsize)s')
                 % {'volumename': volume_name,
                    'oldsize': volume_size,
                    'newsize': new_volume_size})

        lun_id = volume.get('provider_location', None)
        if lun_id is None:
            lun_id = self._get_volume_by_name(volume_name)
        if lun_id is None:
            msg = (_("Can't find lun info on the array, "
                     "lun name is: %(name)s.")
                   % {'name': volume_name})
            LOG.error(msg)
            raise exception.CinderException(msg)

        url = self.url + "/lun/expand"
        data = json.dumps({"TYPE": 11, "ID": lun_id,
                           "CAPACITY": new_volume_size})
        result = self.call(url, data, 'PUT')

        msg = 'Extend volume error.'
        self._assert_rest_result(result, msg)
        self._assert_data_in_result(result, msg)

        return result['data']['ID']

    def _get_volume_type(self, type_id):
        ctxt = context.get_admin_context()
        return volume_types.get_volume_type(ctxt, type_id)

    def _get_qos_by_volume_type(self, volume_type):
        qos = {}
        qos_specs_id = volume_type.get('qos_specs_id')

        # NOTE(kmartin): We prefer the qos_specs association
        # and override any existing extra-specs settings
        # if present.
        if qos_specs_id is not None:
            kvs = qos_specs.get_qos_specs(context.get_admin_context(),
                                          qos_specs_id)['specs']
        else:
            return qos

        LOG.info(_('the qos sepcs is :%s') % kvs)
        for key, value in kvs.items():
            if key in huawei_valid_keys:
                qos[key.upper()] = value

        return qos

    def _get_qos_value(self, qos, key, default=None):
        if key in qos:
            return qos[key]
        else:
            return default

    def _create_qos_policy(self, qos, lun_id):

        # Get local time.
        localtime = time.strftime('%Y%m%d%H%M%S', time.localtime(time.time()))
        # Package QoS name.
        qos_name = QOS_NAME_PREFIX + lun_id + '_' + localtime
        baseData = {"TYPE": "230",
                    "NAME": qos_name,
                    "LUNLIST": ["%s" % lun_id],
                    "CLASSTYPE": "1",
                    "SCHEDULEPOLICY": "2",
                    "SCHEDULESTARTTIME": "1410969600",
                    "STARTTIME": "08:00",
                    "DURATION": "86400",
                    "CYCLESET": "[1,2,3,4,5,6,0]"
                    }

        mergedata = dict(baseData.items() + qos.items())
        url = self.url + "/ioclass/"
        data = json.dumps(mergedata)

        result = self.call(url, data)
        self._assert_rest_result(result, 'Create QoS policy error.')

        return result['data']['ID']

    def _delete_qos_policy(self, qos_id):
        """Delete a QoS policy."""

        url = self.url + "/ioclass/" + qos_id
        data = json.dumps({"TYPE": "230",
                           "ID": qos_id})

        result = self.call(url, data, 'DELETE')
        self._assert_rest_result(result, 'Delete QoS policy error.')

    def _active_deactive_qos(self, qos_id, enablestatus):
        """Active or deactive QoS.

        enablestatus: true (active)
        enbalestatus: false (deactive)
        """

        url = self.url + "/ioclass/active/" + qos_id
        data = json.dumps({"TYPE": 230,
                           "ID": qos_id,
                           "ENABLESTATUS": enablestatus})
        result = self.call(url, data, "PUT")
        self._assert_rest_result(result, 'Active or Deactive QoS error.')

    def _get_qos_info(self, qos_id):
        """Get QoS information."""

        url = self.url + "/ioclass/" + qos_id
        data = json.dumps({"TYPE": "230",
                           "ID": qos_id})
        result = self.call(url, data, "GET")
        self._assert_rest_result(result, 'Get QoS information error.')

        return result['data']

    def _check_qos_high_priority(self, qos):
        """Check QoS priority."""

        for key, value in qos.items():
            if (key.find('MIN') == 0) or (key.find('LATENCY') == 0):
                return True

        return False

    def _change_lun_priority(self, lunid):
        """Change lun priority to high."""

        url = self.url + "/lun/" + lunid
        data = json.dumps({"TYPE": "11",
                           "ID": lunid,
                           "IOPRIORITY": "3"})

        result = self.call(url, data, "PUT")
        self._assert_rest_result(result, 'Change lun priority error.')

    def _get_qosid_by_lunid(self, lunid):
        """Get qosid by lunid."""

        url = self.url + "/lun/" + lunid
        data = json.dumps({"TYPE": "11",
                           "ID": lunid})

        result = self.call(url, data, "GET")
        self._assert_rest_result(result, 'Get qosid by lunid error.')

        return result['data']['IOCLASSID']

    def _get_lungroupids_by_lunid(self, lunid):
        """Get lungroup ids by lunid."""

        url = self.url + ("/lungroup/associate?TYPE=256"
                          "&ASSOCIATEOBJTYPE=11&ASSOCIATEOBJID=%s" % lunid)

        result = self.call(url, None, "GET")
        self._assert_rest_result(result, 'Get lungroup ids by lunid error.')

        lun_group_ids = []
        if 'data' in result:
            for item in result['data']:
                lun_group_ids.append(item['ID'])

        return lun_group_ids
