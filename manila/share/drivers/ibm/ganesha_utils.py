# Copyright 2014 IBM Corp.
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
Ganesha Admin Utilities

Ganesha NFS does not provide many tools for automating the process of creating
and managing export defintions.  This module provides utilities to help parse
a specified ganesha config file and return a map containing the export
definitions and attributes.  A method republishing updated export definitions
is also provided.  And there are methods for requesting the ganesha server
to reload the export definitions.

Consider moving this to common location for use by other manila drivers.
"""

from copy import copy
import os
import re
import socket
import time

import dbus
import netaddr

from manila import exception
from manila.openstack.common import log as logging
from manila import utils

LOG = logging.getLogger(__name__)
# more simple pattern for matching a single avpair per line,
# skips lines starting with # comment char
AVPATTERN = re.compile('^\s*(?!#)\s*(?P<attr>\S+)\s*=\s*(?P<val>\S+)\s*;')

# NFS Ganesha 1.5 format used here.
# TODO(nileshb): Upgrade it to NFS Ganesha 2.1 format
DEFAULT_EXPORT_ATTRS = {
    'export_id': 'undefined',
    'path': 'undefined',
    'fsal': 'undefined',
    'root_access': '"*"',
    'rw_access': '"*"',
    'pseudo': 'undefined',
    'anonymous_root_uid': '-2',
    'nfs_protocols': '"3,4"',
    'transport_protocols': '"UDP,TCP"',
    'sectype': '"sys"',
    'maxread': '65536',
    'maxwrite': '65536',
    'prefread': '65536',
    'prefwrite': '65536',
    'filesystem_id': '192.168',
    'tag': 'undefined',
}

STARTING_EXPORT_ID = 100


def valid_flags():
    return DEFAULT_EXPORT_ATTRS.keys()


def parse_ganesha_config(configpath):
    """Parse the specified ganesha configuration.

    Parse a configuration file and return a list of lines that were found
    before the first EXPORT block, and a dictionary of exports and their
    attributes.

    The input configuration file should be a valid ganesha config file and the
    export blocks should be the last items in the file.
    """
    export_count = 0
    exports = dict()
    pre_lines = []
    with open(configpath) as f:
        for l in f.readlines():
            line = l.strip()
            if export_count == 0 and line != 'EXPORT':
                pre_lines.append(line)
            else:
                if line == 'EXPORT':
                    export_count += 1
                    expattrs = dict()
                try:
                    match_obj = AVPATTERN.match(line)
                    attr = match_obj.group('attr').lower()
                    val = match_obj.group('val')
                    expattrs[attr] = val
                    if attr == 'export_id':
                        exports[val] = expattrs
                except AttributeError:
                    pass

    assert export_count == len(exports), \
        ('Invalid export config file %s: %s export clauses found, but %s'
         ' export_ids' % (configpath, export_count, len(exports)))
    return pre_lines, exports


def get_export_by_path(exports, path):
    """Return the export that matches the specified path."""
    try:
        return [exports[exp] for exp in exports
                if exports[exp]['path'].strip('"\'') == path].pop()
    except IndexError:
        return None


def export_exists(exports, path):
    """Return true if an export exists with the specified path."""
    export_paths = [exports[e]['path'].strip('"\'') for e in exports]
    return path in export_paths


def get_next_id(exports):
    """Return an export id that is one larger than largest existing id."""
    try:
        next_id = max(map(int, exports.keys())) + 1
    except ValueError:
        next_id = STARTING_EXPORT_ID

    return next_id


def get_export_template():
    return copy(DEFAULT_EXPORT_ATTRS)


def _convert_ipstring_to_ipn(ipstring):
    """Transform a single ip string into a list of IPNetwork objects."""
    if netaddr.valid_glob(ipstring):
        ipns = netaddr.glob_to_cidrs(ipstring)
    else:
        try:
            ipns = [netaddr.IPNetwork(ipstring)]
        except netaddr.AddrFormatError:
            msg = (_('Invalid IP access string %s') % ipstring)
            LOG.error(msg)
            ipns = []
    return ipns


def format_access_list(access_string, deny_access=None):
    """Transform access string into a format ganesha understands."""
    ipaddrs = set()
    deny_ipaddrs = set()
    # handle the case where there is an access string with a trailing comma
    access_string = access_string.strip(',')
    iptokens = access_string.split(',')

    if deny_access:
        for deny_token in deny_access.split(','):
            deny_ipns = _convert_ipstring_to_ipn(deny_token)
            for deny_ipn in deny_ipns:
                deny_ips = [ip for ip in netaddr.iter_unique_ips(deny_ipn)]
                deny_ipaddrs = deny_ipaddrs.union(deny_ips)

    for ipstring in iptokens:
        ipn_list = _convert_ipstring_to_ipn(ipstring)
        for ipn in ipn_list:
            ips = [ip for ip in netaddr.iter_unique_ips(ipn)]
            ipaddrs = ipaddrs.union(ips)

        ipaddrs = ipaddrs - deny_ipaddrs
        ipaddrlist = sorted(list(ipaddrs))
    return ','.join([str(ip) for ip in ipaddrlist])


def _publish_local_config(configpath, pre_lines, exports):
    save_path = '%s.sav.%s' % (configpath, time.time())
    LOG.info(_('Save backup copy of the Ganesha config at %s') % save_path)
    cpcmd = ['cp', configpath, save_path]
    try:
        utils.execute(*cpcmd, run_as_root=True)
    except exception.ProcessExecutionError:
        msg = (_('Failed while publishing ganesha config locally.'))
        LOG.error(msg)
        raise exception.GPFSGaneshaException(msg)

    tmp_path = '%s.tmp.%s' % (configpath, time.time())
    LOG.debug("tmp_path = %s" % tmp_path)
    cpcmd = ['cp', configpath, tmp_path]
    try:
        utils.execute(*cpcmd, run_as_root=True)
    except exception.ProcessExecutionError:
        msg = (_('Failed while publishing ganesha config locally.'))
        LOG.error(msg)
        raise exception.GPFSGaneshaException(msg)

    # change permission of the tmp file, so that it can be edited
    # by a non-root user
    chmodcmd = ['chmod', 'o+w', tmp_path]
    try:
        utils.execute(*chmodcmd, run_as_root=True)
    except exception.ProcessExecutionError:
        msg = (_('Failed while publishing ganesha config locally.'))
        LOG.error(msg)
        raise exception.GPFSGaneshaException(msg)

    with open(tmp_path, 'w+') as f:
        for l in pre_lines:
            f.write('%s\n' % l)
        for e in exports:
            f.write('EXPORT\n{\n')
            for attr in exports[e]:
                f.write('%s = %s ;\n' % (attr, exports[e][attr]))

            f.write('}\n')
    mvcmd = ['mv', tmp_path, configpath]
    try:
        utils.execute(*mvcmd, run_as_root=True)
    except exception.ProcessExecutionError:
        msg = (_('Failed while publishing ganesha config locally.'))
        LOG.error(msg)
        raise exception.GPFSGaneshaException(msg)
    LOG.info(_('Ganesha config %s published locally.') % configpath)


def _publish_remote_config(server, sshlogin, sshkey, configpath):
    dest = '%s@%s:%s' % (sshlogin, server, configpath)
    scpcmd = ['scp', '-i', sshkey, configpath, dest]
    try:
        utils.execute(*scpcmd, run_as_root=False)
    except exception.ProcessExecutionError:
        msg = (_('Failed while publishing ganesha config on remote server.'))
        LOG.error(msg)
        raise exception.GPFSGaneshaException(msg)
    LOG.info(_('Ganesha config %(path)s published to %(server)s.') %
             {'path': configpath,
              'server': server})


def publish_ganesha_config(servers, sshlogin, sshkey, configpath,
                           pre_lines, exports):
    """Publish the specified configuration information.

    Save the existing configuration file and then publish a new
    ganesha configuration to the specified path.  The pre-export
    lines are written first, followed by the collection of export
    definitions.
    """
    _publish_local_config(configpath, pre_lines, exports)

    localserver_iplist = socket.gethostbyname_ex(socket.gethostname())[2]
    for gsvr in servers:
        if gsvr not in localserver_iplist:
            _publish_remote_config(gsvr, sshlogin, sshkey, configpath)


def reload_ganesha_config(servers, sshlogin, dbport, service='ganesha.nfsd'):
    """Request ganesha server reload updated config."""

    # Note:  dynamic reload of ganesha config is not enabled
    # in ganesha v2.0. The code here correctly requests the
    # reload, but the request is ignored by the server for now
    # Create an object that will proxy for a particular remote object.
    localhost_iplist = socket.gethostbyname_ex(socket.gethostname())[2]
    for server in servers:
        bus = None
        if server in localhost_iplist:
            try:
                bus = dbus.SystemBus()
            except dbus.exceptions.DBusException as e:
                LOG.info(_('Local DBus Connection failed: %s') % e)
        else:
            hoststring = 'tcp:host=%s,port=%s' % (server, dbport)
            try:
                bus = dbus.bus.BusConnection(hoststring)
            except dbus.exceptions.DBusException as e:
                LOG.info(_('Remote DBus Connection failed: %s') % e)

        reload_status = False
        if bus:
            try:
                cbsim = bus.get_object('org.ganesha.nfsd',
                                       '/org/ganesha/nfsd/admin')

                LOG.info(_('Request config file reload on %s using dbus') %
                         server)
                reload = cbsim.get_dbus_method('reload',
                                               'org.ganesha.nfsd.admin')
                reload_status, msg = reload()
                LOG.info(_('DBus reload completed: %s') % msg)

            except dbus.exceptions.DBusException as e:
                LOG.info(_('DBus reload failed: %s') % e)

        # Until reload is fully implemented and if the reload returns a bad
        # status revert to service restart instead
        if not reload_status or True:
            LOG.info(_('Restart service %(service)s on %(server)s to force a '
                       'config file reload') %
                     {'service': service, 'server': server})
            run_local = True

            reload_cmd = ['service', service, 'restart']
            localserver_iplist = socket.gethostbyname_ex(
                socket.gethostname())[2]
            if server not in localserver_iplist:
                remote_login = sshlogin + '@' + server
                reload_cmd = ['ssh', remote_login] + reload_cmd
                run_local = False
            try:
                utils.execute(*reload_cmd, run_as_root=run_local)
            except exception.ProcessExecutionError as e:
                LOG.error(_('Could not restart service %(service)s on '
                            '%(server)s: %(excmsg)s') %
                          {'service': service,
                           'server': server,
                           'excmsg': str(e)})
