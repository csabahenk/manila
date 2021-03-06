# Copyright (c) 2014 EMC Corporation.
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

"""
EMC specific NAS storage driver. This driver is a pluggable driver
that allows specific EMC NAS devices to be plugged-in as the underlying
backend. Use the Manila configuration variable "share_backend_name"
to specify, which backend plugins to use.
"""

from oslo.config import cfg

from manila.openstack.common import log
from manila.share import driver
from manila.share.drivers.emc.plugins import \
    registry as emc_plugins_registry
# TODO(jay.xu): Implement usage of stevedore for plugins.
from manila.share.drivers.emc.plugins.vnx import connection    # noqa


LOG = log.getLogger(__name__)

EMC_NAS_OPTS = [
    cfg.StrOpt('emc_nas_login',
               default=None,
               help='User name for the EMC server.'),
    cfg.StrOpt('emc_nas_password',
               default=None,
               help='Password for the EMC server.'),
    cfg.StrOpt('emc_nas_server',
               default=None,
               help='EMC server hostname or IP address.'),
    cfg.IntOpt('emc_nas_server_port',
               default=8080,
               help='Port number for the EMC server.'),
    cfg.BoolOpt('emc_nas_server_secure',
                default=True,
                help='Use secure connection to server.'),
    cfg.StrOpt('emc_share_backend',
               default=None,
               help='Share backend.'),
    cfg.StrOpt('emc_nas_server_container',
               default='server_2',
               help='Container of share servers.'),
    cfg.StrOpt('emc_nas_pool_name',
               default=None,
               help='EMC pool name.'),
]

CONF = cfg.CONF
CONF.register_opts(EMC_NAS_OPTS)


class EMCShareDriver(driver.ShareDriver):
    """EMC specific NAS driver. Allows for NFS and CIFS NAS storage usage."""
    def __init__(self, *args, **kwargs):
        super(EMCShareDriver, self).__init__()
        self.configuration = kwargs.get('configuration', None)
        if self.configuration:
            self.configuration.append_config_values(EMC_NAS_OPTS)

        self._storage_conn = None

    def create_share(self, context, share, share_server=None):
        """Is called to create share."""
        location = self._storage_conn.create_share(self, context, share,
                                                   share_server)

        return location

    def create_share_from_snapshot(self, context, share, snapshot,
                                   share_server=None):
        """Is called to create share from snapshot."""
        location = self._storage_conn.create_share_from_snapshot(
            self, context, share, snapshot, share_server)

        return location

    def create_snapshot(self, context, snapshot, share_server=None):
        """Is called to create snapshot."""
        self._storage_conn.create_snapshot(self, context, snapshot,
                                           share_server)

    def delete_share(self, context, share, share_server=None):
        """Is called to remove share."""
        self._storage_conn.delete_share(self, context, share, share_server)

    def delete_snapshot(self, context, snapshot, share_server=None):
        """Is called to remove snapshot."""
        self._storage_conn.delete_snapshot(self, context, snapshot,
                                           share_server)

    def ensure_share(self, context, share, share_server=None):
        """Invoked to sure that share is exported."""
        self._storage_conn.ensure_share(self, context, share, share_server)

    def allow_access(self, context, share, access, share_server=None):
        """Allow access to the share."""
        self._storage_conn.allow_access(self, context, share, access,
                                        share_server)

    def deny_access(self, context, share, access, share_server=None):
        """Deny access to the share."""
        self._storage_conn.deny_access(self, context, share, access,
                                       share_server)

    def check_for_setup_error(self):
        """Check for setup error."""
        pass

    def do_setup(self, context):
        """Any initialization the share driver does while starting."""
        self._storage_conn = emc_plugins_registry.create_storage_connection(
            self.configuration.safe_get('emc_share_backend'), LOG)
        self._storage_conn.connect(self, context)

    def get_share_stats(self, refresh=False):
        """Get share stats.

        If 'refresh' is True, run update the stats first.
        """
        if refresh:
            self._update_share_stats()

        return self._stats

    def _update_share_stats(self):
        """Retrieve stats info from share."""

        LOG.debug("Updating share stats.")
        data = {}
        backend_name = self.configuration.safe_get(
            'share_backend_name') or "EMC_NAS_Storage"
        data["share_backend_name"] = backend_name
        data["vendor_name"] = 'EMC'
        data["driver_version"] = '1.0'
        data["storage_protocol"] = 'NFS_CIFS'

        data['total_capacity_gb'] = 'infinite'
        data['free_capacity_gb'] = 'infinite'
        data['reserved_percentage'] = 0
        data['QoS_support'] = False
        self._storage_conn.update_share_stats(data)
        self._stats = data

    def get_network_allocations_number(self):
        """Returns number of network allocations for creating VIFs."""
        return self._storage_conn.get_network_allocations_number(self)

    def setup_server(self, network_info, metadata=None):
        """Set up and configures share server with given network parameters."""
        return self._storage_conn.setup_server(self, network_info, metadata)

    def teardown_server(self, server_details, security_services=None):
        """Teardown share server."""
        return self._storage_conn.teardown_server(self,
                                                  server_details,
                                                  security_services)
